"""
Блокировка параллельных операций.

Используется flock через fcntl. Одновременный запуск нескольких операций
backup/retention/upload-pending запрещен: повторный запуск получает
понятное сообщение и код завершения 10 (EXIT_LOCKED).

Блокировка освобождается автоматически при штатном завершении и при
завершении процесса (закрытие дескриптора ядром).
"""

import fcntl
import os
from pathlib import Path

from . import LockError
from .logging_setup import get_logger

log = get_logger("locking")

_lock_fd = None
_lock_path = None


def acquire_lock(configured_path, fallback_dir):
    """
    Захватывает файл блокировки (неблокирующий flock).

    configured_path — основной путь (config.LOCK_FILE);
    fallback_dir — каталог для запасного файла (например <проект>/logs).

    При занятой блокировке возбуждается LockError (код завершения 10).
    """
    global _lock_fd, _lock_path
    if _lock_fd is not None:
        return _lock_path
    candidates = [Path(configured_path), Path(fallback_dir) / ".backup.lock"]
    last_error = None
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            last_error = exc
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise LockError(
                "Другой экземпляр Btrfs Backup Manager уже выполняет операцию "
                "(файл блокировки: {}). Параллельные запуски запрещены.".format(path)
            )
        _lock_fd = fd
        _lock_path = path
        log.debug("Получена блокировка параллельного запуска: %s", path)
        return path
    raise LockError(
        "Не удалось создать файл блокировки: {}".format(last_error)
    )


def release_lock():
    """Освобождает файл блокировки (если захвачен этим процессом)."""
    global _lock_fd, _lock_path
    if _lock_fd is not None:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(_lock_fd)
        except OSError:
            pass
        _lock_fd = None
        _lock_path = None
        log.debug("Блокировка освобождена")
