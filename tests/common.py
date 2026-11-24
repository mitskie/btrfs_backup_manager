"""
Общая инфраструктура тестов Btrfs Backup Manager 1.1.

Особенности:
  - эмуляторы btrfs и zstd (tests/btrfs, tests/zstd) добавляются в PATH;
  - GPG, gzip, hashlib — РЕАЛЬНЫЕ; изолированный GNUPGHOME;
  - тесты выполняются без реального root (os.geteuid подменяется);
  - проверка типа ФС (is_btrfs_path) подменяется: расположение снапшотов
    контролируется эмулятором btrfs filesystem show через .shim-fsuuid;
  - активное монтирование сетевой шары имитируется управляемым флагом
    NETMOUNT_IS_MOUNTED (ТЗ: обычный каталог не считается сетевым
    хранилищем, поэтому os.path.ismount подменяется);
  - каждый тестовый модуль запускается runner'ом ОТДЕЛЬНЫМ процессом:
    подмены не перетекают между файлами.

Запуск отдельного модуля: python3 tests/test_compression.py
Запуск всех:              python3 tests/run_all.py
"""

import builtins
import os
import shutil
import sys
import tempfile
import traceback
from contextlib import contextmanager
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT = TESTS_DIR.parent

# Аварийная трассировка, если тестовый модуль завис дольше 240 секунд
import faulthandler  # noqa: E402
faulthandler.dump_traceback_later(240, exit=True)

# Эмуляторы должны быть исполняемыми (например, после распаковки из архива)
for _shim in ("btrfs", "zstd"):
    _shim_path = TESTS_DIR / _shim
    if _shim_path.is_file() and not os.access(str(_shim_path), os.X_OK):
        os.chmod(str(_shim_path), 0o755)

os.environ["PATH"] = str(TESTS_DIR) + os.pathsep + os.environ.get("PATH", "")

# Тесты никогда не читают интерактивный ввод: подменяем sys.stdin на
# пустой поток, чтобы случайные input()/getpass не блокировали прогон
# (EOF/пустая строка -> безопасные значения по умолчанию).
import io  # noqa: E402
sys.stdin = io.StringIO("")

# Изолированный GNUPGHOME: тесты не трогают ключи реального пользователя
_GNUPGHOME = Path(tempfile.mkdtemp(prefix="bbm11-gnupghome-"))
os.environ["GNUPGHOME"] = str(_GNUPGHOME)
os.chmod(str(_GNUPGHOME), 0o700)

sys.path.insert(0, str(PROJECT))

import config  # noqa: E402
from core import (  # noqa: E402
    BtrfsBackupError, EXIT_CONFIG, EXIT_NETWORK, OperationCancelled,
)
from core import archive, encryption  # noqa: E402
from core import compression as compression_module  # noqa: E402
from core import metadata as metadata_module  # noqa: E402
from core import network as network_module  # noqa: E402
from core import process as process_module  # noqa: E402
from core import restore as restore_module  # noqa: E402
from core import retention as retention_module  # noqa: E402
from core import runid as runid_module  # noqa: E402
from core import snapshots as snapshots_module  # noqa: E402
from core import verification as verification_module  # noqa: E402
from core import backup as backup_module  # noqa: E402
from core.logging_setup import reset_logging, setup_logging  # noqa: E402

PASSWORD = "correct-horse-battery-42"


# ============================================
# Окружение (применяется на импорте модуля)
# ============================================

os.geteuid = lambda: 0  # тесты выполняются без реального root

# Целевые каталоги в песочнице не на btrfs — отключаем только проверку
# типа ФС; остальные проверки работают.
restore_module.is_btrfs_path = lambda p: True
snapshots_module.is_btrfs_path = lambda p: True

# Эмуляция активного монтирования сетевой шары (см. докстринг модуля)
REAL_ISMOUNT = os.path.ismount
NETMOUNT_IS_MOUNTED = [True]


def _fake_ismount(path):
    try:
        if str(Path(path).resolve()) == str(Path(config.NETWORK_MOUNT).resolve()):
            return NETMOUNT_IS_MOUNTED[0]
    except (TypeError, ValueError, OSError):
        pass
    return REAL_ISMOUNT(path)


os.path.ismount = _fake_ismount

# Эмуляция /proc/self/mountinfo (v1.4.4): обязательная проверка
# монтирования выполняется по mountinfo ядра (ТЗ 3.2 исправление №1);
# в песочнице она подменяется управляемым флагом NETMOUNT_IS_MOUNTED
# (смонтированная шара — nfs4; не смонтированная — записи нет, что
# корректно отвергается как «обычный локальный каталог»).


def _fake_mountinfo_entries():
    if NETMOUNT_IS_MOUNTED[0]:
        try:
            real = str(Path(config.NETWORK_MOUNT).resolve())
        except (OSError, ValueError):
            real = str(config.NETWORK_MOUNT)
        return [
            ("/", "ext4", "/dev/root"),
            (real, "nfs4", "server:/export"),
        ]
    return []


network_module._mountinfo_entries = _fake_mountinfo_entries

reset_logging()
setup_logging(str(PROJECT / "logs" / "test.log"), "WARNING")


# ============================================
# Песочница
# ============================================

class Sandbox:
    """Изолированная среда одного запуска тестов."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="bbm11-sandbox-"))
        self.snapshot_dir = self.root / "snapshots"
        self.backup_dir = self.root / "backup"
        self.network_mount = self.root / "network"
        self.network_dir = self.network_mount / "btrfs-backups"
        self.restore_dir = self.root / "restore"
        for path in (self.snapshot_dir, self.backup_dir, self.network_mount,
                     self.network_dir, self.restore_dir):
            path.mkdir(parents=True, exist_ok=True)
        # Файловая система для эмулятора btrfs (одна ФС = один .shim-fsuuid)
        # UUID должен соответствовать формату btrfs (hex, 20+ символов)
        (self.root / ".shim-fsuuid").write_text(
            "aabbccdd-0000-0000-0000-000000000001")
        self._apply_config()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.cleanup()
        return False

    def _apply_config(self):
        config.SUBVOLUMES = [
            {"name": "root", "source": str(self.root / "src-root")},
            {"name": "home", "source": str(self.root / "src-home")},
        ]
        config.SNAPSHOT_DIR = str(self.snapshot_dir)
        config.LOCAL_BACKUP_DIR = str(self.backup_dir)
        config.NETWORK_MOUNT = str(self.network_mount)
        config.NETWORK_DIRECTORY = "btrfs-backups"
        config.NETWORK_ENABLED = True
        config.AUTO_UPLOAD = True
        config.CHECK_NETWORK_MOUNT = True
        config.EXPECTED_NETWORK_FS_TYPE = None
        config.MIN_NETWORK_FREE_BYTES = 0
        config.COMPRESSION = "zstd"
        config.COMPRESSION_FALLBACK = "gzip"
        config.CRON_COMPRESSION_FALLBACK = True
        config.RETENTION_COUNT = 5
        config.PROCESS_TIMEOUT = 120
        config.GPG_PASSWORD = PASSWORD
        config.LOG_LEVEL = "WARNING"
        config.LOCK_FILE = str(self.root / "bbm.lock")
        NETMOUNT_IS_MOUNTED[0] = True

    def make_subvolume(self, name, payload, marker=b""):
        """Создает эмулируемый Btrfs subvolume с данными."""
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        (path / ".shim-subvolume").touch()
        (path / ".shim-data").write_bytes(payload)
        if marker:
            (path / "marker.bin").write_bytes(marker)
        return path

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


@contextmanager
def sandbox():
    box = Sandbox()
    try:
        yield box
    finally:
        box.cleanup()


# ============================================
# Реестр результатов
# ============================================

class Suite:
    """Сборщик результатов одного тестового модуля."""

    def __init__(self, name):
        self.name = name
        self.results = []  # (status, message)

    def check(self, condition, message):
        status = "PASS" if condition else "FAIL"
        self.results.append((status, message))
        print("  [{}] {}".format(status, message))
        return condition

    def expect_raises(self, func, exception_class, message):
        try:
            func()
        except exception_class:
            return self.check(True, message)
        except Exception as exc:  # noqa: BLE001
            return self.check(False, "{} (неожиданное исключение: {!r})".format(
                message, exc))
        return self.check(False, "{} (исключение не возбуждено)".format(message))

    def skip(self, message):
        self.results.append(("SKIP", message))
        print("  [SKIP] {}".format(message))

    @property
    def fail_count(self):
        return sum(1 for status, _ in self.results if status == "FAIL")

    @property
    def skip_count(self):
        return sum(1 for status, _ in self.results if status == "SKIP")

    def summary(self):
        passed = sum(1 for s, _ in self.results if s == "PASS")
        print("SUMMARY module={} pass={} fail={} skip={}".format(
            self.name, passed, self.fail_count, self.skip_count))
        return self.fail_count


def run_main(module_main):
    """Точка входа тестового модуля: возвращает код завершения."""
    try:
        failures = module_main()
        return 1 if failures else 0
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1


# ============================================
# Помощники
# ============================================

class RunIdGen:
    """Детерминированный генератор run id v1.1 (ts + 6 hex)."""

    def __init__(self, start=0):
        self.counter = start

    def __call__(self, *args, **kwargs):
        self.counter += 1
        return "2026-03-01_{:06d}_{:06x}".format(self.counter, self.counter)


def patch_run_id(gen):
    """Подменяет генератор run id в backup-модуле (восстанавливать не нужно:
    каждый файл тестов — отдельный процесс)."""
    backup_module.runid.new_run_id = lambda *a, **k: gen()


def make_archive(box, subvolume_name, payload, *, compression="gzip",
                 password=PASSWORD, run_id="2026-03-01_000100_000001"):
    """
    Создает настоящий зашифрованный архив из эмулированного снапшота
    (конвейер send|сжатие|gpg — реальный, btrfs — эмулятор).
    """
    src = box.make_subvolume("tmp-src-" + subvolume_name, payload)
    snap = snapshots_module.create_readonly_snapshot(
        src, box.snapshot_dir, subvolume_name, run_id)
    # hostname="" — детерминированные имена без префикса хоста
    # (формат v1.1); имена с префиксом проверяются в test_deletion.py
    out_path = box.backup_dir / compression_module.archive_filename(
        subvolume_name, run_id, compression, hostname="")
    level = 6 if compression == "gzip" else 3
    size = archive.create_archive(
        snap, out_path, compression=compression, level=level,
        gpg_binary=config.GPG_BINARY, password=password)
    sha = verification_module.sha256_file(out_path)
    verification_module.write_checksum_file(out_path, sha)
    meta = metadata_module.new_metadata(
        archive_name=out_path.name, subvolume_name=subvolume_name,
        subvolume_path=str(box.root / ("src-" + subvolume_name)),
        run_id=run_id, compression=compression, encrypted=True,
        snapshot_id=snap.name, sha256=sha, size=size,
        status=metadata_module.STATUS_VERIFIED)
    metadata_module.write_metadata(out_path, meta)
    return out_path, snap, sha


def cleanup_network_files(box):
    """Очищает сетевой каталог между сценариями (это НЕ операция программы)."""
    for entry in box.network_dir.glob("*"):
        try:
            entry.unlink()
        except OSError:
            pass


def confirm_true(_text):
    return True


def confirm_false(_text):
    return False


def silent_reporter():
    """Reporter, не печатающий в stdout (чистый вывод тестов)."""
    class Quiet(backup_module.Reporter):
        def info(self, text):
            pass
    return Quiet()
