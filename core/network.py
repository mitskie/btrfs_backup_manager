"""
Локальное и сетевое хранилище.

Программа никогда не выполняет mount/umount: сетевая шара должна быть
смонтирована заранее (проверяется реальное состояние точки
монтирования средствами Linux, а не существование каталога).

Копирование выполняется во временный файл *.tmp на сетевой шаре
с последующей проверкой размера и SHA-256 и атомарным переименованием.
Сетевые архивы не удаляются НИКОГДА.
"""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import StorageError
from .compression import compression_ext
from .logging_setup import get_logger
from .process import human_size
from .verification import parse_checksum_file, sha256_file

log = get_logger("network")


@dataclass(frozen=True)
class MountInfo:
    mount_point: str
    fstype: str
    writable: bool
    free_bytes: int


# ============================================
# Локальное хранилище
# ============================================

def delete_local_archive(archive_path):
    """
    Удаляет локальный архив и его sidecar-файлы (.sha256, .json).
    Автоматический вызывающий код должен предварительно подтвердить
    сетевую отправку; ручное удаление допускается только после явного
    пользовательского подтверждения в интерфейсе.
    """
    archive_path = Path(archive_path)
    checksum_path = archive_path.with_name(archive_path.name + ".sha256")
    meta_path = archive_path.with_name(archive_path.name + ".json")
    for target, what in ((archive_path, "архив"),
                         (checksum_path, "контрольная сумма"),
                         (meta_path, "метаданные")):
        try:
            target.unlink()
            log.info("Удален локальный %s: %s", what, target.name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("Не удалось удалить %s %s: %s", what, target, exc)


# ============================================
# Проверка монтирования
# ============================================

# Поля /proc/self/mountinfo, экранируемые восьмеричными последовательностями
_MOUNTINFO_ESCAPES = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}


def _unescape_mountinfo_field(field):
    """Раскодирует \\040 и т.п. в полях /proc/self/mountinfo."""
    result = []
    index = 0
    while index < len(field):
        char = field[index]
        if char == "\\" and index + 3 < len(field) + 1 and \
                field[index + 1:index + 4] in _MOUNTINFO_ESCAPES:
            result.append(_MOUNTINFO_ESCAPES[field[index + 1:index + 4]])
            index += 4
        else:
            result.append(char)
            index += 1
    return "".join(result)


def _mountinfo_entries():
    """
    Записи /proc/self/mountinfo: [(точка_монтирования, тип_ФС, источник)].

    Источник истины о фактических монтированиях ядра (надежнее
    os.path.ismount, который по одному st_dev не отличает bind-монтирования
    и не видит тип ФС). None — /proc/self/mountinfo недоступен (тогда
    check_network_mount переходит на os.path.ismount как на запасной
    механизм). Параметр-функция допускает подмену в тестах.
    """
    try:
        content = Path("/proc/self/mountinfo").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    entries = []
    for line in content.splitlines():
        # Формат: id parent major:minor root mount_point options ... - fstype source super_options
        if " - " not in line:
            continue
        left, _, right = line.partition(" - ")
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 5 or len(right_fields) < 2:
            continue
        mount_point = _unescape_mountinfo_field(left_fields[4])
        entries.append((mount_point, right_fields[0], right_fields[1]))
    return entries


def mountinfo_for(path, entries=None):
    """
    (точка_монтирования, тип_ФС, источник) для пути по /proc/self/mountinfo
    — САМОЕ ГЛУБОКОЕ монтирование-префикс. None — путь не является точкой
    монтирования и не находится под монтированием (для реального
    монтирования совпадение с точкой всегда находится).
    """
    entries = _mountinfo_entries() if entries is None else entries
    if entries is None:
        return None
    target = os.path.realpath(str(path))
    target = target.rstrip("/") or "/"
    best = None
    for mount_point, fstype, source in entries:
        base = mount_point.rstrip("/") or "/"
        if target == base or target.startswith(base + "/"):
            if best is None or len(mount_point) > len(best[0]):
                best = (mount_point, fstype, source)
    return best


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
            field = parts[1]
            try:
                field = field.encode("ascii", "backslashreplace").decode(
                    "unicode_escape"
                )
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
            mounts.append((field, parts[2]))
    return mounts


def mount_info_for(path):
    """(точка монтирования, тип ФС) для пути по /proc/self/mounts."""
    target = os.path.realpath(str(path))
    best = None
    for mount_point, fstype in _parse_mounts():
        if target == mount_point or target.startswith(mount_point.rstrip("/") + "/"):
            if best is None or len(mount_point) > len(best[0]):
                best = (mount_point, fstype)
    return best


def _verify_real_mountpoint(mount):
    """
    ОБЯЗАТЕЛЬНАЯ проверка, что каталог является АКТИВНОЙ точкой
    монтирования (не отключается настройками, ТЗ 3.2 исправление №1):
      1) /proc/self/mountinfo содержит запись для этого каталога;
      2) дополнительно os.path.ismount (второй независимый механизм).
    Обычный локальный каталог проверку не проходит.
    Возвращает (точка, тип_ФС, источник) или возбуждает StorageError.
    """
    mount_path = Path(mount)
    real = os.path.realpath(str(mount_path))
    entries = _mountinfo_entries()
    if entries is None:
        # /proc/self/mountinfo недоступен (нестандартная среда) — запасной
        # механизм: os.path.ismount. Отключить проверку это не позволяет:
        # обычный каталог по-прежнему отвергается.
        if not os.path.ismount(real):
            raise StorageError(
                "Каталог {} не является активной точкой монтирования "
                "(обычный каталог; /proc/self/mountinfo недоступен). "
                "Сетевая шара должна быть смонтирована заранее; программа "
                "не выполняет mount самостоятельно.".format(mount)
            )
        info = mount_info_for(mount)
        if info is None:
            return (real, "unknown", "")
        mount_point, fstype = info
        return (mount_point, fstype, "")
    entry = mountinfo_for(real, entries)
    if entry is None:
        raise StorageError(
            "Каталог {} НЕ является точкой монтирования "
            "(/proc/self/mountinfo не содержит записи для него): обычный "
            "локальный каталог не считается сетевым хранилищем. "
            "Смонтируйте шару и повторите операцию.".format(mount)
        )
    if not os.path.ismount(real):
        # mountinfo знает о монтировании, но st_dev совпадает с родителем:
        # для настоящих монтирований этого не бывает — отказ в пользу
        # безопасности.
        raise StorageError(
            "Каталог {} не подтвержден как точка монтирования "
            "(mountinfo содержит запись, но проверка os.path.ismount ее "
            "не подтверждает); операция запрещена.".format(mount)
        )
    return entry


def check_network_mount(mount, *, expected_fs_type=None, check=True):
    """
    Проверяет точку монтирования сетевой шары:
      - существует и является каталогом;
      - является АКТИВНОЙ точкой монтирования — ВСЕГДА, независимо от
        настроек (параметр `check` НЕ отключает эту проверку с v1.4.4:
        он управляет только дополнительной пробой записи);
      - тип файловой системы соответствует ожидаемому (если задан);
      - доступна для чтения; при check=True — и для записи;
      - определяет свободное место (если доступно).

    Обычный локальный каталог в точке монтирования НЕ считается сетевым
    хранилищем НИКОГДА (CHECK_NETWORK_MOUNT не может отключить
    обязательную проверку безопасности перед сетевой отправкой).
    """
    mount_path = Path(mount)
    if not mount_path.exists():
        raise StorageError(
            "Точка монтирования сетевой шары не существует: {}. "
            "Смонтируйте шару и повторите запуск.".format(mount)
        )
    if not mount_path.is_dir():
        raise StorageError(
            "Точка монтирования не является каталогом: {}".format(mount)
        )
    mount_point, fstype, _source = _verify_real_mountpoint(mount)
    if expected_fs_type and fstype != expected_fs_type:
        raise StorageError(
            "Тип файловой системы в {} — '{}', ожидалось '{}'".format(
                mount, fstype, expected_fs_type
            )
        )
    if not os.access(str(mount_path), os.R_OK | os.X_OK):
        raise StorageError(
            "Нет доступа на чтение к {}: проверка монтирования не пройдена"
            .format(mount)
        )

    if check:
        write_test = mount_path / ".btrfs-backup-write-test-{}".format(os.getpid())
        try:
            with open(str(write_test), "w", encoding="ascii") as handle:
                handle.write("ok")
            write_test.unlink()
        except OSError as exc:
            raise StorageError(
                "Нет доступа на запись в {}: {}".format(mount, exc)
            ) from exc

    free = None
    try:
        free = shutil.disk_usage(str(mount_path)).free
    except OSError:
        pass
    log.info(
        "Сетевая шара %s: смонтирована (%s), запись доступна%s",
        mount, fstype,
        ", свободно {}".format(human_size(free)) if free is not None else "",
    )
    return MountInfo(mount_point, fstype, True, free if free is not None else -1)


def require_network_mount(mount, *, expected_fs_type=None):
    """
    ОБЯЗАТЕЛЬНАЯ проверка активного монтирования сетевой шары (включая
    пробу записи) — НЕ зависит от CHECK_NETWORK_MOUNT. Используется перед
    любыми операциями, на основании которых принимается решение об
    удалении локальных данных (retention, финализация сетевой копии,
    upload-pending) — ТЗ 7.3, 9, 18.
    """
    return check_network_mount(mount, expected_fs_type=expected_fs_type, check=True)


def recheck_network_mount(mount, *, expected_fs_type=None):
    """
    Обязательная ПОВТОРНАЯ проверка монтирования сетевой шары:
    выполняется на каждом опасном этапе сетевой отправки — перед
    копированием, перед финализацией, перед записью метаданных и
    непосредственно перед удалением локального архива. Легкая (без пробы
    записи): факт монтирования по mountinfo + чтение + ожидаемый тип ФС.
    Любая ошибка — StorageError: отправка считается неуспешной, локальный
    архив сохраняется.
    """
    mount_path = Path(mount)
    if not mount_path.exists():
        raise StorageError(
            "Повторная проверка: точка монтирования {} исчезла "
            "(сетевая шара отключилась?)".format(mount)
        )
    if not mount_path.is_dir():
        raise StorageError(
            "Повторная проверка: {} больше не является каталогом".format(mount)
        )
    mount_point, fstype, _source = _verify_real_mountpoint(mount)
    if expected_fs_type and fstype != expected_fs_type:
        raise StorageError(
            "Повторная проверка: тип файловой системы в {} — '{}', "
            "ожидалось '{}' (шара отключилась или заменена?)".format(
                mount, fstype, expected_fs_type
            )
        )
    if not os.access(str(mount_path), os.R_OK | os.X_OK):
        raise StorageError(
            "Повторная проверка: нет доступа на чтение к {}".format(mount)
        )
    return MountInfo(mount_point, fstype, True, -1)


def require_network_mount_for_retention(mount, net_dir, *,
                                        expected_fs_type=None):
    """
    Обязательная проверка сетевой шары ПЕРЕД удалением снапшотов (retention).

    1) точка монтирования должна быть активным монтированием (всегда,
       независимо от CHECK_NETWORK_MOUNT);
    2) каталог сетевых архивов должен лежать на ТОЙ ЖЕ файловой системе,
       что и точка монтирования: исключается подмена копиями, оставшимися
       в локальном каталоге под точкой монтирования (например, когда шара
       не смонтирована, а файлы остались от предыдущего монтирования).

    Любая ошибка означает запрет удаления снапшотов.
    """
    info = require_network_mount(mount, expected_fs_type=expected_fs_type)
    net_path = Path(net_dir)
    mount_path = Path(mount)
    if net_path.exists():
        try:
            net_dev = os.stat(str(net_path)).st_dev
            mount_dev = os.stat(str(mount_path)).st_dev
        except OSError as exc:
            raise StorageError(
                "Не удалось определить файловую систему каталога {}: {}. "
                "Очистка отменена.".format(net_path, exc)
            ) from exc
        if net_dev != mount_dev:
            raise StorageError(
                "Каталог {} находится на другой файловой системе, чем точка "
                "монтирования {}: возможно, проверяются файлы, оставшиеся в "
                "локальном каталоге под точкой монтирования. Очистка "
                "отменена; проверьте монтирование сетевой шары.".format(
                    net_path, mount
                )
            )
    return info


def ensure_network_dir(mount, network_directory):
    """Каталог внутри сетевой шары (создается, если отсутствует)."""
    net_dir = Path(mount) / network_directory
    try:
        net_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageError(
            "Не удалось создать каталог на сетевой шаре {}: {}".format(net_dir, exc)
        ) from exc
    return net_dir


def check_network_free_space(net_dir, min_free_bytes):
    """Проверяет минимум свободного места (если он задан и определяется)."""
    try:
        free = shutil.disk_usage(str(net_dir)).free
    except OSError:
        log.info("Свободное место на сетевой шаре определить не удалось")
        return None
    if min_free_bytes and free < min_free_bytes:
        raise StorageError(
            "На сетевой шаре недостаточно места: свободно {}, требуется не "
            "менее {}".format(human_size(free), human_size(min_free_bytes))
        )
    return free


# ============================================
# Отправка на сетевую шару
# ============================================

def copy_to_network_tmp(local_archive, net_dir, expected_sha):
    """
    Копирует архив во временный файл <имя>.tmp на сетевой шаре и
    проверяет РАЗМЕР и SHA-256 временной копии (обе проверки обязательны
    перед финализацией, ТЗ 7.3 пп.4-5).

    Возвращает (путь tmp, sha256). Временный файл удаляется при любой ошибке.
    """
    local_archive = Path(local_archive)
    net_dir = Path(net_dir)
    tmp_path = net_dir / (local_archive.name + ".tmp")
    try:
        try:
            source_size = os.stat(str(local_archive)).st_size
            with open(str(local_archive), "rb") as src, \
                    open(str(tmp_path), "wb") as dst:
                shutil.copyfileobj(src, dst, length=4 * 1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
        except OSError as exc:
            raise StorageError(
                "Ошибка копирования {} на сетевую шару: {}".format(
                    local_archive.name, exc
                )
            ) from exc
        # Контроль размера ДО вычисления контрольной суммы: обрыв копирования
        # по месту/сети обнаруживается сразу и без больших вычислений.
        try:
            copied_size = os.stat(str(tmp_path)).st_size
        except OSError as exc:
            raise StorageError(
                "Не удалось определить размер временной копии {}: {}".format(
                    tmp_path.name, exc
                )
            ) from exc
        if copied_size != source_size:
            raise StorageError(
                "Размер временной копии '{}' на сетевой шаре не совпадает с "
                "исходным архивом ({} != {} байт).".format(
                    tmp_path.name, copied_size, source_size
                )
            )
        actual = sha256_file(tmp_path)
        if actual != expected_sha:
            raise StorageError(
                "SHA-256 временной копии '{}' на сетевой шаре не совпал: "
                "{} != {}".format(tmp_path.name, actual, expected_sha)
            )
        log.info(
            "Архив скопирован во временный файл %s (размер и SHA-256 "
            "совпадают)",
            tmp_path.name,
        )
        return tmp_path, actual
    except BaseException:
        try:
            tmp_path.unlink()
            log.info("Удален временный файл на сетевой шаре: %s", tmp_path.name)
        except OSError:
            log.warning(
                "Не удалось удалить временный файл на сетевой шаре: %s",
                tmp_path,
            )
        raise


def _fsync_directory(directory):
    """
    Синхронизирует каталог на диске (best effort): после os.replace
    гарантирует, что запись имени файла переживает сбой питания.
    Не все файловые системы поддерживают открытие каталога — ошибки
    игнорируются (атомарность самой записи это не отменяет).
    """
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def write_network_metadata(net_dir, meta, expected_sha):
    """
    АТОМАРНО записывает sidecar-метаданные рядом с сетевым архивом
    (после финализации) и ПОДТВЕРЖДАЕТ запись повторным чтением.

    Порядок (атомарность):
      1) проверка конфликта с существующими метаданными;
      2) временный файл <имя>.json.tmp В ТОМ ЖЕ каталоге;
      3) запись + flush + fsync;
      4) контроль корректности JSON перечитыванием временного файла;
      5) атомарное переименование os.replace;
      6) синхронизация каталога (best effort);
      7) перечитывание итогового файла и сверка sha256/archive.

    Конфликт: рядом уже есть метаданные этого же архива с ДРУГИМ sha256
    или ПОВРЕЖДЕННЫЙ/нечитаемый файл метаданных — перезапись запрещена
    (поврежденные метаданные не исправляются молча; без
    сохранения исходной информации перезапись запрещена).

    Возвращает True только при полном успехе (записано и подтверждено
    перечитыванием). False — при любой ошибке: вызывающий код ОБЯЗАН
    сохранить локальный архив и не считать отправку завершенной.
    """
    net_dir = Path(net_dir)
    target = net_dir / (meta["archive"] + ".json")
    payload = dict(meta)
    payload["status"] = "uploaded"
    payload["upload_status"] = "uploaded"
    if payload.get("uploaded_at") is None:
        from datetime import datetime
        payload["uploaded_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        content = json_dumps(payload)
        roundtrip = json_loads(content)
        if not isinstance(roundtrip, dict) or \
                roundtrip.get("archive") != str(meta["archive"]) or \
                str(roundtrip.get("sha256") or "").lower() != \
                str(expected_sha or "").lower():
            log.warning(
                "Сформированные метаданные %s не проходят контроль "
                "корректности JSON; запись отменена", target.name,
            )
            return False
    except (ValueError, TypeError, KeyError) as exc:
        log.warning(
            "Не удалось сформировать корректный JSON метаданных %s: %s; "
            "запись отменена", target.name, exc,
        )
        return False
    tmp_path = target.with_name(target.name + ".tmp")
    try:
        if target.exists():
            try:
                existing_raw = target.read_text(encoding="utf-8")
                existing = json_loads(existing_raw)
            except (OSError, ValueError) as exc:
                # Существующий sidecar поврежден или нечитаем: исходная
                # информация не может быть сохранена, поэтому молча
                # перезаписывать запрещено.
                log.warning(
                    "Существующие сетевые метаданные %s повреждены или "
                    "нечитаемы (%s); перезапись запрещена — конфликт. "
                    "Локальный архив сохраняется; устраните конфликт "
                    "вручную.", target.name, exc,
                )
                return False
            if isinstance(existing, dict) and \
                    existing.get("sha256") not in (None, expected_sha):
                log.warning(
                    "Сетевые метаданные %s принадлежат другому содержимому; "
                    "перезапись запрещена", target.name,
                )
                return False
        with open(str(tmp_path), "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Контроль корректности записанного временного файла ДО переименования
        tmp_raw = tmp_path.read_text(encoding="utf-8")
        tmp_check = json_loads(tmp_raw)
        if not isinstance(tmp_check, dict) or \
                str(tmp_check.get("sha256") or "").lower() != \
                str(expected_sha or "").lower():
            log.warning(
                "Контроль временного файла метаданных %s не пройден; "
                "запись отменена", target.name,
            )
            tmp_path.unlink()
            return False
        os.replace(str(tmp_path), str(target))
        _fsync_directory(net_dir)
    except OSError as exc:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        log.warning("Не удалось записать сетевые метаданные %s: %s",
                    target.name, exc)
        return False
    except ValueError as exc:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        log.warning(
            "Контроль записанных метаданных %s не пройден (%s); "
            "запись отменена", target.name, exc,
        )
        return False
    # Повторное чтение итогового файла
    try:
        recheck = json_loads(target.read_text(encoding="utf-8"))
        if not isinstance(recheck, dict) or \
                str(recheck.get("sha256") or "").lower() != \
                str(expected_sha or "").lower() or \
                recheck.get("archive") != str(meta["archive"]):
            log.warning(
                "Повторное чтение метаданных %s НЕ подтвердило корректность; "
                "локальный архив сохраняется", target.name,
            )
            return False
    except (OSError, ValueError) as exc:
        log.warning(
            "Повторное чтение метаданных %s не удалось (%s); "
            "локальный архив сохраняется", target.name, exc,
        )
        return False
    log.info("Сетевые метаданные записаны и подтверждены: %s", target.name)
    return True


def json_dumps(payload):
    import json
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def json_loads(text):
    import json
    return json.loads(text)


def promote_network_archive(tmp_path, expected_sha):
    """
    Финализирует сетевую копию: атомарное переименование
    *.tmp -> *.gpg, проверка присутствия готового архива и повторное
    вычисление SHA-256. Отдельный файл .sha256 на шаре НЕ создается:
    контрольная сумма хранится в JSON-метаданных, которые записывает
    write_network_metadata() (зачем два sidecar-файла, если в JSON
    есть поле sha256).

    Повторяемость (идемпотентность, ТЗ 7.5): если архив уже присутствует
    на сетевой шаре и его SHA-256 совпадает, временный файл удаляется —
    копия подтверждается без перезаписи. Архив с НЕсовпадающим
    содержимым никогда не перезаписывается. Локальную копию вызывающий
    код удаляет только после успешного возврата (все проверки к этому
    моменту завершены).
    """
    tmp_path = Path(tmp_path)
    final_path = tmp_path.with_name(tmp_path.name[:-len(".tmp")])
    if final_path.exists():
        existing_sha = sha256_file(final_path)
        if existing_sha != expected_sha:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise StorageError(
                "На сетевой шаре уже существует '{}' с другим содержимым; "
                "перезапись запрещена. Локальная копия сохранена.".format(
                    final_path.name
                )
            )
        log.info(
            "Архив %s уже присутствует на сетевой шаре, содержимое совпадает",
            final_path.name,
        )
        try:
            tmp_path.unlink()
        except OSError:
            pass
    else:
        os.replace(str(tmp_path), str(final_path))

    if not final_path.is_file():
        raise StorageError(
            "После финализации отсутствует архив ({})".format(final_path)
        )
    # Повторное вычисление SHA-256 готовой сетевой копии
    recheck = sha256_file(final_path)
    if recheck != expected_sha:
        raise StorageError(
            "Повторная проверка SHA-256 сетевой копии '{}' не прошла".format(
                final_path.name
            )
        )
    log.info(
        "Сетевая копия готова: %s (SHA-256 подтвержден)",
        final_path.name,
    )
    return final_path


def network_metadata_index(net_dir):
    """
    Индекс сетевых метаданных:
    {имя_снапшота: [(имя_архива, метаданные), ...]} по *.json сетевой
    шары. Имя снапшота берется из поля "snapshot" метаданных; при его
    отсутствии — из имени архива (snapshot_name_from_archive).
    Поврежденные/нечитаемые метаданные в индекс НЕ включаются: они не
    могут подтвердить копию.
    """
    from .compression import snapshot_name_from_archive
    index = {}
    net_dir = Path(net_dir)
    if not net_dir.is_dir():
        return index
    for entry in sorted(net_dir.iterdir()):
        if not entry.name.endswith(".json") or not entry.is_file():
            continue
        try:
            raw = json_loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        archive_name = str(raw.get("archive") or "").strip()
        # Метаданные не должны выводить проверку за пределы сетевого
        # каталога (например, через ../archive.gpg или абсолютный путь).
        if (not archive_name or archive_name in (".", "..") or
                "/" in archive_name or "\\\\" in archive_name):
            continue
        snapshot_name = str(raw.get("snapshot") or "").strip()
        if not snapshot_name:
            snapshot_name = snapshot_name_from_archive(archive_name)
        if not snapshot_name:
            continue
        index.setdefault(snapshot_name, []).append((archive_name, raw))
    return index


def _verified_network_candidate(candidate, expected_reference=None):
    """
    Проверяет КОНКРЕТНЫЙ сетевой файл как подтвержденную копию:
    эталонный SHA-256 читается (JSON-метаданные или .sha256), фактический
    SHA-256 файла вычисляется и полностью совпадает. Возвращает True/False;
    ошибки доступа (OSError) трактуются консервативно как False.
    """
    try:
        # Символическая ссылка может увести проверку за пределы сетевой
        # шары и ложно подтвердить копию, после чего retention удалит
        # локальный снапшот. Для подтверждения принимаются только обычные
        # файлы непосредственно в каталоге архива.
        if candidate.is_symlink():
            log.warning(
                "retention: архив %s является символической ссылкой; "
                "копия не подтверждена, снапшот сохранен",
                candidate.name,
            )
            return False
        expected = _network_expected_sha(candidate)
        if expected is None:
            log.warning(
                "retention: у архива %s отсутствуют метаданные и "
                "файл контрольной суммы — копия не подтверждена, "
                "снапшот сохранен",
                candidate.name,
            )
            return False
        if expected_reference is not None and \
                str(expected).lower() != str(expected_reference).lower():
            log.warning(
                "retention: эталон %s не совпадает с sha256 из метаданных "
                "снапшота — копия не подтверждена, снапшот сохранен",
                candidate.name,
            )
            return False
        try:
            actual = sha256_file(candidate)
        except OSError as exc:
            log.warning(
                "retention: архив %s не читается (%s); снапшот сохранен",
                candidate.name, exc,
            )
            return False
        if actual != expected:
            log.warning(
                "retention: SHA-256 архива %s НЕ совпал (%s != %s) — "
                "копия повреждена, снапшот сохранен",
                candidate.name, expected, actual,
            )
            return False
    except OSError as exc:
        # Сетевая шара недоступна и т.п. — консервативно: не подтверждено
        log.warning(
            "retention: архив %s недоступен (%s); снапшот сохранен",
            candidate, exc,
        )
        return False
    log.info(
        "retention: копия %s подтверждена (SHA-256 совпадает)",
        candidate.name,
    )
    return True


def network_archive_confirmed(net_dir, snapshot_name, compression=None,
                              hostname=None) -> bool:
    """
    Полная проверка подтвержденности резервной копии снапшота на сетевой
    шаре.

    Алгоритм сжатия определяется ИНДИВИДУАЛЬНО ДЛЯ КАЖДОГО архива:
    проверяются ВСЕ допустимые форматы (.zst.gpg, .gz.gpg и поколение
    1.0 с .btrfs.), независимо от текущего COMPRESSION (параметр
    `compression`, если задан, лишь ставит свое расширение ПЕРВЫМ —
    он больше не сужает поиск одним алгоритмом).

    Порядок поиска:
      1) точные имена архивов из JSON-метаданных сетевой шары, у которых
         поле snapshot (или имя архива) соответствует снапшоту — самый
         надежный источник (метаданные пишутся при отправке);
      2) кандидаты по шаблону имени: [хост_]имя[.btrfs].{zst,gz}.gpg
         (оба поколения суффиксов, ТЗ 19).

    Копия считается подтвержденной ТОЛЬКО при выполнении всех условий:
      1) файл архива существует (точное имя, никакой подмены по
         совпадению subvolume/даты);
      2) рядом есть эталонная сумма: JSON-метаданные (поле sha256, v1.3)
         или файл .sha256 (предыдущие версии);
      3) эталон читается и содержит корректный SHA-256;
      4) фактический SHA-256 архива ПОЛНОСТЬЮ совпадает с ожидаемым.

    Поврежденный архив, отсутствующий/некорректный/недоступный эталон
    или ошибка доступа к сетевой шаре означают False: снапшот в этом
    случае НЕ удаляется. Сетевые архивы никогда не удаляются автоматически.
    """
    net_dir = Path(net_dir)
    if hostname is None:
        from .environment import hostname as detect_hostname
        hostname = detect_hostname()
    prefixes = ["{}_".format(hostname)] if hostname else []
    prefixes.append("")
    # Все допустимые форматы всех поколений; compression (если передан)
    # только определяет ПЕРВЫЙ проверяемый кандидат.
    extensions = []
    if compression:
        try:
            extensions.append(compression_ext(compression))
        except Exception:  # noqa: BLE001 — неизвестное значение игнорируется
            pass
    for ext in ("zst", "gz"):
        if ext not in extensions:
            extensions.append(ext)

    # 1) Точные имена из метаданных сетевой шары
    try:
        index = network_metadata_index(net_dir)
    except OSError as exc:
        log.warning(
            "retention: сетевая шара недоступна (%s); снапшот сохранен", exc,
        )
        return False
    for archive_name, meta in index.get(str(snapshot_name), []):
        candidate = net_dir / archive_name
        try:
            if not candidate.is_file():
                continue
        except OSError as exc:
            log.warning(
                "retention: архив %s недоступен (%s); снапшот сохранен",
                candidate, exc,
            )
            continue
        if _verified_network_candidate(
                candidate, expected_reference=(meta or {}).get("sha256")):
            return True

    # 2) Кандидаты по шаблону имени (все расширения, оба поколения)
    for ext in extensions:
        candidates = [
            net_dir / "{}{}.{}.gpg".format(prefix, snapshot_name, ext)
            for prefix in prefixes
        ] + [
            net_dir / "{}{}.btrfs.{}.gpg".format(prefix, snapshot_name, ext)
            for prefix in prefixes
        ]
        for candidate in candidates:
            try:
                if not candidate.is_file():
                    continue
            except OSError as exc:
                log.warning(
                    "retention: архив %s недоступен (%s); снапшот сохранен",
                    candidate, exc,
                )
                continue
            if _verified_network_candidate(candidate):
                return True
    return False


def _network_expected_sha(candidate):
    """
    Эталонный SHA-256 сетевого архива: JSON-метаданные,
    затем файл .sha256 (предыдущие версии). None — эталона нет.
    """
    meta_path = candidate.with_name(candidate.name + ".json")
    try:
        if meta_path.is_file():
            raw = json_loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                value = str(raw.get("sha256") or "").strip().lower()
                if len(value) == 64 and \
                        all(c in "0123456789abcdef" for c in value):
                    return value
    except (OSError, ValueError):
        pass
    checksum = candidate.with_name(candidate.name + ".sha256")
    try:
        if checksum.is_file():
            return parse_checksum_file(checksum)
    except Exception:  # noqa: BLE001
        return None
    return None


def network_archive_exists(net_dir, archive_name) -> bool:
    """True, если указанный архив уже есть на сетевой шаре."""
    return (Path(net_dir) / archive_name).is_file()


def cleanup_network_tmp_files(net_dir, older_than_seconds=0):
    """
    Удаляет осиротевшие временные файлы *.tmp НА СЕТЕВОЙ ШАРЕ, оставленные
    прерванной отправкой. Осторожно: удаляются только файлы строго
    нашего шаблона (*.gpg.tmp / *.sha256.tmp); обычные архивы не трогаются.
    Возвращает количество удаленных файлов.
    """
    removed = 0
    net_dir = Path(net_dir)
    if not net_dir.is_dir():
        return removed
    import time
    now = time.time()
    for entry in net_dir.iterdir():
        try:
            if not entry.is_file():
                continue
            if not (entry.name.endswith(".gpg.tmp") or
                    entry.name.endswith(".sha256.tmp") or
                    entry.name.endswith(".json.tmp")):
                continue
            if older_than_seconds and \
                    now - entry.stat().st_mtime < older_than_seconds:
                continue
            entry.unlink()
            removed += 1
            log.info("Удален временный файл на сетевой шаре: %s", entry.name)
        except OSError as exc:
            log.warning("Не удалось удалить временный файл %s: %s", entry, exc)
    return removed
