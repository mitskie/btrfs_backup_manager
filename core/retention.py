"""
Retention — политика хранения и защита от потери данных.

Правила:
  - хранить последние RETENTION_COUNT успешно сохраненных копий для
    КАЖДОГО subvolume (независимая история);
  - локальные снапшоты остаются на диске после создания архива;
  - снапшот удаляется только если у него есть ПОДТВЕРЖДЕННАЯ сетевая
    копия (фактический SHA-256 совпадает, ТЗ 9.3) и он выходит за
    пределы последних RETENTION_COUNT подтвержденных копий;
  - алгоритм сжатия определяется ИНДИВИДУАЛЬНО ДЛЯ КАЖДОГО архива: смешанные истории gzip/zstd учитываются
    полностью, независимо от текущего COMPRESSION;
  - сетевое хранилище недоступно — удаление не выполняется;
  - главный принцип: при сомнении оставить лишнюю копию, а не
    удалить потенциально единственную рабочую копию данных.
"""

from pathlib import Path

from .logging_setup import get_logger
from .network import network_archive_confirmed
from .snapshots import (
    delete_snapshot, list_snapshots, select_for_deletion,
)

log = get_logger("retention")


def retention_plan(snapshot_dir, subvolume_names, retention_count,
                   net_dir, compression=None, *, confirmed_fn=None):
    """
    Составляет план удаления: {subvolume: [имена снапшотов]}.

    Учитываются только снапшоты с подтвержденной сетевой копией.
    Реального удаления не выполняет (используется для предпросмотра
    и для подтверждаемой очистки).

    compression (если задан) лишь ставит соответствующее расширение
    первым в поиске — поиск копии всегда выполняется по ВСЕМ допустимым
    форматам независимо от текущего COMPRESSION.
    """
    if confirmed_fn is None:
        confirmed_fn = lambda name: network_archive_confirmed(  # noqa: E731
            net_dir, name, compression)
    result = {}
    snapshots = list_snapshots(snapshot_dir, subvolume_names)
    for name, items in snapshots.items():
        confirmed = [s for s in items if confirmed_fn(s.name)]
        victims = select_for_deletion(confirmed, retention_count)
        if victims:
            result[name] = [snapshot.name for snapshot in victims]
    return result


def apply_retention(snapshot_dir, subvolume_names, retention_count,
                    net_dir, *, delete=True, compression=None,
                    confirmed_fn=None, protected_paths=()):
    """
    Выполняет retention снапшотов.

    Удаляются только снапшоты, которые:
      1) выходят за пределы последних `retention_count` ПОДТВЕРЖДЕННЫХ
         снапшотов subvolume;
      2) имеют подтвержденную сетевую копию (SHA-256 сверяется фактически;
         алгоритм сжатия каждого архива определяется по самому архиву
         и его метаданным — НЕ по текущему COMPRESSION, ТЗ 6.2).

    Снапшоты без подтвержденной копии не удаляются никогда.
    protected_paths: пути, удаление которых запрещено
    (исходные рабочие subvolume, снапшоты текущей операции) — ТЗ 4.3.
    Возвращает {имя subvolume: [имена удаленных/кандидатов]}.
    """
    if confirmed_fn is None:
        confirmed_fn = lambda name: network_archive_confirmed(  # noqa: E731
            net_dir, name, compression)
    result = {}
    snapshots = list_snapshots(snapshot_dir, subvolume_names)
    for name, items in snapshots.items():
        confirmed = [s for s in items if confirmed_fn(s.name)]
        skipped = len(items) - len(confirmed)
        if skipped:
            log.info(
                "retention '%s': %d снапшотов без подтвержденной сетевой "
                "копии — сохранены", name, skipped,
            )
        victims = select_for_deletion(confirmed, retention_count)
        for snapshot in victims:
            if delete:
                delete_snapshot(snapshot.path, snapshot_dir,
                                protected_paths=protected_paths)
            result.setdefault(name, []).append(snapshot.name)
        if delete and victims:
            log.info(
                "Retention '%s': удалено %d, осталось подтвержденных %d "
                "(всего снапшотов: %d)",
                name, len(victims), len(confirmed) - len(victims), len(items),
            )
    return result


def retention_safety_check(config, net_dir):
    """
    Обязательная проверка сетевого хранилища перед удалением.

    Точка монтирования обязана быть активным монтированием независимо от
    CHECK_NETWORK_MOUNT; каталог сетевых архивов обязан лежать на той же
    файловой системе. Любая ошибка — StorageError (удаление отменяется).
    """
    from .network import require_network_mount_for_retention
    return require_network_mount_for_retention(
        config.NETWORK_MOUNT, net_dir,
        expected_fs_type=getattr(config, "EXPECTED_NETWORK_FS_TYPE", None),
    )
