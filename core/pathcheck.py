"""
Единая проверка расположения путей источников.

Один механизм используется всеми режимами (TUI, простой CLI, backup):
  - источник существует и является допустимым Btrfs subvolume;
  - каталог снапшотов на ТОЙ ЖЕ файловой системе Btrfs (UUID совпадает);
  - каталог снапшотов доступен для записи;
  - каталог снапшотов НЕ внутри исходного subvolume (иначе снапшот
    рекурсивно включает прошлые снапшоты). Исключение — отдельный
    subvolume: вложенные subvolume не попадают в снапшот и в
    `btrfs send`, поэтому классическая схема (источник "/", каталог
    "/.snapshots"-subvolume) допустима;
  - каталог локальных архивов доступен для записи;
  - пути не образуют циклическую структуру резервирования;
  - фактические UUID файловых систем совпадают там, где требуется.

Обычный каталог на той же файловой системе Btrfs допустим как каталог
снапшотов — отдельный subvolume не требуется.

Интерфейсные режимы используют эти же проверки ДО запуска операции:
проблемные источники показываются с причиной и могут быть исключены. Ошибки проверки не изменяют файловую систему (создание
каталогов — только по явному запросу create_dirs при старте операции).
"""

from pathlib import Path

from . import SnapshotError
from . import environment
from .logging_setup import get_logger
from . import snapshots as snapshots_module

log = get_logger("pathcheck")


def _resolve(path):
    """Канонический путь или None (битый симлинк, недоступен)."""
    try:
        return str(Path(path).resolve())
    except (OSError, ValueError, RuntimeError):
        return None


def _normalize(path):
    """Путь без завершающего слэша ('/' остается '/')."""
    text = str(path).rstrip("/")
    return text or "/"


def equal(a, b) -> bool:
    """True, если пути канонически совпадают."""
    ra, rb = _resolve(a), _resolve(b)
    if ra is None or rb is None:
        return _normalize(a) == _normalize(b)
    return ra == rb


def under(path, prefix) -> bool:
    """True, если path строго внутри prefix (не совпадает с ним)."""
    path = _resolve(path) or _normalize(path)
    prefix = _resolve(prefix) or _normalize(prefix)
    if prefix == "/":
        return path != "/" and path.startswith("/")
    return path != prefix and path.startswith(prefix.rstrip("/") + "/")


def _is_subvolume_or_known(path):
    """
    (на_btrfs, является_subvolume): является_subvolume — только
    однозначное «да» (btrfs subvolume show успешен); None — определить
    не удалось (btrfs недоступен), решение в таком случае fail-closed.
    """
    on_btrfs = snapshots_module.is_btrfs_path(path)
    if not on_btrfs:
        return False, False
    path_obj = Path(path)
    if not path_obj.exists():
        return True, False
    return True, bool(snapshots_module.is_subvolume(path))


def check_source(item, cfg, *, create_dirs=False):
    """
    Проверка одного источника — все правила расположения.

    item: {name, source[, snapshot_dir, local_backup_dir]}; cfg — профиль
    (AppConfig или модуль config). create_dirs=True разрешает создавать
    отсутствующие каталоги снапшотов/архивов (вызывается только при
    старте операции).

    Возвращает {"name", "source", "ok", "errors", "warnings",
    "snapshot_dir", "local_backup_dir"}. ok=False означает: источник
    не участвует в операции (fail-closed, ТЗ 8/14).
    """
    name = (item or {}).get("name") or "?"
    source = (item or {}).get("source")
    errors = []
    warnings = []

    snapshot_dir = (item or {}).get("snapshot_dir") or getattr(
        cfg, "SNAPSHOT_DIR", None)
    local_backup_dir = (item or {}).get("local_backup_dir") or None

    result = {
        "name": name,
        "source": source,
        "ok": False,
        "errors": errors,
        "warnings": warnings,
        "snapshot_dir": snapshot_dir,
        "local_backup_dir": local_backup_dir,
    }

    # --- 1. Источник существует ------------------------------------
    if not source or not str(source).startswith("/"):
        errors.append(
            "источник задан некорректно: '{}' (нужен абсолютный путь)".format(
                source))
        return result
    if not Path(source).exists():
        errors.append("источник не существует: {}".format(source))
        return result
    if not Path(source).is_dir():
        errors.append("источник не является каталогом: {}".format(source))
        return result

    # --- 2. Источник — допустимый Btrfs subvolume --------------------
    on_btrfs, is_subvol = _is_subvolume_or_known(source)
    if not on_btrfs:
        errors.append(
            "источник {} находится не на файловой системе Btrfs — "
            "снапшот невозможен".format(source))
        return result
    if is_subvol is None:
        # Решение не принято однозначно — не рискуем (fail-closed)
        errors.append(
            "не удалось подтвердить, что {} является Btrfs subvolume "
            "(btrfs недоступен); операция для этого источника запрещена".format(
                source))
        return result
    if not is_subvol:
        errors.append(
            "{} не является Btrfs subvolume (обычный каталог внутри "
            "subvolume); снимок возможен только для subvolume — выберите "
            "сам subvolume (btrfs subvolume list)".format(source))
        return result

    # Индивидуальный каталог архивов: глобальный по умолчанию
    if not local_backup_dir:
        base_local = getattr(cfg, "LOCAL_BACKUP_DIR", None)
        if base_local:
            local_backup_dir = base_local
        else:
            local_backup_dir = str(Path(snapshot_dir or "/") / "backup")
    result["local_backup_dir"] = local_backup_dir

    # --- 3-5. Каталог снапшотов --------------------------------------
    if not snapshot_dir or not str(snapshot_dir).startswith("/"):
        errors.append(
            "каталог снапшотов не задан или не абсолютный путь: "
            "'{}'".format(snapshot_dir))
        return result
    if equal(source, snapshot_dir):
        errors.append(
            "каталог снапшотов совпадает с источником ({}) — снапшот "
            "нельзя размещать внутри самого источника".format(source))
        return result

    sd_on_btrfs = snapshots_module.is_btrfs_path(snapshot_dir)
    if not sd_on_btrfs and Path(snapshot_dir).exists():
        errors.append(
            "каталог снапшотов {} находится не на файловой системе "
            "Btrfs; снапшот btrfs возможен только внутри одного тома".format(
                snapshot_dir))
        return result

    # Требование 3.1: каталог снапшотов не внутри исходного subvolume.
    # Исключение: сам каталог является отдельным subvolume — вложенные
    # subvolume не включаются в снапшот и в btrfs send.
    if under(snapshot_dir, source):
        sd_is_subvol = snapshots_module.is_subvolume(snapshot_dir) \
            if Path(snapshot_dir).exists() else False
        if sd_is_subvol:
            warnings.append(
                "каталог снапшотов {} — отдельный subvolume внутри "
                "источника: допустимо (вложенные subvolume не попадают "
                "в снапшот и в btrfs send)".format(snapshot_dir))
        else:
            errors.append(
                "каталог снапшотов {} находится ВНУТРИ источника {} "
                "(обычный каталог): каждый снапшот включал бы предыдущие — "
                "разместите каталог вне источника или создайте его как "
                "отдельный subvolume".format(snapshot_dir, source))
            return result

    # Каталог снапшотов не должен содержать источник (обратная вложенность)
    if under(source, snapshot_dir):
        errors.append(
            "источник {} находится внутри каталога снапшотов {} — "
            "циклическая структура резервирования".format(
                source, snapshot_dir))
        return result

    # --- 6. Одна файловая система Btrfs (фактические UUID) -----------
    # Проверяется ДО создания каталогов: путь на «чужой» файловой системе
    # отклоняется без побочных каталогов (проверка по таблице монтирований
    # работает и для еще не существующих путей).
    if sd_on_btrfs or Path(snapshot_dir).exists():
        src_id = snapshots_module.btrfs_fs_identity(source)
        dst_id = snapshots_module.btrfs_fs_identity(snapshot_dir)
        if src_id is None or dst_id is None:
            errors.append(
                "не удалось однозначно определить файловую систему Btrfs "
                "(источник: '{}', каталог снапшотов: '{}'); операция "
                "прервана из соображений безопасности".format(
                    src_id, dst_id))
            return result
        if src_id != dst_id:
            if not (item or {}).get("snapshot_dir"):
                # Глобальный SNAPSHOT_DIR находится на другой файловой
                # системе, чем ЭТОТ источник. Глобальные настройки не
                # должны применяться к источникам чужих ФС — программа
                # прикладывает безопасное предложение и его признаки
                # (path передается ТОЛЬКО при ok=True, иначе путь не
                # считается пригодным).
                proposal = propose_snapshot_dir(source)
                if proposal.get("ok"):
                    result["proposed_snapshot_dir"] = proposal["path"]
                    result["proposal_ok"] = True
                else:
                    result["proposed_snapshot_dir"] = None
                    result["proposal_ok"] = False
                result["proposal_reason"] = proposal.get("reason")
                result["proposal_needs_subvolume"] = bool(
                    proposal.get("needs_subvolume"))
                result["proposal_exists"] = bool(proposal.get("exists"))
                result["proposal_is_subvolume"] = bool(
                    proposal.get("is_subvolume"))
                errors.append(
                    "глобальный каталог снапшотов SNAPSHOT_DIR {} "
                    "находится на ДРУГОЙ файловой системе Btrfs, чем "
                    "источник {} ({} и {}); предложен индивидуальный "
                    "каталог на той же файловой системе".format(
                        snapshot_dir, source, src_id, dst_id))
            else:
                errors.append(
                    "источник {} и каталог снапшотов {} на РАЗНЫХ "
                    "файловых системах Btrfs ({} и {}); снапшот "
                    "невозможен".format(source, snapshot_dir, src_id,
                                        dst_id))
            return result

    # --- 7. Создание (по запросу) и доступность для записи ------------
    # Создание выполняется ТОЛЬКО после подтверждения принадлежности
    # нужной файловой системе (шаг 6 выше).
    if create_dirs:
        try:
            Path(snapshot_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            errors.append("не удалось создать каталог снапшотов {}: {}".format(
                snapshot_dir, exc))
            return result
        try:
            Path(local_backup_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            errors.append(
                "не удалось создать каталог локальных архивов {}: {}".format(
                    local_backup_dir, exc))
            return result

    if not environment.writable_dir(snapshot_dir):
        if not Path(snapshot_dir).exists():
            parent = str(Path(snapshot_dir).parent)
            if not environment.writable_dir(parent):
                errors.append(
                    "каталог снапшотов {} не существует и не может быть "
                    "создан: {} недоступен для записи".format(
                        snapshot_dir, parent))
                return result
            warnings.append(
                "каталог снапшотов {} не существует — будет создан при "
                "запуске операции".format(snapshot_dir))
        else:
            errors.append(
                "каталог снапшотов {} недоступен для записи".format(
                    snapshot_dir))
            return result
    if not environment.writable_dir(local_backup_dir):
        if not Path(local_backup_dir).exists():
            parent = str(Path(local_backup_dir).parent)
            if not environment.writable_dir(parent):
                errors.append(
                    "каталог архивов {} не существует и не может быть "
                    "создан: {} недоступен для записи".format(
                        local_backup_dir, parent))
                return result
            warnings.append(
                "каталог архивов {} не существует — будет создан при "
                "запуске операции".format(local_backup_dir))
        else:
            errors.append(
                "каталог локальных архивов {} недоступен для записи".format(
                    local_backup_dir))
            return result

    # --- 8. Циклические структуры --------------------------------------
    if equal(snapshot_dir, local_backup_dir):
        errors.append(
            "каталог снапшотов совпадает с каталогом архивов ({}) — "
            "раздельные хранилища обязательны".format(snapshot_dir))
        return result
    if under(snapshot_dir, local_backup_dir):
        errors.append(
            "каталог снапшотов {} внутри каталога архивов {} — циклическая "
            "структура резервирования".format(snapshot_dir, local_backup_dir))
        return result
    if under(source, local_backup_dir):
        errors.append(
            "источник {} внутри каталога архивов {} — архивы попадали бы "
            "в собственные снапшоты".format(source, local_backup_dir))
        return result
    if under(local_backup_dir, source) and not under(local_backup_dir,
                                                     snapshot_dir):
        sd_is_subvol = snapshots_module.is_subvolume(snapshot_dir) \
            if Path(snapshot_dir).exists() else False
        if not (under(snapshot_dir, source) and sd_is_subvol):
            errors.append(
                "каталог архивов {} внутри источника {} — снапшот включал "
                "бы сами архивы; укажите каталог вне источника".format(
                    local_backup_dir, source))
            return result

    result["ok"] = True
    return result


def check_sources(items, cfg, *, create_dirs=False):
    """
    Проверка всех источников + перекрестные предупреждения
    (каталог снапшотов одного источника внутри другого источника).
    Возвращает список результатов check_source в том же порядке.
    """
    results = [check_source(item, cfg, create_dirs=create_dirs)
               for item in items]
    # Перекрестная вложенность каталогов снапшотов. Предупреждение
    # выдается ТОЛЬКО когда содержимое каталога действительно попадает
    # в снапшот другого источника: для отдельного subvolume и точки
    # монтирования вложенные объекты в снапшот Btrfs не входят.
    for i, result_i in enumerate(results):
        if not result_i["ok"]:
            continue
        for j, result_j in enumerate(results):
            if i == j or not result_j["ok"]:
                continue
            if not under(result_i["snapshot_dir"], result_j["source"]):
                continue
            kind = _nested_object_kind(result_i["snapshot_dir"])
            if kind == "subvolume":
                result_i["warnings"].append(
                    "каталог снапшотов {} — отдельный subvolume внутри "
                    "источника {}: в снапшот этого источника он не "
                    "попадает (вложенные subvolume исключаются)".format(
                        result_i["snapshot_dir"], result_j["source"]))
            elif kind == "mountpoint":
                result_i["warnings"].append(
                    "каталог снапшотов {} — точка монтирования внутри "
                    "источника {}: смонтированное содержимое в снапшот "
                    "этого источника не попадает".format(
                        result_i["snapshot_dir"], result_j["source"]))
            else:
                # Обычный каталог (тип не удалось определить —
                # консервативно считаем обычным каталогом).
                result_i["warnings"].append(
                    "каталог снапшотов {} находится внутри другого "
                    "источника {} — снапшот другого источника включит "
                    "содержимое каталога снапшотов".format(
                        result_i["snapshot_dir"], result_j["source"]))
    return results


def _nested_object_kind(path):
    """
    Тип вложенного объекта для перекрестной проверки:
    "subvolume" | "mountpoint" | "directory" | "unknown".
    """
    try:
        if not Path(path).exists():
            return "unknown"
    except OSError:
        return "unknown"
    try:
        if snapshots_module.is_subvolume(path):
            return "subvolume"
    except Exception:  # noqa: BLE001
        return "unknown"
    details = snapshots_module.mount_details_for(path)
    if details and _resolve(path) == _resolve(details[0]):
        return "mountpoint"
    return "directory"


# ============================================
# Автоматический подбор каталога снапшотов
# ============================================

def propose_snapshot_dir(source):
    """
    Предложение каталога снапшотов для источника.

    Возвращает {path, reason, needs_subvolume, exists, is_subvolume, ok}:
    path=None означает, что автоматически предложить безопасный каталог
    не удалось (пользователю нужно указать путь вручную).

    Правило безопасности: каталог НЕ внутри источника.
    Если источник — верхний уровень файловой системы (subvolid=5),
    единственное место на той же файловой системе обязано быть
    отдельным subvolume (только тогда вложенный subvolume не попадает
    в снапшот); обычный каталог не предлагается никогда — молча
    небезопасный путь не используется.
    """
    result = {
        "path": None, "reason": "", "needs_subvolume": False,
        "exists": False, "is_subvolume": False, "ok": False,
    }
    if not source or not Path(source).exists():
        result["reason"] = "источник не существует"
        return result
    details = snapshots_module.mount_details_for(source)
    if not details or details[1] != "btrfs":
        result["reason"] = (
            "{} находится не на файловой системе Btrfs — предложение "
            "невозможно".format(source))
        return result
    mount_point = details[0]
    candidate = (mount_point.rstrip("/") or "") + "/.snapshots"
    result["path"] = candidate
    is_mount_root = equal(source, mount_point)
    result["needs_subvolume"] = is_mount_root
    result["exists"] = Path(candidate).exists()
    result["is_subvolume"] = snapshots_module.is_subvolume(candidate) \
        if result["exists"] else False
    if is_mount_root:
        if result["exists"] and not result["is_subvolume"]:
            result["reason"] = (
                "{} — верхний уровень файловой системы: {} существует как "
                "обычный каталог и не может использоваться (он внутри "
                "источника); укажите другой каталог вне источника или на "
                "другой файловой системе Btrfs".format(source, candidate))
            return result
        result["ok"] = True
        result["reason"] = (
            "{} — верхний уровень файловой системы: каталог снапшотов {} "
            "будет создан как ОТДЕЛЬНЫЙ subvolume (вложенные subvolume не "
            "попадают в снапшот и в btrfs send)".format(source, candidate))
        return result
    # Источник глубже точки монтирования: кандидат вне источника
    if under(candidate, source):
        result["reason"] = (
            "подходящий каталог на той же файловой системе не найден "
            "безопасно; укажите каталог вручную")
        return result
    result["ok"] = True
    if result["exists"]:
        if result["is_subvolume"]:
            result["reason"] = (
                "существующий subvolume {} на той же файловой системе, "
                "вне источника".format(candidate))
        else:
            result["reason"] = (
                "существующий каталог {} на той же файловой системе, "
                "вне источника".format(candidate))
    else:
        result["reason"] = (
            "каталог {} на той же файловой системе, вне источника — "
            "будет создан".format(candidate))
    return result


def ensure_snapshot_dir_as_subvolume(path):
    """
    Создает каталог снапшотов как ОТДЕЛЬНЫЙ subvolume (для источника —
    верхний уровень файловой системы; ТЗ 3.2). Вызывается только по
    явному подтверждению пользователя.
    """
    from .process import run_command
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SnapshotError(
            "Не удалось создать родительский каталог {}: {}".format(
                path.parent, exc)) from exc
    run_command(
        ["btrfs", "subvolume", "create", str(path)],
        what="создание subvolume каталога снапшотов",
        error_class=SnapshotError)
    if not snapshots_module.is_subvolume(path):
        raise SnapshotError(
            "{} не подтвержден как subvolume после создания".format(path))
    return path
