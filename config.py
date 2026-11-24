# ============================================
# BTRFS BACKUP MANAGER — ЗАГРУЗКА И ПРОВЕРКА НАСТРОЕК
# ============================================
# Настройки задаются в файле backup_config.py (копия примера —
# backup_config.py.example). Этот файл содержит загрузку настроек и
# проверку их корректности; редактировать его не требуется.
#
# Отсутствие backup_config.py или отдельных настроек в нем не мешает
# запуску интерактивного режима (TUI обнаружит источники Btrfs сам),
# но неинтерактивные команды (CLI/cron) работают строго по настройкам
# и завершаются понятной ошибкой.
#
# Альтернативный файл настроек для автоматизации и приемочных
# испытаний: python3 main.py --config /путь/файла.py (порядок
# аргументов любой). Пароль из backup_config.py НИКОГДА не
# записывается в лог, не выводится при диагностике и не передается
# внешним процессам в аргументах (используется --passphrase-fd).

from pathlib import Path

from core import appconfig as _appconfig
from core.appconfig import (  # noqa: F401  (переэкспорт для совместимости)
    normalize_subvolumes as _normalize_subvolumes_impl,
)

# ============================================
# ЗАГРУЗКА НАСТРОЕК (backup_config.py)
# ============================================

_MAIN_PATH = _appconfig.main_config_path()

# Ошибка загрузки существующего файла настроек (синтаксис и т.п.):
# неинтерактивные команды завершаются с понятной ошибкой, TUI показывает
# причину; значения по умолчанию позволяют запустить обнаружение.
_LOAD_ERROR = None

try:
    import backup_config as _backup_config  # noqa: F401
except ImportError:
    if _MAIN_PATH.is_file():
        # Файл есть, но импортироваться не может — причина в самом файле
        try:
            _values = _appconfig.read_config_values(_MAIN_PATH)
            _LOAD_ERROR = None
        except Exception as exc:  # noqa: BLE001
            _values = dict(_appconfig.DEFAULTS)
            _LOAD_ERROR = str(exc)
    else:
        # Файла нет: значения по умолчанию для запуска TUI
        _values = dict(_appconfig.DEFAULTS)
else:
    # Файл импортирован: отдельный ключ отсутствует — значение по
    # умолчанию (неинтерактивный запуск при этом вернет ошибку валидации)
    _values = {key: getattr(_backup_config, key, default)
               for key, default in _appconfig.DEFAULTS.items()}

CONFIG_FILE_MISSING = not _MAIN_PATH.is_file()

AUTO_UPLOAD = _values["AUTO_UPLOAD"]
CHECK_NETWORK_MOUNT = _values["CHECK_NETWORK_MOUNT"]
COMPRESSION = _values["COMPRESSION"]
COMPRESSION_FALLBACK = _values["COMPRESSION_FALLBACK"]
CRON_COMPRESSION_FALLBACK = _values["CRON_COMPRESSION_FALLBACK"]
EXPECTED_NETWORK_FS_TYPE = _values["EXPECTED_NETWORK_FS_TYPE"]
GPG_BINARY = _values["GPG_BINARY"]
GPG_PASSWORD = _values["GPG_PASSWORD"]
GZIP_LEVEL = _values["GZIP_LEVEL"]
LOCAL_BACKUP_DIR = _values["LOCAL_BACKUP_DIR"]
LOCK_FILE = _values["LOCK_FILE"]
LOG_FILE = _values["LOG_FILE"]
LOG_LEVEL = _values["LOG_LEVEL"]
MIN_NETWORK_FREE_BYTES = _values["MIN_NETWORK_FREE_BYTES"]
NETWORK_DIRECTORY = _values["NETWORK_DIRECTORY"]
NETWORK_ENABLED = _values["NETWORK_ENABLED"]
NETWORK_MOUNT = _values["NETWORK_MOUNT"]
PROCESS_TIMEOUT = _values["PROCESS_TIMEOUT"]
RETENTION_COUNT = _values["RETENTION_COUNT"]
SNAPSHOT_DIR = _values["SNAPSHOT_DIR"]
SUBVOLUMES = _values["SUBVOLUMES"]
ZSTD_LEVEL = _values["ZSTD_LEVEL"]


def raise_if_broken():
    """Очистная ошибка конфигурации, если backup_config.py поврежден."""
    if _LOAD_ERROR:
        from core import ConfigError
        raise ConfigError(
            "Файл настроек {} не может быть загружен: {}".format(
                _MAIN_PATH, _LOAD_ERROR))


# ============================================
# ПРОВЕРКА КОРРЕКТНОСТИ (код менять при настройке не требуется)
# ============================================

def normalize_subvolumes(raw=None):
    """
    Приводит SUBVOLUMES к единому виду
    [{name, source, snapshot_dir, local_backup_dir}, ...].

    Поддерживаются элементы-строки ("/home") и словари с необязательными
    индивидуальными путями snapshot_dir / local_backup_dir (приоритет над
    глобальными настройками). Возвращает (список, список ошибок).
    """
    return _normalize_subvolumes_impl(
        SUBVOLUMES if raw is None else raw)


def _current_values() -> dict:
    """Текущие значения модуля (тесты меняют их напрямую)."""
    values = {}
    for key in _appconfig.DEFAULTS:
        values[key] = globals().get(key, _appconfig.DEFAULTS.get(key))
    return values


def retention_count():
    """Количество хранимых копий (RETENTION_COUNT) для retention."""
    return globals().get("RETENTION_COUNT")


def local_backup_dir(snapshot_dir) -> str:
    """
    Каталог локальных архивов: LOCAL_BACKUP_DIR или SNAPSHOT_DIR/backup.
    SNAPSHOT_DIR не задан (интерактивный режим без конфигурации) — None.
    """
    if LOCAL_BACKUP_DIR:
        return LOCAL_BACKUP_DIR
    if not snapshot_dir:
        return None
    return str(Path(snapshot_dir) / "backup")


def is_password_placeholder(password) -> bool:
    """True, если пароль фактически не задан (пустой или значение-заполнитель)."""
    return _appconfig.is_password_placeholder_value(password)


def check_config_permissions() -> list:
    """
    Предупреждения о слишком широких правах доступа к файлам конфигурации.

    backup_config.py — предупреждение критично: именно там хранится пароль
    GPG. config.py пароля НЕ содержит, но настройки видны другим
    пользователям системы; единый режим 600 не позволит случайно открыть
    доступ к паролю позже.
    """
    warnings = []
    own_dir = Path(__file__).resolve().parent
    details = (
        ("backup_config.py",
         "файл содержит пароль GPG"),
        ("config.py",
         "пароля не содержит (он в backup_config.py), но настройки видны "
         "другим пользователям"),
    )
    for filename, why in details:
        path = own_dir / filename
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_mode & 0o077:
            warnings.append(
                "{} доступен группе или другим пользователям ({}); {} — "
                "рекомендуется 600. Выполните: chown root:root {} && "
                "chmod 600 {}".format(
                    filename, oct(st.st_mode & 0o777), why, path, path
                )
            )
    return warnings


def validate_config(strict=True) -> tuple:
    """
    Проверяет конфигурацию (строгий режим — для CLI/cron).

    Возвращает кортеж (errors, warnings) — списки строк.
    Пустой список errors означает, что конфигурация корректна.
    strict=False (интерактивный режим, ТЗ 2.1-2.2): отсутствие
    SUBVOLUMES/SNAPSHOT_DIR/GPG_PASSWORD/NETWORK_MOUNT не считается
    ошибкой — программа обнаружит источники и запросит недостающее.
    """
    return _appconfig.validate_values(_current_values(), strict=strict)
