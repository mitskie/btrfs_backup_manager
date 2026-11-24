"""
Тесты загрузки конфигурации и независимости источников: --config
(альтернативный файл настроек), отсутствие/пустота SUBVOLUMES в режимах,
метаданные, резервное копирование с индивидуальными каталогами источников
и защита от пустого списка. Управление профилями удалено (ТЗ §3.1).
Запуск: python3 tests/test_appconfig.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    PASSWORD, RunIdGen, Sandbox, Suite, patch_run_id, run_main,
    silent_reporter,
)
import config  # noqa: E402
import main as main_module  # noqa: E402
from core import (  # noqa: E402
    BtrfsBackupError, ConfigError, StorageError,
)
from core import appconfig as appconfig_module  # noqa: E402
from core import backup as bk  # noqa: E402
from core import metadata as metadata_module  # noqa: E402
from core import verification as verification_module  # noqa: E402
from core.appconfig import AppConfig  # noqa: E402

import common  # noqa: E402


def _config_file(path, box, *, subvolumes, snapshot_dir=None,
                  local_backup_dir=None, network=False, password=PASSWORD,
                  extra=""):
    """Файл профиля, указывающий на каталоги песочницы."""
    snapshot_dir = snapshot_dir or str(box.snapshot_dir)
    local_backup_dir = local_backup_dir or str(box.backup_dir)
    lines = [
        "SUBVOLUMES = {}".format(json.dumps(subvolumes)),
        "SNAPSHOT_DIR = {}".format(json.dumps(snapshot_dir)),
        "LOCAL_BACKUP_DIR = {}".format(json.dumps(local_backup_dir)),
        "COMPRESSION = 'gzip'",
        "GPG_PASSWORD = {}".format(json.dumps(password)),
        "NETWORK_ENABLED = {}".format(repr(network)),
        "NETWORK_MOUNT = {}".format(json.dumps(str(box.network_mount))),
        "NETWORK_DIRECTORY = 'btrfs-backups'",
        "RETENTION_COUNT = 5",
        "PROCESS_TIMEOUT = 120",
        "LOG_FILE = {}".format(
            json.dumps(str(box.root / "profile.log"))),
        "LOCK_FILE = {}".format(json.dumps(str(box.root / "profile.lock"))),
        extra,
    ]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Path(path)


def main():
    suite = Suite("appconfig")

    # =============================================
    # load_config: отсутствующий файл --config — ошибка (ТЗ 2.3)
    # =============================================
    try:
        appconfig_module.load_config("/absent/profile.py")
        suite.check(False, "--config с несуществующим файлом должен падать")
    except ConfigError as exc:
        suite.check("не найден" in str(exc),
                    "--config: отсутствующий файл — понятная ошибка "
                    "конфигурации")

    # =============================================
    # load_config без backup_config.py: TUI не блокируется (ТЗ 2.1-2.2)
    # =============================================
    cfg = AppConfig(dict(appconfig_module.DEFAULTS), path=None,
                    profile_name="основной", config_found=False)
    suite.check(cfg.config_found is False and cfg.SUBVOLUMES is None,
                "профиль без файла: SUBVOLUMES не задан (не ошибка для TUI)")
    errors, warnings = cfg.validate_config(strict=False)
    suite.check(errors == [] and any("SUBVOLUMES" in w for w in warnings),
                "не-строгая валидация без конфигурации — предупреждения, "
                "не ошибки")
    errors, _ = cfg.validate_config(strict=True)
    suite.check(any("SUBVOLUMES" in e for e in errors) and
                any("SNAPSHOT_DIR" in e for e in errors) and
                any("NETWORK_MOUNT" in e for e in errors),
                "строгая валидация требует SUBVOLUMES/SNAPSHOT_DIR/"
                "NETWORK_MOUNT (ТЗ 2.3)")

    # =============================================
    # read_config_values: синтаксическая ошибка — ConfigError с строкой
    # =============================================
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "bad.py"
        bad.write_text("SUBVOLUMES = [Syntax error here\n", encoding="utf-8")
        try:
            appconfig_module.read_config_values(bad)
            suite.check(False, "синтаксическая ошибка должна обнаруживаться")
        except ConfigError as exc:
            suite.check("строка" in str(exc),
                        "ошибка синтаксиса профиля — ConfigError со строкой")
        # Отсутствующие ключи — значения по умолчанию
        partial = Path(tmp) / "partial.py"
        partial.write_text("COMPRESSION = 'gzip'\n", encoding="utf-8")
        values = appconfig_module.read_config_values(partial)
        suite.check(values["COMPRESSION"] == "gzip" and
                    values["SUBVOLUMES"] is None and
                    values["RETENTION_COUNT"] == 5,
                    "частичный профиль: отсутствующие ключи — по умолчанию")

    # =============================================
    # normalize_subvolumes: индивидуальные пути и их ошибки (ТЗ 3.3)
    # =============================================
    items, errors = appconfig_module.normalize_subvolumes([
        {"name": "root", "source": "/", "snapshot_dir": "/.snapshots",
         "local_backup_dir": "/var/backups/root"},
        "/home",
        {"name": "bad-rel", "source": "/x", "snapshot_dir": "relative"},
        {"name": "bad-empty", "source": "/y", "local_backup_dir": "  "},
    ])
    suite.check(items[0]["snapshot_dir"] == "/.snapshots" and
                items[0]["local_backup_dir"] == "/var/backups/root",
                "словарная форма с индивидуальными путями нормализуется")
    suite.check(len(errors) == 2,
                "некорректные индивидуальные пути дают ровно две ошибки")
    suite.check(items[1]["snapshot_dir"] is None and
                items[1]["local_backup_dir"] is None,
                "строковая форма — индивидуальные пути не заданы")
    suite.check(any("абсолютным путем" in e for e in errors) and
                any("пустая строка" in e for e in errors),
                "некорректные индивидуальные пути — ошибки")

    # =============================================
    # validate_values: пересечения индивидуальных путей (ТЗ 3.1)
    # =============================================
    values = dict(appconfig_module.DEFAULTS)
    values.update({
        "SUBVOLUMES": [{"name": "a", "source": "/src",
                        "snapshot_dir": "/src"}],
        "SNAPSHOT_DIR": "/snaps",
        "LOCAL_BACKUP_DIR": "/backups",
        "NETWORK_MOUNT": "/mnt/backup",
        "GPG_PASSWORD": "real",
        "LOCK_FILE": "/run/lock/x",
        "LOG_FILE": "/var/log/x",
    })
    errors, _ = appconfig_module.validate_values(values, strict=True)
    suite.check(any("совпадает с 'source'" in e for e in errors),
                "snapshot_dir совпадает с source — синтаксическая ошибка")

    # =============================================
    # Метаданные: поле profile (ТЗ 5, документирование формата)
    # =============================================
    meta = metadata_module.new_metadata(
        archive_name="a.gz.gpg", subvolume_name="root", subvolume_path="/",
        run_id="2026-03-01_000100_000001", compression="gzip", encrypted=True,
        snapshot_id="root_2026-03-01_000100_000001", profile="laptop")
    suite.check(meta["profile"] == "laptop",
                "new_metadata сохраняет имя профиля")
    from core import APP_VERSION
    suite.check(meta["app_version"] == APP_VERSION,
                "метаданные помечены актуальной версией приложения")
    meta_old = metadata_module.new_metadata(
        archive_name="a.gz.gpg", subvolume_name="root", subvolume_path="/",
        run_id="2026-03-01_000100_000002", compression="gzip", encrypted=True,
        snapshot_id="root_2026-03-01_000100_000002")
    suite.check(meta_old["profile"] is None,
                "без профиля поле равно None (совместимость)")

    # =============================================
    # Backup с индивидуальными каталогами источников (ТЗ 3.3)
    # =============================================
    with Sandbox() as box:
        own_snaps = box.root / "home-snaps"
        own_backup = box.root / "home-backups"
        src_root = box.make_subvolume("src-root", b"root-data" * 100)
        src_home = box.make_subvolume("src-home", b"home-data" * 100)
        common.NETMOUNT_IS_MOUNTED[0] = False
        patch_run_id(RunIdGen(start=700))
        # Песочница «на Btrfs»: точка монтирования для fallback UUID
        from core import snapshots as sm
        sm.mount_details_for = lambda p: (
            str(box.root), "btrfs", "/dev/loop0")
        cfg = appconfig_module.from_module(config)
        cfg.set_value("SNAPSHOT_DIR", str(box.snapshot_dir))
        cfg.set_value("LOCAL_BACKUP_DIR", str(box.backup_dir))
        cfg.set_value("NETWORK_ENABLED", False)
        items = [
            {"name": "root", "source": str(src_root),
             "snapshot_dir": None, "local_backup_dir": None},
            {"name": "home", "source": str(src_home),
             "snapshot_dir": str(own_snaps),
             "local_backup_dir": str(own_backup)},
        ]
        report = bk.run_backup(
            cfg, non_interactive=True, reporter=silent_reporter(),
            items_override=items, lock=False)
        suite.check(report.archives_created == 2,
                    "backup двух источников с разными каталогами выполнен")
        root_archives = list(box.backup_dir.glob("*root_*.gpg"))
        home_archives = list(own_backup.glob("*home_*.gpg"))
        suite.check(len(root_archives) == 1,
                    "архив root — в глобальном каталоге архивов")
        suite.check(len(home_archives) == 1,
                    "архив home — в ИНДИВИДУАЛЬНОМ каталоге архивов")
        home_snaps = list(own_snaps.glob("*home_*"))
        suite.check(len(home_snaps) == 1 and
                    (home_snaps[0] / ".shim-subvolume").exists(),
                    "снапшот home — в индивидуальном каталоге снапшотов")
        root_snaps = list(box.snapshot_dir.glob("*root_*"))
        suite.check(len(root_snaps) == 1,
                    "снапшот root — в глобальном каталоге снапшотов")
        meta = metadata_module.read_metadata(home_archives[0])
        suite.check(meta.get("compression") == "zstd",
                    "метаданные содержат фактический алгоритм сжатия")

    # =============================================
    # Пустой итоговый список не запускает операцию (ТЗ 4)
    # =============================================
    try:
        bk._validate_override_items([])
        suite.check(False, "пустой items_override должен запрещаться")
    except ConfigError as exc:
        suite.check("Все источники исключены" in str(exc),
                    "пустой список — «Все источники исключены...» (ТЗ 4)")

    # =============================================
    # main(): оба порядка --config (ТЗ 2.3) — локальный backup без сети
    # =============================================
    for order, argv in (
            ("config-после-команды", None),
            ("config-до-команды", None)):
        with Sandbox() as box:
            src = box.make_subvolume("src-main", b"main" * 200)
            common.NETMOUNT_IS_MOUNTED[0] = False
            patch_run_id(RunIdGen(start=800))
            profile = _config_file(
                box.root / "profile-main.py", box,
                subvolumes=[{"name": "main", "source": str(src)}],
                network=False)
            if order == "config-после-команды":
                argv = ["backup", "-n", "--config", str(profile)]
            else:
                argv = ["--config", str(profile), "backup", "-n"]
            code = main_module.main(argv)
            suite.check(code == 0,
                        "backup --config ({}) завершен успешно".format(order))
            archives = list(box.backup_dir.glob("*main_*.gpg"))
            suite.check(len(archives) == 1,
                        "({}) архив создан по профилю".format(order))
            meta = metadata_module.read_metadata(archives[0])
            suite.check(meta and meta.get("profile") == "profile-main",
                        "({}) профиль записан в метаданные".format(order))
            main_module.locking.release_lock()

    # --config с отсутствующим файлом: код 2 (ТЗ 2.3)
    for order, argv in (
            ("после", ["backup", "-n", "--config", "/absent/x.py"]),
            ("до", ["--config", "/absent/x.py", "backup", "-n"])):
        code = main_module.main(argv)
        suite.check(code == 2,
                    "--config с отсутствующим файлом ({}): код 2".format(order))
        main_module.locking.release_lock()

    # --config без значения: код 2
    code = main_module.main(["backup", "--config"])
    suite.check(code == 2, "--config без пути: код 2")
    main_module.locking.release_lock()

    # =============================================
    # CLI строгость: пустой SUBVOLUMES в профиле — ошибка (ТЗ 2.3)
    # =============================================
    with Sandbox() as box:
        profile = _config_file(box.root / "empty.py", box, subvolumes=[],
                                network=False)
        code = main_module.main(["backup", "-n", "--config", str(profile)])
        suite.check(code == 2,
                    "пустой SUBVOLUMES профиля: код 2 без вопросов")
        main_module.locking.release_lock()
        # В интерактивной валидации пустой SUBVOLUMES — не ошибка (ТЗ 2.2)
        cfg = appconfig_module.load_config(str(profile))
        errors, _ = cfg.validate_config(strict=False)
        suite.check(errors == [],
                    "TUI-валидация профиля с пустым SUBVOLUMES без ошибок")

    # =============================================
    # Независимость альтернативных файлов настроек: архивы не смешиваются
    # =============================================
    with Sandbox() as box:
        src = box.make_subvolume("src-solo", b"solo" * 200)
        common.NETMOUNT_IS_MOUNTED[0] = False
        patch_run_id(RunIdGen(start=900))
        profile_a = _config_file(box.root / "a.py", box,
                                  subvolumes=[{"name": "solo",
                                               "source": str(src)}],
                                  network=False,
                                  extra="RETENTION_COUNT = 3")
        code = main_module.main(["backup", "-n", "--config", str(profile_a)])
        main_module.locking.release_lock()
        suite.check(code == 0, "конфиг A: backup успешен")
        archives_a = list(box.backup_dir.glob("*solo_*.gpg"))
        suite.check(len(archives_a) == 1, "конфиг A: архив на месте")
        # Профиль B: другой каталог архивов
        own_backup_b = box.root / "backup-b"
        profile_b = _config_file(box.root / "b.py", box,
                                  subvolumes=[{"name": "solo",
                                               "source": str(src)}],
                                  local_backup_dir=str(own_backup_b),
                                  network=False)
        code = main_module.main(["backup", "-n", "--config", str(profile_b)])
        main_module.locking.release_lock()
        suite.check(code == 0, "конфиг B: backup успешен")
        archives_b = list(own_backup_b.glob("*solo_*.gpg"))
        suite.check(len(archives_b) == 1,
                    "конфиг B: архив в СВОЕМ каталоге (не смешиваются)")
        suite.check(len(archives_a) == 1,
                    "архив профиля A не затронут профилем B")
        meta_a = metadata_module.read_metadata(archives_a[0])
        meta_b = metadata_module.read_metadata(archives_b[0])
        suite.check(meta_a["profile"] == "a" and meta_b["profile"] == "b",
                    "каждый архив помечен своим файлом настроек")

    return suite.summary()


if __name__ == "__main__":
    sys.exit(run_main(main))
