"""
Итоговый отчет запуска: создано снапшотов, архивов, отправлено,
ожидает отправки, удалено retention, ошибок.

Отчет содержит длительности этапов: без разбивки по этапам
невозможно понять, куда уходит время. В режиме TUI отчет НИКОГДА не печатается
в терминал напрямую (print() во время curses портит экран) — строки
отчета передаются интерфейсу через reporter.done().
"""

import time

from .logging_setup import get_logger

log = get_logger("report")

# Порядок и подписи этапов в отчете (ключ timings -> подпись)
STAGE_LABELS = (
    ("environment", "окружение и конфигурация"),
    ("snapshots", "снапшоты"),
    ("archives", "создание архивов"),
    ("verify", "проверка архивов"),
    ("upload", "сетевая отправка"),
    ("retention", "retention"),
)


def _format_duration(seconds):
    """Длительность в человекочитаемом виде ("3 мин 40 с")."""
    seconds = int(max(0, seconds))
    if seconds >= 3600:
        return "{} ч {:02d} мин {:02d} с".format(
            seconds // 3600, (seconds % 3600) // 60, seconds % 60)
    if seconds >= 60:
        return "{} мин {:02d} с".format(seconds // 60, seconds % 60)
    return "{} с".format(seconds)


class RunReport:
    """Счетчики итогового отчета после каждого запуска."""

    def __init__(self):
        self.snapshots_created = 0
        self.archives_created = 0
        self.archives_uploaded = 0
        self.pending_local = 0
        self.snapshots_deleted = 0
        self.failures = 0
        self.local_only = 0          # успешно созданных локальных архивов
        self.compression_used = None  # фактический алгоритм сжатия
        self.timings = {}            # {этап: секунды}
        self.total_seconds = None    # общая длительность операции

    # ---------- одна строка "Итог: ..." (совместимо с прежними версиями)

    def summary(self):
        return (
            "Итог: снапшотов создано: {r.snapshots_created}; "
            "архивов создано: {r.archives_created}; "
            "отправлено на шару: {r.archives_uploaded}; "
            "локальных архивов ожидает отправки: {r.pending_local}; "
            "снапшотов удалено retention: {r.snapshots_deleted}; "
            "операций с ошибками: {r.failures}"
        ).format(r=self)

    # ---------- длительности этапов ----------

    def timing_summary(self):
        """Одна строка с длительностями этапов или None, если пусто."""
        parts = []
        for key, label in STAGE_LABELS:
            value = self.timings.get(key)
            if value is None:
                continue
            parts.append("{} {}".format(label, _format_duration(value)))
        if self.total_seconds is not None:
            parts.append("всего {}".format(_format_duration(self.total_seconds)))
        if not parts:
            return None
        return "Время: " + "; ".join(parts)

    def summary_lines(self):
        """
        Многострочный вариант отчета для TUI: строки короткие (< ~78
        символов), не обрезаются даже на терминале 80x24 (замечание
        пользователя: длинная строка "Итог: ..." переносилась и
        перекрывала подсказки внизу экрана).
        """
        lines = [
            "Итог: снапшотов создано: {}; архивов создано: {}".format(
                self.snapshots_created, self.archives_created),
            "  отправлено на шару: {}; локальных архивов "
            "ожидает отправки: {}".format(
                self.archives_uploaded, self.pending_local),
            "  снапшотов удалено retention: {}; операций с "
            "ошибками: {}".format(self.snapshots_deleted, self.failures),
        ]
        parts = []
        for key, label in STAGE_LABELS:
            value = self.timings.get(key)
            if value is None:
                continue
            parts.append("{} {}".format(label, _format_duration(value)))
        if self.total_seconds is not None:
            parts.append("всего {}".format(
                _format_duration(self.total_seconds)))
        if parts:
            chunks = []
            current = "Время:"
            for part in parts:
                separator = " " if current.endswith(":") else "; "
                if len(current) + len(separator) + len(part) <= 78:
                    current += separator + part
                else:
                    chunks.append(current)
                    current = "  " + part
            chunks.append(current)
            lines.extend(chunks)
        return lines

    # ---------- вывод ----------

    def _log(self, started_mono):
        text = self.summary()
        timing = self.timing_summary()
        log.info("Отчет запуска (%.1f с): %s%s",
                 time.monotonic() - started_mono, text,
                 ("; " + timing) if timing else "")

    def print_and_log(self, started_mono):
        """Печать отчета в консоль (только CLI, не TUI) + журнал."""
        print()
        print(self.summary())
        timing = self.timing_summary()
        if timing:
            print(timing)
        self._log(started_mono)

    def emit(self, started_mono, reporter=None):
        """
        Выводит отчет СООТВЕТСТВЕННО интерфейсу.

        reporter=None или ConsoleReporter (CLI) — печать в консоль.
        Иной reporter (TUI CollectReporter) — строки через reporter.done():
        прямой print() во время активного curses портит экран и
        перекрывает подсказки нижней строки.
        """
        from .backup import ConsoleReporter
        if reporter is None or isinstance(reporter, ConsoleReporter):
            self.print_and_log(started_mono)
            return
        for line in self.summary_lines():
            reporter.done(line)
        self._log(started_mono)

    def log_only(self, started_mono):
        self._log(started_mono)


def count_pending_archives(backup_dir):
    """Количество локальных архивов, ожидающих отправки (*.gpg)."""
    from pathlib import Path
    try:
        return len([p for p in Path(backup_dir).glob("*.gpg") if p.is_file()])
    except OSError:
        return 0


def last_backup_state_path():
    """Файл состояния последнего резервного копирования (для TUI)."""
    from pathlib import Path
    return Path(__file__).resolve().parent.parent / "logs" / "last-backup.json"


def write_last_backup_state(ok, detail=""):
    """Фиксирует результат последнего backup."""
    import json
    from datetime import datetime
    path = last_backup_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        payload = {
            "result": "success" if ok else "failed",
            "when": datetime.now().isoformat(timespec="seconds"),
            "detail": detail,
        }
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        import os
        os.replace(str(tmp), str(path))
    except OSError:
        pass


def read_last_backup_state():
    """Читает результат последнего backup или None."""
    import json
    try:
        return json.loads(
            last_backup_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
