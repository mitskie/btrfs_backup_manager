"""
Шифрование и расшифрование GPG (симметричное, AES256).

Требования безопасности, реализованные здесь:
  - пароль никогда не передается в аргументах командной строки (argv);
  - пароль передается через защищенный файловый дескриптор (--passphrase-fd);
  - взаимодействие с терминалом отключено (--batch --no-tty);
  - временные файлы с открытым паролем не создаются (используется os.pipe);
  - пароль не записывается в лог и не выводится при диагностике.
"""

import getpass
import os
import subprocess

from . import EncryptionError
from .logging_setup import get_logger

log = get_logger("encryption")

CIPHER_ALGO = "AES256"


def is_placeholder(password):
    """True, если пароль фактически не задан (пустой/None/'CHANGE_ME')."""
    return (not isinstance(password, str)) or password == "" or \
        password == "CHANGE_ME"

# ============================================
# Защита терминала в режиме TUI (curses)
# ============================================
# Когда TUI активен, терминал находится в режиме curses: главный поток
# опрашивает клавиатуру через getch(). Любой прямой запрос пароля
# (getpass/input) из рабочего потока конкурирует за одни и те же нажатия:
# пароль "разрезается" между curses и getpass, требуется несколько Enter,
# а печать подсказок портит экран. Поэтому в TUI-режиме прямые запросы
# к терминалу запрещены: пароль должен быть собран диалогом TUI и передан
# явно (параметр password в run_backup/upload_pending/receive_archive).

_terminal_prompts_allowed = True


def set_terminal_prompts(enabled):
    """Включает/выключает прямые запросы к терминалу (TUI выключает их)."""
    global _terminal_prompts_allowed
    _terminal_prompts_allowed = bool(enabled)


def _require_terminal_prompts():
    """Гарантия ТЗ 14: скрытый ввод не может конкурировать с curses."""
    if not _terminal_prompts_allowed:
        raise EncryptionError(
            "Прямой ввод пароля с терминала запрещен в режиме TUI: "
            "пароль должен быть передан операции явно "
            "(запрашивается интерфейсом до запуска операции).")


def gpg_encrypt_cmd(gpg_binary, passphrase_fd):
    """
    Команда симметричного шифрования: stdin -> зашифрованный stdout.

    --compress-level 0: gpg по умолчанию ПЫТАЕТСЯ сжать поток перед
    шифрованием. На вход конвейера поступают данные ПОСЛЕ gzip/zstd —
    несжимаемый поток, поэтому внутреннее сжатие gpg — это только
    потерянное процессорное время (замер на 200 МиБ: 5.8 с против
    1.0 с с отключением, ускорение в 5.6 раза). Расшифрование не
    зависит от этой опции: gpg определяет формат потока автоматически.
    """
    return [
        gpg_binary,
        "--batch", "--yes", "--no-tty",
        "--pinentry-mode", "loopback",
        "--symmetric",
        "--cipher-algo", CIPHER_ALGO,
        "--compress-level", "0",
        "--passphrase-fd", str(passphrase_fd),
        "--output", "-",
    ]


def gpg_decrypt_cmd(gpg_binary, passphrase_fd):
    """
    Команда расшифрования: зашифрованный stdin -> расшифрованный stdout.

    --status-fd 2: машиночитаемые статусы ([GNUPG:] BAD_PASSPHRASE,
    NODATA, ...) идут в stderr вместе с сообщениями. Статусы НЕ
    локализуются, поэтому позволяют точно различить «неверный пароль»
    и «поврежденный архив» в diagnose_gpg_error().
    """
    return [
        gpg_binary,
        "--batch", "--yes", "--no-tty",
        "--pinentry-mode", "loopback",
        "--decrypt",
        "--passphrase-fd", str(passphrase_fd),
        "--status-fd", "2",
        "--output", "-",
    ]


def diagnose_gpg_error(stderr_text):
    """
    Точная причина отказа расшифрования по выводу gpg.

    Возвращает короткую диагностику: неверный пароль, поврежденный/
    неполный архив или неопределенная ошибка. Статусные строки
    [GNUPG:] не локализуются и надежнее человекочитаемых сообщений.
    """
    text = stderr_text or ""
    if "BAD_PASSPHRASE" in text or "Bad session key" in text:
        return "неверный пароль GPG (архив зашифрован другим паролем)"
    if "NODATA" in text or "no valid OpenPGP data" in text or \
            "invalid packet" in text:
        return "архив поврежден, не дочитан или не является архивом GPG"
    return "неверный пароль или поврежденный архив"


def make_passphrase_fd(password):
    """
    Создает pipe и записывает в него пароль; возвращает конец для чтения,
    который передается в --passphrase-fd дочернего процесса.

    Родительский процесс обязан закрыть возвращенный дескриптор после
    запуска дочернего процесса (иначе gpg будет ждать EOF).
    """
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, password.encode("utf-8"))
    finally:
        os.close(write_fd)
    os.set_inheritable(read_fd, True)
    return read_fd


def check_gpg(gpg_binary):
    """Возвращает строку версии gpg или None, если gpg недоступен."""
    try:
        proc = subprocess.run(
            [gpg_binary, "--version"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    first_line = (proc.stdout or "").strip().splitlines()
    return first_line[0].strip() if first_line else "gpg (версия неизвестна)"


def prompt_new_password():
    """
    Интерактивный ввод нового пароля с повтором, без отображения символов.

    Только для CLI (без curses): в TUI-режиме возбуждает EncryptionError.
    """
    _require_terminal_prompts()
    for _ in range(3):
        first = getpass.getpass("Задайте пароль GPG (ввод скрыт): ")
        if not first:
            print("Пароль не может быть пустым.")
            continue
        second = getpass.getpass("Повторите пароль: ")
        if first == second:
            return first
        print("Пароли не совпадают, попробуйте еще раз.")
    raise EncryptionError("Пароль не задан: три неудачные попытки ввода")


def resolve_backup_password(config_password, non_interactive):
    """
    Пароль для этапа шифрования резервного копирования.

    - неинтерактивный режим (cron): только пароль из config.py;
      если пароль отсутствует — завершение с ошибкой;
    - интерактивный режим: пользователь может переопределить пароль
      через скрытый ввод.
    """
    placeholder = config_password is None or config_password == "" or \
        config_password == "CHANGE_ME"

    if non_interactive:
        if placeholder:
            raise EncryptionError(
                "Пароль GPG не задан в config.py (GPG_PASSWORD). "
                "Неинтерактивный запуск без пароля невозможен."
            )
        log.info("Используется пароль GPG из config.py (неинтерактивный режим)")
        return config_password

    _require_terminal_prompts()
    if not placeholder:
        try:
            answer = input(
                "Использовать пароль GPG из config.py? "
                "[1=Да, 2=Нет, Enter — Да]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "y"
        if answer in ("", "y", "yes", "д", "да", "у", "1"):
            log.info("Используется пароль GPG из config.py")
            return config_password
    else:
        print("В config.py пароль GPG не задан (значение-заполнитель).")

    print("Введите пароль вручную (символы не отображаются).")
    return prompt_new_password()


def resolve_decryption_password(config_password, non_interactive):
    """
    Пароль для операций НАД СУЩЕСТВУЮЩИМИ архивами (проверка целостности
    перед отправкой upload-pending). Это пароль, КОТОРЫМ АРХИВ УЖЕ
    зашифрован, — однократный ввод, без повтора (в отличие от
    resolve_backup_password, где задается НОВЫЙ пароль).

    - неинтерактивный режим (cron): только config.py; отсутствие — ошибка;
    - интерактивный: пустой Enter — пароль из config.py (если задан),
      иначе явный ввод (prompt_decryption_password).
    """
    placeholder = is_placeholder(config_password)
    if non_interactive:
        if placeholder:
            raise EncryptionError(
                "Пароль GPG не задан в config.py (GPG_PASSWORD). "
                "Неинтерактивная проверка/отправка без пароля невозможна."
            )
        return config_password
    if not placeholder and not _terminal_prompts_allowed:
        # TUI: терминал недоступен, пароль должен быть передан явно;
        # если он не передан — понятная ошибка вместо порчи экрана.
        raise EncryptionError(
            "Пароль GPG не передан операции; в режиме TUI пароль "
            "запрашивается интерфейсом до запуска операции.")
    return prompt_decryption_password(config_password)


def prompt_decryption_password(config_password):
    """
    Пароль для расшифрования при восстановлении/проверке.

    Пустой ввод (Enter) — использовать пароль из config.py, если он задан.

    Только для CLI (без curses): в TUI-режиме возбуждает EncryptionError.
    """
    _require_terminal_prompts()
    for _ in range(3):
        try:
            entered = getpass.getpass(
                "Пароль GPG (Enter — взять пароль из config.py): "
            )
        except (EOFError, KeyboardInterrupt):
            raise EncryptionError("Пароль не введен")
        if entered:
            return entered
        placeholder = config_password is None or config_password == "" or \
            config_password == "CHANGE_ME"
        if not placeholder:
            return config_password
        print("Пароль не введен, а config.py не содержит корректного пароля.")
    raise EncryptionError("Пароль не введен: три пустые попытки")
