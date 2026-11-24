"""
Оркестрация резервного копирования и повторной отправки.

Главное отличие версии 1.1: программа сначала ГАРАНТИРОВАННО
создает и проверяет ЛОКАЛЬНУЮ резервную копию, и только затем пытается
отправить ее в сеть. Сетевая шара не является обязательным условием:
отсутствие сети — это статус upload-pending, а не ошибка резервного
копирования.

Интерфейсный слой (CLI/TUI) не знает деталей операций: операции сообщают
прогресс через Reporter.
"""

import re
import time
from pathlib import Path

from . import (
    BtrfsBackupError, ConfigError, OperationAborted, OperationCancelled,
    StorageError,
)
from . import archive as archive_module
from . import compression as compression_module
from . import encryption as encryption_module
from . import environment, locking, metadata, runid, snapshots, verification
from . import pathcheck as pathcheck_module
from .logging_setup import get_logger
from .network import (
    check_network_free_space, check_network_mount, cleanup_network_tmp_files,
    copy_to_network_tmp, delete_local_archive, ensure_network_dir,
    promote_network_archive, recheck_network_mount, write_network_metadata,
)
from .process import human_size, set_pipeline_timeout
from .report import RunReport, count_pending_archives, write_last_backup_state
from .retention import apply_retention, retention_safety_check

log = get_logger("backup")

# Тот же шаблон, что в config._NAME_RE (валидация имен subvolume,
# переданных интерфейсом поверх конфигурации, v1.3.1).
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TS_LIKE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}_\d{6}(?:_\d{1,6})?(?:_[0-9a-fA-F]{6})?"
)


# ============================================
# Reporter: канал прогресса для интерфейсного слоя
# ============================================

class Reporter:
    """Интерфейс канала прогресса (CLI печатает, TUI рисует экран)."""

    def stage(self, text):
        pass

    def info(self, text):
        pass

    def warn(self, text):
        pass

    def error(self, text):
        pass

    def done(self, text):
        pass


class ConsoleReporter(Reporter):
    """Печать прогресса в консоль + журнал."""

    def stage(self, text):
        print()
        print(text)
        log.info(text)

    def info(self, text):
        print("  {}".format(text))
        log.info(text)

    def warn(self, text):
        print("  [!] " + text)
        log.warning(text)

    def error(self, text):
        print("  [ОШИБКА] " + text)
        log.error(text)

    def done(self, text):
        print(text)
        log.info(text)


def _default(reporter):
    return reporter if reporter is not None else ConsoleReporter()


def _subvolume_items(config, subvolume_filter=None):
    """
    Нормализованный список subvolume; при заданном фильтре — только
    совпавшие по имени или пути.
    Элементы содержат индивидуальные snapshot_dir / local_backup_dir; разрешение до фактических каталогов выполняет
    pathcheck.check_source.
    """
    items, errors = config.normalize_subvolumes(config.SUBVOLUMES)
    if errors:
        raise ConfigError(
            "Конфигурация SUBVOLUMES некорректна: " + "; ".join(errors)
        )
    if not subvolume_filter:
        return items
    selected = []
    for item in items:
        for token in subvolume_filter:
            wanted = str(token).rstrip("/") or "/"
            if item["name"] == token or item["source"].rstrip("/") == wanted:
                selected.append(item)
                break
    missing = [t for t, _ok in zip(
        subvolume_filter,
        [any(item["name"] == t or item["source"].rstrip("/") ==
             (str(t).rstrip("/") or "/") for item in items)
         for t in subvolume_filter]) if not _ok]
    if missing:
        raise ConfigError(
            "Subvolume не найдены в конфигурации: {}. Доступные: {}.".format(
                ", ".join(missing),
                ", ".join("{} ({})".format(i["name"], i["source"])
                          for i in items))
        )
    return selected


def _local_backup_dirs(config, items=None, *, include_config=True):
    """
    Каталоги локальных архивов операции без дубликатов.

    Для CLI/cron include_config=True: используются каталоги профиля.
    Для TUI items_override include_config=False: только каталоги, выбранные
    пользователем для текущей операции; глобальные пути профиля не примешиваются.
    """
    result = []
    seen = set()

    def add(path):
        if not path:
            return
        key = str(path)
        if key not in seen:
            seen.add(key)
            result.append(key)

    for item in items or []:
        add(item.get("local_backup_dir"))
    if include_config:
        try:
            for path in config.local_backup_dirs():
                add(path)
        except AttributeError:
            add(config.local_backup_dir(config.SNAPSHOT_DIR))
    return result


def _check_network(config, reporter, non_interactive, confirm_fn):
    """
    Проверяет доступность сетевой шары (необязательной, ТЗ 7.1).

    Проверка РЕАЛЬНОГО монтирования ОБЯЗАТЕЛЬНА и не зависит от
    CHECK_NETWORK_MOUNT: обычный локальный
    каталог в NETWORK_MOUNT никогда не считается сетевым хранилищем.
    CHECK_NETWORK_MOUNT с v1.4.4 управляет только ДИАГНОСТИЧЕСКОЙ
    пробой записи (см. check_network_mount).

    v1.4.6: NETWORK_MOUNT не задан (профиль без сетевого хранилища) —
    сетевые операции пропускаются, выполняется локальный backup.

    Возвращает Path сетевого каталога или None.
    Отсутствие сети — НЕ критическая ошибка для локального backup.
    """
    if not getattr(config, "NETWORK_ENABLED", True):
        reporter.info("Сетевое хранилище отключено (NETWORK_ENABLED=false)")
        return None
    network_mount = getattr(config, "NETWORK_MOUNT", None)
    if not network_mount:
        reporter.info(
            "Сетевое хранилище не настроено (NETWORK_MOUNT не задан) — "
            "выполняется локальное резервное копирование")
        log.info("Сетевое хранилище не настроено: NETWORK_MOUNT не задан")
        return None
    try:
        check_network_mount(
            config.NETWORK_MOUNT,
            expected_fs_type=getattr(config, "EXPECTED_NETWORK_FS_TYPE", None),
            check=bool(getattr(config, "CHECK_NETWORK_MOUNT", True)),
        )
        net_dir = ensure_network_dir(config.NETWORK_MOUNT, config.NETWORK_DIRECTORY)
        check_network_free_space(
            net_dir, getattr(config, "MIN_NETWORK_FREE_BYTES", 0))
        return net_dir
    except StorageError as exc:
        log.info("Сетевое хранилище недоступно: %s", exc)
        if non_interactive:
            # ТЗ 7.1: в cron подтверждение не запрашивается — локальный backup
            reporter.warn(
                "Сетевое хранилище не подключено ({}). Выполняется локальное "
                "резервное копирование; архивы получат статус "
                "upload-pending.".format(exc)
            )
            return None
        text = ("Сетевое хранилище не подключено.\n\n"
                "Можно создать локальную резервную копию.\n"
                "После подключения сетевого хранилища её можно будет "
                "отправить.\n\n"
                "Продолжить локальное резервное копирование?")
        if confirm_fn is None or not confirm_fn(text):
            raise OperationCancelled(
                "Операция отменена пользователем: сетевое хранилище "
                "недоступно, локальное резервное копирование отклонено."
            )
        reporter.warn(
            "Сетевое хранилище не подключено. Создается локальная "
            "резервная копия (статус архивов: upload-pending)."
        )
        return None


# ============================================
# Полное резервное копирование
# ============================================

def _validate_override_items(items_override):
    """
    Проверяет список subvolume, переданный интерфейсом ПОВЕРХ конфигурации
    (выбор разделов в TUI, включая обнаруженные в системе;
    v1.4.6: элементы могут задавать собственные snapshot_dir и
    local_backup_dir — ТЗ 3.3).
    Возвращает нормализованный список [{name, source, snapshot_dir,
    local_backup_dir}]; нарушение — ConfigError ДО начала каких-либо
    операций.
    """
    if items_override is None:
        return None
    if not isinstance(items_override, (list, tuple)):
        raise ConfigError(
            "items_override: ожидался список subvolume, получено {}".format(
                type(items_override).__name__))
    items = []
    seen = set()
    for index, item in enumerate(items_override, 1):
        if not isinstance(item, dict):
            raise ConfigError(
                "items_override[{}]: элемент должен быть словарем "
                "{name, source}".format(index))
        name = item.get("name")
        source = item.get("source")
        if not isinstance(name, str) or not _NAME_RE.match(name or ""):
            raise ConfigError(
                "items_override[{}]: недопустимое имя '{}' (разрешены "
                "латиница, цифры, '.', '_', '-', длина 1-64)".format(
                    index, name))
        if _TS_LIKE_RE.search(name):
            raise ConfigError(
                "items_override[{}]: имя '{}' не должно напоминать "
                "временную метку".format(index, name))
        if not isinstance(source, str) or not source.startswith("/"):
            raise ConfigError(
                "items_override[{}] ({}): source должен быть абсолютным "
                "путем".format(index, name))
        entry = {"name": name, "source": source,
                 "snapshot_dir": None, "local_backup_dir": None}
        for key in ("snapshot_dir", "local_backup_dir"):
            value = item.get(key)
            if value is not None:
                if not isinstance(value, str) or not value.startswith("/"):
                    raise ConfigError(
                        "items_override[{}] ({}): '{}' должен быть "
                        "абсолютным путем".format(index, name, key))
                entry[key] = value
        key = (name, source.rstrip("/") or "/")
        if key in seen:
            continue
        seen.add(key)
        items.append(entry)
    if not items:
        # ТЗ 4: нельзя запускать резервное копирование с пустым списком
        raise ConfigError(
            "Все источники исключены. Нет источников для резервного "
            "копирования.")
    return items


def run_backup(config, *, non_interactive, confirm_fn=None, reporter=None,
               abort_check=None, password=None, lock=True,
               subvolume_filter=None, items_override=None,
               compression_override=None, auto_upload_override=None):
    """
    Полное резервное копирование: снапшоты -> локальные архивы -> проверка
    -> (необязательно) сетевая отправка -> retention.

    items_override: явный список subvolume от интерфейса
    (TUI: выбор разделов, включая обнаруженные в системе; v1.4.6 — с
    индивидуальными snapshot_dir / local_backup_dir). Задан —
    используется ВМЕСТО конфигурации; subvolume_filter при этом
    игнорируется, а отсутствие источников в конфигурации не является
    ошибкой (интерактивный режим, ТЗ 2.1).

    compression_override: алгоритм сжатия, выбранный
    пользователем в интерактивном режиме ("gzip" | "zstd"). Задан —
    используется вместо resolve_compression (интерфейс уже проверил
    доступность утилиты и fallback). Неинтерактивные запуски параметр
    игнорируют и работают строго по конфигурации.

    Возвращает RunReport. Локально успешный backup при недоступной сети —
    успешная операция, архивы получают статус upload-pending.
    """
    reporter = _default(reporter)
    report = RunReport()
    started = time.monotonic()
    mode = "неинтерактивный (cron)" if non_interactive else "интерактивный"
    log.info("=" * 60)
    log.info("Запуск резервного копирования; режим: %s; профиль: %s",
             mode, getattr(config, "profile_name", "основной"))
    items_override = _validate_override_items(items_override)
    if lock:
        locking.acquire_lock(
            getattr(config, "LOCK_FILE", None) or
            getattr(config, "default_lock_file", lambda: None)() or
            "/run/lock/btrfs-backup-manager.lock",
            Path(__file__).resolve().parent.parent / "logs")
    try:
        _run_backup_inner(config, report, reporter, non_interactive,
                          confirm_fn, abort_check, password,
                          subvolume_filter=subvolume_filter,
                          items_override=items_override,
                          compression_override=compression_override,
                          auto_upload_override=auto_upload_override)
        write_last_backup_state(True, "локальный backup выполнен")
    except BaseException as exc:
        report.failures += 1
        report.pending_local = _count_pending(config, items_override)
        report.log_only(started)
        write_last_backup_state(False, str(exc)[:200])
        raise
    report.pending_local = _count_pending(config, items_override)
    report.total_seconds = time.monotonic() - started
    # в TUI отчет передается интерфейсу (без прямого print(),
    # который портил нижнюю часть экрана после операции).
    report.emit(started, reporter)
    return report


def _count_pending(config, items_override=None):
    """Архивы upload-pending по ВСЕМ каталогам архивов операции."""
    try:
        dirs = _local_backup_dirs(
            config, items_override, include_config=items_override is None)
    except Exception:  # noqa: BLE001
        dirs = []
        if items_override is None:
            try:
                fallback_dir = config.local_backup_dir(config.SNAPSHOT_DIR)
            except Exception:  # noqa: BLE001
                fallback_dir = None
            if fallback_dir:
                dirs = [fallback_dir]
    return sum(count_pending_archives(path) for path in dirs)


def _unique_dirs(paths):
    """Список путей без дубликатов, порядок сохранен."""
    result = []
    seen = set()
    for path in paths:
        if not path:
            continue
        key = str(path)
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _retention_groups(config, items, *, include_config=True):
    """
    Группы retention: [(каталог_снапшотов, [имена])].

    CLI/cron обслуживает профиль целиком. TUI передает include_config=False:
    retention ограничен только источниками и каталогами, выбранными
    пользователем для текущей операции.
    """
    merged = {}

    def add(item):
        sdir = item.get("snapshot_dir") or \
            getattr(config, "SNAPSHOT_DIR", None)
        if not sdir:
            return
        key = str(sdir)
        bucket = merged.setdefault(key, [])
        if item["name"] not in bucket:
            bucket.append(item["name"])

    if include_config:
        try:
            for item in config.normalize_subvolumes(config.SUBVOLUMES)[0]:
                add(item)
        except Exception:  # noqa: BLE001
            log.debug("retention: конфигурационные источники не добавлены")
    for item in items or []:
        add(item)
    return [(sdir, names) for sdir, names in merged.items()]


def _run_backup_inner(config, report, reporter, non_interactive, confirm_fn,
                      abort_check, password, subvolume_filter=None,
                      items_override=None, compression_override=None,
                      auto_upload_override=None):
    set_pipeline_timeout(getattr(config, "PROCESS_TIMEOUT", 3600))
    stage_env_started = time.monotonic()
    if items_override is not None:
        items = items_override
    else:
        items = _subvolume_items(config, subvolume_filter)
    # ТЗ 4: резервное копирование с пустым списком источников не запускается
    if not items:
        raise ConfigError(
            "Все источники исключены. Нет источников для резервного "
            "копирования.")
    log.info(
        "Subvolumes: %s",
        ", ".join("{} -> {}".format(i["name"], i["source"]) for i in items),
    )
    log.info("Профиль: %s", getattr(config, "profile_name", "основной"))

    # [1/7] Окружение и конфигурация
    reporter.stage("[1/7] Проверка окружения и конфигурации")
    environment.require_root()
    # Интерфейсный выбор (items_override) работает и без полного конфига:
    # отсутствие SUBVOLUMES/SNAPSHOT_DIR в профиле не ошибка
    strict = items_override is None
    if hasattr(config, "validate_or_raise"):
        config.validate_or_raise(strict=strict)
    else:
        environment.validate_or_raise(config, strict=strict)
    environment.check_utilities(need_btrfs=True, need_gpg=True,
                                gpg_binary=config.GPG_BINARY,
                                gpg_check=encryption_module.check_gpg)

    # Единая проверка расположения ВСЕХ источников (один
    # механизм для TUI, CLI и backup). Каталоги создаются здесь — до
    # начала каких-либо снапшотов. Источник с ошибками — исключается
    # из операции целиком (fail-closed); при ошибках ХОТЯ БЫ одного
    # источника операция не начинается (частичное резервирование из-за
    # несовместимого расположения недопустимо, ТЗ 4.1).
    checks = pathcheck_module.check_sources(items, config, create_dirs=True)
    problems = [check for check in checks if not check["ok"]]
    if problems:
        details = "\n".join(
            "  - {} ({}): {}".format(
                check["name"], check["source"], "; ".join(check["errors"]))
            for check in problems)
        raise ConfigError(
            "Расположение источников не прошло проверку, операция "
            "прервана ДО создания снапшотов:\n{}".format(details))
    # Фактические каталоги проверки записываются в элементы
    # индивидуальные пути имеют приоритет над глобальными)
    for item, check in zip(items, checks):
        item["snapshot_dir"] = check["snapshot_dir"]
        item["local_backup_dir"] = check["local_backup_dir"]
    for warning in [w for check in checks for w in check["warnings"]]:
        reporter.warn(warning)
    backup_dirs = _local_backup_dirs(
        config, items, include_config=items_override is None)
    snapshot_dirs = _unique_dirs(item["snapshot_dir"] for item in items)
    log.info("Каталоги снапшотов: %s", ", ".join(snapshot_dirs))
    log.info("Каталоги архивов: %s", ", ".join(backup_dirs))

    # Сжатие с fallback. В интерактивном режиме алгоритм может быть
    # выбран пользователем заранее — тогда он передается
    # явно и здесь только проверяется.
    compression = compression_module.resolve_compression(
        config, non_interactive=non_interactive, confirm_fn=confirm_fn,
        override=compression_override)
    report.compression_used = compression
    level = config.GZIP_LEVEL if compression == "gzip" else config.ZSTD_LEVEL
    reporter.info("Сжатие: {} (уровень {})".format(compression, level))

    # Пароль GPG
    if password is None:
        password = encryption_module.resolve_backup_password(
            config.GPG_PASSWORD, non_interactive)
    log.info("Пароль GPG получен (значение не логируется)")

    # Сетевая шара (необязательная, ТЗ 7.1)
    net_dir = _check_network(config, reporter, non_interactive, confirm_fn)
    report.timings["environment"] = time.monotonic() - stage_env_started

    # [2/7] Снапшоты
    stage_snapshots_started = time.monotonic()
    reporter.stage("[2/7] Создание read-only снапшотов")
    run_id = runid.new_run_id(
        snapshot_dirs, backup_dirs, net_dir or "/nonexistent-network",
        [item["name"] for item in items],
    )
    log.info("Идентификатор резервной копии: %s", run_id)
    reporter.info("Идентификатор резервной копии: {}".format(run_id))
    created = []
    for item in items:
        snap = snapshots.create_readonly_snapshot(
            item["source"], item["snapshot_dir"], item["name"], run_id
        )
        created.append((item, snap))
        report.snapshots_created += 1
        reporter.info("Создан read-only снапшот: {} (в {})".format(
            snap.name, item["snapshot_dir"]))
    report.timings["snapshots"] = time.monotonic() - stage_snapshots_started

    # [3/7] Архивы (конвейер send|сжатие|gpg, ТЗ 5.2 пп.7-11)
    reporter.stage("[3/7] Создание зашифрованных архивов ({} — фактический "
                   "алгоритм)".format(compression))
    archives = []
    stage3_started = time.monotonic()
    stage3_bytes = 0
    profile_name = getattr(config, "profile_name", None)
    for item, snap in created:
        out_path = Path(item["local_backup_dir"]) / \
            compression_module.archive_filename(
                item["name"], run_id, compression
            )
        archive_started = time.monotonic()
        size = archive_module.create_archive(
            snap, out_path,
            compression=compression, level=level,
            gpg_binary=config.GPG_BINARY, password=password,
            abort_check=abort_check,
        )
        archive_elapsed = time.monotonic() - archive_started
        stage3_bytes += size
        # Наблюдаемость производительности: этап/время/скорость видны
        # в журнале и TUI.
        log.info(
            "Архив создан за %.1f с (%s/с): %s",
            archive_elapsed, human_size(size / max(archive_elapsed, 0.001)),
            out_path.name,
        )
        meta = metadata.new_metadata(
            archive_name=out_path.name,
            subvolume_name=item["name"],
            subvolume_path=item["source"],
            run_id=run_id,
            compression=compression,
            encrypted=True,
            snapshot_id=snap.name,
            size=size,
            profile=profile_name,
        )
        metadata.write_metadata(out_path, meta)
        archives.append((item, snap, out_path, size, meta))
        report.archives_created += 1
        reporter.info("Архив: {} ({})".format(out_path.name, human_size(size)))
        reporter.info("    время: {}; скорость: {}/с".format(
            _format_duration(archive_elapsed),
            human_size(size / max(archive_elapsed, 0.001)),
        ))
    stage3_elapsed = time.monotonic() - stage3_started
    report.timings["archives"] = stage3_elapsed
    reporter.info("Этап [3/7] завершен за {}; суммарно сжато и зашифровано "
                  "{}".format(_format_duration(stage3_elapsed),
                              human_size(stage3_bytes)))

    # [4/7] Проверка целостности
    reporter.stage("[4/7] Проверка архивов (расшифрование, сжатый поток, "
                   "SHA-256)")
    stage4_started = time.monotonic()
    verified = []
    for item, snap, path, size, meta in archives:
        verification.test_decrypted_stream(
            path, gpg_binary=config.GPG_BINARY, password=password,
            compression=compression, abort_check=abort_check,
        )
        sha = verification.sha256_file(path)
        # SHA-256 хранится в JSON-метаданных архива (замечание
        # пользователя: отдельный файл .sha256 избыточен; JSON —
        # единственный sidecar-файл с версии 1.3).
        meta = metadata.set_status(meta, metadata.STATUS_VERIFIED)
        meta["sha256"] = sha
        metadata.write_metadata(path, meta)
        verified.append((item, snap, path, size, sha, meta))
        log.info(
            "Архив проверен: %s (subvolume: %s; сжатие: %s; размер: %s; "
            "шифрование: AES256; SHA-256: %s)",
            path.name, item["name"], compression, human_size(size), sha,
        )
        reporter.info("Проверен: {} (SHA-256 подтвержден)".format(path.name))
    report.timings["verify"] = time.monotonic() - stage4_started
    reporter.info("Этап [4/7] завершен за {}.".format(
        _format_duration(report.timings["verify"])))

    # [5/7] Сетевая отправка (необязательная, ТЗ 7)
    stage5_started = time.monotonic()
    reporter.stage("[5/7] Сетевая отправка")
    auto_upload = (getattr(config, "AUTO_UPLOAD", True)
                   if auto_upload_override is None
                   else bool(auto_upload_override))
    if net_dir is None or not auto_upload:
        for item, snap, path, size, sha, meta in verified:
            if net_dir is None:
                meta = metadata.set_status(meta, metadata.STATUS_UPLOAD_PENDING)
            else:
                meta = metadata.set_status(meta, metadata.STATUS_UPLOAD_PENDING)
            metadata.write_metadata(path, meta)
        if net_dir is None:
            reporter.warn(
                "Архивы сохранены локально со статусом upload-pending; "
                "отправка будет выполнена командой upload-pending после "
                "подключения сетевого хранилища."
            )
        else:
            reporter.info(
                "Автоматическая отправка отключена для этой операции: "
                "архивы ожидают отправки (upload-pending)."
            )
        report.local_only = len(verified)
    else:
        _upload_archives(config, verified, net_dir, report, reporter,
                         abort_check)
    report.timings["upload"] = time.monotonic() - stage5_started

    # [6/7] Retention снапшотов
    stage6_started = time.monotonic()
    reporter.stage("[6/7] Retention снапшотов")
    # ТЗ 4.3 исправление №2: снапшоты текущей операции и исходные рабочие
    # subvolume защищены от удаления даже при ошибке отбора кандидатов.
    _run_retention(
        config, net_dir, report, reporter, compression,
        groups=_retention_groups(
            config, items, include_config=items_override is None),
        protected_paths=[str(snap) for _item, snap in created] +
                        [item["source"] for item in items],
    )
    report.timings["retention"] = time.monotonic() - stage6_started

    # [7/7] Завершение
    reporter.stage("[7/7] Завершение")
    total_size = sum(size for _i, _s, _p, size, _sha, _m in verified)
    log.info(
        "Резервное копирование завершено: архивов %d (%s); сжатие: %s",
        len(verified), human_size(total_size), compression,
    )
    reporter.done("Резервное копирование завершено.")


def _format_duration(seconds):
    """Длительность в человекочитаемом виде ("3 мин 40 с")."""
    seconds = int(max(0, seconds))
    if seconds >= 3600:
        return "{} ч {:02d} мин {:02d} с".format(
            seconds // 3600, (seconds % 3600) // 60, seconds % 60)
    if seconds >= 60:
        return "{} мин {:02d} с".format(seconds // 60, seconds % 60)
    return "{} с".format(seconds)


def _upload_archives(config, verified, net_dir, report, reporter,
                     abort_check=None):
    """
    Отправка проверенных архивов на сетевую шару.

    На каждом опасном этапе выполняется ОБЯЗАТЕЛЬНАЯ повторная проверка
    монтирования: перед копированием, перед
    финализацией, перед записью метаданных и непосредственно перед
    удалением локального архива. Потеря сети на любом этапе: отправка
    считается неуспешной, локальный архив сохраняется, статус
    upload-failed.

    Локальный архив удаляется только при одновременном выполнении всех
    условий: шара смонтирована, сетевой архив
    записан и проверен (размер + SHA-256), финальное имя присвоено,
    метаданные записаны атомарно и подтверждены перечитыванием.
    Ошибка отправки одного архива: локальная копия сохраняется,
    статус upload-failed, остальные архивы отправляются. Если хотя бы
    одна отправка не удалась — операция завершается StorageError (код 7).
    """
    failures = 0
    for item, snap, path, size, sha, meta in verified:
        if metadata.read_metadata(path) and metadata.read_metadata(path).get(
                "upload_status") == "uploaded" and \
                not path.exists():
            continue
        tmp_path = None
        try:
            # [1] Повторная проверка перед отправкой
            recheck_network_mount(
                config.NETWORK_MOUNT,
                expected_fs_type=getattr(config,
                                         "EXPECTED_NETWORK_FS_TYPE", None),
            )
            meta = metadata.set_status(meta, metadata.STATUS_UPLOADING)
            metadata.write_metadata(path, meta)
            tmp_path, actual_sha = copy_to_network_tmp(path, net_dir, sha)
            # [2] Повторная проверка перед финализацией сетевого файла
            recheck_network_mount(
                config.NETWORK_MOUNT,
                expected_fs_type=getattr(config,
                                         "EXPECTED_NETWORK_FS_TYPE", None),
            )
            final_path = promote_network_archive(tmp_path, actual_sha)
            tmp_path = None
            # [3] Повторная проверка перед записью метаданных
            recheck_network_mount(
                config.NETWORK_MOUNT,
                expected_fs_type=getattr(config,
                                         "EXPECTED_NETWORK_FS_TYPE", None),
            )
            if not write_network_metadata(net_dir, meta, actual_sha):
                # ТЗ 5.2 исправление №3: без подтвержденных метаданных
                # удаление локального архива ЗАПРЕЩЕНО, отправка не
                # считается завершенной; архив доступен для повторной
                # отправки (upload-pending).
                raise StorageError(
                    "Сетевые метаданные '{}' не записаны или не "
                    "подтверждены повторным чтением; ЛОКАЛЬНЫЙ АРХИВ "
                    "СОХРАНЕН, отправка не считается завершенной "
                    "(повторная отправка — upload-pending).".format(path.name)
                )
            # [4] Повторная проверка непосредственно перед удалением
            # локального архива
            recheck_network_mount(
                config.NETWORK_MOUNT,
                expected_fs_type=getattr(config,
                                         "EXPECTED_NETWORK_FS_TYPE", None),
            )
            # ТЗ 9.4: локальный архив удаляется только после подтвержденной
            # отправки (SHA-256 сетевой копии перепроверен в promote,
            # метаданные записаны и подтверждены).
            delete_local_archive(path)
            report.archives_uploaded += 1
            log.info("Сетевая копия подтверждена: %s (subvolume: %s)",
                     final_path.name, item["name"])
            reporter.info("Отправлен и подтвержден: {}".format(final_path.name))
        except BtrfsBackupError as exc:
            failures += 1
            report.failures += 1
            if tmp_path is not None:
                try:
                    Path(tmp_path).unlink()
                except OSError:
                    pass
            meta = metadata.set_status(meta, metadata.STATUS_UPLOAD_FAILED)
            metadata.write_metadata(path, meta)
            reporter.error(
                "Ошибка отправки {}: {}. ЛОКАЛЬНЫЙ АРХИВ СОХРАНЕН.".format(
                    path.name, exc)
            )
            log.error("Отправка %s не удалась: %s", path.name, exc)
    if failures:
        raise StorageError(
            "Не все архивы отправлены на сетевое хранилище: ошибок {}. "
            "Локальные копии сохранены; повторная отправка — "
            "upload-pending.".format(failures)
        )


def _run_retention(config, net_dir, report, reporter, compression,
                   groups, protected_paths=()):
    """
    Retention снапшотов: только при ДОСТУПНОЙ и ПОДТВЕРЖДЕННОЙ
    сетевой шаре; монтирование проверяется обязательно (независимо от
    CHECK_NETWORK_MOUNT). Недоступная сеть — retention отменяется,
    ни один снапшот не удаляется.

    ТЗ 6.2 исправление №4: алгоритм сжатия НЕ передается в проверку
    сетевой копии — retention должен находить архивы gzip и zstd
    независимо от текущего COMPRESSION (алгоритм определяется
    индивидуально для каждого архива).

    protected_paths: пути, удаление которых запрещено
    (исходные рабочие subvolume и снапшоты текущей операции).

    groups: [(каталог_снапшотов, [имена subvolume])] — retention
    выполняется по каждой группе независимо (индивидуальные каталоги
    снапшотов источников, ТЗ 3.3).
    """
    if net_dir is None:
        reporter.warn(
            "Retention ПРОПУЩЕН: сетевое хранилище недоступно, удаление "
            "снапшотов без подтвержденной удаленной копии запрещено."
        )
        log.info("Retention пропущен: сеть недоступна")
        return
    try:
        retention_safety_check(config, net_dir)
    except StorageError as exc:
        message = ("Retention ОТМЕНЕН: обязательная проверка сетевого "
                   "хранилища не пройдена ({}). Ни один снапшот не "
                   "удалялся.".format(exc))
        log.error(message)
        reporter.error(message)
        report.failures += 1
        return
    total_deleted = 0
    for snapshot_dir, retention_names in groups or []:
        deleted = apply_retention(
            snapshot_dir,
            retention_names,
            config.retention_count(),
            net_dir,
            delete=True,
            compression=None,
            protected_paths=protected_paths,
        )
        for deleted_names in deleted.values():
            for name in deleted_names:
                reporter.info("Retention: удален снапшот {}".format(name))
                log.info("Retention: удален снапшот %s", name)
        total_deleted += sum(len(v) for v in deleted.values())
    report.snapshots_deleted = total_deleted
    if not total_deleted:
        reporter.info("Старые снапшоты для удаления не найдены.")
    log.info("Retention: удалено снапшотов %d", total_deleted)


# ============================================
# Повторная отправка upload-pending

# ============================================
# Повторная отправка upload-pending
# ============================================

def upload_pending(config, *, non_interactive=True, reporter=None,
                   abort_check=None, password=None, lock=True,
                   archive_path=None, verify_integrity=True):
    """
    Отправка локальных архивов, ожидающих отправки.

    - не создает снапшотов и архивов, не выполняет retention;
    - не удаляет сетевые архивы;
    - монтирование шары ОБЯЗАТЕЛЬНО (проверка не зависит от
      CHECK_NETWORK_MOUNT);
    - для каждого архива: SHA-256; при verify_integrity=True также
      расшифрование и тест потока; сжатие определяется по расширению архива;
    - идемпотентно: уже подтвержденные архивы не отправляются повторно;
      повторный запуск не создает поврежденных дубликатов;
    - при ошибке локальный архив сохраняется.
    """
    reporter = _default(reporter)
    started = time.monotonic()
    report = RunReport()
    mode = "неинтерактивный" if non_interactive else "интерактивный"
    log.info("Запуск отправки локальных архивов (upload-pending); режим: %s",
             mode)
    if lock:
        locking.acquire_lock(
            getattr(config, "LOCK_FILE", None) or
            getattr(config, "default_lock_file", lambda: None)() or
            "/run/lock/btrfs-backup-manager.lock",
            Path(__file__).resolve().parent.parent / "logs")
    try:
        set_pipeline_timeout(getattr(config, "PROCESS_TIMEOUT", 3600))
        environment.require_root()
        environment.validate_or_raise(config)
        if not getattr(config, "NETWORK_ENABLED", True):
            raise StorageError(
                "Сетевое хранилище отключено (NETWORK_ENABLED=false); "
                "отправка невозможна. Локальные архивы сохранены.")
        network_mount = getattr(config, "NETWORK_MOUNT", None)
        if not network_mount:
            raise StorageError(
                "Сетевое хранилище не настроено (NETWORK_MOUNT не задан); "
                "отправка невозможна. Локальные архивы сохранены.")
        if verify_integrity:
            environment.check_utilities(need_gpg=True,
                                        gpg_binary=config.GPG_BINARY,
                                        gpg_check=encryption_module.check_gpg)
        backup_dirs = _local_backup_dirs(config)
        for path in backup_dirs:
            environment.ensure_local_dirs(path)
        # Отправка выполняется именно на сетевую шару: монтирование
        # обязательно, независимо от CHECK_NETWORK_MOUNT
        # ТЗ 3.2 исправление №1).
        check_network_mount(
            config.NETWORK_MOUNT,
            expected_fs_type=getattr(config, "EXPECTED_NETWORK_FS_TYPE", None),
            check=True,
        )
        net_dir = ensure_network_dir(config.NETWORK_MOUNT, config.NETWORK_DIRECTORY)
        check_network_free_space(net_dir,
                                 getattr(config, "MIN_NETWORK_FREE_BYTES", 0))
        if verify_integrity:
            if password is None:
                password = encryption_module.resolve_decryption_password(
                    config.GPG_PASSWORD, non_interactive)
            log.info("Пароль GPG получен (значение не логируется)")

        entries = []
        if archive_path is not None:
            selected_paths = (archive_path if isinstance(
                archive_path, (list, tuple, set)) else [archive_path])
            for selected in selected_paths:
                try:
                    selected_resolved = Path(selected).resolve(strict=True)
                except OSError as exc:
                    log.warning("Выбранный архив недоступен %s: %s",
                                selected, exc)
                    continue
                # При явном выборе в TUI разрешён любой локальный .gpg-файл
                # из списка архивов. Каталоги из конфигурации ограничивают
                # автоматический поиск pending, но не ручную отправку.
                if (selected_resolved.is_file() and
                        selected_resolved.suffix.lower() == ".gpg"):
                    entries.append((str(selected_resolved.parent),
                                    selected_resolved.name))
            if not entries:
                raise StorageError(
                    "Среди выбранных файлов нет доступных локальных .gpg "
                    "архивов в настроенных каталогах.")
        else:
            for path in backup_dirs:
                entries.extend(
                    (str(path), entry) for entry in _list_pending(path, net_dir))
        entries.sort(key=lambda pair: pair[1])
        if not entries:
            reporter.info(
                "Локальные архивы для отправки не найдены: {}".format(
                    ", ".join(backup_dirs)))
            log.info("upload-pending: локальные архивы не найдены")
            report.pending_local = 0
            return report

        delivered = 0
        failed = 0
        for backup_dir, entry in entries:
            path = Path(backup_dir) / entry
            reporter.info("Архив: {} (в {})".format(path.name, backup_dir))
            try:
                sha, meta = _pending_expected_sha(path)
                compression = compression_module.detect_compression(path.name)
                reporter.info("    Сжатие: {} (определено по архиву)".format(
                    compression))
                compression_module.require_tool(compression)
                # При ручной отправке уже зашифрованные файлы копируются
                # как есть: пароли могут отличаться у каждого архива.
                if verify_integrity:
                    verification.test_decrypted_stream(
                        path, gpg_binary=config.GPG_BINARY, password=password,
                        compression=compression, abort_check=abort_check,
                    )
                    reporter.info(
                        "    Расшифрование и тест сжатого потока пройдены")
                else:
                    reporter.info(
                        "    Передача готового GPG-файла без расшифрования")
                # Если выбранный архив (или pending-копия) уже физически
                # лежит на шаре, сверяем SHA-256 ДО копирования. Совпадение
                # подтверждаем метаданными; конфликт никогда не перезаписываем.
                remote_path = Path(net_dir) / path.name
                if remote_path.exists() or remote_path.is_symlink():
                    if remote_path.is_symlink() or not remote_path.is_file():
                        raise StorageError(
                            "На сетевой шаре уже существует объект с именем "
                            "'{}', но это не обычный файл; перезапись запрещена."
                            .format(path.name))
                    remote_sha = verification.sha256_file(remote_path)
                    if remote_sha != sha:
                        raise StorageError(
                            "На сетевой шаре уже существует '{}' с другим "
                            "SHA-256; перезапись запрещена. Локальный архив "
                            "сохранен.".format(path.name))
                    if archive_path is not None:
                        # При ручной отправке физическое наличие совпадающего
                        # файла на NAS достаточно: sidecar не обязателен для
                        # защиты от дубликата и не должен блокировать отправку.
                        pass
                    else:
                        if meta is None:
                            meta = metadata.infer_archive_info(path)
                        if meta is None:
                            raise StorageError(
                                "Архив на шаре совпадает по SHA-256, но не "
                                "удалось сформировать метаданные; локальная "
                                "копия сохранена.")
                        recheck_network_mount(
                            config.NETWORK_MOUNT,
                            expected_fs_type=getattr(
                                config, "EXPECTED_NETWORK_FS_TYPE", None))
                        if not write_network_metadata(net_dir, meta, sha):
                            raise StorageError(
                                "Архив уже есть на шаре, но сетевые метаданные "
                                "не удалось подтвердить; локальная копия сохранена.")
                    if meta is not None:
                        meta = metadata.set_status(meta, metadata.STATUS_UPLOADED)
                        metadata.write_metadata(path, meta)
                    recheck_network_mount(
                        config.NETWORK_MOUNT,
                        expected_fs_type=getattr(
                            config, "EXPECTED_NETWORK_FS_TYPE", None))
                    delete_local_archive(path)
                    delivered += 1
                    report.archives_uploaded += 1
                    reporter.info(
                        "    Уже находится на шаре (SHA-256 совпадает); "
                        "повторная передача не выполнялась")
                    continue

                # Повторная проверка монтирования перед копированием
                recheck_network_mount(
                    config.NETWORK_MOUNT,
                    expected_fs_type=getattr(config,
                                             "EXPECTED_NETWORK_FS_TYPE", None),
                )
                tmp_path, actual_sha = copy_to_network_tmp(path, net_dir, sha)
                try:
                    # Повторная проверка перед финализацией
                    recheck_network_mount(
                        config.NETWORK_MOUNT,
                        expected_fs_type=getattr(
                            config, "EXPECTED_NETWORK_FS_TYPE", None),
                    )
                    promote_network_archive(tmp_path, actual_sha)
                except BaseException:
                    try:
                        Path(tmp_path).unlink()
                    except OSError:
                        pass
                    raise
                # Метаданные обязательны для удаления локальной копии
                #: при их отсутствии
                # формируются минимальные по имени архива; если даже это
                # невозможно — локальный архив сохраняется.
                if meta is None:
                    meta = metadata.infer_archive_info(path)
                if meta is None:
                    raise StorageError(
                        "Не удалось сформировать сетевые метаданные для "
                        "{} (имя не соответствует шаблону приложения); "
                        "ЛОКАЛЬНЫЙ АРХИВ СОХРАНЕН.".format(path.name)
                    )
                # Повторная проверка перед записью метаданных
                recheck_network_mount(
                    config.NETWORK_MOUNT,
                    expected_fs_type=getattr(config,
                                             "EXPECTED_NETWORK_FS_TYPE", None),
                )
                if not write_network_metadata(net_dir, meta, actual_sha):
                    raise StorageError(
                        "Сетевые метаданные '{}' не записаны или не "
                        "подтверждены повторным чтением; ЛОКАЛЬНЫЙ АРХИВ "
                        "СОХРАНЕН.".format(path.name)
                    )
                # Повторная проверка перед удалением локального архива
                #
                recheck_network_mount(
                    config.NETWORK_MOUNT,
                    expected_fs_type=getattr(config,
                                             "EXPECTED_NETWORK_FS_TYPE", None),
                )
                delete_local_archive(path)
                delivered += 1
                report.archives_uploaded += 1
                log.info("upload-pending: отправлен %s (сжатие: %s, SHA-256: %s)",
                         path.name, compression, actual_sha)
                reporter.info("    Отправлен и подтвержден; локальная копия удалена")
            except BtrfsBackupError as exc:
                failed += 1
                report.failures += 1
                reporter.error(
                    "Ошибка: {}. ЛОКАЛЬНЫЙ АРХИВ СОХРАНЕН.".format(exc))
                log.error("upload-pending: %s: %s", path.name, exc)

        report.pending_local = sum(
            count_pending_archives(path) for path in backup_dirs)
        report.archives_created = 0
        report.snapshots_created = 0
        report.total_seconds = time.monotonic() - started
        report.timings["upload"] = report.total_seconds
        log.info(
            "upload-pending завершено: отправлено %d, с ошибками %d, "
            "время %.1f с", delivered, failed, report.total_seconds,
        )
        # в TUI отчет через reporter (без прямого print())
        report.emit(started, reporter)
        if failed:
            raise StorageError(
                "Не все локальные архивы отправлены: успешных {}, с "
                "ошибками {}. Локальные копии с ошибками сохранены.".format(
                    delivered, failed)
            )
        reporter.done("Отправка завершена: архивов {}, ошибок нет.".format(
            delivered))
        return report
    finally:
        if lock:
            locking.release_lock()


def _list_pending(backup_dir, net_dir):
    """
    Имена локальных архивов, ожидающих отправки.

    Уже подтвержденные (upload_status=uploaded при существующем сетевом
    дубликате с совпадающим SHA-256) не отправляются повторно.
    """
    result = []
    backup_dir = Path(backup_dir)
    for entry in sorted(backup_dir.glob("*.gpg")):
        if not entry.is_file():
            continue
        meta = metadata.read_metadata(entry)
        if meta is None:
            meta = metadata.infer_archive_info(entry)
        expected_sha = (meta or {}).get("sha256")
        if (meta or {}).get("upload_status") == "uploaded" and \
                expected_sha and \
                network_archive_exists_with_sha(net_dir, entry.name,
                                                expected_sha):
            log.info("upload-pending: %s уже подтвержден на шаре — пропуск",
                     entry.name)
            continue
        result.append(entry.name)
    return result


def network_archive_exists_with_sha(net_dir, archive_name, expected_sha):
    """True, если сетевой архив существует и его SHA-256 совпадает."""
    path = Path(net_dir) / archive_name
    if not path.is_file():
        return False
    try:
        return verification.sha256_file(path) == (expected_sha or "").lower()
    except OSError:
        return False


def _pending_expected_sha(path):
    """
    Ожидаемый SHA-256 и метаданные локального архива.

    Приоритет: метаданные JSON (поле sha256) -> файл .sha256
    (архивы предыдущих версий, ТЗ 19) -> фактическое вычисление
    (архив целиком проверяется расшифрованием и тестом потока;
    фактическая сумма фиксируется как эталон для сетевой копии).
    """
    from .verification import parse_checksum_file, sha256_file
    meta = metadata.read_metadata(path) or metadata.infer_archive_info(path)
    if meta and meta.get("sha256"):
        actual = sha256_file(path)
        if actual != meta["sha256"]:
            raise verification.VerificationError(
                "SHA-256 архива {} НЕ совпал с метаданными ({} != {}). "
                "Архив поврежден; отправка отменена.".format(
                    path.name, meta["sha256"], actual)
            )
        return meta["sha256"], meta
    checksum = Path(str(path) + ".sha256")
    if checksum.is_file():
        expected = parse_checksum_file(checksum)
        # Наличие .sha256 не доказательство — сумма сверяется
        # с фактической.
        actual = sha256_file(path)
        if actual != expected:
            raise verification.VerificationError(
                "SHA-256 архива {} НЕ совпал с файлом контрольной суммы "
                "({} != {}). Архив поврежден; отправка отменена.".format(
                    path.name, expected, actual)
            )
        return expected, meta
    # Эталона нет: вычисляем фактическую сумму (архив будет полностью
    # проверен расшифрованием и тестом потока перед отправкой).
    actual = sha256_file(path)
    log.warning(
        "У архива %s отсутствуют метаданные и .sha256; "
        "целостность подтверждается расшифрованием и тестом потока, "
        "SHA-256 вычислен фактически", path.name,
    )
    return actual, meta


# ============================================
# Очистка осиротевших временных файлов
# ============================================

def preflight_selected_uploads(config, archive_paths):
    """Проверяет выбранные архивы и сетевые дубликаты до подтверждения отправки.

    Возвращает (ready_paths, already_present, blocked), где blocked —
    пары (имя, причина). Файлы на NAS никогда не перезаписываются.
    """
    if not getattr(config, "NETWORK_ENABLED", True):
        raise StorageError("Сетевое хранилище отключено (NETWORK_ENABLED=false).")
    mount = getattr(config, "NETWORK_MOUNT", None)
    if not mount:
        raise StorageError("Не задан NETWORK_MOUNT.")
    check_network_mount(
        mount,
        expected_fs_type=getattr(config, "EXPECTED_NETWORK_FS_TYPE", None),
        check=True,
    )
    net_dir = ensure_network_dir(mount, config.NETWORK_DIRECTORY)
    ready, already, blocked = [], [], []
    for selected in archive_paths:
        path = Path(selected)
        try:
            resolved = path.resolve(strict=True)
            # Ручной выбор пользователем является явным разрешением на
            # отправку файла из любого каталога, показанного в списке TUI.
            # _local_backup_dirs() применяется только для фонового поиска.
            if not resolved.is_file() or resolved.suffix.lower() != ".gpg":
                raise StorageError("файл отсутствует или не является обычным .gpg-архивом")
            expected_sha, _meta = _pending_expected_sha(resolved)
            remote = Path(net_dir) / resolved.name
            if remote.exists() or remote.is_symlink():
                if remote.is_symlink() or not remote.is_file():
                    raise StorageError("на NAS уже есть объект с таким именем, это не обычный файл")
                if verification.sha256_file(remote) != expected_sha:
                    raise StorageError("архив с таким именем уже есть на NAS, но SHA-256 отличается; перезапись запрещена")
                # Физическое наличие файла с совпадающим SHA-256 уже
                # доказывает, что передавать его повторно не требуется.
                # JSON sidecar может отсутствовать у старых/ручных копий;
                # это не повод гонять архив по сети ещё раз.
                already.append(resolved)
            else:
                ready.append(resolved)
        except (BtrfsBackupError, OSError, ValueError) as exc:
            blocked.append((path.name, str(exc)))
    return ready, already, blocked

def cleanup_tmp_files(config, reporter=None):
    """
    Очистка незавершенных файлов (*.part, *.tmp) строго своих шаблонов
    после прерванных операций.

    Готовые архивы и снапшоты не затрагиваются. Возвращает количество
    удаленных файлов.
    """
    reporter = _default(reporter)
    removed = 0
    try:
        backup_dirs = _local_backup_dirs(config)
    except Exception:  # noqa: BLE001
        backup_dirs = [config.local_backup_dir(config.SNAPSHOT_DIR)]
    for dir_path in backup_dirs:
        backup_dir = Path(dir_path)
        if backup_dir.is_dir():
            for entry in backup_dir.iterdir():
                if entry.is_file() and (entry.name.endswith(".gpg.part") or
                                        entry.name.endswith(".sha256.tmp") or
                                        entry.name.endswith(".json.tmp")):
                    try:
                        entry.unlink()
                        removed += 1
                        reporter.info("Удален незавершенный файл: {}".format(
                            entry.name))
                    except OSError as exc:
                        reporter.warn("Не удалось удалить {}: {}".format(
                            entry, exc))
    if getattr(config, "NETWORK_ENABLED", True):
        network_mount = getattr(config, "NETWORK_MOUNT", None)
        if not network_mount:
            return removed
        try:
            net_dir = Path(network_mount) / config.NETWORK_DIRECTORY
            if net_dir.is_dir():
                removed += cleanup_network_tmp_files(net_dir)
        except OSError as exc:
            reporter.warn("Сетевая шара недоступна для очистки: {}".format(exc))
    log.info("Очистка временных файлов: удалено %d", removed)
    return removed


_ = OperationAborted  # сохранение импорта для расширений
