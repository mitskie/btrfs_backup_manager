"""
Загрузка и проверка конфигурации.

Единственный рабочий файл настроек — backup_config.py рядом с config.py.
Для автоматизации и приемочных испытаний допускается альтернативный
файл: python3 main.py --config /путь/файла.py (порядок аргументов
любой). Отдельного управления профилями в программе нет.

Загрузка: файл исполняется в изолированном пространстве имен.
Отсутствие отдельного ключа — значение по умолчанию. Отсутствие ФАЙЛА,
заданного в --config, — ошибка конфигурации. Отсутствие основного
backup_config.py не мешает интерактивному режиму: TUI обнаруживает
источники сам (разделение режимов ТЗ 2); неинтерактивные команды при
этом завершаются понятной ошибкой конфигурации.

Каждый источник в SUBVOLUMES может задавать собственные
snapshot_dir и local_backup_dir (приоритет над глобальными значениями).
"""

import os
import re
from pathlib import Path

from . import ConfigError
from .logging_setup import get_logger

log = get_logger("appconfig")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TS_LIKE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}_\d{6}(?:_\d{1,6})?(?:_[0-9a-fA-F]{6})?"
)
_COMPRESSIONS = ("gzip", "zstd")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Ключи настроек и значения по умолчанию. Значения-None пользователь
# должен задать сам; None в SUBVOLUMES означает «не задан» (для TUI это
# не ошибка — включается обнаружение источников).
DEFAULTS = {
    "SUBVOLUMES": None,
    "SNAPSHOT_DIR": None,
    "LOCAL_BACKUP_DIR": None,
    "COMPRESSION": "zstd",
    "COMPRESSION_FALLBACK": "gzip",
    "CRON_COMPRESSION_FALLBACK": True,
    "GZIP_LEVEL": 6,
    "ZSTD_LEVEL": 3,
    "GPG_BINARY": "/usr/bin/gpg",
    "GPG_PASSWORD": None,
    "NETWORK_ENABLED": True,
    "NETWORK_MOUNT": None,
    "NETWORK_DIRECTORY": "btrfs-backups",
    "AUTO_UPLOAD": True,
    "CHECK_NETWORK_MOUNT": True,
    "EXPECTED_NETWORK_FS_TYPE": None,
    "MIN_NETWORK_FREE_BYTES": 0,
    "RETENTION_COUNT": 5,
    "PROCESS_TIMEOUT": 3600,
    "LOG_FILE": None,
    "LOG_LEVEL": "INFO",
    "LOCK_FILE": None,
}

# Имя основного конфига
MAIN_CONFIG_NAME = "backup_config.py"


def project_root():
    """Каталог проекта (родитель пакета core)."""
    return Path(__file__).resolve().parent.parent


def main_config_path():
    """Путь основного конфигурационного файла backup_config.py."""
    return project_root() / MAIN_CONFIG_NAME


def sanitize_profile_name(name):
    """Имя -> безопасное имя файла (без расширения)."""
    label = str(name).strip()
    label = re.sub(r"[^A-Za-z0-9._-]", "_", label)
    if not re.match(r"[A-Za-z0-9]", label):
        label = "profile_" + label
    return label[:64]


# ============================================
# Загрузка конфигурации из файла
# ============================================

def read_config_values(path):
    """
    Значения настроек из файла конфигурации.

    Файл исполняется в изолированном пространстве имен; берутся только
    известные ключи. Отсутствующие ключи — значения по умолчанию.
    Ошибка синтаксиса/чтения — ConfigError с понятной причиной.
    """
    path = Path(path)
    namespace = {"__file__": str(path), "__name__": "backup_config"}
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            "Не удалось прочитать конфигурацию {}: {}".format(path, exc)
        ) from exc
    try:
        exec(compile(source, str(path), "exec"), namespace)  # noqa: S102
    except SyntaxError as exc:
        raise ConfigError(
            "Ошибка синтаксиса в конфигурации {} (строка {}): {}".format(
                path, exc.lineno, exc.msg)
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(
            "Ошибка при загрузке конфигурации {}: {}".format(path, exc)
        ) from exc
    values = {}
    for key, default in DEFAULTS.items():
        value = namespace.get(key, default)
        values[key] = value
    return values


def load_config(path=None, *, profile_name=None):
    """
    Загружает конфигурацию.

    path=None — основной backup_config.py. Если его нет, возвращается
    конфигурация из значений по умолчанию (config_found=False): этого
    достаточно для запуска TUI с обнаружением источников.

    Явно заданный path обязан существовать: иначе ConfigError
    (неинтерактивные команды и cron работают строго по конфигурации).
    """
    if path is None:
        main_path = main_config_path()
        if main_path.is_file():
            values = read_config_values(main_path)
            return AppConfig(
                values, path=main_path,
                profile_name=profile_name or "основной", config_found=True)
        return AppConfig(
            dict(DEFAULTS), path=None,
            profile_name=profile_name or "основной", config_found=False)
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = (project_root() / path).resolve()
    if not path.is_file():
        raise ConfigError(
            "Файл конфигурации не найден: {}. Укажите существующий "
            "файл настроек или создайте его по backup_config.py.example".format(path)
        )
    values = read_config_values(path)
    if profile_name is None:
        profile_name = path.stem if path.name != MAIN_CONFIG_NAME else "основной"
    return AppConfig(values, path=path, profile_name=profile_name,
                     config_found=True)


def from_module(config_module):
    """
    Конфигурация из текущих значений модуля config: единый объект
    без повторного чтения файла — тесты и запущенный процесс могут
    менять настройки в памяти.

    Отсутствие backup_config.py не мешает: config CONFIG_FILE_MISSING
    отражается в config_found (TUI включит обнаружение источников,
    CLI получит понятную ошибку валидации).
    """
    values = {}
    for key in DEFAULTS:
        values[key] = getattr(config_module, key, DEFAULTS.get(key))
    path = getattr(config_module, "_MAIN_PATH", None) or main_config_path()
    missing = bool(getattr(config_module, "CONFIG_FILE_MISSING", False))
    return AppConfig(values, path=None if missing else Path(path),
                     profile_name="основной", config_found=not missing)


def load_config_or_fail(path=None):
    """
    Конфигурация для неинтерактивных команд (CLI/cron).

    Отсутствие основного backup_config.py — ConfigError (CLI
    работает строго по конфигурации и не запускает обнаружение).
    """
    cfg = load_config(path)
    if not cfg.config_found:
        raise ConfigError(
            "Файл конфигурации не найден: {}. Для неинтерактивного запуска "
            "необходимы настройки (см. backup_config.py.example) либо "
            "укажите --config ПУТЬ.".format(main_config_path())
        )
    return cfg


# ============================================
# SUBVOLUMES: нормализация с индивидуальными каталогами
# ============================================

def normalize_subvolumes(raw):
    """
    Приводит SUBVOLUMES к единому виду
    [{name, source, snapshot_dir, local_backup_dir}, ...].

    Поддерживаются элементы-строки ("/home") и словари:
      {"name": "home", "source": "/home",
       "snapshot_dir": "/home/.snapshots",          # необязательно
       "local_backup_dir": "/var/backups/btrfs/home"}  # необязательно
    Индивидуальные пути приоритетнее глобальных SNAPSHOT_DIR /
    LOCAL_BACKUP_DIR. Возвращает (список, список ошибок).
    """
    items = []
    errors = []
    if raw is None:
        return items, ["SUBVOLUMES: не задан в конфигурации"]
    if not isinstance(raw, list) or len(raw) == 0:
        return items, ["SUBVOLUMES: должен быть непустым списком"]
    for i, item in enumerate(raw, 1):
        label = "SUBVOLUMES[{}]".format(i)
        entry = {
            "name": None,
            "source": None,
            "snapshot_dir": None,
            "local_backup_dir": None,
        }
        if isinstance(item, str):
            source = item.strip()
            name = None
        elif isinstance(item, dict):
            source = item.get("source")
            source = source.strip() if isinstance(source, str) else source
            name = item.get("name")
            if isinstance(name, str):
                name = name.strip()
            entry["snapshot_dir"] = _optional_path(
                item.get("snapshot_dir"), label, "snapshot_dir", errors)
            entry["local_backup_dir"] = _optional_path(
                item.get("local_backup_dir"), label, "local_backup_dir",
                errors)
        else:
            errors.append("{}: элемент должен быть строкой или словарем".format(
                label))
            continue
        if not name:
            if source == "/":
                name = "root"
            else:
                name = source.rstrip("/").rpartition("/")[2] if source else ""
        entry["name"] = name
        entry["source"] = source
        items.append(entry)
    return items, errors


def _optional_path(value, label, field, errors):
    """Индивидуальный путь источника: None или абсолютный путь."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            errors.append(
                "{}: '{}' пустая строка — задайте путь или уберите "
                "поле (значение берется из общих настроек)".format(
                    label, field))
            return None
        if not value.startswith("/"):
            errors.append(
                "{}: '{}' должен быть абсолютным путем (получено "
                "'{}')".format(label, field, value))
            return None
        return value
    errors.append(
        "{}: '{}' должен быть абсолютным путем или отсутствовать".format(
            label, field))
    return None


# ============================================
# Валидация значений (единая для config.py и AppConfig)
# ============================================

def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_values(values, *, strict=True) -> tuple:
    """
    Проверяет значения настроек.

    Возвращает (errors, warnings) — списки строк.

    strict=True — неинтерактивные команды (CLI/cron, ТЗ 2.3): отсутствие
    SUBVOLUMES, SNAPSHOT_DIR, GPG_PASSWORD, NETWORK_MOUNT — ошибка;
    программа не задает вопросов и не выбирает источники сама.

    strict=False — интерактивный режим: отсутствие этих
    настроек не ошибка — программа обнаружит источники и запросит
    недостающие каталоги и пароль у пользователя.
    """
    if strict:
        missing_errors = True
        missing_warnings = False
    else:
        missing_errors = False
        missing_warnings = True
    errors = []
    warnings = []

    raw_subvolumes = values.get("SUBVOLUMES")
    items, sub_errors = normalize_subvolumes(raw_subvolumes)
    if not strict and (raw_subvolumes is None or
                       (isinstance(raw_subvolumes, list) and
                        not raw_subvolumes)):
        # TUI: отсутствие источников — не ошибка, обнаружение включится
        sub_errors = []
        if raw_subvolumes is None:
            warnings.append(
                "SUBVOLUMES не задан: источники будут выбраны при "
                "обнаружении")
        else:
            warnings.append(
                "SUBVOLUMES пуст: источники будут выбраны при обнаружении")
    errors.extend(sub_errors)

    seen_names = set()
    for i, item in enumerate(items, 1):
        label = "SUBVOLUMES[{}]".format(i)
        name = item["name"]
        source = item["source"]
        if not isinstance(source, str) or not source:
            errors.append("{}: обязательный путь source".format(label))
        elif "\0" in source:
            errors.append("{}: 'source' содержит недопустимые символы".format(
                label))
        elif not source.startswith("/"):
            errors.append(
                "{}: 'source' должен быть абсолютным путем "
                "(получено '{}')".format(label, source))
        if not isinstance(name, str) or not name:
            errors.append("{}: не удалось определить имя subvolume".format(
                label))
        elif not _NAME_RE.match(name):
            errors.append(
                "{}: недопустимое имя '{}' (разрешены латиница, цифры, "
                "'.', '_', '-', длина 1-64)".format(label, name))
        elif _TS_LIKE_RE.search(name):
            errors.append(
                "{}: имя '{}' не должно напоминать временную метку".format(
                    label, name))
        elif name in seen_names:
            errors.append("{}: дублирующееся имя subvolume '{}'".format(
                label, name))
        else:
            seen_names.add(name)

    snapshot_dir = values.get("SNAPSHOT_DIR")
    local_backup_dir = values.get("LOCAL_BACKUP_DIR")
    network_mount = values.get("NETWORK_MOUNT")
    network_directory = values.get("NETWORK_DIRECTORY")

    if snapshot_dir is None:
        if missing_errors:
            errors.append("SNAPSHOT_DIR: не задан (укажите абсолютный путь)")
        elif missing_warnings:
            warnings.append(
                "SNAPSHOT_DIR не задан: каталог снапшотов будет выбран "
                "при обнаружении источников")
    elif not isinstance(snapshot_dir, str) or not snapshot_dir.startswith("/"):
        errors.append("SNAPSHOT_DIR: должен быть абсолютным путем")
    if local_backup_dir is not None and (
            not isinstance(local_backup_dir, str) or
            not local_backup_dir.startswith("/")):
        errors.append("LOCAL_BACKUP_DIR: должен быть абсолютным путем или None")
    if network_mount is None:
        if values.get("NETWORK_ENABLED", True) and missing_errors:
            errors.append(
                "NETWORK_MOUNT: не задан (укажите точку монтирования "
                "сетевой шары или отключите NETWORK_ENABLED)")
        else:
            warnings.append(
                "NETWORK_MOUNT не задан: сетевое хранилище не "
                "используется (локальное резервное копирование)")
    elif not isinstance(network_mount, str) or not network_mount.startswith("/"):
        errors.append("NETWORK_MOUNT: должен быть абсолютным путем")
    if not isinstance(network_directory, str) or not network_directory or \
            network_directory.startswith("/"):
        errors.append(
            "NETWORK_DIRECTORY: должен быть относительным каталогом "
            "внутри сетевой шары")
    elif ".." in Path(network_directory).parts:
        errors.append("NETWORK_DIRECTORY: не должен содержать '..'")

    # Синтаксические пересечения путей (полную проверку расположения на
    # файловой системе выполняет core.pathcheck.check_source).
    if isinstance(snapshot_dir, str) and snapshot_dir.startswith("/") and \
            isinstance(network_mount, str) and network_mount.startswith("/"):
        try:
            if Path(snapshot_dir).resolve() == Path(network_mount).resolve():
                errors.append(
                    "SNAPSHOT_DIR совпадает с NETWORK_MOUNT ({}): локальные "
                    "снапшоты и сетевые архивы должны находиться на разных "
                    "хранилищах".format(snapshot_dir)
                )
        except (OSError, ValueError):
            pass
    if isinstance(snapshot_dir, str) and snapshot_dir.startswith("/"):
        snap_resolved = _resolve_or_none(snapshot_dir)
        if snap_resolved:
            for i, item in enumerate(items, 1):
                source_resolved = _resolve_or_none(item.get("source"))
                if source_resolved and source_resolved == snap_resolved:
                    errors.append(
                        "SNAPSHOT_DIR совпадает с источником "
                        "SUBVOLUMES[{}] ('{}')".format(i, item["source"])
                    )
        for i, item in enumerate(items, 1):
            item_snap = item.get("snapshot_dir")
            item_snap_resolved = _resolve_or_none(item_snap) if item_snap \
                else None
            source_resolved = _resolve_or_none(item.get("source"))
            if item_snap_resolved and source_resolved and \
                    item_snap_resolved == source_resolved:
                errors.append(
                    "SUBVOLUMES[{}]: 'snapshot_dir' совпадает с "
                    "'source' ('{}')".format(i, item["source"])
                )

    # --- Retention --------------------------------------------------
    retention = values.get("RETENTION_COUNT")
    if not _is_int(retention) or retention <= 0:
        errors.append("RETENTION_COUNT: должно быть целым числом больше 0")

    # --- Сжатие -----------------------------------------------------
    compression = values.get("COMPRESSION")
    compression_fallback = values.get("COMPRESSION_FALLBACK")
    if compression not in _COMPRESSIONS:
        errors.append(
            "COMPRESSION: '{}' недопустимо (разрешено: {})".format(
                compression, ", ".join(_COMPRESSIONS))
        )
    if compression_fallback not in _COMPRESSIONS:
        errors.append(
            "COMPRESSION_FALLBACK: '{}' недопустимо (разрешено: {})".format(
                compression_fallback, ", ".join(_COMPRESSIONS))
        )
    if compression_fallback == compression:
        warnings.append(
            "COMPRESSION_FALLBACK совпадает с COMPRESSION: fallback не дает "
            "альтернативы, если основная утилита отсутствует"
        )
    if not isinstance(values.get("CRON_COMPRESSION_FALLBACK"), bool):
        errors.append("CRON_COMPRESSION_FALLBACK: должно быть true или false")
    if not _is_int(values.get("GZIP_LEVEL")) or \
            not 1 <= values.get("GZIP_LEVEL") <= 9:
        errors.append("GZIP_LEVEL: должно быть целым числом от 1 до 9")
    if not _is_int(values.get("ZSTD_LEVEL")) or \
            not 1 <= values.get("ZSTD_LEVEL") <= 19:
        errors.append("ZSTD_LEVEL: должно быть целым числом от 1 до 19")

    # --- GPG ----------------------------------------------------------
    gpg_binary = values.get("GPG_BINARY")
    if not isinstance(gpg_binary, str) or not gpg_binary.strip():
        errors.append("GPG_BINARY: должен быть путем к исполняемому файлу gpg")
    password = values.get("GPG_PASSWORD")
    if is_password_placeholder_value(password):
        if password == "CHANGE_ME":
            warnings.append(
                "GPG_PASSWORD: установлено значение-заполнитель 'CHANGE_ME'. "
                "Неинтерактивный запуск (cron) будет завершаться ошибкой; "
                "задайте настоящий пароль."
            )
        elif missing_errors:
            errors.append("GPG_PASSWORD: пароль не задан")
        elif missing_warnings:
            warnings.append(
                "GPG_PASSWORD не задан: пароль будет запрошен при "
                "интерактивном запуске")

    # --- Сетевая шара ---------------------------------------------------
    if not isinstance(values.get("NETWORK_ENABLED"), bool):
        errors.append("NETWORK_ENABLED: должно быть true или false")
    if not isinstance(values.get("AUTO_UPLOAD"), bool):
        errors.append("AUTO_UPLOAD: должно быть true или false")
    if not isinstance(values.get("CHECK_NETWORK_MOUNT"), bool):
        errors.append("CHECK_NETWORK_MOUNT: должно быть true или false")
    if values.get("EXPECTED_NETWORK_FS_TYPE") is not None and \
            not isinstance(values.get("EXPECTED_NETWORK_FS_TYPE"), str):
        errors.append(
            "EXPECTED_NETWORK_FS_TYPE: должно быть строкой или None")
    if not _is_int(values.get("MIN_NETWORK_FREE_BYTES")) or \
            values.get("MIN_NETWORK_FREE_BYTES") < 0:
        errors.append(
            "MIN_NETWORK_FREE_BYTES: должно быть целым числом >= 0")

    # --- Таймауты, блокировка, логирование ------------------------------
    if not _is_int(values.get("PROCESS_TIMEOUT")) or \
            values.get("PROCESS_TIMEOUT") < 1:
        errors.append(
            "PROCESS_TIMEOUT: должно быть целым числом >= 1 (секунды)")
    lock_file = values.get("LOCK_FILE")
    log_file = values.get("LOG_FILE")
    if lock_file is None:
        warnings.append(
            "LOCK_FILE не задан: используется файл по умолчанию для "
            "конфигурации '{}'".format(values.get("_profile_name", "основной")))
    elif not isinstance(lock_file, str) or not lock_file.startswith("/"):
        errors.append("LOCK_FILE: должен быть абсолютным путем")
    if log_file is None:
        warnings.append(
            "LOG_FILE не задан: используется журнал по умолчанию для "
            "конфигурации '{}'".format(values.get("_profile_name", "основной")))
    elif not isinstance(log_file, str) or not log_file.startswith("/"):
        errors.append("LOG_FILE: должен быть абсолютным путем")
    log_level = values.get("LOG_LEVEL")
    if not isinstance(log_level, str) or log_level.upper() not in _LOG_LEVELS:
        errors.append(
            "LOG_LEVEL: '{}' недопустим (разрешено: {})".format(
                log_level, ", ".join(_LOG_LEVELS))
        )

    return errors, warnings


def _resolve_or_none(path):
    try:
        return str(Path(path).resolve())
    except (OSError, ValueError, RuntimeError):
        return None


def is_password_placeholder_value(password) -> bool:
    """True, если пароль фактически не задан (пустой или заполнитель)."""
    return (not isinstance(password, str)) or password == "" or \
        password == "CHANGE_ME"


# ============================================
# Конфигурация как объект
# ============================================

class AppConfig:
    """
    Загруженная конфигурация.

    Предоставляет тот же интерфейс, что и модуль config.py
    (normalize_subvolumes, retention_count, local_backup_dir,
    is_password_placeholder, validate_config,
    check_config_permissions), поэтому core-функции работают и с
    модулем, и с этим объектом без изменений (единый механизм).
    """

    def __init__(self, values, *, path=None, profile_name="основной",
                 config_found=True):
        self._values = dict(values)
        self.path = path
        self.profile_name = profile_name
        self.config_found = config_found
        # Атрибуты-настройки (config.SUBVOLUMES и т.д. читаются атрибутом)
        for key in DEFAULTS:
            setattr(self, key, self._values.get(key))

    # ---------- настройки ----------

    def value(self, key):
        """Значение настройки с учетом изменений в текущем сеансе."""
        return getattr(self, key, DEFAULTS.get(key))

    def set_value(self, key, value):
        """Изменение настройки в памяти (без записи файла)."""
        self._values[key] = value
        setattr(self, key, value)

    def as_values(self):
        """Снимок значений настроек (для валидации и отчетов)."""
        snapshot = {}
        for key in DEFAULTS:
            snapshot[key] = getattr(self, key, DEFAULTS.get(key))
        snapshot["_profile_name"] = self.profile_name
        return snapshot

    # ---------- совместимость с модулем config ----------

    def normalize_subvolumes(self, raw=None):
        return normalize_subvolumes(
            self.SUBVOLUMES if raw is None else raw)

    def retention_count(self):
        return getattr(self, "RETENTION_COUNT", None)

    def local_backup_dir(self, snapshot_dir=None):
        """Каталог локальных архивов: LOCAL_BACKUP_DIR или SNAPSHOT_DIR/backup."""
        if self.LOCAL_BACKUP_DIR:
            return self.LOCAL_BACKUP_DIR
        if snapshot_dir is None:
            snapshot_dir = self.SNAPSHOT_DIR
        if not snapshot_dir:
            return None
        return str(Path(snapshot_dir) / "backup")

    @staticmethod
    def is_password_placeholder(password) -> bool:
        return is_password_placeholder_value(password)

    def validate_config(self, *, strict=True) -> tuple:
        return validate_values(self.as_values(), strict=strict)

    def check_config_permissions(self) -> list:
        """Предупреждение о широких правах файла настроек (пароль GPG)."""
        if self.path is None:
            return []
        try:
            st = os.stat(str(self.path))
        except OSError:
            return []
        if st.st_mode & 0o077:
            return [
                "{} доступен группе или другим пользователям ({}); файл "
                "содержит настройки и пароль GPG — рекомендуется 600. "
                "Выполните: chown root:root {} && chmod 600 {}".format(
                    self.path.name, oct(st.st_mode & 0o777),
                    self.path, self.path)
            ]
        return []

    def validate_or_raise(self, *, strict=True) -> list:
        """Ошибки — ConfigError; возвращает список предупреждений."""
        errors, warnings = self.validate_config(strict=strict)
        all_warnings = list(warnings) + list(self.check_config_permissions())
        for warning in all_warnings:
            log.warning("Конфигурация: %s", warning)
        if errors:
            raise ConfigError(
                "Ошибки конфигурации:\n  - " + "\n  - ".join(errors)
            )
        return all_warnings

    # ---------- индивидуальные каталоги источников ----------

    def resolve_snapshot_dir(self, item) -> str:
        """Каталог снапшотов источника: индивидуальный или глобальный."""
        own = (item or {}).get("snapshot_dir")
        return own or self.SNAPSHOT_DIR

    def resolve_local_backup_dir(self, item, snapshot_dir=None) -> str:
        """Каталог архивов источника: индивидуальный или производный."""
        own = (item or {}).get("local_backup_dir")
        if own:
            return own
        if snapshot_dir is None:
            snapshot_dir = self.resolve_snapshot_dir(item)
        return self.local_backup_dir(snapshot_dir)

    def source_items(self, require_valid=True):
        """
        Нормализованный список источников профиля; при require_valid
        ошибки нормализации — ConfigError (неинтерактивные команды).
        """
        items, errors = normalize_subvolumes(self.SUBVOLUMES)
        if errors and require_valid:
            raise ConfigError(
                "Конфигурация SUBVOLUMES некорректна: " + "; ".join(errors)
            )
        return items

    def local_backup_dirs(self):
        """
        Все каталоги локальных архивов профиля (уникальные, в порядке
        обхода источников): индивидуальные пути источников и глобальный.
        """
        result = []
        seen = set()

        def add(path):
            if not path:
                return
            key = str(Path(path))
            if key not in seen:
                seen.add(key)
                result.append(key)

        if self.config_found and self.SUBVOLUMES is not None:
            for item in self.source_items(require_valid=False):
                add(self.resolve_local_backup_dir(item))
        add(self.local_backup_dir(self.SNAPSHOT_DIR))
        return result

    def default_lock_file(self):
        """Файл блокировки профиля: LOCK_FILE или производный по имени."""
        if self.LOCK_FILE:
            return self.LOCK_FILE
        stem = sanitize_profile_name(self.profile_name)
        if stem == "backup_config":
            stem = "main"
        if stem == "основной":
            return "/run/lock/btrfs-backup-manager.lock"
        lock_dir = Path("/run/lock") if os.geteuid() == 0 else \
            project_root() / "logs"
        return str(lock_dir / "bbm-{}.lock".format(stem))

    def default_log_file(self):
        """Файл журнала: LOG_FILE или производный по имени."""
        if self.LOG_FILE:
            return self.LOG_FILE
        stem = sanitize_profile_name(self.profile_name)
        if stem == "backup_config":
            stem = "main"
        log_dir = project_root() / "logs"
        if stem == "основной":
            return str(log_dir / "btrfs-backup.log")
        return str(log_dir / "bbm-{}.log".format(stem))
