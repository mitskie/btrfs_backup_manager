"""
Уникальный идентификатор операции.

Формат: YYYY-MM-DD_HHMMSS_<run id: 6 hex>.
  - сохраняет сортировку по дате и времени;
  - run id (6 hex-символов, из микросекунд) обеспечивает уникальность даже
    при нескольких операциях в течение одной секунды;
  - уникальность дополнительно проверяется по существующим снапшотам,
    локальным и сетевым архивам: при коллизии берется следующая метка;
  - перезапись существующих объектов исключена также защитами при
    создании снапшотов и архивов.
"""

import time
from datetime import datetime
from pathlib import Path

from . import BtrfsBackupError
from .logging_setup import get_logger

log = get_logger("runid")

MAX_ATTEMPTS = 200


def _candidate(now):
    return "{:%Y-%m-%d_%H%M%S}_{:06x}".format(now, now.microsecond)


def _as_dirs(value):
    """Путь или список путей -> список строк."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item]
    return [str(value)]


def run_id_taken(run_id, snapshot_dir, backup_dir, net_dir,
                 subvolume_names=None):
    """
    True, если идентификатор уже занят: существует снапшот, локальный или
    сетевой архив с этим идентификатором. Проверка по всем subvolume,
    с префиксом хоста и без него (архивы предыдущих версий).
    Недоступное хранилище не считается занятым (best-effort): коллизию
    в этом случае перехватят защиты при создании объектов.

    v1.4.6: snapshot_dir и backup_dir могут быть списком — проверка по
    КАЖДОМУ каталогу всех источников профиля.
    """
    from .environment import hostname as detect_hostname
    names = subvolume_names or []
    host = detect_hostname()
    prefixes = ["{}_".format(host)] if host else []
    prefixes.append("")  # архивы версий без префикса хоста
    snapshot_dirs = _as_dirs(snapshot_dir)
    backup_dirs = _as_dirs(backup_dir)
    net_dirs = _as_dirs(net_dir)
    try:
        for path in snapshot_dirs:
            sdir = Path(path)
            if sdir.is_dir():
                for name in names:
                    for prefix in prefixes:
                        if (sdir / "{}{}_{}".format(prefix, name, run_id)).exists():
                            return True
        for path in backup_dirs:
            bdir = Path(path)
            if bdir.is_dir():
                for name in names:
                    for prefix in prefixes:
                        if any(bdir.glob(
                                "{}{}_{}.?*.gpg*".format(prefix, name, run_id))) or \
                                any(bdir.glob(
                                    "{}{}_{}.btrfs.*.gpg*".format(
                                        prefix, name, run_id))):
                            return True
        for path in net_dirs:
            ndir = Path(path)
            if ndir.is_dir():
                for name in names:
                    for prefix in prefixes:
                        if any(ndir.glob(
                                "{}{}_{}.?*.gpg*".format(prefix, name, run_id))) or \
                                any(ndir.glob(
                                    "{}{}_{}.btrfs.*.gpg*".format(
                                        prefix, name, run_id))):
                            return True
    except OSError:
        return False
    return False


def new_run_id(snapshot_dir, backup_dir, net_dir, subvolume_names=None,
               now_fn=None):
    """
    Подбирает уникальный идентификатор операции.

    now_fn — точка расширения для тестов (детерминированное время).
    """
    if now_fn is None:
        now_fn = datetime.now
    for _ in range(MAX_ATTEMPTS):
        candidate = _candidate(now_fn())
        if not run_id_taken(candidate, snapshot_dir, backup_dir, net_dir,
                            subvolume_names):
            return candidate
        time.sleep(0.005)
    raise BtrfsBackupError(
        "Не удалось подобрать уникальный идентификатор запуска; "
        "проверьте содержимое {} на посторонние объекты".format(snapshot_dir)
    )
