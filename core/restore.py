"""
Восстановление: из зашифрованного архива (локального или сетевого)
и из локального read-only снапшота.

Безопасность:
  - автоматическая перезапись существующих subvolume запрещена;
  - восстановление выполняется только в указанный пользователем каталог
    на файловой системе Btrfs;
  - автоматическая замена работающего корневого subvolume не выполняется;
  - исходный read-only снапшот при восстановлении не изменяется;
  - наличие свободного места проверяется ДО восстановления;
  - расшифрование ПРОВЕРЯЕТСЯ ДО запуска btrfs receive: при неверном
    пароле или поврежденном архиве receive не выполняется вовсе;
  - при ошибке receive частичный результат НЕ удаляется автоматически;
    предусмотрена отдельная подтверждаемая операция очистки
    (cleanup_partial_subvolume) со строгими проверками.

ТЗ 1.4.8.2 §3.1 (D-1): очистка частичного subvolume выполняется
НЕПОСРЕДСТВЕННЫМ `btrfs subvolume delete` БЕЗ предварительного
`btrfs property set ro false` — для subvolume с received_uuid смена ro
требует force (btrfs-subvolume(8), SUBVOLUME FLAGS), а read-only
subvolume удаляется напрямую. Частичным считается только объект,
созданный ТЕКУЩЕЙ операцией: состояние целевого каталога фиксируется
до запуска конвейера (preexisting) и передается в очистку вместе с
ошибкой (RestoreError.partial_info).

ТЗ 1.4.8.2 §3.3 (D-4): stdout стадии `btrfs receive` перенаправляется
во временный файл (как stderr) и НЕ попадает в терминал — отображение
TUI (curses) не нарушается; диагностика сохраняется в сообщении об
ошибке, коды возврата всех стадий проверяются как раньше.
"""

import shutil
from pathlib import Path

from . import RestoreError, VerificationError
from .compression import decompressor_cmd, detect_compression, snapshot_name_from_archive
from .encryption import gpg_decrypt_cmd, make_passphrase_fd
from .logging_setup import get_logger
from .process import (
    human_size, new_stderr_capture, popen_pipeline, run_command,
    stderr_tail, terminate_processes, wait_pipeline,
)
from .snapshots import (
    is_btrfs_path, is_subvolume, subvolume_ro_property,
)
from .verification import sha256_file, test_decrypted_stream

log = get_logger("restore")


def validate_restore_target(target_dir):
    """Проверяет целевой каталог: существует, является каталогом, на Btrfs."""
    target = Path(target_dir)
    if not target.exists():
        raise RestoreError(
            "Целевой каталог не существует: {}. Создайте его на файловой "
            "системе Btrfs.".format(target)
        )
    if not target.is_dir():
        raise RestoreError(
            "Целевой путь не является каталогом: {}".format(target)
        )
    if not is_btrfs_path(str(target)):
        raise RestoreError(
            "Целевой каталог находится не на файловой системе Btrfs: {} "
            "(определена ФС, отличная от btrfs)".format(target)
        )


def free_space(path):
    """Свободное место в байтах или None."""
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return None


def check_free_space(target_dir, archive_size, *, required_factor=1.0,
                     warn_factor=3.0):
    """
    Проверка наличия свободного места.

    Поток btrfs send распаковывается, поэтому фактические потребности
    могут превышать размер архива. Правила:
      - свободного места меньше размера архива — RestoreError;
      - меньше warn_factor кратного размера — предупреждение (возврат
        сообщения вызывающему коду для подтверждения пользователем).
    Возвращает строку предупреждения или None.
    """
    free = free_space(target_dir)
    if free is None:
        return ("Свободное место в {} определить не удалось; "
                "продолжайте, если уверены в емкости хранилища.".format(target_dir))
    if free < archive_size * required_factor:
        raise RestoreError(
            "Недостаточно свободного места в {}: свободно {}, требуется "
            "не менее {} (размер архива).".format(
                target_dir, human_size(free), human_size(archive_size))
        )
    if free < archive_size * warn_factor:
        return ("Свободно {} — распакованный поток может занимать больше "
                "размера архива ({}).".format(human_size(free),
                                              human_size(archive_size)))
    return None


def predicted_subvolume_path(archive_path, target_dir):
    """Путь subvolume, который создаст `btrfs receive` в target_dir."""
    snapshot_name = snapshot_name_from_archive(Path(archive_path).name)
    return Path(target_dir) / snapshot_name


def check_name_conflict(archive_path, target_dir):
    """Проверяет отсутствие конфликта имен до запуска восстановления."""
    dest = predicted_subvolume_path(archive_path, target_dir)
    if dest.exists():
        raise RestoreError(
            "Отказ: subvolume '{}' уже существует в целевом каталоге. "
            "Автоматическая перезапись запрещена.".format(dest)
        )
    return dest


def _subvolume_names_in_dir(directory):
    """
    Имена Btrfs subvolume, находящихся НЕПОСРЕДСТВЕННО в каталоге (D-1).

    Возвращает множество имен или None, если перечень определить не
    удалось (недоступен каталог): неизвестное состояние НЕ подменяется
    пустым множеством — очистка частичного результата в этом случае
    откажет (fail-closed).
    """
    names = set()
    try:
        entries = sorted(Path(directory).iterdir())
    except OSError:
        return None
    for entry in entries:
        try:
            if entry.is_dir() and is_subvolume(entry):
                names.add(entry.name)
        except OSError:
            continue
    return names


def verify_archive_reference(archive_path):
    """
    Проверка эталонной суммы sidecar-файла перед восстановлением.

    Правила (fail-closed по содержимому, но без блокировки доступа к
    данным пользователя):
      - JSON-метаданные содержат sha256: эталон сверяется с ФАКТИЧЕСКОЙ
        суммой архива; несовпадение — RestoreError (архив поврежден,
        восстановление отменено);
      - sidecar отсутствует, поврежден или не содержит sha256 —
        возвращается предупреждение: целостность подтверждается полным
        расшифрованием (gpg проверяет аутентичность тегом) и тестом
        сжатого потока; восстановление НЕ блокируется — отсутствие
        вспомогательного файла не должно лишать доступа к данным;
      - legacy .sha256 (предыдущие версии, ТЗ 19) тоже принимается.

    Возвращает строку предупреждения или None.
    """
    from . import metadata as metadata_module
    archive_path = Path(archive_path)
    meta = metadata_module.read_metadata(archive_path)
    if meta is None:
        meta = metadata_module.infer_archive_info(archive_path)
    if meta is None:
        return ("Эталонная сумма недоступна (sidecar отсутствует); "
                "целостность подтверждается расшифрованием и тестом "
                "сжатого потока")
    expected = (meta or {}).get("sha256")
    if not expected:
        return ("В метаданных нет эталонной суммы; целостность "
                "подтверждается расшифрованием и тестом сжатого потока")
    try:
        actual = sha256_file(archive_path)
    except OSError as exc:
        raise RestoreError(
            "Архив {} не читается при сверке эталонной суммы: {}".format(
                archive_path.name, exc)
        )
    if str(actual).lower() != str(expected).lower():
        raise RestoreError(
            "SHA-256 архива {} НЕ совпал с эталоном из метаданных "
            "({} != {}). Архив поврежден; восстановление отменено, "
            "исходный архив не изменен.".format(
                archive_path.name, expected, actual)
        )
    log.info("Эталонная сумма %s подтверждена фактическим SHA-256",
             archive_path.name)
    return None


def receive_archive(archive_path, target_dir, *, gpg_binary, password,
                    compression=None, receive_cmd_builder=None, precheck=True,
                    abort_check=None):
    """
    Полное восстановление из зашифрованного архива.

    Конвейер: gpg --decrypt | gzip/zstd -d | btrfs receive <target_dir>.

    compression=None — алгоритм сжатия определяется по расширению архива
    (архивы разных периодов могут сжиматься по-разному; COMPRESSION из
    config.py задает алгоритм только для новых архивов, ТЗ 6.4).

    precheck=True (по умолчанию): ДО запуска конвейера выполняется полная
    проверка расшифрования и сжатого потока; при неверном пароле или
    поврежденном контейнере btrfs receive НЕ выполняется вовсе.

    Возвращает путь созданного subvolume. Все коды возврата контролируются.
    При ошибке btrfs receive частичный результат НЕ удаляется автоматически:
    в сообщении об ошибке приводятся путь и инструкция по проверке.
    """
    archive_path = Path(archive_path)
    target = Path(target_dir)
    if not archive_path.is_file():
        raise RestoreError("Архив не найден: {}".format(archive_path))
    if compression is None:
        compression = detect_compression(archive_path.name)
        log.info("Алгоритм сжатия определен по расширению: %s (%s)",
                 compression, archive_path.name)
    # Эталонная сумма sidecar: несовпадение — восстановление
    # отменено ДО receive; отсутствие/повреждение sidecar — предупреждение.
    reference_warning = verify_archive_reference(archive_path)
    if reference_warning:
        log.warning("%s (%s)", reference_warning, archive_path.name)
    validate_restore_target(target)
    dest = check_name_conflict(archive_path, target)

    # D-1: фиксируем subvolume целевого каталога ДО
    # запуска конвейера. Частичным результатом текущей операции считается
    # только объект, которого в этом перечне нет; перечень передается
    # в очистку вместе с ошибкой receive (RestoreError.partial_info).
    preexisting = _subvolume_names_in_dir(target)
    if preexisting is None:
        log.warning(
            "Не удалось зафиксировать состояние целевого каталога до "
            "операции: %s; очистка частичного результата будет "
            "недоступна (fail-closed)", target,
        )

    archive_size = archive_path.stat().st_size
    warning = check_free_space(target, archive_size)
    if warning:
        log.warning("Место: %s", warning)

    if precheck:
        # Требование безопасности: при любой ошибке
        # расшифровки НЕ выполнять btrfs receive.
        log.info(
            "Предварительная проверка расшифрования и сжатого потока: %s",
            archive_path.name,
        )
        try:
            test_decrypted_stream(
                archive_path,
                gpg_binary=gpg_binary,
                password=password,
                compression=compression,
                abort_check=abort_check,
            )
        except VerificationError as exc:
            raise RestoreError(
                "Восстановление ОТМЕНЕНО до выполнения btrfs receive: {}. "
                "Исходный архив и локальные снапшоты не изменены.".format(exc)
            ) from exc

    if receive_cmd_builder is None:
        receive_cmd = ["btrfs", "receive", str(target)]
    else:  # точка расширения для тестов
        receive_cmd = receive_cmd_builder(target)

    import os

    passphrase_fd = make_passphrase_fd(password)
    passphrase_fd_closed = False
    procs = []
    captures = []
    try:
        err_gpg = new_stderr_capture()
        err_dec = new_stderr_capture()
        err_recv = new_stderr_capture()
        # D-4: stdout стадии btrfs receive наследуется
        # от родительского процесса и выводится прямо в терминал, нарушая
        # отображение TUI (curses). Перенаправляем его во временный файл
        # (как stderr): PIPE не используется, поэтому зависание из-за
        # непрочитанного канала исключено; диагностика сохраняется и
        # попадает в сообщение об ошибке, коды возврата не затрагиваются.
        out_recv = new_stderr_capture()
        captures = [err_gpg, err_dec, err_recv, out_recv]

        with open(str(archive_path), "rb") as archive_stream:
            procs.append(
                popen_pipeline(
                    gpg_decrypt_cmd(gpg_binary, passphrase_fd),
                    stdin=archive_stream,
                    stdout=__import__("subprocess").PIPE,
                    stderr=err_gpg,
                    pass_fds=(passphrase_fd,),
                )
            )
        # Пароль уже записан в буфер pipe; gpg прочитает его до EOF.
        os.close(passphrase_fd)
        passphrase_fd_closed = True

        try:
            procs.append(
                popen_pipeline(
                    decompressor_cmd(compression),
                    stdin=procs[0].stdout,
                    stdout=__import__("subprocess").PIPE,
                    stderr=err_dec,
                )
            )
        finally:
            procs[0].stdout.close()

        try:
            procs.append(
                popen_pipeline(
                    receive_cmd,
                    stdin=procs[1].stdout,
                    stdout=out_recv,
                    stderr=err_recv,
                )
            )
        finally:
            procs[1].stdout.close()

        # Ожидание с тайм-аутом; ошибка одной стадии завершает остальные
        # (например, отказ receive не оставляет висящие gpg/распаковщик).
        return_codes, killed = wait_pipeline(
            procs,
            error_class=RestoreError,
            what="Конвейер восстановления ({})".format(archive_path.name),
            abort_check=abort_check,
        )
        rc_gpg, rc_dec, rc_recv = return_codes

        # Первопричина: btrfs receive, завершившийся САМОСТОЯТЕЛЬНО с
        # ошибкой. Вышестоящие стадии, умершие от EPIPE после отказа
        # receive, — следствия, а не причины (иначе ошибка receive
        # маскировалась бы как «ошибка расшифрования»).
        if rc_recv != 0 and 2 not in killed:
            tail = stderr_tail(captures[2])
            out_tail = stderr_tail(captures[3])
            raise _partial_receive_error(
                target, dest, rc_recv, tail, out_tail, preexisting)
        # Самая ранняя стадия, завершившаяся самостоятельно с ошибкой.
        self_failed = [i for i, rc in enumerate(return_codes)
                       if rc != 0 and i not in killed]
        if self_failed:
            first = self_failed[0]
            if first == 0:
                tail = stderr_tail(captures[0])
                error = RestoreError(
                    "Расшифрование не удалось (gpg rc={}): неверный пароль "
                    "или поврежденный архив.{}".format(
                        rc_gpg, (" " + tail) if tail else ""
                    )
                )
                error.exit_code = 5
                raise error
            tail = stderr_tail(captures[1])
            error = RestoreError(
                "Распаковка не удалась ({} rc={}).{}".format(
                    compression, rc_dec,
                    (" " + tail) if tail else "",
                )
            )
            error.exit_code = 6
            raise error
        if rc_recv != 0:
            # receive был завершен принудительно (таймаут конвейера или
            # прерывание операции) — трактуем как прерванное восстановление.
            tail = stderr_tail(captures[2])
            out_tail = stderr_tail(captures[3])
            raise _partial_receive_error(
                target, dest, rc_recv, tail, out_tail, preexisting)

        if not is_subvolume(dest):
            raise RestoreError(
                "btrfs receive завершился, но subvolume не обнаружен: "
                "{}".format(dest)
            )
        log.info("Восстановление из архива завершено: %s", dest)
        return dest
    except FileNotFoundError as exc:
        raise RestoreError(
            "Не удалось запустить процесс конвейера восстановления: "
            "{}".format(exc)
        ) from exc
    except Exception:
        terminate_processes(procs)
        raise
    finally:
        if not passphrase_fd_closed:
            try:
                os.close(passphrase_fd)
            except OSError:
                pass
        for capture in captures:
            try:
                capture.close()
            except OSError:
                pass


def _partial_receive_message(target, dest, return_code, tail, out_tail=""):
    """
    Сообщение об ошибке btrfs receive: целевой
    путь, наличие частичного subvolume и точная инструкция по проверке и
    безопасному удалению. Автоматическое удаление частичного результата
    не выполняется.

    D-1: подсказка рекомендует НЕПОСРЕДСТВЕННОЕ удаление
    `btrfs subvolume delete` БЕЗ предварительного изменения свойства ro:
    для subvolume с received_uuid смена ro требует force
    (btrfs-subvolume(8), SUBVOLUME FLAGS), а read-only subvolume
    удаляется напрямую.
    """
    parts = [
        "btrfs receive завершился с ошибкой (rc={}).".format(return_code),
        "Целевой каталог: {}.".format(target),
        "Ожидаемый subvolume: {}.".format(dest),
    ]
    if dest.exists():
        kind = "subvolume" if is_subvolume(dest) else "обычный каталог"
        parts.append(
            "ВНИМАНИЕ: в целевом каталоге обнаружен частичный результат — "
            "{} {}. Автоматическое удаление НЕ выполняется.".format(
                kind, dest)
        )
        parts.append(
            "Проверка содержимого: btrfs subvolume show {d}; ls -la {d}. "
            "Безопасное удаление (если восстановленные данные не нужны): "
            "btrfs subvolume delete {d} — изменение свойства ro НЕ "
            "требуется (read-only subvolume удаляется напрямую; смена ro "
            "для subvolume с received_uuid требует force); затем при "
            "необходимости rmdir {t}.".format(d=dest, t=target)
        )
    else:
        parts.append(
            "Частичный subvolume '{}' в целевом каталоге не обнаружен; "
            "удалять ничего не требуется.".format(dest.name)
        )
        # Дополнительная диагностика: ищем в целевом каталоге другие
        # недавно появившиеся subvolume с именами приложения (частичные
        # результаты под другим именем не приписываем пользователю).
        try:
            from .snapshots import SNAPSHOT_NAME_RE
            for entry in sorted(Path(target).iterdir()):
                if entry.name == dest.name or not entry.is_dir():
                    continue
                if SNAPSHOT_NAME_RE.match(entry.name) and is_subvolume(entry):
                    parts.append(
                        "ВНИМАНИЕ: обнаружен частичный subvolume {} — "
                        "автоматическое удаление НЕ выполняется; проверьте "
                        "и удалите вручную при необходимости.".format(entry)
                    )
                    break
        except OSError:
            pass
    if tail and ("no space left" in tail.lower() or "enospc" in tail.lower()):
        parts.append(
            "Причина: на целевой файловой системе закончилось свободное "
            "место. Освободите место (или увеличьте ФС) и повторите "
            "восстановление; сначала удалите частичный результат."
        )
    if tail:
        parts.append("Подробности: " + tail)
    if out_tail:
        # D-4: stdout receive перенаправлен в файл и не попадает в
        # терминал — включаем его в диагностику ошибки.
        parts.append("Вывод receive (stdout): " + out_tail)
    return " ".join(parts)


def _partial_receive_error(target, dest, return_code, tail, out_tail,
                           preexisting):
    """
    Возбуждает RestoreError для неудачного btrfs receive с приобщением
    partial_info (D-1): целевой каталог, ожидаемый subvolume и перечень
    subvolume, существовавших в целевом каталоге ДО операции. По этому
    перечню очистка (cleanup_partial_subvolume) отличает частичный
    результат ТЕКУЩЕЙ операции от существовавших ранее объектов.
    """
    error = RestoreError(_partial_receive_message(
        target, dest, return_code, tail, out_tail))
    error.partial_info = {
        "dest": str(dest),
        "target": str(target),
        "preexisting": sorted(preexisting) if preexisting is not None else None,
    }
    return error


def cleanup_partial_subvolume(dest_path, *, target_dir, expected_name=None,
                              preexisting=None):
    """
    Безопасное удаление частичного результата НЕУДАВШЕГОСЯ восстановления
    (отдельная операция, выполняется только после явного подтверждения
    пользователя; ТЗ 1.4.8.2 §3.1, дефект D-1).

    Обязательные проверки перед удалением (fail-closed — при любой
    неоднозначности отказ с диагностикой):
      1) объект существует;
      2) имя совпадает с ожидаемым (если expected_name задан) — защита
         от случайного указания чужого пути;
      3) путь — ПРЯМОЙ потомок разрешенного целевого каталога
         target_dir (по реальным путям): объекты вне целевого каталога
         и вложенные глубже первого уровня не удаляются;
      4) объект является Btrfs subvolume по авторитетной проверке
         (btrfs subvolume show) — обычные каталоги НЕ удаляются никогда;
      5) имя объекта НЕ входит в перечень subvolume, существовавших в
         целевом каталоге ДО начала операции (preexisting; фиксируется
         в receive_archive до запуска конвейера и передается через
         RestoreError.partial_info): удаляется только результат
         ТЕКУЩЕЙ операции, существовавший ранее объект защищен;
         preexisting=None (состояние каталога до операции неизвестно) —
         отказ: безопасность удаления не может быть подтверждена.

    D-1: предварительное изменение свойства ro НЕ выполняется. Для
    subvolume, полученного через btrfs receive (с received_uuid),
    `btrfs property set <path> ro false` требует force
    (btrfs-subvolume(8), SUBVOLUME FLAGS) и может быть запрещен;
    `btrfs subvolume delete` работает с read-only subvolume
    непосредственно. Удаление выполняется ТОЛЬКО командой
    `btrfs subvolume delete` (не рекурсивным удалением каталога).

    После выполнения проверяется фактическое исчезновение объекта:
    ложное сообщение об успешной очистке исключено.
    """
    dest = Path(dest_path)
    if not dest.exists():
        raise RestoreError(
            "Отказ: объект не существует: {}".format(dest)
        )
    if expected_name is not None and dest.name != expected_name:
        raise RestoreError(
            "Отказ: имя объекта '{}' не совпадает с ожидаемым '{}'; "
            "удаление запрещено.".format(dest.name, expected_name)
        )
    # Проверка 3: прямой потомок разрешенного целевого каталога
    try:
        dest_real = dest.resolve()
        target_real = Path(target_dir).resolve()
    except (OSError, ValueError) as exc:
        raise RestoreError(
            "Отказ: не удалось определить реальный путь '{}' ({}); "
            "удаление запрещено".format(dest, exc)
        )
    if dest_real.parent != target_real:
        raise RestoreError(
            "Отказ: '{}' не является прямым потомком разрешенного "
            "целевого каталога '{}'; удаление объекта вне целевого "
            "каталога запрещено.".format(dest, target_dir)
        )
    # Проверка 4: только настоящий Btrfs subvolume
    if not is_subvolume(dest):
        raise RestoreError(
            "Отказ: '{}' не является Btrfs subvolume. Удаление обычного "
            "каталога через btrfs subvolume delete запрещено; проверьте "
            "объект вручную.".format(dest)
        )
    # Проверка 5: объект создан ТЕКУЩЕЙ операцией (не существовал до нее)
    if preexisting is None:
        raise RestoreError(
            "Отказ: состояние целевого каталога до начала операции не "
            "зафиксировано (preexisting неизвестно); безопасность "
            "удаления не может быть подтверждена. Проверьте объект "
            "вручную (btrfs subvolume show {d}) и при необходимости "
            "удалите его самостоятельно: btrfs subvolume delete {d} "
            "(изменение свойства ro не требуется).".format(d=dest)
        )
    if dest.name in preexisting:
        raise RestoreError(
            "Отказ: '{}' существовал в целевом каталоге ДО начала "
            "операции восстановления; удаление существовавшего объекта "
            "запрещено (защита пользовательских данных, ТЗ 1.4.8.2 "
            "§3.1).".format(dest)
        )
    run_command(
        ["btrfs", "subvolume", "delete", str(dest)],
        error_class=RestoreError,
        what="Удаление частичного subvolume {}".format(dest),
        timeout=600,
    )
    if dest.exists():
        raise RestoreError(
            "btrfs subvolume delete завершился без ошибки, но объект всё "
            "ещё существует: {}. Проверьте состояние вручную "
            "(btrfs subvolume show); удаление не может считаться "
            "выполненным.".format(dest)
        )
    log.info("Частичный результат восстановления удален: %s", dest)
    return True


def restore_snapshot_writable(snapshot_path, target_dir):
    """
    Восстановление из локального read-only снапшота: создает
    новый записываемый снапшот на его основании. Исходный снапшот
    не изменяется и не удаляется.
    """
    snapshot_path = Path(snapshot_path)
    target = Path(target_dir)
    validate_restore_target(target)
    if not is_subvolume(snapshot_path):
        raise RestoreError(
            "Источник не является Btrfs subvolume: {}".format(snapshot_path)
        )
    # Fail-closed: восстановление из локального
    # снапшота разрешено ТОЛЬКО при однозначно подтвержденном read-only
    # состоянии, проверенном непосредственно перед операцией. ro=False —
    # источник доступен на запись (ожидается read-only); ro=None —
    # состояние определить не удалось. Оба случая запрещены.
    ro = subvolume_ro_property(snapshot_path)
    if ro is False:
        raise RestoreError(
            "Источник уже доступен на запись (ro=false); ожидается "
            "read-only снапшот: {}. Состояние проверено непосредственно "
            "перед операцией (btrfs property get).".format(snapshot_path)
        )
    if ro is None:
        raise RestoreError(
            "Не удалось подтвердить read-only состояние источника "
            "(ro определить не удалось; btrfs property get недоступен "
            "или завершился ошибкой): {}. Операция запрещена (fail-closed: "
            "неизвестное состояние трактуется как запрет).".format(snapshot_path)
        )

    new_name = snapshot_path.name + "_restored"
    dest = target / new_name
    if dest.exists():
        raise RestoreError(
            "Отказ: объект '{}' уже существует. Автоматическая перезапись "
            "запрещена.".format(dest)
        )

    run_command(
        ["btrfs", "subvolume", "snapshot", str(snapshot_path), str(dest)],
        error_class=RestoreError,
        what="Создание записываемого снапшота {}".format(new_name),
    )
    if not is_subvolume(dest):
        raise RestoreError(
            "Снапшот создан, но subvolume не обнаружен: {}".format(dest)
        )
    log.info(
        "Создан записываемый снапшот %s (на основании %s; исходный снапшот "
        "не изменен)", dest, snapshot_path.name,
    )
    return dest
