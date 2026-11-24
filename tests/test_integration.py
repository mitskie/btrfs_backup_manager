"""
Интеграционный тест полного цикла (ТЗ 20.9) на эмуляторе btrfs:
данные -> снапшот -> архив -> проверка -> сеть -> повторная отправка ->
восстановление -> сравнение -> retention.
Запуск: python3 tests/test_integration.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    RunIdGen, Sandbox, Suite, patch_run_id, run_main, silent_reporter,
)
import config  # noqa: E402
from core import backup as bk  # noqa: E402
from core import metadata as md  # noqa: E402
from core import network as net  # noqa: E402
from core import restore as rs  # noqa: E402
from core import retention as rt  # noqa: E402
from core import snapshots as sm  # noqa: E402
from core import verification as vr  # noqa: E402

import common  # noqa: E402

PAYLOAD_ROOT = os.urandom(180000) + b"INTEGRATION-ROOT" * 500
PAYLOAD_HOME = os.urandom(90000) + b"INTEGRATION-HOME" * 300


def main():
    suite = Suite("integration")

    with Sandbox() as box:
        config.RETENTION_COUNT = 3  # после Sandbox (он ставит значение 5)
        # 1-2. Тестовые данные + read-only снапшоты выполняются backup'ом
        box.make_subvolume("src-root", PAYLOAD_ROOT)
        box.make_subvolume("src-home", PAYLOAD_HOME)
        common.NETMOUNT_IS_MOUNTED[0] = False  # 8. имитация отсутствующей сети
        patch_run_id(RunIdGen(start=100))

        # 3-7. Полный локальный backup (снапшоты, архивы, проверка)
        report = bk.run_backup(config, non_interactive=True,
                               reporter=silent_reporter())
        local = sorted(p for p in box.backup_dir.glob("*.gpg"))
        suite.check(len(local) == 2,
                    "шаги 1-7: два локальных архива созданы и проверены")
        suite.check(report.snapshots_created == 2,
                    "два read-only снапшота")
        for path in local:
            meta = md.read_metadata(path)
            suite.check(meta["status"] == "upload-pending" and meta["sha256"],
                        "{}: статус upload-pending, SHA-256 зафиксирован".
                        format(path.name))
            suite.check(vr.verify_checksum(path, meta["sha256"]) ==
                        meta["sha256"],
                        "{}: контрольная сумма сверяется фактически".format(
                            path.name))
        suite.check(not any(box.network_dir.glob("*")),
                    "шаг 8: без сети на шару ничего не попало")

        # 9-10. Подключение «шары» и повторная отправка
        common.NETMOUNT_IS_MOUNTED[0] = True
        report = bk.upload_pending(config, non_interactive=True,
                                   reporter=silent_reporter())
        net_files = sorted(p.name for p in box.network_dir.glob("*.gpg"))
        suite.check(len(net_files) == 2 and not local[0].exists(),
                    "шаги 9-10: повторная отправка после подключения сети")

        # 11. Проверка SHA-256 сетевых копий
        for name in net_files:
            net_path = box.network_dir / name
            # v1.3: эталон сетевой копии — JSON-метаданные (без .sha256)
            expected = (md.read_metadata(net_path) or {}).get("sha256")
            suite.check(bool(expected), "сетевые JSON-метаданные есть")
            suite.check(vr.sha256_file(net_path) == expected,
                        "шаг 11: SHA-256 сетевой копии {} подтвержден".format(
                            name))

        # 12-13. Восстановление и сравнение с исходными данными
        # (v1.3: имена начинаются с префикса хоста — subvolume выделяется
        # разбором имени, а не startswith)
        def _by_subvolume(names, sub):
            for candidate in names:
                parsed = md.parse_archive_name(candidate)
                if parsed and parsed["subvolume"] == sub:
                    return candidate
            raise AssertionError(
                "архив subvolume '{}' не найден среди {}".format(sub, names))

        root_archive = box.network_dir / _by_subvolume(net_files, "root")
        home_archive = box.network_dir / _by_subvolume(net_files, "home")
        dest_root = rs.receive_archive(root_archive, box.restore_dir,
                                       gpg_binary=config.GPG_BINARY,
                                       password=config.GPG_PASSWORD)
        dest_home = rs.receive_archive(home_archive, box.restore_dir,
                                       gpg_binary=config.GPG_BINARY,
                                       password=config.GPG_PASSWORD)
        suite.check((dest_root / ".shim-data").read_bytes() == PAYLOAD_ROOT,
                    "шаг 13: восстановленный root идентичен исходным данным")
        suite.check((dest_home / ".shim-data").read_bytes() == PAYLOAD_HOME,
                    "шаг 13: восстановленный home идентичен исходным данным")

        # Смена COMPRESSION не ломает восстановление старых копий (ТЗ 6.4)
        config.COMPRESSION = "gzip"
        try:
            alt_target = box.restore_dir / "after-config-change"
            alt_target.mkdir(exist_ok=True)
            dest2 = rs.receive_archive(
                root_archive, alt_target,
                gpg_binary=config.GPG_BINARY, password=config.GPG_PASSWORD)
            suite.check((dest2 / ".shim-data").read_bytes() == PAYLOAD_ROOT,
                        "восстановление не зависит от текущего COMPRESSION")
        finally:
            config.COMPRESSION = "zstd"

        # 14. Retention: создаем еще копии сверх RETENTION_COUNT=3
        patch_run_id(RunIdGen(start=200))
        for i in range(3):
            common.NETMOUNT_IS_MOUNTED[0] = True
            bk.run_backup(config, non_interactive=True,
                          reporter=silent_reporter())
        root_snaps = sm.list_snapshots(box.snapshot_dir, {"root"})["root"]
        suite.check(len(root_snaps) <= 3 + 1,
                    "шаг 14: retention держит не больше RETENTION_COUNT "
                    "(+1 новая неподтвержденная)")
        # Все оставшиеся снапшоты root имеют подтвержденные копии либо новые
        confirmed = [s for s in root_snaps if net.network_archive_confirmed(
            box.network_dir, s.name)]
        suite.check(len(confirmed) <= 3,
                    "подтвержденных копий не больше RETENTION_COUNT")
        # Сетевые архивы никуда не делись (ТЗ 7.6)
        suite.check(len(net_files) <=
                    len(list(box.network_dir.glob("*.gpg"))),
                    "сетевые архивы никогда не удаляются автоматически")

        # Итоговые статусы метаданных последнего backup: upload-pending
        # (последние архивы не отправлялись отдельно)
        latest = sorted(p for p in box.backup_dir.glob("*.gpg"))
        for path in latest:
            meta = md.read_metadata(path)
            suite.check(meta["status"] in ("upload-pending", "uploaded"),
                        "статус последнего архива корректен: {} ({})".format(
                            meta["status"], path.name))

    return suite.summary()


if __name__ == "__main__":
    sys.exit(run_main(main))
