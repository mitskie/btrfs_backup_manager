"""
Тесты проверки целостности (ТЗ 10): SHA-256, sidecar-файлы, расшифрование,
тест сжатого потока — на реальных gzip и gpg (zstd — эмулятор).
Запуск: python3 tests/test_verification.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import PASSWORD, Suite, run_main  # noqa: E402
import config  # noqa: E402
from core import VerificationError  # noqa: E402
from core import verification as vr  # noqa: E402
from core.encryption import (  # noqa: E402
    gpg_encrypt_cmd, make_passphrase_fd,
)


def _make_encrypted(payload, compression, tmp, password=PASSWORD):
    """Создает реальный зашифрованный сжатый архив: payload -> compress -> gpg."""
    comp_cmd = {"gzip": ["gzip", "-c"],
                "zstd": ["zstd", "-c", "-3", "-q"]}[compression]
    compressed = subprocess.run(comp_cmd, input=payload, capture_output=True,
                                timeout=60)
    assert compressed.returncode == 0
    fd = make_passphrase_fd(password)
    try:
        enc = subprocess.run(
            gpg_encrypt_cmd(config.GPG_BINARY, fd), input=compressed.stdout,
            capture_output=True, pass_fds=(fd,), timeout=60)
    finally:
        os.close(fd)
    assert enc.returncode == 0, enc.stderr.decode()
    path = Path(tmp) / "root_2026-11-06_120000_a1b2c3.{}.gpg".format(
        "zst" if compression == "zstd" else "gz")
    path.write_bytes(enc.stdout)
    return path


def main():
    suite = Suite("verification")

    payload = os.urandom(200000) + b"VERIFY-ME" * 50

    with tempfile.TemporaryDirectory() as tmp:
        # sha256_file
        f = Path(tmp) / "data.bin"
        f.write_bytes(b"hello world")
        import hashlib
        suite.check(vr.sha256_file(f) ==
                    hashlib.sha256(b"hello world").hexdigest(),
                    "sha256_file совпадает с hashlib")

        # Файл контрольной суммы: запись, разбор, сверка
        sha = vr.sha256_file(f)
        vr.write_checksum_file(f, sha)
        suite.check(vr.parse_checksum_file(vr.checksum_path(f)) == sha,
                    "файл .sha256 записан и разобран")
        suite.check(vr.verify_checksum_file(f) == sha,
                    "сверка с .sha256 проходит")
        suite.check(vr.verify_checksum(f, sha) == sha,
                    "сверка с явной суммой проходит")

        # Несовпадение — архив поврежден или подменен (ТЗ 10)
        suite.expect_raises(
            lambda: vr.verify_checksum(f, "cd" * 32),
            VerificationError, "SHA-256 не совпал -> VerificationError")
        f.write_bytes(b"tampered!")
        suite.expect_raises(
            lambda: vr.verify_checksum_file(f),
            VerificationError, "измененный файл против .sha256 -> ошибка "
            "(наличие .sha256 НЕ доказательство целостности)")

        # Отсутствующие файлы
        suite.expect_raises(
            lambda: vr.verify_checksum_file(Path(tmp) / "nope.gpg"),
            VerificationError, "отсутствующий архив -> ошибка")
        f2 = Path(tmp) / "root_2026-11-06_120001_a1b2c4.gz.gpg"
        f2.write_bytes(b"data")
        suite.expect_raises(
            lambda: vr.verify_checksum_file(f2),
            VerificationError, "отсутствующий .sha256 -> ошибка "
            "(присутствие архива не делает его исправным)")

        # --- Полная проверка зашифрованных архивов ---
        for compression in ("gzip", "zstd"):
            path = _make_encrypted(payload, compression, tmp)
            result = vr.verify_archive_full(
                path, gpg_binary=config.GPG_BINARY, password=PASSWORD,
                expected_sha=vr.sha256_file(path))
            suite.check(result["decrypted"] and result["stream_ok"],
                        "полная проверка {}: расшифрование+поток OK".
                        format(compression))
            # по расширению
            result2 = vr.verify_archive_full(
                path, gpg_binary=config.GPG_BINARY, password=PASSWORD)
            suite.check(result2["compression"] == compression,
                        "{}: сжатие определено по расширению".format(compression))

        # Неверный пароль (ТЗ 2.5)
        path = _make_encrypted(payload, "gzip", tmp)
        suite.expect_raises(
            lambda: vr.test_decrypted_stream(
                path, gpg_binary=config.GPG_BINARY, password="WRONG"),
            VerificationError, "неверный пароль -> проверка не пройдена")

        # Поврежденный контейнер (обрезан)
        broken = Path(tmp) / "root_2026-11-06_120002_a1b2c5.gz.gpg"
        broken.write_bytes(path.read_bytes()[:len(path.read_bytes()) // 2])
        suite.expect_raises(
            lambda: vr.test_decrypted_stream(
                broken, gpg_binary=config.GPG_BINARY, password=PASSWORD),
            VerificationError, "поврежденный архив -> проверка не пройдена")

        # Поврежденный сжатый поток (расшифровывается, но gzip-поток бит)
        fd = make_passphrase_fd(PASSWORD)
        try:
            enc = subprocess.run(
                gpg_encrypt_cmd(config.GPG_BINARY, fd),
                input=b"this is not gzip", capture_output=True,
                pass_fds=(fd,), timeout=60)
        finally:
            os.close(fd)
        badstream = Path(tmp) / "root_2026-11-06_120003_a1b2c6.gz.gpg"
        badstream.write_bytes(enc.stdout)
        suite.expect_raises(
            lambda: vr.test_decrypted_stream(
                badstream, gpg_binary=config.GPG_BINARY, password=PASSWORD),
            VerificationError, "поврежденный сжатый поток -> ошибка")

        # Проверка не перезаписывает исходный архив (ТЗ 10)
        before = path.read_bytes()
        try:
            vr.test_decrypted_stream(
                path, gpg_binary=config.GPG_BINARY, password="WRONG")
        except VerificationError:
            pass
        suite.check(path.read_bytes() == before,
                    "исходный архив не изменен проверкой")

    return suite.summary()


if __name__ == "__main__":
    sys.exit(run_main(main))
