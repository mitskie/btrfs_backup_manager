"""
TUI — текстовый интерфейс на curses.

Требования, реализованные здесь:
  - один постоянный экран терминала; многократная печать меню запрещена;
  - главное меню и статус-панель;
  - управление: стрелки, Enter, Esc, Q, Tab, цифры;
  - прогресс операций: этап, subvolume, сжатие, индикатор выполнения,
    прошедшее время, ошибки; выдуманные проценты не показываются;
  - компактная область событий и отдельный экран журнала;
  - маленький терминал: второстепенное скрывается, curses.error не
    приводит к аварийному завершению;
  - терминал восстанавливается после Ctrl+C/SIGTERM/ошибок.

Операции выполняются в рабочем потоке; главный поток рисует экран.
Подтверждения передаются в поток через TuiConfirm (очередь вопросов).
Прерывание: abort-флаг + завершение активных конвейеров (core.process).
"""

import threading
import time
import textwrap
from pathlib import Path

from core import (
    APP_VERSION, BtrfsBackupError, OperationAborted,
)
from core import backup as backup_ops
from core import environment
from core import pathcheck as pathcheck_module
from core import restore as restore_module
from core import snapshots as snapshots_module
from core.logging_setup import get_logger
from core.process import terminate_all_active
from . import common, dialogs

log = get_logger("ui.tui")

SPINNER = "|/-\\"
# Главное меню: только четыре пункта. Диагностика —
# отдельная CLI-команда (§7.2), журналы — системными средствами,
# профили — функциональность CLI/автоматизации (§7.4).
MENU_ITEMS = [
    "Резервное копирование",
    "Восстановление",
    "Локальные снапшоты",
    "Выход",
]

# Клавиши выбора пунктов главного экрана: 1-3 — запуск пункта,
# q — выход (наряду с Q/Esc).
MENU_KEYS = ("1", "2", "3", "q")


def confirm_hint(default_yes):
    """
    Подсказка диалога подтверждения TUI.

    Основной ответ — цифры 1 (да) / 2 (нет): они одинаковы при любой
    раскладке (на русской раскладке физическая клавиша Y посылает
    кириллическую «н», поэтому латиница не может быть основным
    ответом). Латиница Y/N и кириллица Д/у/Н по-прежнему принимаются.
    """
    return "[1] Да / [2] Нет   Enter — {}".format(
        "Да" if default_yes else "Нет")


def is_available():
    """True, если TUI возможен: curses есть, терминал интерактивный."""
    if not environment.curses_available():
        return False
    import sys
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        return False
    import os
    return bool(os.environ.get("TERM"))


# ============================================
# Канал прогресса для TUI
# ============================================

class CollectReporter(backup_ops.Reporter):
    """Потокобезопасный сбор событий операции для отображения."""

    MAX_LINES = 400

    def __init__(self):
        self._lock = threading.Lock()
        self._lines = []
        self.stage_text = ""
        self.current_subvolume = ""
        self.compression = ""

    def _add(self, level, text):
        with self._lock:
            self._lines.append((level, text))
            if len(self._lines) > self.MAX_LINES:
                self._lines = self._lines[-self.MAX_LINES:]

    def stage(self, text):
        with self._lock:
            self.stage_text = text
        self._add("stage", text)

    def info(self, text):
        self._add("info", text)
        # Отслеживаем текущий subvolume и сжатие для строки статуса
        stripped = text.strip()
        if stripped.startswith("Создан read-only снапшот: "):
            self.current_subvolume = stripped.split(":", 1)[1].strip()
        if stripped.startswith("Сжатие: "):
            self.compression = stripped[len("Сжатие: "):].split(" ")[0]

    def warn(self, text):
        self._add("warn", text)

    def error(self, text):
        self._add("error", text)

    def done(self, text):
        self._add("done", text)

    def snapshot(self):
        with self._lock:
            return (list(self._lines), self.stage_text,
                    self.current_subvolume, self.compression)


class AbortFlag:
    """abort_check для core: возбуждает OperationAborted при запросе."""

    def __init__(self):
        self.event = threading.Event()

    def __call__(self):
        if self.event.is_set():
            raise OperationAborted(
                "операция прервана пользователем (Esc в TUI)")

    def request(self):
        self.event.set()


class TuiConfirm:
    """
    Потокобезопасный диалог подтверждения: рабочий поток публикует вопрос,
    главный поток отображает его и возвращает ответ.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._question = None
        self._answered = threading.Event()
        self._answer = False

    def ask(self, text):
        self._answered.clear()
        with self._lock:
            self._question = text
        self._answered.wait()  # ответ дает главный поток
        return self._answer

    def pending(self):
        with self._lock:
            return self._question

    def resolve(self, answer):
        with self._lock:
            self._question = None
        self._answer = answer
        self._answered.set()


class MultiSelect:
    """
    Логика множественного выбора subvolume (чистая, без curses —
    проверяется автотестом). Список флагов + курсор.
    """

    def __init__(self, names, checked=True):
        self.names = list(names)
        self.flags = [bool(checked)] * len(self.names)
        self.index = 0

    def toggle(self, index=None):
        index = self.index if index is None else index
        if 0 <= index < len(self.flags):
            self.flags[index] = not self.flags[index]

    def set_all(self, value):
        self.flags = [bool(value)] * len(self.flags)

    def move(self, delta):
        if not self.flags:
            return
        self.index = max(0, min(len(self.flags) - 1, self.index + delta))

    def selected_names(self):
        return [name for name, flag in zip(self.names, self.flags) if flag]

    def selected_indices(self):
        return [index for index, flag in enumerate(self.flags) if flag]


# ============================================
# Приложение
# ============================================

class TuiApp:
    """TUI-приложение: один экран, навигация, фоновые операции."""

    def __init__(self, config):
        self.config = config
        self.menu_index = 0
        self.status_cache = None
        self.status_time = 0.0
        self.message = None

    # ---------- запуск ----------

    def run(self, stdscr):
        import curses
        self.stdscr = stdscr
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)
        stdscr.timeout(-1)
        self._init_colors()
        # Полная физическая перерисовка: устраняет остатки любых символов,
        # напечатанных в терминал до запуска TUI
        self._erase()
        while True:
            self._draw_main()
            key = stdscr.getch()
            if key in (ord("q"), ord("Q"), 27):
                # Esc (27): при включенном keypad curses возвращает 27
                # ТОЛЬКО для одиночного нажатия — escape-последовательности
                # стрелок и мыши разбираются самим curses в коды KEY_*
                # (задержка распознавания — ESCDELAY, устанавливается в
                # run() до инициализации curses). Дополнительный getch()
                # не нужен: блокирующее ожидание «второй клавиши» здесь
                # было дефектом (подвешивало программу до любого нажатия).
                return 0
            if key in (curses.KEY_UP, ord("k")):
                self.menu_index = (self.menu_index - 1) % len(MENU_ITEMS)
            elif key in (curses.KEY_DOWN, ord("j"), 9):  # Tab тоже циклит
                self.menu_index = (self.menu_index + 1) % len(MENU_ITEMS)
            elif key in (10, 13, curses.KEY_ENTER):
                result = self._activate_current()
                if result is not None:
                    return result
            elif ord("1") <= key <= ord("9"):
                # Цифра МГНОВЕННО запускает пункт (без Enter, удобство UX)
                index = key - ord("1")
                if index < len(MENU_ITEMS):
                    self.menu_index = index
                    result = self._activate_current()
                    if result is not None:
                        return result
            elif key in (ord("r"), ord("R"), ord("p"), ord("П"), ord("п"),
                         ord("P")):
                # r — немедленное обновление статусов с видимой
                # обратной связью (сообщение с временем).
                self.status_cache = self._collect_status()
                self.status_time = time.monotonic()
                self.message = "Статусы обновлены ({})".format(
                    time.strftime("%H:%M:%S"))
            elif key == 12:  # Ctrl+L — полная перерисовка
                self._erase()
            elif key == curses.KEY_RESIZE:
                # ТЗ 10.1: изменение размера терминала — полная перерисовка
                self._erase()
                self.status_cache = None

    def _activate_current(self):
        """Запуск выбранного пункта меню. Возвращает 0 для выхода."""
        if self.menu_index == len(MENU_ITEMS) - 1:
            return 0
        try:
            self._handle_menu(self.menu_index)
        except OperationAborted as exc:
            self._show_error("Прервано: {}".format(exc))
        except BtrfsBackupError as exc:
            self._show_error("Ошибка: {}".format(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка TUI")
            self._show_error("Ошибка: {}".format(exc))
        return None

    def _show_error(self, text):
        """
        Показ сообщения об ошибке.

        Краткое сообщение — в строку статуса главного экрана; длинное
        (не помещается целиком) открывает отдельный экран подробностей
        с прокруткой. Полный текст всегда сохраняется в журнал.
        """
        log.error("%s", text)
        self.message = text
        try:
            height, width = self.stdscr.getmaxyx()
        except Exception:  # noqa: BLE001
            height, width = 24, 80
        wrapped = common.wrap_text(text, max(20, width - 4))
        if len(wrapped) > 2 or any(len(line) > width - 2 for line in wrapped):
            self._error_details_screen(text)

    def _init_colors(self):
        import curses
        self.attrs = {"ok": curses.A_NORMAL, "warn": curses.A_BOLD,
                      "err": curses.A_BOLD, "sel": curses.A_REVERSE,
                      "dim": curses.A_DIM, "hint": curses.A_BOLD,
                      "title": curses.A_BOLD}
        try:
            if curses.has_colors():
                curses.start_color()
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_GREEN, -1)
                curses.init_pair(2, curses.COLOR_YELLOW, -1)
                curses.init_pair(3, curses.COLOR_RED, -1)
                curses.init_pair(4, curses.COLOR_CYAN, -1)
                curses.init_pair(5, curses.COLOR_WHITE, -1)
                self.attrs["ok"] = curses.color_pair(1)
                self.attrs["warn"] = curses.color_pair(2) | curses.A_BOLD
                self.attrs["err"] = curses.color_pair(3) | curses.A_BOLD
                self.attrs["sel"] = curses.A_REVERSE
                # Два стиля вместо плохо читаемого cyan+A_DIM:
                # hint — ЯРКИЙ бирюзовый (bold, без dim) для подсказок,
                # кнопок, таймеров и сносок — то, что нужно ЧИТАТЬ;
                # dim — нейтральный тусклый белый — только для разделителей
                # и недоступных строк, которым приглушенность нужна по смыслу.
                self.attrs["hint"] = curses.color_pair(4) | curses.A_BOLD
                self.attrs["dim"] = curses.color_pair(5) | curses.A_DIM
        except curses.error:
            pass

    # ---------- помощники отрисовки ----------

    def _h(self):
        try:
            return self.stdscr.getmaxyx()[0]
        except Exception:  # noqa: BLE001
            return 24

    def _w(self):
        try:
            return self.stdscr.getmaxyx()[1]
        except Exception:  # noqa: BLE001
            return 80

    def _add(self, y, x, text, attr=None, clip=True):
        """Безопасный вывод: маленький терминал не вызывает аварий."""
        import curses
        try:
            height, width = self.stdscr.getmaxyx()
            if y < 0 or y >= height or x >= width:
                return
            if clip and x + len(text) > width - 1:
                text = text[:max(0, width - 1 - x)]
            if attr is None:
                self.stdscr.addstr(y, x, text)
            else:
                self.stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def _erase(self, full=True):
        """
        Очистка экрана. full=True — физическая перерисовка всего экрана
        (clear + clearok): гарантирует отсутствие остатков текста, если
        что-то было напечатано в терминал напрямую в обход curses.
        """
        try:
            if full:
                self.stdscr.clear()
            else:
                self.stdscr.erase()
        except Exception:  # noqa: BLE001
            pass

    def _header(self, title):
        import curses
        self._add(0, 0, title, curses.A_BOLD)
        self._add(1, 0, "=" * min(self._w() - 1, 60), self.attrs["dim"])

    # ---------- главный экран ----------

    def _collect_status(self):
        """Данные статус-панели."""
        import os
        import shutil
        cfg = self.config
        status = []
        status.append(("Пользователь", "{} ({})".format(
            os.environ.get("USER") or os.environ.get("LOGNAME") or "?",
            "root" if os.geteuid() == 0 else "БЕЗ root")))
        status.append(("Права доступа", "OK" if os.geteuid() == 0 else "нет root"))
        status.append(("Btrfs", "OK" if shutil.which("btrfs") else "NOT FOUND"))
        gpg_version = None
        try:
            from core.encryption import check_gpg
            gpg_version = check_gpg(cfg.GPG_BINARY)
        except Exception:  # noqa: BLE001
            gpg_version = None
        status.append(("GPG", gpg_version or "NOT FOUND"))
        status.append(("gzip", "OK" if shutil.which("gzip") else "NOT FOUND"))
        if shutil.which("zstd"):
            status.append(("zstd", "OK"))
        elif shutil.which("gzip"):
            status.append(("zstd", "NOT FOUND (fallback: gzip)"))
        else:
            status.append(("zstd", "NOT FOUND (gzip тоже нет!)"))
        if cfg.SNAPSHOT_DIR:
            backup_dir = Path(cfg.local_backup_dir(cfg.SNAPSHOT_DIR))
            if backup_dir.is_dir() and os.access(str(backup_dir), os.W_OK):
                status.append(("Локальное хранилище",
                               "OK ({})".format(backup_dir)))
            elif backup_dir.is_dir():
                status.append(("Локальное хранилище",
                               "НЕТ ЗАПИСИ ({})".format(backup_dir)))
            else:
                status.append(("Локальное хранилище",
                               "НЕТ ({} отсутствует)".format(backup_dir)))
            local = common.collect_local_archives(backup_dir)
        else:
            status.append(("Локальное хранилище", "не задан SNAPSHOT_DIR"))
            local = []
        mounted, detail = common.network_mount_status(cfg)
        status.append(("Сетевая шара", "OK" if mounted else
                       ("DISABLED" if detail == "DISABLED"
                        else ("НЕ НАСТРОЕНА" if detail == "NOT CONFIGURED"
                              else "NOT MOUNTED"))))
        pending = 0
        for path in local:
            label = common.archive_status_label(path)
            if label in ("created", "verified", "upload-pending",
                         "uploading", "upload-failed", "restore-failed", "?"):
                pending += 1
        status.append(("Локальные архивы", str(len(local))))
        status.append(("Ожидают отправки", str(pending)))
        # Счётчик — РЕАЛЬНО подтвержденные сетевые копии: архивы,
        # физически находящиеся на шаре (они попадают туда только после
        # передачи, проверки содержимого и SHA-256). Шара не смонтирована —
        # число не выдумывается.
        if mounted and getattr(cfg, "NETWORK_MOUNT", None):
            net_dir = Path(cfg.NETWORK_MOUNT) / cfg.NETWORK_DIRECTORY
            network = common.collect_network_archives(net_dir)
            status.append(("Отправлено на сетевую шару", str(len(network))))
        else:
            status.append(("Отправлено на сетевую шару", "нет данных"))
        from core.report import read_last_backup_state
        state = read_last_backup_state()
        if state:
            mark = "Успешно" if state.get("result") == "success" else "ОШИБКА"
            status.append(("Последний backup", "{} ({})".format(
                mark, state.get("when", "?"))))
        else:
            status.append(("Последний backup", "нет данных"))
        return status

    def _draw_main(self):
        import curses
        self._erase()
        height, width = self.stdscr.getmaxyx()
        self._header("Btrfs Backup Manager {}".format(APP_VERSION))
        if self.status_cache is None or \
                time.monotonic() - self.status_time > 2.0:
            self.status_cache = self._collect_status()
            self.status_time = time.monotonic()
        row = 2
        compact = height < 24
        for label, value in self.status_cache:
            if compact and label in ("Отправлено на сетевую шару", "Локальные архивы"):
                continue  # маленький терминал: второстепенное скрывается
            attr = self.attrs["ok"]
            if any(word in value for word in
                   ("NOT", "НЕТ", "БЕЗ", "ОШИБКА", "нет")):
                attr = self.attrs["warn"] if "fallback" in value or \
                    "нет данных" in value else self.attrs["err"]
            self._add(row, 1, "{:22} {}".format(label + ":", value), attr)
            row += 1
        row += 1
        if self.message:
            # ТЗ 10.1: перенос по ширине терминала, не более 3 строк;
            # полный текст длинных ошибок — на экране подробностей
            wrapped = common.wrap_text(self.message, max(20, width - 4))[:3]
            for line in wrapped:
                self._add(row, 1, line, self.attrs["warn"])
                row += 1
        self._add(row, 0, "-" * min(width - 1, 60), self.attrs["dim"])
        row += 1
        for index, item in enumerate(MENU_ITEMS):
            marker = "> " if index == self.menu_index else "  "
            attr = self.attrs["sel"] if index == self.menu_index else None
            self._add(row, 1, "{}{}. {}".format(
                marker, MENU_KEYS[index], item), attr)
            row += 1
        self._add(height - 1, 0,
                  "Цифра 1-3 — запуск, стрелки/Tab — выбор, "
                  "r — обновить статусы, q/Esc — выход", self.attrs["hint"])
        try:
            self.stdscr.refresh()
        except curses.error:
            pass

    # ---------- операции ----------

    def _run_operation(self, title, op_callable):
        """Выполняет операцию в рабочем потоке с экраном прогресса."""
        import curses
        reporter = CollectReporter()
        confirm = TuiConfirm()
        abort = AbortFlag()

        def worker():
            try:
                op_callable(reporter=reporter, confirm_fn=confirm.ask,
                            abort_check=abort)
                reporter._add("finish", "ОПЕРАЦИЯ ЗАВЕРШЕНА УСПЕШНО")
            except OperationAborted as exc:
                reporter._add("error", "ПРЕРВАНО: {}".format(exc))
            except BtrfsBackupError as exc:
                reporter._add("error", "ОШИБКА: {}".format(exc))
            except Exception as exc:  # noqa: BLE001
                log.exception("Ошибка операции в TUI")
                reporter._add("error", "ОШИБКА: {}".format(exc))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self._progress_screen(title, reporter, confirm, abort, thread)
        thread.join(timeout=30)
        terminate_all_active()  # страховка от зависших потомков
        with reporter._lock:
            return any(level == "finish" for level, _t in reporter._lines)

    def _progress_screen(self, title, reporter, confirm, abort, thread):
        import curses
        self.stdscr.timeout(150)
        frame = 0
        spin_frame = 0
        started = time.monotonic()
        # Время замораживается в момент завершения рабочего потока —
        # успех, ошибка или прерывание (таймер не «продолжает идти»).
        stopped = None
        try:
            while True:
                question = confirm.pending()
                if question is not None:
                    answer = self._confirm_dialog(question)
                    confirm.resolve(answer)
                    continue
                lines, stage, subvol, comp = reporter.snapshot()
                running = thread.is_alive()
                if not running and stopped is None:
                    stopped = time.monotonic()
                    # Спиннер замирает в момент завершения операции —
                    # успех, ошибка или прерывание.
                    spin_frame = frame
                elapsed = int((stopped or time.monotonic()) - started)
                self._draw_progress(title, stage, subvol, comp, lines,
                                    elapsed, running,
                                    frame if running else spin_frame,
                                    full=(frame == 0))
                frame += 1
                key = self.stdscr.getch()
                if not running and key in (10, 13, 27,
                                           curses.KEY_ENTER,
                                           ord("q"), ord("Q")):
                    break
                if key in (27, ord("c"), ord("C"), ord("q"), ord("Q")) and \
                        running:
                    if self._confirm_dialog(
                            "Прервать текущую операцию? Рабочие процессы "
                            "будут корректно завершены.") :
                        abort.request()
                        terminate_all_active()
                if key == curses.KEY_RESIZE:
                    self._erase()  # полная перерисовка после смены размера
        finally:
            self.stdscr.timeout(-1)

    def _draw_progress(self, title, stage, subvol, comp, lines, elapsed,
                       running, spin_frame, full=False):
        """spin_frame — кадр спиннера; после завершения операции он
        заморожен, при работе — текущий кадр анимации."""
        import curses
        self._erase(full=full)
        height, width = self.stdscr.getmaxyx()
        self._header(title)
        self._add(2, 1, "Этап: {}".format(stage or "подготовка"))
        status_row = 3
        status = "[{}] {}".format(
            SPINNER[spin_frame % len(SPINNER)],
            "выполняется" if running else "завершено")
        self._add(status_row, 1, status, self.attrs["warn"] if running
                  else self.attrs["ok"])
        info = []
        if subvol:
            info.append("subvolume: {}".format(subvol))
        if comp:
            info.append("сжатие: {}".format(comp))
        info.append("время: {:02d}:{:02d}:{:02d}".format(
            elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60))
        self._add(status_row + 1, 1, "; ".join(info)[:width - 2],
                  self.attrs["hint"])
        self._add(status_row + 2, 0, "-" * min(width - 1, 60),
                  self.attrs["dim"])
        # Компактная область событий: последние N строк
        visible = max(3, height - (status_row + 3) - 2)
        # События могут содержать длинные предупреждения (например,
        # upload-pending). Переносим их по фактической ширине терминала,
        # затем показываем последние строки, чтобы текст не обрезался.
        rendered = []
        text_width = max(10, width - 4)
        for level, text in lines:
            wrapped = common.wrap_text(text, text_width)
            rendered.extend((level, part) for part in wrapped)
        tail = rendered[-visible:]
        row = status_row + 3
        for level, text in tail:
            attr = {
                "stage": curses.A_BOLD, "info": None, "warn": self.attrs["warn"],
                "error": self.attrs["err"], "done": self.attrs["ok"],
                "finish": self.attrs["ok"],
            }.get(level)
            self._add(row, 1, text, attr)
            row += 1
        hint = "Esc — прервать операцию" if running else \
            "Enter/Esc — вернуться в меню"
        self._add(height - 1, 0, hint, self.attrs["hint"])
        try:
            self.stdscr.refresh()
        except curses.error:
            pass

    def _message_dialog(self, text, title="Информация"):
        """Показывает переносимое информационное сообщение без Да/Нет."""
        import curses
        height, width = self.stdscr.getmaxyx()
        inner_width = max(12, width - 8)
        wrapped = []
        for paragraph in str(text).splitlines():
            if not paragraph:
                wrapped.append("")
            else:
                wrapped.extend(textwrap.wrap(
                    paragraph, width=inner_width,
                    break_long_words=True, break_on_hyphens=False) or [""])
        max_rows = max(1, height - 7)
        if len(wrapped) > max_rows:
            wrapped = wrapped[:max_rows - 1] + ["… (подробности в журнале)"]
        box_width = min(max((len(line) for line in wrapped), default=12) + 4,
                        max(12, width - 2))
        box_height = min(len(wrapped) + 5, height - 2)
        top = max(0, (height - box_height) // 2)
        left = max(0, (width - box_width) // 2)
        self.stdscr.timeout(-1)
        try:
            self._erase()
            self._add(top, left + 2, title, self.attrs.get("title"))
            for row, line in enumerate(wrapped[:box_height - 3]):
                self._add(top + 1 + row, left + 2, line)
            self._add(top + box_height - 2, left + 2,
                      "Нажмите любую клавишу для возврата",
                      self.attrs.get("hint"))
            try:
                self.stdscr.refresh()
            except curses.error:
                pass
            self.stdscr.getch()
        finally:
            self.stdscr.timeout(150)

    def _confirm_dialog(self, text, default_yes=False):
        """
        Модальный запрос подтверждения одной клавишей.

        Y — да, N — нет (в любом регистре), без необходимости нажимать
        Enter. Enter означает значение по умолчанию (default_yes).
        Esc/Q — отказ.
        """
        import curses
        height, width = self.stdscr.getmaxyx()
        inner_width = max(12, width - 8)
        wrapped = []
        for paragraph in text.splitlines():
            if not paragraph:
                wrapped.append("")
            else:
                wrapped.extend(textwrap.wrap(
                    paragraph, width=inner_width,
                    break_long_words=True, break_on_hyphens=False) or [""])
        box_width = min(max((len(line) for line in wrapped), default=12) + 4,
                        max(12, width - 2))
        max_text_rows = max(1, height - 6)
        if len(wrapped) > max_text_rows:
            wrapped = wrapped[:max_text_rows - 1] + ["… (текст сокращён)"]
        box_height = min(len(wrapped) + 5, height - 2)
        top = max(0, (height - box_height) // 2)
        left = max(0, (width - box_width) // 2)
        self.stdscr.timeout(-1)
        try:
            while True:
                try:
                    self._erase()
                    for row, line in enumerate(wrapped[:box_height - 4]):
                        self._add(top + 1 + row, left + 2, line)
                    # кнопки подтверждения возвращены; v1.4.3:
                    # основной ответ — ЦИФРЫ 1/2, работают при любой
                    # раскладке (латинское «y» на русской раскладке
                    # недоступно).
                    self._add(top + box_height - 3, left + 2,
                              confirm_hint(default_yes), self.attrs["hint"])
                    self.stdscr.refresh()
                except curses.error:
                    pass
                key = self._read_char()
                if key == -1:
                    continue
                answer = dialogs.parse_confirm_key(chr(key), default_yes)
                if answer is None:
                    continue
                return answer
        finally:
            self.stdscr.timeout(150)

    # ---------- пункты меню ----------

    def _select_subvolumes(self, entries, discovery_errors=None):
        """
        Экран выбора subvolume для резервного копирования.

        ТЗ 1.4.7: список строится по фактическому обнаружению (§3.4);
        записи конфигурации сверены с ним и не сверяемые показаны
        с предупреждением (§8); корень ФС (subvolid=5, §3.3) помечен и
        требует явного подтверждения; ошибки обнаружения показываются
        с подробностями (§3.2, клавиша d).

        entries: [{name, source, from_config, selectable, device,
        notation, fs_uuid, fs_root[, origin]}].
        Пробел — снять/поставить отметку, A — все/ничего, R — обновить,
        d — подробности об ошибках обнаружения, Enter — начать с
        выбранными, Esc — отмена (None).
        Возвращает список выбранных записей (dict) или None.
        """
        import curses
        selection = self._make_selection(entries)
        self.stdscr.timeout(-1)
        # При пустом выборе Enter НЕ продолжает — на экране появляется
        # объяснение; при полном отсутствии выбираемых разделов
        # предупреждение видно сразу (заведомо провальная операция не
        # начинается вовсе).
        selectable_total = sum(
            1 for e in entries if e.get("selectable", True))
        notice = None
        if selectable_total == 0:
            notice = ("В системе нет доступных разделов Btrfs — "
                      "копировать нечего (Esc — отмена).")
        try:
            while True:
                self._erase()
                height, width = self.stdscr.getmaxyx()
                self._header("Выбор разделов (subvolume) для резервного "
                             "копирования")
                # сноска о разделах конфигурации — обычным языком,
                # вместо пометки «из конфигурации» у каждой строки.
                self._add(2, 1, "Разделы, указанные в конфигурации, "
                                "отмечены заранее — при необходимости "
                                "измените выбор.")
                self._add(3, 1, "Пробел — отметить, A — все/ничего, "
                                "R — обновить список, Enter — продолжить, "
                                "Esc — отмена",
                          self.attrs["hint"])
                rows = common.source_rows(entries)
                label_width = common.source_label_width(entries)
                first_list_row = 5
                visible = max(1, height - first_list_row - 3)
                cursor_row = 0
                for r, row in enumerate(rows):
                    if row["kind"] == "entry" and \
                            row["index"] == selection.index:
                        cursor_row = r
                        break
                offset = max(0, min(cursor_row - visible + 1,
                                    max(0, len(rows) - visible)))
                for r, row in enumerate(rows[offset:offset + visible]):
                    screen_row = first_list_row + r
                    if row["kind"] == "header":
                        self._add(screen_row, 1, row["text"],
                                  self.attrs["title"])
                        continue
                    entry = entries[row["index"]]
                    selectable = entry.get("selectable", True)
                    if selectable:
                        mark = "[x]" if selection.flags[row["index"]] \
                            else "[ ]"
                    else:
                        mark = "[–]"  # недоступен — причина справа
                    label, suffix = common.source_line(
                        entries, row, label_width)
                    if entry.get("fs_root"):
                        # Верхний уровень Btrfs обозначен явно — его нельзя
                        # перепутать с обычным Linux-корнем (§5.1).
                        suffix = (suffix + " " if suffix else "") + \
                            "[Верхний уровень Btrfs (subvolid=5)]"
                    line = "{} {}  {}".format(mark, label, suffix).rstrip()
                    if row["index"] == selection.index:
                        attr = self.attrs["sel"]
                    else:
                        attr = None if selectable else self.attrs["dim"]
                    self._add(screen_row, 1, line, attr)
                selectable_chosen = sum(
                    1 for i in selection.selected_indices()
                    if entries[i].get("selectable", True))
                if discovery_errors:
                    self._add(height - 4, 1,
                              "ВНИМАНИЕ: обнаружение subvolume не удалось "
                              "для {} ФС — d — подробности, R — повторить".
                              format(len(discovery_errors)),
                              self.attrs["warn"])
                if notice:
                    self._add(height - 3, 1, notice[:width - 2],
                              self.attrs["err"])
                self._add(height - 2, 1,
                          "Выбрано: {} из {}".format(
                              selectable_chosen, selectable_total),
                          self.attrs["warn"] if not selectable_chosen
                          else None)
                self._add(height - 1, 0,
                          "Пробел — отметить, A — все/ничего, R — обновить, "
                          "D — сведения об обнаружении, Enter — OK, Esc — отмена",
                          self.attrs["hint"])
                try:
                    self.stdscr.refresh()
                except curses.error:
                    pass
                key = self.stdscr.getch()
                if key in (ord(" "), 32):
                    if entries[selection.index].get("selectable", True):
                        selection.toggle()
                    notice = None
                elif key in (ord("a"), ord("A"), ord("ф"), ord("Ф")):
                    self._selection_set_all(selection, entries)
                    notice = None
                elif key in (ord("r"), ord("R"), ord("к"), ord("К")):
                    entries, selection = self._refresh_selection(
                        entries, selection)
                    selectable_total = sum(
                        1 for e in entries if e.get("selectable", True))
                    notice = None
                elif key in (ord("d"), ord("D"), ord("в"), ord("В")):
                    if discovery_errors:
                        details = ("Ошибки обнаружения subvolume:\n" +
                                   "\n".join(discovery_errors))
                    else:
                        found = []
                        seen = set()
                        for entry in entries:
                            device = entry.get("device") or "устройство не определено"
                            mount = entry.get("notation") or entry.get("fs_uuid") or ""
                            label = "{} {}".format(device, mount).strip()
                            if label not in seen:
                                seen.add(label)
                                found.append(label)
                        details = (
                            "Ошибок обнаружения subvolume не зарегистрировано.\n"
                            "Найдено доступных записей: {}.\n"
                            "Устройства/файловые системы:\n{}".format(
                                selectable_total,
                                "\n".join(found) if found else "  нет данных"))
                    self._error_details_screen(details)
                elif key in (curses.KEY_UP, ord("k")):
                    selection.move(-1)
                elif key in (curses.KEY_DOWN, ord("j")):
                    selection.move(1)
                elif key in (10, 13, curses.KEY_ENTER):
                    chosen = [entries[i]
                              for i in selection.selected_indices()
                              if entries[i].get("selectable", True)]
                    if not chosen:
                        # без выбираемых разделов — причина про
                        # отсутствие Btrfs; иначе — напоминание о выборе.
                        if selectable_total == 0:
                            notice = ("В системе нет доступных разделов "
                                      "Btrfs — копировать нечего "
                                      "(Esc — отмена).")
                        else:
                            notice = ("Не выбран ни один раздел — "
                                      "отметьте хотя бы один (Пробел) "
                                      "или нажмите Esc для отмены.")
                        continue
                    # ТЗ 1.4.7 §3.3: корень ФС (subvolid=5) — не обычный
                    # источник; требуется явное подтверждение с объяснением
                    # ограничений btrfs send и вложенных subvolume.
                    fs_root_chosen = [e for e in chosen if e.get("fs_root")]
                    if fs_root_chosen:
                        roots = "\n".join(
                            "  " + e["source"]
                            for e in fs_root_chosen[:10])
                        if len(fs_root_chosen) > 10:
                            roots += "\n  ... и еще {}".format(
                                len(fs_root_chosen) - 10)
                        if not self._confirm_dialog(
                                "Выбран ВЕРХНИЙ УРОВЕНЬ файловой системы "
                                "Btrfs (subvolid=5):\n{}\n\n"
                                "Снапшот НЕ включает содержимое вложенных "
                                "subvolume — их необходимо резервировать "
                                "отдельно. Если вы ожидали другой объект — "
                                "выберите конкретный subvolume из списка. "
                                "Продолжить?".format(roots),
                                default_yes=False):
                            notice = ("Выбор верхнего уровня не подтвержден — "
                                      "отметьте конкретные subvolume.")
                            continue
                    return chosen
                elif key in (27, ord("q"), ord("Q")):
                    return None
                elif key == curses.KEY_RESIZE:
                    self._erase()
                else:
                    notice = None
        finally:
            self.stdscr.timeout(150)

    @staticmethod
    def _make_selection(entries):
        """
        MultiSelect по записям выбора; конфигурационные отмечены.
        v1.4.2: отмечаются только ВЫБИРАЕМые конфигурационные записи —
        источник без монтирования Btrfs ([–]) не должен выглядеть
        выбранным по умолчанию.
        """
        selection = MultiSelect([e["name"] for e in entries], checked=False)
        for i, entry in enumerate(entries):
            if entry.get("from_config") and entry.get("selectable", True):
                selection.flags[i] = True  # из конфигурации — отмечены
        return selection

    @staticmethod
    def _selection_set_all(selection, entries):
        """A — все/ничего: меняются только выбираемые строки."""
        selectable_flags = [
            flag for flag, entry in zip(selection.flags, entries)
            if entry.get("selectable", True)]
        target = not all(selectable_flags)
        for i, entry in enumerate(entries):
            if entry.get("selectable", True):
                selection.flags[i] = target

    def _refresh_selection(self, entries, selection):
        """
        R — повторное обнаружение разделов с сохранением отметок
        (совпадение по источнику; новые конфигурационные отмечаются).
        """
        try:
            fresh, _errors = self._backup_entries()
        except Exception:  # noqa: BLE001
            log.exception("Не удалось обновить список разделов")
            return entries, selection
        if not fresh:
            return entries, selection
        old_marks = {}
        for entry, flag in zip(entries, selection.flags):
            if entry.get("selectable", True):
                old_marks[entry["source"].rstrip("/") or "/"] = flag
        cursor = selection.index
        new_selection = self._make_selection(fresh)
        for i, entry in enumerate(fresh):
            if not entry.get("selectable", True):
                continue
            key = entry["source"].rstrip("/") or "/"
            # по умолчанию отмечаются только выбираемые
            # конфигурационные записи ([–] не выглядит выбранным).
            new_selection.flags[i] = old_marks.get(
                key, bool(entry.get("from_config"))
                and entry.get("selectable", True))
        new_selection.index = min(cursor, max(0, len(fresh) - 1))
        return fresh, new_selection

    def _backup_entries(self):
        """
        Записи экрана выбора subvolume: общий список
        ui.common.backup_source_entries — только Btrfs: конфигурация,
        сверенная с обнаружением, + subvolume системы.
        Возвращает (entries, discovery_errors): причины неудач обнаружения
        показываются пользователю.
        """
        try:
            return common.backup_source_entries(self.config)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось собрать список разделов для выбора")
            items, _ = self.config.normalize_subvolumes(
                self.config.SUBVOLUMES)
            return ([{"name": item["name"], "source": item["source"],
                      "from_config": True, "selectable": True,
                      "device": None, "notation": None,
                      "fs_uuid": None, "fs_root": False}
                     for item in items], [])

    # ---------- подготовка источников ----------

    def _prepare_sources(self, chosen):
        """
        Проверка выбранных источников и подбор каталогов.

        Единый механизм core.pathcheck используется здесь ДО запуска
        операции: проблемные источники показываются с причиной;
        пользователь может исключить их, применить предложенный каталог
        (автоподбор с показом РЕЗУЛЬТАТА, ТЗ §6.2) или указать свой
        (ручной ввод с проверками, ТЗ §6.1). Возвращает список словарей
        {name, source, snapshot_dir, local_backup_dir} или None (отмена/
        все источники исключены).
        """
        items = [{"name": entry["name"], "source": entry["source"],
                  "snapshot_dir": entry.get("snapshot_dir"),
                  "local_backup_dir": entry.get("local_backup_dir")}
                 for entry in chosen]
        for _round in range(20):  # защита от бесконечного цикла правок
            checks = pathcheck_module.check_sources(items, self.config)
            failed = [c for c in checks if not c["ok"]]

            # Если глобальный SNAPSHOT_DIR оказался на другой Btrfs,
            # автоматически применяем безопасное индивидуальное
            # предложение для обнаруженного источника. Пользователь
            # увидит выбранный путь на экране итоговых параметров и
            # сможет изменить его клавишей E. Для корня ФС предложение
            # требует создания отдельного subvolume — оставляем его
            # на явное подтверждение через экран проблем.
            auto_changed = False
            for item, check in zip(items, checks):
                proposed = check.get("proposed_snapshot_dir")
                if (not check["ok"] and proposed and
                        check.get("proposal_ok") and
                        not item.get("snapshot_dir") and
                        not check.get("proposal_needs_subvolume")):
                    item["snapshot_dir"] = proposed
                    auto_changed = True
            if auto_changed:
                # Автоматический выбор каталога — внутренний шаг подготовки.
                # Итоговые параметры будут показаны на экране подтверждения;
                # не оставляем техническое сообщение на главном экране.
                continue

            if not failed:
                confirmed = self._confirm_sources_screen(checks)
                if confirmed is None:
                    return None  # Esc — отмена
                if not confirmed:
                    # E на экране итоговых параметров должно открывать
                    # редактор каталогов, а не повторно показывать тот же экран.
                    self._edit_dirs_screen(items, checks)
                    continue
                self.message = None
                return items
            ok_count = sum(1 for c in checks if c["ok"])
            action = self._problems_screen(checks, ok_count)
            if action == "exclude":
                items = [item for item, c in zip(items, checks) if c["ok"]]
                if not items:
                    # ТЗ 4: корректное завершение без повторной проверки
                    self.message = ("Все источники исключены. Нет "
                                    "источников для резервного копирования")
                    return None
                continue
            if action == "auto":
                changed, lines = self._apply_dir_proposals(items, checks)
                # ТЗ §6.2: результат автоподбора ВСЕГДА показывается —
                # нажатие не остается без реакции; E на экране результата
                # открывает правку каталогов, Esc — отменяет операцию.
                result = self._auto_result_screen(lines)
                if result is None:
                    return None  # Esc — отмена
                if changed:
                    self._ensure_needed_subvolumes(items)
                if result == "edit":
                    self._edit_dirs_screen(items, checks)
            elif action == "edit":
                self._edit_dirs_screen(items, checks)
            else:
                return None  # Esc — отмена
        return None

    def _edit_dirs_screen(self, items, checks):
        """
        Выбор источника и правка его каталогов:
        общий путь для клавиши E экрана проблем и экрана результата
        автоподбора.
        """
        targets = [(i, items[i]) for i, c in enumerate(checks)
                   if not c["ok"]] or \
            [(i, items[i]) for i in range(len(items))]
        index = self._select_screen(
            "Источник для правки каталогов:",
            ["{} ({})".format(entry["name"], entry["source"])
             for _i, entry in targets])
        if index is not None:
            item = targets[index][1]
            self._edit_source_dirs(item)

    def _apply_dir_proposals(self, items, checks):
        """
        Автоподбор каталога снапшотов для каждого проблемного источника:
        сначала предложение из результата проверки (глобальный
        SNAPSHOT_DIR на чужой ФС), затем propose_snapshot_dir.
        Возвращает (изменено_ли, [строки результата]) — строки показываются
        пользователю на отдельном экране.

        Предложение из проверки применяется ТОЛЬКО при ok=True; признак
        needs_subvolume передается вместе с путем и не теряется —
        предложение для верхнего уровня Btrfs требует отдельного subvolume.
        """
        changed = False
        lines = ["Результат автоматического подбора каталога снапшотов:",
                 ""]
        for item, check in zip(items, checks):
            label = "{} ({})".format(item["name"], item["source"])
            if check["ok"]:
                lines.append("{}: каталог уже корректен".format(label))
                continue
            path = check.get("proposed_snapshot_dir")
            reason = check.get("proposal_reason") or ""
            needs_subvolume = bool(check.get("proposal_needs_subvolume"))
            if path is None:
                proposal = pathcheck_module.propose_snapshot_dir(
                    item["source"])
                path = proposal["path"] if proposal["ok"] else None
                reason = proposal["reason"]
                needs_subvolume = bool(proposal.get("needs_subvolume"))
            if path:
                item["snapshot_dir"] = path
                if needs_subvolume:
                    item["_needs_subvolume"] = True
                else:
                    item.pop("_needs_subvolume", None)
                lines.append("{}: -> {}{}".format(
                    label, path,
                    " ({})".format(reason) if reason else ""))
                changed = True
            else:
                lines.append("{}: ПОДОБРАТЬ НЕ УДАЛОСЬ{}".format(
                    label, " — {}".format(reason) if reason else ""))
        lines.append("")
        lines.append("Если безопасный каталог подобрать не удалось — "
                     "укажите путь вручную (E).")
        return changed, lines

    def _auto_result_screen(self, lines):
        """
        Экран результата автоподбора: выбранные пути
        и причины неудач видны, нажатие не остается без реакции.
        Возвращает "continue" (Enter/1), "edit" (E) или None (Esc).
        """
        import curses
        offset = 0
        self.stdscr.timeout(-1)
        try:
            while True:
                self._erase()
                height, width = self.stdscr.getmaxyx()
                self._header("Автоматический подбор каталогов: результат")
                visible = height - 6
                offset = max(0, min(offset, max(0, len(lines) - visible)))
                row = 2
                for line in lines[offset:offset + visible]:
                    attr = self.attrs["err"] if "НЕ УДАЛОСЬ" in line else \
                        (self.attrs["ok"] if " -> " in line else None)
                    self._add(row, 1, line, attr)
                    row += 1
                self._add(height - 2, 1,
                          "Enter — продолжить проверку, E — указать путь "
                          "вручную, Esc — отмена", self.attrs["hint"])
                try:
                    self.stdscr.refresh()
                except curses.error:
                    pass
                key = self.stdscr.getch()
                if key in (10, 13, curses.KEY_ENTER, ord("1")):
                    return "continue"
                if key in (ord("e"), ord("E"), ord("у"), ord("У")):
                    return "edit"
                if key in (27, ord("q"), ord("Q")):
                    return None
                if key == curses.KEY_PPAGE:
                    offset = max(0, offset - visible)
                elif key == curses.KEY_NPAGE:
                    offset = min(max(0, len(lines) - visible),
                                 offset + visible)
                elif key == curses.KEY_RESIZE:
                    self._erase()
        finally:
            self.stdscr.timeout(150)

    def _edit_source_dirs(self, item):
        """
        Правка каталогов ОДНОГО источника:
        каталог снапшотов и каталог локальных архивов выбираются
        независимо, с проверками и показом результата.
        """
        while True:
            options = [
                "Каталог снапшотов: {}".format(
                    item.get("snapshot_dir") or "(не задан)"),
                "Каталог локальных архивов: {}".format(
                    item.get("local_backup_dir") or "(не задан)"),
            ]
            index = self._select_screen(
                "Каталоги источника {} ({}):".format(
                    item["name"], item["source"]),
                options)
            if index is None:
                return
            if index == 0:
                new_dir = self._choose_snapshot_dir(item)
                if new_dir:
                    item["snapshot_dir"] = new_dir
                    item.pop("_needs_subvolume", None)
                    proposal = pathcheck_module.propose_snapshot_dir(
                        item["source"])
                    if proposal["ok"] and \
                            proposal["path"] == new_dir and \
                            proposal["needs_subvolume"]:
                        item["_needs_subvolume"] = True
                        self._ensure_needed_subvolumes([item])
            elif index == 1:
                new_dir = self._choose_backup_dir(item)
                if new_dir:
                    item["local_backup_dir"] = new_dir

    def _choose_backup_dir(self, item):
        """
        Выбор каталога локальных архивов: ручной
        ввод с проверками и возможностью создать каталог.
        """
        from core import environment as env_module
        initial = item.get("local_backup_dir") or ""
        while True:
            text = self._input_text(
                "Каталог локальных архивов для {} (абсолютный путь)".format(
                    item["name"]), initial)
            if text is None:
                return None  # Esc/назад
            if not text.startswith("/"):
                if not self._confirm_dialog(
                        "Путь '{}' не абсолютный. Ввести заново?".format(
                            text), default_yes=True):
                    return None
                initial = text
                continue
            path = Path(text)
            if not path.exists():
                if self._confirm_dialog(
                        "Каталог {} не существует. Создать?".format(text),
                        default_yes=False):
                    try:
                        path.mkdir(parents=True, exist_ok=True)
                    except OSError as exc:
                        self._show_error(
                            "Каталог не создан: {}".format(exc))
                        initial = text
                        continue
                else:
                    initial = text
                    continue
            if not env_module.writable_dir(text):
                self._show_error(
                    "Каталог {} недоступен для записи — укажите другой"
                    .format(text))
                initial = text
                continue
            self.message = "Каталог архивов: {}".format(text)
            return text

    def _ensure_needed_subvolumes(self, items):
        """
        Создание каталогов снапшотов как ОТДЕЛЬНЫХ subvolume:
        только по явному подтверждению; отказ — путь сбрасывается и
        источник вернется на экран правки.
        """
        for item in items:
            if not item.pop("_needs_subvolume", False):
                continue
            target = item.get("snapshot_dir")
            if not target:
                continue
            if not self._confirm_dialog(
                    "Создать каталог снапшотов {} как ОТДЕЛЬНЫЙ subvolume? "
                    "(источник — верхний уровень файловой системы: обычный "
                    "каталог внутри источника запрещен)".format(target),
                    default_yes=False):
                item["snapshot_dir"] = None
                continue
            try:
                pathcheck_module.ensure_snapshot_dir_as_subvolume(target)
                self.message = "Создан subvolume: {}".format(target)
            except BtrfsBackupError as exc:
                self._show_error("Subvolume не создан: {}".format(exc))
                item["snapshot_dir"] = None

    def _problems_screen(self, checks, ok_count):
        """
        Экран проблемных источников.

        Возвращает действие: "exclude" (исключить проблемные),
        "auto" (подобрать каталоги автоматически), "edit" (указать путь
        вручную) или None (Esc — отмена операции).
        """
        import curses
        failed = [c for c in checks if not c["ok"]]
        lines = []
        for check in failed:
            for error in check["errors"]:
                lines.extend(common.wrap_text(
                    "{}: {}".format(check["source"], error), 70))
            lines.append("")
        self.stdscr.timeout(-1)
        try:
            while True:
                self._erase()
                height, width = self.stdscr.getmaxyx()
                self._header("Проверка источников: проблемы")
                row = 2
                visible = height - 9
                for line in lines[row - 2:row - 2 + visible]:
                    self._add(row, 1, line, self.attrs["warn"])
                    row += 1
                if ok_count:
                    self._add(height - 5, 1,
                              "Прошло проверку: {}. Проблемных: {}.".format(
                                  ok_count, len(failed)),
                              self.attrs["hint"])
                    self._add(height - 4, 1,
                              "1 — продолжить без проблемных источников, "
                              "2 — отменить операцию")
                    self._add(height - 3, 1,
                              "A — подобрать каталог снапшотов "
                              "автоматически, E — указать путь вручную",
                              self.attrs["hint"])
                    self._add(height - 2, 1,
                              "Исключенные источники не участвуют в "
                              "операции и повторно не проверяются",
                              self.attrs["dim"])
                else:
                    self._add(height - 5, 1,
                              "Ни один источник не прошел проверку.",
                              self.attrs["err"])
                    self._add(height - 4, 1,
                              "1 — исключить все (операция завершится), "
                              "2 — отменить")
                    self._add(height - 3, 1,
                              "A — подобрать каталоги автоматически, "
                              "E — указать путь вручную",
                              self.attrs["hint"])
                    self._add(height - 2, 1,
                              "Без источников резервное копирование "
                              "не выполняется", self.attrs["dim"])
                try:
                    self.stdscr.refresh()
                except curses.error:
                    pass
                key = self.stdscr.getch()
                if key in (ord("1"), ord("y"), ord("Y"), ord("д"), ord("Д")):
                    return "exclude"
                if key in (ord("2"), ord("n"), ord("N"), ord("н"), ord("Н"),
                           27, ord("q"), ord("Q")):
                    return None
                if key in (ord("a"), ord("A"), ord("ф"), ord("Ф")):
                    return "auto"
                if key in (ord("e"), ord("E"), ord("у"), ord("У")):
                    return "edit"
                if key == curses.KEY_RESIZE:
                    self._erase()
        finally:
            self.stdscr.timeout(150)

    def _confirm_sources_screen(self, checks):
        """
        Итоговые параметры операции: источники, каталоги
        снапшотов и архивов, предупреждения. Enter — подтвердить,
        E — вернуться к правке, Esc — отмена. Возвращает True/False/None.
        """
        import curses
        lines = []
        for check in checks:
            lines.append("{}: {}".format(check["name"],
                                         check["source"]))
            lines.append("    снимки: {}".format(
                check["snapshot_dir"]))
            lines.append("    архивы: {}".format(
                check["local_backup_dir"]))
            for warning in check["warnings"]:
                lines.extend(common.wrap_text(
                    "    [!] {}".format(warning), 66))
        offset = 0
        self.stdscr.timeout(-1)
        try:
            while True:
                self._erase()
                height, width = self.stdscr.getmaxyx()
                self._header("Проверка источников: итоговые параметры")
                self._add(2, 1, "Источники: {} шт.".format(len(checks)),
                          self.attrs["hint"])
                visible = height - 7
                offset = max(0, min(offset, max(0, len(lines) - visible)))
                row = 4
                for line in lines[offset:offset + visible]:
                    attr = self.attrs["warn"] if "[!]" in line else None
                    self._add(row, 1, line, attr)
                    row += 1
                self._add(height - 2, 1,
                          "Enter — продолжить (пароль GPG), "
                          "E — изменить каталоги, Esc — отмена",
                          self.attrs["hint"])
                try:
                    self.stdscr.refresh()
                except curses.error:
                    pass
                key = self.stdscr.getch()
                if key in (10, 13, curses.KEY_ENTER):
                    return True
                if key in (27, ord("q"), ord("Q")):
                    return None
                if key in (ord("e"), ord("E"), ord("у"), ord("У")):
                    return False
                if key == curses.KEY_PPAGE:
                    offset = max(0, offset - visible)
                elif key == curses.KEY_NPAGE:
                    offset = min(max(0, len(lines) - visible),
                                 offset + visible)
                elif key == curses.KEY_RESIZE:
                    self._erase()
        finally:
            self.stdscr.timeout(150)

    def _choose_snapshot_dir(self, item):
        """
        Выбор каталога снапшотов для источника:
        безопасное предложение, текущее значение или ручной ввод с
        проверками. Нажатие Enter ВСЕГДА выполняет выбранное действие
        (раньше «Ввести путь вручную» возвращало None — ввод не
        открывался). Возвращает путь или None.
        """
        proposal = pathcheck_module.propose_snapshot_dir(item["source"])
        # (подпись, действие, значение); действие: "use" — принять путь,
        # "manual" — ручной ввод. Информационные строки НЕ входят в
        # выбираемые (раньше они смешивались с опциями и ломали индексы).
        options = []
        if proposal["ok"] and proposal["path"]:
            mark = " (создать отдельный subvolume)" \
                if proposal["needs_subvolume"] else ""
            options.append(("Предложенный: {}{}".format(
                proposal["path"], mark), "use", proposal["path"]))
        if item.get("snapshot_dir"):
            options.append(("Текущий: {}".format(item["snapshot_dir"]),
                            "use", item["snapshot_dir"]))
        options.append(("Ввести путь вручную", "manual", None))
        info_lines = []
        if proposal["reason"]:
            info_lines.extend(common.wrap_text(
                "Причина предложения: {}".format(proposal["reason"]), 66))
        index = self._select_screen(
            "Каталог снапшотов для {} ({}):".format(
                item["name"], item["source"]),
            [label for label, _kind, _value in options],
            info_lines=info_lines)
        if index is None or index >= len(options):
            return None
        kind, value = options[index][1], options[index][2]
        if kind == "manual":
            # ТЗ §6.1: ручной ввод с проверками (существование, запись,
            # расположение относительно источника, UUID файловой системы)
            return self._manual_snapshot_dir(item)
        return value

    def _manual_snapshot_dir(self, item):
        """
        Ручной ввод каталога снапшотов с проверками:
        абсолютный путь, существование (с предложением создать), запись,
        расположение относительно источника, UUID файловой системы;
        результат проверки показывается. Esc — отмена, повтор — новый
        ввод. Возвращает путь или None.
        """
        source = item["source"]
        initial = item.get("snapshot_dir") or ""
        while True:
            text = self._input_text(
                "Каталог снапшотов для {} (абсолютный путь)".format(
                    item["name"]), initial)
            if text is None:
                return None  # отмена/назад
            if not text.startswith("/"):
                self._show_error(
                    "Путь '{}' не абсолютный — введите абсолютный путь"
                    .format(text))
                initial = text
                continue
            path = Path(text)
            if not path.exists():
                if self._confirm_dialog(
                        "Каталог {} не существует. Создать?".format(text),
                        default_yes=False):
                    try:
                        path.mkdir(parents=True, exist_ok=True)
                    except OSError as exc:
                        self._show_error(
                            "Каталог не создан: {}".format(exc))
                        initial = text
                        continue
                else:
                    initial = text
                    continue
            # Авторитетная проверка единым механизмом: та же
            # логика, что и у backup — ошибки показываются немедленно
            check = pathcheck_module.check_source(
                {"name": item["name"], "source": source,
                 "snapshot_dir": text,
                 "local_backup_dir": item.get("local_backup_dir")},
                self.config)
            if not check["ok"]:
                self._show_error(
                    "Каталог не подходит:\n" + "\n".join(check["errors"]))
                initial = text
                continue
            self.message = "Каталог снапшотов: {}".format(text)
            return text

    def _input_text(self, title, default=""):
        """Редактируемый ввод строки; Esc всегда возвращает на предыдущий экран."""
        import curses
        buffer = list(str(default))
        self.stdscr.timeout(-1)
        try:
            try:
                curses.noecho()
                curses.curs_set(1)
            except curses.error:
                # В тестовой/частичной curses-среде терминал может быть
                # не инициализирован; ввод всё равно остаётся проверяемым.
                pass
            while True:
                self._erase()
                height, width = self.stdscr.getmaxyx()
                self._header(title)
                self._add(2, 1, "Введите значение; Enter — принять, Esc — назад")
                prompt = "> " + "".join(buffer)
                max_width = max(1, width - 4)
                shown = prompt[-max_width:]
                self._add(4, 2, shown)
                try:
                    self.stdscr.refresh()
                except curses.error:
                    pass
                try:
                    key = self.stdscr.get_wch()
                except AttributeError:
                    key = self.stdscr.getch()
                except curses.error:
                    continue
                if key in (27, "\x1b"):
                    return None
                if key in (10, 13, curses.KEY_ENTER, "\n", "\r"):
                    value = "".join(buffer).strip()
                    return value or None
                if key in (curses.KEY_BACKSPACE, 8, 127, "\b", "\x7f"):
                    if buffer:
                        buffer.pop()
                    continue
                if key == curses.KEY_RESIZE:
                    continue
                if isinstance(key, str):
                    if key.isprintable() and key not in ("\x00",):
                        buffer.append(key)
                elif isinstance(key, int) and 32 <= key <= 0x10ffff:
                    try:
                        char = chr(key)
                    except ValueError:
                        continue
                    if char.isprintable():
                        buffer.append(char)
        finally:
            self.stdscr.timeout(150)
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    def _error_details_screen(self, text):
        """Экран подробностей ошибки: полный текст с прокруткой."""
        import curses
        try:
            width = self.stdscr.getmaxyx()[1]
        except Exception:  # noqa: BLE001
            width = 80
        lines = []
        for line in common.wrap_text(text, max(20, width - 3)):
            lines.append(line)
        self._list_screen("Подробности ошибки", lines or ["(нет текста)"])

    def _handle_menu(self, index):
        # Свежие сообщение и статусы: обработчик может установить новые
        self.message = None
        self.status_cache = None
        if index == 0:
            self._backup_flow()
        elif index == 1:
            self._restore_menu()
        elif index == 2:
            self._local_copies_screen()

    def _backup_flow(self):
        """
        Полная последовательность интерактивного резервного копирования: обнаружение -> выбор устройства и subvolume ->
        каталог snapshots (автоопределение или выбор) -> каталог локальных
        архивов -> сжатие -> шифрование -> проверка параметров ->
        подтверждение -> резервное копирование -> результат.

        Выбранные параметры используются для ТЕКУЩЕЙ операции; сохранение
        в конфигурацию — только по отдельному явному запросу (§7.3).
        """
        entries, discovery_errors = self._backup_entries()
        chosen = self._select_subvolumes(entries, discovery_errors)
        if chosen is None:
            return  # Esc — отмена выбора
        if not chosen:
            self.message = "Не выбран ни один subvolume"
            return
        # Единая проверка расположения + подбор каталогов;
        # проблемные источники можно исключить
        items = self._prepare_sources(chosen)
        if items is None:
            return
        # ТЗ §7.3 п.6: алгоритм сжатия выбирается для текущей операции
        compression = self._choose_compression()
        if compression is None:
            return
        auto_upload = self._choose_auto_upload()
        if auto_upload is None:
            return
        # ТЗ §7.3 п.7: шифрование — пароль GPG собирается ДО запуска
        # рабочего потока: скрытый ввод не конкурирует с curses
        password = self._collect_backup_password()
        if password is None:
            return
        success = self._run_operation(
            "Резервное копирование",
            lambda **kw: backup_ops.run_backup(
                self.config, non_interactive=False, password=password,
                items_override=items, compression_override=compression,
                auto_upload_override=auto_upload, **kw))

    def _choose_compression(self):
        """
        Выбор алгоритма сжатия для текущей операции.
        Доступность утилит проверяется; по умолчанию предлагается
        алгоритм из конфигурации. Возвращает "zstd"/"gzip" или None.
        """
        import shutil
        available = [algo for algo in ("zstd", "gzip") if shutil.which(algo)]
        if not available:
            self._show_error(
                "Не найдены утилиты сжатия ни zstd, ни gzip — операция "
                "невозможна. Установите zstd или gzip.")
            return None
        default_algo = self.config.COMPRESSION
        if default_algo not in available:
            default_algo = available[0]
        options = ["{}{}".format(
            algo, " — по умолчанию" if algo == default_algo else "")
            for algo in available]
        index = self._select_screen(
            "Алгоритм сжатия для этой операции:",
            options,
            info_lines=["zstd обычно быстрее; gzip доступен почти везде."])
        if index is None or index >= len(available):
            return None
        return available[index]

    def _choose_auto_upload(self):
        """Выбор сетевой отправки только для текущего запуска TUI."""
        configured = bool(getattr(self.config, "AUTO_UPLOAD", True))
        choices = (
            ["Автоматически отправлять на шару (по умолчанию)",
             "Оставить архивы локально, без автоматической отправки"]
            if configured else
            ["Оставить архивы локально (по умолчанию)",
             "Автоматически отправлять на шару"]
        )
        index = self._select_screen(
            "Автоматическая отправка архивов для этой операции:",
            choices,
            info_lines=[
                "Выбор действует только на текущий запуск.",
                "backup_config.py и режим CLI/cron не изменяются.",
            ])
        if index is None:
            return None
        return configured if index == 0 else not configured

    def _restore_menu(self):
        sources = ["Локальный зашифрованный архив",
                   "Сетевой зашифрованный архив",
                   "Локальный read-only snapshot"]
        chosen = self._select_screen("Источник восстановления:", sources)
        if chosen is None:
            return
        if chosen == 0:
            # Восстановление доступно из всех известных локальных каталогов,
            # а не только из глобального SNAPSHOT_DIR конфигурации.
            archives = []
            seen = set()
            for directory in self._archive_dirs():
                for path in common.collect_local_archives(directory):
                    try:
                        key = str(path.resolve())
                    except OSError:
                        key = str(path)
                    if key not in seen:
                        seen.add(key)
                        archives.append(path)
            archives.sort(key=lambda p: (str(p.parent), p.name))
            self._restore_archive_flow(archives, local=True)
        elif chosen == 1:
            net_dir = Path(self.config.NETWORK_MOUNT) / \
                self.config.NETWORK_DIRECTORY
            self._restore_archive_flow(
                common.collect_network_archives(net_dir), local=False)
        else:
            self._restore_snapshot_flow()

    def _restore_archive_flow(self, archives, local=False):
        if not archives:
            self.message = "Архивы не найдены"
            return
        labels = []
        for path in archives:
            prefix = ""
            if local:
                prefix = "{} — {} — ".format(
                    common._device_label(path.parent), path.parent)
            labels.append(prefix + common.archive_line(path))
        index = self._select_screen("Выберите архив:", labels)
        if index is None:
            return
        archive_path = archives[index]
        target = self._prompt_string(
            "Целевой каталог (Btrfs)", "/tmp/bbm-restore")
        if not target:
            return
        password = self._password_prompt()
        if password is None:
            return
        self._run_operation(
            "Восстановление: {}".format(archive_path.name),
            lambda reporter, confirm_fn, abort_check, _a=archive_path, _t=target,
            _p=password: self._restore_op(_a, _t, _p, reporter))

    def _restore_op(self, archive_path, target, password, reporter):
        dest = restore_module.receive_archive(
            archive_path, target,
            gpg_binary=self.config.GPG_BINARY, password=password,
        )
        reporter.info("Восстановление завершено: {}".format(dest))

    def _restore_snapshot_flow(self):
        # Список собирается со всех обнаруженных и конфигурированных Btrfs,
        # включая индивидуальные каталоги снапшотов выбранных вручную источников.
        flat = []
        seen = set()
        for directory in self._snapshot_dirs():
            groups = common.collect_snapshots(directory, None)
            for items in groups.values():
                for snap in items:
                    try:
                        key = str(snap.path.resolve())
                    except OSError:
                        key = str(snap.path)
                    if key not in seen:
                        seen.add(key)
                        flat.append(snap)
        flat.sort(key=lambda snap: (str(snap.path.parent), snap.name))
        if not flat:
            self.message = "Снапшоты не найдены"
            return
        labels = ["{} — {} — {}".format(
            common._device_label(snap.path.parent), snap.path.parent, snap.name)
            for snap in flat]
        index = self._select_screen("Выберите snapshot:", labels)
        if index is None:
            return
        snap = flat[index]
        target = self._prompt_string(
            "Целевой каталог (Btrfs)", "/tmp/bbm-restore")
        if not target:
            return
        self._run_operation(
            "Восстановление из снапшота: {}".format(snap.name),
            lambda reporter, confirm_fn, abort_check, _s=snap, _t=target:
            restore_module.restore_snapshot_writable(_s.path, _t))

    # ---------- просмотр локальных копий (§4.3) ----------

    def _local_copies_screen(self):
        """Просмотр и управление локальными снапшотами/архивами."""
        while True:
            options = ["Снапшоты Btrfs", "Локальные архивы"]
            index = self._select_screen(
                "Локальные снапшоты и архивы:", options)
            if index is None:
                return
            self._local_copy_category_screen(index)

    def _local_copy_category_screen(self, category):
        """Сразу показывает сгруппированный список с флажками выбора."""
        if category == 0:
            self._delete_snapshots_screen()
        else:
            self._delete_archives_screen()

    def _multi_select_delete(self, title, entries, kind):
        """Множественный выбор с флажками и подтверждением без потери отметок.

        Пробел — переключить, A — все/ничего, Enter — подтверждение,
        Esc — вернуться. Отмена в диалоге подтверждения возвращает к тому
        же списку с сохранёнными отметками.
        """
        import curses
        if not entries:
            self.message = "Объекты для удаления не найдены"
            return None
        flags = [False] * len(entries)
        cursor = 0
        offset = 0
        notice = None
        self.stdscr.timeout(-1)
        try:
            while True:
                self._erase()
                height, width = self.stdscr.getmaxyx()
                self._header(title)
                rows = []
                previous_group = None
                for idx, item in enumerate(entries):
                    group = item.get("group", "")
                    if group != previous_group:
                        rows.append(("header", group, None))
                        previous_group = group
                    rows.append(("item", item.get("label", item["path"].name), idx))
                visible = max(1, height - 6)
                cursor_row = next((r for r, row in enumerate(rows)
                                   if row[0] == "item" and row[2] == cursor), 0)
                offset = max(0, min(offset, max(0, len(rows) - visible)))
                if cursor_row < offset:
                    offset = cursor_row
                elif cursor_row >= offset + visible:
                    offset = cursor_row - visible + 1
                for screen_row, row in enumerate(rows[offset:offset + visible]):
                    y = 2 + screen_row
                    if row[0] == "header":
                        self._add(y, 1, row[1], self.attrs["title"])
                        continue
                    idx = row[2]
                    mark = "[x]" if flags[idx] else "[ ]"
                    attr = self.attrs["sel"] if idx == cursor else None
                    self._add(y, 1, mark, attr)
                    self._add(y, 5, row[1], attr)
                selected_count = sum(flags)
                if notice:
                    self._add(height - 4, 1, notice[:max(1, width - 2)],
                              self.attrs["warn"])
                self._add(height - 3, 1,
                          "Выбрано: {} из {}".format(selected_count, len(entries)),
                          self.attrs["warn"] if selected_count else self.attrs["hint"])
                help_text = ("Пробел — отметить; A — все/ничего; "
                             "D/Enter — удалить; U — отправить выбранные"
                             if kind == "archives" else
                             "Пробел — отметить; A — все/ничего; "
                             "D/Enter — удалить выбранное")
                self._add(height - 2, 1, help_text, self.attrs["hint"])
                self._add(height - 1, 0,
                          "Стрелки — перемещение; Esc — назад",
                          self.attrs["hint"])
                try:
                    self.stdscr.refresh()
                except curses.error:
                    pass
                key = self.stdscr.getch()
                if key in (curses.KEY_UP, ord("k")):
                    cursor = max(0, cursor - 1)
                    notice = None
                elif key in (curses.KEY_DOWN, ord("j")):
                    cursor = min(len(entries) - 1, cursor + 1)
                    notice = None
                elif key == ord(" "):
                    flags[cursor] = not flags[cursor]
                    notice = None
                elif key in (ord("a"), ord("A"), ord("ф"), ord("Ф")):
                    target = not all(flags)
                    flags = [target] * len(entries)
                    notice = None
                elif kind == "archives" and key in (
                        ord("u"), ord("U"), ord("г"), ord("Г")):
                    selected = [item for item, checked in zip(entries, flags)
                                if checked]
                    if not selected:
                        notice = "Сначала отметьте архивы для отправки"
                        continue
                    try:
                        ready_paths, already_paths, blocked = (
                            backup_ops.preflight_selected_uploads(
                                self.config,
                                [item["path"] for item in selected]))
                    except Exception as exc:
                        notice = "Предварительная проверка не пройдена: {}".format(
                            str(exc)[:max(20, width - 42)])
                        continue
                    blocked_lines = [
                        "{} — {}".format(name, reason)
                        for name, reason in blocked[:5]
                    ]
                    if len(blocked) > 5:
                        blocked_lines.append("… и ещё {}".format(len(blocked) - 5))
                    already_lines = [
                        path.name + " — уже на NAS (SHA-256 совпадает)"
                        for path in already_paths[:3]
                    ]
                    if len(already_paths) > 3:
                        already_lines.append("… и ещё {}".format(len(already_paths) - 3))
                    prompt_parts = [
                        "Предварительная проверка выбранных архивов",
                        "Доступно для отправки/подтверждения: {}".format(
                            len(ready_paths)),
                    ]
                    if already_lines:
                        prompt_parts.extend(["", "Уже переданы:"] + already_lines)
                    if blocked_lines:
                        prompt_parts.extend(["", "Не будут отправлены:", *blocked_lines])
                    if not ready_paths:
                        # Нечего подтверждать: показываем итог и причины,
                        # но не предлагаем Да/Нет без доступных к передаче файлов.
                        if already_paths and not blocked:
                            prompt_parts.extend([
                                "", "Все выбранные архивы уже есть на NAS "
                                "(SHA-256 совпадает). Повторная передача не требуется."
                            ])
                        elif already_paths:
                            prompt_parts.extend([
                                "", "Новых архивов для передачи нет. Уже имеющиеся "
                                "на NAS повторно не отправляются."
                            ])
                        else:
                            prompt_parts.extend([
                                "", "Ни один из выбранных архивов не может быть отправлен."
                            ])
                        self._message_dialog("\n".join(prompt_parts),
                                             title="Отправка архивов")
                        notice = ("Архивы уже находятся на NAS; повторная отправка не нужна"
                                  if already_paths and not blocked else
                                  "Нет архивов, доступных для отправки")
                        log.info("Ручная отправка отменена предварительной проверкой: %s",
                                 "; ".join(
                                     ["{}: уже на NAS".format(p.name)
                                      for p in already_paths] +
                                     ["{}: {}".format(n, r) for n, r in blocked]))
                        continue
                    prompt_parts.extend([
                        "", ("Продолжить с доступными архивами?" if blocked_lines
                             else "Конфликтов не обнаружено. Продолжить?")
                    ])
                    if self._confirm_dialog("\n".join(prompt_parts),
                                             default_yes=False):
                        ready_set = {str(path) for path in ready_paths}
                        upload_items = [
                            item for item in selected
                            if str(item["path"].resolve()) in ready_set
                        ]
                        return ("upload", upload_items)
                    notice = "Отправка отменена; выбор сохранён"
                elif key in (ord("d"), ord("D"), ord("в"), ord("В"),
                             10, 13, curses.KEY_ENTER):
                    selected = [item for item, checked in zip(entries, flags)
                                if checked]
                    if not selected:
                        notice = "Сначала отметьте хотя бы один объект"
                        continue
                    names = "\n".join(item["path"].name
                                       for item in selected[:8])
                    if len(selected) > 8:
                        names += "\n… и ещё {}".format(len(selected) - 8)
                    if kind == "archives":
                        warning = ("Локальные архивы будут удалены вместе с их "
                                   "метаданными. Сетевые копии не затрагиваются. "
                                   "Архивы upload-pending могут быть единственными копиями.")
                    else:
                        warning = "Исходные subvolume защищены; удаляются только проверенные read-only снапшоты."
                    prompt = ("Удалить безвозвратно {} объект(а/ов)?\n{}\n\n{}".
                              format(len(selected), names, warning))
                    if self._confirm_dialog(prompt, default_yes=False):
                        return selected
                    # ВАЖНО: остаёмся на этом экране, отметки сохраняются.
                    notice = "Удаление отменено; выбор сохранён"
                elif key in (27, ord("q"), ord("Q")):
                    return None
                elif key == curses.KEY_RESIZE:
                    self._erase()
        finally:
            self.stdscr.timeout(150)

    def _delete_archives_screen(self):
        """Удаление выбранных локальных архивов."""
        from core.network import delete_local_archive
        unique = {}
        for directory in self._archive_dirs():
            for path in common.collect_local_archives(directory):
                try:
                    unique[str(path.resolve())] = path
                except OSError:
                    continue
        archives = sorted(unique.values(), key=lambda p: (str(p.parent), p.name))
        entries = []
        for path in archives:
            group = "Устройство {} — {}".format(
                common._device_label(path.parent), path.parent)
            status = common.archive_status_label(path)
            entries.append({"path": path, "group": group,
                            "label": "{} [{}]".format(path.name, status)})
        result = self._multi_select_delete(
            "Локальные архивы: отправка или удаление", entries, "archives")
        if result is None:
            return
        if isinstance(result, tuple) and len(result) == 2 and result[0] == "upload":
            self._upload_selected_archives(
                [item["path"] for item in result[1]])
            return
        selected = result
        deleted, failed = 0, []
        for item in selected:
            path = item["path"]
            try:
                delete_local_archive(path)
                if not path.exists():
                    deleted += 1
                else:
                    failed.append(path.name)
            except OSError as exc:
                failed.append("{}: {}".format(path.name, exc))
        self.message = "Удалено архивов: {}".format(deleted)
        if failed:
            self.message += "; ошибок: {}".format(len(failed))
            log.warning("Не удалось удалить архивы: %s", "; ".join(failed))

    def _upload_selected_archives(self, archive_paths):
        """Передаёт выбранные зашифрованные архивы без запроса GPG-пароля."""
        paths = [Path(path) for path in archive_paths]
        if not paths:
            self.message = "Архивы для отправки не выбраны"
            return
        self._run_operation(
            "Отправка {} архивов".format(len(paths)),
            lambda reporter, confirm_fn, abort_check, _paths=paths:
                backup_ops.upload_pending(
                    self.config, non_interactive=False, reporter=reporter,
                    abort_check=abort_check, archive_path=_paths,
                    verify_integrity=False,
                ))

    def _delete_snapshots_screen(self):
        """Удаление выбранных проверенных read-only снапшотов."""
        flat = []
        for directory in self._snapshot_dirs():
            try:
                groups = common.collect_snapshots(directory, None)
                for snapshots in groups.values():
                    flat.extend(snapshots)
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось прочитать %s: %s", directory, exc)
        unique = {}
        for snap in flat:
            try:
                unique[str(snap.path.resolve())] = snap
            except OSError:
                continue
        snapshots = sorted(unique.values(), key=lambda item: (str(item.path.parent), item.name))
        entries = []
        for snap in snapshots:
            group = "Устройство {} — {}".format(
                common._device_label(snap.path.parent), snap.path.parent)
            entries.append({"path": snap.path, "snapshot": snap,
                            "group": group, "label": snap.name})
        selected = self._multi_select_delete(
            "Выберите Btrfs-снапшоты для удаления", entries, "snapshots")
        if selected is None:
            return
        protected = []
        try:
            items, _errors = self.config.normalize_subvolumes(
                self.config.SUBVOLUMES)
            protected.extend(item["source"] for item in items)
        except Exception:  # noqa: BLE001
            pass
        try:
            discovered = snapshots_module.discover_sources(
                exclude_dirs=common.snapshot_dirs(self.config))
            protected.extend(item["source"]
                             for item in discovered.get("sources", []))
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось собрать список защищаемых источников: %s", exc)
            self.message = ("Не удалось проверить все исходные subvolume; "
                            "удаление отменено")
            return
        deleted, failed = 0, []
        for item in selected:
            snap = item["snapshot"]
            try:
                snapshots_module.delete_snapshot(
                    snap.path, str(snap.path.parent),
                    protected_paths=protected)
                deleted += 1
            except BtrfsBackupError as exc:
                failed.append("{}: {}".format(snap.name, exc))
        self.message = "Удалено снапшотов: {}".format(deleted)
        if failed:
            self.message += "; ошибок: {}".format(len(failed))
            log.warning("Не удалось удалить снапшоты: %s", "; ".join(failed))

    def _snapshot_dirs(self):
        """Каталоги снапшотов операции (общий построитель ui.common)."""
        return common.snapshot_dirs(self.config)

    def _archive_dirs(self):
        """Каталоги локальных архивов операции (общий построитель)."""
        return common.archive_dirs(self.config)

    def _snapshot_lines(self):
        """Список снапшотов Btrfs (§4.3)."""
        return common.snapshot_view_lines(self.config)

    def _archives_screen(self):
        """
        Просмотр локальных архивов (§4.3). Если каталоги архивов нельзя
        определить из конфигурации — пользователю предлагается указать
        каталог для просмотра.
        """
        dirs = self._archive_dirs()
        if not dirs:
            self.message = ("Каталоги архивов не определяются из конфигурации "
                             "(источники и LOCAL_BACKUP_DIR не заданы)")
            manual = self._input_text(
                "Каталог локальных архивов для просмотра (абсолютный путь)")
            if not manual or not manual.startswith("/"):
                return
            dirs = [manual]
        lines = common.archive_view_lines(self.config, dirs=dirs)
        self._list_screen("Локальные архивы", lines or ["Архивы не найдены"])

    # ---------- экраны списков ----------

    def _list_screen(self, title, lines):
        import curses
        offset = 0
        self.stdscr.timeout(-1)
        while True:
            self._erase()
            height, width = self.stdscr.getmaxyx()
            self._header(title)
            visible = height - 5
            offset = max(0, min(offset, len(lines) - visible))
            for row, line in enumerate(lines[offset:offset + visible]):
                self._add(2 + row, 1, line)
            self._add(height - 2, 1,
                      "Строк: {} (показаны {}-{})".format(
                          len(lines), offset + 1,
                          min(offset + visible, len(lines))),
                      self.attrs["dim"])
            self._add(height - 1, 0,
                      "Стрелки/PgUp/PgDn — прокрутка, Esc/Q — назад",
                      self.attrs["hint"])
            try:
                self.stdscr.refresh()
            except curses.error:
                pass
            key = self.stdscr.getch()
            if key in (27, ord("q"), ord("Q"), 10, 13):
                return
            if key == curses.KEY_UP:
                offset = max(0, offset - 1)
            elif key == curses.KEY_DOWN:
                offset += 1
            elif key == curses.KEY_PPAGE:
                offset -= visible
            elif key == curses.KEY_NPAGE:
                offset += visible
            elif key == curses.KEY_RESIZE:
                self._erase()

    def _select_screen(self, title, entries, info_lines=None):
        """
        Выбор элемента списка; возвращает индекс или None.
        info_lines — НЕвыбираемые строки подсказки, отображаются ниже
        списка контрастным читаемым стилем (не выбираются клавишами).
        """
        import curses
        if not entries:
            self.message = "Список пуст"
            return None
        index = 0
        offset = 0
        info_lines = info_lines or []
        self.stdscr.timeout(-1)
        while True:
            self._erase()
            height, width = self.stdscr.getmaxyx()
            self._header(title)
            visible = height - 4 - len(info_lines)
            offset = max(0, min(offset, max(0, index - visible + 1)))
            for row, entry in enumerate(entries[offset:offset + visible]):
                real = offset + row
                attr = self.attrs["sel"] if real == index else None
                self._add(2 + row, 1,
                          "> " if real == index else "  ", attr)
                self._add(2 + row, 3, entry, attr)
            info_row = 2 + min(len(entries) - offset, visible) + 1
            for info in info_lines:
                # Важные подсказки — контрастный стиль (не dim):
                # тусклость остается только второстепенным элементам.
                self._add(info_row, 1, info, self.attrs["hint"])
                info_row += 1
            self._add(height - 1, 0,
                      "Стрелки — выбор, Enter — OK, Esc — отмена",
                      self.attrs["hint"])
            try:
                self.stdscr.refresh()
            except curses.error:
                pass
            key = self.stdscr.getch()
            if key in (curses.KEY_UP, ord("k")):
                index = max(0, index - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                index = min(len(entries) - 1, index + 1)
            elif key in (10, 13, curses.KEY_ENTER):
                return index
            elif key in (27, ord("q"), ord("Q")):
                return None
            elif key == curses.KEY_RESIZE:
                self._erase()

    # ---------- ввод текста ----------

    def _read_char(self):
        """
        Один символ клавиатуры как код (int) или -1.

        get_wch корректно отдает unicode-символы (в том числе кириллицу
        в паролях); специальные клавиши возвращаются кодами curses.
        """
        import curses
        reader = getattr(self.stdscr, "get_wch", None)
        if reader is not None:
            try:
                result = reader()
            except curses.error:
                return -1
            if isinstance(result, str) and len(result) == 1:
                return ord(result)
            if isinstance(result, int):
                return result
            return -1
        return self.stdscr.getch()

    def _prompt_string(self, prompt, default=""):
        """Простой однострочный ввод в curses."""
        import curses
        self.stdscr.timeout(-1)
        value = default
        try:
            while True:
                self._erase()
                height, width = self.stdscr.getmaxyx()
                self._header("Ввод")
                self._add(2, 1, prompt + ":")
                self._add(3, 1, value + "_", self.attrs["sel"])
                self._add(height - 1, 0,
                          "Enter — OK, Esc — отмена, Backspace — стереть",
                          self.attrs["hint"])
                try:
                    self.stdscr.refresh()
                except curses.error:
                    pass
                key = self._read_char()
                if key in (10, 13, curses.KEY_ENTER):
                    return value.strip() or None
                if key == 27:
                    return None
                if key in (curses.KEY_BACKSPACE, 127, 8):
                    value = value[:-1]
                elif key == curses.KEY_RESIZE:
                    continue
                elif 32 <= key <= 126 or key >= 0x400:
                    try:
                        value += chr(key)
                    except ValueError:
                        pass
        finally:
            self.stdscr.timeout(150)

    def _password_dialog(self, title, prompt, note=None):
        """
        Скрытый ввод пароля в curses: маска '*', пароль не отображается
        и не логируется. Enter — принять (ОДНО нажатие),
        Esc — отмена (None). Пустой ввод возвращается как "".
        """
        import curses
        self.stdscr.timeout(-1)
        value = ""
        try:
            while True:
                self._erase()
                height, width = self.stdscr.getmaxyx()
                self._header(title)
                self._add(2, 1, prompt)
                if note:
                    self._add(3, 1, note[:width - 2], self.attrs["warn"])
                mask_row = 4 if note else 3
                self._add(mask_row, 1, "*" * len(value), self.attrs["sel"])
                self._add(height - 1, 0,
                          "Enter — принять, Backspace — стереть, Esc — отмена",
                          self.attrs["hint"])
                try:
                    self.stdscr.refresh()
                except curses.error:
                    pass
                key = self._read_char()
                if key in (10, 13, curses.KEY_ENTER):
                    return value
                if key == 27:
                    return None
                if key in (curses.KEY_BACKSPACE, 127, 8):
                    value = value[:-1]
                elif key == curses.KEY_RESIZE:
                    continue
                elif 32 <= key <= 126 or key >= 0x400:
                    try:
                        value += chr(key)
                    except ValueError:
                        pass
        finally:
            self.stdscr.timeout(150)

    def _ask_password_twice(self, note=None):
        """
        Установка нового пароля GPG: ввод + повтор. До 3 попыток;
        сообщения об ошибках рисуются ВНУТРИ диалога curses, терминал
        не используется напрямую. Возвращает пароль или None (Esc/исчерпано).
        """
        error = ""
        for _ in range(3):
            first = self._password_dialog(
                "Пароль GPG", "Задайте пароль GPG (символы скрыты):",
                note=error or note)
            if first is None:
                return None
            if not first:
                error = "Пароль не может быть пустым. Попробуйте еще раз."
                continue
            second = self._password_dialog(
                "Пароль GPG", "Повторите пароль:",
                note="Введите тот же пароль еще раз для проверки.")
            if second is None:
                return None
            if first == second:
                return first
            error = "Пароли не совпадают. Попробуйте еще раз."
        return None

    def _collect_backup_password(self):
        """
        Пароль GPG для операции (backup/upload-pending). Собирается ДО
        запуска рабочего потока, в главном потоке curses: рабочий поток
        никогда не читает терминал сам. Возвращает пароль или None (отказ).
        """
        cfg = self.config
        if cfg.is_password_placeholder(cfg.GPG_PASSWORD):
            password = self._ask_password_twice(
                note="Архивы шифруются этим паролем; он потребуется для "
                     "проверки и восстановления. Запомните его.")
            if password is None:
                self.message = "Пароль GPG не задан — операция отменена"
            return password
        use_config = self._confirm_dialog(
            "Использовать сохраненный пароль GPG (backup_config.py)?\n"
            "No — задать другой пароль только для этой операции.",
            default_yes=True)
        if use_config:
            return cfg.GPG_PASSWORD
        password = self._ask_password_twice(
            note="Архивы шифруются этим паролем; он потребуется для "
                 "проверки и восстановления. Запомните его.")
        if password is None:
            self.message = "Пароль GPG не задан — операция отменена"
        return password

    def _password_prompt(self, prompt_text=None, note=None):
        """
        Пароль GPG для операций над СУЩЕСТВУЮЩИМИ архивами (проверка,
        восстановление, отправка). ТЗ 4.2: ввод скрыт, не логируется.
        Enter — принять введенный пароль ОДНИМ нажатием; пустой Enter —
        пароль из config.py (если задан); Esc — отмена (None).
        """
        import curses
        placeholder = self.config.is_password_placeholder(
            self.config.GPG_PASSWORD)
        if prompt_text is None:
            prompt_text = "Пароль для расшифрования архива:"
        self.stdscr.timeout(-1)
        value = ""
        try:
            while True:
                self._erase()
                height, width = self.stdscr.getmaxyx()
                self._header("Пароль GPG")
                self._add(2, 1, prompt_text)
                hint = ("Пустой Enter — пароль из backup_config.py"
                        if not placeholder
                        else "Пароль в backup_config.py не задан — нужен ввод")
                self._add(3, 1, hint[:width - 2], self.attrs["hint"])
                if note:
                    self._add(4, 1, note[:width - 2], self.attrs["warn"])
                mask_row = 5 if note else 4
                self._add(mask_row, 1, "*" * len(value), self.attrs["sel"])
                self._add(height - 1, 0,
                          "Enter — принять, Backspace — стереть, Esc — отмена",
                          self.attrs["hint"])
                try:
                    self.stdscr.refresh()
                except curses.error:
                    pass
                key = self._read_char()
                note = None
                if key in (10, 13, curses.KEY_ENTER):
                    if value:
                        return value
                    if not placeholder:
                        return self.config.GPG_PASSWORD
                    note = "Пароль не введен, а в backup_config.py он не задан."
                    continue
                if key == 27:
                    return None
                if key in (curses.KEY_BACKSPACE, 127, 8):
                    value = value[:-1]
                elif key == curses.KEY_RESIZE:
                    continue
                elif 32 <= key <= 126 or key >= 0x400:
                    try:
                        value += chr(key)
                    except ValueError:
                        pass
        finally:
            self.stdscr.timeout(150)


def run(config):
    """Запуск TUI; возвращает код завершения (0)."""
    import curses
    import os
    from core import encryption as encryption_module
    # TUI владеет терминалом: прямые запросы пароля (getpass/input) в ядре
    # запрещены — иначе они конкурировали бы с getch() за нажатия клавиш.
    # Пароли собираются диалогами TUI и передаются явно.
    encryption_module.set_terminal_prompts(False)
    # Задержка распознавания одиночного Esc: ncurses читает ESCDELAY
    # при инициализации, поэтому значение ставится ДО initscr (wrapper).
    # 50 мс — компромисс: Esc отзывчив, escape-последовательности стрелок
    # и мыши успевают прийти целиком. Уже заданное значение не трогаем.
    os.environ.setdefault("ESCDELAY", "50")
    try:
        return curses.wrapper(lambda stdscr: TuiApp(config).run(stdscr))
    except curses.error as exc:
        log.error("Ошибка curses: %s", exc)
        print("Терминал не поддерживает полноэкранный режим: {}".format(exc))
        return 1
    finally:
        encryption_module.set_terminal_prompts(True)
