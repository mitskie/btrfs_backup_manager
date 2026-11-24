"""Тесты сжатия и fallback (ТЗ 6, 20.2). Запуск: python3 tests/test_compression.py"""

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    Suite, confirm_false, confirm_true, run_main,
)
import config  # noqa: E402
from core import (  # noqa: E402
    CompressionError, OperationCancelled, UtilityMissingError,
)
from core import compression as cm  # noqa: E402

SHIM_ZSTD = Path(__file__).resolve().parent / "zstd"


class FakeShutilWhich:
    """Управляемый shutil.which: кто из компрессоров «установлен»."""

    def __init__(self, zstd=True, gzip=True):
        self.available = {"zstd": zstd, "gzip": gzip}
        self._real = shutil.which

    def __call__(self, name, *args, **kwargs):
        if name in self.available and not self.available[name]:
            return None
        return self._real(name, *args, **kwargs)


def _with_tools(zstd, gzip):
    cm.tool_available = lambda name: {"zstd": zstd, "gzip": gzip}[name]
    cm.available_tools = lambda: [n for n, ok in
                                  (("zstd", zstd), ("gzip", gzip)) if ok]


def main():
    suite = Suite("compression")

    # --- Определение сжатия по расширению (оба поколения, ТЗ 19) ---
    suite.check(cm.detect_compression("root_2026-11-06_120000_a1b2c3.zst.gpg")
                == "zstd", "расширение .zst.gpg (v1.1) -> zstd")
    suite.check(cm.detect_compression("root_2026-11-06_120000_a1b2c3.gz.gpg")
                == "gzip", "расширение .gz.gpg (v1.1) -> gzip")
    suite.check(cm.detect_compression(
        "root_2026-03-01_120000_123456.btrfs.zst.gpg") == "zstd",
        "расширение .btrfs.zst.gpg (v1.0 v3) -> zstd")
    suite.check(cm.detect_compression("root_2026-03-01_120000.btrfs.gz.gpg")
                == "gzip", "расширение .btrfs.gz.gpg (v1.0) -> gzip")
    suite.expect_raises(
        lambda: cm.detect_compression("archive.tar.gz"),
        Exception, "неизвестное расширение — ошибка")

    # --- Имена снапшотов из архивов ---
    suite.check(cm.snapshot_name_from_archive(
        "root_2026-11-06_120000_a1b2c3.zst.gpg")
        == "root_2026-11-06_120000_a1b2c3", "имя снапшота v1.1")
    suite.check(cm.snapshot_name_from_archive(
        "root_2026-03-01_120000_123456.btrfs.zst.gpg")
        == "root_2026-03-01_120000_123456", "имя снапшота v1.0 v3")
    suite.check(cm.archive_filename("root", "2026-11-06_120000_a1b2c3", "zstd",
                                    hostname="")
                == "root_2026-11-06_120000_a1b2c3.zst.gpg",
                "формат имени архива без хоста (ТЗ 5.3, v1.1)")
    suite.check(cm.archive_filename("root", "2026-11-06_120000_a1b2c3", "zstd",
                                    hostname="srv1")
                == "srv1_root_2026-11-06_120000_a1b2c3.zst.gpg",
                "v1.3: префикс хоста в имени архива (замечание пользователя)")
    suite.check(cm.snapshot_name_from_archive(
        "srv1_root_2026-11-06_120000_a1b2c3.zst.gpg")
        == "root_2026-11-06_120000_a1b2c3",
        "имя снапшота из архива с префиксом хоста (хост отбрасывается)")

    # --- Команды компрессоров ---
    suite.check(cm.compressor_cmd("gzip", 6) == ["gzip", "-c", "-6"],
                "команда gzip")
    suite.check(cm.compressor_cmd("zstd", 3) == ["zstd", "-c", "-3", "-q"],
                "команда zstd")
    suite.check(cm.decompressor_cmd("zstd") == ["zstd", "-d", "-c", "-q"],
                "команда распаковки zstd")
    suite.check(cm.stream_test_cmd("gzip") == ["gzip", "-t"], "тест gzip -t")

    # --- Матрица fallback (ТЗ 20.2) ---
    real_which = shutil.which

    # 1. zstd доступен — используется zstd
    _with_tools(True, True)
    suite.check(cm.resolve_compression(config, non_interactive=False,
                                       confirm_fn=confirm_true) == "zstd",
                "20.2-1: zstd доступен -> zstd")
    suite.check(cm.resolve_compression(config, non_interactive=True) == "zstd",
                "20.2-1: zstd доступен (cron) -> zstd")

    # 2-4. zstd отсутствует, gzip доступен: интерактивный режим
    _with_tools(False, True)
    answers = []
    chosen = cm.resolve_compression(
        config, non_interactive=False,
        confirm_fn=lambda text: (answers.append(text), True)[1])
    suite.check(chosen == "gzip", "20.2-3: подтверждение -> gzip для операции")
    suite.check(config.COMPRESSION == "zstd",
                "20.2-3: config.py НЕ изменяется")
    suite.check(answers and "ВНИМАНИЕ" in answers[0] and
                "zstd" in answers[0] and "gzip" in answers[0],
                "20.2-2: текст диалога содержит zstd/gzip (ТЗ 6.1)")
    suite.expect_raises(
        lambda: cm.resolve_compression(config, non_interactive=False,
                                       confirm_fn=confirm_false),
        OperationCancelled, "20.2-4: отказ пользователя -> отмена операции")

    # 5-6. cron
    suite.check(cm.resolve_compression(config, non_interactive=True) == "gzip",
                "20.2-5: cron, fallback разрешен -> gzip")
    config.CRON_COMPRESSION_FALLBACK = False
    suite.expect_raises(
        lambda: cm.resolve_compression(config, non_interactive=True),
        UtilityMissingError, "20.2-6: cron, fallback запрещен -> ошибка")
    config.CRON_COMPRESSION_FALLBACK = True

    # 7. отсутствуют обе утилиты — backup не начинается
    _with_tools(False, False)
    suite.expect_raises(
        lambda: cm.resolve_compression(config, non_interactive=True),
        UtilityMissingError, "20.2-7: обе утилиты отсутствуют -> ошибка до начала")
    suite.expect_raises(
        lambda: cm.resolve_compression(config, non_interactive=False,
                                       confirm_fn=confirm_true),
        UtilityMissingError, "20.2-7: обе утилиты отсутствуют (интерактив)")

    # 8-9. Восстановление независимо от COMPRESSION — на уровне детекта:
    _with_tools(True, True)
    old_compression = config.COMPRESSION
    config.COMPRESSION = "zstd"
    suite.check(cm.detect_compression("home_2026-11-06_120000_abc123.gz.gpg")
                == "gzip",
                "20.2-8: gzip-архив опознан при COMPRESSION=zstd")
    config.COMPRESSION = "gzip"
    suite.check(cm.detect_compression("home_2026-11-06_120000_abc123.zst.gpg")
                == "zstd",
                "20.2-9: zstd-архив опознан при COMPRESSION=gzip")
    config.COMPRESSION = old_compression

    # Реальный shutil.which восстанавливается
    shutil.which = real_which

    # --- Реальные утилиты в окружении тестов (шим zstd в PATH) ---
    suite.check(cm.tool_available("zstd"),
                "zstd доступен в тестовой среде (шим)")
    suite.check(cm.tool_available("gzip"), "gzip доступен (реальный)")

    return suite.summary()


if __name__ == "__main__":
    sys.exit(run_main(main))
