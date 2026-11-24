"""Тесты конфигурации (ТЗ 4.1, 20.1). Запуск: python3 tests/test_config.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Suite, run_main  # noqa: E402
import config  # noqa: E402


def main():
    suite = Suite("config")

    # Базовая валидация примера из репозитория
    errors, warnings = config.validate_config()
    suite.check(errors == [], "базовый config.py валиден (errors=[])")
    suite.check(any("CHANGE_ME" in w for w in warnings),
                "заполнитель пароля дает предупреждение")

    # normalize_subvolumes: строки и словари
    items, errors = config.normalize_subvolumes(["/", "/home"])
    suite.check(
        errors == [] and items[0]["name"] == "root" and
        items[0]["source"] == "/" and
        items[0]["snapshot_dir"] is None and
        items[0]["local_backup_dir"] is None,
        "строковая форма: '/' -> имя 'root' (без индивидуальных путей)")
    suite.check(
        items[1]["name"] == "home" and items[1]["source"] == "/home" and
        items[1]["snapshot_dir"] is None,
        "строковая форма: '/home' -> имя 'home'")
    items, errors = config.normalize_subvolumes(
        [{"name": "var", "source": "/var"}])
    suite.check(errors == [] and items[0]["name"] == "var",
                "словарная форма сохраняет имя (совместимость v1.0 v3)")
    items, errors = config.normalize_subvolumes([])
    suite.check(len(errors) == 1, "пустой SUBVOLUMES — ошибка")

    # Относительный путь — ошибка (validate_config, ТЗ 4.1)
    old_sub = config.SUBVOLUMES
    config.SUBVOLUMES = ["relative/path"]
    errors, _ = config.validate_config()
    suite.check(any("абсолютным путем" in e for e in errors),
                "относительный путь — ошибка")

    # Дубликаты имен
    config.SUBVOLUMES = [{"name": "root", "source": "/"},
                         {"name": "root", "source": "/home"}]
    errors, _ = config.validate_config()
    suite.check(any("дублирующееся" in e for e in errors),
                "дублирующееся имя subvolume — ошибка")

    # Запрещенные имена (похожи на метки времени)
    config.SUBVOLUMES = [{"name": "root_2026-01-01_120000", "source": "/"}]
    errors, _ = config.validate_config()
    suite.check(any("временную метку" in e for e in errors),
                "имя-метка времени — ошибка")
    config.SUBVOLUMES = old_sub

    # Некорректное сжатие
    old = config.COMPRESSION
    config.COMPRESSION = "zip"
    errors, _ = config.validate_config()
    suite.check(any("COMPRESSION:" in e for e in errors),
                "недопустимое COMPRESSION — ошибка")
    config.COMPRESSION = old

    old = config.COMPRESSION_FALLBACK
    config.COMPRESSION_FALLBACK = "bzip2"
    errors, _ = config.validate_config()
    suite.check(any("COMPRESSION_FALLBACK:" in e for e in errors),
                "недопустимое COMPRESSION_FALLBACK — ошибка")
    config.COMPRESSION_FALLBACK = old

    # RETENTION_COUNT
    old = config.RETENTION_COUNT
    config.RETENTION_COUNT = 0
    errors, _ = config.validate_config()
    suite.check(any("RETENTION_COUNT" in e for e in errors),
                "RETENTION_COUNT=0 — ошибка")
    config.RETENTION_COUNT = old

    # Поддержка старого имени SNAPSHOT_RETENTION удалена:
    # единственный параметр — RETENTION_COUNT
    suite.check(config.retention_count() == config.RETENTION_COUNT,
                "retention_count() = RETENTION_COUNT (единственное имя)")

    # Конфликт SNAPSHOT_DIR и NETWORK_MOUNT
    old_snap, old_mount = config.SNAPSHOT_DIR, config.NETWORK_MOUNT
    config.SNAPSHOT_DIR = "/same/place"
    config.NETWORK_MOUNT = "/same/place"
    errors, _ = config.validate_config()
    suite.check(any("SNAPSHOT_DIR совпадает с NETWORK_MOUNT" in e
                    for e in errors),
                "SNAPSHOT_DIR == NETWORK_MOUNT — ошибка")
    config.SNAPSHOT_DIR, config.NETWORK_MOUNT = old_snap, old_mount

    # Типы флагов
    old_flag = config.NETWORK_ENABLED
    config.NETWORK_ENABLED = "yes"
    errors, _ = config.validate_config()
    suite.check(any("NETWORK_ENABLED" in e for e in errors),
                "NETWORK_ENABLED строкой — ошибка")
    config.NETWORK_ENABLED = old_flag

    # PROCESS_TIMEOUT
    old_to = config.PROCESS_TIMEOUT
    config.PROCESS_TIMEOUT = 0
    errors, _ = config.validate_config()
    suite.check(any("PROCESS_TIMEOUT" in e for e in errors),
                "PROCESS_TIMEOUT=0 — ошибка")
    config.PROCESS_TIMEOUT = old_to

    # Права доступа к config.py — проверка НЕ зависит от прав файла в
    # окружении: оба файла временно открываются (0644), условие
    # предупреждения создается явно, затем режим восстанавливается.
    _config_path = Path(__file__).resolve().parent.parent / "config.py"
    _backup_cfg_path = Path(__file__).resolve().parent.parent / \
        "backup_config.py"
    _saved_modes = {}
    for _p in (_config_path, _backup_cfg_path):
        try:
            _saved_modes[_p] = _p.stat().st_mode & 0o777
            _p.chmod(0o644)
        except OSError:
            pass
    try:
        warnings = config.check_config_permissions()
        suite.check(isinstance(warnings, list),
                    "check_config_permissions возвращает список предупреждений")
        suite.check(any(w.startswith("backup_config.py ") for w in warnings),
                    "права backup_config.py проверяются (там хранится пароль)")
        # 1.4: в предупреждении — готовая команда chmod 600 и пояснение
        suite.check(all("chmod 600" in w for w in warnings),
                    "предупреждение о правах содержит готовую команду chmod 600")
        config_warnings = [w for w in warnings if w.startswith("config.py ")]
        suite.check(config_warnings and
                    "пароля не содержит" in config_warnings[0],
                    "предупреждение для config.py поясняет, что пароль "
                    "хранится не в нем (вопрос пользователя 1.4)")
        # 600 -> предупреждений нет
        for _p in (_config_path, _backup_cfg_path):
            try:
                _p.chmod(0o600)
            except OSError:
                pass
        suite.check(config.check_config_permissions() == [],
                    "при правах 600 предупреждений нет")
    finally:
        for _p, _mode in _saved_modes.items():
            try:
                _p.chmod(_mode)
            except OSError:
                pass

    # === Разделение настроек и валидации (замечание пользователя 1.2) ===
    import backup_config
    suite.check(config.SUBVOLUMES is backup_config.SUBVOLUMES and
                config.GPG_PASSWORD is backup_config.GPG_PASSWORD and
                config.COMPRESSION is backup_config.COMPRESSION,
                "config.py реэкспортирует настройки из backup_config.py")
    suite.check(not hasattr(backup_config, "validate_config"),
                "backup_config.py содержит только настройки (без валидации)")
    # Редактирование настройки в backup_config видно через config
    old_compression = backup_config.COMPRESSION
    backup_config.COMPRESSION = "gzip"
    import importlib
    import config as config_module
    importlib.reload(config_module)
    suite.check(config_module.COMPRESSION == "gzip",
                "правка backup_config.py подхватывается config.py")
    backup_config.COMPRESSION = old_compression
    importlib.reload(config_module)

    # Локальный каталог архивов
    suite.check(config.local_backup_dir("/snaps") == "/snaps/backup",
                "LOCAL_BACKUP_DIR=None -> SNAPSHOT_DIR/backup")
    config.LOCAL_BACKUP_DIR = "/custom/backups"
    suite.check(config.local_backup_dir("/snaps") == "/custom/backups",
                "LOCAL_BACKUP_DIR задан явно")
    config.LOCAL_BACKUP_DIR = None

    return suite.summary()


if __name__ == "__main__":
    sys.exit(run_main(main))
