"""
Запуск внешних команд и управление конвейерами subprocess.

Безопасность конвейеров:
  - каждый процесс стартует в СОБСТВЕННОЙ процессной группе
    (start_new_session): SIGINT с терминала не достается детям напрямую,
    а родитель может гарантированно завершить все дерево дочернего процесса;
  - ожидание завершения конвейера всегда ограничено тайм-аутом
    (core.config PROCESS_TIMEOUT / переменная окружения BBM_PIPELINE_TIMEOUT):
    бесконечное ожидание исключено;
  - если один из процессов конвейера завершился с ошибкой, остальные
    принудительно завершаются (SIGTERM, затем SIGKILL);
  - поддержан внешний флаг прерывания (abort_check): TUI останавливает
    фоновые операции из главного потока;
  - реестр активных конвейеров позволяет завершить все дочерние процессы
    при аварийной остановке интерфейса.
"""

import logging
import os
import signal
import subprocess
import tempfile
import threading
import time

from . import BtrfsBackupError, PROCESS_TIMEOUT_FALLBACK

# Тайм-аут ожидания конвейера по умолчанию (секунды). Переопределяется
# config.PROCESS_TIMEOUT через set_pipeline_timeout().
PIPELINE_TIMEOUT = int(os.environ.get("BBM_PIPELINE_TIMEOUT", str(PROCESS_TIMEOUT_FALLBACK)))

# Реестр активных процессов для аварийного завершения (TUI, сигналы)
_active_lock = threading.Lock()
_active_procs = set()


def set_pipeline_timeout(seconds):
    """Устанавливает таймаут конвейеров из конфигурации."""
    global PIPELINE_TIMEOUT
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return
    if value >= 1:
        PIPELINE_TIMEOUT = value


def register_process(proc):
    with _active_lock:
        _active_procs.add(proc)


def unregister_process(proc):
    with _active_lock:
        _active_procs.discard(proc)


def terminate_all_active():
    """Аварийное завершение всех активных дочерних процессов (TUI/signals)."""
    with _active_lock:
        procs = list(_active_procs)
    for proc in procs:
        try:
            terminate_processes([proc])
        except Exception:
            pass


def run_command(cmd, *, error_class=BtrfsBackupError, what=None, timeout=600,
                check=True):
    """
    Запускает внешнюю команду без shell, с тайм-аутом.

    Возвращает subprocess.CompletedProcess; при ненулевом коде возврата
    возбуждает error_class с хвостом stderr (если check=True).
    Команды никогда не содержат секретов (пароль передается через fd).
    """
    log = logging.getLogger("btrfs_backup.cmd")
    log.debug("Запуск команды: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise error_class(
            "{}: утилита не найдена ({})".format(what or cmd[0], exc)
        ) from exc
    except PermissionError as exc:
        raise error_class(
            "{}: нет прав на выполнение утилиты ({})".format(
                what or cmd[0], exc)
        ) from exc
    except OSError as exc:
        # ТЗ 1.4.8 §5: остальные ошибки запуска (EACCES на промежуточном
        # каталоге PATH, ENOEXEC и т.п.) — понятное сообщение вместо
        # необработанного исключения.
        raise error_class(
            "{}: команда не выполнена ({})".format(what or cmd[0], exc)
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise error_class(
            "{}: превышен таймаут выполнения ({} с)".format(what or cmd[0], timeout)
        ) from exc
    if check and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()
        if tail:
            tail = tail.splitlines()[-5:]
            tail = "; ".join(line.strip() for line in tail)
        else:
            tail = ""
        raise error_class(
            "{}: код возврата {}{}".format(
                what or cmd[0], proc.returncode, ("; " + tail) if tail else ""
            )
        )
    return proc


def popen_pipeline(cmd, **kwargs):
    """
    Запускает процесс конвейера в отдельной процессной группе (сессии).

    Благодаря этому SIGINT с терминала не достается детям напрямую (родитель
    сам завершает их корректно), а terminate_processes может завершить ВСЕ
    дерево дочернего процесса (включая внуков, например `sleep` внутри
    скриптов-оберток).
    """
    kwargs.setdefault("start_new_session", True)
    proc = subprocess.Popen(cmd, **kwargs)
    register_process(proc)
    return proc


def _signal_process_group(proc, sig):
    """Отправляет сигнал всей процессной группе потомка (с запасным вариантом)."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (OSError, ProcessLookupError):
        try:
            proc.send_signal(sig)
        except (OSError, ProcessLookupError):
            pass


def terminate_processes(procs):
    """Корректно завершает процессы конвейера: сначала SIGTERM, затем SIGKILL.

    Сигнал отправляется всей процессной группе каждого потомка, чтобы не
    оставлять «внуков». Завершенные процессы удаляются из реестра активных.
    """
    deadline = time.monotonic() + 5.0
    for proc in procs:
        if proc is not None and proc.poll() is None:
            _signal_process_group(proc, signal.SIGTERM)
    for proc in procs:
        if proc is None:
            continue
        try:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                _signal_process_group(proc, signal.SIGKILL)
                proc.wait(timeout=5)
            except Exception:
                pass
        except Exception:
            pass
        finally:
            unregister_process(proc)


def wait_pipeline(procs, *, timeout=None, error_class=BtrfsBackupError,
                  what="конвейер", abort_check=None):
    """
    Ожидает завершения ВСЕХ процессов конвейера с ограничением по времени.

    Правила:
      - если какой-то процесс завершился с ненулевым кодом, остальные
        принудительно завершаются (их коды возврата все равно фиксируются):
        ошибка одной стадии не может привести к бесконечному ожиданию;
      - при превышении тайм-аута все процессы завершаются и возбуждается
        error_class с понятным сообщением;
      - abort_check — вызывается в цикле ожидания; если возбуждает
        исключение (например OperationAborted из TUI), процессы принудительно
        завершаются и исключение распространяется.

    Возвращает (rcs, killed) — список кодов возврата (по одному на процесс)
    и множество индексов процессов, завершенных ПРИНУДИТЕЛЬНО этим вызовом.
    Процессы из killed не считаются первопричиной ошибки: они были убиты
    из-за отказа другой стадии.
    """
    if timeout is None:
        timeout = PIPELINE_TIMEOUT
    deadline = time.monotonic() + max(1, int(timeout))
    rcs = [None] * len(procs)
    killed = set()
    while True:
        if abort_check is not None:
            try:
                abort_check()  # возбуждает исключение при запросе прерывания
            except BaseException:
                # ТЗ 14: при прерывании все процессы конвейера корректно
                # завершаются — исключение распространяется после очистки.
                remaining = [i for i, p in enumerate(procs) if rcs[i] is None]
                terminate_processes([procs[i] for i in remaining])
                killed.update(remaining)
                for i in remaining:
                    try:
                        rcs[i] = procs[i].wait(timeout=10)
                    except Exception:
                        rcs[i] = -1
                for proc in procs:
                    unregister_process(proc)
                raise
        alive = False
        failed = False
        for i, proc in enumerate(procs):
            if rcs[i] is not None:
                continue
            rc = proc.poll()
            if rc is None:
                alive = True
            else:
                rcs[i] = rc
                if rc != 0:
                    failed = True
        if not alive:
            break
        if failed:
            # Одна из стадий отказала: нижестоящие процессы могут ожидать
            # данные бесконечно — завершаем их принудительно.
            remaining = [i for i, p in enumerate(procs) if rcs[i] is None]
            terminate_processes([procs[i] for i in remaining])
            killed.update(remaining)
            for i in remaining:
                try:
                    rcs[i] = procs[i].wait(timeout=10)
                except Exception:
                    rcs[i] = -1
            break
        if time.monotonic() >= deadline:
            remaining = [i for i, p in enumerate(procs) if rcs[i] is None]
            terminate_processes(list(procs))
            killed.update(remaining)
            for i in remaining:
                try:
                    rcs[i] = procs[i].wait(timeout=10)
                except Exception:
                    rcs[i] = -1
            raise error_class(
                "{}: превышен таймаут ожидания ({} с); процессы конвейера "
                "принудительно завершены (SIGTERM/SIGKILL).".format(
                    what, int(timeout)
                )
            )
        time.sleep(0.05)
    for proc in procs:
        unregister_process(proc)
    return rcs, killed


def new_stderr_capture():
    """Временный автоудаляемый файл для stderr дочернего процесса."""
    return tempfile.TemporaryFile(mode="w+")


def stderr_tail(tmpfile, limit=600):
    """Читает хвост stderr из временного файла (для сообщений об ошибках)."""
    if tmpfile is None:
        return ""
    try:
        tmpfile.seek(0)
        data = tmpfile.read()
    except (OSError, ValueError):
        return ""
    text = (data or "").strip()
    if len(text) > limit:
        text = "..." + text[-limit:]
    return text


def human_size(num_bytes):
    """Размер в человекочитаемом виде."""
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return "?"
    units = ["Б", "КиБ", "МиБ", "ГиБ", "ТиБ"]
    for unit in units:
        if abs(size) < 1024.0 or unit == units[-1]:
            if unit == "Б":
                return "{:.0f} {}".format(size, unit)
            return "{:.1f} {}".format(size, unit)
        size /= 1024.0
    return "{:.0f} Б".format(num_bytes)
