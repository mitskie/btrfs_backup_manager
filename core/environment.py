"""
Окружение: права, системные утилиты, каталоги, диагностика.

Диагностика разделяет результаты на три уровня:
  critical — операция невозможна;
  warning  — операция возможна с ограничениями;
  optional — необязательные компоненты (zstd, сетевая шара, curses).
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import (
    ConfigError, EnvironmentCheckError, UtilityMissingError,
)
from .logging_setup import get_logger

log = get_logger("environment")

CRITICAL = "critical"
WARNING = "warning"
INFO = "info"
OK = "ok"

_HOSTNAME_CACHE = None


def hostname():
    """
    Имя хоста для именования архивов: на нескольких машинах без имени
    хоста в имени архива возникает путаница, откуда архив.

    Приводится к безопасному для имени файла виду: строчные буквы, цифры
    и "-", длина не более 32 символов. Пустое/неразборчивое имя хоста
    заменяется на "host". Результат кешируется: имя архива не должно
    меняться в пределах запуска.
    """
    global _HOSTNAME_CACHE
    if _HOSTNAME_CACHE is not None:
        return _HOSTNAME_CACHE
    raw = ""
    try:
        raw = os.uname().nodename or ""
    except (OSError, AttributeError):
        raw = ""
    if not raw:
        raw = os.environ.get("HOSTNAME") or ""
    cleaned = re.sub(r"[^a-z0-9-]+", "-", (raw or "").lower()).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)[:32].strip("-")
    _HOSTNAME_CACHE = cleaned or "host"
    return _HOSTNAME_CACHE


def require_root():
    """Проверка прав root (операции с Btrfs и системными каталогами)."""
    if os.geteuid() != 0:
        raise EnvironmentCheckError(
            "Программа должна запускаться от root (операции с Btrfs и "
            "системными каталогами): sudo python3 main.py ..."
        )


def which(name):
    """Путь к утилите или None (shutil.which)."""
    try:
        return shutil.which(name)
    except (OSError, TypeError):
        return None


def btrfs_version():
    """Версия btrfs-progs или None."""
    try:
        proc = subprocess.run(
            ["btrfs", "--version"], capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    first = (proc.stdout or "").strip().splitlines()
    return first[0].strip() if first else "btrfs"


def check_utilities(compression=None, need_btrfs=True, need_gpg=True,
                    gpg_binary=None, gpg_check=None):
    """
    Проверка наличия системных утилит.

    Отсутствие btrfs/gpg — критическая ошибка (UtilityMissingError, код 3).
    compression: если задан, утилита сжатия обязательна (UtilityMissingError);
    проверку gpg можно передать внешней функцией (gpg_check(binary) -> str|None).
    Возвращает dict {name: версия/путь}.
    """
    found = {}
    missing = []
    if need_btrfs:
        if which("btrfs") is None:
            missing.append("btrfs (пакет btrfs-progs)")
        else:
            found["btrfs"] = btrfs_version() or "btrfs"
    if need_gpg:
        binary = gpg_binary or "gpg"
        version = gpg_check(binary) if gpg_check else _gpg_version(binary)
        if version is None:
            missing.append("gpg ({})".format(binary))
        else:
            found["gpg"] = version
    if compression:
        if which(compression) is None:
            missing.append(compression)
        else:
            found[compression] = which(compression)
    if missing:
        raise UtilityMissingError(
            "Отсутствуют необходимые утилиты: {}".format(", ".join(missing))
        )
    return found


def _gpg_version(binary):
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    first = (proc.stdout or "").strip().splitlines()
    return first[0].strip() if first else "gpg (версия неизвестна)"


def curses_available():
    """True, если модуль curses доступен в этой сборке Python."""
    try:
        import curses  # noqa: F401
        return True
    except Exception:
        return False


def ensure_local_dirs(local_backup_dir):
    """Создает каталог локальных архивов; возвращает Path."""
    backup_dir = Path(local_backup_dir)
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise EnvironmentCheckError(
            "Не удалось создать каталог локальных архивов {}: {}".format(
                backup_dir, exc)
        ) from exc
    return backup_dir


def writable_dir(path):
    """True, если в каталог возможна запись."""
    try:
        with tempfile.NamedTemporaryFile(
                prefix=".bbm-write-test-", dir=str(path), delete=True):
            pass
        return True
    except OSError:
        return False


def free_space(path):
    """Свободное место в байтах или None, если определить не удалось."""
    try:
        import shutil as _shutil
        return _shutil.disk_usage(str(path)).free
    except OSError:
        return None


# ============================================
# Диагностика
# ============================================

def run_diagnostics(config, *, ops=None):
    """
    Полная диагностика конфигурации и окружения.

    Возвращает список словарей {"level", "message"}; уровни:
    critical / warning / info / ok. Функция не возбуждает исключений:
    любые проблемы отражаются в результате.
    """
    results = []

    def add(level, message):
        results.append({"level": level, "message": message})

    def ok(msg):
        add(OK, msg)

    def fail(msg):
        add(CRITICAL, msg)

    def warn(msg):
        add(WARNING, msg)

    # 1-2. Права пользователя
    if os.geteuid() == 0:
        ok("Права root")
    else:
        fail("Программа запущена без root (операции с Btrfs требуют root)")

    # 3. Версия Python
    ok("Python {}.{}.{}".format(*sys.version_info[:3]))

    # 4-5. btrfs
    if which("btrfs"):
        version = btrfs_version()
        ok("btrfs: {}".format(version or "версию определить не удалось"))
    else:
        fail("Утилита btrfs не найдена (пакет btrfs-progs)")

    # 6. GPG
    gpg_version = None
    try:
        from .encryption import check_gpg
        gpg_version = check_gpg(config.GPG_BINARY)
    except Exception as exc:  # noqa: BLE001
        warn("Проверка GPG не выполнена: {}".format(exc))
    if gpg_version:
        ok("GPG: {}".format(gpg_version))
    else:
        fail("GPG недоступен: {}".format(config.GPG_BINARY))

    # 7-8. gzip / zstd (zstd — необязательный при доступном gzip, ТЗ 16)
    gzip_path = which("gzip")
    if gzip_path:
        ok("gzip: {}".format(gzip_path))
    else:
        fail("Утилита gzip не найдена")
    zstd_path = which("zstd")
    primary = getattr(config, "COMPRESSION", "zstd")
    fallback = getattr(config, "COMPRESSION_FALLBACK", "gzip")
    if zstd_path:
        ok("zstd: {}".format(zstd_path))
    elif gzip_path:
        warn("zstd не найден; доступен fallback на gzip (основной: {}, "
             "резервный: {})".format(primary, fallback))
    else:
        if primary == "zstd":
            fail("Не найдены ни zstd, ни gzip: резервное копирование невозможно")

    # 9. curses
    if curses_available():
        ok("Модуль curses доступен (TUI)")
    else:
        warn("Модуль curses недоступен: будет использован простой CLI")

    # 10-11. Локальные каталоги
    try:
        snapshot_dir = Path(config.SNAPSHOT_DIR)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        ok("Каталог снапшотов: {}".format(snapshot_dir))
    except OSError as exc:
        fail("Каталог снапшотов {}: {}".format(config.SNAPSHOT_DIR, exc))
        snapshot_dir = None
    try:
        backup_dir = Path(config.local_backup_dir(config.SNAPSHOT_DIR))
        backup_dir.mkdir(parents=True, exist_ok=True)
        ok("Локальный каталог архивов: {}".format(backup_dir))
    except Exception as exc:  # noqa: BLE001
        fail("Локальный каталог архивов: {}".format(exc))
        backup_dir = None
    if snapshot_dir is not None and not writable_dir(snapshot_dir):
        fail("Каталог снапшотов {} недоступен для записи".format(snapshot_dir))
    if backup_dir is not None and not writable_dir(backup_dir):
        fail("Каталог архивов {} недоступен для записи".format(backup_dir))
    if snapshot_dir is not None:
        from .snapshots import is_btrfs_path
        if is_btrfs_path(str(snapshot_dir)):
            ok("Каталог снапшотов находится на файловой системе Btrfs")
        else:
            warn("Каталог снапшотов {} находится НЕ на файловой системе "
                 "Btrfs: создание снапшотов завершится ошибкой".format(snapshot_dir))

    # 12. Исходные subvolume
    try:
        from .snapshots import is_subvolume
        items, sub_errors = config.normalize_subvolumes(config.SUBVOLUMES)
        for error in sub_errors:
            fail("Конфигурация: " + error)
        for item in items:
            source = item["source"]
            if not os.path.exists(source):
                fail("Subvolume '{}' ({}) не существует".format(item["name"], source))
            elif not is_subvolume(source):
                fail("'{}' не является Btrfs subvolume ({})".format(
                    source, item["name"]))
            else:
                ok("Subvolume '{}': {}".format(item["name"], source))
    except Exception as exc:  # noqa: BLE001
        fail("Проверка subvolume не выполнена: {}".format(exc))

    # 13. Сетевая шара
    network_enabled = getattr(config, "NETWORK_ENABLED", True)
    if not network_enabled:
        add(INFO, "Сетевое хранилище отключено (NETWORK_ENABLED=false)")
    else:
        from .network import check_network_mount
        try:
            info = check_network_mount(
                config.NETWORK_MOUNT,
                expected_fs_type=getattr(config, "EXPECTED_NETWORK_FS_TYPE", None),
                check=getattr(config, "CHECK_NETWORK_MOUNT", True),
            )
            ok("Сетевая шара: {} (ФС {})".format(
                config.NETWORK_MOUNT, info.fstype))
        except Exception as exc:  # noqa: BLE001
            warn("Сетевая шара недоступна: {}. Локальное резервное копирование "
                 "работает; архивы получат статус upload-pending.".format(exc))

    # 14. Права доступа к конфигурации
    try:
        for warning in config.check_config_permissions():
            warn(warning)
    except Exception as exc:  # noqa: BLE001
        warn("Проверка прав config.py не выполнена: {}".format(exc))

    # Конфигурация целиком
    try:
        errors, warnings = config.validate_config()
        if errors:
            for error in errors:
                fail("Конфигурация: " + error)
        else:
            ok("Конфигурация (config.py): ошибок нет")
        for warning in warnings:
            warn(warning)
    except Exception as exc:  # noqa: BLE001
        fail("Конфигурация не валидируется: {}".format(exc))

    # 15. Временные файлы
    try:
        with tempfile.TemporaryFile() as handle:
            handle.write(b"bbm")
        ok("Создание временных файлов")
    except OSError as exc:
        fail("Не удается создавать временные файлы: {}".format(exc))

    # 16. Свободное место
    for label, path in (("Каталог снапшотов", snapshot_dir),
                        ("Локальный каталог архивов", backup_dir)):
        if path is None:
            continue
        free = free_space(path)
        if free is None:
            add(INFO, "Свободное место ({}) определить не удалось".format(label))
        else:
            from .process import human_size
            ok("{}: свободно {}".format(label, human_size(free)))
    if network_enabled:
        from .process import human_size
        free = free_space(config.NETWORK_MOUNT)
        if free is not None:
            ok("Сетевая шара: свободно {}".format(human_size(free)))

    return results


def validate_or_raise(config, *, strict=True):
    """
    Валидация конфигурации: ошибки — ConfigError (код 2).

    strict=True — неинтерактивные команды (CLI/cron, ТЗ 2.3);
    strict=False — интерактивный режим (отсутствие SUBVOLUMES и прочих
    незаданных настроек не ошибка, ТЗ 2.1-2.2).
    """
    try:
        errors, warnings = config.validate_config(strict=strict)
    except TypeError:
        errors, warnings = config.validate_config()
    all_warnings = list(warnings) + list(config.check_config_permissions())
    for warning in all_warnings:
        log.warning("Конфигурация: %s", warning)
    if errors:
        raise ConfigError(
            "Ошибки конфигурации:\n  - " + "\n  - ".join(errors)
        )
    return all_warnings
