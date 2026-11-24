"""
Общие помощники интерфейсов (CLI и TUI): сбор и выбор архивов,
подписи статусов. Бизнес-логики не содержит.
"""

import os
import re
from pathlib import Path

from core import compression as compression_module
from core import metadata
from core import snapshots as snapshots_module
from core.logging_setup import get_logger

log = get_logger("ui.common")

STATUS_LABELS = {
    metadata.STATUS_CREATED: "created",
    metadata.STATUS_VERIFIED: "verified",
    metadata.STATUS_UPLOAD_PENDING: "upload-pending",
    metadata.STATUS_UPLOADING: "uploading",
    metadata.STATUS_UPLOADED: "uploaded",
    metadata.STATUS_UPLOAD_FAILED: "upload-failed",
    metadata.STATUS_RESTORE_FAILED: "restore-failed",
}


def archive_status_label(path):
    """Краткая подпись статуса архива по метаданным (или '?')."""
    meta = metadata.read_metadata(path)
    if meta is None:
        meta = metadata.infer_archive_info(path)
    if meta is None:
        return "?"
    return STATUS_LABELS.get(meta.get("status"), str(meta.get("status")))


def archive_compression_label(path):
    """Алгоритм сжатия архива по расширению."""
    try:
        return compression_module.detect_compression(Path(path).name)
    except Exception:  # noqa: BLE001
        return "?"


def collect_local_archives(backup_dir):
    """Список локальных архивов (Path), отсортированный по имени."""
    directory = Path(backup_dir)
    if not directory.is_dir():
        return []
    return sorted(
        entry for entry in directory.glob("*.gpg") if entry.is_file()
    )


def collect_network_archives(net_dir):
    """Список сетевых архивов (Path); сетевые каталоги не изменяются."""
    directory = Path(net_dir)
    if not directory.is_dir():
        return []
    return sorted(
        entry for entry in directory.glob("*.gpg") if entry.is_file()
    )


def archive_line(path):
    """Однострочное описание архива для списков."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    from core.process import human_size
    return "{}  {:>9}  {}".format(
        path.name, human_size(size), archive_status_label(path))


def collect_snapshots(snapshot_dir, subvolume_names=None):
    """{имя: [SnapshotInfo]} для интерфейсов."""
    return snapshots_module.list_snapshots(snapshot_dir, subvolume_names)


def wrap_text(text, width=76):
    """
    Перенос длинных строк по ширине.

    Кириллица и пути переносятся по словам; слово длиннее ширины
    разрывается принудительно (пути без пробелов не теряются).
    Переводы строк исходного текста сохраняются.
    """
    result = []
    width = max(10, int(width))
    for line in str(text).splitlines() or [""]:
        if not line:
            result.append("")
            continue
        remaining = line
        while len(remaining) > width:
            chunk = remaining[:width]
            cut = chunk.rfind(" ")
            if cut <= 0:
                cut = width  # длинное слово без пробелов — принудительный разрыв
            result.append(chunk[:cut].rstrip())
            remaining = remaining[cut:].lstrip()
        result.append(remaining)
    return result


def network_mount_status(config):
    """(доступна, сообщение) для строки статуса интерфейса."""
    if not getattr(config, "NETWORK_ENABLED", True):
        return False, "DISABLED"
    network_mount = getattr(config, "NETWORK_MOUNT", None)
    if not network_mount:
        return False, "NOT CONFIGURED"
    from core import network as network_module
    try:
        network_module.check_network_mount(
            network_mount,
            expected_fs_type=getattr(config, "EXPECTED_NETWORK_FS_TYPE", None),
            check=getattr(config, "CHECK_NETWORK_MOUNT", True),
        )
        return True, "OK"
    except Exception as exc:  # noqa: BLE001
        return False, str(getattr(exc, "args", [None])[0] if exc.args else exc)


# ============================================
# Удаление архивов/снапшотов
# ============================================

def deletion_needs_second_confirm(count_total, count_selected, touches_last):
    """
    True, если удаление требует ПОВТОРНОГО подтверждения (лучше
    оставить лишнюю копию, чем потерять единственную):
    удаляются ВСЕ объекты или затрагивается последняя копия subvolume.
    """
    if count_total and count_selected == count_total:
        return True
    return bool(touches_last)


def counts_by_subvolume(values):
    """{subvolume: количество} — значения с атрибутом subvolume."""
    counts = {}
    for item in values:
        name = getattr(item, "subvolume", None) or "?"
        counts[name] = counts.get(name, 0) + 1
    return counts


def archive_counts_by_subvolume(paths):
    """{subvolume: количество} для списка архивов (по метаданным)."""
    counts = {}
    for path in paths:
        meta = metadata.read_metadata(path) or \
            metadata.infer_archive_info(path)
        name = (meta or {}).get("subvolume_name") or "?"
        counts[name] = counts.get(name, 0) + 1
    return counts


def archive_size(paths):
    """Суммарный размер файлов списка (байты; ошибки чтения — 0)."""
    total = 0
    for path in paths:
        try:
            total += Path(path).stat().st_size
        except OSError:
            pass
    return total


# ============================================
# Выбор источников backup: единый список для TUI и простого CLI.
# Только Btrfs (разделы других файловых систем источником быть не
# могут); происхождение записи показывается сноской над списком, а не
# пометкой в каждой строке; группировка по устройству с деревом
# subvolume («/dev/sda1[/@home]») — при нескольких дисках с Btrfs
# понятно, к какому диску относится каждый subvolume. Источник, под
# которым НЕТ монтирования Btrfs, помечается НЕвыбираемым с причиной —
# заведомо провальная операция не начинается вовсе.
# ============================================

def backup_source_entries(config):
    """
    Записи экрана выбора источников резервного копирования (только Btrfs;
    ТЗ 1.4.7 §3.4, §8).

    Основа списка — ФАКТИЧЕСКОЕ обнаружение (core.snapshots.discover_sources):
    каждая запись привязана к своей файловой системе (fs_uuid) и помечена
    при необходимости как корень ФС (fs_root, subvolid=5). Записи
    конфигурации сверяются с обнаружением по каноническому пути:
      - совпавшие — обычные выбираемые записи, отмечены по умолчанию;
      - НЕ совпавшие — показываются с предупреждением и НЕ включаются
        автоматически в резервирование; выбираемыми они остаются
        только если путь реально существует, смонтирован как Btrfs и
        авторитетно является subvolume.

    Возвращает (entries, discovery_errors): entries — [{name, source,
    from_config, selectable, device, notation, fs_uuid, fs_root[, origin,
    snapshot_dir, local_backup_dir]}], discovery_errors — [строки]: причины
    неудачного обнаружения subvolume.

    Порядок списка = порядок отображения: группы устройств, внутри —
    обычные пути, точечные в конце (см. group_source_entries).
    """
    items, _errors = config.normalize_subvolumes(config.SUBVOLUMES)
    mount_lines = snapshots_module.read_mount_lines()
    try:
        detection = snapshots_module.discover_sources(
            exclude_dirs=_snapshot_exclude_dirs(config),
            mount_lines=mount_lines)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось обнаружить файловые системы Btrfs")
        detection = {"sources": [], "errors": []}
    detected = detection["sources"]
    discovery_errors = list(detection["errors"])
    detected_name_counts = {}
    for discovered in detected:
        discovered_name = str(discovered.get("name") or "")
        detected_name_counts[discovered_name] = (
            detected_name_counts.get(discovered_name, 0) + 1)
    detected_by_real = {}
    for sub in detected:
        try:
            detected_by_real[os.path.realpath(sub["source"])] = sub
        except OSError:
            continue

    entries = []
    used_names = set()
    used_sources = set()
    used_reals = set()

    def unique_name(base, source=None, disambiguate_path=False):
        """Сохраняет понятное имя; при коллизии различает пути, не `_2`."""
        name = base
        if disambiguate_path and source:
            parent = Path(str(source)).parent.name
            if parent:
                name = "{}_{}".format(base, parent)
        counter = 2
        candidate = name
        while candidate in used_names:
            candidate = "{}_{}".format(name, counter)
            counter += 1
        used_names.add(candidate)
        return candidate

    # --- записи конфигурации: сверка с обнаружением --------
    for item in items:
        source = item["source"]
        source_key = str(source).rstrip("/") or "/"
        name = unique_name(item["name"])
        used_sources.add(source_key)
        entry = {"name": name, "source": source,
                 "snapshot_dir": item.get("snapshot_dir"),
                 "local_backup_dir": item.get("local_backup_dir"),
                 "from_config": True, "selectable": True}
        real = _resolve_or_none(source)
        if real:
            used_reals.add(real)
        match = detected_by_real.get(real) if real else None
        if match is not None:
            entry.update(_entry_with_device(entry, mount_lines))
            entry["fs_uuid"] = match.get("fs_uuid")
            entry["fs_root"] = bool(match.get("fs_root"))
            entry["subvolume_uuid"] = match.get("subvolume_uuid")
        else:
            # ТЗ §8: путь конфигурации не соответствует обнаружению —
            # предупреждение и НЕ включать автоматически.
            entry["from_config"] = False
            entry["fs_uuid"] = None
            entry["fs_root"] = False
            _apply_unmatched_info(entry, mount_lines)
        entries.append(entry)

    # --- обнаруженные subvolume, отсутствующие в конфигурации --------
    for sub in detected:
        real = None
        try:
            real = os.path.realpath(sub["source"])
        except OSError:
            continue
        source_key = str(sub["source"]).rstrip("/") or "/"
        if real in used_reals or source_key in used_sources:
            continue
        used_reals.add(real)
        used_sources.add(source_key)
        entry = {"name": unique_name(
                     sub["name"], sub["source"],
                     disambiguate_path=(
                         detected_name_counts.get(str(sub["name"]), 0) > 1
                         or str(sub["name"]) in used_names)),
                 "source": sub["source"],
                 "from_config": False, "selectable": True}
        entry.update(_entry_with_device(entry, mount_lines))
        entry["fs_uuid"] = sub.get("fs_uuid")
        entry["fs_root"] = bool(sub.get("fs_root"))
        entry["subvolume_uuid"] = sub.get("subvolume_uuid")
        entries.append(entry)

    _append_partition_entries(config, entries, used_names, used_sources,
                              _snapshot_exclude_dirs(config))
    # Порядок списка = порядок отображения: группировка выполняется по
    # текущему списку, затем записи переставляются в том же порядке.
    entries[:] = [entry for group in group_source_entries(entries)
                  for _index, entry in group["members"]]
    return entries, discovery_errors


def _snapshot_exclude_dirs(config):
    """Каталоги снапшотов/архивов, не предлагаемые как источники."""
    exclude_dirs = []
    if config.SNAPSHOT_DIR:
        exclude_dirs.append(config.SNAPSHOT_DIR)
        try:
            exclude_dirs.append(config.local_backup_dir(config.SNAPSHOT_DIR))
        except Exception:  # noqa: BLE001
            pass
    return exclude_dirs


def _apply_unmatched_info(entry, mount_lines):
    """
    Сведения о записи конфигурации, не найденной при обнаружении: точная причина и выбираемость. Выбираемой запись
    остается только при СООТВЕТСТВИИ фактам: путь существует, находится
    на смонтированной Btrfs и авторитетно является subvolume.
    """
    source = entry.get("source")
    notation = None
    try:
        notation = snapshots_module.btrfs_notation(source,
                                                   mount_lines=mount_lines)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось определить устройство для %s", source)
    entry["notation"] = notation
    entry["device"] = notation.split("[", 1)[0] if notation else None
    path = Path(source)
    if not path.exists():
        entry["selectable"] = False
        entry["origin"] = ("не найден при обнаружении и не существует — "
                           "выбрать нельзя")
        return
    if notation is None:
        entry["selectable"] = False
        entry["origin"] = ("не найден при обнаружении; нет точки "
                           "монтирования Btrfs — выбрать нельзя")
        return
    if not snapshots_module.is_subvolume(source):
        entry["selectable"] = False
        entry["origin"] = ("не найден при обнаружении; не является "
                           "Btrfs subvolume — выбрать нельзя")
        return
    entry["selectable"] = True
    entry["origin"] = "не найден при обнаружении — проверьте путь"


def _entry_with_device(entry, mount_lines):
    """
    Дополняет запись выбора устройством и обозначением.

    v1.4.2: если у пути нет монтирования Btrfs (обозначение определить
    нельзя — notation None), запись помечается НЕвыбираемой с причиной.
    Так на ВМ без Btrfs источник конфигурации «/» показывается как [–]
    «нет точки монтирования Btrfs — выбрать нельзя», а не как [x],
    пускающий по Enter в заведомо провальную операцию.
    """
    notation = None
    try:
        notation = snapshots_module.btrfs_notation(
            entry["source"], mount_lines=mount_lines)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось определить устройство для %s",
                      entry["source"])
    entry["notation"] = notation
    entry["device"] = notation.split("[", 1)[0] if notation else None
    if notation is None and entry.get("selectable", True):
        entry["selectable"] = False
        entry["origin"] = "нет точки монтирования Btrfs — выбрать нельзя"
    return entry


def _append_partition_entries(config, entries, used_names, used_sources,
                              exclude_dirs):
    """
    Дополняет список разделами Btrfs, которые НЕ видны через свои точки
    монтирования (не-Btrfs разделы не показываются вовсе).
    """
    try:
        partitions = snapshots_module.list_partitions()
    except Exception:  # noqa: BLE001
        log.exception("Не удалось получить список разделов дисков")
        partitions = []
    if not partitions:
        return
    try:
        excluded_real = {str(Path(d).resolve()) for d in exclude_dirs}
    except (OSError, ValueError):
        excluded_real = {str(d) for d in exclude_dirs}
    for part in partitions:
        if part["fstype"] != "btrfs":
            continue  # не Btrfs — в списке выбора не нужен
        origin = _partition_origin(part, used_sources, excluded_real)
        if origin is None:
            continue  # раздел уже представлен своей точкой монтирования
        name, counter = part["name"], 2
        while name in used_names:
            name = "{}_{}".format(part["name"], counter)
            counter += 1
        used_names.add(name)
        entries.append({"name": name, "source": part["device"],
                        "from_config": False, "selectable": False,
                        "device": part["device"], "notation": None,
                        "origin": origin})


def _partition_origin(part, used_sources, excluded_real):
    """
    Причина недоступности раздела Btrfs для backup (строка) или None,
    если раздел уже присутствует в списке источников (его точка
    монтирования совпадает с уже перечисленным subvolume).
    """
    mount_points = part.get("mount_points") or []
    if part.get("mounted") and any(
            (mp.rstrip("/") or "/") in used_sources for mp in mount_points):
        return None
    if not part.get("mounted"):
        return "не смонтирован — выбрать нельзя"
    resolved = [_resolve_or_none(mp) for mp in mount_points]
    resolved = [r for r in resolved if r]
    if resolved and all(_inside(r, excluded_real) for r in resolved):
        return "служебный каталог снапшотов/архивов — выбрать нельзя"
    return "нет доступных subvolume — выбрать нельзя"


# ============================================
# Группировка по устройству и строки экрана выбора
# Формат (предложение пользователя):
#   /dev/sda1:
#   [x] /             /dev/sda1[/@]
#   [ ] ├─/home       /dev/sda1[/@home]
#   [ ] └─/.snapshots /dev/sda1[/@snapshots]
# ============================================

def _natural_key(text):
    """Ключ сортировки с числами по-человечески: sda2 < sda10."""
    return tuple((0, int(part)) if part.isdigit() else (1, part)
                 for part in re.split(r"(\d+)", str(text)))


def _entry_sort_key(pair):
    """
    Ключ сортировки записей внутри группы: обычные пути сначала
    (по алфавиту), пути с точкой в имени (/.snapshots и подобные
    служебные) — в конце группы, как в примере пользователя:
    /, /home, /var/cache, /var/log, /.snapshots.
    """
    source = str(pair[1].get("source"))
    base = source.rstrip("/").rpartition("/")[2]
    return (1 if base.startswith(".") else 0, source)


def group_source_entries(entries):
    """
    Записи выбора, сгруппированные по устройству:
    [{device, members: [(index, entry), ...]}]. Порядок групп — по имени
    устройства (естественная сортировка; без устройства — в конце),
    внутри группы — обычные пути по алфавиту, пути с точкой в имени —
    в конце (см. _entry_sort_key). Сортировка копий не меняет entries.
    """
    by_device = {}
    for index, entry in enumerate(entries):
        by_device.setdefault(entry.get("device"), []).append((index, entry))
    groups = []
    for device in sorted(by_device, key=lambda d: (
            (1, ()) if d is None else (0, _natural_key(d)))):
        members = sorted(by_device[device], key=_entry_sort_key)
        groups.append({"device": device, "members": members})
    return groups


def source_rows(entries):
    """
    Строки экрана выбора (TUI и простой CLI рендерят единообразно):
    [{"kind": "header", "text": "/dev/sda1:"}, {"kind": "entry",
    "index": <номер в entries>, "prefix": "" | "├─" | "└─"}...].
    Префиксы дерева: первая строка группы — без префикса, средние —
    '├─', последняя — '└─' (в группе из одной строки префикса нет);
    соединитель примыкает к пути без пробела: '├─/home'.
    """
    rows = []
    for group in group_source_entries(entries):
        header = "{}:".format(group["device"]) if group["device"] \
            else "(точка монтирования не найдена):"
        rows.append({"kind": "header", "text": header})
        members = group["members"]
        for position, (index, _entry) in enumerate(members):
            if position == 0 or len(members) == 1:
                prefix = ""
            elif position == len(members) - 1:
                prefix = "└─"
            else:
                prefix = "├─"
            rows.append({"kind": "entry", "index": index,
                         "prefix": prefix})
    return rows


def source_entry_suffix(entry):
    """Правая колонка строки выбора: обозначение устройства[subvol] или
    причина недоступности."""
    if not entry.get("selectable", True):
        return entry.get("origin") or ""
    return entry.get("notation") or ""


def source_label_width(entries):
    """Ширина левой колонки (метка с префиксом дерева) для выравнивания."""
    width = 0
    for row in source_rows(entries):
        if row["kind"] != "entry":
            continue
        label = row["prefix"] + str(entries[row["index"]].get("source"))
        width = max(width, len(label))
    return width


def source_line(entries, row, label_width=0):
    """
    (метка, правая колонка) строки выбора: метка — префикс дерева +
    путь, дополненная до общей ширины; правая колонка — обозначение
    'устройство[subvol]' или причина недоступности.
    """
    entry = entries[row["index"]]
    label = row["prefix"] + str(entry.get("source"))
    suffix = source_entry_suffix(entry)
    if suffix and label_width:
        label = label.ljust(label_width)
    return label, suffix


def _resolve_or_none(path):
    try:
        return str(Path(path).resolve())
    except (OSError, ValueError, RuntimeError):
        return None


def _inside(path, dirs):
    """True, если path совпадает с одним из dirs или находится внутри."""
    path = str(path).rstrip("/")
    for directory in dirs:
        directory = str(directory).rstrip("/")
        if path == directory or path.startswith(directory + "/"):
            return True
    return False


# ============================================
# Просмотр локальных копий (§4.3): общие построители для TUI и CLI
# ============================================

def _mounted_btrfs_snapshot_dirs():
    """Обычные каталоги снапшотов на всех смонтированных Btrfs.

    Автоматически обнаруживаем только известные стандартные имена на
    верхнем уровне точки монтирования. Произвольные пользовательские пути
    добавляются из конфигурации; рекурсивный обход файловых систем не
    выполняется.
    """
    candidates = []
    try:
        mounts = snapshots_module.btrfs_mounts(
            snapshots_module.read_mount_lines())
    except Exception:  # noqa: BLE001
        log.exception("Не удалось обнаружить точки монтирования Btrfs")
        return candidates
    for mount_point, _device, _subvol in mounts:
        base = Path(mount_point)
        for dirname in (".snapshots", ".snapshot"):
            candidate = base / dirname
            try:
                if candidate.is_dir() and snapshots_module.btrfs_notation(
                        candidate) is not None:
                    candidates.append(str(candidate))
            except OSError:
                continue
    return candidates


def snapshot_dirs(config):
    """Каталоги снапшотов из конфигурации и стандартных путей всех Btrfs."""
    candidates = []
    try:
        items, _errors = config.normalize_subvolumes(config.SUBVOLUMES)
        for item in items:
            candidates.append(item.get("snapshot_dir"))
    except Exception:  # noqa: BLE001
        pass
    candidates.append(getattr(config, "SNAPSHOT_DIR", None))
    candidates.extend(_mounted_btrfs_snapshot_dirs())
    return _dedupe_dirs(candidates)


def archive_dirs(config):
    """Каталоги архивов из конфигурации и стандартных путей всех Btrfs."""
    try:
        from core.backup import _local_backup_dirs
        candidates = list(_local_backup_dirs(config))
    except Exception:  # noqa: BLE001
        candidates = []
    # Для каждого найденного каталога снапшотов учитываем стандартный
    # дочерний каталог backup. Существование проверяется при просмотре.
    for snapshot_dir in snapshot_dirs(config):
        candidates.append(str(Path(snapshot_dir) / "backup"))
    return _dedupe_dirs(candidates)

def _dedupe_dirs(candidates):
    dirs = []
    seen = set()
    for path in candidates:
        if not path:
            continue
        try:
            real = str(Path(path).resolve())
        except (OSError, ValueError):
            real = str(path)
        if real in seen:
            continue
        seen.add(real)
        dirs.append(path)
    return dirs


def _device_label(directory):
    """Краткая метка Btrfs-устройства для группировки локальных копий."""
    try:
        notation = snapshots_module.btrfs_notation(directory)
        if notation:
            return notation.split("[", 1)[0]
    except Exception:  # noqa: BLE001
        pass
    return "устройство не определено"


def snapshot_view_lines(config):
    """
    Строки списка снапшотов Btrfs (§4.3): источник, имя, состояние
    read-only и дата (метка времени — часть имени снапшота, достоверна).
    """
    from core import snapshots as snapshots_module
    from core import BtrfsBackupError
    dirs = snapshot_dirs(config)
    if not any(Path(str(d)).is_dir() for d in dirs):
        return ["Каталоги снапшотов не заданы в конфигурации"]
    lines = []
    seen_devices = set()
    for directory in dirs:
        groups = collect_snapshots(directory, None)
        device = _device_label(directory)
        if device not in seen_devices:
            if lines:
                lines.append("")
            lines.append("Устройство {}".format(device))
            seen_devices.add(device)
        count = sum(len(items) for items in groups.values())
        lines.append("  Каталог {} (снапшотов: {}):".format(directory, count))
        if not groups:
            lines.append("  (нет)")
            continue
        for name in sorted(groups):
            for snap in groups[name]:
                try:
                    ro = snapshots_module.subvolume_ro_property(snap.path)
                except BtrfsBackupError:
                    ro = None
                state = "read-only" if ro else (
                    "записываемый" if ro is False else "неизвестно")
                when = "нет данных"
                if snap.timestamp:
                    ts = str(snap.timestamp)
                    when = "{} {}:{}".format(
                        ts[:10], ts[11:13], ts[13:15]) \
                        if len(ts) >= 14 else ts
                # Компактная строка (влезает в 80 колонок терминала)
                lines.append("  {} [{}] дата: {} (источник {})".format(
                    snap.name, state, when, name))
    return lines or ["Снапшоты не найдены"]


def archive_view_entry(path):
    """
    Строки одного архива списка (§4.3): имя; дата, размер, сжатие;
    источник, состояние проверки; сетевая отправка. Отсутствующие или
    поврежденные метаданные — честное «нет данных», без фиктивного
    статуса. Каждая строка укладывается в 80 колонок терминала.
    """
    from core import metadata as metadata_module
    from core.process import human_size
    path = Path(path)
    meta = metadata_module.read_metadata(path)
    if meta is None:
        meta = metadata_module.infer_archive_info(path)
    if meta is None:
        source = when = compression = "нет данных"
        check_state = net_state = "нет данных"
    else:
        source = meta.get("subvolume_name") or "нет данных"
        when = "нет данных"
        created = meta.get("created")
        if isinstance(created, str) and created.strip():
            # ISO 8601 metadata uses hyphens in YYYY-MM-DD; checking
            # created[:10].isdigit() incorrectly rejected every valid date.
            try:
                from datetime import datetime
                parsed_created = datetime.fromisoformat(
                    created.strip().replace("Z", "+00:00"))
                when = parsed_created.strftime("%Y-%m-%d %H:%M")
            except (TypeError, ValueError):
                when = "нет данных"
        if when == "нет данных":
            # Legacy/missing/broken sidecar: recover timestamp from the
            # archive filename rather than displaying a false absence.
            try:
                parsed_name = metadata_module.parse_archive_name(path.name)
                stamp = parsed_name.get("timestamp") if parsed_name else None
                if stamp and len(stamp) == 16:
                    when = "{} {}:{}".format(
                        stamp[:10], stamp[11:13], stamp[13:15])
            except (AttributeError, TypeError, ValueError):
                pass
        compression = meta.get("compression") or \
            archive_compression_label(path)
        if compression == "?":
            compression = "нет данных"
        label = archive_status_label(path)
        if label == "created":
            check_state = "не проводилась"
        elif label == "?":
            check_state = "нет данных"
        else:
            check_state = "пройдена (SHA-256)"
        net_map = {
            "uploaded": "отправлен (подтверждено)",
            "upload-pending": "ожидает отправки",
            "uploading": "отправляется",
            "upload-failed": "ошибка отправки",
            "verified": "не отправлен (локальный)",
            "created": "не отправлен (локальный)",
            "restore-failed": "не отправлен (локальный)",
        }
        net_state = net_map.get(label, "нет данных")
    try:
        size = human_size(path.stat().st_size)
    except OSError:
        size = "нет данных"
    return [
        "  {}".format(path.name),
        "      дата: {} | размер: {} | сжатие: {}".format(
            when, size, compression),
        "      источник: {} | проверка: {}".format(source, check_state),
        "      сеть: {}".format(net_state),
    ]


def archive_view_lines(config, dirs=None):
    """Строки списка локальных архивов по каждому каталогу (§4.3)."""
    if dirs is None:
        dirs = archive_dirs(config)
    lines = []
    seen_files = set()
    seen_devices = set()
    for directory in dirs:
        archives = collect_local_archives(directory)
        device = _device_label(directory)
        if device not in seen_devices:
            if lines:
                lines.append("")
            lines.append("Устройство {}".format(device))
            seen_devices.add(device)
        lines.append("  Каталог {} (архивов: {}):".format(
            directory, len(archives)))
        if not archives:
            lines.append("  (нет)")
            continue
        for path in archives:
            try:
                real = str(path.resolve())
            except (OSError, ValueError):
                real = str(path)
            if real in seen_files:
                continue  # тот же объект через другой путь
            seen_files.add(real)
            lines.extend(archive_view_entry(path))
    return lines
