"""
Тесты удаления локальных архивов и снапшотов (v1.3, замечание
пользователя: "нет возможности удаления локальных бэкапов и снапшотов"),
а также именования архивов с префиксом хоста (v1.3).
Запуск: python3 tests/test_deletion.py
"""

import argparse
import builtins
import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    PASSWORD, Sandbox, Suite, make_archive, run_main, silent_reporter,
)
import config  # noqa: E402
from core import BtrfsBackupError  # noqa: E402
from core import compression as cm  # noqa: E402
from core import metadata as md  # noqa: E402
from core import snapshots as sm  # noqa: E402
from core import verification as vr  # noqa: E402
import main as main_module  # noqa: E402
from ui import common as ui_common  # noqa: E402


def _feed_input(lines):
    """Подменяет input() последовательностью ответов (для CLI-диалогов)."""
    queue = list(lines)

    def fake_input(_prompt=""):
        if not queue:
            raise EOFError
        return queue.pop(0)

    builtins.input = fake_input
    return queue


def _release_lock():
    from core import locking
    try:
        locking.release_lock()
    except Exception:  # noqa: BLE001
        pass


def main():
    suite = Suite("deletion")

    # =============================================
    # ui.common: правило повторного подтверждения (ТЗ 14)
    # =============================================
    suite.check(ui_common.deletion_needs_second_confirm(5, 2, False) is False,
                "часть архивов — одного подтверждения достаточно")
    suite.check(ui_common.deletion_needs_second_confirm(5, 5, False) is True,
                "удаление ВСЕХ архивов требует повторного подтверждения")
    suite.check(ui_common.deletion_needs_second_confirm(5, 2, True) is True,
                "затронута последняя копия subvolume — повторное "
                "подтверждение")
    suite.check(ui_common.deletion_needs_second_confirm(0, 0, False) is False,
                "пустой список не требует подтверждения")

    # =============================================
    entries = ["a", "b", "c", "d", "e"]
    try:
        _feed_input(["1,3-5"])
        suite.check(ui_common is not None, "input подменен")
        from ui import cli as ui_cli
        got = ui_cli._choose_indices_cli(entries)
        suite.check(got == [0, 2, 3, 4],
                    "выбор номерами и диапазонами ('1,3-5')")
        _feed_input(["a"])
        suite.check(ui_cli._choose_indices_cli(entries) == [0, 1, 2, 3, 4],
                    "'a' выбирает все")
        _feed_input(["99 x 2"])
        suite.check(ui_cli._choose_indices_cli(entries) == [1],
                    "некорректные номера пропускаются")
        _feed_input([""])
        suite.check(ui_cli._choose_indices_cli(entries) is None,
                    "Enter — отмена выбора")
    finally:
        builtins.input = _real_input()

    # =============================================
    # main: сопоставление --archive (имя/путь/без суффикса)
    # =============================================
    with Sandbox() as box:
        path1, _s1, _sha1 = make_archive(box, "root", b"data-root" * 100,
                                         compression="gzip",
                                         run_id="2026-03-01_000101_000001")
        path2, _s2, _sha2 = make_archive(box, "home", b"data-home" * 100,
                                         compression="gzip",
                                         run_id="2026-03-01_000102_000002")
        targets, missing = main_module._resolve_archive_targets(
            box.backup_dir, [path1.name])
        suite.check(targets == [path1] and not missing,
                    "по имени файла находится ровно один архив")
        targets, missing = main_module._resolve_archive_targets(
            box.backup_dir, [path1.name[:-len(".gz.gpg")]])
        suite.check(targets == [path1] and not missing,
                    "имя без суффикса сжатия опознается")
        targets, missing = main_module._resolve_archive_targets(
            box.backup_dir, [str(path2)])
        suite.check(targets == [path2] and not missing,
                    "полный путь внутри каталога принимается")

        outside = box.root / "outside.gz.gpg"
        outside.write_bytes(b"must-not-delete")
        targets, missing = main_module._resolve_archive_targets(
            box.backup_dir, [str(outside)])
        suite.check(not targets and missing == [str(outside)] and
                    outside.read_bytes() == b"must-not-delete",
                    "абсолютный путь за пределами backup_dir отклоняется")
        args = argparse.Namespace(archive=[str(outside)], all=False, yes=True)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main_module.cmd_delete_archives(
                args, main_module.appconfig.from_module(config))
        _release_lock()
        suite.check(code == 1 and outside.read_bytes() == b"must-not-delete",
                    "delete-archives --yes не удаляет внешний файл")

        outside_link = box.backup_dir / "linked.gz.gpg"
        try:
            outside_link.symlink_to(outside)
            targets, missing = main_module._resolve_archive_targets(
                box.backup_dir, [outside_link.name])
            suite.check(not targets and missing == [outside_link.name] and
                        outside.read_bytes() == b"must-not-delete",
                        "символическая ссылка на внешний файл отклоняется")
        except (OSError, NotImplementedError):
            suite.skip("создание symlink недоступно в среде")

        targets, missing = main_module._resolve_archive_targets(
            box.backup_dir, ["missing.gz.gpg"])
        suite.check(not targets and missing == ["missing.gz.gpg"],
                    "несуществующее имя попадает в missing")
        _ = path2

    # =============================================
    # Удаление архива с префиксом хоста + sidecars (v1.3)
    # =============================================
    with Sandbox() as box:
        src = box.make_subvolume("tmp-src-root", b"host-arch" * 100)
        snap = sm.create_readonly_snapshot(
            src, box.snapshot_dir, "root", "2026-03-01_000201_000003")
        out_path = box.backup_dir / cm.archive_filename(
            "root", "2026-03-01_000201_000003", "gzip", hostname="testhost")
        size = __import__("core.archive", fromlist=["archive"]) \
            .create_archive(
                snap, out_path, compression="gzip", level=1,
                gpg_binary=config.GPG_BINARY, password=PASSWORD)
        suite.check(out_path.is_file() and size > 0,
                    "v1.3: архив создан с префиксом хоста "
                    "(testhost_root_...)")
        parsed = md.parse_archive_name(out_path.name)
        suite.check(parsed and parsed["hostname"] == "testhost" and
                    parsed["subvolume"] == "root",
                    "имя с хостом разбирается: хост и subvolume распознаны")
        sha = vr.sha256_file(out_path)
        meta = md.new_metadata(
            archive_name=out_path.name, subvolume_name="root",
            subvolume_path="/", run_id="2026-03-01_000201_000003",
            compression="gzip", encrypted=True,
            snapshot_id=snap.name, sha256=sha, size=size,
            status=md.STATUS_VERIFIED, hostname="testhost")
        md.write_metadata(out_path, meta)
        # v1.3: отдельный .sha256 не создается, только JSON
        suite.check(not (Path(str(out_path) + ".sha256")).exists(),
                    "v1.3: .sha256 не создается")
        suite.check(md.read_metadata(out_path).get("sha256") == sha,
                    "SHA-256 хранится в JSON-метаданных")

        from core.network import delete_local_archive
        delete_local_archive(out_path)
        suite.check(not out_path.exists(),
                    "архив с хостом удален (delete_local_archive)")
        suite.check(not Path(str(out_path) + ".json").exists(),
                    "метаданные удалены вместе с архивом")
        suite.check(snap.exists(),
                    "снапшот при удалении архива не затрагивается")

    # =============================================
    # main: cmd_delete_archives (--archive + --yes, без tty)
    # =============================================
    with Sandbox() as box:
        path1, _s1, _sha1 = make_archive(box, "root", b"del-me-1" * 200,
                                         compression="gzip",
                                         run_id="2026-03-01_000301_000004")
        path2, _s2, _sha2 = make_archive(box, "root", b"keep-me" * 200,
                                         compression="gzip",
                                         run_id="2026-03-01_000302_000005")
        args = argparse.Namespace(archive=[path1.name], all=False, yes=True)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main_module.cmd_delete_archives(
                args, main_module.appconfig.from_module(config))
        _release_lock()
        suite.check(code == 0, "delete-archives --archive ... --yes -> 0")
        suite.check(not path1.exists(),
                    "указанный архив удален")
        suite.check(not Path(str(path1) + ".json").exists() and
                    not Path(str(path1) + ".sha256").exists(),
                    "sidecar-файлы удаленного архива также удалены")
        suite.check(path2.exists(),
                    "остальные архивы не затронуты")
        suite.check("Удалено архивов: 1" in out.getvalue(),
                    "итог удаления напечатан")

    # =============================================
    # main: --all без --yes запрещено; несуществующее имя -> код 1
    # =============================================
    with Sandbox() as box:
        make_archive(box, "root", b"one" * 300, compression="gzip",
                     run_id="2026-03-01_000401_000006")
        args = argparse.Namespace(archive=None, all=True, yes=False)
        try:
            with redirect_stdout(io.StringIO()):
                main_module.cmd_delete_archives(
                    args, main_module.appconfig.from_module(config))
            suite.check(False, "--all без --yes должно давать ошибку")
        except BtrfsBackupError:
            suite.check(True, "--all без --yes отклонено (ТЗ 14)")
        finally:
            _release_lock()

        args = argparse.Namespace(archive=["no-such-archive.gz.gpg"],
                                  all=False, yes=True)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main_module.cmd_delete_archives(
                args, main_module.appconfig.from_module(config))
        _release_lock()
        suite.check(code == 1,
                    "только несуществующие имена -> код 1, ничего не удалено")

    # =============================================
    # ui.cli: интерактивное удаление архивов (ввод подменен)
    # =============================================
    with Sandbox() as box:
        path1, _s1, _sha1 = make_archive(box, "root", b"cli-del" * 200,
                                         compression="gzip",
                                         run_id="2026-03-01_000501_000007")
        path2, _s2, _sha2 = make_archive(box, "home", b"cli-keep" * 200,
                                         compression="gzip",
                                         run_id="2026-03-01_000502_000008")
        from ui import cli as ui_cli
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                # архивы отсортированы по имени: home_... №1, root_... №2;
                # каждый subvolume имеет ровно один архив -> удаление
                # последней копии требует ВТОРОГО подтверждения (ТЗ 14)
                _feed_input(["2", "y", "y"])
                ui_cli.delete_archives_cli(config)
            suite.check(not path1.exists() and path2.exists(),
                        "CLI: выбранная единица удалена, остальные на месте")
        finally:
            builtins.input = _real_input()

        # Отказ в подтверждении -> ничего не удалено
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                _feed_input(["2", "n"])
                ui_cli.delete_archives_cli(config)
            suite.check(path2.exists(),
                        "CLI: отказ в подтверждении сохраняет архив")
        finally:
            builtins.input = _real_input()

    # =============================================
    # ui.cli: интерактивное удаление снапшотов (эмулятор btrfs)
    # =============================================
    with Sandbox() as box:
        box.make_subvolume("src-root", b"snapshot-payload" * 100)
        snap1 = sm.create_readonly_snapshot(
            box.root / "src-root", box.snapshot_dir, "root",
            "2026-03-01_000601_000009")
        snap2 = sm.create_readonly_snapshot(
            box.root / "src-root", box.snapshot_dir, "root",
            "2026-03-01_000602_00000a")
        from ui import cli as ui_cli
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                # список newest-first: №1 — snap2, №2 — snap1
                _feed_input(["2", "y"])  # выбрать snap1 (старейший)
                ui_cli.delete_snapshots_cli(config)
            suite.check(not snap1.exists() and snap2.exists(),
                        "CLI: выбранный снапшот удален, второй сохранен")
        finally:
            builtins.input = _real_input()

        # Отказ -> оба на месте
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                _feed_input(["1", "n"])
                ui_cli.delete_snapshots_cli(config)
            suite.check(snap2.exists(),
                        "CLI: отказ в подтверждении сохраняет снапшот")
        finally:
            builtins.input = _real_input()

    _ = silent_reporter
    return suite.summary()


def _real_input():
    """Возвращает оригинальный input (до подмен)."""
    return builtins.__dict__.get("_bbm_real_input", builtins.input)


# Сохранить оригинал при первом импорте модуля
if "_bbm_real_input" not in builtins.__dict__:
    builtins.__dict__["_bbm_real_input"] = builtins.input


if __name__ == "__main__":
    sys.exit(run_main(main))
