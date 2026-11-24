"""
Пакет core Btrfs Backup Manager.

Содержит иерархию исключений с кодами завершения и константы
приложения. Помощники запуска внешних команд — в core.process.
"""

APP_VERSION = "1.4.9.13"
APP_TITLE = "Btrfs Backup Manager"

# Коды завершения CLI
EXIT_OK = 0              # операция успешна
EXIT_ERROR = 1           # общая ошибка
EXIT_CONFIG = 2          # ошибка конфигурации или аргументов
EXIT_MISSING_TOOLS = 3   # отсутствуют необходимые системные утилиты
EXIT_BTRFS = 4           # ошибка Btrfs
EXIT_GPG = 5             # ошибка GPG
EXIT_COMPRESSION = 6     # ошибка сжатия или распаковки
EXIT_NETWORK = 7         # ошибка сетевой отправки
EXIT_VERIFICATION = 8    # ошибка проверки целостности
EXIT_ABORTED = 9         # операция отменена
EXIT_LOCKED = 10         # операция уже выполняется

# Расширения сжатия
COMPRESSOR_EXT = {"gzip": "gz", "zstd": "zst"}

# Запасной таймаут конвейеров, если config недоступен (секунды)
PROCESS_TIMEOUT_FALLBACK = 3600


class BtrfsBackupError(Exception):
    """Базовая ошибка приложения."""

    #: код завершения по умолчанию; может переопределяться точкой возбуждения
    exit_code = EXIT_ERROR


class ConfigError(BtrfsBackupError):
    """Некорректная конфигурация."""

    exit_code = EXIT_CONFIG


class EnvironmentCheckError(BtrfsBackupError):
    """Окружение не соответствует требованиям (root, утилиты, каталоги)."""


class UtilityMissingError(EnvironmentCheckError):
    """Отсутствует необходимая системная утилита."""

    exit_code = EXIT_MISSING_TOOLS


class LockError(BtrfsBackupError):
    """Операция уже выполняется (файл блокировки занят)."""

    exit_code = EXIT_LOCKED


class SnapshotError(BtrfsBackupError):
    """Ошибка работы со снапшотами."""

    exit_code = EXIT_BTRFS


class ArchiveError(BtrfsBackupError):
    """Ошибка формирования архива (конвейер btrfs send / сжатие / GPG)."""

    exit_code = EXIT_BTRFS


class CompressionError(BtrfsBackupError):
    """Ошибка сжатия или распаковки (отсутствие утилиты, поврежденный поток)."""

    exit_code = EXIT_COMPRESSION


class EncryptionError(BtrfsBackupError):
    """Ошибка шифрования/расшифрования GPG или работы с паролем."""

    exit_code = EXIT_GPG


class StorageError(BtrfsBackupError):
    """Ошибка локального или сетевого хранилища."""

    exit_code = EXIT_NETWORK


class VerificationError(BtrfsBackupError):
    """Ошибка проверки целостности (SHA-256, поток GPG/компрессора)."""

    exit_code = EXIT_VERIFICATION


class RestoreError(BtrfsBackupError):
    """Ошибка восстановления."""


class OperationAborted(BtrfsBackupError):
    """Операция прервана сигналом (SIGINT/SIGTERM) или пользователем."""

    exit_code = EXIT_ABORTED


class OperationCancelled(BtrfsBackupError):
    """Пользователь ответил «Нет» на запрос подтверждения."""

    exit_code = EXIT_ABORTED


def exit_code_for(exc):
    """Код завершения для исключения."""
    if isinstance(exc, RestoreError):
        return getattr(exc, "exit_code", EXIT_ERROR) or EXIT_ERROR
    code = getattr(exc, "exit_code", None)
    if isinstance(code, int) and 0 <= code <= 255:
        return code
    if isinstance(exc, BtrfsBackupError):
        return EXIT_ERROR
    return EXIT_ERROR
