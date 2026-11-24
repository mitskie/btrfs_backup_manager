"""
Тесты CLI и кодов завершения (ТЗ 13.1, 13.2).
Запуск: python3 tests/test_cli.py
"""

import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    RunIdGen, Sandbox, Suite, patch_run_id, run_main, silent_reporter,
)
import config  # noqa: E402
import main as main_module  # noqa: E402
from core import (  # noqa: E402
    EXIT_CONFIG, EXIT_ERROR, EXIT_LOCKED, EXIT_MISSING_TOOLS, EXIT_NETWORK,
    EXIT_VERIFICATION, BtrfsBackupError, ConfigError, EncryptionError,
    EnvironmentCheckError, LockError, RestoreError, SnapshotError,
    StorageError, UtilityMissingError, VerificationError, exit_code_for,
)
from core import backup as bk  # noqa: E402
from core import locking  # noqa: E402

import common  # noqa: E402


def _run_main(argv):
    """Запускает main() в процессе, возвращает код завершения."""
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            code = main_module.main(argv)
        return code, out.getvalue()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0, out.getvalue()


def main():
    suite = Suite("cli")

    # Код завершения 0: успешный локальный backup без сети (ТЗ 13.2)
    with Sandbox() as box:
        box.make_subvolume("src-root", b"cli-root" * 300)
        box.make_subvolume("src-home", b"h" * 200)
        common.NETMOUNT_IS_MOUNTED[0] = False
        patch_run_id(RunIdGen(start=300))
        code, out = _run_main(["backup", "-n"])
        suite.check(code == 0,
                    "коды: локальный backup без сети -> 0 (ТЗ 13.2)")
        suite.check("Итог:" in out, "итоговый отчет напечатан")

    # backup --subvolume: только указанный subvolume (ТЗ 13)
    with Sandbox() as box:
        box.make_subvolume("src-root", b"only-root" * 300)
        box.make_subvolume("src-home", b"h" * 200)
        common.NETMOUNT_IS_MOUNTED[0] = False
        patch_run_id(RunIdGen(start=330))
        code, out = _run_main(["backup", "-n", "--subvolume", "root"])
        suite.check(code == 0, "--subvolume: backup одного subvolume -> 0")
        suite.check("root_2026-" in out and "home_2026-" not in out,
                    "--subvolume: обработан только выбранный subvolume")
        # По пути источника
        code, out = _run_main(["backup", "-n", "--subvolume",
                               str(box.root / "src-home")])
        suite.check(code == 0 and "home_2026-" in out,
                    "--subvolume: выбор по пути источника")
        # Неизвестный subvolume -> ошибка конфигурации/аргументов
        code, out = _run_main(["backup", "-n", "--subvolume", "no-such"])
        suite.check(code == 2,
                    "--subvolume: неизвестное имя -> код 2 с диагностикой")

    # upload-pending без ожидающих архивов -> 0
    with Sandbox() as box:
        common.NETMOUNT_IS_MOUNTED[0] = True
        code, _out = _run_main(["upload-pending", "-n"])
        suite.check(code == 0, "коды: upload-pending без архивов -> 0")

    # snapshots -> 0
    with Sandbox() as box:
        box.make_subvolume("src-root", b"x")
        from core import snapshots as sm
        sm.create_readonly_snapshot(box.root / "src-root", box.snapshot_dir,
                                    "root", "2026-03-06_000001_aa0301")
        code, out = _run_main(["snapshots"])
        suite.check(code == 0 and "read-only" in out,
                    "коды: snapshots -> 0, снапшот перечислен")

    # list-archives -> 0 (без root не требуется)
    with Sandbox() as box:
        code, _out = _run_main(["list-archives"])
        suite.check(code == 0, "коды: list-archives -> 0")

    # diagnostics -> 0 в исправной тестовой среде (ТЗ 16)
    with Sandbox() as box:
        box.make_subvolume("src-root", b"x")
        box.make_subvolume("src-home", b"h")
        common.NETMOUNT_IS_MOUNTED[0] = True
        code, out = _run_main(["diagnostics"])
        suite.check(code == 0, "коды: диагностика исправной среды -> 0")
        suite.check("[КРИТИЧНО]" not in out,
                    "диагностика: критических ошибок нет")

    # verify --archive -> 0
    with Sandbox() as box:
        from common import make_archive
        path, _snap, sha = make_archive(box, "root", b"verify-cli" * 200,
                                        compression="gzip")
        common.NETMOUNT_IS_MOUNTED[0] = True
        code, out = _run_main(["verify", "--archive", str(path)])
        suite.check(code == 0 and "Проверка пройдена" in out,
                    "коды: verify -> 0, проверка пройдена")

    # verify поврежденного архива -> 8 (ошибка проверки целостности)
    with Sandbox() as box:
        from common import make_archive
        path, _snap, sha = make_archive(box, "root", b"broken" * 200,
                                        compression="gzip")
        data = path.read_bytes()
        path.write_bytes(data[:len(data) // 2])
        code, _out = _run_main(["verify", "--archive", str(path)])
        suite.check(code == EXIT_VERIFICATION,
                    "коды: verify поврежденного архива -> 8")

    # lock занят другим процессом -> LockError; main() отображает в 10 (ТЗ 15)
    with Sandbox() as box:
        locking.release_lock()  # сброс блокировки этого процесса
        import fcntl
        foreign_fd = os.open(config.LOCK_FILE,
                             os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(foreign_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            suite.expect_raises(
                lambda: locking.acquire_lock(config.LOCK_FILE, str(box.root)),
                LockError, "блокировка, занятая другим процессом -> LockError")
            suite.check(exit_code_for(LockError("x")) == EXIT_LOCKED,
                        "коды: LockError -> 10 (ТЗ 13.2)")
        finally:
            os.close(foreign_fd)
            locking.release_lock()

    # Некорректная конфигурация -> 2
    with Sandbox() as box:
        config.COMPRESSION = "bzip2"
        try:
            code, _out = _run_main(["backup", "-n"])
            suite.check(code == EXIT_CONFIG,
                        "коды: ошибка конфигурации -> 2")
        finally:
            config.COMPRESSION = "zstd"

    # Пароль-заполнитель в cron -> 5 (ошибка GPG/пароля)
    with Sandbox() as box:
        box.make_subvolume("src-root", b"x" * 100)
        box.make_subvolume("src-home", b"h" * 90)
        common.NETMOUNT_IS_MOUNTED[0] = False
        config.GPG_PASSWORD = "CHANGE_ME"
        try:
            code, _out = _run_main(["backup", "-n"])
            suite.check(code == 5,
                        "коды: cron без пароля GPG -> 5")
        finally:
            config.GPG_PASSWORD = "correct-horse-battery-42"

    # Прерывание (Ctrl+C) -> 9
    with Sandbox() as box:
        box.make_subvolume("src-root", b"x" * 100)
        box.make_subvolume("src-home", b"h" * 90)
        common.NETMOUNT_IS_MOUNTED[0] = False
        real_backup = bk.run_backup
        def interrupted(*a, **k):
            raise KeyboardInterrupt()
        bk.run_backup = interrupted
        try:
            code, _out = _run_main(["backup", "-n"])
            suite.check(code == 9, "коды: Ctrl+C -> 9 (операция отменена)")
        finally:
            bk.run_backup = real_backup

    # Ошибка сети при отправке -> 7
    with Sandbox() as box:
        box.make_subvolume("src-root", b"net-fail" * 200)
        box.make_subvolume("src-home", b"h" * 150)
        common.NETMOUNT_IS_MOUNTED[0] = True
        patch_run_id(RunIdGen(start=320))
        real_copy = bk.copy_to_network_tmp
        def failing_copy(*a, **k):
            raise StorageError("сеть потеряна")
        bk.copy_to_network_tmp = failing_copy
        try:
            code, _out = _run_main(["backup", "-n"])
            suite.check(code == EXIT_NETWORK, "коды: ошибка отправки -> 7")
        finally:
            bk.copy_to_network_tmp = real_copy

    # Нет утилит сжатия -> 3
    with Sandbox() as box:
        box.make_subvolume("src-root", b"x" * 50)
        box.make_subvolume("src-home", b"h" * 40)
        common.NETMOUNT_IS_MOUNTED[0] = False
        from core import compression as cm
        config.CRON_COMPRESSION_FALLBACK = False
        real_tool = cm.tool_available
        cm.tool_available = lambda name: False
        try:
            code, _out = _run_main(["backup", "-n"])
            suite.check(code == EXIT_MISSING_TOOLS,
                        "коды: отсутствуют утилиты сжатия -> 3")
        finally:
            cm.tool_available = real_tool
            config.CRON_COMPRESSION_FALLBACK = True

    # Неизвестная команда -> 2 (argparse)
    code, _out = _run_main(["no-such-command"])
    suite.check(code == 2, "коды: неизвестная команда -> 2")

    # =============================================
    # 1.4: интерактивный выбор разделов в простом CLI (замечание
    # пользователя: «в tui и cli должна быть возможность интерактивно
    # выбрать то, что нужно для бэкапа»); 1.4.1: только Btrfs,
    # группировка по устройству, сноска вместо пометок origin
    # =============================================
    import builtins
    import io
    from contextlib import redirect_stdout
    from ui import cli as cli_ui
    from ui import common as ui_common
    real_discover = ui_common.snapshots_module.discover_sources
    real_parts = ui_common.snapshots_module.list_partitions
    real_mount_lines = ui_common.snapshots_module.read_mount_lines
    real_input = builtins.input
    old_subvolumes = config.SUBVOLUMES
    try:
        # ТЗ 1.4.7: обнаружение подменяется целиком — emulация системы,
        # где / и /home обнаружены (ТЗ §3.1), без ошибок (§3.2)
        ui_common.snapshots_module.discover_sources = \
            lambda exclude_dirs=(), timeout=60, mount_lines=None: {
                "sources": [
                    {"name": "at", "source": "/", "id": "257",
                     "fs_uuid": None, "fs_root": False},
                    {"name": "athome", "source": "/home", "id": "258",
                     "fs_uuid": None, "fs_root": False}],
                "errors": []}
        ui_common.snapshots_module.list_partitions = lambda: [
            {"name": "sda1", "device": "/dev/sda1", "fstype": "vfat",
             "mounted": True, "mount_points": ["/boot/efi"]}]
        # Детерминированность: монтирования не зависят от машины теста
        ui_common.snapshots_module.read_mount_lines = lambda: [
            "/dev/sda1[/@] / btrfs rw,relatime,subvol=/@ 0 0",
            "/dev/sda1[/@home] /home btrfs rw,relatime,subvol=/@home 0 0",
            "/dev/sdc1 /boot/efi vfat rw,relatime 0 0"]
        config.SUBVOLUMES = ["/", "/home"]

        # Enter сразу — конфигурационные отметки сохраняются
        answers = iter([""])
        builtins.input = lambda *a, **k: next(answers)
        result = cli_ui._choose_subvolumes_cli(config)
        suite.check(
            result and [r["name"] for r in result] == ["root", "home"] and
            [r["source"] for r in result] == ["/", "/home"],
            "1.4 CLI: Enter продолжает с конфигурационными отметками")

        # Номер переключает отметку; недоступный номер не переключается
        answers = iter(["2", "3", ""])
        builtins.input = lambda *a, **k: next(answers)
        result = cli_ui._choose_subvolumes_cli(config)
        suite.check(
        result and result[0]["name"] == "root" and
        result[0]["source"] == "/",
        "1.4 CLI: номер снимает отметку, недоступный пропускается")

        # a — отметить все выбираемые (после снятия одной отметки);
        # 0 — отмена
        answers = iter(["2", "a", ""])
        builtins.input = lambda *a, **k: next(answers)
        result = cli_ui._choose_subvolumes_cli(config)
        suite.check(
        result and [r["name"] for r in result] == ["root", "home"] and
        [r["source"] for r in result] == ["/", "/home"],
        "1.4 CLI: a отмечает все выбираемые разделы")
        answers = iter(["0"])
        builtins.input = lambda *a, **k: next(answers)
        suite.check(cli_ui._choose_subvolumes_cli(config) is None,
                    "1.4 CLI: 0 — отмена выбора")

        # Сняты все отметки -> пустой Enter НЕ продолжает (v1.4.2:
        # напоминает про выбор и остается на экране), 0 — отмена
        answers = iter(["1 2", "", "0"])
        builtins.input = lambda *a, **k: next(answers)
        suite.check(cli_ui._choose_subvolumes_cli(config) is None,
                    "1.4 CLI: пустой выбор — не запуск backup")

        # v1.4.2 (замечание пользователя: на ВМ без Btrfs выбор все
        # равно пускал дальше по Enter): нет монтирования Btrfs ->
        # источник конфигурации [–] с причиной, выбор закрывается
        # сразу с понятным сообщением, без запроса ввода
        ui_common.snapshots_module.read_mount_lines = lambda: [
            "/dev/sda2 / ext4 rw,relatime 0 0"]
        answers = iter([])  # input не должен вызываться НИ РАЗУ
        builtins.input = lambda *a, **k: next(answers)
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = cli_ui._choose_subvolumes_cli(config)
        out = buf.getvalue()
        suite.check(result is None,
                    "1.4.2 CLI: без Btrfs выбор закрывается (None)")
        suite.check("нет точки монтирования Btrfs" in out and
                    "[–]" in out,
                    "1.4.2 CLI: источник конфигурации [–] с причиной")
        suite.check("нет доступных разделов Btrfs" in out,
                    "1.4.2 CLI: сообщение об отсутствии Btrfs")
        suite.check("[x] /" not in out,
                    "1.4.2 CLI: недоступный источник не отмечен [x]")
        ui_common.snapshots_module.read_mount_lines = lambda: [
            "/dev/sda1[/@] / btrfs rw,relatime,subvol=/@ 0 0",
            "/dev/sda1[/@home] /home btrfs rw,relatime,subvol=/@home 0 0",
            "/dev/sdc1 /boot/efi vfat rw,relatime 0 0"]

        # 1.4.1: вывод списка — сноска, группа устройства, обозначение
        # устройство[subvol], дерево; не-Btrfs раздел не показывается
        entries, _disc_errors = ui_common.backup_source_entries(config)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli_ui._print_source_entries(
                entries, [bool(e.get("from_config")) for e in entries])
        out = buf.getvalue()
        suite.check("отмечены заранее" in out and
                    "из конфигурации" not in out and
                    "найден в системе" not in out,
                    "1.4.1 CLI: сноска о конфигурации, пометок origin нет")
        suite.check("/dev/sda1:" in out and "/dev/sda1[/@]" in out and
                    "/dev/sda1[/@home]" in out,
                    "1.4.1 CLI: группа устройства с обозначением "
                    "устройство[subvol]")
        suite.check("└─/home" in out,
                    "1.4.1 CLI: дерево subvolume в списке")
        suite.check("sda1" not in out.replace("/dev/sda1", ""),
                    "1.4.1 CLI: не-Btrfs раздел (vfat sda1) не показывается")
        suite.check("vfat" not in out,
                    "1.4.1 CLI: упоминаний не-Btrfs файловых систем нет")
    finally:
        builtins.input = real_input
        ui_common.snapshots_module.discover_sources = real_discover
        ui_common.snapshots_module.list_partitions = real_parts
        ui_common.snapshots_module.read_mount_lines = real_mount_lines
        config.SUBVOLUMES = old_subvolumes

    # exit_code_for: стабильная карта исключений (ТЗ 13.2)
    suite.check(exit_code_for(ConfigError("x")) == 2, "карта: ConfigError -> 2")
    suite.check(exit_code_for(UtilityMissingError("x")) == 3,
                "карта: UtilityMissingError -> 3")
    suite.check(exit_code_for(SnapshotError("x")) == 4,
                "карта: SnapshotError -> 4")
    suite.check(exit_code_for(EncryptionError("x")) == 5,
                "карта: EncryptionError -> 5")
    suite.check(exit_code_for(StorageError("x")) == 7,
                "карта: StorageError -> 7")
    suite.check(exit_code_for(VerificationError("x")) == 8,
                "карта: VerificationError -> 8")
    suite.check(exit_code_for(LockError("x")) == 10, "карта: LockError -> 10")
    suite.check(exit_code_for(RestoreError("x")) == 1,
                "карта: RestoreError -> 1 (по умолчанию)")

    # ТЗ 1.4.8.2 §3.1/§5.2 (D-1): предложение очистки частичного
    # результата — только для объекта текущей операции (partial_info),
    # отказ подтверждения не удаляет, отказ очистки не выдает успех.
    with Sandbox() as box:
        from ui import restore_helpers
        partial = box.restore_dir / "root_2026-03-01_000100_000001"
        partial.mkdir()
        (partial / ".shim-subvolume").touch()
        (partial / ".shim-readonly").touch()
        (partial / ".shim-received").touch()
        info = {"dest": str(partial), "target": str(box.restore_dir),
                "preexisting": []}
        answers = []
        out = io.StringIO()
        with redirect_stdout(out):
            restore_helpers.offer_partial_cleanup(
                info, lambda text: (answers.append(text) or True))
        suite.check(len(answers) == 1 and "частичный результат" in answers[0]
                    and "btrfs subvolume delete" in answers[0],
                    "D-1 CLI: очистка предложена с объяснением операции")
        suite.check(not partial.exists(),
                    "D-1 CLI: подтвержденная очистка удалила частичный "
                    "subvolume напрямую (без смены ro)")
        suite.check("Удален частичный результат" in out.getvalue(),
                    "D-1 CLI: сообщение об удалении после факта")
        # Отказ пользователя — объект сохранен
        partial.mkdir()
        (partial / ".shim-subvolume").touch()
        out2 = io.StringIO()
        with redirect_stdout(out2):
            restore_helpers.offer_partial_cleanup(
                info, lambda _text: False)
        suite.check(partial.exists(),
                    "D-1 CLI: отказ подтверждения — объект сохранен")
        # partial_info=None (объект не от текущей операции) — предложения нет
        called = []
        out3 = io.StringIO()
        with redirect_stdout(out3):
            restore_helpers.offer_partial_cleanup(None, lambda t: called.append(t))
        suite.check(not called and not out3.getvalue().strip(),
                    "D-1 CLI: без partial_info очистка НЕ предлагается "
                    "(успешное восстановление не сопровождается удалением)")
        # Отказ самой очистки (preexisting=None) — понятное сообщение,
        # объект сохранен, успех не объявляется
        info_bad = {"dest": str(partial), "target": str(box.restore_dir),
                    "preexisting": None}
        out4 = io.StringIO()
        with redirect_stdout(out4):
            restore_helpers.offer_partial_cleanup(
                info_bad, lambda _text: True)
        suite.check(partial.exists() and
                    "Удаление отменено" in out4.getvalue() and
                    "Удален частичный результат" not in out4.getvalue(),
                    "D-1 CLI: fail-closed отказ очистки — без ложного "
                    "успеха, с инструкцией")

    # =============================================
    # §4.3: пункт «Локальные снапшоты» простого CLI — вложенный выбор
    # =============================================
    import tempfile
    from ui import dialogs as ui_dialogs
    import config as config_mod

    class _ViewConfig:
        SUBVOLUMES = None
        SNAPSHOT_DIR = None
        LOCAL_BACKUP_DIR = None

        @staticmethod
        def normalize_subvolumes(raw=None):
            return config_mod.normalize_subvolumes([])

        @staticmethod
        def local_backup_dir(snapshot_dir):
            return str(snapshot_dir) + "/backup" if snapshot_dir else None

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        snaps = root / "snaps"
        arch = root / "archives"
        snaps.mkdir()
        arch.mkdir()
        (snaps / "root_2026-01-15_103000").mkdir()
        _ViewConfig.SNAPSHOT_DIR = str(snaps)
        _ViewConfig.LOCAL_BACKUP_DIR = str(arch)
        # подмена select_from_list: первый выбор — «Снапшоты Btrfs»,
        # второй — «Локальные архивы»
        answers = iter(["Снапшоты Btrfs", "Локальные архивы"])
        real_select = ui_dialogs.select_from_list

        def fake_select(title, entries):
            return next(answers)
        ui_dialogs.select_from_list = fake_select
        sink = io.StringIO()
        try:
            with redirect_stdout(sink):
                cli_ui.show_local_copies(_ViewConfig())
                cli_ui.show_local_copies(_ViewConfig())
        finally:
            ui_dialogs.select_from_list = real_select
        joined = sink.getvalue()
        suite.check("root_2026-01-15_103000" in joined and
                    "2026-01-15 10:30" in joined,
                    "§4.3 CLI: снапшоты показываются с источником и датой")
        suite.check("Каталог" in joined and
                    "снапшотов:" in joined,
                    "§4.3 CLI: списки формируются общими построителями")

    return suite.summary()


if __name__ == "__main__":
    sys.exit(run_main(main))
