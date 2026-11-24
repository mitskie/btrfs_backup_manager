# ============================================
# BTRFS BACKUP MANAGER — НАСТРОЙКИ ПРИЕМОЧНОГО ТЕСТА (zstd)
# ============================================
# Тестовая среда: Btrfs на loop-устройстве.
# НЕ ИСПОЛЬЗОВАТЬ для рабочих данных: источник — subvolume на тестовой
# файловой системе /mnt/btrfs-test (loop0).
#
# Пароль задается при запуске скрытым вводом (GPG_PASSWORD — заполнитель).
# Сетевое хранилище отключено: тест выполняется полностью локально.

SUBVOLUMES = [
    {
        "name": "btrfs-test",
        "source": "/mnt/btrfs-test/source",
        "snapshot_dir": "/mnt/btrfs-test/.snapshots",
        "local_backup_dir": "/mnt/btrfs-test/.snapshots/backup",
    },
]

SNAPSHOT_DIR = "/mnt/btrfs-test/.snapshots"
LOCAL_BACKUP_DIR = "/mnt/btrfs-test/.snapshots/backup"

# Настройки группы 10 сценария приемки: zstd. Проверяется только при
# установленном zstd (если утилиты нет — группа помечается
# «ПРОПУЩЕНО», не «успешно»).
COMPRESSION = "zstd"
COMPRESSION_FALLBACK = "gzip"
CRON_COMPRESSION_FALLBACK = True

GZIP_LEVEL = 6
ZSTD_LEVEL = 3

GPG_BINARY = "/usr/bin/gpg"
GPG_PASSWORD = "CHANGE_ME"

NETWORK_ENABLED = False
NETWORK_MOUNT = None
NETWORK_DIRECTORY = "btrfs-backups"
AUTO_UPLOAD = False
CHECK_NETWORK_MOUNT = True
EXPECTED_NETWORK_FS_TYPE = None
MIN_NETWORK_FREE_BYTES = 0

RETENTION_COUNT = 5
PROCESS_TIMEOUT = 3600

# Отдельные журнал и блокировка: тест не мешает рабочим заданиям.
LOG_FILE = "/tmp/bbm-acceptance-test.log"
LOG_LEVEL = "INFO"
LOCK_FILE = "/run/lock/bbm-acceptance-test.lock"
