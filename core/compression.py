"""
Сжатие: выбор алгоритма, fallback, определение по архиву.

Основной алгоритм — COMPRESSION (zstd). Если утилита отсутствует:
  - интерактивный режим — предложение переключиться на COMPRESSION_FALLBACK
    (config.py НЕ изменяется, фактический алгоритм попадает в имя архива,
    метаданные и журнал);
  - cron — fallback по флагу CRON_COMPRESSION_FALLBACK, иначе ошибка;
  - недоступны обе утилиты — резервное копирование не начинается.

Метод сжатия для восстановления/проверки/повторной отправки определяется
по расширению КОНКРЕТНОГО архива (и метаданным), а не по текущему
COMPRESSION: смена конфигурации не ломает старые копии.

Поддерживаются два поколения суффиксов:
  1.1:       <база>.zst.gpg / <база>.gz.gpg
  1.0 v3:    <база>.btrfs.zst.gpg / <база>.btrfs.gz.gpg
"""

import re
import shutil

from . import (
    COMPRESSOR_EXT, ArchiveError, CompressionError, UtilityMissingError,
)
from .logging_setup import get_logger

log = get_logger("compression")

SUPPORTED = ("zstd", "gzip")


def compression_ext(compression):
    """Расширение сжатия: zstd -> .zst, gzip -> .gz."""
    ext = COMPRESSOR_EXT.get(compression)
    if ext is None:
        raise ArchiveError("Неизвестный тип сжатия: {}".format(compression))
    return ext


def tool_available(compression) -> bool:
    """True, если системная утилита сжатия установлена."""
    return bool(shutil.which(compression))


def available_tools():
    """Список доступных алгоритмов из поддерживаемых."""
    return [c for c in SUPPORTED if tool_available(c)]


def detect_compression(filename):
    """
    Определяет алгоритм сжатия по расширению имени архива.

    root_2026-11-06_120000_a1b2c3.zst.gpg -> zstd
    root_2026-11-06_120000_a1b2c3.gz.gpg  -> gzip
    root_2026-03-01_120000_123456.btrfs.zst.gpg -> zstd (1.0 v3)
    root_2026-03-01_120000.btrfs.gz.gpg   -> gzip (1.0)

    COMPRESSION из config.py задает алгоритм только для НОВЫХ архивов;
    при восстановлении, проверке и повторной отправке алгоритм всегда
    определяется по самому архиву. Неизвестное расширение — ошибка.
    """
    name = str(filename)
    if name.endswith(".btrfs.zst.gpg") or name.endswith(".zst.gpg"):
        return "zstd"
    if name.endswith(".btrfs.gz.gpg") or name.endswith(".gz.gpg"):
        return "gzip"
    raise ArchiveError(
        "Не удалось определить алгоритм сжатия по имени архива '{}': "
        "ожидаются суффиксы .zst.gpg / .gz.gpg или .btrfs.zst.gpg / "
        ".btrfs.gz.gpg. Архив пропущен.".format(name)
    )


def compressor_cmd(compression, level):
    """Команда компрессора: stdin -> stdout (сжатый поток)."""
    if compression == "gzip":
        return ["gzip", "-c", "-{}".format(int(level))]
    if compression == "zstd":
        return ["zstd", "-c", "-{}".format(int(level)), "-q"]
    raise CompressionError("Неизвестный тип сжатия: {}".format(compression))


def decompressor_cmd(compression):
    """Команда распаковки: stdin -> stdout (распакованный поток)."""
    if compression == "gzip":
        return ["gzip", "-d", "-c"]
    if compression == "zstd":
        return ["zstd", "-d", "-c", "-q"]
    raise CompressionError("Неизвестный тип сжатия: {}".format(compression))


def stream_test_cmd(compression):
    """Команда проверки целостности сжатого потока (gzip -t / zstd -t)."""
    if compression == "gzip":
        return ["gzip", "-t"]
    if compression == "zstd":
        return ["zstd", "-t", "-q"]
    raise CompressionError("Неизвестный тип сжатия: {}".format(compression))


def require_tool(compression):
    """Проверяет наличие утилиты; отсутствие — CompressionError (код 6)."""
    if not tool_available(compression):
        raise CompressionError(
            "Утилита сжатия '{}' не найдена; операция невозможна. "
            "Установите пакет (gzip/zstd) и повторите.".format(compression)
        )


# ============================================
# Выбор алгоритма для новой операции
# ============================================

FALLBACK_DIALOG = """ВНИМАНИЕ!

Утилита {primary} не найдена.

Основной алгоритм сжатия: {primary}
Доступный альтернативный алгоритм: {fallback}

Продолжить резервное копирование с {fallback}?"""


def resolve_compression(config, *, non_interactive, confirm_fn=None,
                        override=None):
    """
    Определяет алгоритм сжатия для НОВОЙ операции.

    override: алгоритм, выбранный пользователем в
    интерактивном интерфейсе ("zstd"/"gzip"). Допустимость проверяется;
    при недоступности утилиты выбранного алгоритма выполняется обычная
    логика fallback по конфигурации. Неинтерактивные запуски (cron)
    override игнорируют — там алгоритм строго по конфигурации.

    non_interactive=True (cron): вопросы запрещены; при недоступности
    основного алгоритма используется COMPRESSION_FALLBACK, если разрешен
    CRON_COMPRESSION_FALLBACK, иначе возбуждается ошибка.

    non_interactive=False: при недоступности основного алгоритма
    запрашивается подтверждение через confirm_fn(текст) -> bool.
    Отказ пользователя прерывает операцию (OperationCancelled).

    Возвращает имя алгоритма ("zstd"/"gzip"). COMPRESSION в config.py
    никогда не изменяется.
    """
    if override is not None and not non_interactive:
        if override not in SUPPORTED:
            raise UtilityMissingError(
                "Недопустимый алгоритм сжатия '{}' (допустимо: {}); "
                "операция не начата.".format(override, ", ".join(SUPPORTED))
            )
        if tool_available(override):
            return override
        log.warning(
            "Выбранный в интерфейсе алгоритм '%s' недоступен: выполняется "
            "обычный выбор по конфигурации", override,
        )
    primary = config.COMPRESSION
    fallback = config.COMPRESSION_FALLBACK

    primary_ok = tool_available(primary)
    if primary_ok:
        return primary

    fallback_ok = fallback in SUPPORTED and tool_available(fallback)

    if not fallback_ok:
        # ТЗ 6.3: недоступны оба алгоритма — не начинать резервное копирование.
        raise UtilityMissingError(
            "Отсутствуют утилиты сжатия: '{}' (основной) и '{}' (резервный) "
            "не найдены. Резервное копирование не начато; установите gzip "
            "или zstd.".format(primary, fallback)
        )

    if non_interactive:
        if getattr(config, "CRON_COMPRESSION_FALLBACK", True):
            log.warning(
                "Утилита '%s' не найдена; используется fallback '%s' "
                "(CRON_COMPRESSION_FALLBACK=true)", primary, fallback,
            )
            return fallback
        raise UtilityMissingError(
            "Утилита сжатия '{}' не найдена, автоматический fallback "
            "запрещен (CRON_COMPRESSION_FALLBACK=false). Операция не "
            "выполнена.".format(primary)
        )

    # Интерактивный режим: диалог подтверждения
    if confirm_fn is None:
        confirm_fn = lambda _text: True  # noqa: E731
    text = FALLBACK_DIALOG.format(primary=primary, fallback=fallback)
    if not confirm_fn(text):
        from . import OperationCancelled
        raise OperationCancelled(
            "Операция отменена пользователем: утилита '{}' не найдена, "
            "сжатие '{}' отклонено. Существующие снапшоты и архивы не "
            "изменены.".format(primary, fallback)
        )
    log.warning(
        "Утилита '%s' не найдена; для текущей операции используется '%s'. "
        "Для использования %s установите соответствующую системную утилиту.",
        primary, fallback, primary,
    )
    return fallback


# ============================================
# Имена архивов и снапшотов
# ============================================

def archive_filename(subvolume_name, run_id, compression, hostname=None):
    """
    Имя архива v1.3: <хост>_<subvolume>_<дата_время>_<run id>.<zst|gz>.gpg.

    Префикс хоста: скрипт используется на нескольких машинах с общей
    сетевой шарой — без имени хоста в имени
    архива невозможно определить, откуда копия. hostname=None — взять имя
    текущей машины; hostname="" — префикс не добавляется (архивы старых
    версий и тесты). Run id обеспечивает уникальность даже при нескольких
    операциях в течение одной секунды; коллизии имен недопустимы и
    проверяются отдельно (создание архива запрещает перезапись).
    """
    if hostname is None:
        from .environment import hostname as detect_hostname
        hostname = detect_hostname()
    prefix = "{}_".format(hostname) if hostname else ""
    return "{}{}_{}.{}.gpg".format(
        prefix, subvolume_name, run_id, compression_ext(compression)
    )


# База имени архива/снапшота обоих поколений; префикс хоста —
# необязательный, хост не может содержать "_" (см. environment.hostname),
# поэтому разбор однозначен.
_BASE_NAME_RE = re.compile(
    r"^(?:(?P<host>[A-Za-z0-9-]{1,32})_)?"
    r"(?P<subvol>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"_(?P<ts>\d{4}-\d{2}-\d{2}_\d{6})"
    r"(?:_(?P<rid>[0-9a-fA-F]{6}|\d{1,6}))?$"
)


def snapshot_name_from_archive(filename):
    """
    Имя снапшота из имени архива (снапшоты хранятся БЕЗ префикса хоста):
      root_2026-11-06_120000_a1b2c3.zst.gpg    -> root_2026-11-06_120000_a1b2c3
      mysrv1_root_2026-11-06_120000_a1b2c3.zst.gpg -> root_2026-11-06_120000_a1b2c3
      root_2026-03-01_120000_123456.btrfs.zst.gpg -> root_2026-03-01_120000_123456
    """
    name = str(filename)
    if name.endswith(".gpg"):
        name = name[:-4]
    for ext in (".zst", ".gz"):
        if name.endswith(ext):
            name = name[: -len(ext)]
            break
    if name.endswith(".btrfs"):
        name = name[: -len(".btrfs")]
    match = _BASE_NAME_RE.match(name)
    if match is None:
        return name
    return "{}_{}{}".format(
        match.group("subvol"), match.group("ts"),
        ("_" + match.group("rid")) if match.group("rid") else "",
    )
