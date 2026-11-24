"""
Тесты метаданных (ТЗ 8): JSON sidecar, атомарная запись, статусы,
восстановление характеристик без sidecar (совместимость, ТЗ 19).
Запуск: python3 tests/test_metadata.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Suite, run_main  # noqa: E402
from core import metadata as md  # noqa: E402


def main():
    suite = Suite("metadata")

    # parse_archive_name: все поколения (ТЗ 19)
    parsed = md.parse_archive_name("root_2026-11-06_120000_a1b2c3.zst.gpg")
    suite.check(parsed and parsed["subvolume"] == "root" and
                parsed["run_id"] == "a1b2c3" and
                parsed["compression"] == "zstd" and parsed["generation"] == 1,
                "разбор имени v1.1")
    parsed = md.parse_archive_name("home_2026-03-01_120000_123456.btrfs.zst.gpg")
    suite.check(parsed and parsed["generation"] == 2 and
                parsed["compression"] == "zstd",
                "разбор имени v1.0 v3 (.btrfs.zst)")
    parsed = md.parse_archive_name("var_2026-03-01_120000.btrfs.gz.gpg")
    suite.check(parsed and parsed["run_id"] == "" and
                parsed["compression"] == "gzip",
                "разбор имени v1.0 (без run id)")
    parsed = md.parse_archive_name("srv1_root_2026-11-06_120000_a1b2c3.zst.gpg")
    suite.check(parsed and parsed["hostname"] == "srv1" and
                parsed["subvolume"] == "root" and parsed["run_id"] == "a1b2c3",
                "v1.3: префикс хоста разбирается (замечание пользователя)")
    parsed = md.parse_archive_name("srv1_data_disk_2026-03-01_120000.gz.gpg")
    suite.check(parsed and parsed["hostname"] == "srv1" and
                parsed["subvolume"] == "data_disk" and parsed["run_id"] == "",
                "v1.3: хост + subvolume с подчеркиванием")
    suite.check(md.parse_archive_name("random.tar.gz") is None,
                "посторонний файл не опознан")

    with tempfile.TemporaryDirectory() as tmp:
        archive_path = Path(tmp) / "root_2026-11-06_120000_a1b2c3.zst.gpg"
        archive_path.write_bytes(b"\x00" * 1234)

        # new_metadata: полный набор полей ТЗ 8
        meta = md.new_metadata(
            archive_name=archive_path.name, subvolume_name="root",
            subvolume_path="/", run_id="a1b2c3", compression="zstd",
            encrypted=True, snapshot_id="root_2026-11-06_120000_a1b2c3",
            size=1234)
        suite.check("hostname" in meta and meta["hostname"],
                    "метаданные содержат имя хоста (v1.3)")
        for field in ("archive", "subvolume_name", "subvolume_path", "created",
                      "run_id", "compression", "encrypted", "sha256", "size",
                      "status", "upload_status", "uploaded_at", "app_version",
                      "snapshot", "hostname"):
            suite.check(field in meta, "метаданные содержат поле " + field)

        # Запись/чтение
        suite.check(md.write_metadata(archive_path, meta),
                    "метаданные записаны")
        loaded = md.read_metadata(archive_path)
        suite.check(loaded and loaded["run_id"] == "a1b2c3",
                    "метаданные читаются обратно")

        # Атомарность: tmp-файл не остается
        suite.check(not (Path(tmp) / (archive_path.name + ".json.tmp")).exists(),
                    "атомарная запись не оставляет tmp-файл")

        # Статусы (ТЗ 8)
        meta = md.set_status(meta, md.STATUS_VERIFIED)
        suite.check(meta["status"] == "verified" and
                    meta["upload_status"] == "none",
                    "verified -> upload_status=none")
        meta = md.set_status(meta, md.STATUS_UPLOAD_PENDING)
        suite.check(meta["upload_status"] == "pending",
                    "upload-pending синхронизирован")
        meta = md.set_status(meta, md.STATUS_UPLOADED)
        suite.check(meta["uploaded_at"] is not None,
                    "uploaded фиксирует дату отправки")
        meta = md.set_status(meta, md.STATUS_UPLOAD_FAILED)
        suite.check(meta["upload_status"] == "upload-failed" and
                    meta["uploaded_at"] is not None,
                    "upload-failed сохраняет дату прошлой отправки")

        # update_status на архиве
        md.update_status(archive_path, md.STATUS_UPLOAD_PENDING)
        suite.check(md.read_metadata(archive_path)["status"] == "upload-pending",
                    "update_status изменяет файл")

        # Поврежденный sidecar -> None, не исключение (ТЗ 8)
        md_path = md.metadata_path(archive_path)
        md_path.write_text("{corrupt json", encoding="utf-8")
        suite.check(md.read_metadata(archive_path) is None,
                    "поврежденный JSON -> None (без исключения)")

        # infer_archive_info: восстановление характеристик по имени+сумме
        checksum = Path(str(archive_path) + ".sha256")
        checksum.write_text("ab" * 32 + "  " + archive_path.name + "\n",
                            encoding="utf-8")
        info = md.infer_archive_info(archive_path)
        suite.check(info is not None and info["subvolume_name"] == "root" and
                    info["compression"] == "zstd" and
                    info["sha256"] == "ab" * 32 and info["size"] == 1234 and
                    info.get("inferred") is True,
                    "характеристики восстановлены по имени и .sha256")
        suite.check(md.parse_archive_name("x.txt") is None and
                    md.infer_archive_info(Path(tmp) / "unknown.bin") is None,
                    "неопознанный файл не имеет характеристик")

        # load_or_infer: поврежденные метаданные -> вывод из имени
        got = md.load_or_infer(archive_path)
        suite.check(got.get("inferred") is True and
                    got["compression"] == "zstd",
                    "load_or_infer использует имя при поврежденном sidecar")

        # load_or_infer: расхождение sidecar с расширением -> приоритет имени
        md_path.write_text(json.dumps({"compression": "gzip", "sha256": None,
                                       "archive": archive_path.name}),
                           encoding="utf-8")
        got = md.load_or_infer(archive_path)
        suite.check(got["compression"] == "zstd",
                    "сжатие из sidecar противоречит расширению -> берется архив")

        # load_or_infer: неопознанный архив — ошибка
        stranger = Path(tmp) / "stranger.bin"
        stranger.write_bytes(b"x")
        try:
            md.load_or_infer(stranger)
            suite.check(False, "неопознанный архив должен давать ошибку")
        except Exception:
            suite.check(True, "неопознанный архив -> VerificationError")

    return suite.summary()


if __name__ == "__main__":
    sys.exit(run_main(main))
