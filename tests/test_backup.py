"""
Тесты резервного копирования (ТЗ 5, 7, 20.3): локальный backup без сети,
upload-pending, сохранность при ошибках.
Запуск: python3 tests/test_backup.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    PASSWORD, RunIdGen, Sandbox, Suite, confirm_false, confirm_true,
    patch_run_id, run_main, silent_reporter,
)
import config  # noqa: E402
from core import (  # noqa: E402
    BtrfsBackupError, StorageError, UtilityMissingError,
)
from core import backup as bk  # noqa: E402
from core import metadata as md  # noqa: E402
from core import snapshots as sm  # noqa: E402
from core import verification as vr  # noqa: E402

import common  # noqa: E402


def _local_archives(box):
    return sorted(p for p in box.backup_dir.glob("*.gpg") if p.is_file())


def main():
    suite = Suite("backup")

    # =============================================
    # 20.3-1/5/6: локальный backup без сети
    # =============================================
    with Sandbox() as box:
        box.make_subvolume("src-root", b"root-data" * 500)
        box.make_subvolume("src-home", b"home-data" * 300)
        common.NETMOUNT_IS_MOUNTED[0] = False  # шара не подключена
        gen = RunIdGen()
        patch_run_id(gen)

        report = bk.run_backup(config, non_interactive=True,
                               reporter=silent_reporter())
        archives = _local_archives(box)
        suite.check(len(archives) == 2,
                    "20.3-1: без сети созданы оба локальных архива")
        suite.check(all(a.name.endswith(".zst.gpg") for a in archives),
                    "сжатие по config (zstd-шим): расширение .zst.gpg")
        suite.check(report.snapshots_created == 2 and
                    report.archives_created == 2,
                    "отчет: снапшотов 2, архивов 2")
        suite.check(report.pending_local == 2,
                    "20.3-6: статус upload-pending (оба архива)")
        suite.check(report.archives_uploaded == 0,
                    "без сети отправок нет")
        # Статусы в метаданных
        statuses = {a.name: md.read_metadata(a)["status"] for a in archives}
        suite.check(all(s == "upload-pending" for s in statuses.values()),
                    "метаданные: статус upload-pending (ТЗ 8)")
        # Локальные снапшоты остались (ТЗ 9.2)
        suite.check(len(sm.list_snapshots(box.snapshot_dir)["root"]) == 1,
                    "20.3: локальный снапшот остается после создания архива")
        # Снапшоты read-only
        snaps = sm.list_snapshots(box.snapshot_dir)
        suite.check(all(sm.subvolume_ro_property(s.path) is True
                        for items in snaps.values() for s in items),
                    "созданные снапшоты read-only")
        # Контрольные суммы и проверяемость (v1.3: эталон в JSON-метаданных)
        for a in archives:
            expected = md.read_metadata(a)["sha256"]
            sha = vr.verify_checksum(a, expected)
            suite.check(sha == expected,
                        "SHA-256 архива {} зафиксирован и сверяется "
                        "фактически".format(a.name))
            suite.check(not Path(str(a) + ".sha256").exists(),
                        "v1.3: отдельный .sha256 локально не создается")

    # =============================================
    # 20.3-3: пользователь подтверждает локальный backup
    # =============================================
    with Sandbox() as box:
        box.make_subvolume("src-root", b"x" * 1000)
        box.make_subvolume("src-home", b"h" * 800)
        common.NETMOUNT_IS_MOUNTED[0] = False
        patch_run_id(RunIdGen())
        report = bk.run_backup(config, non_interactive=False,
                               confirm_fn=confirm_true,
                               reporter=silent_reporter())
        suite.check(len(_local_archives(box)) == 2,
                    "20.3-3: интерактивное подтверждение -> локальный backup")

    # 20.3-4: пользователь отменяет — операция не начинается
    with Sandbox() as box:
        box.make_subvolume("src-root", b"x" * 1000)
        box.make_subvolume("src-home", b"h" * 800)
        common.NETMOUNT_IS_MOUNTED[0] = False
        patch_run_id(RunIdGen())
        from core import OperationCancelled
        try:
            bk.run_backup(config, non_interactive=False,
                          confirm_fn=confirm_false,
                          reporter=silent_reporter())
            suite.check(False, "отмена должна прерывать операцию")
        except OperationCancelled:
            suite.check(True, "20.3-4: отказ пользователя отменяет операцию")
        suite.check(not _local_archives(box),
                    "20.3-4: после отмены архивов нет")
        suite.check(not any(sm.list_snapshots(box.snapshot_dir).values()),
                    "20.3-4: снапшоты после отмены не созданы")

    # =============================================
    # 20.4-1/2: сеть доступна — отправка, подтверждение, удаление локальной
    # =============================================
    with Sandbox() as box:
        box.make_subvolume("src-root", b"upload-me" * 400)
        box.make_subvolume("src-home", b"home-upload" * 300)
        common.NETMOUNT_IS_MOUNTED[0] = True
        patch_run_id(RunIdGen(start=10))
        report = bk.run_backup(config, non_interactive=True,
                               reporter=silent_reporter())
        net_files = sorted(p.name for p in box.network_dir.glob("*.gpg"))
        suite.check(len(net_files) == 2,
                    "20.4-1: архивы отправлены на шару")
        suite.check(not _local_archives(box),
                    "локальный архив удален после подтвержденной отправки "
                    "(ТЗ 9.4)")
        net_archive = box.network_dir / net_files[0]
        suite.check((box.network_dir / (net_files[0] + ".sha256")).is_file()
                    is False,
                    "v1.3: отдельный .sha256 на шаре не создается")
        suite.check((box.network_dir / (net_files[0] + ".json")).is_file(),
                    "на шаре есть метаданные со статусом uploaded")
        net_meta = md.read_metadata(net_archive) or {}
        suite.check(net_meta.get("status") == "uploaded",
                    "статус сетевой копии uploaded (JSON)")
        suite.check(net_meta.get("sha256") and
                    vr.sha256_file(net_archive) == net_meta["sha256"],
                    "20.4-2: SHA-256 сетевой копии совпадает с JSON")
        suite.check(report.archives_uploaded == 2 and
                    report.pending_local == 0,
                    "отчет: отправлено 2, ожидающих 0")

    # AUTO_UPLOAD=False -> архивы остаются локально (ТЗ 7.4)
    with Sandbox() as box:
        box.make_subvolume("src-root", b"x" * 500)
        box.make_subvolume("src-home", b"home-x" * 400)
        common.NETMOUNT_IS_MOUNTED[0] = True
        config.AUTO_UPLOAD = False
        try:
            patch_run_id(RunIdGen(start=20))
            report = bk.run_backup(config, non_interactive=True,
                                   reporter=silent_reporter())
            suite.check(len(_local_archives(box)) == 2 and
                        not any(box.network_dir.glob("*.gpg")),
                        "AUTO_UPLOAD=false: отправки нет, архивы локальны")
            statuses = {md.read_metadata(a)["status"]
                        for a in _local_archives(box)}
            suite.check(statuses == {"upload-pending"},
                        "AUTO_UPLOAD=false: статус upload-pending")
        finally:
            config.AUTO_UPLOAD = True

    # =============================================
    # 20.4-3/4/7: ошибка отправки — локальный архив сохранен
    # =============================================
    with Sandbox() as box:
        box.make_subvolume("src-root", b"fail-upload" * 300)
        box.make_subvolume("src-home", b"home-fail" * 200)
        common.NETMOUNT_IS_MOUNTED[0] = True
        patch_run_id(RunIdGen(start=30))
        # Имитация сбоя сети при копировании
        real_copy = bk.copy_to_network_tmp
        def failing_copy(local_archive, net_dir, expected_sha):
            raise StorageError("Сеть недоступна во время копирования")
        bk.copy_to_network_tmp = failing_copy
        try:
            bk.run_backup(config, non_interactive=True,
                          reporter=silent_reporter())
            suite.check(False, "ошибка отправки должна финализировать ошибкой")
        except StorageError:
            suite.check(True, "ошибка отправки -> код ошибки сети (7)")
        finally:
            bk.copy_to_network_tmp = real_copy
        archives = _local_archives(box)
        suite.check(len(archives) == 2,
                    "20.4-3: ЛОКАЛЬНЫЕ АРХИВЫ СОХРАНЕНЫ при ошибке отправки")
        statuses = {md.read_metadata(a)["status"] for a in archives}
        suite.check(statuses == {"upload-failed"},
                    "статус upload-failed зафиксирован")
        suite.check(all(
            vr.verify_checksum(a, md.read_metadata(a)["sha256"]) ==             md.read_metadata(a)["sha256"] for a in archives),
                    "архивы после ошибки отправки целы (SHA-256 совпадает)")

    # =============================================
    # 20.3-8: неудачная проверка не удаляет архив (повреждение на лету)
    # =============================================
    with Sandbox() as box:
        box.make_subvolume("src-root", b"corrupt-check" * 200)
        box.make_subvolume("src-home", b"home-corrupt" * 150)
        common.NETMOUNT_IS_MOUNTED[0] = False
        patch_run_id(RunIdGen(start=40))
        # Подменяем проверку так, будто архив поврежден
        real_verify = bk.verification.test_decrypted_stream
        def failing_verify(*a, **k):
            raise vr.VerificationError("Сжатый поток поврежден")
        bk.verification.test_decrypted_stream = failing_verify
        try:
            bk.run_backup(config, non_interactive=True,
                          reporter=silent_reporter())
            suite.check(False, "повреждение должно останавливать backup")
        except vr.VerificationError:
            suite.check(True, "ошибка проверки останавливает backup")
        finally:
            bk.verification.test_decrypted_stream = real_verify
        # ЧАСТИЧНЫЙ архив (создан до проверки) сохранен, метаданные созданы,
        # НО статус не verified и отправки не было
        archives = _local_archives(box)
        if archives:
            suite.check(md.read_metadata(archives[0])["status"] in
                        ("created", "verified"),
                        "архив не помечен отправленным при ошибке проверки")
        suite.check(not any(box.network_dir.glob("*")),
                    "на шару при ошибке проверки ничего не попало")

    # =============================================
    # 20.3-9: ошибка создания snapshot не оставляет ложный статус успеха
    # =============================================
    with Sandbox() as box:
        # Источник root есть, home отсутствует -> backup отменяется ДО
        # создания любых снапшотов (проверка расположения всех источников)
        box.make_subvolume("src-root", b"partial" * 100)
        # src-home не создаем
        common.NETMOUNT_IS_MOUNTED[0] = False
        patch_run_id(RunIdGen(start=50))
        try:
            bk.run_backup(config, non_interactive=True,
                          reporter=silent_reporter())
            suite.check(False, "отсутствующий источник должен отменять backup")
        except BtrfsBackupError as exc:
            suite.check(True,
                        "20.3-9: ошибка источника -> отказ до снапшотов")
            suite.check("снапшоты не создавались" in str(exc) or
                        "ДО создания снапшотов" in str(exc),
                        "сообщение объясняет, что снапшоты не созданы")
        suite.check(not any(sm.list_snapshots(box.snapshot_dir).values()),
                    "ни одного снапшота не создано (нет частичного backup)")
        suite.check(not _local_archives(box), "архивов нет")

    # =============================================
    # Fallback сжатия в реальном backup (ТЗ 6.2)
    # =============================================
    with Sandbox() as box:
        box.make_subvolume("src-root", b"gzip-fallback" * 300)
        box.make_subvolume("src-home", b"home-gzip" * 200)
        common.NETMOUNT_IS_MOUNTED[0] = False
        patch_run_id(RunIdGen(start=60))
        # Отдельный bin: btrfs-шим есть, zstd-шима НЕТ (zstd "не установлен")
        bin_dir = box.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        shim_btrfs = Path(__file__).resolve().parent / "btrfs"
        link = bin_dir / "btrfs"
        if not link.exists():
            try:
                os.symlink(str(shim_btrfs), str(link))
            except OSError:
                import shutil as _shutil
                _shutil.copy(str(shim_btrfs), str(link))
                os.chmod(str(link), 0o755)
        real_path = os.environ["PATH"]
        os.environ["PATH"] = str(bin_dir) + ":/usr/bin:/bin"
        try:
            import shutil as _shutil
            if not _shutil.which("gzip"):
                suite.skip("20.3: gzip недоступен вне шим-PATH")
            elif _shutil.which("zstd"):
                suite.skip("20.3: zstd найден вне шим-PATH, проверка невозможна")
            else:
                report = bk.run_backup(config, non_interactive=True,
                                       reporter=silent_reporter())
                archives = _local_archives(box)
                suite.check(len(archives) == 2 and
                            all(a.name.endswith(".gz.gpg") for a in archives),
                            "20.2-5: cron без zstd -> созданы gzip-архивы")
                metas = {md.read_metadata(a)["compression"]
                         for a in archives}
                suite.check(metas == {"gzip"},
                            "фактический алгоритм в метаданных: gzip")
                suite.check(report.compression_used == "gzip",
                            "отчет: фактическое сжатие gzip (в журнале)")
                vr.test_decrypted_stream(
                    archives[0], gpg_binary=config.GPG_BINARY,
                    password=PASSWORD, compression="gzip")
                suite.check(True, "gzip-архив проходит полную проверку")
        finally:
            os.environ["PATH"] = real_path

    # =============================================
    # Очистка временных файлов после прерывания
    # =============================================
    with Sandbox() as box:
        part = box.backup_dir / "root_2026-03-04_000001_ca0001.zst.gpg.part"
        part.write_bytes(b"partial")
        removed = bk.cleanup_tmp_files(config, reporter=silent_reporter())
        suite.check(removed >= 1 and not part.exists(),
                    "очистка удаляет *.part после прерванной операции")
        real_archive = box.backup_dir / "keep_2026-03-04_000002_ca0002.zst.gpg"
        real_archive.write_bytes(b"real")
        bk.cleanup_tmp_files(config, reporter=silent_reporter())
        suite.check(real_archive.exists(),
                    "очистка не трогает готовые архивы")

    # =============================================
    # v1.3.1: отчет с длительностями этапов + вывод через reporter
    # (замечание пользователя: "создание архива заняло 15 минут" — без
    # разбивки по этапам не видно, куда уходит время; в TUI прямой
    # print() портил нижнюю часть экрана после операции)
    # =============================================
    rep = bk.RunReport()
    rep.snapshots_created = 1
    rep.archives_created = 1
    rep.timings = {"snapshots": 1.2, "archives": 692.5, "verify": 171.0}
    rep.total_seconds = 870.0
    suite.check("создание архивов 11 мин 32 с" in rep.timing_summary(),
                "отчет: длительности этапов в человекочитаемом виде")
    lines = rep.summary_lines()
    suite.check(lines[0].startswith("Итог:"),
                "отчет: первая строка итога — Итог:")
    suite.check(all(len(line) <= 80 for line in lines),
                "отчет: строки итога помещаются на экран 80 колонок "
                "(не переносятся и не перекрывают подсказки)")
    suite.check(any("Время:" in line for line in lines),
                "отчет: длительности включены в итог для TUI")

    class _Collect:
        """Минимальный reporter, как CollectReporter TUI (не Console)."""
        def __init__(self):
            self.done_lines = []
        def done(self, text):
            self.done_lines.append(text)

    collect = _Collect()
    rep.emit(0.0, collect)
    suite.check(collect.done_lines == lines,
                "v1.3.1: в TUI отчет передается через reporter.done — "
                "без прямого print(), портившего экран")

    import io
    import contextlib
    buf_out = io.StringIO()
    with contextlib.redirect_stdout(buf_out):
        rep.emit(0.0, None)
    printed = buf_out.getvalue()
    suite.check("Итог:" in printed and "Время:" in printed,
                "в CLI отчет печатается в консоль, включая длительности")

    # v1.3.1: валидация items_override (выбор разделов интерфейсом)
    suite.expect_raises(
        lambda: bk._validate_override_items([{"name": "@", "source": "/@"}]),
        bk.ConfigError,
        "items_override: недопустимое имя '@' отклоняется до операций")
    suite.expect_raises(
        lambda: bk._validate_override_items([]),
        bk.ConfigError, "items_override: пустой список отклоняется")
    ok_items = bk._validate_override_items([
        {"name": "root", "source": "/"},
        {"name": "root", "source": "/"},
        {"name": "athome", "source": "/@home"}])
    suite.check(len(ok_items) == 2 and ok_items[1]["name"] == "athome",
                "items_override: дубликаты отбрасываются, список "
                "нормализован")

    # v1.3.1: items_override — бэкапируется ровно выбранное
    with Sandbox() as box:
        box.make_subvolume("src-root", b"root-data" * 500)
        box.make_subvolume("src-home", b"home-data" * 300)
        common.NETMOUNT_IS_MOUNTED[0] = False  # шара не подключена
        patch_run_id(RunIdGen())
        report = bk.run_backup(
            config, non_interactive=True, reporter=silent_reporter(),
            items_override=[{"name": "root",
                             "source": str(box.root / "src-root")}])
        archives = _local_archives(box)
        suite.check(len(archives) == 1 and "root_" in archives[0].name,
                    "items_override: архив создан только для выбранного "
                    "раздела")
        suite.check(report.snapshots_created == 1,
                    "items_override: снапшот только выбранного раздела")
        suite.check(report.timings.get("archives") is not None,
                    "отчет: длительность этапа архивов зафиксирована")

    # =============================================
    # ТЗ 3.5 п.3 (исправление №1): CHECK_NETWORK_MOUNT=False НЕ отключает
    # обязательную проверку монтирования — обычный локальный каталог в
    # NETWORK_MOUNT больше НЕ считается сетевой шарой.
    # =============================================
    with Sandbox() as box:
        box.make_subvolume("src-root", b"no-fake-share" * 200)
        common.NETMOUNT_IS_MOUNTED[0] = False
        config.CHECK_NETWORK_MOUNT = False  # попытка отключить проверку
        patch_run_id(RunIdGen(start=30))
        try:
            report = bk.run_backup(config, non_interactive=True,
                                   reporter=silent_reporter(),
                                   items_override=[{"name": "root",
                                                    "source": str(
                                                        box.root /
                                                        "src-root")}])
            suite.check(len(_local_archives(box)) == 1,
                        "ТЗ 3.5 п.3: архив создан ЛОКАЛЬНО (без отправки)")
            suite.check(not list(box.network_dir.glob("*.gpg")),
                        "ТЗ 3.5 п.3: несмонтированный каталог НЕ принял "
                        "архивы (отправки не было)")
            statuses = {a.name: md.read_metadata(a)["status"]
                        for a in _local_archives(box)}
            suite.check(list(statuses.values()) == ["upload-pending"],
                        "ТЗ 3.5 п.3: архив получил статус upload-pending")
        finally:
            config.CHECK_NETWORK_MOUNT = True

    # =============================================
    # ТЗ 5.4 пп.2/7 (исправление №3): ошибка записи сетевых метаданных
    # сохраняет локальный архив; повторный upload-pending завершает работу.
    # =============================================
    with Sandbox() as box:
        box.make_subvolume("src-root", b"meta-fail" * 300)
        common.NETMOUNT_IS_MOUNTED[0] = True
        patch_run_id(RunIdGen(start=40))
        real_wnm = bk.write_network_metadata
        bk.write_network_metadata = lambda *_a, **_k: False
        try:
            raised = False
            try:
                bk.run_backup(config, non_interactive=True,
                              reporter=silent_reporter(),
                              items_override=[{"name": "root",
                                               "source": str(
                                                   box.root / "src-root")}])
            except StorageError:
                raised = True
            suite.check(raised,
                        "ТЗ 5.4 п.2: операция НЕ объявлена успешной при "
                        "отказе метаданных")
            archives = _local_archives(box)
            suite.check(len(archives) == 1,
                        "ТЗ 5.4 п.2: ЛОКАЛЬНЫЙ АРХИВ СОХРАНЕН при ошибке "
                        "метаданных")
            statuses = {a.name: md.read_metadata(a)["status"]
                        for a in archives}
            suite.check(list(statuses.values()) == ["upload-failed"],
                        "ТЗ 5.4 п.2: статус upload-failed")
            suite.check(list(box.network_dir.glob("*.gpg")),
                        "сетевой архив при этом записан и проверен")
        finally:
            bk.write_network_metadata = real_wnm
        # Повторная отправка после устранения ошибки завершается успешно
        # (архив уже на шаре — promote подтверждает без перезаписи)
        report = bk.upload_pending(config, non_interactive=True,
                                   reporter=silent_reporter())
        suite.check(not _local_archives(box),
                    "ТЗ 5.4 п.7: повторный upload-pending удалил локальный "
                    "архив после успешных метаданных")
        net_meta = list(box.network_dir.glob("*.json"))
        suite.check(len(net_meta) == 1 and net_meta[0].is_file(),
                    "ТЗ 5.4 п.7: сетевые метаданные записаны повторно")

    # =============================================
    # ТЗ 3.5 пп.4-6 (исправление №1): обрыв сети на разных этапах.
    # =============================================
    for stage, label in (
            (1, "перед копированием"),
            (2, "перед финализацией"),
            (4, "перед удалением локального архива")):
        with Sandbox() as box:
            box.make_subvolume("src-root", b"net-drop" * 200)
            common.NETMOUNT_IS_MOUNTED[0] = True
            patch_run_id(RunIdGen(start=50))
            real_recheck = bk.recheck_network_mount
            state = {"calls": 0}
            def _flaky_recheck(*_a, **_k):
                state["calls"] += 1
                if state["calls"] == stage:
                    raise StorageError("сеть отключилась (имитация)")
                return real_recheck(*_a, **_k)
            bk.recheck_network_mount = _flaky_recheck
            try:
                raised = False
                try:
                    bk.run_backup(config, non_interactive=True,
                                  reporter=silent_reporter(),
                                  items_override=[{"name": "root",
                                                   "source": str(
                                                       box.root /
                                                       "src-root")}])
                except StorageError:
                    raised = True
                suite.check(raised,
                            "ТЗ 3.5 (обрыв {}): операция не успешна".format(
                                label))
                suite.check(len(_local_archives(box)) == 1,
                            "ТЗ 3.5 (обрыв {}): локальный архив сохранен"
                            .format(label))
                statuses = {a.name: md.read_metadata(a)["status"]
                            for a in _local_archives(box)}
                suite.check(list(statuses.values()) == ["upload-failed"],
                            "ТЗ 3.5 (обрыв {}): статус upload-failed"
                            .format(label))
                leftovers = list(box.network_dir.glob("*.tmp"))
                suite.check(not leftovers,
                            "ТЗ 3.5 (обрыв {}): временный файл на шаре "
                            "очищен".format(label))
            finally:
                bk.recheck_network_mount = real_recheck

    # ТЗ 3.5 п.6 (обрыв непосредственно перед удалением): сетевая копия
    # полностью подтверждена, но локальный архив НЕ удаляется
    with Sandbox() as box:
        box.make_subvolume("src-root", b"delete-guard" * 200)
        common.NETMOUNT_IS_MOUNTED[0] = True
        patch_run_id(RunIdGen(start=60))
        real_recheck = bk.recheck_network_mount
        state = {"calls": 0}
        def _drop_before_delete(*_a, **_k):
            state["calls"] += 1
            if state["calls"] == 4:  # [1] копия, [2] финализация,
                raise StorageError("сеть отключилась перед удалением")  # [3] метаданные, [4] удаление
            return real_recheck(*_a, **_k)
        bk.recheck_network_mount = _drop_before_delete
        try:
            raised = False
            try:
                bk.run_backup(config, non_interactive=True,
                              reporter=silent_reporter(),
                              items_override=[{"name": "root",
                                               "source": str(
                                                   box.root / "src-root")}])
            except StorageError:
                raised = True
            suite.check(raised,
                        "ТЗ 3.5 п.6: обрыв перед удалением — не успешна")
            suite.check(len(_local_archives(box)) == 1,
                        "ТЗ 3.5 п.6: удаление локального архива ЗАПРЕЩЕНО")
            suite.check(list(box.network_dir.glob("*.gpg")) and
                        list(box.network_dir.glob("*.json")),
                        "ТЗ 3.5 п.6: сетевой архив и метаданные записаны")
        finally:
            bk.recheck_network_mount = real_recheck

    # Ошибка копирования (обрыв во время копирования, ТЗ 3.5 п.4)
    with Sandbox() as box:
        box.make_subvolume("src-root", b"copy-fail" * 200)
        common.NETMOUNT_IS_MOUNTED[0] = True
        patch_run_id(RunIdGen(start=70))
        real_copy = bk.copy_to_network_tmp
        def _broken_copy(*_a, **_k):
            raise StorageError("Ошибка копирования: сеть отключилась")
        bk.copy_to_network_tmp = _broken_copy
        try:
            raised = False
            try:
                bk.run_backup(config, non_interactive=True,
                              reporter=silent_reporter(),
                              items_override=[{"name": "root",
                                               "source": str(
                                                   box.root / "src-root")}])
            except StorageError:
                raised = True
            suite.check(raised, "ТЗ 3.5 п.4: ошибка копирования — не успешна")
            suite.check(len(_local_archives(box)) == 1,
                        "ТЗ 3.5 п.4: локальный архив сохранен при обрыве "
                        "копирования")
            suite.check(not list(box.network_dir.glob("*")),
                        "ТЗ 3.5 п.4: на шаре не осталось файлов")
        finally:
            bk.copy_to_network_tmp = real_copy

    # =============================================
    # TUI: выбранные источники не должны затрагивать глобальные пути профиля
    # =============================================
    class _ProfilePaths:
        SUBVOLUMES = [{"name": "configured-root", "source": "/"}]
        SNAPSHOT_DIR = "/production/.snapshots"

        @staticmethod
        def normalize_subvolumes(raw):
            return ([{"name": "configured-root", "source": "/",
                      "snapshot_dir": None,
                      "local_backup_dir": None}], [])

        @staticmethod
        def local_backup_dirs():
            return ["/production/.snapshots/backup"]

        @staticmethod
        def local_backup_dir(path):
            return str(Path(path) / "backup")

    selected_items = [{
        "name": "test-source",
        "source": "/mnt/btrfs-test/source",
        "snapshot_dir": "/mnt/btrfs-test/.snapshots",
        "local_backup_dir": "/mnt/btrfs-test/.snapshots/backup",
    }]
    tui_backup_dirs = bk._local_backup_dirs(
        _ProfilePaths, selected_items, include_config=False)
    cli_backup_dirs = bk._local_backup_dirs(
        _ProfilePaths, selected_items, include_config=True)
    suite.check(tui_backup_dirs == ["/mnt/btrfs-test/.snapshots/backup"],
                "TUI: каталоги архивов ограничены выбором текущей операции")
    suite.check("/production/.snapshots/backup" in cli_backup_dirs,
                "CLI: профильные каталоги остаются доступны")

    tui_retention = bk._retention_groups(
        _ProfilePaths, selected_items, include_config=False)
    cli_retention = bk._retention_groups(
        _ProfilePaths, selected_items, include_config=True)
    suite.check(tui_retention == [
        ("/mnt/btrfs-test/.snapshots", ["test-source"])],
        "TUI: retention ограничен выбранными источниками и каталогами")
    suite.check(any(path == "/production/.snapshots"
                    for path, _names in cli_retention),
                "CLI: retention профиля не изменен")



    # TUI per-run override отключает автозагрузку без изменения конфигурации.
    with Sandbox() as box:
        box.make_subvolume("src-root", b"interactive-local" * 250)
        box.make_subvolume("src-home", b"interactive-home" * 120)
        common.NETMOUNT_IS_MOUNTED[0] = True
        config.AUTO_UPLOAD = True
        try:
            patch_run_id(RunIdGen(start=25))
            report = bk.run_backup(
                config, non_interactive=True, auto_upload_override=False,
                reporter=silent_reporter())
            local = _local_archives(box)
            suite.check(len(local) == 2 and
                        not any(box.network_dir.glob("*.gpg")),
                        "per-run AUTO_UPLOAD override=false оставляет архив локально")
            suite.check(md.read_metadata(local[0])["status"] == "upload-pending",
                        "per-run override сохраняет статус upload-pending")
            suite.check(config.AUTO_UPLOAD is True,
                        "per-run override не меняет конфигурацию CLI/cron")
        finally:
            config.AUTO_UPLOAD = True

    # TUI per-run override может включить отправку при AUTO_UPLOAD=false.
    with Sandbox() as box:
        box.make_subvolume("src-root", b"interactive-network" * 250)
        box.make_subvolume("src-home", b"interactive-home-network" * 120)
        common.NETMOUNT_IS_MOUNTED[0] = True
        config.AUTO_UPLOAD = False
        try:
            patch_run_id(RunIdGen(start=26))
            report = bk.run_backup(
                config, non_interactive=True, auto_upload_override=True,
                reporter=silent_reporter())
            suite.check(len(list(box.network_dir.glob("*.gpg"))) == 2,
                        "per-run AUTO_UPLOAD override=true запускает отправку")
            suite.check(not _local_archives(box),
                        "после подтвержденной отправки локальный архив удален")
            suite.check(config.AUTO_UPLOAD is False,
                        "per-run включение не меняет конфигурацию CLI/cron")
        finally:
            config.AUTO_UPLOAD = True

    return suite.summary()


if __name__ == "__main__":
    sys.exit(run_main(main))
