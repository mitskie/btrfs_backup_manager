"""
Простой CLI-интерфейс: используется, когда curses недоступен
или терминал неинтерактивный. Один экран меню на итерацию, без повторной
стены строк: заголовок печатается один раз, после операции — только ее итог.
"""

from pathlib import Path

from core import (
    APP_TITLE, APP_VERSION, BtrfsBackupError, OperationAborted,
    OperationCancelled,
)
from core import backup as backup_ops
from core import compression as compression_module
from core import encryption as encryption_module
from core import metadata, restore as restore_module
from core import snapshots as snapshots_module
from core.backup import ConsoleReporter
from core.logging_setup import get_logger
from core.process import human_size
from . import common, dialogs

log = get_logger("ui.cli")

# Меню простого CLI зеркально TUI: четыре пункта.
# Диагностика — отдельная CLI-команда `main.py diagnostics` (§7.2),
# журналы — системными средствами, профили — CLI/автоматизация (§7.4).
MENU_TEMPLATE = """
=============================================
 {} {}
=============================================
  1. Резервное копирование
  2. Восстановление
  3. Локальные снапшоты
  0. Выход
"""

MENU = MENU_TEMPLATE.format(APP_TITLE, APP_VERSION)


def run_menu(config):
    """Простой интерактивный интерфейс (fallback без curses)."""
    while True:
        print(MENU)
        try:
            choice = input("Выберите действие: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if choice == "0":
            break
        if choice == "":
            continue
        try:
            if choice == "1":
                interactive_backup(config)
            elif choice == "2":
                interactive_restore(config)
            elif choice == "3":
                show_local_copies(config)
            else:
                print("Неизвестный пункт меню: '{}'".format(choice))
        except OperationAborted as exc:
            print("\nПрервано: {}".format(exc))
        except OperationCancelled as exc:
            print("\nОтменено: {}".format(exc))
        except BtrfsBackupError as exc:
            print("\nОшибка: {}".format(exc))
            log.error("Ошибка в меню: %s", exc)
        print()
    print("Выход.")


# ============================================
# Операции
# ============================================

def interactive_backup(config):
    """
    Экран выбора показывается всегда: конфигурационные разделы отмечены
    по умолчанию, остальные пользователь добавляет или отменяет сам.
    После выбора — единая проверка расположения; проблемные источники
    можно исключить; итоговые параметры подтверждаются перед операцией.
    """
    chosen = _choose_subvolumes_cli(config)
    if chosen is None:
        print("Отменено.")
        return
    items = _prepare_sources_cli(config, chosen)
    if items is None:
        return
    # ТЗ 1.4.7 §7.3 п.6: алгоритм сжатия выбирается для текущей операции
    compression = _choose_compression_cli(config)
    if compression is None:
        return
    backup_ops.run_backup(
        config, non_interactive=False,
        confirm_fn=dialogs.confirm, reporter=ConsoleReporter(),
        items_override=items, compression_override=compression,
    )


def _choose_compression_cli(config):
    """
    Выбор алгоритма сжатия для текущей операции в простом CLI: доступность утилит проверяется, по умолчанию —
    алгоритм из конфигурации. Возвращает "zstd"/"gzip" или None.
    """
    import shutil
    available = [algo for algo in ("zstd", "gzip") if shutil.which(algo)]
    if not available:
        print("Не найдены утилиты сжатия ни zstd, ни gzip — операция "
              "невозможна. Установите zstd или gzip.")
        return None
    default_algo = config.COMPRESSION
    if default_algo not in available:
        default_algo = available[0]
    for number, algo in enumerate(available, 1):
        mark = " (по умолчанию)" if algo == default_algo else ""
        print("  {}. {}{}".format(number, algo, mark))
    try:
        raw = input("Алгоритм сжатия (Enter — {}): ".format(
            default_algo)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if raw == "":
        return default_algo
    if raw in available:
        return raw
    if raw.isdigit() and 1 <= int(raw) <= len(available):
        return available[int(raw) - 1]
    print("Неизвестный алгоритм: '{}' — используйте zstd или gzip.".format(
        raw))
    return None


def _prepare_sources_cli(config, chosen):
    """
    Проверка источников и подбор каталогов для простого CLI — тот же
    механизм core.pathcheck, что в TUI и в backup.

    Проблемные источники: исключить (1) / отменить (2) / предложить
    автоматически подобранный каталог (3). Возвращает список словарей
    или None (отмена/все исключены).
    """
    from core import pathcheck as pathcheck_module
    items = [{"name": entry["name"], "source": entry["source"],
              "snapshot_dir": entry.get("snapshot_dir"),
              "local_backup_dir": entry.get("local_backup_dir")}
             for entry in chosen]
    while True:
        checks = pathcheck_module.check_sources(items, config)
        failed = [c for c in checks if not c["ok"]]
        if not failed:
            print("Итоговые параметры:")
            for check in checks:
                print("  {}: {}".format(check["name"], check["source"]))
                print("      снимки: {}".format(check["snapshot_dir"]))
                print("      архивы: {}".format(check["local_backup_dir"]))
                for warning in check["warnings"]:
                    print("      [!] {}".format(warning))
            return items
        for check in failed:
            print("Не прошел проверку {} ({}):".format(
                check["name"], check["source"]))
            for error in check["errors"]:
                for line in common.wrap_text(error, 70):
                    print("    " + line)
        ok_count = sum(1 for c in checks if c["ok"])
        if not ok_count:
            try:
                answer = input(
                    "Ни один источник не прошел проверку. 1 — завершить "
                    "операцию, 2 — предложить каталоги автоматически, "
                    "другое — отмена: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return None
            if answer == "1":
                print("Все источники исключены. Нет источников для "
                      "резервного копирования")
                return None
            if answer != "2":
                return None
        else:
            try:
                answer = input(
                    "1 — продолжить без проблемных источников, 2 — отменить, "
                    "3 — подобрать каталоги автоматически: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return None
            if answer == "1":
                items = [item for item, c in zip(items, checks) if c["ok"]]
                continue
            if answer != "3":
                return None
        changed = False
        for item, check in zip(items, checks):
            if check["ok"]:
                continue
            proposal = pathcheck_module.propose_snapshot_dir(item["source"])
            if proposal["ok"] and proposal["path"]:
                print("  {}: предлагаю каталог снапшотов {} ({})".format(
                    item["name"], proposal["path"], proposal["reason"]))
                if dialogs.confirm(
                        "Использовать предложенный каталог?", default_yes=True):
                    item["snapshot_dir"] = proposal["path"]
                    changed = True
        if not changed:
            print("Автоматический подбор не выполнен — источников с "
                  "подходящим каталогом нет.")
            return None


def _print_source_entries(entries, flags):
    """
    Список записей выбора с отметками: сноска о разделах конфигурации
    вместо пометки в каждой строке; группировка по устройству с деревом
    subvolume («/dev/sda1[/@home]»); не-Btrfs разделы не показываются.
    """
    print("Разделы, указанные в конфигурации, отмечены заранее — "
          "при необходимости измените выбор.")
    print("Разделы для резервного копирования ([x] — будут включены):")
    label_width = common.source_label_width(entries)
    for row in common.source_rows(entries):
        if row["kind"] == "header":
            print()
            print(row["text"])
            continue
        entry = entries[row["index"]]
        if entry.get("selectable", True):
            mark = "[x]" if flags[row["index"]] else "[ ]"
        else:
            mark = "[–]"  # недоступен — причина справа
        label, suffix = common.source_line(entries, row, label_width)
        print("  {:3}. {} {}  {}".format(
            row["index"] + 1, mark, label, suffix).rstrip())


def _choose_subvolumes_cli(config):
    """
    Интерактивный выбор разделов в простом CLI (экран выбора
    пользователя: «в tui и cli должна быть возможность интерактивно
    выбрать то, что нужно для бэкапа»): тот же список, что и в TUI
    (ui.common.backup_source_entries, v1.4.1 — только Btrfs,
    группировка по устройству). Конфигурационные разделы отмечены
    заранее (сноска над списком), недоступные — с причиной, номер не
    переключает их. Номер — переключить отметку (можно несколько:
    «2 4»), a — все/ничего, Enter — продолжить, 0 — отмена.

    Если выбираемых разделов НЕТ — сообщается об этом и выбор сразу
    закрывается; Enter без отметок не продолжает, а напоминает, что
    нужно выбрать хотя бы один раздел (или 0 — отмена): заведомо
    провальная операция не начинается вовсе.

    Возвращает items_override [{name, source}] или None (отмена).
    """
    entries, discovery_errors = common.backup_source_entries(config)
    # ТЗ 1.4.7 §3.2: ошибки обнаружения subvolume не скрываются
    if discovery_errors:
        print("ВНИМАНИЕ: обнаружение subvolume не удалось для {} ФС — "
              "эти ФС в списке отсутствуют:".format(len(discovery_errors)))
        for error in discovery_errors:
            for line in common.wrap_text("  " + error, 70):
                print(line)
        print("Проверьте монтирование и права (запуск от root) или "
              "повторите выбор.")
    # по умолчанию отмечаются только ВЫБИРАЕМые записи
    # конфигурации ([–] не выглядит выбранным).
    flags = [bool(entry.get("from_config")) and
             entry.get("selectable", True) for entry in entries]
    _print_source_entries(entries, flags)
    selectable_total = sum(1 for e in entries if e.get("selectable", True))
    if selectable_total == 0:
        print("В системе нет доступных разделов Btrfs — копировать нечего.")
        return None
    while True:
        try:
            raw = input(
                "Номер — переключить отметку (можно несколько), "
                "a — все/ничего, Enter — продолжить, 0 — отмена: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if raw in ("0", "c", "C", "н", "Н", "отмена"):
            return None
        if raw == "":
            chosen = [entry for entry, flag in zip(entries, flags)
                      if flag and entry.get("selectable", True)]
            if not chosen:
                # пустой Enter не продолжает операцию — раньше
                # отсюда выходило «Отменено», а в TUI пустой выбор
                # уходил в пароль и падал уже при выполнении.
                print("Не выбран ни один раздел — отметьте номера "
                      "разделов или нажмите 0 для отмены.")
                continue
            break  # Enter — продолжить с текущими отметками
        if raw.lower() in ("a", "all", "все", "ф"):
            selectable_flags = [f for f, e in zip(flags, entries)
                                if e.get("selectable", True)]
            target = not all(selectable_flags)
            flags = [target if e.get("selectable", True) else False
                     for e in entries]
        else:
            for token in raw.replace(",", " ").replace(";", " ").split():
                if token.isdigit() and 1 <= int(token) <= len(entries):
                    index = int(token) - 1
                    if entries[index].get("selectable", True):
                        flags[index] = not flags[index]
                    else:
                        print("  раздел {} недоступен для выбора — "
                              "пропущен".format(entries[index]["name"]))
                else:
                    print("  нет такого номера: '{}' — пропущен".format(token))
        _print_source_entries(entries, flags)
    # chosen уже проверен на непустоту внутри цикла (пустой
    # Enter не выходит из цикла, а напоминает про выбор).
    chosen = [{"name": entry["name"], "source": entry["source"],
               "snapshot_dir": entry.get("snapshot_dir"),
               "local_backup_dir": entry.get("local_backup_dir")}
              for entry, flag in zip(entries, flags)
              if flag and entry.get("selectable", True)]
    print("Будут скопированы: " + ", ".join(e["name"] for e in chosen))
    return chosen


def interactive_restore(config):
    """Выбор источника: локальный архив / сетевой архив / снапшот."""
    sources = ["Локальный зашифрованный архив",
               "Сетевой зашифрованный архив",
               "Локальный read-only snapshot"]
    choice = dialogs.select_from_list("Источник восстановления:", sources)
    if choice is None:
        print("Отменено.")
        return
    if choice.startswith("Локальный зашифрованный"):
        restore_archive_flow(config, common.collect_local_archives(
            config.local_backup_dir(config.SNAPSHOT_DIR)))
    elif choice.startswith("Сетевой"):
        net_dir = Path(config.NETWORK_MOUNT) / config.NETWORK_DIRECTORY
        restore_archive_flow(config, common.collect_network_archives(net_dir),
                             network=True)
    else:
        restore_snapshot_flow(config)


def _choose_target_dir(default=None):
    prompt = "Целевой каталог (на файловой системе Btrfs)"
    if default:
        prompt += " [{}]".format(default)
    try:
        raw = input(prompt + ": ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return raw or default


def restore_archive_flow(config, candidates, network=False):
    if not candidates:
        print("Архивы не найдены.")
        return
    entries = [common.archive_line(p) for p in candidates]
    chosen = dialogs.select_from_list("Выберите архив:", entries)
    if chosen is None:
        print("Отменено.")
        return
    archive_path = candidates[entries.index(chosen)]
    target = _choose_target_dir()
    if not target:
        print("Отменено.")
        return
    # ТЗ 10: перед восстановлением интерфейс показывает состав операции —
    # выбранный архив, исходный subvolume, целевой каталог, алгоритм
    # сжатия и состояние проверки архива (до запуска btrfs receive)
    meta = metadata.read_metadata(archive_path) or \
        metadata.infer_archive_info(archive_path)
    print("Параметры восстановления:")
    print("  Архив: {} ({})".format(
        archive_path.name,
        "на сетевой шаре" if network else "локальный"))
    print("  Subvolume: {}".format(
        (meta or {}).get("subvolume_name") or "неизвестен (из имени архива)"))
    print("  Целевой каталог: {}".format(target))
    try:
        compression = compression_module.detect_compression(archive_path.name)
        print("  Сжатие: {} (определено по архиву)".format(compression))
    except Exception:  # noqa: BLE001 — ошибка имени покажется при операции
        print("  Сжатие: определить по имени не удалось")
    reference = restore_module.verify_archive_reference(archive_path)
    if reference:
        print("  Проверка архива: {} (ЦЕЛОСТНОСТЬ подтверждается "
              "расшифрованием)".format(reference))
    else:
        print("  Проверка архива: SHA-256 совпадает с метаданными")
    password = encryption_module.prompt_decryption_password(config.GPG_PASSWORD)
    try:
        dest = restore_module.receive_archive(
            archive_path, target,
            gpg_binary=config.GPG_BINARY, password=password,
        )
        # Сообщение об успехе печатается ТОЛЬКО после завершения
        # btrfs receive и проверки результата. Полностью
        # восстановленный subvolume принадлежит пользователю — его
        # удаление НЕ предлагается.
        print("Восстановление завершено: {}".format(dest))
    except BtrfsBackupError as exc:
        print("Ошибка: {}".format(exc))
        # Очистка предлагается ТОЛЬКО при неудачном receive и только для
        # объекта, созданного текущей операцией (partial_info с перечнем
        # preexisting; ТЗ 1.4.8.2 §3.1).
        partial_info = getattr(exc, "partial_info", None)
        if partial_info is not None:
            _offer_partial_cleanup(partial_info)
        raise


def restore_snapshot_flow(config):
    names = {item["name"] for item in
             config.normalize_subvolumes(config.SUBVOLUMES)[0]}
    groups = common.collect_snapshots(config.SNAPSHOT_DIR, names)
    flat = []
    for items in groups.values():
        flat.extend(items)
    if not flat:
        print("Снапшоты не найдены: {}".format(config.SNAPSHOT_DIR))
        return
    entries = [snap.name for snap in flat]
    chosen = dialogs.select_from_list("Выберите snapshot:", entries)
    if chosen is None:
        print("Отменено.")
        return
    snap = flat[entries.index(chosen)]
    ro = snapshots_module.subvolume_ro_property(snap.path)
    print("Снапшот: {} [{}]".format(
        snap.name,
        "read-only" if ro else ("записываемый" if ro is False else "?")))
    target = _choose_target_dir()
    if not target:
        print("Отменено.")
        return
    if not dialogs.confirm(
            "Будет создан записываемый снапшот в {}; исходный снапшот "
            "не изменяется. Продолжить?".format(target)):
        print("Отменено.")
        return
    dest = restore_module.restore_snapshot_writable(snap.path, target)
    print("Создан записываемый снапшот: {}".format(dest))


def _offer_partial_cleanup(partial_info):
    """
    Предложение удалить частичный результат НЕУДАВШЕГОСЯ восстановления
    (отдельная операция, ТЗ 11, ТЗ 1.4.8.2 §3.1).
    """
    from .restore_helpers import offer_partial_cleanup
    offer_partial_cleanup(partial_info, dialogs.confirm)


def show_local_copies(config):
    """
    Пункт «Локальные снапшоты» простого CLI (§4.3): вложенный выбор —
    снапшоты Btrfs / локальные архивы / назад (не новый пункт меню).
    """
    options = ["Снапшоты Btrfs", "Локальные архивы", "Назад"]
    choice = dialogs.select_from_list("Локальные снапшоты и архивы:", options)
    if choice is None or choice == "Назад":
        return
    if choice == "Снапшоты Btrfs":
        show_snapshots(config)
    else:
        show_archives(config)


def show_snapshots(config):
    """Список снапшотов Btrfs (§4.3) — общий построитель ui.common."""
    for line in common.snapshot_view_lines(config):
        print(line)


def show_archives(config):
    """
    Список локальных архивов (§4.3): индивидуальные каталоги источников
    учитываются наряду с глобальным; дубликаты не показываются. Если
    каталоги не определяются из конфигурации — запрос каталога у
    пользователя.
    """
    dirs = common.archive_dirs(config)
    if not dirs:
        print("Каталоги архивов не определяются из конфигурации "
              "(источники и LOCAL_BACKUP_DIR не заданы).")
        try:
            manual = input(
                "Каталог локальных архивов для просмотра "
                "(абсолютный путь, Enter — отмена): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not manual.startswith("/"):
            print("Отменено.")
            return
        dirs = [manual]
    for line in common.archive_view_lines(config, dirs=dirs):
        print(line)


# ============================================
# Удаление: интерактивные пути команд main.py delete-archives /
# delete-snapshots (главное меню осталось из 4 пунктов; отдельные
# команды удаления остаются).
# ============================================

def _choose_indices_cli(entries, allow_all_hint=True):
    """
    Множественный выбор номерами: "1,3-5", "a"/"all" — все.
    Возвращает список индексов, [] — ничего не выбрано, None — отмена.
    """
    hint = "Номера через запятую/пробел или диапазоны"
    if allow_all_hint:
        hint += ", 'a' — все"
    try:
        raw = input("{} [Enter — отмена]: ".format(hint)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not raw:
        return None
    if raw.lower() in ("a", "all", "все"):
        return list(range(len(entries)))
    chosen = []
    for token in raw.replace(";", " ").replace(",", " ").split():
        if "-" in token and not token.startswith("-"):
            bounds = token.split("-", 1)
            if all(b.isdigit() for b in bounds) and len(bounds) == 2:
                lo, hi = int(bounds[0]), int(bounds[1])
                if 1 <= lo <= hi <= len(entries):
                    chosen.extend(range(lo - 1, hi))
                    continue
        if token.isdigit() and 1 <= int(token) <= len(entries):
            chosen.append(int(token) - 1)
            continue
        print("Некорректный номер: '{}' — пропущен".format(token))
    return sorted(set(i for i in chosen if 0 <= i < len(entries)))


def _confirm_or_abort(text):
    """Подтверждение Y/N (одна клавиша); False — отказ."""
    return dialogs.confirm(text)


def delete_archives_cli(config):
    """Удаление выбранных локальных архивов вместе с метаданными."""
    from core.network import delete_local_archive
    backup_dir = Path(config.local_backup_dir(config.SNAPSHOT_DIR))
    archives = common.collect_local_archives(backup_dir)
    if not archives:
        print("Локальные архивы не найдены: {}".format(backup_dir))
        return
    entries = [common.archive_line(p) for p in archives]
    print("Локальные архивы ({}):".format(backup_dir))
    for index, entry in enumerate(entries, 1):
        print("  {:3}. {}".format(index, entry))
    selected = _choose_indices_cli(entries)
    if selected is None:
        print("Отменено.")
        return
    if not selected:
        print("Не выбран ни один архив.")
        return
    selected_paths = [archives[i] for i in selected]
    total_bytes = common.archive_size(selected_paths)
    touched = common.archive_counts_by_subvolume(selected_paths)
    remaining = common.archive_counts_by_subvolume(archives)
    touches_last = any(
        remaining.get(name, 0) <= count
        for name, count in touched.items())
    print("К удалению ({} шт., {}):".format(
        len(selected_paths), human_size(total_bytes)))
    for path in selected_paths:
        print("  " + path.name)
    if not _confirm_or_abort(
            "Удалить перечисленные архивы БЕЗВОЗВРАТНО (включая метаданные)?"):
        print("Отменено.")
        return
    if common.deletion_needs_second_confirm(
            len(archives), len(selected_paths), touches_last):
        print("ВНИМАНИЕ: удаляются ВСЕ архивы или последняя копия "
              "subvolume — это может лишить единственной резервной копии.")
        if not _confirm_or_abort("Подтвердите удаление еще раз:"):
            print("Отменено (второе подтверждение).")
            return
    deleted = 0
    for path in selected_paths:
        delete_local_archive(path)
        deleted += 1
    print("Удалено архивов: {} (освобождено {})".format(
        deleted, human_size(total_bytes)))
    log.info("CLI: удалено локальных архивов %d (%s)", deleted,
             human_size(total_bytes))


def delete_snapshots_cli(config):
    """Удаление выбранных снапшотов (btrfs subvolume delete, ТЗ 18)."""
    names = {item["name"] for item in
             config.normalize_subvolumes(config.SUBVOLUMES)[0]}
    groups = common.collect_snapshots(config.SNAPSHOT_DIR, names)
    flat = []
    for subvol in sorted(groups):
        flat.extend(groups[subvol])
    if not flat:
        print("Снапшоты не найдены: {}".format(config.SNAPSHOT_DIR))
        return
    entries = []
    for snap in flat:
        ro = snapshots_module.subvolume_ro_property(snap.path)
        state = "read-only" if ro else \
            ("записываемый" if ro is False else "состояние ?")
        entries.append("{} [{}]".format(snap.name, state))
    print("Снапшоты ({}):".format(config.SNAPSHOT_DIR))
    for index, entry in enumerate(entries, 1):
        print("  {:3}. {}".format(index, entry))
    selected = _choose_indices_cli(entries)
    if selected is None:
        print("Отменено.")
        return
    if not selected:
        print("Не выбран ни один снапшот.")
        return
    touched = common.counts_by_subvolume(flat[i] for i in selected)
    remaining = common.counts_by_subvolume(flat)
    touches_last = any(
        remaining.get(name, 0) <= count
        for name, count in touched.items())
    print("К удалению ({} шт.):".format(len(selected)))
    for i in selected:
        print("  " + flat[i].name)
    if not _confirm_or_abort(
            "Удалить перечисленные снапшоты (btrfs subvolume delete)?"):
        print("Отменено.")
        return
    if common.deletion_needs_second_confirm(
            len(flat), len(selected), touches_last):
        print("ВНИМАНИЕ: удаляются ВСЕ снапшоты или последний снапшот "
              "subvolume (архивы останутся).")
        if not _confirm_or_abort("Подтвердите удаление еще раз:"):
            print("Отменено (второе подтверждение).")
            return
    deleted, failed = 0, 0
    for i in selected:
        snap = flat[i]
        try:
            snapshots_module.delete_snapshot(
                snap.path, config.SNAPSHOT_DIR)
            deleted += 1
        except BtrfsBackupError as exc:
            failed += 1
            print("  Не удалось удалить {}: {}".format(snap.name, exc))
    print("Удалено снапшотов: {}{}".format(
        deleted, "; с ошибками: {}".format(failed) if failed else ""))
    log.info("CLI: удалено снапшотов %d (ошибок: %d)", deleted, failed)
