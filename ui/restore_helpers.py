"""Помощники восстановления: предложение удалить частичный результат."""

from pathlib import Path

from core import restore as restore_module
from core.logging_setup import get_logger

log = get_logger("ui.restore_helpers")


def offer_partial_cleanup(partial_info, confirm_fn):
    """
    Предлагает удалить частичный результат НЕУДАВШЕГОСЯ восстановления.
    Вызывается только по ошибке receive (RestoreError.partial_info от
    receive_archive); после УСПЕШНОГО восстановления не предлагается —
    полный результат принадлежит пользователю.

    Удаление выполняется ОТДЕЛЬНОЙ операцией cleanup_partial_subvolume со
    строгими проверками (только Btrfs subvolume, прямой потомок целевого
    каталога, объект создан текущей операцией по перечню preexisting,
    `btrfs subvolume delete` без изменения свойства ro, без рекурсивного
    rm).
    """
    if not partial_info:
        return
    dest = Path(partial_info["dest"])
    if not dest.exists():
        return
    if not confirm_fn(
            "Обнаружен частичный результат восстановления {} (создан "
            "текущей операцией). Проверить и удалить его "
            "(btrfs subvolume delete)?".format(dest)):
        return
    preexisting = partial_info.get("preexisting")
    if preexisting is not None:
        preexisting = set(preexisting)
    try:
        restore_module.cleanup_partial_subvolume(
            dest,
            target_dir=partial_info["target"],
            expected_name=dest.name,
            preexisting=preexisting,
        )
        print("Удален частичный результат: {}".format(dest))
    except Exception as exc:  # noqa: BLE001
        print("Удаление отменено: {}".format(exc))
        print("Объект сохранен; обработайте его вручную "
              "(btrfs subvolume delete {}; изменение свойства ro не "
              "требуется).".format(dest))
