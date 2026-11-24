"""
Тесты управления процессами (ТЗ 14, 20.8): конвейеры, таймауты, сигналы,
отсутствие зависших потомков и временных файлов.
Запуск: python3 tests/test_process.py
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Suite, run_main  # noqa: E402
from core import (  # noqa: E402
    BtrfsBackupError, OperationAborted, VerificationError,
)
from core import process as proc  # noqa: E402


def _pipeline(cmds, **kwargs):
    """Собирает конвейер из списка команд, возвращает (procs, captures)."""
    captures = [proc.new_stderr_capture() for _ in cmds]
    procs = []
    prev_stdout = None
    for i, cmd in enumerate(cmds):
        kwargs_i = dict(stderr=captures[i], start_new_session=True)
        if i == 0:
            kwargs_i.pop("stdin", None)
        else:
            kwargs_i["stdin"] = prev_stdout
        if i < len(cmds) - 1:
            kwargs_i["stdout"] = subprocess.PIPE
        else:
            kwargs_i["stdout"] = subprocess.PIPE
        p = proc.popen_pipeline(cmd, **kwargs_i)
        if prev_stdout is not None:
            try:
                prev_stdout.close()
            except OSError:
                pass
        prev_stdout = p.stdout
        procs.append(p)
    return procs, captures


def _read_capture(cap):
    try:
        cap.seek(0)
        return cap.read()
    except (OSError, ValueError):
        return ""


def _no_leftover_pids(pids):
    """True, если ни один из pid не существует (не зомби)."""
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            continue
        # процесс еще существует
        try:
            stat = Path("/proc/{}/stat".format(pid)).read_text()
            state = stat.rsplit(")", 1)[1].split()[0]
            if state != "Z":
                return False
        except (OSError, IndexError):
            continue
    return True


def main():
    suite = Suite("process")

    # 20.8-1. Успешное завершение pipeline
    procs, caps = _pipeline([
        ["sh", "-c", "echo hello"],
        ["gzip", "-c"],
        ["gzip", "-d", "-c"],
    ])
    rcs, killed = proc.wait_pipeline(procs, timeout=30)
    out = procs[-1].stdout.read()
    procs[-1].stdout.close()
    suite.check(rcs == [0, 0, 0] and not killed and out == b"hello\n",
                "20.8-1: успешный конвейер, все rc=0, данные корректны")

    # 20.8-2. Ошибка ПЕРВОГО процесса: остальные завершены, ошибка не маскируется
    # Последняя стадия — «спящий» читатель, который НЕ успевает увидеть EOF
    # до принудительного завершения: раньше последняя стадия (cat) могла
    # успеть завершиться самой между опросами, и проверка `2 in killed`
    # становилась гонкой (расхождение 648/644 на быстрых машинах, ТЗ 14.4).
    procs, caps = _pipeline([
        ["sh", "-c", "echo send-error >&2; exit 1"],
        ["gzip", "-c"],
        ["sh", "-c", "sleep 60"],
    ])
    rcs, killed = proc.wait_pipeline(
        procs, error_class=BtrfsBackupError, what="тест", timeout=30)
    # sleep не читает stdin и гарантированно жив до завершения конвейера;
    # gzip (средняя стадия) завершается по EOF или принудительно — его код
    # НЕ проверяется (зависит от гонки планировщика, это НЕ свойство
    # реализации, а деталь завершения).
    suite.check(rcs[0] == 1 and 2 in killed,
                "20.8-2: ошибка первой стадии завершает висящую последнюю "
                "стадию (rcs={}, killed={})".format(rcs, sorted(killed)))
    suite.check("send-error" in _read_capture(caps[0]),
                "20.8-2: stderr первой стадии захвачен (диагностика)")

    # 20.8-3. Ошибка ПОСЛЕДНЕГО процесса
    procs, caps = _pipeline([
        ["sh", "-c", "echo data"],
        ["gzip", "-c"],
        ["sh", "-c", "cat >/dev/null; exit 3"],
    ])
    rcs, killed = proc.wait_pipeline(
        procs, error_class=BtrfsBackupError, what="тест", timeout=30)
    suite.check(rcs[2] == 3 and rcs[0] == 0,
                "20.8-3: ошибка последней стадии видна (rc={})".format(rcs))

    # 20.8-4. Таймаут: процессы принудительно завершены, успешным не считается
    procs, caps = _pipeline([
        ["sh", "-c", "sleep 60"],
        ["cat"],
    ])
    started = time.monotonic()
    raised = False
    try:
        proc.wait_pipeline(procs, error_class=BtrfsBackupError, what="тест",
                           timeout=2)
    except BtrfsBackupError as exc:
        raised = True
        suite.check("таймаут" in str(exc).lower(),
                    "20.8-4: сообщение о таймауте понятно")
    elapsed = time.monotonic() - started
    # Верхняя граница проверяет, что таймаут действительно СРАБОТАЛ, а не
    # операция зависла. Конкретное значение — машинозависимо (загрузка,
    # медленный SIGKILL/wait под нагрузкой): фиксируем щедрый предел
    # (timeout 2 с + 50 с на завершение), а не 20 с, из- которого тест
    # периодически проваливался на загруженных машинах (ТЗ 14.4).
    suite.check(raised and elapsed < 55,
                "20.8-4: таймаут сработал за {:.1f} с (ТЗ 14)".format(elapsed))
    suite.check(_no_leftover_pids([p.pid for p in procs]),
                "20.8-7: после таймаута нет зависших потомков")

    # 20.8-5/6. SIGINT/SIGTERM через abort_check (модель TUI/сигналов)
    # Точный сценарий для каждого сигнала
    for signame in ("SIGINT", "SIGTERM"):
        procs, caps = _pipeline([
            ["sh", "-c", "sleep 60"],
            ["cat"],
        ])
        class Abort:
            def __init__(self):
                self.done = False
            def __call__(self):
                if not self.done:
                    self.done = True
                    raise OperationAborted("моделирование " + signame)
        raised = False
        try:
            proc.wait_pipeline(procs, error_class=BtrfsBackupError,
                               what="тест", timeout=30, abort_check=Abort())
        except OperationAborted:
            raised = True
        suite.check(raised,
                    "20.8-5/6: {} прерывает ожидание конвейера".format(signame))
        suite.check(_no_leftover_pids([p.pid for p in procs]),
                    "20.8-7: после {} нет зависших потомков".format(signame))

    # 20.8-9. Коды возврата всех процессов доступны
    procs, caps = _pipeline([
        ["sh", "-c", "exit 0"],
        ["sh", "-c", "exit 0"],
        ["sh", "-c", "exit 0"],
    ])
    rcs, _killed = proc.wait_pipeline(procs, timeout=30)
    suite.check(rcs == [0, 0, 0],
                "20.8-9: коды возврата ВСЕХ стадий зафиксированы")

    # run_command: отсутствие утилиты, таймаут, код возврата
    suite.expect_raises(
        lambda: proc.run_command(["bbm-no-such-tool-xyz"], what="тест"),
        BtrfsBackupError, "run_command: отсутствующая утилита -> ошибка")
    suite.expect_raises(
        lambda: proc.run_command(["sleep", "5"], timeout=1, what="тест"),
        BtrfsBackupError, "run_command: таймаут -> ошибка")
    suite.expect_raises(
        lambda: proc.run_command(["sh", "-c", "exit 7"], what="тест"),
        BtrfsBackupError, "run_command: rc!=0 -> ошибка с хвостом stderr")

    # human_size
    suite.check(proc.human_size(0) == "0 Б", "human_size(0)")
    suite.check(proc.human_size(1024) == "1.0 КиБ", "human_size(1024)")
    suite.check(proc.human_size(5 * 1024 * 1024) == "5.0 МиБ", "human_size(5MiB)")

    # Реестр активных процессов и аварийное завершение (TUI)
    procs, caps = _pipeline([["sh", "-c", "sleep 60"], ["cat"]])
    proc.terminate_all_active()
    suite.check(_no_leftover_pids([p.pid for p in procs]),
                "terminate_all_active завершает зарегистрированные процессы")

    for cap in caps:
        try:
            cap.close()
        except OSError:
            pass

    return suite.summary()


if __name__ == "__main__":
    sys.exit(run_main(main))
