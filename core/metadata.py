"""
Метаданные архивов (sidecar JSON, ТЗ 8).

Для каждого архива хранится ОДИН sidecar-файл <имя архива>.json рядом
с архивом: отдельный файл .sha256 избыточен, поскольку SHA-256
хранится в поле "sha256" JSON. Файл .sha256 больше не создается, но
для архивов предыдущих версий по-прежнему читается, если
JSON-метаданные отсутствуют.

Зачем нужен JSON: он хранит machine-readable
сведения об архиве (subvolume, хост, дата, размер, сжатие, SHA-256,
статус отправки), которые используются списками, проверкой, retention
и диагностикой БЕЗ расшифрования самого архива.

Метаданные записываются АТОМАРНО (временный файл + os.replace).

Статусы: created, verified, upload-pending, uploading, uploaded,
upload-failed, restore-failed.

Формат 3: новое поле "profile" — имя профиля конфигурации
(--config), создавшего архив (None для архива основного конфига и
архивов предыдущих версий — поле нечитается как неизвестное и на
совместимость не влияет).

Метаданные НЕ являются заменой проверки SHA-256: при отсутствии
или повреждении sidecar-файла характеристики архива определяются по
имени и фактическому содержимому (infer_archive_info).

Поддерживаются архивы без метаданных предыдущих версий.
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import VerificationError
from .compression import detect_compression
from .logging_setup import get_logger

log = get_logger("metadata")

FORMAT_VERSION = 3

# Статусы
STATUS_CREATED = "created"
STATUS_VERIFIED = "verified"
STATUS_UPLOAD_PENDING = "upload-pending"
STATUS_UPLOADING = "uploading"
STATUS_UPLOADED = "uploaded"
STATUS_UPLOAD_FAILED = "upload-failed"
STATUS_RESTORE_FAILED = "restore-failed"

ALL_STATUSES = (
    STATUS_CREATED, STATUS_VERIFIED, STATUS_UPLOAD_PENDING,
    STATUS_UPLOADING, STATUS_UPLOADED, STATUS_UPLOAD_FAILED,
    STATUS_RESTORE_FAILED,
)

_UPLOAD_BY_STATUS = {
    STATUS_CREATED: "none",
    STATUS_VERIFIED: "none",
    STATUS_UPLOAD_PENDING: "pending",
    STATUS_UPLOADING: "uploading",
    STATUS_UPLOADED: "uploaded",
    STATUS_UPLOAD_FAILED: "upload-failed",
}

# Шаблон имени архива всех поколений:
# <хост>_<subvol>_<ГГГГ-ММ-ДД_ЧЧММСС>_<run id>.<zst|gz>.gpg
# <subvol>_<ГГГГ-ММ-ДД_ЧЧММСС>_<run id>.<zst|gz>.gpg
#   v1.0 v3: <subvol>_<ГГГГ-ММ-ДД_ЧЧММСС>_<микросекунды>.btrfs.<zst|gz>.gpg
# <subvol>_<ГГГГ-ММ-ДД_ЧЧММСС>.btrfs.<zst|gz>.gpg
# Хост — необязательный префикс; environment.hostname допускает
# в нем только [a-z0-9-], поэтому разбор однозначен.
_ARCHIVE_NAME_RE = re.compile(
    r"^(?:(?P<host>[A-Za-z0-9-]{1,32})_)?"
    r"(?P<subvol>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"_(?P<ts>\d{4}-\d{2}-\d{2}_\d{6})"
    r"(?:_(?P<rid>[0-9a-fA-F]{6}|\d{1,6}))?"
    r"(?:\.btrfs)?"
    r"\.(?P<ext>zst|gz)"
    r"\.gpg$"
)


def metadata_path(archive_path) -> Path:
    """Путь sidecar-файла: <архив>.json."""
    return Path(str(archive_path) + ".json")


def parse_archive_name(filename):
    """
    Разбирает имя архива любого поколения.

    Возвращает dict(hostname, subvolume, timestamp, run_id, compression,
    generation) или None, если имя не соответствует шаблону приложения.
    """
    match = _ARCHIVE_NAME_RE.match(str(filename))
    if match is None:
        return None
    return {
        "hostname": match.group("host") or "",
        "subvolume": match.group("subvol"),
        "timestamp": match.group("ts"),
        "run_id": match.group("rid") or "",
        "compression": {"zst": "zstd", "gz": "gzip"}[match.group("ext")],
        "generation": 2 if ".btrfs." in str(filename) else 1,
    }


def new_metadata(*, archive_name, subvolume_name, subvolume_path, run_id,
                 compression, encrypted, snapshot_id, sha256=None, size=None,
                 status=STATUS_CREATED, hostname=None, app_version=None,
                 profile=None):
    """Создает словарь метаданных нового архива."""
    if app_version is None:
        from . import APP_VERSION
        app_version = APP_VERSION
    now = datetime.now().isoformat(timespec="microseconds")
    if hostname is None:
        from .environment import hostname as detect_hostname
        hostname = detect_hostname()
    return {
        "format": FORMAT_VERSION,
        "archive": str(archive_name),
        "hostname": hostname,
        "subvolume_name": subvolume_name,
        "subvolume_path": subvolume_path,
        "created": now,
        "run_id": run_id,
        "compression": compression,
        "encrypted": bool(encrypted),
        "sha256": sha256,
        "size": size,
        "snapshot": snapshot_id,
        "app_version": app_version,
        "status": status,
        "upload_status": _UPLOAD_BY_STATUS.get(status, "none"),
        "uploaded_at": None,
        "profile": profile,
    }


def write_metadata(archive_path, meta):
    """
    Атомарно записывает метаданные (tmp + os.replace).

    Повреждение при записи исключено: читатель видит либо старую,
    либо новую полную версию файла.
    """
    meta = dict(meta)
    meta["format"] = FORMAT_VERSION
    meta["archive"] = Path(archive_path).name
    target = metadata_path(archive_path)
    tmp_path = target.with_name(target.name + ".tmp")
    try:
        with open(str(tmp_path), "w", encoding="utf-8") as handle:
            json.dump(meta, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp_path), str(target))
    except OSError as exc:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        log.warning("Не удалось записать метаданные %s: %s", target, exc)
        return False
    log.debug("Метаданные записаны: %s (status=%s)", target.name, meta.get("status"))
    return True


def read_metadata(archive_path):
    """
    Читает метаданные; None — файл отсутствует или поврежден.
    Повреждение НЕ считается доказательством целостности архива:
    вызывающий код обязан использовать infer_archive_info().
    """
    path = metadata_path(archive_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        meta = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("Метаданные %s повреждены (%s); используются данные "
                    "из имени архива", path.name, exc)
        return None
    if not isinstance(meta, dict):
        return None
    return meta


def set_status(meta, status, *, uploaded_at=None):
    """
    Обновляет статус метаданных (единый жизненный цикл, ТЗ 8).

    Синхронизирует поле upload_status; uploaded_at фиксирует дату
    успешной отправки и не сбрасывается при upload-failed.
    """
    meta = dict(meta)
    meta["status"] = status
    meta["upload_status"] = _UPLOAD_BY_STATUS.get(status, meta.get("upload_status", "none"))
    if status == STATUS_UPLOADED:
        meta["uploaded_at"] = uploaded_at or datetime.now().isoformat(
            timespec="seconds")
    if uploaded_at is not None:
        meta["uploaded_at"] = uploaded_at
    return meta


def update_status(archive_path, status, **kwargs) -> bool:
    """Читает метаданные архива, обновляет статус и атомарно записывает."""
    meta = read_metadata(archive_path)
    if meta is None:
        meta = infer_archive_info(archive_path) or {}
    meta = set_status(meta, status, **kwargs)
    return write_metadata(archive_path, meta)


# ============================================
# Восстановление характеристик без sidecar
# ============================================

def infer_archive_info(archive_path):
    """
    Определяет характеристики архива по имени файла, фактическому размеру
    и файлу контрольной суммы (.sha256). Используется, когда метаданные
    отсутствуют или повреждены (архивы предыдущих версий, ТЗ 19).

    Возвращает словарь метаданных (с пометкой "inferred": True) или None,
    если имя не соответствует шаблону приложения.
    """
    path = Path(archive_path)
    parsed = parse_archive_name(path.name)
    if parsed is None:
        return None
    sha = None
    checksum = Path(str(path) + ".sha256")
    if checksum.is_file():
        try:
            first_line = checksum.read_text(encoding="utf-8").splitlines()[0]
            value = first_line.split()[0].strip().lower()
            if len(value) == 64 and all(c in "0123456789abcdef" for c in value):
                sha = value
        except (OSError, IndexError):
            sha = None
    size = None
    try:
        size = path.stat().st_size
    except OSError:
        pass
    return {
        "format": FORMAT_VERSION,
        "archive": path.name,
        "hostname": parsed["hostname"],
        "subvolume_name": parsed["subvolume"],
        "subvolume_path": None,
        "created": parsed["timestamp"].replace("_", " ", 1),
        "run_id": parsed["run_id"],
        "compression": parsed["compression"],
        "encrypted": True,
        "sha256": sha,
        "size": size,
        "snapshot": parsed["subvolume"] + "_" + parsed["timestamp"] + (
            "_" + parsed["run_id"] if parsed["run_id"] else ""
        ),
        "app_version": "1.0 v3 (inferred)",
        "status": STATUS_CREATED,
        "upload_status": "none",
        "uploaded_at": None,
        "inferred": True,
    }


def load_or_infer(archive_path):
    """
    Надежное получение характеристик архива:
    метаданные -> данные из имени и содержимого.

    Возбуждает VerificationError, если архив не опознан вовсе.
    """
    meta = read_metadata(archive_path)
    if meta is not None:
        # Метаданные не заменяют проверку: алгоритм сжатия сверяется
        # с фактическим расширением архива.
        try:
            actual = detect_compression(Path(archive_path).name)
        except VerificationError:
            actual = None
        if actual and meta.get("compression") not in (None, actual):
            log.warning(
                "Метаданные %s указывают сжатие '%s', по расширению — '%s'; "
                "используется значение из имени архива",
                Path(archive_path).name, meta.get("compression"), actual,
            )
            meta = dict(meta, compression=actual)
        return meta
    inferred = infer_archive_info(archive_path)
    if inferred is None:
        raise VerificationError(
            "Не удалось определить характеристики архива {}: имя не "
            "соответствует шаблону приложения и метаданные отсутствуют.".format(
                Path(archive_path).name)
        )
    log.info(
        "Метаданные %s отсутствуют; параметры определены по имени и "
        "содержимому", Path(archive_path).name,
    )
    return inferred
