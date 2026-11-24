"""
Проверка целостности резервных копий.

Проверять можно:
  - наличие архива и размер;
  - SHA-256 и соответствие файлу контрольной суммы / метаданным;
  - возможность расшифрования (gpg --decrypt);
  - возможность распаковки (gzip -t / zstd -t).

Наличие файла .sha256 или sidecar-метаданных САМО ПО СЕБЕ не считается
доказательством целостности: всегда вычисляется фактическая
сумма. Проверка не перезаписывает исходный архив: данные идут через pipe.
"""

import hashlib
import os
from pathlib import Path

from . import VerificationError
from .compression import detect_compression, stream_test_cmd
from .encryption import diagnose_gpg_error, gpg_decrypt_cmd, make_passphrase_fd
from .logging_setup import get_logger
from .process import (
    new_stderr_capture, popen_pipeline, stderr_tail, terminate_processes,
    wait_pipeline,
)

log = get_logger("verification")

_CHUNK = 4 * 1024 * 1024


def sha256_file(path):
    """Потоковое вычисление SHA-256 файла."""
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checksum_path(archive_path):
    """Путь файла контрольной суммы: <архив>.sha256."""
    return Path(str(archive_path) + ".sha256")


def parse_checksum_file(checksum_path):
    """Извлекает SHA-256 из файла формата sha256sum (<hash>  <имя>)."""
    from pathlib import Path
    try:
        first_line = Path(checksum_path).read_text(
            encoding="utf-8"
        ).splitlines()[0].strip()
    except (OSError, IndexError) as exc:
        raise VerificationError(
            "Файл контрольной суммы {} пуст или не читается: {}".format(
                checksum_path, exc
            )
        ) from exc
    value = first_line.split()[0].strip().lower() if first_line else ""
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise VerificationError(
            "Некорректная контрольная сумма в файле {}".format(checksum_path)
        )
    return value


def write_checksum_file(archive_path, sha256_hex):
    """Создает файл контрольной суммы рядом с архивом (атомарно)."""
    from pathlib import Path
    checksum = checksum_path(archive_path)
    content = "{}  {}\n".format(sha256_hex, Path(archive_path).name)
    tmp = checksum.with_name(checksum.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(str(tmp), str(checksum))
    log.info("Создан файл контрольной суммы: %s", checksum.name)
    return checksum


def verify_checksum(archive_path, expected_sha):
    """
    Сверяет фактический SHA-256 архива с ожидаемым значением.

    Возвращает вычисленный SHA-256; несовпадение — VerificationError
    (архив поврежден или подменен; ТЗ 10).
    """
    from pathlib import Path
    archive_path = Path(archive_path)
    if not archive_path.is_file():
        raise VerificationError(
            "Архив не найден: {}. Наличие файла проверяется ДО проверки "
            "контрольной суммы.".format(archive_path)
        )
    actual = sha256_file(archive_path)
    if actual != (expected_sha or "").lower():
        raise VerificationError(
            "SHA-256 архива {} НЕ совпал: ожидалось {}, фактически {}. "
            "Архив поврежден или подменен.".format(
                archive_path.name, expected_sha, actual
            )
        )
    log.info("SHA-256 подтвержден: %s (%s)", archive_path.name, actual)
    return actual


def verify_checksum_file(archive_path):
    """
    Сверяет SHA-256 архива с его файлом контрольной суммы.

    Возвращает вычисленный SHA-256; при отсутствии файла или несовпадении
    возбуждает VerificationError. Наличие .sha256 само по себе НЕ
    доказательство целостности — сумма вычисляется заново.
    """
    from pathlib import Path
    archive_path = Path(archive_path)
    if not archive_path.is_file():
        raise VerificationError(
            "Архив не найден: {}. Наличие файла проверяется ДО проверки "
            "контрольной суммы.".format(archive_path)
        )
    checksum = checksum_path(archive_path)
    if not checksum.is_file():
        raise VerificationError(
            "Файл контрольной суммы отсутствует: {}. Архив не может считаться "
            "исправным только из-за своего присутствия.".format(checksum)
        )
    expected = parse_checksum_file(checksum)
    return verify_checksum(archive_path, expected)


def test_decrypted_stream(archive_path, *, gpg_binary, password,
                          compression=None, abort_check=None):
    """
    Проверяет архив: расшифрование GPG и целостность сжатого потока
    (gzip -t / zstd -t). Данные передаются через pipe и не записываются
    на диск в незашифрованном виде.

    compression=None — алгоритм определяется по расширению архива.
    """
    from pathlib import Path
    archive_path = Path(archive_path)
    if not archive_path.is_file():
        raise VerificationError("Архив не найден: {}".format(archive_path))
    if compression is None:
        compression = detect_compression(archive_path.name)
        log.info("Алгоритм сжатия определен по расширению: %s (%s)",
                 compression, archive_path.name)

    passphrase_fd = make_passphrase_fd(password)
    passphrase_fd_closed = False
    procs = []
    captures = []
    try:
        err_gpg = new_stderr_capture()
        err_test = new_stderr_capture()
        captures = [err_gpg, err_test]
        with open(str(archive_path), "rb") as archive_stream:
            procs.append(
                popen_pipeline(
                    gpg_decrypt_cmd(gpg_binary, passphrase_fd),
                    stdin=archive_stream,
                    stdout=__import__("subprocess").PIPE,
                    stderr=err_gpg,
                    pass_fds=(passphrase_fd,),
                )
            )
        # Пароль находится в буфере pipe; gpg прочитает его до EOF.
        os.close(passphrase_fd)
        passphrase_fd_closed = True

        try:
            procs.append(
                popen_pipeline(
                    stream_test_cmd(compression),
                    stdin=procs[0].stdout,
                    stdout=__import__("subprocess").DEVNULL,
                    stderr=err_test,
                )
            )
        finally:
            procs[0].stdout.close()

        # Ожидание с тайм-аутом; отказ gpg завершает тестовый процесс.
        return_codes, killed = wait_pipeline(
            procs,
            error_class=VerificationError,
            what="Проверка архива {}".format(archive_path.name),
            abort_check=abort_check,
        )
        rc_gpg, rc_test = return_codes
        if rc_gpg != 0 and 0 not in killed:
            tail = stderr_tail(captures[0])
            reason = diagnose_gpg_error(tail)
            raise VerificationError(
                "Не удалось расшифровать архив {} (gpg rc={}): {}.\n"
                "Подсказка: архив мог быть создан с паролем, введенным "
                "вручную (не из config.py).{}".format(
                    archive_path.name, rc_gpg, reason,
                    ("" if reason.endswith("GPG)") else "\n" + tail),
                )
            )
        if rc_test != 0 and 1 not in killed:
            tail = stderr_tail(captures[1])
            raise VerificationError(
                "Сжатый поток архива {} поврежден ({} rc={}).{}".format(
                    archive_path.name, compression, rc_test,
                    (" " + tail) if tail else "",
                )
            )
        if rc_gpg != 0 or rc_test != 0:
            raise VerificationError(
                "Проверка архива {} прервана: один из процессов конвейера "
                "принудительно завершен (gpg rc={}, {} rc={}).".format(
                    archive_path.name, rc_gpg, compression, rc_test
                )
            )
        log.info(
            "Проверка архива пройдена (расшифрование + тест потока %s): %s",
            compression, archive_path.name,
        )
    except FileNotFoundError as exc:
        raise VerificationError(
            "Утилита проверки не найдена: {}".format(exc)
        ) from exc
    except Exception:
        terminate_processes(procs)
        raise
    finally:
        if not passphrase_fd_closed:
            try:
                os.close(passphrase_fd)
            except OSError:
                pass
        for capture in captures:
            try:
                capture.close()
            except OSError:
                pass


def verify_archive_full(archive_path, *, gpg_binary, password,
                        expected_sha=None, compression=None, abort_check=None):
    """
    Полная проверка архива: наличие, размер, SHA-256
    (фактический), расшифрование, распаковка.

    expected_sha: значение из метаданных/файла суммы; None — только
    вычислить фактическую сумму (без сверки).
    Возвращает dict с результатами проверки.
    """
    from pathlib import Path
    archive_path = Path(archive_path)
    result = {"path": str(archive_path), "name": archive_path.name}
    if not archive_path.is_file():
        raise VerificationError("Архив не найден: {}".format(archive_path))
    try:
        result["size"] = archive_path.stat().st_size
    except OSError as exc:
        raise VerificationError(
            "Не удалось определить размер архива {}: {}".format(archive_path, exc)
        ) from exc
    if result["size"] == 0:
        raise VerificationError(
            "Архив {} пуст — проверка не пройдена".format(archive_path.name)
        )
    result["compression"] = compression or detect_compression(archive_path.name)
    actual = sha256_file(archive_path)
    result["sha256"] = actual
    if expected_sha:
        if actual != expected_sha.lower():
            raise VerificationError(
                "SHA-256 архива {} НЕ совпал: ожидалось {}, фактически {}. "
                "Архив поврежден или подменен.".format(
                    archive_path.name, expected_sha, actual)
            )
    test_decrypted_stream(
        archive_path,
        gpg_binary=gpg_binary,
        password=password,
        compression=result["compression"],
        abort_check=abort_check,
    )
    result["decrypted"] = True
    result["stream_ok"] = True
    log.info("Полная проверка пройдена: %s", archive_path.name)
    return result
