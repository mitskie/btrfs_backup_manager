#!/usr/bin/env python3
"""
Smoke-тест интерфейса 1.1.1 в реальном псевдотерминале (pty).

Проверяет жалобы пользователя:
  1. dialogs.confirm принимает ОДНУ клавишу Y/N без Enter (оба регистра);
  2. TUI: установка пароля GPG принимается ОДНИМ нажатием Enter, без
     цикла "Пароли не совпадают" и без возврата в главное меню;
  3. экран TUI не содержит мусора от прямых печатей в терминал.
"""

import os
import pty
import select
import signal
import sys
import time
from pathlib import Path

# Path используется в тесте выбора subvolume (копия проекта)
_ = Path

PROJECT = str(__import__("pathlib").Path(__file__).resolve().parent.parent)

RESULTS = []


def check(condition, message):
    RESULTS.append((bool(condition), message))
    print("  [{}] {}".format("PASS" if condition else "FAIL", message))
    return bool(condition)


class Pty:
    def __init__(self, cmd, cwd, env=None):
        self.master, slave = pty.openpty()
        merged = dict(os.environ)
        if env:
            merged.update(env)
        # Значения по умолчанию применяются ПОСЛЕ env: унаследованный
        # TERM=dumb ломал curses (частичные обновления вместо полных
        # кадров — проверки экрана не находили текст)
        merged.update({"TERM": "xterm", "LANG": "C.UTF-8",
                       "LC_ALL": "C.UTF-8"})
        pid = os.fork()
        if pid == 0:
            os.close(self.master)
            os.setsid()
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            if slave > 2:
                os.close(slave)
            os.chdir(cwd)
            os.execvpe(cmd[0], cmd, merged)
            os._exit(127)
        os.close(slave)
        self.pid = pid
        self.buffer = b""
        self.mark = 0

    def read_until(self, patterns, timeout=15.0, fresh=True):
        """
        Читает вывод, пока не найдется один из patterns (bytes/str).
        fresh=True — искать только после последней найденной позиции
        (mark), чтобы не считать старые кадры повторным совпадением.
        """
        deadline = time.monotonic() + timeout
        if isinstance(patterns, (bytes, str)):
            patterns = [patterns]
        patterns = [p.encode() if isinstance(p, str) else p for p in patterns]
        while time.monotonic() < deadline:
            for p in patterns:
                index = self.buffer.find(p, self.mark if fresh else 0)
                if index != -1:
                    if fresh:
                        self.mark = index + len(p)
                    return p
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                self.buffer += chunk
        return None

    def send(self, data):
        if isinstance(data, str):
            data = data.encode()
        os.write(self.master, data)

    def clear_buffer(self):
        self.buffer = b""
        self.mark = 0

    def stop(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass
        try:
            os.close(self.master)
        except OSError:
            pass


def test_confirm_single_key():
    """confirm() в tty: одна клавиша без Enter."""
    child_code = (
        "import sys; sys.path.insert(0, '" + PROJECT + "');"
        "from ui import dialogs;"
        "v = dialogs.confirm(sys.argv[1], default_yes=sys.argv[2] == '1');"
        "print('RESULT:{}'.format(v))"
    )
    cases = [
        (b"y", "0", "True", "y без Enter -> да"),
        (b"Y", "0", "True", "Y без Enter -> да"),
        (b"n", "1", "False", "N без Enter -> нет (при умолчании Yes)"),
        (b"\r", "1", "True", "Enter при умолчании Yes -> да"),
        (b"\r", "0", "False", "Enter при умолчании No -> нет"),
        (b"yes\n", "0", "True", "привычное 'yes'+Enter тоже работает"),
        # v1.4.3: раскладконезависимые ответы и кириллица
        ("1".encode(), "0", "True", "цифра 1 — да (любая раскладка, v1.4.3)"),
        ("2".encode(), "1", "False", "цифра 2 — нет (любая раскладка, v1.4.3)"),
        ("у".encode(), "0", "True",
         "кириллическая у — да (двухбайтовый UTF-8 читается, v1.4.3)"),
        ("н".encode(), "1", "False", "кириллическая н — нет (v1.4.3)"),
        ("д".encode(), "0", "True", "кириллическая д — да (v1.4.3)"),
    ]
    for keys, dfl, expected, message in cases:
        child = Pty([sys.executable, "-c", child_code, "Продолжить?", dfl],
                    cwd=PROJECT)
        try:
            # v1.4.3: подсказка [1=Да, 2=Нет] (Enter — Нет) / (Enter — Да)
            got = child.read_until(b"[1=", timeout=20)
            check(got is not None,
                  "confirm: подсказка [1=Да, 2=Нет] отображена ({})".format(message))
            child.clear_buffer()
            child.send(keys)
            got = child.read_until("RESULT:", timeout=15)
            ok = got is not None
            if ok:
                tail = child.buffer.split(b"RESULT:", 1)[1][:16]
                ok = expected in tail.decode("utf-8", "replace")
            check(ok, "confirm: {} (ожидалось RESULT:{})".format(
                message, expected))
        finally:
            child.stop()


DRIVER_PATHCHECK_EMULATION = (
    "from core import pathcheck as _pc\n"
    "def _fake_check_source(item, cfg, create_dirs=False):\n"
    "    sdir = item.get('snapshot_dir') or cfg.SNAPSHOT_DIR\n"
    "    local = (item.get('local_backup_dir') or\n"
    "             cfg.local_backup_dir(cfg.SNAPSHOT_DIR))\n"
    "    return {'name': item['name'], 'source': item['source'],\n"
    "            'ok': True, 'errors': [], 'warnings': [],\n"
    "            'snapshot_dir': sdir, 'local_backup_dir': local}\n"
    "_pc.check_source = _fake_check_source\n"
)


def make_btrfs_copy(prefix, subvolumes):
    """
    Временная копия проекта + драйвер запуска TUI с ПОДМЕНЕННОЙ таблицей
    монтирований (Btrfs-машиина: / и /home на /dev/sda1). В песочнице нет
    настоящих Btrfs-монтирований, а с v1.4.2 источник без монтирования
    Btrfs — [–] и не пускает по Enter (замечание пользователя о ВМ без
    Btrfs): чтобы проверить обычный поток, записи конфигурации должны
    быть выбираемыми, как на реальной машине. Возвращает (tmp, work).
    """
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix=prefix)
    work = Path(tmp) / "bbm"
    shutil.copytree(PROJECT, work,
                    ignore=shutil.ignore_patterns("__pycache__", "logs"))
    driver_head = (
        "import sys\n"
        "sys.path.insert(0, __file__.rsplit('/', 1)[0])\n"
        "from core import snapshots as _snap\n"
        "_snap.read_mount_lines = lambda: [\n"
        "    '/dev/sda1[/@] / btrfs rw,relatime,ssd,subvol=/@ 0 0',\n"
        "    '/dev/sda1[/@home] /home btrfs rw,relatime,ssd,"
        "subvol=/@home 0 0',\n"
        "    '/dev/sdc1 /boot/efi vfat rw,relatime 0 0',\n"
        "]\n"
        "import config as _cfg_mod\n"
        "_subvol_paths = " + repr([str(s) for s in subvolumes]) + "\n"
        "_snap.discover_sources = lambda exclude_dirs=(), timeout=60, "
        "mount_lines=None: {'sources': ["
        "{'name': 'sv%d' % i, 'source': path, 'id': None, "
        "'fs_uuid': None, 'fs_root': False} "
        "for i, path in enumerate(_subvol_paths)], 'errors': []}\n"
        "_snap.list_partitions = lambda: []\n"
        "import config\n"
        "config.SUBVOLUMES = _subvol_paths\n"
    )
    driver_tail = (
        "from ui import tui\n"
        "sys.exit(tui.run(config))\n"
    )
    driver = driver_head + DRIVER_PATHCHECK_EMULATION + driver_tail
    (work / "driver_tui.py").write_text(driver, encoding="utf-8")
    return tmp, work


def test_tui_password_flow():
    """TUI: цифра запускает пункт; выбор разделов; пароль — один Enter."""
    import shutil
    tmp, work = make_btrfs_copy("bbm-pass-", ["/"])
    child = Pty([sys.executable, "driver_tui.py"], cwd=str(work))
    try:
        got = child.read_until("Btrfs Backup Manager", timeout=25)
        check(got is not None, "TUI: главный экран отображен")
        child.read_until("Выход", timeout=10)
        # Цифра 1 запускает пункт БЕЗ Enter (жалоба пользователя)
        child.send("1")
        # v1.3.1: выбор разделов показывается ВСЕГДА (жалоба: "нет выбора
        # разделов"); конфигурационные разделы отмечены — Enter продолжает
        got = child.read_until("Выбор разделов", timeout=25)
        check(got is not None,
              "TUI: цифра 1 открыла экран выбора разделов (без Enter)")
        child.send("\r")
        got = child.read_until("итоговые параметры", timeout=25)
        check(got is not None,
              "TUI: Enter на выборе разделов открыл итоговые параметры "
              "(ТЗ 2.1 п.10)")
        child.send("\r")
        got = child.read_until("Алгоритм сжатия", timeout=25)
        check(got is not None,
              "ТЗ §7.3 п.6: после параметров открыт выбор сжатия")
        child.send("\r")
        got = child.read_until("Задайте пароль GPG", timeout=25)
        check(got is not None,
              "TUI: выбор сжатия подтвержден — открыт диалог пароля")
        # Пароль + ОДИН Enter, повтор + ОДИН Enter (жалоба пользователя:
        # раньше требовалось несколько Enter и цикл "не совпадают")
        child.send("smoke-pass-42")
        time.sleep(0.3)
        child.send("\r")
        got = child.read_until("Повторите пароль", timeout=25)
        check(got is not None,
              "TUI: первый Enter принят сразу — диалог повтора открыт")
        child.send("smoke-pass-42")
        time.sleep(0.3)
        child.send("\r")
        got = child.read_until("Этап:", timeout=30)
        check(got is not None,
              "TUI: второй Enter принят — операция запущена (экран прогресса)")
        text = child.buffer.decode("utf-8", "replace")
        check("Пароли не совпадают" not in text,
              "TUI: цикл 'Пароли не совпадают' не возник (одинаковые вводы)")
        # Ожидание завершения (не-root: ожидаемая ошибка окружения) и возврат
        child.read_until("завершено", timeout=15)
        time.sleep(1.0)
        child.send("\r")
        child.read_until("Цифра 1-3 — запуск", timeout=15)
        # Повторный запуск: Esc отменяет пароль без зависания
        child.send("1")
        got = child.read_until("Выбор разделов", timeout=25)
        check(got is not None,
              "TUI: повторный запуск — экран выбора разделов снова")
        child.send("\r")
        child.read_until("итоговые параметры", timeout=25)
        # ТЗ §7.3 п.6: подтверждение параметров — Enter; затем экран
        # «Алгоритм сжатия», затем диалог пароля
        child.send("\r")
        child.read_until("Алгоритм сжатия", timeout=25)
        child.send("\r")
        got = child.read_until("Задайте пароль GPG", timeout=25)
        check(got is not None, "TUI: повторный запуск — диалог пароля снова")
        child.send(chr(27))  # Esc
        got = child.read_until("операция отменена", timeout=25)
        check(got is not None,
              "TUI: Esc отменяет установку пароля и возвращает в меню")
        child.send("q")  # v1.3.1: выход — клавиша q
    finally:
        child.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def _make_app_copy(prefix, *, remove_backup_config=False):
    """Копия проекта + драйвер TUI через AppConfig (v1.4.6, ТЗ 2.1)."""
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix=prefix)
    work = Path(tmp) / "bbm"
    shutil.copytree(PROJECT, work,
                    ignore=shutil.ignore_patterns("__pycache__", "logs"))
    if remove_backup_config:
        # ТЗ 2.1: TUI запускается БЕЗ предварительно заполненного конфига
        (work / "backup_config.py").unlink()
    driver = (
        "import sys\n"
        "sys.path.insert(0, __file__.rsplit('/', 1)[0])\n"
        "from core import snapshots as _snap\n"
        "_snap.read_mount_lines = lambda: [\n"
        "    '/dev/sda1[/@] / btrfs rw,relatime,ssd,subvol=/@ 0 0',\n"
        "    '/dev/sda1[/@home] /home btrfs rw,relatime,ssd,"
        "subvol=/@home 0 0',\n"
        "]\n"
        "_snap.discover_sources = lambda exclude_dirs=(), timeout=60, "
        "mount_lines=None: {'sources': [{'name': 'home', "
        "'source': '/home', 'id': None, 'fs_uuid': None, "
        "'fs_root': False}], 'errors': []}\n"
        "_snap.list_partitions = lambda: []\n"
        "import config\n"
        "from core import appconfig\n"
        "cfg = appconfig.from_module(config)\n"
        "from ui import tui\n"
        "sys.exit(tui.run(cfg))\n"
    )
    (work / "driver_tui.py").write_text(driver, encoding="utf-8")
    return tmp, work


def test_tui_no_config():
    """
    TUI v1.4.6 (ТЗ 2.1, 2.2): запуск БЕЗ backup_config.py — не падает,
    главный экран сообщает про обнаружение источников, пункт «Профили
    конфигурации» доступен, выбор разделов показывает обнаруженные
    subvolume, единая проверка источников выполняется до операции.
    """
    import shutil
    tmp, work = _make_app_copy("bbm-nocfg-", remove_backup_config=True)
    child = Pty([sys.executable, "driver_tui.py"], cwd=str(work))
    try:
        got = child.read_until("Btrfs Backup Manager", timeout=25)
        check(got is not None, "Без конфига: TUI запускается (не падает)")
        # Строка внутреннего имени профиля удалена (§4.1); TUI без
        # конфигурации по-прежнему работает через обнаружение источников
        got = child.read_until("Отправлено на сетевую шару", timeout=10)
        check(got is not None,
              "Без конфига: статус-панель отображается (§4.2)")
        check("Профиль:".encode() not in child.buffer,
              "Без конфига: строки профиля на экране нет (§4.1)")
        child.send("1")
        # Выбор разделов: обнаруженный subvolume показан
        child.send("1")
        got = child.read_until("Выбор разделов", timeout=25)
        check(got is not None,
              "Без конфига: экран выбора открыт (обнаружение)")
        got = child.read_until("/home", timeout=10)
        check(got is not None,
              "Без конфига: обнаруженный subvolume /home показан")
        # Выбор обнаруженного источника -> единая проверка (ТЗ 3):
        # в песочнице /home не на Btrfs — экран проблем с причиной
        child.send(" ")
        child.send("\r")
        got = child.read_until("Проверка источников", timeout=25)
        check(got is not None,
              "Без конфига: после выбора — единая проверка источников")
        got = child.read_until("файловой системе Btrfs", timeout=15)
        check(got is not None,
              "Без конфига: проблемный источник показан с понятной причиной")
        child.send(chr(27))  # Esc — отмена операции
        child.read_until("Цифра 1-3 — запуск", timeout=15)
        # ТЗ 1.4.7 §7.2: пункта профилей в главном меню больше нет
        child.send("p")
        import time as _t
        _t.sleep(0.5)
        check("Профили конфигурации:" not in child.buffer.decode(
                  "utf-8", "replace"),
              "ТЗ §7.2: клавиша p больше не открывает меню профилей")
        child.send("q")
    finally:
        child.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def test_tui_menu_four_items():
    """ТЗ 1.4.7 §7.1/§7.2: главное меню — 4 пункта, профилей нет."""
    import shutil
    tmp, work = _make_app_copy("bbm-menu4-")
    child = Pty([sys.executable, "driver_tui.py"], cwd=str(work))
    try:
        got = child.read_until("Btrfs Backup Manager", timeout=25)
        check(got is not None, "Меню 4: главный экран")
        child.read_until("Цифра 1-3 — запуск", timeout=15)
        text = child.buffer.decode("utf-8", "replace")
        for item in ("Резервное копирование", "Восстановление",
                     "Локальные снапшоты", "Выход"):
            check(item in text, "Меню 4: пункт «{}» присутствует".format(item))
        for removed in ("Профили конфигурации", "Диагностика системы",
                        "Просмотр журнала", "Настройки и информация",
                        "Локальные snapshots"):
            check(removed not in text,
                  "Меню 4: «{}» удалено из главного меню".format(removed))
        check("Цифра 1-3 — запуск" in text,
              "Меню 4: подсказка соответствует 4 пунктам")
        child.send("q")
    finally:
        child.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def test_tui_dirs_flow():
    """
    ТЗ 1.4.7 §6.1/§6.2: 1) «Ввести путь вручную» открывает поле ввода
    (Enter выполняет действие, не возвращает None); 2) автоподбор
    показывает результат на отдельном экране (не «без реакции»).
    Драйвер: check_source не проходит, пока каталог снапшотов не задан
    индивидуально (эмуляция §4.1: глобальный SNAPSHOT_DIR чужой ФС).
    """
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="bbm-dirs-")
    work = Path(tmp) / "bbm"
    shutil.copytree(PROJECT, work,
                    ignore=shutil.ignore_patterns("__pycache__", "logs"))
    (work / "driver_dirs.py").write_text(
        "import sys\n"
        "sys.path.insert(0, __file__.rsplit('/', 1)[0])\n"
        "from core import snapshots as _snap\n"
        "_snap.read_mount_lines = lambda: [\n"
        "    '/dev/sda1[/@] / btrfs rw,relatime,ssd,subvol=/@ 0 0',\n"
        "    '/dev/sda1[/@home] /home btrfs rw,relatime,ssd,"
        "subvol=/@home 0 0',\n"
        "]\n"
        "_snap.discover_sources = lambda exclude_dirs=(), timeout=60, "
        "mount_lines=None: {'sources': [{'name': 'home', 'source': "
        "'/home', 'id': None, 'fs_uuid': None, 'fs_root': False}], "
        "'errors': []}\n"
        "_snap.list_partitions = lambda: []\n"
        "import config\n"
        "config.SUBVOLUMES = ['/home']\n"
        "from core import pathcheck as _pc\n"
        "def _fake_check_source(item, cfg, create_dirs=False):\n"
        "    sdir = item.get('snapshot_dir')\n"
        "    if sdir and sdir != cfg.SNAPSHOT_DIR:\n"
        "        return {'name': item['name'], 'source': item['source'],\n"
        "                'ok': True, 'errors': [], 'warnings': [],\n"
        "                'snapshot_dir': sdir,\n"
        "                'local_backup_dir': sdir + '/backup'}\n"
        "    return {'name': item['name'], 'source': item['source'],\n"
        "            'ok': False,\n"
        "            'errors': ['глобальный каталог снапшотов SNAPSHOT_DIR "
        "находится на ДРУГОЙ файловой системе Btrfs; задайте "
        "индивидуальный каталог'], 'warnings': [],\n"
        "            'snapshot_dir': sdir,\n"
        "            'local_backup_dir': None,\n"
        "            'proposed_snapshot_dir': None,\n"
        "            'proposal_reason': 'эмуляция: глобальный каталог на "
        "чужой ФС'}\n"
        "_pc.check_source = _fake_check_source\n"
        "_pc.check_sources = lambda items, cfg, create_dirs=False: "
        "[_fake_check_source(i, cfg, create_dirs=create_dirs) "
        "for i in items]\n"
        "from ui import tui\n"
        "sys.exit(tui.run(config))\n",
        encoding="utf-8")
    child = Pty([sys.executable, "driver_dirs.py"], cwd=str(work))
    try:
        check(child.read_until("Btrfs Backup Manager", timeout=25)
              is not None, "Каталоги: главный экран")
        child.clear_buffer()
        child.send("1")
        check(child.read_until("Выбор разделов", timeout=25) is not None,
              "Каталоги: экран выбора открыт")
        # /home совпадает с обнаружением — по умолчанию отмечена (ТЗ §8)
        child.send("\r")
        # Единая проверка: источник не проходит (глобальный каталог чужой ФС)
        check(child.read_until("Проверка источников", timeout=25)
              is not None, "Каталоги: экран проблем показан")
        check(child.read_until("глобальный каталог снапшотов", timeout=15)
              is not None, "Каталоги: причина объяснена (§4.1)")
        # A — автоподбор: результат ОБЯЗАН показаться (§6.2)
        child.send("a")
        check(child.read_until("Автоматический подбор каталогов: результат",
                               timeout=25) is not None,
              "ТЗ §6.2: автоподбор показывает экран результата")
        check(child.read_until("ПОДОБРАТЬ НЕ УДАЛОСЬ", timeout=15)
              is not None,
              "ТЗ §6.2: неудача автоподбора объяснена (не без реакции)")
        # Enter — продолжить проверку после результата автоподбора (§6.2);
        # затем E на экране проблем — правка вручную (§6.1, §7.3 пп.4-5)
        child.send("\r")
        got = child.read_until("Проверка источников", timeout=25)
        check(got is not None, "Каталоги: повторная проверка после автоподбора")
        child.send("e")
        got = child.read_until("Источник для правки каталогов", timeout=25)
        check(got is not None, "Каталоги: экран правки открыт")
        child.send("\r")   # выбрать источник
        got = child.read_until("Каталоги источника", timeout=25)
        check(got is not None, "Каталоги: подменю каталогов источника")
        child.send("\r")   # пункт «Каталог снапшотов»
        got = child.read_until("Ввести путь вручную", timeout=25)
        check(got is not None,
              "ТЗ §6.1: пункт «Ввести путь вручную» виден в списке")
        child.send("\r")   # Enter на «Ввести путь вручную»
        got = child.read_until("абсолютный путь", timeout=25)
        check(got is not None,
              "ТЗ §6.1: Enter ОТКРЫВАЕТ поле ввода (не возвращает None)")
        child.send(str(Path(tmp) / "manual-snap"))
        import time as _t
        _t.sleep(0.3)
        child.send("\r")
        check(child.read_until("Создать?", timeout=25)
              is not None, "Каталоги: предложено создать каталог")
        child.send("1")   # Да — каталог создан, путь принят проверкой
        # После правки подменю каталогов источника открывается снова
        # (§7.3 пп.4-5: каталог снапшотов и архивов правятся независимо)
        got = child.read_until("Каталоги источника", timeout=25)
        check(got is not None, "Каталоги: подменю открыто снова после правки")
        child.send(chr(27))  # Esc — правка завершена
        got = child.read_until("итоговые параметры", timeout=30)
        check(got is not None,
              "ТЗ §6.1: путь принят проверкой — итоговые параметры")
        child.send(chr(27))  # Esc — отмена операции
        child.read_until("Цифра 1-3 — запуск", timeout=15)
        child.send("q")
    finally:
        child.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def test_tui_subvolume_selection():
    """
    TUI: выбор subvolume перед резервным копированием (замечание
    пользователя 1.2: «нет выбора разделов»). Создается временная копия
    проекта с ДРАЙВЕРОМ, который подменяет таблицу монтирований на
    Btrfs-машиину (в песочнице нет настоящих Btrfs-монтирований);
    проверяются отметки, Пробел, A (все/ничего), Enter — запуск до
    ошибки окружения — и ЗАМОРОЗКА таймера после завершения/ошибки
    (v1.4.2, замечание: «таймер выполнения сверху продолжает идти»).
    После теста копия удаляется.
    """
    import re
    import shutil
    tmp, work = make_btrfs_copy("bbm-sel-", ["/", "/home"])

    def drain(child, seconds=0.5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([child.master], [], [], 0.1)
            if ready:
                try:
                    child.buffer += os.read(child.master, 65536)
                except OSError:
                    break

    def screen_has(child, needle):
        drain(child)
        return needle in child.buffer.decode("utf-8", "replace")

    def screen_lacks(child, needle):
        drain(child)
        return needle not in child.buffer.decode("utf-8", "replace")

    def last_elapsed(child):
        drain(child)
        matches = re.findall(
            r"время: (\d\d:\d\d:\d\d)",
            child.buffer.decode("utf-8", "replace"))
        return matches[-1] if matches else None

    child = Pty([sys.executable, "driver_tui.py"], cwd=str(work))
    try:
        check(child.read_until("Btrfs Backup Manager", timeout=25) is not None,
              "выбор: главный экран")
        child.clear_buffer()
        child.send("1")
        check(screen_has(child, "Выбор разделов"),
              "выбор: экран выбора разделов открыт")
        # v1.4.1: в списке сам путь источника, группировка по устройству,
        # сноска о разделах конфигурации вместо пометки «из конфигурации».
        # Точный вид строки зависит от группировки (в группе из двух
        # записей у последней префикс дерева «└─»), поэтому допускаются
        # оба варианта
        check(screen_has(child, "[x] └─/home") or
              screen_has(child, "[x] /home"),
              "выбор: subvolume конфигурации отмечен по умолчанию")
        check(screen_has(child, "отмечены заранее"),
              "выбор: сноска о разделах конфигурации (v1.4.1)")
        check(screen_lacks(child, "из конфигурации") and
              screen_lacks(child, "найден в системе"),
              "выбор: пометки origin убраны (v1.4.1)")
        check(screen_has(child, "Выбрано: 2 из 2"),
              "выбор: оба subvolume конфигурации отмечены")
        child.send(" ")  # курсор на первой строке списка
        check(screen_has(child, "Выбрано: 1 из 2"),
              "выбор: Пробел снял отметку первой строки")
        child.send("A")  # смешанное состояние -> отметить все
        check(screen_has(child, "Выбрано: 2 из 2"),
              "выбор: A отметил все (из смешанного состояния)")
        child.send("A")  # все отмечены -> снять все
        check(screen_has(child, "Выбрано: 0 из 2"),
              "выбор: A снял все")
        child.send("A")  # снова все
        check(screen_has(child, "Выбрано: 2 из 2"),
              "выбор: A вернул все")
        # v1.4.2: Enter с отметками продолжает — до диалога пароля
        child.send("\r")
        child.send("\r")
        check(child.read_until("итоговые параметры", timeout=25) is not None,
              "выбор: Enter открывает итоговые параметры (ТЗ 2.1 п.10)")
        child.send("\r")
        check(child.read_until("Алгоритм сжатия", timeout=25) is not None,
              "выбор: Enter открывает выбор сжатия (ТЗ §7.3 п.6)")
        child.send("\r")
        check(child.read_until("Задайте пароль GPG", timeout=25) is not None,
              "выбор: Enter с отметками открывает пароль (v1.4.2)")
        child.send("tp\r")
        check(child.read_until("Повторите пароль", timeout=25) is not None,
              "выбор: повтор пароля запрошен")
        child.send("tp\r")
        # Прогресс: операция падает на проверке окружения (не root) —
        # экран показывает ошибку и «завершено»
        got = child.read_until(["ОШИБКА", "завершено"], timeout=30)
        check(got is not None,
              "прогресс: операция завершена (ошибка окружения ожидаема)")
        # v1.4.2: таймер ЗАМОРАЖИВАЕТСЯ после завершения операции —
        # раньше продолжал идти до нажатия клавиши
        frozen = last_elapsed(child)
        time.sleep(2.5)
        again = last_elapsed(child)
        check(frozen is not None and again == frozen,
              "прогресс: таймер заморожен после завершения ({} — "
              "v1.4.2)".format(frozen))
        check(screen_has(child, "завершено"),
              "прогресс: статус «завершено»")
        child.send("\r")  # возврат в меню
        check(child.read_until("Цифра 1-3 — запуск", timeout=15)
              is not None,
              "прогресс: Enter возвращает в меню")
        child.send("q")  # v1.3.1: выход — клавиша q
    finally:
        child.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def test_tui_no_btrfs_vm():
    """
    TUI (v1.4.2, замечание пользователя: «запустил на вм, где нет btrfs,
    оно показывает что нет разделов btrfs, но все равно дает нажать
    Enter и провалиться дальше... написано точка монтирования не найдена
    и под этой надписью идет [x] / который дает пройти дальше»):
    источник конфигурации без монтирования Btrfs показывается [–] с
    причиной, по умолчанию НЕ отмечен, Enter НЕ продолжает операцию
    (ни пароль, ни прогресс не открываются), предупреждение видно сразу.
    """
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="bbm-vm-")
    work = Path(tmp) / "bbm"
    shutil.copytree(PROJECT, work,
                    ignore=shutil.ignore_patterns("__pycache__", "logs"))
    # Драйвер «ВМ без Btrfs»: корень — ext4, других ФС нет
    (work / "driver_vm.py").write_text(
        "import sys\n"
        "sys.path.insert(0, __file__.rsplit('/', 1)[0])\n"
        "from core import snapshots as _snap\n"
        "_snap.read_mount_lines = lambda: [\n"
        "    '/dev/sda2 / ext4 rw,relatime 0 0',\n"
        "    '/dev/sdc1 /boot/efi vfat rw,relatime 0 0',\n"
        "]\n"
        "_snap.discover_sources = lambda exclude_dirs=(), timeout=60, mount_lines=None: {'sources': [], 'errors': []}\n"
        "_snap.list_partitions = lambda: []\n"
        "import config\n"
        "config.SUBVOLUMES = ['/']\n"
        "from ui import tui\n"
        "sys.exit(tui.run(config))\n",
        encoding="utf-8")

    def drain(child, seconds=0.5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([child.master], [], [], 0.1)
            if ready:
                try:
                    child.buffer += os.read(child.master, 65536)
                except OSError:
                    break

    def screen_has(child, needle):
        drain(child)
        return needle in child.buffer.decode("utf-8", "replace")

    def screen_lacks(child, needle):
        drain(child)
        return needle not in child.buffer.decode("utf-8", "replace")

    child = Pty([sys.executable, "driver_vm.py"], cwd=str(work))
    try:
        check(child.read_until("Btrfs Backup Manager", timeout=25) is not None,
              "ВМ: главный экран")
        child.clear_buffer()
        child.send("1")
        check(screen_has(child, "Выбор разделов"),
              "ВМ: экран выбора открыт")
        check(screen_has(child, "(точка монтирования не найдена):"),
              "ВМ: группа без устройства подписана (v1.4.1)")
        check(screen_has(child, "[–] /") and
              screen_has(child, "нет точки монтирования Btrfs"),
              "ВМ: источник конфигурации [–] с причиной (v1.4.2)")
        check(screen_lacks(child, "[x] /"),
              "ВМ: источник НЕ отмечен [x] по умолчанию (v1.4.2)")
        check(screen_has(child, "нет доступных разделов Btrfs"),
              "ВМ: предупреждение видно сразу (v1.4.2)")
        check(screen_has(child, "Выбрано: 0 из 0"),
              "ВМ: выбрано 0 из 0")
        # Enter НЕ продолжает: экран выбора остается, пароль не
        # запрашивается, операция не запускается
        child.send("\r")
        check(screen_has(child, "Выбор разделов") and
              screen_lacks(child, "Задайте пароль"),
              "ВМ: Enter с пустым выбором не продолжает (v1.4.2)")
        check(screen_has(child, "нет доступных разделов Btrfs"),
              "ВМ: предупреждение остается на экране после Enter")
        child.send(chr(27))  # Esc — отмена выбора
        # read_until (а не разовый drain): перерисовка меню должна
        # ПРИЙТИ — разовый срез буфера на загруженной машине давал
        # нестабильный результат
        check(child.read_until("Цифра 1-3 — запуск", timeout=15)
              is not None,
              "ВМ: Esc возвращает в меню")
        child.send("q")
    finally:
        child.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def test_tui_partitions_listed():
    """
    TUI (v1.4.1, замечание пользователя: «вывод дисков, где не btrfs —
    лишнее»): НЕ-Btrfs разделы в списке выбора не показываются вовсе;
    несмонтированный раздел Btrfs виден с пометкой [–] и причиной.
    Заглушки: btrfs неработоспособен, lsblk печатает разделы.
    """
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="bbm-part-")
    work = Path(tmp) / "bbm"
    shutil.copytree(PROJECT, work,
                    ignore=shutil.ignore_patterns("__pycache__", "logs"))
    stub = Path(tmp) / "stub"
    stub.mkdir()
    (stub / "btrfs").write_text("#!/bin/sh\nexit 1\n")
    (stub / "btrfs").chmod(0o755)
    (stub / "lsblk").write_text(
        "#!/bin/sh\n"
        "echo 'sda1 part vfat'\n"
        "echo 'sda2 part ext4'\n"
        "echo 'sda3 part btrfs'\n")
    (stub / "lsblk").chmod(0o755)
    child_env = dict(os.environ)
    child_env["PATH"] = str(stub) + ":" + child_env.get("PATH", "")

    def drain(child, seconds=0.5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([child.master], [], [], 0.1)
            if ready:
                try:
                    child.buffer += os.read(child.master, 65536)
                except OSError:
                    break

    def screen_has(child, needle):
        drain(child)
        return needle in child.buffer.decode("utf-8", "replace")

    def screen_lacks(child, needle):
        drain(child)
        return needle not in child.buffer.decode("utf-8", "replace")

    child = Pty([sys.executable, "main.py", "tui"], cwd=str(work),
                env=child_env)
    try:
        check(child.read_until("Btrfs Backup Manager", timeout=25) is not None,
              "разделы: главный экран")
        child.clear_buffer()
        child.send("1")
        check(screen_has(child, "Выбор разделов"),
              "разделы: экран выбора открыт")
        check(screen_lacks(child, "vfat") and screen_lacks(child, "ext4"),
              "разделы: не-Btrfs разделы не показываются (v1.4.1)")
        check(screen_lacks(child, "sda1") and screen_lacks(child, "sda2"),
              "разделы: sda1/sda2 (vfat/ext4) отсутствуют в списке")
        check(screen_has(child, "sda3") and
              screen_has(child, "не смонтирован"),
              "разделы: несмонтированный Btrfs виден с причиной")
        child.send(chr(27))  # Esc — отмена выбора
        check(child.read_until("Цифра 1-3 — запуск", timeout=15)
              is not None,
              "разделы: Esc возвращает в меню")
        child.send("q")
    finally:
        child.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def test_tui_zstd_fallback():
    """
    TUI (v1.4.3, замечание пользователя: «после ввода пароля выскакивает
    сообщение про zstd, если нажать y — сваливается в ошибку ... сжатие
    'gzip' отклонено»): причина — русская раскладка в диалоге fallback.
    v1.4.7 (ТЗ §7.3 п.6): вместо диалога подтверждения — экран выбора
    «Алгоритм сжатия» ДО пароля; недоступный zstd не предлагается,
    Enter выбирает единственный доступный gzip. Операция идет с gzip
    («Сжатие: gzip»), доходит до ошибки btrfs-заглушки и НЕ содержит
    «отклонено». Заодно: спиннер в строке статуса замирает после
    завершения операции (v1.4.3).
    """
    import re
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="bbm-zstd-")
    work = Path(tmp) / "bbm"
    shutil.copytree(PROJECT, work,
                    ignore=shutil.ignore_patterns("__pycache__", "logs"))
    # Драйвер: Btrfs-машиина (/dev/sda1[/@]), окружение обезопасено
    # (root/валидация/утилиты/каталоги подменены), сеть отключена.
    (work / "driver_zstd.py").write_text(
        "import sys, tempfile, os\n"
        "sys.path.insert(0, __file__.rsplit('/', 1)[0])\n"
        "from core import snapshots as _snap\n"
        "_snap.read_mount_lines = lambda: [\n"
        "    '/dev/sda1[/@] / btrfs rw,relatime,ssd,subvol=/@ 0 0',\n"
        "]\n"
        "_snap.is_btrfs_path = lambda path: True\n"
        "_snap.is_subvolume = lambda path: True\n"
        "_snap.btrfs_fs_identity = lambda path: 'fake-btrfs-uuid'\n"
        "_snap.discover_sources = lambda exclude_dirs=(), timeout=60, mount_lines=None: {'sources': [{'name': 'sv0', 'source': '/', 'id': None, 'fs_uuid': None, 'fs_root': False}], 'errors': []}\n"
        "_snap.list_partitions = lambda: []\n"
        "from core import environment as _env\n"
        "_env.require_root = lambda: None\n"
        "_env.validate_or_raise = lambda cfg, **kw: None\n"
        "_env.check_utilities = lambda **kw: None\n"
        "_work = os.path.join(os.path.dirname(__file__), '.bbm-work')\n"
        "_env.ensure_local_dirs = lambda base: _work\n"
        "import config\n"
        "config.SUBVOLUMES = ['/']\n"
        "config.NETWORK_ENABLED = False\n"
        "config.LOCK_FILE = _work + os.sep + 'test.lock'\n"
        "config.SNAPSHOT_DIR = _work + os.sep + 'snaps'\n"
        "config.LOCAL_BACKUP_DIR = _work + os.sep + 'archives'\n"
        "from core import pathcheck as _pc\n"
        "def _fake_check_source(item, cfg, create_dirs=False):\n"
        "    sdir = item.get('snapshot_dir') or cfg.SNAPSHOT_DIR\n"
        "    local = (item.get('local_backup_dir') or\n"
        "             cfg.local_backup_dir(cfg.SNAPSHOT_DIR))\n"
        "    return {'name': item['name'], 'source': item['source'],\n"
        "            'ok': True, 'errors': [], 'warnings': [],\n"
        "            'snapshot_dir': sdir, 'local_backup_dir': local}\n"
        "_pc.check_source = _fake_check_source\n"
        "from ui import tui\n"
        "sys.exit(tui.run(config))\n",
        encoding="utf-8")
    stub = Path(tmp) / "stub"
    stub.mkdir()
    # В песочнице zstd НЕТ (проверено), gzip есть: появляется диалог
    # fallback. btrfs-заглушка падает — операция продолжается после
    # подтверждения и завершается ошибкой btrfs (доказательство продолжения).
    (stub / "btrfs").write_text("#!/bin/sh\nexit 1\n")
    (stub / "btrfs").chmod(0o755)
    # Изолированный PATH делает сценарий детерминированным даже на хосте,
    # где zstd установлен: видны только btrfs-заглушка и реальный gzip.
    gzip_binary = shutil.which("gzip")
    if gzip_binary:
        (stub / "gzip").symlink_to(gzip_binary)

    def drain(child, seconds=0.5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([child.master], [], [], 0.1)
            if ready:
                try:
                    child.buffer += os.read(child.master, 65536)
                except OSError:
                    break

    def screen_has(child, needle):
        drain(child)
        return needle in child.buffer.decode("utf-8", "replace")

    def screen_lacks(child, needle):
        drain(child)
        return needle not in child.buffer.decode("utf-8", "replace")

    def last_status(child):
        drain(child)
        matches = re.findall(
            r"\[([|\-\\/])\] (выполняется|завершено)",
            child.buffer.decode("utf-8", "replace"))
        return matches[-1] if matches else None

    child_env = dict(os.environ)
    # Изолированный PATH: zstd отсутствует, gzip доступен, btrfs
    # присутствует как контролируемая заглушка с отказом на snapshot.
    child_env["PATH"] = str(stub)
    child = Pty([sys.executable, "driver_zstd.py"], cwd=str(work),
                env=child_env)
    try:
        check(child.read_until("Btrfs Backup Manager", timeout=25)
              is not None, "zstd: главный экран")
        child.clear_buffer()
        child.send("1")
        check(child.read_until("Выбор разделов", timeout=25) is not None,
              "zstd: экран выбора разделов")
        child.send("\r")
        check(child.read_until("итоговые параметры", timeout=25)
              is not None,
              "zstd: итоговые параметры (ТЗ 2.1 п.10)")
        child.send("\r")
        # ТЗ 1.4.7 §7.3 п.6: алгоритм сжатия выбирается ДО пароля;
        # zstd в песочнице нет — предлагается только доступный gzip
        got = child.read_until("Алгоритм сжатия", timeout=25)
        check(got is not None,
              "zstd: экран выбора сжатия показан")
        drain(child)  # дождаться полной отрисовки кадра (список gzip)
        text = child.buffer.decode("utf-8", "replace")
        check("gzip" in text and "zstd" not in text.split("gzip")[0],
              "zstd: недоступный zstd не предлагается, есть gzip")
        time.sleep(0.3)
        child.clear_buffer()
        child.send("\r")  # Enter — выбрать единственный доступный gzip
        check(child.read_until("Задайте пароль GPG", timeout=25)
              is not None,
              "zstd: Enter выбрал gzip — диалог пароля")
        child.send("tp\r")
        child.read_until("Повторите пароль", timeout=25)
        child.send("tp\r")
        # Операция очень короткая: отдельный кадр со строкой сжатия
        # может не попасть в PTY до завершения потока. Проверяем надежный
        # маркер: после выбора единственного доступного gzip TUI запустил
        # резервирование и дошел до реальной стадии создания снапшота.
        got = child.read_until(["ОШИБКА", "завершено"], timeout=30)
        operation_text = child.buffer.decode("utf-8", "replace")
        check(got is not None and
              "Создание read-only снапшотов" in operation_text and
              "Сжатие: gzip" in operation_text and
              "Сжатие: zstd" not in operation_text,
              "zstd: выбор gzip запускает операцию до стадии снапшотов")
        check(screen_lacks(child, "отклонено"),
              "zstd: отмены «отклонено» нет (v1.4.3)")
        # v1.4.3: спиннер заморожен после завершения — символ в
        # статусе больше не меняется
        time.sleep(1.0)
        frozen = last_status(child)
        time.sleep(2.0)
        check(frozen is not None and last_status(child) == frozen,
              "zstd: спиннер заморожен после завершения (v1.4.3)")
        child.send("\r")
        child.read_until("Цифра 1-3 — запуск", timeout=15)
        child.send("q")
    finally:
        child.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def test_tui_local_copies_view():
    """
    §4.3: пункт «Локальные снапшоты» — вложенный выбор (снапшоты Btrfs /
    локальные архивы / назад; не новый пункт главного меню). Список
    снапшотов показывает источник и дату; список архивов — сжатие,
    состояние проверки и сетевой отправки. Esc возвращает назад.
    """
    import json as _json
    tmp, work = _make_app_copy("bbm-copies-")
    data = Path(tmp) / "data"
    snaps = data / "snaps"
    arch = data / "archives"
    snaps.mkdir(parents=True)
    arch.mkdir(parents=True)
    (snaps / "root_2026-01-15_103000").mkdir()   # снапшот с меткой времени
    archive = arch / "host_root_2026-11-16_120000_ab12cd.gz.gpg"
    archive.write_bytes(b"smoke-archive")
    (arch / (archive.name + ".json")).write_text(_json.dumps({
        "subvolume_name": "root",
        "created": "2026-11-16T12:00:00.123456",
        "compression": "gzip",
        "status": "verified",
    }), encoding="utf-8")
    (work / "backup_config.py").write_text(
        "SUBVOLUMES = [{'name': 'root', 'source': '/nonexistent'}]\n"
        "SNAPSHOT_DIR = " + repr(str(snaps)) + "\n"
        "LOCAL_BACKUP_DIR = " + repr(str(arch)) + "\n"
        "GPG_PASSWORD = 'x'\n"
        "NETWORK_ENABLED = False\n"
        "NETWORK_MOUNT = None\n", encoding="utf-8")
    child = Pty([sys.executable, "driver_tui.py"], cwd=str(work))
    try:
        got = child.read_until("Btrfs Backup Manager", timeout=25)
        check(got is not None, "Копии: TUI запускается")
        child.send("3")
        got = child.read_until("Локальные снапшоты и архивы", timeout=10)
        check(got is not None,
              "§4.3: вложенный выбор открыт (это не новый пункт меню)")
        got = child.read_until("Назад", timeout=10)
        check(got is not None and
              "Снапшоты Btrfs".encode() in child.buffer and
              "Локальные архивы".encode() in child.buffer,
              "§4.3: варианты — снапшоты / архивы / назад")
        # Снапшоты Btrfs: источник, имя, read-only, дата
        child.send("\r")
        got = child.read_until("root_2026-01-15_103000", timeout=10)
        check(got is not None, "§4.3: снапшот показан по имени")
        got = child.read_until(["неизвестно", "read-only",
                                "записываемый"], timeout=10)
        check(got is not None,
              "§4.3: состояние read-only отображается честно "
              "(без подмены btrfs)")
        got = child.read_until("дата: 2026-01-15 10:30", timeout=10)
        check(got is not None, "§4.3: у снапшота видна дата (из имени)")
        got = child.read_until("(источник root)", timeout=10)
        check(got is not None, "§4.3: у снапшота виден источник")
        child.send("\x1b")   # Esc — назад из списка
        got = child.read_until("Локальные снапшоты и архивы", timeout=10)
        check(got is not None, "§4.3: Esc вернул к выбору списков")
        # Локальные архивы: сжатие, проверка, сеть, источник
        child.send("j")
        child.send("\r")
        got = child.read_until("host_root_2026-11-16_120000_ab12cd.gz.gpg",
                               timeout=10)
        check(got is not None, "§4.3: архив показан по имени")
        got = child.read_until("сжатие: gzip", timeout=10)
        check(got is not None, "§4.3: алгоритм сжатия определен по архиву")
        got = child.read_until("источник: root", timeout=10)
        check(got is not None, "§4.3: у архива виден источник")
        got = child.read_until("пройдена (SHA-256)", timeout=10)
        check(got is not None, "§4.3: состояние проверки из метаданных")
        got = child.read_until("не отправлен (локальный)", timeout=10)
        check(got is not None, "§4.3: сетевая отправка — честный статус")
        # Esc из списка архивов, затем «Назад» в главное меню
        child.send("\x1b")
        got = child.read_until("Локальные снапшоты и архивы", timeout=10)
        check(got is not None, "§4.3: Esc вернул к выбору списков (архивы)")
        child.send("j")
        child.send("j")
        child.send("\r")
        got = child.read_until("Резервное копирование", timeout=10)
        check(got is not None, "§4.3: «Назад» возвращает в главное меню")
    finally:
        child.stop()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("=== Smoke-тест интерфейса (pty) ===")
    if os.geteuid() != 0:
        print("(запуск не от root: операция backup в TUI завершится ошибкой "
              "окружения — это ожидаемо и не влияет на проверки)")
    print("\n--- dialogs.confirm: одиночная клавиша ---")
    test_confirm_single_key()
    print("\n--- TUI: пароль и отмена ---")
    test_tui_password_flow()
    print("\n--- TUI: меню из 4 пунктов (1.4.7, ТЗ §7.1) ---")
    test_tui_menu_four_items()
    print("\n--- TUI: каталоги — ручной ввод и автоподбор (1.4.7, §6) ---")
    test_tui_dirs_flow()
    print("\n--- TUI: выбор subvolume ---")
    test_tui_subvolume_selection()
    print("\n--- TUI: ВМ без Btrfs (1.4.2) ---")
    test_tui_no_btrfs_vm()
    print("\n--- TUI: без конфигурации (1.4.6, ТЗ 2.1) ---")
    test_tui_no_config()
    print("\n--- TUI: все разделы в выборе (1.4) ---")
    test_tui_partitions_listed()
    print("\n--- TUI: zstd-fallback и спиннер (1.4.3) ---")
    test_tui_zstd_fallback()
    print("\n--- TUI: просмотр локальных копий (§4.3) ---")
    test_tui_local_copies_view()
    failed = sum(1 for ok, _ in RESULTS if not ok)
    print("\n=== ИТОГО: pass={} fail={} ===".format(
        len(RESULTS) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
