#!/usr/bin/env python3
"""
Btrfs Backup Manager — точка входа.

Режимы управления:
  python3 main.py          — простой интерактивный CLI-меню (без curses);
  python3 main.py tui      — полноэкранный curses-интерфейс;
  аргументы командной строки — для автоматизации, скриптов и cron.

Альтернативный файл настроек (для отдельного cron-задания или
приемочных испытаний):
  python3 main.py backup --config /путь/файла.py
  python3 main.py --config /путь/файла.py backup
Оба порядка аргументов равнозначны.

Коды завершения:
  0  операция успешна (локальный backup без сети — тоже успех);
  1  общая ошибка;
  2  ошибка конфигурации или аргументов;
  3  отсутствуют необходимые системные утилиты;
  4  ошибка Btrfs;
  5  ошибка GPG;
  6  ошибка сжатия или распаковки;
  7  ошибка сетевой отправки;
  8  ошибка проверки целостности;
  9  операция отменена;
  10 операция уже выполняется (файл блокировки занят).
"""

import argparse
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from core import (  # noqa: E402
    APP_TITLE, APP_VERSION, BtrfsBackupError, EXIT_ERROR, EXIT_OK,
    ConfigError, OperationAborted, exit_code_for,
)
from core import appconfig  # noqa: E402
from core import environment, locking  # noqa: E402
from core.logging_setup import get_logger, setup_logging  # noqa: E402
from core.process import set_pipeline_timeout  # noqa: E402

log = get_logger("main")

_LOCK_FALLBACK_DIR = Path(__file__).resolve().parent / "logs"


# ============================================
# Сигналы
# ============================================

def _handle_signal(signum, frame):
    raise OperationAborted(
        "получен сигнал {}".format(signal.Signals(signum).name))


def install_signal_handlers():
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass


# ============================================
# Построение парсера
# ============================================

def build_parser():
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="{} {} — резервное копирование и восстановление Btrfs "
                    "subvolume (снапшоты, btrfs send, gzip/zstd, GPG, "
                    "необязательное сетевое хранилище).".format(
                        APP_TITLE, APP_VERSION),
        epilog="Примеры:\n"
               "  python3 main.py                            простой CLI-меню\n"
               "  python3 main.py tui                        полноэкранный TUI (curses)\n"
               "  python3 main.py backup                     резервное копирование\n"
               "  python3 main.py backup -n                  cron-режим (без вопросов)\n"
               "  python3 main.py backup --config /путь/файла.py\n"
               "  python3 main.py --config /путь/файла.py backup\n"
               "  python3 main.py restore --archive A --target D\n"
               "  python3 main.py upload-pending             отправка ожидающих архивов\n"
               "  python3 main.py verify --archive PATH      проверка архива\n"
               "  python3 main.py delete-archives            удаление локальных архивов\n"
               "  python3 main.py delete-snapshots           удаление снапшотов\n"
               "  python3 main.py snapshots                  список снапшотов\n"
               "  python3 main.py list-archives              список архивов\n"
               "  python3 main.py diagnostics                диагностика системы\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # --config объявлен для справки; фактический разбор выполняется
    # _extract_config_path ДО argparse (порядок аргументов любой, ТЗ 2.3)
    parser.add_argument(
        "--config", metavar="ПУТЬ",
        help="альтернативный файл настроек (любая позиция аргументов)")
    sub = parser.add_subparsers(dest="command", metavar="команда")

    sub.add_parser("tui", help="полноэкранный текстовый интерфейс (curses)")

    p_backup = sub.add_parser(
        "backup", help="полное резервное копирование всех subvolume")
    p_backup.add_argument(
        "-n", "--non-interactive", action="store_true",
        help="неинтерактивный режим (cron): без вопросов и запросов пароля; "
             "пароль берется только из config.py")
    p_backup.add_argument(
        "--subvolume", action="append", metavar="ИМЯ_ИЛИ_ПУТЬ",
        help="обработать только указанный subvolume (имя из конфигурации "
             "или путь); можно повторять")

    p_restore = sub.add_parser(
        "restore", help="восстановление из архива или локального снапшота")
    p_restore.add_argument("--archive", help="путь к архиву (локальному или "
                                             "сетевому)")
    p_restore.add_argument("--target", help="целевой каталог на Btrfs")
    p_restore.add_argument("-n", "--non-interactive", action="store_true",
                           help="неинтерактивный режим: без вопросов")

    p_upload = sub.add_parser(
        "upload-pending",
        help="отправить локальные архивы, ожидающие отправки")
    p_upload.add_argument("-n", "--non-interactive", action="store_true",
                          help="неинтерактивный режим (cron)")

    p_verify = sub.add_parser(
        "verify", help="проверка целостности архива")
    p_verify.add_argument("--archive", help="путь к архиву "
                                            "(иначе интерактивный выбор)")
    p_verify.add_argument("-n", "--non-interactive", action="store_true")

    sub.add_parser("snapshots", help="список локальных снапшотов")
    sub.add_parser("list-archives",
                   help="список локальных и сетевых архивов")
    sub.add_parser("diagnostics", help="диагностика системы")

    p_cleanup = sub.add_parser(
        "cleanup", help="очистка старых снапшотов по retention "
                        "(с подтверждением)")
    p_cleanup.add_argument("--yes", action="store_true",
                           help="удалить без запроса подтверждения")

    p_del_arch = sub.add_parser(
        "delete-archives",
        help="удаление локальных архивов (вместе с метаданными)")
    p_del_arch.add_argument(
        "--archive", action="append", metavar="ИМЯ_ИЛИ_ПУТЬ",
        help="имя файла архива или путь; можно повторять")
    p_del_arch.add_argument(
        "--all", action="store_true",
        help="удалить ВСЕ локальные архивы (только с --yes)")
    p_del_arch.add_argument(
        "--yes", action="store_true",
        help="удалить без запроса подтверждения")

    p_del_snap = sub.add_parser(
        "delete-snapshots",
        help="удаление локальных снапшотов (btrfs subvolume delete)")
    p_del_snap.add_argument(
        "--snapshot", action="append", metavar="ИМЯ",
        help="имя снапшота; можно повторять")
    p_del_snap.add_argument(
        "--all", action="store_true",
        help="удалить ВСЕ снапшоты (только с --yes)")
    p_del_snap.add_argument(
        "--yes", action="store_true",
        help="удалить без запроса подтверждения")
    return parser


# ============================================
# Команды
# ============================================

def _lock_or_fail(cfg):
    locking.acquire_lock(
        getattr(cfg, "LOCK_FILE", None) or
        (cfg.default_lock_file() if hasattr(cfg, "default_lock_file")
         else "/run/lock/btrfs-backup-manager.lock"),
        _LOCK_FALLBACK_DIR)


def cmd_tui(cfg):
    from ui import tui
    if tui.is_available():
        return tui.run(cfg)
    print("curses недоступен или терминал неинтерактивен; "
          "запускается простой CLI.")
    from ui import cli
    cli.run_menu(cfg)
    return EXIT_OK


def cmd_default(cfg):
    """
    Без команды: простой интерактивный CLI-меню.

    python3 main.py запускает простой CLI-меню, а полноэкранный
    curses-интерфейс — main.py tui. В нетерминальной среде печатается
    справка по командам.
    """
    from ui import cli
    if sys.stdin.isatty() and sys.stdout.isatty():
        cli.run_menu(cfg)
        return EXIT_OK
    parser = build_parser()
    parser.print_help()
    return EXIT_OK


def cmd_backup(cfg, non_interactive, subvolume_filter=None):
    from core import backup as backup_ops
    from core.backup import ConsoleReporter
    _lock_or_fail(cfg)
    backup_ops.run_backup(
        cfg, non_interactive=non_interactive,
        confirm_fn=None if non_interactive else _console_confirm,
        reporter=ConsoleReporter(),
        subvolume_filter=subvolume_filter,
    )
    return EXIT_OK


def _console_confirm(text):
    from ui.dialogs import confirm
    return confirm(text)


def cmd_restore(args, cfg):
    from core import encryption as encryption_module
    from core import restore as restore_module
    from core.backup import ConsoleReporter

    archive_path = args.archive
    target = args.target
    non_interactive = args.non_interactive
    interactive = not (archive_path and target)

    if archive_path:
        archive_path = Path(archive_path).expanduser()
        if not archive_path.is_file():
            raise BtrfsBackupError("Архив не найден: {}".format(archive_path))
    if target:
        target = Path(target).expanduser()

    _lock_or_fail(cfg)
    environment.validate_or_raise(cfg, strict=not interactive)
    environment.check_utilities(need_gpg=True, gpg_binary=cfg.GPG_BINARY,
                                gpg_check=encryption_module.check_gpg)

    if interactive:
        from ui import cli
        cli.interactive_restore(cfg)
        return EXIT_OK

    # Неинтерактивное восстановление: пароль только из конфигурации
    if cfg.is_password_placeholder(cfg.GPG_PASSWORD):
        raise BtrfsBackupError(
            "Пароль GPG не задан в конфигурации; неинтерактивное "
            "восстановление невозможно (запустите интерактивно или "
            "задайте GPG_PASSWORD)."
        )
    dest = restore_module.receive_archive(
        archive_path, target,
        gpg_binary=cfg.GPG_BINARY, password=cfg.GPG_PASSWORD,
    )
    reporter = ConsoleReporter()
    reporter.done("Восстановление завершено: {}".format(dest))
    return EXIT_OK


def cmd_upload_pending(cfg, non_interactive):
    from core import backup as backup_ops
    _lock_or_fail(cfg)
    backup_ops.upload_pending(
        cfg, non_interactive=non_interactive,
        reporter=None, lock=False)
    return EXIT_OK


def cmd_verify(args, cfg):
    from core import encryption as encryption_module
    from core import metadata, verification
    from core.process import human_size

    path = args.archive
    if path:
        path = Path(path).expanduser()
        if not path.is_file():
            raise BtrfsBackupError("Архив не найден: {}".format(path))
    else:
        from ui import cli
        path = cli.choose_archive_cli(cfg)
        if path is None:
            print("Отменено.")
            return 9
    meta = metadata.read_metadata(path) or metadata.infer_archive_info(path)
    expected_sha = (meta or {}).get("sha256")
    if cfg.is_password_placeholder(cfg.GPG_PASSWORD):
        if args.non_interactive:
            raise BtrfsBackupError(
                "Пароль GPG не задан в конфигурации; неинтерактивная "
                "проверка расшифрования невозможна.")
        password = encryption_module.prompt_decryption_password(
            cfg.GPG_PASSWORD)
    else:
        password = cfg.GPG_PASSWORD
    result = verification.verify_archive_full(
        path, gpg_binary=cfg.GPG_BINARY, password=password,
        expected_sha=expected_sha)
    print("Архив: {}".format(path))
    print("  Размер: {}".format(human_size(result["size"])))
    print("  Сжатие: {} (определено по архиву)".format(result["compression"]))
    print("  SHA-256: {}".format(result["sha256"]))
    print("  Расшифрование и сжатый поток: OK")
    print("Проверка пройдена.")
    return EXIT_OK


def cmd_snapshots(cfg):
    from core import snapshots as snapshots_module
    environment.require_root()
    environment.validate_or_raise(cfg)
    names = {item["name"] for item in cfg.normalize_subvolumes()[0]}
    groups = snapshots_module.list_snapshots(cfg.SNAPSHOT_DIR, names)
    if not groups:
        print("Снапшоты не найдены: {}".format(cfg.SNAPSHOT_DIR))
        return EXIT_OK
    for name in sorted(groups):
        print("{}:".format(name))
        for snap in groups[name]:
            ro = snapshots_module.subvolume_ro_property(snap.path)
            state = "read-only" if ro else \
                ("записываемый" if ro is False else "состояние неизвестно")
            print("  {}  [{}]".format(snap.name, state))
    return EXIT_OK


def cmd_list_archives(cfg):
    from core.backup import _local_backup_dirs
    from ui import common
    dirs = _local_backup_dirs(cfg)
    first = True
    for dir_path in dirs:
        if not first:
            print()
        first = False
        print("Локальные архивы ({}):".format(dir_path))
        local = common.collect_local_archives(dir_path)
        if not local:
            print("  (нет)")
        for path in local:
            print("  " + common.archive_line(path))
    network_mount = getattr(cfg, "NETWORK_MOUNT", None)
    if not network_mount:
        print()
        print("Сетевые архивы: хранилище не настроено (NETWORK_MOUNT)")
        return EXIT_OK
    net_dir = Path(network_mount) / cfg.NETWORK_DIRECTORY
    print()
    print("Сетевые архивы ({}):".format(net_dir))
    try:
        network = common.collect_network_archives(net_dir)
    except OSError as exc:
        print("  Сетевая шара недоступна: {}".format(exc))
        return EXIT_OK
    if not network:
        print("  (нет)")
    for path in network:
        print("  " + common.archive_line(path))
    return EXIT_OK


def cmd_diagnostics(cfg):
    results = environment.run_diagnostics(cfg)
    labels = {"critical": "[КРИТИЧНО]", "warning": "[ПРЕДУПР.]",
              "info": "[ИНФО]    ", "ok": "[OK]      "}
    for item in results:
        print("{} {}".format(labels.get(item["level"], "[?]"),
                             item["message"]))
    critical = sum(1 for item in results if item["level"] == "critical")
    warnings = sum(1 for item in results if item["level"] == "warning")
    print()
    print("Критических ошибок: {}; предупреждений: {}".format(
        critical, warnings))
    return 1 if critical else EXIT_OK


def cmd_cleanup(cfg, assume_yes):
    """Очистка старых снапшотов по retention (с обязательной проверкой сети)."""
    from core import retention as retention_module
    from core.backup import _retention_groups
    _lock_or_fail(cfg)
    environment.require_root()
    environment.validate_or_raise(cfg)
    network_mount = getattr(cfg, "NETWORK_MOUNT", None)
    if not network_mount:
        raise BtrfsBackupError(
            "Сетевое хранилище не настроено (NETWORK_MOUNT): очистка "
            "снапшотов без подтвержденной сетевой копии запрещена.")
    net_dir = Path(network_mount) / cfg.NETWORK_DIRECTORY
    # ТЗ 9.3/18: монтирование проверяется обязательно, независимо от
    # CHECK_NETWORK_MOUNT. Любая ошибка — удаление отменяется.
    retention_module.retention_safety_check(cfg, net_dir)
    groups = _retention_groups(cfg, None)
    if not groups:
        print("Источники не заданы в конфигурации — очищать нечего.")
        return EXIT_OK
    total_plan = {}
    for snapshot_dir, names in groups:
        plan = retention_module.retention_plan(
            snapshot_dir, names, cfg.retention_count(), net_dir)
        for name, items in plan.items():
            total_plan.setdefault(name, []).extend(items)
    total = sum(len(items) for items in total_plan.values())
    if not total:
        print("Снапшоты для удаления не найдены: все последние {} "
              "подтвержденных копий сохранены.".format(cfg.retention_count()))
        return EXIT_OK
    print("Кандидаты на удаление (сверх retention {}, только с подтвержденной "
          "копией на сетевой шаре):".format(cfg.retention_count()))
    for items in total_plan.values():
        for name in items:
            print("  {}".format(name))
    if not assume_yes and not _console_confirm(
            "Удалить перечисленные снапшоты ({} шт.)?".format(total)):
        print("Отменено.")
        return 9
    removed = 0
    for snapshot_dir, names in groups:
        deleted = retention_module.apply_retention(
            snapshot_dir, names, cfg.retention_count(), net_dir,
            delete=True)
        removed += sum(len(items) for items in deleted.values())
    print("Удалено снапшотов: {}".format(removed))
    log.info("Ручная очистка: удалено снапшотов %d", removed)
    return EXIT_OK


# ============================================
# Удаление архивов/снапшотов: интерактивные пути команд
# delete-archives / delete-snapshots
# ============================================

def _resolve_archive_targets(backup_dir, requested):
    """Сопоставляет --archive только с обычными файлами внутри backup_dir.

    Абсолютный путь допустим, но он не расширяет область удаления за
    пределы настроенного каталога локальных архивов. Символические ссылки
    исключены: команда удаления работает только с непосредственными
    файлами каталога резервных копий.
    """
    from ui import common as ui_common
    found, missing = [], []
    base = Path(backup_dir).expanduser()
    try:
        base_real = base.resolve(strict=True)
    except OSError:
        return [], list(requested)

    def local_regular_archive(path):
        path = Path(path).expanduser()
        try:
            if path.is_symlink() or not path.is_file():
                return None
            resolved = path.resolve(strict=True)
            if resolved.parent != base_real or resolved.suffix != ".gpg":
                return None
            return resolved
        except (OSError, RuntimeError):
            return None

    for item in requested:
        candidate = Path(item).expanduser()
        if not candidate.is_absolute():
            candidate = base / item
        local = local_regular_archive(candidate)
        if local is not None:
            found.append(local)
            continue
        # Имя без суффикса: ищем только обычные архивы непосредственно
        # в настроенном каталоге; ссылки и внешние пути не принимаются.
        fuzzy = []
        for path in ui_common.collect_local_archives(base):
            if path.name == item or path.name.startswith(item + "."):
                local = local_regular_archive(path)
                if local is not None:
                    fuzzy.append(local)
        if fuzzy:
            found.extend(fuzzy)
        else:
            missing.append(item)
    # убрать дубликаты, сохранив порядок
    seen, unique = set(), []
    for path in found:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique, missing


def cmd_delete_archives(args, cfg):
    """
    Удаление локальных архивов. Без аргументов — интерактивный выбор;
    --archive ИМЯ (можно повторять) или --all; --yes — без вопросов.
    Список архивов печатается ПЕРЕД подтверждением; --all требует --yes.
    """
    from core.backup import _local_backup_dirs
    from core.network import delete_local_archive
    from ui import common as ui_common
    _lock_or_fail(cfg)
    environment.validate_or_raise(cfg)
    dirs = _local_backup_dirs(cfg)
    if args.all:
        if not args.yes:
            raise BtrfsBackupError(
                "Удаление ВСЕХ архивов требует явного --yes; без него "
                "используйте --archive ИМЯ или интерактивный режим.")
        targets = []
        for dir_path in dirs:
            targets.extend(ui_common.collect_local_archives(dir_path))
    elif args.archive:
        targets, missing = [], []
        for dir_path in dirs:
            found, missed = _resolve_archive_targets(dir_path, args.archive)
            targets.extend(found)
            missing.extend(missed)
        for item in missing:
            print("Не найден архив: {}".format(item), file=sys.stderr)
        if missing and not targets:
            return 1
    else:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("Укажите --archive ИМЯ (можно повторять), --all --yes "
                  "или запустите интерактивно без аргументов.",
                  file=sys.stderr)
            return 2
        from ui import cli
        before = sum(len(ui_common.collect_local_archives(d)) for d in dirs)
        cli.delete_archives_cli(cfg)
        after = sum(len(ui_common.collect_local_archives(d)) for d in dirs)
        log.info("delete-archives (интерактивно): удалено %d", before - after)
        return EXIT_OK

    if not targets:
        print("Локальные архивы не найдены: {}".format(", ".join(dirs)))
        return EXIT_OK
    print("К удалению ({} шт.):".format(len(targets)))
    for path in targets:
        print("  " + path.name)
    if not args.yes:
        from ui.dialogs import confirm
        if not confirm("Удалить перечисленные архивы БЕЗВОЗВРАТНО?"):
            print("Отменено.")
            return 9
    for path in targets:
        delete_local_archive(path)
    print("Удалено архивов: {}".format(len(targets)))
    log.info("delete-archives: удалено %d", len(targets))
    return EXIT_OK


def cmd_delete_snapshots(args, cfg):
    """
    Удаление снапшотов. Без аргументов — интерактивный выбор;
    --snapshot ИМЯ (можно повторять) или --all; --yes — без вопросов.
    Каждый снапшот проходит проверки core.snapshots.delete_snapshot
    (шаблон имени, каталог, subvolume, read-only, ТЗ 18).
    """
    from core import snapshots as snapshots_module
    from core.backup import _retention_groups
    _lock_or_fail(cfg)
    environment.require_root()
    environment.validate_or_raise(cfg)
    groups_by_dir = {sdir: names
                     for sdir, names in _retention_groups(cfg, None)}
    if not groups_by_dir:
        print("Источники не заданы в конфигурации — удалять нечего.")
        return EXIT_OK
    flat = []
    for sdir in sorted(groups_by_dir):
        groups = snapshots_module.list_snapshots(sdir, groups_by_dir[sdir])
        for subvol in sorted(groups):
            flat.extend(groups[subvol])
    if args.all:
        if not args.yes:
            raise BtrfsBackupError(
                "Удаление ВСЕХ снапшотов требует явного --yes; без него "
                "используйте --snapshot ИМЯ или интерактивный режим.")
        targets = list(flat)
    elif args.snapshot:
        wanted = set(args.snapshot)
        targets = [snap for snap in flat if snap.name in wanted]
        missing = wanted - {snap.name for snap in targets}
        for item in sorted(missing):
            print("Не найден снапшот: {}".format(item), file=sys.stderr)
        if missing and not targets:
            return 1
    else:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("Укажите --snapshot ИМЯ (можно повторять), --all --yes "
                  "или запустите интерактивно без аргументов.",
                  file=sys.stderr)
            return 2
        from ui import cli
        before = len(flat)
        cli.delete_snapshots_cli(cfg)
        after = sum(
            len(sum((list(v) for v in
                     snapshots_module.list_snapshots(
                         sdir, names).values()), []))
            for sdir, names in groups_by_dir.items())
        log.info("delete-snapshots (интерактивно): удалено %d", before - after)
        return EXIT_OK

    if not targets:
        print("Снапшоты не найдены: {}".format(
            ", ".join(groups_by_dir)))
        return EXIT_OK
    print("К удалению ({} шт.):".format(len(targets)))
    for snap in targets:
        print("  " + snap.name)
    if not args.yes:
        from ui.dialogs import confirm
        if not confirm("Удалить перечисленные снапшоты безвозвратно?"):
            print("Отменено.")
            return 9
    deleted, failed = 0, 0
    for snap in targets:
        try:
            snapshots_module.delete_snapshot(snap.path, str(snap.path.parent))
            deleted += 1
        except BtrfsBackupError as exc:
            failed += 1
            print("Не удалось удалить {}: {}".format(snap.name, exc),
                  file=sys.stderr)
    print("Удалено снапшотов: {}{}".format(
        deleted, "; ошибок: {}".format(failed) if failed else ""))
    log.info("delete-snapshots: удалено %d (ошибок: %d)", deleted, failed)
    return 1 if (failed and not deleted) else EXIT_OK


# ============================================
# Профили конфигурации
# ============================================

def _extract_config_path(argv):
    """
    Извлекает --config (любая позиция, `--config ПУТЬ` или
    `--config=ПУТЬ`) из аргументов. Возвращает (путь_или_None, остаток).
    argparse разбирает остаток штатно — поэтому оба порядка
    (`backup --config X` и `--config X backup`) работают одинаково.
    """
    path = None
    rest = []
    tokens = list(argv)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--config":
            if index + 1 >= len(tokens):
                raise ConfigError(
                    "--config: укажите путь к файлу настроек после ключа")
            path = tokens[index + 1]
            index += 2
            continue
        if token.startswith("--config="):
            path = token.split("=", 1)[1]
            index += 1
            continue
        rest.append(token)
        index += 1
    return path, rest


def _load_profile(config_path):
    """
    Загружает настройки.

    config_path задан — файл обязан существовать (CLI/cron строго по
    конфигурации). Не задан — основной backup_config.py; его отсутствие
    не мешает TUI (обнаружение источников), а неинтерактивные команды
    получат понятную ошибку валидации.
    """
    if config_path:
        return appconfig.load_config_or_fail(config_path)
    cfg = appconfig.from_module(config)
    config.raise_if_broken()
    return cfg


# ============================================
# main
# ============================================

def main(argv=None):
    raw_argv = list(sys.argv[1:]) if argv is None else list(argv)
    try:
        config_path, rest_argv = _extract_config_path(raw_argv)
    except ConfigError as exc:
        setup_logging(
            config.LOG_FILE, config.LOG_LEVEL,
            debug_stderr=bool(os.environ.get("BBM_DEBUG")))
        log.error("Ошибка конфигурации: %s", exc)
        print("Ошибка конфигурации: {}".format(exc), file=sys.stderr)
        return exit_code_for(exc)
    # Разбираем аргументы ДО настройки логирования и загрузки профиля.
    # argparse сам завершает --help; справка не должна пытаться открыть
    # системный LOG_FILE и засорять stderr предупреждением о правах.
    parser = build_parser()
    args = parser.parse_args(rest_argv)
    setup_logging(
        config.LOG_FILE, config.LOG_LEVEL,
        debug_stderr=bool(os.environ.get("BBM_DEBUG")))
    set_pipeline_timeout(getattr(config, "PROCESS_TIMEOUT", 3600))
    install_signal_handlers()
    try:
        cfg = _load_profile(config_path)
        # Журнал профиля: у каждого профиля свой файл; при
        # отличии от журнала по умолчанию — переинициализация
        profile_log = getattr(cfg, "LOG_FILE", None) or \
            (cfg.default_log_file() if hasattr(cfg, "default_log_file")
             else config.LOG_FILE)
        if config_path and profile_log and profile_log != config.LOG_FILE:
            from core.logging_setup import reset_logging
            reset_logging()
            setup_logging(
                profile_log, cfg.LOG_LEVEL,
                debug_stderr=bool(os.environ.get("BBM_DEBUG")))
        set_pipeline_timeout(getattr(cfg, "PROCESS_TIMEOUT", 3600))
        if args.command is None:
            return cmd_default(cfg)
        if args.command == "tui":
            return cmd_tui(cfg)
        if args.command == "backup":
            return cmd_backup(cfg, args.non_interactive,
                              subvolume_filter=args.subvolume)
        if args.command == "restore":
            return cmd_restore(args, cfg)
        if args.command == "upload-pending":
            return cmd_upload_pending(cfg, args.non_interactive)
        if args.command == "verify":
            return cmd_verify(args, cfg)
        if args.command == "snapshots":
            return cmd_snapshots(cfg)
        if args.command == "list-archives":
            return cmd_list_archives(cfg)
        if args.command == "diagnostics":
            return cmd_diagnostics(cfg)
        if args.command == "cleanup":
            return cmd_cleanup(cfg, args.yes)
        if args.command == "delete-archives":
            return cmd_delete_archives(args, cfg)
        if args.command == "delete-snapshots":
            return cmd_delete_snapshots(args, cfg)
        parser.error("неизвестная команда")
        return EXIT_ERROR
    except ConfigError as exc:
        log.error("Ошибка конфигурации: %s", exc)
        print("Ошибка конфигурации: {}".format(exc), file=sys.stderr)
        return exit_code_for(exc)
    except OperationAborted as exc:
        log.error("Операция прервана: %s", exc)
        print("Прервано: {}".format(exc), file=sys.stderr)
        return exit_code_for(exc)
    except KeyboardInterrupt:
        from core.process import terminate_all_active
        terminate_all_active()
        log.error("Прервано пользователем (SIGINT)")
        print("Прервано пользователем.", file=sys.stderr)
        return 9
    except BtrfsBackupError as exc:
        log.error("Ошибка: %s", exc)
        print("Ошибка: {}".format(exc), file=sys.stderr)
        return exit_code_for(exc)
    finally:
        locking.release_lock()


if __name__ == "__main__":
    sys.exit(main())
