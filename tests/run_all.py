#!/usr/bin/env python3
"""
Runner всех тестов Btrfs Backup Manager.

Каждый тестовый модуль запускается ОТДЕЛЬНЫМ процессом (изоляция подмен
окружения), результаты агрегируются и сохраняются в tests/last-results.json.

Запуск: python3 tests/run_all.py [имя_модуля ...]
Пример: python3 tests/run_all.py test_compression test_network

Результаты: [PASS]/[FAIL]/[SKIP] построчно + сводка. Возвращает 1 при
наличии провалов. Пропущенные тесты НЕ засчитываются как пройденные.
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT = TESTS_DIR.parent

ALL_MODULES = [
    "test_static",
    "test_config",
    "test_discovery",
    "test_appconfig",
    "test_compression",
    "test_process",
    "test_encryption",
    "test_snapshots",
    "test_metadata",
    "test_verification",
    "test_network",
    "test_backup",
    "test_upload_pending",
    "test_retention",
    "test_restore",
    "test_integration",
    "integration_restore",
    "test_cli",
    "test_tui",
    "test_deletion",
    "integration_btrfs",
]

MODULE_TIMEOUT = 300  # секунд на модуль


def run_module(name):
    """Запускает модуль, возвращает (status, results, elapsed, output_tail)."""
    path = TESTS_DIR / (name + ".py")
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True, text=True, timeout=MODULE_TIMEOUT,
            cwd=str(PROJECT),
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - started
        return ("TIMEOUT", [], elapsed,
                ["модуль не завершился за {} с — зависание (это провал)".
                 format(MODULE_TIMEOUT)])
    elapsed = time.monotonic() - started
    output = (proc.stdout or "") + (proc.stderr or "")
    results = []
    for line in output.splitlines():
        # re.search, а не re.match (ТЗ 14.4): эхо промпта input() без
        # перевода строки может склеиться с последующей строкой [PASS],
        # и проверка выпадала из учета (расхождение 648/644).
        match = re.search(r"\[(PASS|FAIL|SKIP)\]\s*(.*)", line)
        if match:
            results.append((match.group(1), match.group(2)))
    if proc.returncode != 0:
        status = "FAIL"
    else:
        status = "OK"
    tail = output.splitlines()[-60:]
    return status, results, elapsed, tail


def main():
    modules = sys.argv[1:] or ALL_MODULES
    totals = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    report = {
        "started": datetime.now().isoformat(timespec="seconds"),
        "modules": [],
    }
    overall_ok = True
    print("=" * 72)
    print("Btrfs Backup Manager — прогон тестов")
    print("=" * 72)
    for name in modules:
        print("\n--- {} ---".format(name))
        status, results, elapsed, tail = run_module(name)
        module_entry = {
            "module": name, "status": status,
            "elapsed_sec": round(elapsed, 1),
            "results": [{"status": s, "message": m} for s, m in results],
        }
        passed = sum(1 for s, _ in results if s == "PASS")
        failed = sum(1 for s, _ in results if s == "FAIL")
        skipped = sum(1 for s, _ in results if s == "SKIP")
        totals["PASS"] += passed
        totals["FAIL"] += failed
        totals["SKIP"] += skipped
        if status in ("FAIL", "TIMEOUT"):
            overall_ok = False
            module_entry["output_tail"] = tail
            print("  [МОДУЛЬ ЗАВЕРШЕН С ОШИБКОЙ: {} за {:.1f} с]".format(
                status, elapsed))
            for line in tail:
                print("    | " + line)
        else:
            print("  {} тестов: прошло {}, провалено {}, пропущено {} "
                  "({:.1f} с)".format(len(results), passed, failed, skipped,
                                      elapsed))
        report["modules"].append(module_entry)

    print("\n" + "=" * 72)
    print("ИТОГО: прошло {}, провалено {}, пропущено {}".format(
        totals["PASS"], totals["FAIL"], totals["SKIP"]))
    print("=" * 72)
    report["totals"] = totals
    report["finished"] = datetime.now().isoformat(timespec="seconds")
    report["ok"] = overall_ok
    out_path = TESTS_DIR / "last-results.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print("Подробный отчет: {}".format(out_path))
    if totals["SKIP"]:
        print("ВНИМАНИЕ: часть тестов пропущена — см. TEST-REPORT.md; "
              "пропуск не засчитывается как успех.")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
