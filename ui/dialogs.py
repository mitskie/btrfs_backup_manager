"""Диалоги подтверждения и выбора для консольных интерфейсов.

Подтверждение confirm() работает БЕЗ Enter: достаточно одной клавиши.
Основной ответ — ЦИФРАМИ 1 (да) / 2 (нет): они одинаковы при любой
раскладке (на русской раскладке ЙЦУКЕН физическая клавиша Y посылает
кириллическую «н», поэтому латиница не может быть основным ответом).
Латиница Y/N, кириллица Д/у (да) и Н (нет) тоже принимаются; «у» — то,
что печатает русская раскладка вместо латинского y.
В нетерминальной среде (конвейеры, тесты) используется построчный
fallback с теми же правилами ответа.
"""

import os
import sys

# Клавиши-ответы одиночного подтверждения. Цифры 1/2 — основные
# (раскладконезависимые, v1.4.3); латиница и кириллица — дополнительно.
_YES_KEYS = ("y", "Y", "д", "Д", "у", "У", "1", "+")
_NO_KEYS = ("n", "N", "н", "Н", "0", "2", "q", "Q", "\x1b")
_ENTER_KEYS = ("\n", "\r")


def parse_confirm_key(key, default_yes):
    """
    Преобразует одиночную клавишу в ответ подтверждения.

    Возвращает True (да), False (нет) или None (клавиша не распознана —
    продолжать ожидание). Enter означает значение по умолчанию.
    """
    if key in _YES_KEYS:
        return True
    if key in _NO_KEYS:
        return False
    if key in _ENTER_KEYS:
        return bool(default_yes)
    return None


def parse_confirm_line(answer, default_yes):
    """Преобразует построчный ответ (fallback без tty) в True/False."""
    answer = (answer or "").strip().lower()
    if not answer:
        return bool(default_yes)
    if answer in ("y", "yes", "д", "да", "у", "1", "true", "+"):
        return True
    return False


def _prompt_suffix(default_yes):
    """
    Видимая подсказка. Цифры 1/2 показаны первыми — основной ответ,
    работающий при любой раскладке. Enter — значение по умолчанию;
    латиница Y/N и кириллица Д/у/Н принимаются без отображения
    в подсказке.
    """
    return " [1=Да, 2=Нет] (Enter — Да): " if default_yes else \
        " [1=Да, 2=Нет] (Enter — Нет): "


def _drain_line_remainder(fd):
    """
    Дочитывает остаток строки, если пользователь по привычке набрал
    "yes" или нажал Enter после клавиши: иначе ввод протек бы в следующий
    запрос. Одиночное нажатие добавляет лишь ~60 мс (быстрая проверка);
    при обнаружении продолжения строка поглощается целиком.
    """
    import select
    ready = select.select([fd], [], [], 0.06)[0]
    if not ready:
        return  # ничего не последовало — обычное одиночное нажатие
    while ready:
        try:
            chunk = os.read(fd, 256)
        except OSError:
            return
        if not chunk:
            return
        if b"\n" in chunk or b"\r" in chunk:
            return  # конец строки — остаток полностью поглощен
        ready = select.select([fd], [], [], 0.08)[0]


def _read_single_key():
    """
    Читает ОДНУ клавишу без ожидания Enter (только tty).

    Возвращает строку из одного символа ("" — EOF). Терминал
    восстанавливается даже при KeyboardInterrupt (Ctrl+C).
    """
    import termios
    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    try:
        modified = termios.tcgetattr(fd)
        # Отключаем канонический режим (ожидание Enter) и эхо; сигналы
        # (Ctrl+C) остаются включенными — прерывание трактуется как отказ.
        modified[3] = modified[3] & ~(termios.ICANON | termios.ECHO)
        termios.tcsetattr(fd, termios.TCSANOW, modified)
        data = os.read(fd, 1)
        # кириллица в UTF-8 занимает ДВА байта, os.read(fd, 1)
        # возвращал только первый, decode() давал заменяющий символ —
        # кириллические ответы (Д/Н/у) в одно-клавишном режиме
        # игнорировались. Дочитываем продолжение UTF-8-последовательности.
        if data:
            lead = data[0]
            if 0xC2 <= lead <= 0xDF:
                need = 1
            elif 0xE0 <= lead <= 0xEF:
                need = 2
            elif 0xF0 <= lead <= 0xF4:
                need = 3
            else:
                need = 0
            while need:
                chunk = os.read(fd, need)
                if not chunk:
                    break
                data += chunk
                need -= len(chunk)
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, original)
    if not data:
        return ""
    encoding = getattr(sys.stdin, "encoding", None) or "utf-8"
    return data.decode(encoding, errors="replace")


def confirm(prompt, default_yes=False):
    """
    Подтверждение одной клавишей: Y — да, N — нет (Enter — по умолчанию).

    В терминале Enter нажимать НЕ нужно (это опция значения по умолчанию).
    В нетерминальной среде (конвейеры, тесты) работает построчный вариант.
    EOF/прерывание трактуется как отказ (безопасное умолчание, ТЗ 14).
    """
    suffix = _prompt_suffix(default_yes)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        if interactive:
            sys.stdout.write(str(prompt).rstrip() + suffix)
            sys.stdout.flush()
            while True:
                try:
                    key = _read_single_key()
                except KeyboardInterrupt:
                    sys.stdout.write("N\n")
                    sys.stdout.flush()
                    return False
                if key == "":
                    # EOF: отказ (безопасное умолчание)
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return False
                _drain_line_remainder(sys.stdin.fileno())
                answer = parse_confirm_key(key, default_yes)
                if answer is None:
                    continue
                # Показываем принятый ответ, чтобы выбор был очевиден
                sys.stdout.write("Да\n" if answer else "Нет\n")
                sys.stdout.flush()
                return answer
        sys.stdout.write(str(prompt).rstrip() + suffix)
        sys.stdout.flush()
        answer = input()
        return parse_confirm_line(answer, default_yes)
    except (EOFError, KeyboardInterrupt):
        try:
            sys.stdout.write("N\n")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
        return False


def select_from_list(title, entries):
    """
    Нумерованный выбор из списка. Возвращает выбранный элемент или None.

    entries: список строк (или объектов с методом __str__).
    """
    if not entries:
        print("Список пуст: {}".format(title))
        return None
    print(title)
    for index, entry in enumerate(entries, 1):
        print("  {:3}. {}".format(index, entry))
    try:
        raw = input("Номер (Enter — отмена): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not raw:
        return None
    try:
        index = int(raw)
    except ValueError:
        print("Некорректный номер: {}".format(raw))
        return None
    if not 1 <= index <= len(entries):
        print("Номер вне диапазона: {}".format(index))
        return None
    return entries[index - 1]
