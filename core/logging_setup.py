"""
Логирование Btrfs Backup Manager.

Стандартный logging + ротация файла (RotatingFileHandler).
Пароль GPG в лог никогда не попадает: он не включается ни в одно
сообщение, а внешние команды логируются только своими аргументами
(пароль передается через файловый дескриптор, --passphrase-fd).
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT_LOGGER_NAME = "btrfs_backup"

_MAX_BYTES = 10 * 1024 * 1024  # 10 МиБ на файл
_BACKUP_COUNT = 5              # хранить 5 архивных файлов

_configured = False


def _parse_level(level):
    try:
        return getattr(logging, str(level).upper())
    except (AttributeError, TypeError):
        return logging.INFO


def setup_logging(log_file, level="INFO", debug_stderr=False):
    """
    Настраивает корневой логгер приложения (однократно).

    log_file=None — путь не задан (профиль без настроек): используется
    журнал по умолчанию в каталоге logs/ проекта; если и он недоступен
    или путь в целом не открывается — stderr.
    debug_stderr — дублировать поток DEBUG в stderr (режим отладки).
    """
    global _configured
    if _configured:
        return
    _configured = True

    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.setLevel(_parse_level(level))
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    if not log_file:
        # Путь журнала не задан —
        # запасной путь рядом с проектом
        try:
            from .appconfig import project_root
            log_file = str(project_root() / "logs" / "btrfs-backup.log")
        except Exception:  # noqa: BLE001
            log_file = None

    if log_file:
        try:
            path = Path(log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                str(path), maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError as exc:
            file_handler = None
            stream_handler = logging.StreamHandler(sys.stderr)
            stream_handler.setFormatter(formatter)
            logger.addHandler(stream_handler)
            logger.warning(
                "Лог-файл %s недоступен (%s); логирование направлено в stderr",
                log_file, exc,
            )
    else:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    if debug_stderr:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        stream_handler.setLevel(logging.DEBUG)
        logger.addHandler(stream_handler)

    logger.propagate = False


def get_logger(name="app"):
    """Возвращает дочерний логгер приложения."""
    return logging.getLogger("{}.{}".format(ROOT_LOGGER_NAME, name))


def reset_logging():
    """Сброс конфигурации (для тестов)."""
    global _configured
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(logger.handlers):
        try:
            handler.close()
        except Exception:
            pass
        logger.removeHandler(handler)
    _configured = False
