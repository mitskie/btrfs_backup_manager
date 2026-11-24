"""
Тесты интерфейсных компонентов (ТЗ 20.7): Reporter/TuiConfirm/AbortFlag,
fallback при отсутствии curses. Полноценный TUI проверяется вручную
(отмечено в TEST-REPORT.md).
Запуск: python3 tests/test_tui.py
"""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Suite, run_main  # noqa: E402
from core import OperationAborted  # noqa: E402


def main():
    suite = Suite("tui")

    try:
        from ui import tui
    except Exception as exc:  # noqa: BLE001
        suite.skip("TUI-модуль недоступен: {}".format(exc))
        return suite.summary()

    # 20.7-2: отсутствие curses -> fallback CLI (is_available False)
    available = tui.is_available()
    if sys.stdout.isatty() and sys.stdin.isatty():
        suite.check(isinstance(available, bool),
                    "is_available возвращает bool (tty)")
    else:
        suite.check(available is False,
                    "20.7-2: в нетерминальной среде TUI не запускается "
                    "(fallback на простой CLI)")

    # Модуль curses может отсутствовать — это не блокирует программу
    from core import environment
    suite.check(isinstance(environment.curses_available(), bool),
                 "curses_available() не падает при отсутствии curses")

    # CollectReporter: потокобезопасность
    reporter = tui.CollectReporter()
    errors = []
    def writer(n):
        try:
            for i in range(200):
                reporter.info("поток {}: строка {}".format(n, i))
                if i % 50 == 0:
                    reporter.stage("этап {}: {}".format(n, i))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines, stage, _sub, _comp = reporter.snapshot()
    suite.check(not errors and len(lines) == tui.CollectReporter.MAX_LINES,
                "CollectReporter: буфер ограничен MAX_LINES={}, ошибок "
                "потоков нет (получено {})".format(
                    tui.CollectReporter.MAX_LINES, len(lines)))
    suite.check(stage.startswith("этап 3:") or stage.startswith("этап 2:"),
                "CollectReporter: последний этап зафиксирован")

    # TuiConfirm: вопрос из потока -> ответ из главного потока
    confirm = tui.TuiConfirm()
    answers = []
    def asker():
        answers.append(confirm.ask("Продолжить?"))
    thread = threading.Thread(target=asker)
    thread.start()
    import time
    deadline = time.monotonic() + 5
    while confirm.pending() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    suite.check(confirm.pending() == "Продолжить?",
                "TuiConfirm: вопрос дошел до главного потока")
    confirm.resolve(True)
    thread.join(timeout=5)
    suite.check(answers == [True], "TuiConfirm: ответ вернулся в поток")

    # AbortFlag: request -> abort_check возбуждает OperationAborted
    abort = tui.AbortFlag()
    clean = True
    try:
        abort()
    except OperationAborted:
        clean = False
    suite.check(clean, "AbortFlag: до request ожидание продолжается")
    abort.request()
    try:
        abort()
        suite.check(False, "после request должно быть прерывание")
    except OperationAborted:
        suite.check(True, "AbortFlag: после request операции прерываются")

    # MultiSelect: выбор subvolume для бэкапа (замечание пользователя 1.2)
    sel = tui.MultiSelect(["root", "home", "var"], checked=True)
    suite.check(sel.selected_names() == ["root", "home", "var"],
                "MultiSelect: по умолчанию отмечены все")
    sel.toggle(1)
    suite.check(sel.selected_names() == ["root", "var"],
                "MultiSelect: снятие отметки (пробел)")
    sel.toggle(1)
    suite.check(sel.selected_names() == ["root", "home", "var"],
                "MultiSelect: повторная отметка")
    sel.set_all(False)
    suite.check(sel.selected_names() == [],
                "MultiSelect: снять все (клавиша A)")
    msel = tui.MultiSelect(["arch1", "arch2", "arch3"], checked=False)
    msel.toggle(0)
    msel.toggle(2)
    suite.check(msel.selected_indices() == [0, 2],
                "MultiSelect: selected_indices (множественный выбор)")
    sel.set_all(True)
    sel.move(2)
    sel.toggle()
    suite.check(sel.selected_names() == ["root", "home"],
                "MultiSelect: курсор + переключение текущей строки")
    sel.move(10)
    sel.move(-50)
    suite.check(sel.index == 0, "MultiSelect: курсор не выходит за границы")

    # =============================================
    # 1.4: выбор ВСЕХ разделов (замечание пользователя: «у меня 4 раздела,
    # скрипт все еще предлагает мне только один /... выводить все разделы
    # и отмечать те, что прописаны в конфиге»); 1.4.1: только Btrfs,
    # группировка по устройству, сноска вместо пометок origin
    # =============================================
    entries = [
        {"name": "root", "source": "/", "selectable": True,
         "from_config": True, "device": "/dev/sda1",
         "notation": "/dev/sda1[/@]"},
        {"name": "sda3", "source": "/dev/sda3", "selectable": False,
         "from_config": False, "device": "/dev/sda3", "notation": None,
         "origin": "не смонтирован — выбрать нельзя"},
        {"name": "athome", "source": "/home", "selectable": True,
         "from_config": False, "device": "/dev/sda1",
         "notation": "/dev/sda1[/@home]"},
    ]
    selection = tui.TuiApp._make_selection(entries)
    suite.check(selection.flags == [True, False, False],
                "1.4: по умолчанию отмечен только конфигурационный раздел")
    tui.TuiApp._selection_set_all(selection, entries)
    suite.check(selection.flags == [True, False, True],
                "1.4: A отмечает только выбираемые строки")
    tui.TuiApp._selection_set_all(selection, entries)
    suite.check(selection.flags == [False, False, False],
                "1.4: A снимает все выбираемые, недоступная не меняется")

    # 1.4.1: строки экрана — заголовки устройств + дерево subvolume
    # (предложение пользователя: «над выбором разделов указать на каком
    # они устройстве... /dev/sda1: [x] / /dev/sda1[/@] ...»)
    from ui import common as ui_common
    rows = ui_common.source_rows(entries)
    headers = [r["text"] for r in rows if r["kind"] == "header"]
    suite.check(headers == ["/dev/sda1:", "/dev/sda3:"],
                "1.4.1: записи сгруппированы по устройству (заголовки)")
    prefixes = {entries[r["index"]]["source"]: r["prefix"]
                for r in rows if r["kind"] == "entry"}
    suite.check(prefixes.get("/") == "" and
                prefixes.get("/home") == "└─" and
                prefixes.get("/dev/sda3") == "",
                "1.4.1: префиксы дерева (первый без префикса, последний └─)")
    suite.check(ui_common.source_entry_suffix(entries[0]) ==
                "/dev/sda1[/@]",
                "1.4.1: у выбираемой строки обозначение устройство[subvol]")
    suite.check(ui_common.source_entry_suffix(entries[1]) ==
                "не смонтирован — выбрать нельзя",
                "1.4.1: у недоступной строки причина, а не обозначение")
    suite.check(ui_common.source_label_width(entries) == len("/dev/sda3"),
                "1.4.1: ширина левой колонки — по самой длинной метке")
    label, suffix = ui_common.source_line(
        entries, [r for r in rows
                  if r["kind"] == "entry" and r["prefix"] == "└─"][0],
        ui_common.source_label_width(entries))
    suite.check(label == "└─/home".ljust(len("/dev/sda3")) and
                suffix == "/dev/sda1[/@home]",
                "1.4.1: метка выровнена до общей ширины колонки")
    # порядок внутри группы: обычные пути, точечные в конце (пример
    # пользователя: /, /home, /var/cache, /var/log, /.snapshots)
    dot_entries = [
        {"name": "root", "source": "/", "selectable": True,
         "from_config": True, "device": "/dev/sda1",
         "notation": "/dev/sda1[/@]"},
        {"name": "atsnap", "source": "/.snapshots", "selectable": True,
         "from_config": False, "device": "/dev/sda1",
         "notation": "/dev/sda1[/@snapshots]"},
        {"name": "athome", "source": "/home", "selectable": True,
         "from_config": False, "device": "/dev/sda1",
         "notation": "/dev/sda1[/@home]"},
    ]
    dot_groups = ui_common.group_source_entries(dot_entries)
    suite.check([pair[1]["source"] for pair in
                 dot_groups[0]["members"]] == ["/", "/home", "/.snapshots"],
                "1.4.1: точечные каталоги (.snapshots) в конце группы")

    # 1.4/1.4.1: общий список источников (TUI и простой CLI):
    # конфигурация + subvolume системы без дубликатов; НЕ-Btrfs разделы
    # не показываются («вывод дисков, где не btrfs — лишнее»), Btrfs без
    # монтирования виден с причиной
    import config as config_module
    real_discover = ui_common.snapshots_module.discover_sources
    real_parts = ui_common.snapshots_module.list_partitions
    real_mount_lines = ui_common.snapshots_module.read_mount_lines
    try:
        # ТЗ 1.4.7: обнаружение подменяется целиком (emulация системы с
        # двумя subvolume, ТЗ §3.1): список, id и fs_uuid зашиты явно
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
             "mounted": True, "mount_points": ["/boot/efi"]},
            {"name": "sda3", "device": "/dev/sda3", "fstype": "btrfs",
             "mounted": False, "mount_points": []}]
        # Детерминированность: монтирования не зависят от машины теста
        ui_common.snapshots_module.read_mount_lines = lambda: [
            "/dev/sda1[/@] / btrfs rw,relatime,subvol=/@ 0 0",
            "/dev/sda1[/@home] /home btrfs rw,relatime,subvol=/@home 0 0",
            "/dev/sdc1 /boot/efi vfat rw,relatime 0 0"]

        class _StubConfig:
            SUBVOLUMES = [{"name": "root", "source": "/"}]
            SNAPSHOT_DIR = "/snaps"

            @staticmethod
            def local_backup_dir(snapshot_dir):
                return snapshot_dir + "/backup"

            normalize_subvolumes = staticmethod(
                config_module.normalize_subvolumes)

        src_entries, _disc_errors = ui_common.backup_source_entries(
            _StubConfig())
        src_names = [e["name"] for e in src_entries]
        suite.check(src_names[0] == "root" and
                    src_entries[0]["from_config"] and
                    src_entries[0]["selectable"],
                    "1.4: источник конфигурации в начале списка, "
                    "отмечен и выбирается")
        suite.check(
            [e["source"] for r in ui_common.source_rows(src_entries)
             if r["kind"] == "entry"
             for e in [src_entries[r["index"]]]] ==
            [e["source"] for e in src_entries],
            "1.4.1: порядок списка совпадает с порядком отображения "
            "(номера CLI подряд, курсор TUI без перескоков)")
        suite.check(sum(1 for e in src_entries
                        if (e["source"].rstrip("/") or "/") == "/") == 1,
                    "1.4: источник конфигурации не дублируется обнаруженным")
        suite.check(all(e["name"] != "sda1" for e in src_entries),
                    "1.4.1: не-Btrfs раздел (vfat) не показывается вовсе")
        found_home = [e for e in src_entries if e["name"] == "athome"]
        suite.check(found_home and found_home[0]["selectable"] and
                    found_home[0]["notation"] == "/dev/sda1[/@home]" and
                    found_home[0]["device"] == "/dev/sda1",
                    "1.4.1: обнаруженный subvolume выбирается, "
                    "с устройством и обозначением")
        suite.check(src_entries[0]["notation"] == "/dev/sda1[/@]" and
                    src_entries[0]["device"] == "/dev/sda1",
                    "1.4.1: у источника конфигурации тоже устройство и "
                    "обозначение")
        sda3 = [e for e in src_entries if e["name"] == "sda3"]
        suite.check(sda3 and not sda3[0]["selectable"] and
                    "не смонтирован" in sda3[0]["origin"],
                    "1.4: несмонтированный Btrfs виден с причиной")

        # v1.4.2: по умолчанию отмечаются только ВЫБИРАЕМые записи —
        # [–] не выглядит выбранным на экране выбора
        selection = tui.TuiApp._make_selection(src_entries)
        suite.check(
            all(selection.flags[i] for i, e in enumerate(src_entries)
                if e.get("from_config") and e.get("selectable", True)) and
            all(not selection.flags[i] for i, e in enumerate(src_entries)
                if not e.get("selectable", True)),
            "1.4.2 TUI: предотметка только у выбираемых конфигурационных")
    finally:
        ui_common.snapshots_module.discover_sources = real_discover
        ui_common.snapshots_module.list_partitions = real_parts
        ui_common.snapshots_module.read_mount_lines = real_mount_lines

    # v1.4.2 (замечание пользователя: на ВМ без Btrfs список «(точка
    # монтирования не найдена)» все равно пускал дальше по Enter — до
    # пароля и ошибки «нет .snapshots»): источник конфигурации под
    # монтированием НЕ-Btrfs помечается [–] с причиной
    try:
        ui_common.snapshots_module.discover_sources = \
            lambda exclude_dirs=(), timeout=60, mount_lines=None: {
                "sources": [], "errors": []}
        ui_common.snapshots_module.list_partitions = lambda: []
        ui_common.snapshots_module.read_mount_lines = lambda: [
            "/dev/sda2 / ext4 rw,relatime 0 0",
            "/dev/sdc1 /boot/efi vfat rw,relatime 0 0"]
        vm_entries, _vm_errors = ui_common.backup_source_entries(_StubConfig())
        suite.check(len(vm_entries) == 1 and
                    not vm_entries[0]["selectable"] and
                    "нет точки монтирования Btrfs" in vm_entries[0]["origin"],
                    "1.4.2: на ВМ без Btrfs источник конфигурации [–] "
                    "с причиной")
        suite.check(vm_entries[0]["notation"] is None and
                    vm_entries[0]["device"] is None,
                    "1.4.2: у недоступного источника нет устройства")
        rows = ui_common.source_rows(vm_entries)
        suite.check(any("(точка монтирования не найдена):" == r["text"]
                        for r in rows if r["kind"] == "header"),
                    "1.4.2: группа без устройства подписана «(точка "
                    "монтирования не найдена)»")
        vm_selection = tui.TuiApp._make_selection(vm_entries)
        suite.check(not any(vm_selection.flags),
                    "1.4.2 TUI: на ВМ без Btrfs ничего не отмечено — "
                    "Enter не продолжит операцию")
    finally:
        ui_common.snapshots_module.discover_sources = real_discover
        ui_common.snapshots_module.list_partitions = real_parts
        ui_common.snapshots_module.read_mount_lines = real_mount_lines

    # SimpleCLI меню: импортируется без curses
    from ui import cli
    suite.check(hasattr(cli, "run_menu"),
                "простой CLI доступен (fallback без curses, ТЗ 2.3)")
    suite.check(hasattr(cli, "_choose_subvolumes_cli") and
                hasattr(tui.TuiApp, "_select_subvolumes"),
                "выбор subvolume есть в обоих интерфейсах (1.2)")

    # ui.dialogs.confirm: EOF -> отказ (безопасное умолчание)
    import builtins
    from ui import dialogs
    real_input = builtins.input
    builtins.input = lambda *a: (_ for _ in ()).throw(EOFError())
    try:
        suite.check(dialogs.confirm("Тест?") is False,
                    "dialogs.confirm при EOF -> отказ")
    finally:
        builtins.input = real_input

    # 1.1.1: подтверждение одной клавишей Y/N (латиница, оба регистра)
    parse = dialogs.parse_confirm_key
    suite.check(parse("y", False) is True and parse("Y", False) is True,
                "confirm: Y в любом регистре — да")
    suite.check(parse("n", True) is False and parse("N", True) is False,
                "confirm: N в любом регистре — нет")
    suite.check(parse("д", False) is True and parse("Д", False) is True,
                "confirm: кириллическая Д тоже принимается")
    suite.check(parse("н", True) is False and parse("Н", True) is False,
                "confirm: кириллическая Н тоже принимается")
    # 1.4.3 (замечание пользователя: «нажимаю y — операция отменяется,
    # сжатие 'gzip' отклонено»): русская раскладка. Физическая клавиша
    # Y на ЙЦУКЕН посылает кириллическую «н» (Нет — мгновенная отмена),
    # а фонетическое «у» (то, что пользователь печатает вместо y)
    # раньше игнорировалось. у/У — Да; цифры 1/2 — основные, они
    # одинаковы при любой раскладке.
    suite.check(parse("у", False) is True and parse("У", False) is True,
                "confirm: кириллическая у/У (фонетический аналог y) — да "
                "(v1.4.3)")
    suite.check(parse("1", False) is True and parse("+", False) is True,
                "confirm: цифра 1 — да (раскладконезависимый ответ)")
    suite.check(parse("2", True) is False,
                "confirm: цифра 2 — нет (раскладконезависимый ответ)")
    suite.check(parse(chr(13), True) is True and parse(chr(10), False) is False,
                "confirm: Enter — значение по умолчанию")
    suite.check(parse(chr(27), True) is False and parse("q", True) is False,
                "confirm: Esc/Q — отказ (безопасное умолчание)")
    suite.check(parse("x", False) is None and parse(" ", False) is None,
                "confirm: нераспознанная клавиша игнорируется, а не ответ")
    suite.check(dialogs.parse_confirm_line("yes", False) is True and
                dialogs.parse_confirm_line("YES", False) is True and
                dialogs.parse_confirm_line("да", False) is True and
                dialogs.parse_confirm_line("у", False) is True and
                dialogs.parse_confirm_line("1", False) is True and
                dialogs.parse_confirm_line("", False) is False and
                dialogs.parse_confirm_line("", True) is True,
                "confirm: построчный fallback принимает yes/да/у/1 и пустую "
                "строку")

    # 1.4.3: подсказка показывает цифры 1/2 ПЕРВЫМИ — основной ответ,
    # работает при любой раскладке; Enter — значение по умолчанию.
    suffix = dialogs._prompt_suffix(True)
    suffix_no = dialogs._prompt_suffix(False)
    suite.check("1=Да" in suffix and "2=Нет" in suffix and
                "Enter — Да" in suffix,
                "confirm: подсказка [1=Да, 2=Нет] + Enter — Да (v1.4.3)")
    suite.check("1=Да" in suffix_no and "2=Нет" in suffix_no and
                "Enter — Нет" in suffix_no,
                "confirm: подсказка [1=Да, 2=Нет] + Enter — Нет (v1.4.3)")

    # 1.4.3 (замечание пользователя: диалог с «y» отменял операцию на
    # русской раскладке): в диалоге TUI кнопки "[1] Да / [2] Нет",
    # цифры одинаковы при любой раскладке; пояснение не выводится.
    hint_yes = tui.confirm_hint(True)
    hint_no = tui.confirm_hint(False)
    suite.check("[1] Да" in hint_yes and "[2] Нет" in hint_yes and
                "Enter — Да" in hint_yes,
                "TUI confirm: кнопки [1] Да / [2] Нет + Enter — Да (v1.4.3)")
    suite.check("[1] Да" in hint_no and "[2] Нет" in hint_no and
                "Enter — Нет" in hint_no,
                "TUI confirm: кнопки видны и при умолчании Нет (v1.4.3)")
    suite.check("клавиш" not in hint_yes and "клавиш" not in hint_no,
                "TUI confirm: пояснение про одну клавишу не возвращено")

    # 1.3.1 (замечание пользователя): "проблема с цифрами" — у пункта
    # "Выход" выводилась цифра 1, а клавиша 0 завершала программу вместо
    # открытия подписанного нулем пункта настроек.
    suite.check(len(tui.MENU_KEYS) == len(tui.MENU_ITEMS) == 4 and
                len(set(tui.MENU_KEYS)) == len(tui.MENU_KEYS) and
                tui.MENU_KEYS == ("1", "2", "3", "q"),
                "главное меню: 4 пункта, у каждого своя клавиша "
                "(1-3 — пункты, q — выход; ТЗ 1.4.7 §7.1)")

    # =============================================
    # §4.1/§4.2: статус-панель без строки профиля; счетчик сетевых копий
    # =============================================
    import tempfile
    from ui import common as ui_common2
    import config as config_mod

    class _StatusConfig:
        """Конфигурация для _collect_status: реальный "/" как «шара»."""
        config_found = True
        profile_name = "основной"
        SNAPSHOT_DIR = None
        LOCAL_BACKUP_DIR = None
        NETWORK_ENABLED = True
        NETWORK_MOUNT = "/"
        NETWORK_DIRECTORY = "btrfs-backups"
        CHECK_NETWORK_MOUNT = False
        EXPECTED_NETWORK_FS_TYPE = None
        COMPRESSION = "gzip"
        GPG_BINARY = "/usr/bin/gpg"
        GPG_PASSWORD = None
        is_password_placeholder = staticmethod(
            config_mod.is_password_placeholder)

        @staticmethod
        def local_backup_dir(snapshot_dir):
            return str(snapshot_dir) + "/backup" if snapshot_dir else None

    with tempfile.TemporaryDirectory() as tmp:
        _StatusConfig.SNAPSHOT_DIR = tmp
        app = tui.TuiApp(_StatusConfig())
        status = app._collect_status()
        labels = [label for label, _value in status]
        suite.check("Профиль" not in labels,
                    "§4.1: строка внутреннего имени профиля удалена")
        suite.check(any(label == "Отправлено на сетевую шару"
                        for label, _v in status),
                    "§4.2: подпись «Отправлено на сетевую шару» на месте")
        sent = [value for label, value in status
                if label == "Отправлено на сетевую шару"][0]
        suite.check(sent == "0",
                    "§4.2: счетчик — реально подтвержденные сетевые копии "
                    "(архивы на шаре), не локальные созданные")
        # Шара не смонтирована/не настроена — честное «нет данных»
        _StatusConfig.NETWORK_MOUNT = None
        status = tui.TuiApp(_StatusConfig())._collect_status()
        sent = [value for label, value in status
                if label == "Отправлено на сетевую шару"]
        suite.check(sent and sent[0] == "нет данных",
                    "§4.2: без смонтированной шары число не выдумывается")
        suite.check("Ожидают отправки" in [l for l, _v in status],
                    "счетчик ожидающих отправки сохранен")

    # =============================================
    # §4.5: Esc — выход БЕЗ дополнительного блокирующего getch()
    # =============================================
    import curses as curses_mod

    class _ScrStub:
        def __init__(self, keys):
            self._keys = list(keys)
            self.getch_calls = 0
            self._timeout = -1

        def getmaxyx(self):
            return (24, 80)

        def curs_set(self, visibility):
            return None

        def keypad(self, value):
            return None

        def timeout(self, value):
            self._timeout = value

        def getch(self):
            self.getch_calls += 1
            return self._keys.pop(0) if self._keys else -1

        def addstr(self, *args):
            return None

        def clear(self):
            return None

        def erase(self):
            return None

        def refresh(self):
            return None

    # Одиночный Esc: выход после РОВНО одного getch (старый код ждал
    # вторую клавишу блокирующим getch — подвешивал программу)
    stub = _ScrStub([27])
    app = tui.TuiApp(_StatusConfig())
    app.run(stub)
    suite.check(stub.getch_calls == 1,
                "§4.5: одиночный Esc — выход после одного getch "
                "(без ожидания второй клавиши)")
    # Стрелки работают, Esc после них — тоже выход
    stub = _ScrStub([curses_mod.KEY_UP, 27])
    app = tui.TuiApp(_StatusConfig())
    app.run(stub)
    suite.check(stub.getch_calls == 2,
                "§4.5: стрелка обрабатывается, затем Esc — выход")
    # Q тоже выходит без лишних чтений
    stub = _ScrStub([ord("q")])
    app = tui.TuiApp(_StatusConfig())
    app.run(stub)
    suite.check(stub.getch_calls == 1, "§4.5: q — выход одним нажатием")

    # =============================================
    # §6: безопасное предложение каталога при глобальном SNAPSHOT_DIR
    # на другой Btrfs применяется автоматически и проверяется повторно.
    # =============================================
    from unittest.mock import patch
    proposal_calls = {"count": 0}
    def _proposal_checks(items, _cfg):
        proposal_calls["count"] += 1
        if proposal_calls["count"] == 1:
            return [{
                "name": "btrfs-test", "source": "/mnt/btrfs-test/source",
                "ok": False, "errors": ["глобальный SNAPSHOT_DIR на другой ФС"],
                "proposed_snapshot_dir": "/mnt/btrfs-test/.snapshots",
                "proposal_ok": True, "proposal_needs_subvolume": False,
            }]
        return [{
            "name": "btrfs-test", "source": "/mnt/btrfs-test/source",
            "ok": True, "errors": [], "warnings": [],
            "snapshot_dir": items[0]["snapshot_dir"],
            "local_backup_dir": "/mnt/btrfs-test/.snapshots/backup",
        }]
    proposal_app = tui.TuiApp(_StatusConfig())
    proposal_app._confirm_sources_screen = lambda _checks: True
    with patch.object(tui.pathcheck_module, "check_sources",
                      side_effect=_proposal_checks):
        proposal_result = proposal_app._prepare_sources([{
            "name": "btrfs-test", "source": "/mnt/btrfs-test/source",
            "snapshot_dir": None, "local_backup_dir": None,
        }])
    suite.check(
        proposal_result is not None and
        proposal_result[0]["snapshot_dir"] == "/mnt/btrfs-test/.snapshots" and
        proposal_calls["count"] == 2,
        "§6: индивидуальный каталог на правильной Btrfs подставлен "
        "автоматически и проверен повторно")

    # =============================================
    # §4.3: просмотр локальных копий (общие построители ui.common)
    # =============================================
    from core import metadata as metadata_mod

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
        snaps.mkdir()
        archives = root / "archives"
        archives.mkdir()
        # снапшот с достоверной меткой времени в имени
        (snaps / "root_2026-01-15_103000").mkdir()
        _ViewConfig.SNAPSHOT_DIR = str(snaps)
        _ViewConfig.LOCAL_BACKUP_DIR = str(archives)
        lines = ui_common2.snapshot_view_lines(_ViewConfig())
        suite.check(any("root_2026-01-15_103000" in line and
                        "(источник root)" in line and "2026-01-15 10:30"
                        in line for line in lines),
                    "§4.3: снапшот показывается с источником и датой")
        # архив без метаданных — честные «нет данных», не фиктивный статус
        bare = archives / "host_root_2026-11-16_120000_ab12cd.gz.gpg"
        bare.write_bytes(b"fake-archive")
        entry = ui_common2.archive_view_entry(bare)
        line = "\n".join(entry)
        suite.check("сжатие: gzip" in line and
                    "2026-11-16 12:00" in line and
                    "проверка: не проводилась" in line,
                    "§4.3: старый архив без sidecar получает дату из имени, "
                    "а проверка честно помечается как не проводившаяся")
        suite.check("2026-11-16 12:00" in line,
                    "§4.3: дата старого архива извлекается из имени "
                    "при отсутствии метаданных")
        # архив с метаданными verified: проверка пройдена, дата из метаданных
        good = archives / "host_root_2026-11-16_130000_ab12cd.gz.gpg"
        good.write_bytes(b"fake-archive-2")
        meta = metadata_mod.new_metadata(
            archive_name=good.name, subvolume_name="root",
            subvolume_path="/", run_id="2026-11-16_130000_ab12cd",
            compression="gzip", encrypted=True,
            snapshot_id="root_2026-11-16_130000")
        meta["created"] = "2026-11-16T13:00:00.000000"
        meta = metadata_mod.set_status(meta, metadata_mod.STATUS_VERIFIED)
        metadata_mod.write_metadata(good, meta)
        entry = ui_common2.archive_view_entry(good)
        line = "\n".join(entry)
        suite.check("пройдена (SHA-256)" in line and
                    "не отправлен (локальный)" in line and
                    "источник: root" in line,
                    "§4.3: состояние проверки и сетевой отправки из "
                    "метаданных")
        suite.check("2026-11-16 13:00" in line,
                    "§4.3: дата ISO 8601 из метаданных корректно "
                    "отображается")
        # дубликаты: один и тот же файл через два пути не дублируется
        _ViewConfig.LOCAL_BACKUP_DIR = str(archives)
        dup_config_lines = ui_common2.archive_view_lines(
            _ViewConfig(), dirs=[str(archives), str(archives)])
        shown = [ln for ln in dup_config_lines if ".gpg" in ln]
        suite.check(len(shown) == 2,
                    "§4.3: несколько путей к одному объекту не дублируют "
                    "архив в списке")
        # снапшоты/архивы: списки не пусты
        suite.check(any("Каталог" in ln for ln in dup_config_lines),
                    "§4.3: строки списка каталогов присутствуют")

    # =============================================
    # Дополнение §4.3: просмотр снапшотов/архивов со всех смонтированных
    # Btrfs, а не только путей из backup_config.py.
    # =============================================
    from unittest.mock import patch
    from ui import common as ui_common3
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dev_a = root / "dev-a"
        dev_b = root / "dev-b"
        (dev_a / ".snapshots" / "backup").mkdir(parents=True)
        (dev_b / ".snapshots" / "backup").mkdir(parents=True)
        (dev_a / ".snapshots" / "root_2026-11-20_100000_aaaaaa").mkdir()
        (dev_b / ".snapshots" / "home_2026-11-20_110000_bbbbbb").mkdir()
        arc_a = dev_a / ".snapshots" / "backup" /             "host_root_2026-11-20_100000_aaaaaa.gz.gpg"
        arc_b = dev_b / ".snapshots" / "backup" /             "host_home_2026-11-20_110000_bbbbbb.zst.gpg"
        arc_a.write_bytes(b"archive-a")
        arc_b.write_bytes(b"archive-b")

        class _EmptyViewConfig:
            SUBVOLUMES = []
            SNAPSHOT_DIR = None
            LOCAL_BACKUP_DIR = None

            @staticmethod
            def normalize_subvolumes(raw=None):
                return [], []

            @staticmethod
            def local_backup_dir(snapshot_dir):
                return str(snapshot_dir) + "/backup" if snapshot_dir else None

        fake_mounts = [
            (str(dev_a), "/dev/loop10", "/"),
            (str(dev_b), "/dev/loop11", "/"),
        ]
        with patch.object(ui_common3.snapshots_module, "read_mount_lines",
                          return_value=["fake mount table"]), \
             patch.object(ui_common3.snapshots_module, "btrfs_mounts",
                          return_value=fake_mounts), \
             patch.object(ui_common3.snapshots_module, "btrfs_notation",
                          side_effect=lambda path: (
                              "/dev/loop10[/]" if str(path).startswith(str(dev_a))
                              else "/dev/loop11[/]")):
            discovered_snaps = ui_common3.snapshot_dirs(_EmptyViewConfig())
            discovered_archives = ui_common3.archive_dirs(_EmptyViewConfig())
            snap_lines = ui_common3.snapshot_view_lines(_EmptyViewConfig())
            archive_lines = ui_common3.archive_view_lines(_EmptyViewConfig())
        suite.check(str(dev_a / ".snapshots") in discovered_snaps and
                    str(dev_b / ".snapshots") in discovered_snaps,
                    "§4.3: каталоги снапшотов обнаруживаются на всех Btrfs")
        suite.check(str(dev_a / ".snapshots" / "backup") in discovered_archives and
                    str(dev_b / ".snapshots" / "backup") in discovered_archives,
                    "§4.3: локальные архивы обнаруживаются на всех Btrfs")
        suite.check(any("Устройство /dev/loop10" in line for line in snap_lines) and
                    any("Устройство /dev/loop11" in line for line in snap_lines) and
                    any("root_2026-11-20_100000_aaaaaa" in line for line in snap_lines) and
                    any("home_2026-11-20_110000_bbbbbb" in line for line in snap_lines),
                    "§4.3: снапшоты показаны с группировкой по устройствам")
        suite.check(any("Устройство /dev/loop10" in line for line in archive_lines) and
                    any("Устройство /dev/loop11" in line for line in archive_lines) and
                    any(arc_a.name in line for line in archive_lines) and
                    any(arc_b.name in line for line in archive_lines),
                    "§4.3: архивы показаны с группировкой по устройствам")

    # E на экране итоговых параметров должен открыть редактор каталогов.
    from ui import tui as tui_module
    app = tui_module.TuiApp.__new__(tui_module.TuiApp)
    app.config = object()
    app.message = None
    item = {"name": "test", "source": "/mnt/test/source",
            "snapshot_dir": "/old/snaps", "local_backup_dir": "/old/backup"}
    checks = [{"name": "test", "source": item["source"],
               "snapshot_dir": item["snapshot_dir"],
               "local_backup_dir": item["local_backup_dir"],
               "warnings": [], "ok": True}]
    confirm_results = iter([False, True])
    edit_calls = []
    def _fake_edit(items, current_checks):
        edit_calls.append(True)
        items[0]["snapshot_dir"] = "/new/snaps"
    with patch.object(tui_module.pathcheck_module, "check_sources",
                      side_effect=[checks, [dict(checks[0],
                          snapshot_dir="/new/snaps")]]), \
         patch.object(app, "_confirm_sources_screen",
                      side_effect=lambda _checks: next(confirm_results)), \
         patch.object(app, "_edit_dirs_screen", side_effect=_fake_edit):
        prepared = app._prepare_sources([item])
    suite.check(bool(edit_calls) and prepared and
                prepared[0]["snapshot_dir"] == "/new/snaps",
                "TUI: E на итоговом экране открывает редактирование и повторную проверку")

    # Выбор категории сразу открывает список с флажками; отдельный
    # промежуточный экран «Просмотреть/Удалить» не нужен.
    app_menu = tui_module.TuiApp.__new__(tui_module.TuiApp)
    seen_options = []
    with patch.object(app_menu, "_select_screen",
                      side_effect=lambda title, options: (
                          seen_options.append((title, list(options))) or None)):
        app_menu._local_copies_screen()
    suite.check(seen_options == [(
        "Локальные снапшоты и архивы:", ["Снапшоты Btrfs", "Локальные архивы"]
    )], "TUI: меню локальных копий содержит два типа объектов")
    with patch.object(app_menu, "_delete_snapshots_screen") as del_snaps:
        app_menu._local_copy_category_screen(0)
    with patch.object(app_menu, "_delete_archives_screen") as del_archives:
        app_menu._local_copy_category_screen(1)
    suite.check(del_snaps.call_count == 1 and del_archives.call_count == 1,
                "TUI: выбор категории сразу открывает список выбора объектов")

    # Esc во время ручного ввода каталога должен отменять ввод и возвращать
    # управление на предыдущий экран, а не попадать в текстовое поле.
    class _InputScreen:
        def __init__(self, keys):
            self.keys = iter(keys)
        def getmaxyx(self):
            return (24, 80)
        def get_wch(self):
            return next(self.keys)
        def refresh(self):
            return None
        def timeout(self, value):
            return None
    app_input = tui_module.TuiApp.__new__(tui_module.TuiApp)
    app_input.stdscr = _InputScreen(["/", "t", "m", "p", "\x1b"])
    with patch.object(app_input, "_erase"), \
         patch.object(app_input, "_header"), \
         patch.object(app_input, "_add"):
        escaped = app_input._input_text("Каталог", "")
    suite.check(escaped is None,
                "TUI: Esc отменяет ручной ввод и возвращает назад")

    # Проверка множественного выбора: две отметки переживают отмену
    # первого диалога подтверждения и возвращаются при повторном Enter.
    import curses
    class _FakeScreen:
        def __init__(self, keys):
            self.keys = iter(keys)
        def getmaxyx(self):
            return (24, 100)
        def getch(self):
            return next(self.keys)
        def refresh(self):
            return None
        def timeout(self, value):
            return None
    app_select = tui_module.TuiApp.__new__(tui_module.TuiApp)
    app_select.stdscr = _FakeScreen([ord(" "), curses.KEY_DOWN,
                                     ord(" "), 10, 10])
    app_select.attrs = {"title": 0, "sel": 0, "hint": 0, "warn": 0}
    app_select.message = None
    confirm_answers = iter([False, True])
    with patch.object(app_select, "_erase"), \
         patch.object(app_select, "_header"), \
         patch.object(app_select, "_add"), \
         patch.object(app_select, "_confirm_dialog",
                      side_effect=lambda *a, **k: next(confirm_answers)):
        chosen = app_select._multi_select_delete(
            "Выбор", [{"path": Path("a.gpg"), "group": "dev", "label": "a"},
                      {"path": Path("b.gpg"), "group": "dev", "label": "b"}],
            "archives")
    suite.check([item["path"].name for item in chosen] == ["a.gpg", "b.gpg"],
                "TUI: несколько отметок сохраняются после отмены подтверждения")

    # Клавиша U отправляет выбранные архивы после предварительной проверки.
    app_upload = tui_module.TuiApp.__new__(tui_module.TuiApp)
    app_upload.stdscr = _FakeScreen([ord(" "), ord("u")])
    app_upload.attrs = {"title": 0, "sel": 0, "hint": 0, "warn": 0}
    app_upload.message = None
    app_upload.config = object()
    with patch.object(app_upload, "_erase"), \
         patch.object(app_upload, "_header"), \
         patch.object(app_upload, "_add"), \
         patch.object(app_upload, "_confirm_dialog", return_value=True), \
         patch.object(tui_module.backup_ops, "preflight_selected_uploads",
                      return_value=([Path("only.gpg").resolve()], [], [])):
        upload_choice = app_upload._multi_select_delete(
            "Архивы", [{"path": Path("only.gpg"), "group": "dev",
                        "label": "only.gpg"}], "archives")
    suite.check(isinstance(upload_choice, tuple) and
                upload_choice[0] == "upload" and
                upload_choice[1][0]["path"].name == "only.gpg",
                "TUI: U возвращает действие отправки отмеченного набора архивов")

    # Переключатель автозагрузки по умолчанию отражает конфиг, но меняет
    # только параметр текущего запуска.
    app_toggle = tui_module.TuiApp.__new__(tui_module.TuiApp)
    app_toggle.config = type("Cfg", (), {"AUTO_UPLOAD": True})()
    with patch.object(app_toggle, "_select_screen", return_value=1):
        toggle_off = app_toggle._choose_auto_upload()
    suite.check(toggle_off is False and app_toggle.config.AUTO_UPLOAD is True,
                "TUI: автозагрузка отключается только для текущей операции")
    app_toggle.config = type("Cfg", (), {"AUTO_UPLOAD": False})()
    with patch.object(app_toggle, "_select_screen", return_value=1):
        toggle_on = app_toggle._choose_auto_upload()
    suite.check(toggle_on is True and app_toggle.config.AUTO_UPLOAD is False,
                "TUI: автозагрузка включается для запуска без изменения конфига")

    # Одинаковые basenames на разных Btrfs получают имена по родительскому
    # каталогу, а не безликие source_2 — имя влияет на снапшот и архив.
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        src_a = base / "disk-a" / "source"
        src_b = base / "disk-b" / "source"
        src_a.mkdir(parents=True)
        src_b.mkdir(parents=True)
        class _EmptyDiscoveryConfig:
            SUBVOLUMES = []
            SNAPSHOT_DIR = None
            @staticmethod
            def normalize_subvolumes(raw=None):
                return [], []
            @staticmethod
            def local_backup_dir(snapshot_dir):
                return None
        discovered = [
            {"name": "source", "source": str(src_a), "fs_uuid": "uuid-a",
             "fs_root": False},
            {"name": "source", "source": str(src_b), "fs_uuid": "uuid-b",
             "fs_root": False},
        ]
        with patch.object(ui_common3.snapshots_module, "discover_sources",
                          return_value={"sources": discovered, "errors": []}), \
             patch.object(ui_common3.snapshots_module, "read_mount_lines",
                          return_value=[]), \
             patch.object(ui_common3.snapshots_module, "btrfs_notation",
                          side_effect=lambda path, mount_lines=None:
                          "/dev/loop-test[/]"), \
             patch.object(ui_common3.snapshots_module, "list_partitions",
                          return_value=[]):
            duplicate_entries, _ = ui_common3.backup_source_entries(
                _EmptyDiscoveryConfig())
        duplicate_names = {entry["name"] for entry in duplicate_entries}
        suite.check(duplicate_names == {"source_disk-a", "source_disk-b"},
                    "TUI: одинаковые имена subvolume различаются по пути, не source_2")

    # Сообщение upload-pending должно переноситься по ширине терминала.
    pending_message = (
        "Архивы сохранены локально со статусом upload-pending; отправка "
        "будет выполнена командой upload-pending после подключения "
        "сетевого хранилища.")
    wrapped_pending = ui_common3.wrap_text(pending_message, 36)
    suite.check(len(wrapped_pending) > 1 and
                all(len(line) <= 36 for line in wrapped_pending) and
                "upload-pending" in " ".join(wrapped_pending),
                "§10: длинное сообщение upload-pending переносится для узкого терминала")

    # Restore local archives must aggregate configured + discovered archive dirs.
    import tempfile
    from types import SimpleNamespace
    from unittest.mock import patch
    with tempfile.TemporaryDirectory(prefix="bbm-restore-discovery-") as temp:
        base = Path(temp)
        archive_a = base / "disk-a" / "backup"
        archive_b = base / "disk-b" / "backup"
        archive_a.mkdir(parents=True)
        archive_b.mkdir(parents=True)
        (archive_a / "a.gz.gpg").write_bytes(b"a")
        (archive_b / "b.zst.gpg").write_bytes(b"b")
        app = tui.TuiApp.__new__(tui.TuiApp)
        captured = {}
        app._select_screen = lambda title, options, info_lines=None: 0
        app._archive_dirs = lambda: [archive_a, archive_b]
        app._restore_archive_flow = lambda archives, local=False: captured.update(
            archives=list(archives), local=local)
        app._restore_menu()
        suite.check(
            {p.name for p in captured.get("archives", [])} ==
            {"a.gz.gpg", "b.zst.gpg"} and captured.get("local") is True,
            "TUI restore: локальные архивы собираются со всех обнаруженных каталогов")

        # Restore snapshots likewise aggregates all snapshot directories.
        snap_a = SimpleNamespace(path=archive_a.parent / "snap-a", name="snap-a")
        snap_b = SimpleNamespace(path=archive_b.parent / "snap-b", name="snap-b")
        (archive_a.parent / "snap-a").mkdir()
        (archive_b.parent / "snap-b").mkdir()
        app = tui.TuiApp.__new__(tui.TuiApp)
        app._snapshot_dirs = lambda: [archive_a.parent, archive_b.parent]
        app._select_screen = lambda title, options, info_lines=None: (
            captured.update(snapshot_labels=list(options)) or 0)
        app._prompt_string = lambda *args, **kwargs: None
        with patch.object(ui_common3, "collect_snapshots",
                          side_effect=[{"a": [snap_a]}, {"b": [snap_b]}]), \
             patch.object(ui_common3, "_device_label", side_effect=lambda p: str(p)):
            app._restore_snapshot_flow()
        suite.check(
            len(captured.get("snapshot_labels", [])) == 2 and
            any("snap-a" in line for line in captured["snapshot_labels"]) and
            any("snap-b" in line for line in captured["snapshot_labels"]),
            "TUI restore: локальные снапшоты собираются со всех Btrfs-каталогов")

    # Directory editor uses Esc only; no redundant explicit Back row.
    app = tui.TuiApp.__new__(tui.TuiApp)
    captured_options = {}
    app._select_screen = lambda title, options, info_lines=None: (
        captured_options.update(options=list(options)) or None)
    app._edit_source_dirs({"name": "test", "source": "/source",
                           "snapshot_dir": "/snap", "local_backup_dir": "/backup"})
    suite.check(len(captured_options.get("options", [])) == 2 and
                not any("Назад" in item for item in captured_options["options"]),
                "TUI: редактор каталогов использует Esc без лишней кнопки Назад")

    return suite.summary()


if __name__ == "__main__":
    sys.exit(run_main(main))
