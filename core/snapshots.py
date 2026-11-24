"""
Работа с Btrfs subvolume и снапшотами.

Все операции выполняются внешними командами btrfs-progs через subprocess
(без shell=True). Каждый код возврата контролируется.

Имена снапшотов (поддерживаются все поколения формата, ТЗ 19):
  1.0/1.0 v2:  <subvolume>_ГГГГ-ММ-ДД_ЧЧММСС
  1.0 v3:      <subvolume>_ГГГГ-ММ-ДД_ЧЧММСС_микросекунды
  1.1:         <subvolume>_ГГГГ-ММ-ДД_ЧЧММСС_<run id: 6 hex>
"""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import SnapshotError
from .logging_setup import get_logger
from .process import run_command

log = get_logger("snapshots")

# Строгий шаблон имени снапшота/архива — все поколения формата.
SNAPSHOT_NAME_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"_(?P<ts>\d{4}-\d{2}-\d{2}_\d{6})"
    r"(?:_(?P<extra>\d{1,6}|[0-9a-fA-F]{6}))?$"
)


@dataclass(frozen=True)
class SnapshotInfo:
    """Информация о снапшоте."""
    subvolume: str
    timestamp: str
    path: Path

    @property
    def name(self):
        return self.path.name

    @property
    def sort_key(self):
        # Метка времени в формате ISO сортируется лексикографически
        return (self.timestamp, self.path.name)


# ============================================
# Проверки
# ============================================

def is_subvolume(path) -> bool:
    """Авторитетная проверка: `btrfs subvolume show` возвращает успех."""
    path = Path(path)
    if not path.exists():
        return False
    try:
        proc = subprocess.run(
            ["btrfs", "subvolume", "show", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _unescape_mount_field(field):
    """Раскодирует восьмеричные последовательности /proc/self/mounts (\\040 и т.п.)."""
    try:
        return field.encode("ascii", "backslashreplace").decode("unicode_escape")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return field


def _parse_mounts():
    mounts = []
    try:
        content = Path("/proc/self/mounts").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return mounts
    for line in content.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            mounts.append((_unescape_mount_field(parts[1]), parts[2], parts[0]))
    return mounts


def filesystem_type(path):
    """
    Тип файловой системы, на которой находится путь (по /proc/self/mounts),
    или None, если определить не удалось.
    """
    target = os.path.realpath(str(path))
    best = None
    for mount_point, fstype, _dev in _parse_mounts():
        if target == mount_point or \
                target.startswith(mount_point.rstrip("/") + "/"):
            if best is None or len(mount_point) > len(best[0]):
                best = (mount_point, fstype)
    return best[1] if best else None


def is_btrfs_path(path) -> bool:
    """True, если путь находится на файловой системе Btrfs."""
    return filesystem_type(path) == "btrfs"


def mount_details_for(path):
    """
    (точка монтирования, fstype, устройство) для пути по /proc/self/mounts
    или None, если определить не удалось.
    """
    target = os.path.realpath(str(path))
    best = None
    for mount_point, fstype, device in _parse_mounts():
        if target == mount_point or \
                target.startswith(mount_point.rstrip("/") + "/"):
            if best is None or len(mount_point) > len(best[0]):
                best = (mount_point, fstype, device)
    return best


def _fs_uuid_query(path):
    """Один запрос `btrfs filesystem show <path>` -> UUID или None."""
    try:
        proc = subprocess.run(
            ["btrfs", "filesystem", "show", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    match = re.search(r"uuid:\s*([0-9a-fA-F-]{20,})", proc.stdout or "")
    return match.group(1) if match else None


def btrfs_fs_uuid(path):
    """
    UUID файловой системы Btrfs, содержащей путь, или None.

    v1.4.6: `btrfs filesystem show` принимает не всякий путь — обычный
    каталог (не subvolume и не точка монтирования) может получить ответ
    «not a valid btrfs filesystem», из-за чего принадлежность одной
    файловой системе определялась неверно (источник-подvol давал
    «uuid:...», обычный каталог снапшотов — «mount:...», и одна и та же
    ФС выглядела двумя разными файловыми системами). Теперь при неудаче
    запрос повторяется для ТОЧКИ МОНТИРОВАНИЯ, внутри которой находится
    путь (/proc/self/mounts) — она всегда корректна для filesystem show.
    """
    uuid = _fs_uuid_query(path)
    if uuid:
        return uuid
    details = mount_details_for(path)
    if details and details[1] == "btrfs":
        return _fs_uuid_query(details[0])
    return None


def btrfs_fs_identity(path):
    """
    Идентификатор файловой системы Btrfs для пути: приоритет — FS UUID
    (btrfs filesystem show); при недоступности — устройство и точка
    монтирования из /proc/self/mounts. None — путь не на Btrfs или ФС
    определить не удалось.
    """
    uuid = btrfs_fs_uuid(path)
    if uuid:
        return "uuid:" + uuid
    details = mount_details_for(path)
    if details and details[1] == "btrfs":
        return "mount:{}:{}".format(details[0], details[2])
    return None


def ensure_same_btrfs_filesystem(source, snapshot_dir, label=None):
    """
    Проверяет, что источник и каталог снапшотов находятся на ОДНОЙ
    файловой системе Btrfs: снапшот btrfs возможен только внутри одного
    тома. Нарушение — ошибка до начала каких-либо операций.
    """
    what = label or str(source)
    if not is_btrfs_path(str(snapshot_dir)):
        raise SnapshotError(
            "Каталог снапшотов {} находится не на файловой системе Btrfs; "
            "создание снапшотов невозможно.".format(snapshot_dir)
        )
    if not is_btrfs_path(str(source)):
        raise SnapshotError(
            "Источник '{}' ({}) находится не на файловой системе Btrfs; "
            "создание снапшотов невозможно.".format(what, source)
        )
    src_id = btrfs_fs_identity(str(source))
    dst_id = btrfs_fs_identity(str(snapshot_dir))
    if src_id is None or dst_id is None:
        raise SnapshotError(
            "Не удалось определить принадлежность к файловой системе Btrfs "
            "(источник: '{}', каталог снапшотов: '{}'); операция прервана "
            "из соображений безопасности.".format(source, snapshot_dir)
        )
    if src_id != dst_id:
        raise SnapshotError(
            "Источник '{}' и каталог снапшотов {} находятся на РАЗНЫХ "
            "файловых системах Btrfs ({} и {}); снапшот невозможен. "
            "Разместите SNAPSHOT_DIR на той же файловой системе Btrfs, "
            "что и источник.".format(what, snapshot_dir, src_id, dst_id)
        )


def subvolume_ro_property(path):
    """
    Возвращает True/False (свойство ro) или None, если определить не удалось.
    """
    try:
        proc = subprocess.run(
            ["btrfs", "property", "get", str(path), "ro"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        key, _, value = line.strip().partition("=")
        if key in ("ro", "readonly"):
            return value.strip().lower() == "true"
    return None


def subvolume_uuid(path):
    """
    UUID subvolume из `btrfs subvolume show <path>`; документация:
    https://btrfs.readthedocs.io/en/latest/btrfs-subvolume.html — вывод
    show содержит строку «UUID: <uuid>»). None — определить не удалось
    (btrfs недоступен, путь не subvolume); вызывающий код трактует None
    как «сведенье недоступно», не как отказ.
    """
    try:
        proc = subprocess.run(
            ["btrfs", "subvolume", "show", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        key, _, value = line.strip().partition(":")
        if key.strip() == "UUID":
            value = value.strip()
            return value or None
    return None


# ============================================
# Создание снапшотов
# ============================================

def create_readonly_snapshot(source, snapshot_dir, subvolume_name, run_id) -> Path:
    """
    Создает read-only снапшот источника в каталоге снапшотов.

    Проверяет: существование источника, что источник является Btrfs subvolume,
    что источник не совпадает с каталогом снапшотов, что объект с таким
    именем еще не существует (защита от перезаписи, ТЗ 5).
    """
    src = Path(source)
    sdir = Path(snapshot_dir)
    label = "{}_{}".format(subvolume_name, run_id)

    if not src.exists():
        raise SnapshotError("Источник не существует: {}".format(src))
    if not src.is_dir():
        raise SnapshotError("Источник не является каталогом: {}".format(src))
    try:
        if src.resolve() == sdir.resolve():
            raise SnapshotError(
                "Источник совпадает с каталогом хранения снапшотов: {}".format(src)
            )
    except (OSError, ValueError):
        pass
    if not is_subvolume(src):
        raise SnapshotError(
            "Источник не является Btrfs subvolume: {} "
            "(резервное копирование Btrfs subvolume невозможно)".format(src)
        )

    try:
        sdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SnapshotError(
            "Не удалось создать каталог снапшотов {}: {}".format(sdir, exc)
        ) from exc

    # Снапшот btrfs возможен только внутри одного тома.
    ensure_same_btrfs_filesystem(src, sdir, label=subvolume_name)

    dest = sdir / label
    if dest.exists():
        raise SnapshotError(
            "Объект с именем снапшота уже существует, перезапись запрещена: "
            "{}".format(dest)
        )

    run_command(
        ["btrfs", "subvolume", "snapshot", "-r", str(src), str(dest)],
        error_class=SnapshotError,
        what="Создание read-only снапшота {}".format(label),
        timeout=600,
    )

    # ТЗ 5.2 п.6: проверка свойства read-only у созданного снапшота.
    ro = subvolume_ro_property(dest)
    if ro is False:
        raise SnapshotError(
            "Снапшот {} создан БЕЗ режима read-only — операция отклонена".format(dest)
        )
    if ro is None:
        raise SnapshotError(
            "Не удалось определить свойство read-only для снапшота {} "
            "(btrfs property get недоступен или завершился ошибкой). "
            "Продолжение архивирования запрещено.".format(dest)
        )

    log.info("Создан read-only снапшот: %s (источник: %s)", dest, src)
    return dest


# ============================================
# Просмотр
# ============================================

def list_snapshots(snapshot_dir, subvolume_names=None):
    """
    Возвращает {имя subvolume: [SnapshotInfo, ...]} — только объекты,
    соответствующие строгому шаблону именования приложения (все поколения),
    отсортированные по времени по убыванию.
    """
    result = {}
    sdir = Path(snapshot_dir)
    if not sdir.is_dir():
        return result
    for entry in sdir.iterdir():
        if not entry.is_dir():
            continue
        match = SNAPSHOT_NAME_RE.match(entry.name)
        if match is None:
            continue
        name = match.group("name")
        if subvolume_names is not None and name not in subvolume_names:
            continue
        info = SnapshotInfo(name, match.group("ts"), entry)
        result.setdefault(name, []).append(info)
    for items in result.values():
        items.sort(key=lambda s: s.sort_key, reverse=True)
    return result


# ============================================
# Удаление
# ============================================

def select_for_deletion(sorted_desc, retention):
    """Выбирает снапшоты сверх retention из списка, отсортированного по убыванию."""
    if retention is None or retention < 0:
        return []
    return list(sorted_desc[retention:])


# ============================================
# Обнаружение subvolume в системе
# Замечание пользователя: "нет выбора разделов, с которых делать
# снапшот и бэкап" — конфигурация может содержать один раздел, а выбрать
# пользователь хочет из всех subvolume Btrfs, доступных в системе.
# ============================================

# Максимальная глубина пути subvolume относительно корня ФС: отсекает
# служебные subvolume контейнеров (docker: var/lib/docker/btrfs/...),
# но включает типовые точки монтирования (home, opt, srv, var/...).
MAX_LIST_DEPTH = 3

_LABEL_ALLOWED_RE = re.compile(r"[^A-Za-z0-9._-]")


def parse_mount_options(opts):
    """'rw,relatime,subvol=/@home' -> {'rw': '', 'subvol': '/@home'}."""
    result = {}
    for token in str(opts or "").split(","):
        key, _, value = token.partition("=")
        if key:
            result[key] = value
    return result


def btrfs_mounts(mount_lines):
    """
    Монтирования Btrfs из строк /proc/self/mounts (параметр передается
    явно для тестируемости). Возвращает [(точка_монтирования, устройство,
    subvol)] — subvol это путь смонтированного subvolume относительно
    корня файловой системы ("/" для корня ФС, опция subvol= отсутствует).
    """
    result = []
    for line in mount_lines or []:
        parts = line.split()
        if len(parts) < 4 or parts[2] != "btrfs":
            continue
        mnt = _unescape_mount_field(parts[1])
        device = parts[0]
        subvol = parse_mount_options(parts[3]).get("subvol") or "/"
        result.append((mnt, device, subvol))
    return result


# Строка списка subvolume — РЕАЛЬНЫЙ формат btrfs-progs
# (btrfs-subvolume.html, фактический вывод 4.4-6.x):
#   ID <id> gen <gen> top level <top> path <путь>
# Колонки разделяются ОДНИМ пробелом; путь — всё после «path » ДОСЛОВНО.
_SUBVOL_LIST_LINE_RE = re.compile(
    r"^ID (?P<subvolid>\d+) gen \d+ top level \d+ path (?P<path>.*)$")


def _parse_subvolume_lines(list_output):
    """
    Разбор строк `btrfs subvolume list`.

    РЕАЛЬНЫЙ формат вывода поддерживаемых версий btrfs-progs (документация:
    https://btrfs.readthedocs.io/en/latest/btrfs-subvolume.html — «ID *ID*
    gen *generation* top level *parent_ID* path *path*»; фактический вывод
    содержит текстовые метки колонок, подтверждено на btrfs-progs 4.4-6.x):

        ID 256 gen 39 top level 5 path boot
        ID 257 gen 75 top level 5 path var/lib/docker

    Путь — ВСЁ после структурной метки «path » (одна колонка-разделитель)
    ДОСЛОВНО: несколько подряд пробелов, табуляция, начальные/конечные
    пробелы имени сохраняются без нормализации (btrfs допускает такие
    имена; btrfs-progs печатает путь как есть, без экранирования).
    Разбиение строки на токены здесь недопустимо — оно разрушает такие
    имена. Структурные поля до метки в путь не включаются.

    Возвращается [(id, путь_относительно_корня_ФС)]; ведущий '/' добавляется
    как прежде (контракт функции для reachable_subvolume_paths и
    discover_btrfs_filesystems). Строки не реального формата пропускаются
    С ДИАГНОСТИКОЙ в журнал (log.warning) — никогда не трактуются как
    «голые» токены и не превращают ошибку разбора в успешный результат.
    """
    result = []
    for line in (list_output or "").splitlines():
        if not line.strip():
            continue  # пустая строка — не содержательная запись вывода
        match = _SUBVOL_LIST_LINE_RE.match(line)
        if match is None:
            log.warning(
                "btrfs subvolume list: строка не соответствует формату "
                "'ID <id> gen <gen> top level <top> path <путь>', "
                "пропущена: %r", line)
            continue
        rel = match.group("path")
        if not rel:
            # После «path » пусто: реальная утилита так не печатает.
            log.warning(
                "btrfs subvolume list: пустой путь в строке, пропущена: %r",
                line)
            continue
        if not rel.startswith("/"):
            rel = "/" + rel
        result.append((match.group("subvolid"), rel))
    return result


def reachable_subvolume_paths(list_output, mount_point, mounted_subvol):
    """
    Абсолютные пути subvolume, ДОСТУПНЫЕ через данное монтирование.

    list_output — вывод `btrfs subvolume list <точка_монтирования>`;
    путь в нем задается ОТНОСИТЕЛЬНО КОРНЯ файловой системы, а не точки
    монтирования. Доступен через монтирование только subvolume,
    являющийся самим смонтированным subvolume или его потомком
    (замечание: при стандартной разметке Ubuntu монтируется /@,
    и subvolume "@home" через такое монтирование НЕ доступен — он виден
    через собственное монтирование /home с subvol=/@home).

    Глубина пути ограничена MAX_LIST_DEPTH (отсекает служебные
    subvolume контейнеров).
    """
    prefix = (mounted_subvol or "/").rstrip("/")  # "" для корня ФС
    paths = []
    for _subvolid, rel in _parse_subvolume_lines(list_output):
        if prefix and not (rel == prefix or rel.startswith(prefix + "/")):
            continue  # вне смонтированного поддерева
        depth = rel.strip("/").count("/") + 1
        if depth > MAX_LIST_DEPTH:
            continue
        suffix = rel[len(prefix):] if prefix else rel
        path = (mount_point.rstrip("/") + suffix).rstrip("/") or "/"
        paths.append(path)
    return paths


# ============================================
# Обозначение "устройство[subvol]": "/home" -> "/dev/sda1[/@home]" —
# при нескольких дисках с Btrfs видно, к какому диску относится каждый
# subvolume.
# ============================================

def read_mount_lines():
    """Строки /proc/self/mounts ([] при ошибке чтения)."""
    try:
        return Path("/proc/self/mounts").read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def btrfs_notation(path, mount_lines=None):
    """
    Обозначение 'устройство[subvol]' для пути path:
    '/home' -> '/dev/sda1[/@home]'. Разрешение — через САМОЕ ГЛУБОКОЕ
    монтирование-префикс среди ВСЕХ файловых систем: путь под vfat-
    монтированием /boot/efi не должен разрешаться через Btrfs-монтирование
    корня. Если это монтирование — не Btrfs, возвращается None.
    (Сама точка монтирования — subvol монтирования; путь внутри — subvol
    монтирования плюс относительный хвост.) mount_lines — строки
    /proc/self/mounts (параметр для автотестов).
    """
    if mount_lines is None:
        mount_lines = read_mount_lines()
    path = str(path).rstrip("/") or "/"
    best_fstype = None
    best_len = -1
    for line in mount_lines or []:
        parts = line.split()
        if len(parts) < 4:
            continue
        base = _unescape_mount_field(parts[1]).rstrip("/")
        if path == base or path.startswith(base + "/"):
            if len(base) > best_len:
                best_fstype, best_len = parts[2], len(base)
    if best_fstype != "btrfs":
        return None
    return btrfs_notation_for(path, btrfs_mounts(mount_lines))


def btrfs_notation_for(path, mounts):
    """Вариант btrfs_notation для готового списка btrfs_mounts (тесты)."""
    path = str(path).rstrip("/") or "/"
    best = None
    best_len = -1
    for mnt, device, subvol in mounts or []:
        base = mnt.rstrip("/")
        if path == base or path.startswith(base + "/"):
            if len(base) > best_len:
                best, best_len = (device, subvol, base), len(base)
    if best is None:
        return None
    device, subvol, base = best
    # Полный путь устройства без суффикса '[/subvol]' (важно для
    # /dev/mapper/*, /dev/disk/* — имя не восстанавливается).
    base_dev = str(device).split("[", 1)[0]
    if not base_dev:
        return None
    rel = path[len(base):].strip("/") if base else path.strip("/")
    mounted_sub = (subvol or "").rstrip("/")
    if rel:
        full = (mounted_sub + "/" + rel) if mounted_sub else "/" + rel
        return base_dev + "[" + full + "]"
    if mounted_sub:
        return base_dev + "[" + mounted_sub + "]"
    return base_dev


def subvolume_label(path):
    """
    Имя subvolume из пути: последний элемент; '@' заменяется на 'at'
    (типовая разметка Ubuntu: /@ -> at, /@home -> athome), недопустимые
    символы — на '_', имя, не начинающееся с латинской буквы/цифры,
    получает префикс 'sv_'. Результат соответствует шаблону имени
    subvolume (config._NAME_RE).
    """
    base = str(path).rstrip("/").rpartition("/")[2] or "root"
    label = base.replace("@", "at")
    label = _LABEL_ALLOWED_RE.sub("_", label)
    if not re.match(r"[A-Za-z0-9]", label):
        label = "sv_" + label
    return label[:64]


def _is_excluded(path, exclude_real):
    """True, если path совпадает с exclude_real или находится внутри него."""
    path = str(path).rstrip("/")
    for excluded in exclude_real:
        excluded = str(excluded).rstrip("/")
        if path == excluded or path.startswith(excluded + "/"):
            return True
    return False


def _mount_subvolid_options(mount_lines):
    """{точка монтирования: (subvol, subvolid)} из опций /proc/self/mounts."""
    result = {}
    for line in mount_lines or []:
        parts = line.split()
        if len(parts) < 4 or parts[2] != "btrfs":
            continue
        mnt = _unescape_mount_field(parts[1])
        opts = parse_mount_options(parts[3])
        result[mnt] = (opts.get("subvol") or "/", opts.get("subvolid"))
    return result


def _is_fs_root_subvolid(subvolid_option, subvol):
    """
    True, если монтирование — корень файловой системы Btrfs (subvolid=5,
    ТЗ 1.4.7 §3.3): либо опция subvolid=5 задана явно, либо не заданы ни
    subvol, ни subvolid (умолчание ядра — корень ФС). Монтирование
    вложенного subvolume (subvol=/@ и подобные) корнем ФС не является.
    """
    if subvolid_option is not None:
        return str(subvolid_option) == "5"
    return not subvol or subvol == "/"


def discover_btrfs_filesystems(mount_lines=None, timeout=60):
    """
    Обнаружение смонтированных файловых систем Btrfs.

    Основной идентификатор файловой системы — UUID Btrfs
    (`btrfs filesystem show` точки монтирования — она всегда корректна
    для этого запроса). Если UUID определить не удалось, группа строится
    по устройству (fallback), fs_uuid=None. Одна ФС может использовать
    несколько устройств — все они попадают в devices.

    Возвращает список ФС:
      [{fs_uuid, devices, mounts, subvolumes, errors}]
      fs_uuid    — UUID ФС или None (fallback по устройству);
      devices    — устройства ФС (без суффикса [/subvol]);
      mounts     — [(точка, subvol, is_fs_root)]; is_fs_root — корень
                   ФС с subvolid=5;
      subvolumes — [{id, source, fs_root}] — subvolume, доступные через
                   точки монтирования ФС, с идентификаторами;
      errors     — причины неудач `btrfs subvolume list` (код возврата,
                   stderr). Список subvolume НЕ подменяется одной точкой
                   монтирования: ФС с ошибкой не дает
                   источников, причина возвращается пользователю.
    """
    if mount_lines is None:
        mount_lines = read_mount_lines()
    mounts = btrfs_mounts(mount_lines)
    options = _mount_subvolid_options(mount_lines)
    fs_map = {}  # ключ группы -> данные ФС
    for mnt, device, subvol in mounts:
        base_device = device.split("[", 1)[0]
        uuid = _fs_uuid_query(mnt)
        key = ("uuid", uuid) if uuid else ("dev", base_device)
        fs = fs_map.setdefault(key, {
            "fs_uuid": uuid, "devices": [], "mounts": [],
            "subvolumes": [], "errors": [],
        })
        if uuid and not fs["fs_uuid"]:
            fs["fs_uuid"] = uuid
        if base_device not in fs["devices"]:
            fs["devices"].append(base_device)
        subvolid_opt = options.get(mnt, (subvol, None))[1]
        fs["mounts"].append(
            (mnt, subvol, _is_fs_root_subvolid(subvolid_opt, subvol)))
    for fs in fs_map.values():
        fs["devices"].sort()
        listing = None
        # Список subvolume описывает ВСЮ файловую систему: достаточно
        # одного успешного запроса по любой точке монтирования.
        for mnt, _subvol, _root in fs["mounts"]:
            try:
                proc = subprocess.run(
                    ["btrfs", "subvolume", "list", mnt],
                    capture_output=True, text=True, timeout=timeout,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                fs["errors"].append(
                    "btrfs subvolume list {}: не выполнен ({})".format(
                        mnt, exc))
                continue
            if proc.returncode != 0:
                stderr = (proc.stderr or "").strip()
                fs["errors"].append(
                    "btrfs subvolume list {}: код возврата {}{}".format(
                        mnt, proc.returncode,
                        "; stderr: {}".format(stderr[:500]) if stderr else ""))
                continue
            listing = proc.stdout
            break
        if listing is None:
            # ТЗ §3.2: при ошибке `btrfs subvolume list` точка монтирования
            # НЕ подменяет полный список subvolume; причина — в errors.
            continue
        for subvolid, rel in _parse_subvolume_lines(listing):
            for mnt, subvol, _root in fs["mounts"]:
                prefix = (subvol or "/").rstrip("/")
                if prefix and not (rel == prefix or
                                   rel.startswith(prefix + "/")):
                    continue
                depth = rel.strip("/").count("/") + 1
                if depth > MAX_LIST_DEPTH:
                    continue
                suffix = rel[len(prefix):] if prefix else rel
                path = (mnt.rstrip("/") + suffix).rstrip("/") or "/"
                fs["subvolumes"].append(
                    {"id": subvolid, "source": path, "fs_root": False,
                     "mount_point": mnt})
        # Точки монтирования тоже являются subvolume (ФС без
        # вложенных subvolume иначе вообще не попадала в выбор). Один и
        # тот же объект, обнаруженный и в списке, и как точка монтирования,
        # сливается по realpath в discover_sources.
        for mnt, _subvol, is_root in fs["mounts"]:
            fs["subvolumes"].append(
                {"id": None, "source": mnt, "fs_root": is_root,
                 "mount_point": mnt})
    return list(fs_map.values())


def discover_sources(exclude_dirs=(), timeout=60, mount_lines=None):
    """
    Обнаружение источников резервного копирования.

    Возвращает {"sources": [{name, source, id, fs_uuid, fs_root,
    subvolume_uuid, mount_point, devices}], "errors": [строки]}.
    Каждый источник привязан к UUID своей файловой системы (fs_uuid;
    None — UUID не определен, группа по устройству) и, если доступно,
    несет UUID subvolume (btrfs subvolume show), точку монтирования,
    через которую он доступен, и устройства ФС.
    Источники ФС, для которых `btrfs subvolume list` завершился ошибкой,
    не создаются, а причина возвращается в errors — пользователь получает
    понятное сообщение с возможностью посмотреть подробности.
    """
    if mount_lines is None:
        mount_lines = read_mount_lines()
    filesystems = discover_btrfs_filesystems(mount_lines, timeout=timeout)
    errors = []
    for fs in filesystems:
        errors.extend(fs["errors"])
    exclude_real = []
    for directory in exclude_dirs:
        try:
            exclude_real.append(os.path.realpath(str(directory)))
        except (OSError, ValueError):
            exclude_real.append(str(directory))
    found = {}  # realpath -> запись
    for fs in filesystems:
        for sub in fs["subvolumes"]:
            path = sub["source"]
            try:
                real = os.path.realpath(path)
            except (OSError, ValueError):
                continue
            if _is_excluded(real, exclude_real):
                continue
            if not os.path.isdir(real):
                continue
            if not os.access(real, os.R_OK | os.X_OK):
                continue
            existing = found.get(real)
            if existing is not None:
                # Один объект обнаружен и как элемент списка (с id), и как
                # точка монтирования: слияние сведений.
                if existing["id"] is None and sub["id"] is not None:
                    existing["id"] = sub["id"]
                existing["fs_root"] = existing["fs_root"] or \
                    bool(sub["fs_root"])
                continue
            found[real] = {
                "source": path, "id": sub["id"],
                "fs_uuid": fs["fs_uuid"], "fs_root": bool(sub["fs_root"]),
                "mount_point": sub.get("mount_point"),
                "devices": list(fs["devices"]),
            }
    result = []
    used = set()
    for real in sorted(found):
        info = found[real]
        # ТЗ 1.4.8 §4.4: UUID subvolume (если доступен) — один запрос
        # `btrfs subvolume show` на итоговый источник.
        info["subvolume_uuid"] = subvolume_uuid(info["source"])
        base_name = subvolume_label(info["source"])
        name, counter = base_name, 2
        while name in used:
            name = "{}_{}".format(base_name, counter)
            counter += 1
        used.add(name)
        result.append({
            "name": name, "source": info["source"], "id": info["id"],
            "fs_uuid": info["fs_uuid"], "fs_root": info["fs_root"],
            "subvolume_uuid": info["subvolume_uuid"],
            "mount_point": info["mount_point"],
            "devices": info["devices"],
        })
    return {"sources": result, "errors": errors}


# ============================================
# Разделы дисков. Разделы, которые НЕ могут быть источником
# (не Btrfs / не смонтирован), показываются интерфейсами с причиной
# и без возможности выбора.
# ============================================

# Учитываемые типы устройств lsblk (loop/ram/rom отсекаются по имени).
_PARTITION_TYPES = ("part", "disk", "lvm", "crypt", "dm", "raid")
_SKIP_DEVICE_RE = re.compile(r"^(loop|ram|zram|sr|fd)")


def device_base_name(device_field):
    """'/dev/sda2[/@]' -> 'sda2' (имя устройства без пути и суффикса subvol)."""
    base = str(device_field).split("[", 1)[0]
    return base.rstrip("/").rpartition("/")[2]


def device_mount_points(mounts=None):
    """
    {имя устройства: [точки монтирования]} по /proc/self/mounts — для
    ЛЮБЫХ файловых систем (не только Btrfs).
    """
    if mounts is None:
        mounts = _parse_mounts()
    result = {}
    for mount_point, _fstype, device in mounts:
        name = device_base_name(device)
        if name:
            result.setdefault(name, []).append(mount_point)
    return result


def parse_lsblk_partitions(lines):
    """
    Разбор вывода `lsblk -rno NAME,TYPE,FSTYPE` (сырой формат: поля через
    пробел, пустые поля в конце строки отсутствуют). Возвращает
    [{name, device, fstype}] для дисков и разделов с ОПРЕДЕЛЕННОЙ
    файловой системой; служебные устройства (loop, zram, ...) исключены.
    """
    result = []
    for line in lines or []:
        fields = str(line).strip().split(None, 2)
        if len(fields) < 2:
            continue
        name, dtype = fields[0], fields[1]
        # fstype — первый токен (значения ФС не содержат пробелов); защита
        # от случайных лишних полей в конце строки.
        tokens = fields[2].split() if len(fields) > 2 else []
        fstype = tokens[0].lower() if tokens else ""
        if dtype not in _PARTITION_TYPES or not fstype:
            continue  # раздел без ФС (extended, пустой) — не источник
        if _SKIP_DEVICE_RE.match(name):
            continue
        result.append({"name": name,
                       "device": "/dev/" + name,
                       "fstype": fstype})
    return result


def list_partitions(timeout=15, lsblk_lines=None, mounts=None):
    """
    Разделы дисков для экрана выбора. Возвращает
    [{name, device, fstype, mounted, mount_points}]; mounted/точки
    монтирования определяются по /proc/self/mounts. Ошибки (нет lsblk,
    таймаут) не прерывают операцию: возвращается [].
    """
    if lsblk_lines is None:
        try:
            proc = subprocess.run(
                ["lsblk", "-r", "-n", "-o", "NAME,TYPE,FSTYPE"],
                capture_output=True, text=True, timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if proc.returncode != 0:
            return []
        lsblk_lines = proc.stdout.splitlines()
    by_mount = device_mount_points(mounts)
    result = []
    for part in parse_lsblk_partitions(lsblk_lines):
        points = by_mount.get(part["name"], [])
        entry = dict(part)
        entry["mounted"] = bool(points)
        entry["mount_points"] = points
        result.append(entry)
    return result


def delete_snapshot(path, snapshot_dir, *, protected_paths=()):
    """
    Удаляет снапшот `btrfs subvolume delete` с обязательными проверками:

      1) строгое имя шаблона приложения;
      2) расположение ВНУТРИ каталога снапшотов и строго ПРЯМЫМ потомком
         его (исключает удаление самого каталога, вложенных и внешних
         объектов);
      3) объект является каталогом;
      4) объект является Btrfs subvolume (авторитетная проверка);
      5) объект НЕ входит в protected_paths (исходные рабочие subvolume и
         снапшоты текущей операции);
      6) свойство read-only ОДНОЗНАЧНО равно True: ro=False (доступен на
         запись) И ro=None (определить не удалось) И ошибка определения
         — удаление ЗАПРЕЩЕНО (fail-closed, ТЗ 4.2).

    Состояние read-only проверяется НЕПОСРЕДСТВЕННО ПЕРЕД удалением;
    имя каталога, метаданные или записи журнала свойство не заменяют.
    Если хотя бы одна проверка не пройдена — SnapshotError, удаление
    не выполняется.
    """
    p = Path(path)
    if SNAPSHOT_NAME_RE.match(p.name) is None:
        raise SnapshotError(
            "Отказ: имя '{}' не соответствует шаблону приложения".format(p.name)
        )
    try:
        p_real = p.resolve()
        sdir_real = Path(snapshot_dir).resolve()
    except (ValueError, OSError) as exc:
        raise SnapshotError(
            "Отказ: не удалось определить реальный путь '{}' ({}); "
            "удаление запрещено".format(p, exc)
        )
    try:
        p_real.relative_to(sdir_real)
    except ValueError:
        raise SnapshotError(
            "Отказ: '{}' находится вне каталога снапшотов".format(p)
        )
    if p_real.parent != sdir_real:
        raise SnapshotError(
            "Отказ: '{}' не является прямым потомком каталога снапшотов "
            "{}; удаление разрешено только для снапшотов верхнего уровня"
            .format(p, snapshot_dir)
        )
    if not p.is_dir():
        raise SnapshotError("Отказ: '{}' не является каталогом".format(p))
    if not is_subvolume(p):
        raise SnapshotError(
            "Отказ: '{}' не является Btrfs subvolume".format(p)
        )
    for protected in protected_paths:
        try:
            if p_real == Path(protected).resolve():
                raise SnapshotError(
                    "Отказ: '{}' является защищенным объектом (исходный "
                    "рабочий subvolume или снапшот текущей операции); "
                    "удаление запрещено".format(p)
                )
        except (OSError, ValueError):
            # Защищенный путь не разрешается — консервативно считаем
            # пересечение возможным и запрещаем удаление (fail-closed).
            raise SnapshotError(
                "Отказ: не удалось проверить защищенный путь '{}'; "
                "удаление '{}' запрещено".format(protected, p)
            )
    try:
        ro = subvolume_ro_property(p)
    except Exception as exc:  # noqa: BLE001 — fail-closed при любой ошибке
        raise SnapshotError(
            "Отказ: ошибка определения свойства read-only для '{}' ({}): "
            "удаление запрещено (неизвестное состояние трактуется как "
            "запрет)".format(p, exc)
        )
    if ro is False:
        raise SnapshotError(
            "Отказ: '{}' доступен на запись (ro=false) — удаление "
            "запрещено: read-only снапшот мог быть подменен или изменен".format(p)
        )
    if ro is None:
        raise SnapshotError(
            "Отказ: не удалось определить свойство read-only для '{}' "
            "(btrfs property get недоступен или завершился ошибкой) — "
            "удаление запрещено (fail-closed: неизвестное состояние "
            "трактуется как запрет)".format(p)
        )

    run_command(
        ["btrfs", "subvolume", "delete", str(p)],
        error_class=SnapshotError,
        what="Удаление снапшота {}".format(p.name),
        timeout=600,
    )
    log.info("Снапшот удален (retention): %s", p.name)
