"""
Формирование потока `btrfs send`, сжатие и сборка зашифрованного архива.

Конвейер:  btrfs send SNAPSHOT | gzip/zstd | gpg --symmetric > архив

Особенности:
  - промежуточный незашифрованный архив на диске не создается;
  - данные передаются потоково через subprocess.Popen (без shell=True);
  - проверяются коды возврата ВСЕХ процессов конвейера: архив считается
    успешным только если и btrfs send, и компрессор, и gpg завершились
    с кодом 0;
  - готовый архив записывается во временный файл *.part и переименовывается
    атомарно (os.replace) — незавершенные данные никогда не выглядят
    как готовый архив;
  - ошибка btrfs send не маскируется успешным завершением компрессора/gpg.
"""

import os
import subprocess
from pathlib import Path

from . import ArchiveError
from .compression import compressor_cmd
from .encryption import gpg_encrypt_cmd, make_passphrase_fd
from .logging_setup import get_logger
from .process import (
    new_stderr_capture, popen_pipeline, stderr_tail, terminate_processes,
    wait_pipeline,
)

log = get_logger("archive")


def _start_pipeline(send_cmd, compression, level, gpg_binary, passphrase_fd,
                    out_fd, abort_check=None):
    """
    Запускает конвейер из трех процессов.

    Возвращает (procs, stderr_captures). Родительские концы pipe закрываются
    сразу после передачи дочерним процессам.
    """
    err_send = new_stderr_capture()
    err_comp = new_stderr_capture()
    err_gpg = new_stderr_capture()
    procs = []
    try:
        procs.append(
            popen_pipeline(send_cmd, stdout=subprocess.PIPE, stderr=err_send)
        )
        try:
            procs.append(
                popen_pipeline(
                    compressor_cmd(compression, level),
                    stdin=procs[0].stdout,
                    stdout=subprocess.PIPE,
                    stderr=err_comp,
                )
            )
        finally:
            procs[0].stdout.close()
        try:
            procs.append(
                popen_pipeline(
                    gpg_encrypt_cmd(gpg_binary, passphrase_fd),
                    stdin=procs[1].stdout,
                    stdout=out_fd,
                    stderr=err_gpg,
                    pass_fds=(passphrase_fd,),
                )
            )
        finally:
            procs[1].stdout.close()
        return procs, [err_send, err_comp, err_gpg]
    except FileNotFoundError as exc:
        terminate_processes(procs)
        raise ArchiveError(
            "Не удалось запустить процесс конвейера: {}. Проверьте наличие "
            "утилит (btrfs, {}, {}).".format(
                exc, compression, Path(gpg_binary).name
            )
        ) from exc
    except Exception:
        terminate_processes(procs)
        raise


def create_archive(snapshot_path, out_path, *, compression, level,
                   gpg_binary, password, send_cmd_builder=None,
                   abort_check=None):
    """
    Создает полный зашифрованный архив снапшота.

    out_path — окончательный путь архива; сначала данные пишутся в
    <out_path>.part, после успешного завершения всех процессов конвейера
    файл переименовывается атомарно. Возвращает размер архива в байтах.

    Перезапись существующего архива запрещена. Незавершенный файл
    прошлой аварийной остановки (*.part) удаляется безопасно: это всегда
    частичные данные строго нашего шаблона.
    """
    out_path = Path(out_path)
    part_path = out_path.with_name(out_path.name + ".part")
    label = out_path.name

    if out_path.exists():
        raise ArchiveError(
            "Архив с таким именем уже существует, перезапись запрещена: "
            "{}".format(out_path)
        )

    if part_path.exists():
        log.warning("Удаляется незавершенный файл прошлой операции: %s", part_path)
        try:
            part_path.unlink()
        except OSError as exc:
            raise ArchiveError(
                "Не удалось удалить незавершенный файл {}: {}".format(
                    part_path, exc
                )
            ) from exc

    if send_cmd_builder is None:
        send_cmd = ["btrfs", "send", str(snapshot_path)]
    else:  # точка расширения для тестов
        send_cmd = send_cmd_builder(snapshot_path)

    out_fd = None
    passphrase_fd = None
    procs = []
    stderr_captures = []
    try:
        try:
            out_fd = os.open(
                str(part_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except OSError as exc:
            raise ArchiveError(
                "Не удалось создать файл архива {}: {}".format(part_path, exc)
            ) from exc

        passphrase_fd = make_passphrase_fd(password)
        procs, stderr_captures = _start_pipeline(
            send_cmd, compression, level, gpg_binary, passphrase_fd, out_fd,
            abort_check=abort_check,
        )
        os.close(passphrase_fd)
        passphrase_fd = None  # gpg получит EOF после дочитывания пароля

        # Ожидание с тайм-аутом; отказ одной стадии завершает остальные.
        return_codes, _killed = wait_pipeline(
            procs,
            error_class=ArchiveError,
            what="Конвейер архивирования ({})".format(label),
            abort_check=abort_check,
        )

        # Первопричина — только процессы, завершившиеся САМОСТОЯТЕЛЬНО с
        # ошибкой (принудительно завершенные нами — следствия, а не причины).
        failed = [
            (proc.args[0], rc)
            for i, (proc, rc) in enumerate(zip(procs, return_codes))
            if rc != 0 and i not in _killed
        ]
        if not failed and any(rc != 0 for rc in return_codes):
            failed = [(proc.args[0], rc)
                      for proc, rc in zip(procs, return_codes) if rc != 0]
        if failed:
            details = []
            stage_names = ["btrfs send", "компрессор " + compression, "gpg"]
            for stage, capture in zip(stage_names, stderr_captures):
                tail = stderr_tail(capture)
                if tail:
                    details.append("{}: {}".format(stage, tail))
            raise ArchiveError(
                "Конвейер архивирования завершен с ошибкой ({}); "
                "архив '{}' не создан.{}".format(
                    ", ".join("{}: rc={}".format(n, rc) for n, rc in failed),
                    label,
                    (" Подробности: " + " | ".join(details)) if details else "",
                )
            )

        size = os.fstat(out_fd).st_size
        if size == 0:
            raise ArchiveError(
                "Архив '{}' получен пустым — операция не может считаться "
                "успешной".format(label)
            )
        os.fsync(out_fd)
        os.close(out_fd)
        out_fd = None

        os.replace(str(part_path), str(out_path))  # атомарное переименование
        log.info(
            "Архив создан: %s (%s байт; снапшот: %s; сжатие: %s -%d)",
            out_path.name, size, Path(snapshot_path).name, compression, level,
        )
        return size
    except Exception:
        terminate_processes(procs)
        raise
    finally:
        if passphrase_fd is not None:
            try:
                os.close(passphrase_fd)
            except OSError:
                pass
        if out_fd is not None:
            try:
                os.close(out_fd)
            except OSError:
                pass
        for capture in stderr_captures:
            try:
                capture.close()
            except OSError:
                pass
        if part_path.exists():
            try:
                part_path.unlink()
                log.info("Удален незавершенный файл: %s", part_path)
            except OSError:
                log.warning("Не удалось удалить незавершенный файл: %s", part_path)
