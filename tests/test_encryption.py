"""
Тесты шифрования GPG (ТЗ 2.2, 4.2, 18): реальный gpg, пароль только через fd.
Запуск: python3 tests/test_encryption.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import PASSWORD, Suite, run_main  # noqa: E402
import config  # noqa: E402
from core import EncryptionError  # noqa: E402
from core.encryption import (  # noqa: E402
    check_gpg, diagnose_gpg_error, gpg_decrypt_cmd, gpg_encrypt_cmd,
    is_placeholder, make_passphrase_fd, prompt_new_password,
    resolve_backup_password, resolve_decryption_password,
    set_terminal_prompts,
)


def _encrypt_decrypt_roundtrip(password, payload):
    """Шифрует и расшифровывает payload реальным gpg, возвращает результат."""
    enc_fd = make_passphrase_fd(password)
    enc_cmd = gpg_encrypt_cmd(config.GPG_BINARY, enc_fd)
    try:
        enc = subprocess.run(
            enc_cmd, input=payload, capture_output=True, pass_fds=(enc_fd,),
            timeout=60)
    finally:
        os.close(enc_fd)
    if enc.returncode != 0:
        raise RuntimeError("gpg encrypt failed: " + enc.stderr.decode())
    dec_fd = make_passphrase_fd(password)
    dec_cmd = gpg_decrypt_cmd(config.GPG_BINARY, dec_fd)
    try:
        dec = subprocess.run(
            dec_cmd, input=enc.stdout, capture_output=True, pass_fds=(dec_fd,),
            timeout=60)
    finally:
        os.close(dec_fd)
    return enc, dec


def main():
    suite = Suite("encryption")

    # Реальный gpg доступен
    version = check_gpg(config.GPG_BINARY)
    suite.check(bool(version), "gpg доступен: {}".format(version))

    # Круговой шифрование/расшифрование AES256
    payload = os.urandom(50000) + b"MARKER" * 10
    enc, dec = _encrypt_decrypt_roundtrip(PASSWORD, payload)
    suite.check(dec.returncode == 0 and dec.stdout == payload,
                "шифрование/расшифрование AES256: данные идентичны")

    # Неверный пароль: расшифрование завершается ошибкой (ТЗ 2.5)
    enc2_fd = make_passphrase_fd(PASSWORD)
    enc2 = subprocess.run(
        gpg_encrypt_cmd(config.GPG_BINARY, enc2_fd), input=payload,
        capture_output=True, pass_fds=(enc2_fd,), timeout=60)
    os.close(enc2_fd)
    bad_fd = make_passphrase_fd("wrong-password")
    dec2 = subprocess.run(
        gpg_decrypt_cmd(config.GPG_BINARY, bad_fd), input=enc2.stdout,
        capture_output=True, pass_fds=(bad_fd,), timeout=60)
    os.close(bad_fd)
    suite.check(dec2.returncode != 0 and not dec2.stdout,
                "неверный пароль: расшифрование не выполнено, данные не выданы")

    # Диагностика причины отказа (замечание пользователя о rc=2):
    # --status-fd 2 дает машиночитаемые статусы, причина точно различается
    suite.check("BAD_PASSPHRASE" in dec2.stderr.decode("utf-8", "replace"),
                "--status-fd 2: статус BAD_PASSPHRASE в stderr")
    suite.check("пароль" in diagnose_gpg_error(dec2.stderr.decode()),
                "diagnose_gpg_error: неверный пароль опознан точно")
    suite.check("поврежден" in diagnose_gpg_error(
                    "gpg: no valid OpenPGP data found."),
                "diagnose_gpg_error: не-архив/повреждение опознано")

    # --compress-level 0: gpg НЕ повторно сжимает уже сжатый поток
    # (замер: на 200 МиБ несжимаемых данных 5.8 с против 1.0 с —
    # ускорение в 5.6 раза, замечание пользователя о 5 ГиБ за 15 минут)
    suite.check("--compress-level" in gpg_encrypt_cmd(config.GPG_BINARY, 3)
                and "0" in gpg_encrypt_cmd(config.GPG_BINARY, 3),
                "шифрование без повторного сжатия (--compress-level 0)")
    suite.check("--status-fd" in gpg_decrypt_cmd(config.GPG_BINARY, 3),
                "расшифрование пишет статусы для диагностики (--status-fd 2)")

    # Тест производительности на реальном gpg: шифрование несжимаемых
    # данных должно идти быстрее 100 МиБ/с (--compress-level 0)
    big_payload = os.urandom(32 * 1024 * 1024)  # 32 МиБ несжимаемых данных
    import time as time_module
    enc_fd = make_passphrase_fd(PASSWORD)
    started = time_module.monotonic()
    enc3 = subprocess.run(
        gpg_encrypt_cmd(config.GPG_BINARY, enc_fd), input=big_payload,
        capture_output=True, pass_fds=(enc_fd,), timeout=120)
    os.close(enc_fd)
    elapsed = time_module.monotonic() - started
    suite.check(enc3.returncode == 0, "шифрование 32 МиБ выполнено")
    # Замер производительности — ИНФОРМАЦИОННЫЙ: жесткая граница <3 с
    # проваливалась на медленных/загруженных машинах (расхождение 648/644,
    # ТЗ 14.4). Свойство реализации (--compress-level 0) проверяется выше
    # статически; здесь остается щедрая граница, отсекающая только
    # патологическое зависание (gpg без сжатия шифрует 32 МиБ за секунды).
    suite.check(elapsed < 30,
                "скорость шифрования: 32 МиБ за {:.2f} с (<30 с; "
                "повторное сжатие отключено, замер информационный)".format(elapsed))
    # и расшифрование возвращает исходные данные
    dec_fd = make_passphrase_fd(PASSWORD)
    dec3 = subprocess.run(
        gpg_decrypt_cmd(config.GPG_BINARY, dec_fd), input=enc3.stdout,
        capture_output=True, pass_fds=(dec_fd,), timeout=120)
    os.close(dec_fd)
    suite.check(dec3.returncode == 0 and dec3.stdout == big_payload,
                "расшифрование без сжатия gpg: данные идентичны")

    # Пароль не попадает в argv (ТЗ 4.2, 18)
    enc_fd = make_passphrase_fd(PASSWORD)
    argv = gpg_encrypt_cmd(config.GPG_BINARY, enc_fd) + \
        gpg_decrypt_cmd(config.GPG_BINARY, enc_fd)
    os.close(enc_fd)
    suite.check(all(PASSWORD not in arg for arg in argv),
                "пароль отсутствует в командной строке (--passphrase-fd)")
    suite.check("--passphrase-fd" in gpg_encrypt_cmd(
        config.GPG_BINARY, 3), "используется --passphrase-fd")

    # check_gpg на отсутствующем бинарнике
    suite.check(check_gpg("/nonexistent/gpg") is None,
                "check_gpg: отсутствующий бинарник -> None")

    # resolve_backup_password: cron без пароля — ошибка (ТЗ 4.2)
    suite.expect_raises(
        lambda: resolve_backup_password("CHANGE_ME", non_interactive=True),
        EncryptionError, "cron: пароль-заполнитель -> ошибка")
    suite.check(resolve_backup_password("secret", non_interactive=True)
                == "secret", "cron: пароль берется из config.py")

    # resolve_decryption_password: существующий пароль для проверки/отправки
    suite.check(is_placeholder("CHANGE_ME") and is_placeholder("")
                and is_placeholder(None) and not is_placeholder("pw"),
                "is_placeholder: заполнитель распознан")
    suite.check(resolve_decryption_password("secret", non_interactive=True)
                == "secret",
                "cron-проверка/отправка: пароль из config.py")
    suite.expect_raises(
        lambda: resolve_decryption_password("CHANGE_ME", non_interactive=True),
        EncryptionError,
        "cron: проверка/отправка без пароля — ошибка")

    # prompt_new_password: совпадающие вводы принимаются
    import getpass as getpass_module
    inputs = iter(["alpha1", "alpha1"])
    real_getpass = getpass_module.getpass
    getpass_module.getpass = lambda *a, **k: next(inputs)
    try:
        suite.check(prompt_new_password() == "alpha1",
                    "интерактивный ввод нового пароля (скрытый ввод)")
    finally:
        getpass_module.getpass = real_getpass

    # Защита терминала в режиме TUI (1.1.1): прямые запросы пароля
    # запрещены — они конкурировали бы с curses за нажатия клавиш
    set_terminal_prompts(False)
    try:
        suite.expect_raises(
            prompt_new_password, EncryptionError,
            "TUI: prompt_new_password запрещен (защита терминала)")
        suite.expect_raises(
            lambda: resolve_backup_password("CHANGE_ME", non_interactive=False),
            EncryptionError,
            "TUI: интерактивный resolve_backup_password запрещен")
        suite.check(
            resolve_backup_password("secret", non_interactive=True)
            == "secret",
            "TUI: неинтерактивный (cron) путь пароля из config.py работает")
        suite.expect_raises(
            lambda: resolve_decryption_password("secret", non_interactive=False),
            EncryptionError,
            "TUI: проверка/отправка без явно переданного пароля — ошибка "
            "(однократный ввод делает интерфейс)")
    finally:
        set_terminal_prompts(True)
    inputs2 = iter(["alpha1", "alpha1"])
    getpass_module.getpass = lambda *a, **k: next(inputs2)
    try:
        suite.check(prompt_new_password() == "alpha1",
                    "после set_terminal_prompts(True) ввод снова разрешен")
    finally:
        getpass_module.getpass = real_getpass

    return suite.summary()


if __name__ == "__main__":
    sys.exit(run_main(main))
