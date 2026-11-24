"""
Тесты сетевого хранилища (ТЗ 7): монтирование, отправка, финализация,
идемпотентность upload-pending.
Запуск: python3 tests/test_network.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    PASSWORD, Sandbox, Suite, cleanup_network_files, make_archive, run_main,
)
import config  # noqa: E402
from core import StorageError, VerificationError  # noqa: E402
from core import metadata as md  # noqa: E402
from core import network as net  # noqa: E402
from core import verification as vr  # noqa: E402


def main():
    suite = Suite("network")

    # --- Проверка монтирования (ТЗ 7.2) ---
    with Sandbox() as box:
        info = net.check_network_mount(box.network_mount)
        suite.check(info.writable, "смонтированная шара доступна для записи")

        # Каталог существует, но НЕ является точкой монтирования (эмуляция:
        # NETMOUNT_IS_MOUNTED=False) — обычный каталог не считается шарой
        import common
        common.NETMOUNT_IS_MOUNTED[0] = False
        suite.expect_raises(
            lambda: net.check_network_mount(box.network_mount),
            StorageError, "каталог без активного монтирования — НЕ шара")
        # Обязательная проверка не зависит от CHECK_NETWORK_MOUNT
        config.CHECK_NETWORK_MOUNT = False
        suite.expect_raises(
            lambda: net.require_network_mount(box.network_mount),
            StorageError, "require_network_mount игнорирует CHECK_NETWORK_MOUNT")
        suite.expect_raises(
            lambda: net.require_network_mount_for_retention(
                box.network_mount, box.network_dir),
            StorageError, "retention: без активного монтирования удаление "
            "отменено (ТЗ 9.3)")
        config.CHECK_NETWORK_MOUNT = True
        common.NETMOUNT_IS_MOUNTED[0] = True

        # Не существующая точка
        suite.expect_raises(
            lambda: net.check_network_mount(box.root / "no-such-mount"),
            StorageError, "несуществующая точка монтирования -> ошибка")

        # Несоответствующий тип ФС
        suite.expect_raises(
            lambda: net.check_network_mount(box.network_mount,
                                            expected_fs_type="cifs"),
            StorageError, "тип ФС не совпал -> ошибка")

    # --- Отправка и финализация (ТЗ 7.3) ---
    with Sandbox() as box:
        payload = os.urandom(120000) + b"NET-TEST" * 50
        path, _snap, sha = make_archive(box, "root", payload,
                                        compression="gzip")
        common.NETMOUNT_IS_MOUNTED[0] = True

        # Копия во временный файл + проверки размера и суммы
        tmp_path, actual = net.copy_to_network_tmp(path, box.network_dir, sha)
        suite.check(tmp_path.name == path.name + ".tmp" and
                    tmp_path.is_file(),
                    "архив скопирован во временный файл *.tmp")
        suite.check(actual == sha, "SHA-256 временной копии совпадает")

        # Финализация: атомарное переименование + повторная сверка
        # (v1.3: отдельный файл .sha256 на шаре НЕ создается — сумма
        # хранится в JSON-метаданных, замечание пользователя)
        final = net.promote_network_archive(tmp_path, sha)
        suite.check(final.name == path.name and final.is_file(),
                    "tmp переименован в окончательное имя")
        suite.check(not (box.network_dir / (final.name + ".sha256")).is_file(),
                    "v1.3: файл .sha256 на шаре не создается (сумма в JSON)")
        suite.check(not tmp_path.exists(), "tmp-файл не остался")

        # Идемпотентность: повторная финализация того же содержимого
        tmp2, _ = net.copy_to_network_tmp(path, box.network_dir, sha)
        final2 = net.promote_network_archive(tmp2, sha)
        suite.check(final2 == final and final2.is_file(),
                    "повторная отправка не создает дубликатов (ТЗ 7.5)")

        # Конфликт содержимого: перезапись запрещена, локальный сохранен
        other = box.root / "other.gpg"
        other.write_bytes(b"different-content")
        tmp3, sha3 = net.copy_to_network_tmp(other, box.network_dir,
                                             vr.sha256_file(other))
        target_name = tmp3.with_name(final.name + ".tmp")
        os.replace(str(tmp3), str(target_name))
        suite.expect_raises(
            lambda: net.promote_network_archive(target_name, sha3),
            StorageError, "существующий архив с другим содержимым не "
            "перезаписывается")
        suite.check(final.is_file() and
                    vr.sha256_file(final) == sha,
                    "исходный сетевой архив не поврежден")

        # Ошибка копирования: несовпадающая сумма -> tmp удален, ошибка видна
        suite.expect_raises(
            lambda: net.copy_to_network_tmp(path, box.network_dir, "ef" * 32),
            StorageError, "SHA-256 копии не совпал -> ошибка отправки")
        suite.check(not (box.network_dir / (path.name + ".tmp")).exists(),
                    "неудачная отправка не оставляет временных файлов")

        # Подтвержденность сетевой копии (retention, ТЗ 9.3)
        snap_name = path.name.replace(".gz.gpg", "").replace(".zst.gpg", "")
        # v1.3: без эталонной суммы копия НЕ подтверждена (снапшот сохранен)
        suite.check(not net.network_archive_confirmed(box.network_dir,
                                                      snap_name),
                    "без метаданных и .sha256 копия не подтверждена")
        # JSON-метаданные (v1.3) подтверждают копию
        meta = md.read_metadata(path)
        suite.check(net.write_network_metadata(box.network_dir, meta, sha),
                    "сетевые метаданные JSON записаны")
        suite.check(net.network_archive_confirmed(box.network_dir, snap_name),
                    "копия подтверждена: архив + JSON(sha256) + фактический "
                    "SHA-256")
        # Legacy .sha256 (архивы предыдущих версий, ТЗ 19) тоже работает
        (box.network_dir / (final.name + ".json")).unlink()
        vr.write_checksum_file(final, sha)
        suite.check(net.network_archive_confirmed(box.network_dir, snap_name),
                    "legacy .sha256 подтверждает копию (ТЗ 19)")
        # Поддельная сумма (не совпадает с архивом) -> не подтверждена
        (box.network_dir / (final.name + ".sha256")).write_text(
            "be" * 32 + "  x\n", encoding="utf-8")
        suite.check(not net.network_archive_confirmed(box.network_dir,
                                                      snap_name),
                    "поддельный sidecar не подтверждает копию (ТЗ 20.5-4)")
        vr.write_checksum_file(final, sha)

        # Поврежденный сетевой архив -> не подтверждена
        saved = final.read_bytes()
        final.write_bytes(saved[:len(saved) // 2])
        suite.check(not net.network_archive_confirmed(box.network_dir,
                                                      snap_name),
                    "поврежденная сетевая копия не подтверждена (ТЗ 20.5-7)")
        final.write_bytes(saved)

        # Отсутствующий архив
        suite.check(not net.network_archive_confirmed(
            box.network_dir, "root_2026-01-01_000000_ffffff"),
            "несуществующий снапшот не подтвержден")

        # Старое поколение имен (.btrfs.) тоже опознается
        legacy = box.network_dir / "root_2026-03-01_120000_000123.btrfs.gz.gpg"
        legacy.write_bytes(saved)
        vr.write_checksum_file(legacy, sha)
        suite.check(net.network_archive_confirmed(
            box.network_dir, "root_2026-03-01_120000_000123"),
            "подтвержденность опознает архивы v1.0 v3 (ТЗ 19)")

        # Очистка осиротевших tmp (только своего шаблона)
        junk = box.network_dir / "readme.txt"
        junk.write_text("не трогать")
        tmp_orphan = box.network_dir / "orphan.gz.gpg.tmp"
        tmp_orphan.write_bytes(b"123")
        removed = net.cleanup_network_tmp_files(box.network_dir)
        suite.check(removed >= 1 and not tmp_orphan.exists() and junk.exists(),
                    "очистка удаляет только *.tmp своего шаблона")

        # Метаданные на шаре
        meta = {"archive": final.name, "sha256": sha, "status": "uploading"}
        suite.check(net.write_network_metadata(box.network_dir, meta, sha),
                    "сетевые метаданные записаны")
        suite.check(net.write_network_metadata(box.network_dir, meta, sha),
                    "повторная запись тех же метаданных безопасна")
        other_meta = dict(meta, sha256="cd" * 32)
        suite.check(not net.write_network_metadata(
            box.network_dir, other_meta, "cd" * 32),
            "метаданные чужого содержимого не перезаписывают существующие")

    # --- ТЗ 3.5 (исправление №1): mountinfo — источник истины ---
    with Sandbox() as box:
        import common
        # Разбор экранированных последовательностей mountinfo
        suite.check(net._unescape_mountinfo_field(
            "/mnt/with\\040space") == "/mnt/with space",
            "mountinfo: \\040 раскодируется в пробел")
        suite.check(net._unescape_mountinfo_field(
            "a\\134b\\012c") == "a\\b\nc",
            "mountinfo: \\134 и \\012 раскодируются")
        # Самое глубокое монтирование-префикс
        entries = [("/", "ext4", "/dev/root"),
                   ("/mnt", "ext4", "/dev/sda1"),
                   ("/mnt/share", "nfs4", "srv:/export"),
                   ("/mnt/share/deep", "cifs", "//x/y")]
        suite.check(net.mountinfo_for("/mnt/share/file", entries) ==
                    ("/mnt/share", "nfs4", "srv:/export"),
                    "mountinfo_for выбирает самое глубокое монтирование-префикс")
        suite.check(net.mountinfo_for("/mnt/other", entries) ==
                    ("/mnt", "ext4", "/dev/sda1"),
                    "mountinfo_for не принимает чужое подмонтирование за свое")
        # mountinfo содержит запись для каталога, но os.path.ismount ее НЕ
        # подтверждает — расхождение механизмов: отказ (fail-closed, ТЗ 18)
        real_entries = net._mountinfo_entries
        try:
            net._mountinfo_entries = lambda: [
                (str(Path(box.network_mount).resolve()), "nfs4", "srv:/e")]
            common.NETMOUNT_IS_MOUNTED[0] = False
            suite.expect_raises(
                lambda: net.check_network_mount(box.network_mount),
                StorageError, "mountinfo говорит «монтирование», ismount — "
                "нет: расхождение -> отказ")
            # Обратное расхождение невозможно через один флаг; главное —
            # проверка двух независимых механизмов обязательна
        finally:
            net._mountinfo_entries = real_entries
            common.NETMOUNT_IS_MOUNTED[0] = True
        # CHECK_NETWORK_MOUNT=False НЕ отключает обязательную проверку (ТЗ 3.5 п.3):
        # каталог существует, но не смонтирован — отправка запрещена
        config.CHECK_NETWORK_MOUNT = False
        common.NETMOUNT_IS_MOUNTED[0] = False
        suite.expect_raises(
            lambda: net.check_network_mount(box.network_mount, check=False),
            StorageError, "check=False не отключает проверку монтирования "
            "(ТЗ 3.2: настройка не может отключить безопасность)")
        common.NETMOUNT_IS_MOUNTED[0] = True
        config.CHECK_NETWORK_MOUNT = True
        # Повторные проверки (ТЗ 3.3): смонтировано -> ок
        info = net.recheck_network_mount(box.network_mount)
        suite.check(info.fstype == "nfs4",
                    "recheck: смонтированная шара подтверждена")
        # Повторная проверка: шара отключилась -> отказ
        common.NETMOUNT_IS_MOUNTED[0] = False
        suite.expect_raises(
            lambda: net.recheck_network_mount(box.network_mount),
            StorageError, "recheck: не смонтировано -> отказ (ТЗ 3.3)")
        suite.expect_raises(
            lambda: net.recheck_network_mount(box.root / "no-such"),
            StorageError, "recheck: точка исчезла -> отказ")
        common.NETMOUNT_IS_MOUNTED[0] = True

    # --- ТЗ 5.4 (исправление №3): атомарность метаданных ---
    with Sandbox() as box:
        payload = os.urandom(50000)
        path, _snap, sha = make_archive(box, "root", payload)
        meta = md.read_metadata(path)
        target = box.network_dir / (path.name + ".json")
        # Успешная запись подтверждается перечитыванием (ТЗ 5.4 п.1)
        suite.check(net.write_network_metadata(box.network_dir, meta, sha),
                    "метаданные записаны и подтверждены перечитыванием")
        suite.check(target.is_file() and
                    json.loads(target.read_text(encoding="utf-8"))["sha256"]
                    == sha,
                    "файл метаданных существует с полем sha256")
        # Поврежденный существующий sidecar — перезапись ЗАПРЕЩЕНА (ТЗ 5.4 п.5)
        target.write_text("{ broken json", encoding="utf-8")
        suite.check(not net.write_network_metadata(box.network_dir, meta, sha),
                    "поврежденный sidecar не перезаписывается молча (конфликт)")
        suite.check(target.read_text(encoding="utf-8") == "{ broken json",
                    "исходное содержимое sidecar сохранено (ТЗ 2)")
        target.unlink()
        # Ошибка записи JSON -> False, мусор не остается (ТЗ 5.4 п.3)
        real_replace = os.replace
        def _broken_replace(_a, _b):
            raise OSError("simulated ENOSPC")
        os.replace = _broken_replace
        try:
            suite.check(not net.write_network_metadata(box.network_dir,
                                                       meta, sha),
                        "ошибка записи (ENOSPC) -> False")
            suite.check(not target.exists(),
                        "основной JSON не оставлен частично записанным")
            suite.check(not (box.network_dir / (path.name + ".json.tmp"))
                        .exists(),
                        "временный файл удален после ошибки")
        finally:
            os.replace = real_replace
        # Несериализуемое значение -> корректный отказ (ТЗ 5.4 п.3)
        bad_meta = dict(meta, broken=set([1, 2]))
        suite.check(not net.write_network_metadata(box.network_dir, bad_meta,
                                                   sha),
                    "несериализуемые метаданные -> отказ без записи")
        suite.check(not target.exists(),
                    "после отказа файл метаданных не создан")
        # Повторное чтение не подтверждает -> False (ТЗ 5.4 п.6)
        target.unlink(missing_ok=True)
        real_loads = net.json_loads
        state = {"calls": 0}
        def _loads_three(text):
            state["calls"] += 1
            if state["calls"] == 3:  # 1=roundtrip, 2=tmp, 3=перечитывание
                return {"archive": meta["archive"], "sha256": "ef" * 32}
            return real_loads(text)
        net.json_loads = _loads_three
        try:
            suite.check(not net.write_network_metadata(box.network_dir,
                                                       meta, sha),
                        "перечитывание НЕ подтвердило корректность -> False")
        finally:
            net.json_loads = real_loads
        suite.check(target.is_file() and
                    not (box.network_dir / (path.name + ".json.tmp"))
                    .exists(),
                    "записанные, но не подтвержденные метаданные остаются "
                    "на шаре; временный файл не остается")
        # После исправления ошибки повторная запись успешна (ТЗ 5.4 п.7)
        suite.check(net.write_network_metadata(box.network_dir, meta, sha),
                    "после устранения ошибки запись успешна")

    # --- ТЗ 6.4 (исправление №4): retention без привязки к COMPRESSION ---
    with Sandbox() as box:
        import common
        # 1) Старый gzip-архив находит даже при compression="zstd" (ТЗ 6.4 п.1)
        gz_path, _snap, gz_sha = make_archive(box, "root", b"old gzip" * 900,
                                              compression="gzip")
        gz_meta = md.read_metadata(gz_path)
        gz_snap = gz_path.name[:-len(".gz.gpg")]
        gz_final = box.network_dir / gz_path.name
        gz_final.write_bytes(gz_path.read_bytes())
        suite.check(net.write_network_metadata(box.network_dir, gz_meta,
                                               gz_sha),
                    "метаданные gzip-архива записаны")
        suite.check(net.network_archive_confirmed(
                box.network_dir, gz_snap, compression="zstd"),
            "gzip-архив подтвержден при текущем COMPRESSION=zstd (ТЗ 6.4 п.1)")
        suite.check(not net.network_archive_confirmed(
                box.network_dir, "root_2026-11-06_120000_beef01"),
            "отсутствующая копия gzip не подтверждена (ТЗ 6.4 п.5)")
        # 2) Новый zstd-архив находит при compression="gzip" (ТЗ 6.4 п.2)
        zst_path, _snap2, zst_sha = make_archive(box, "home",
                                                 b"new zstd" * 900,
                                                 compression="zstd")
        zst_meta = md.read_metadata(zst_path)
        zst_snap = zst_path.name[:-len(".zst.gpg")]
        (box.network_dir / zst_path.name).write_bytes(zst_path.read_bytes())
        suite.check(net.write_network_metadata(box.network_dir, zst_meta,
                                               zst_sha),
                    "метаданные zstd-архива записаны")
        suite.check(net.network_archive_confirmed(
                box.network_dir, zst_snap, compression="gzip"),
            "zstd-архив подтвержден при текущем COMPRESSION=gzip (ТЗ 6.4 п.2)")
        # 3) Смешанная история: оба архива подтверждены при одном вызове
        for name, expected in ((gz_snap, True), (zst_snap, True)):
            suite.check(net.network_archive_confirmed(box.network_dir, name)
                        is expected,
                        "смешанная история gzip/zstd: {} подтвержден "
                        "(ТЗ 6.4 п.3)".format(name))
        # 4) Поврежденный zstd на шаре — не подтвержден (ТЗ 6.4 п.6)
        saved = (box.network_dir / zst_path.name).read_bytes()
        (box.network_dir / zst_path.name).write_bytes(saved[:-100])
        suite.check(not net.network_archive_confirmed(box.network_dir,
                                                      zst_snap),
                    "поврежденная сетевая копия zstd — снапшот сохранен "
                    "(ТЗ 6.4 п.6)")
        (box.network_dir / zst_path.name).write_bytes(saved)
        # 5) Метаданные указывают на ДРУГОЙ архив — не подтвержден (ТЗ 6.4 п.7)
        foreign = box.network_dir / (zst_path.name + ".json")
        foreign.write_text(net.json_dumps({
            "archive": zst_path.name, "snapshot": zst_snap,
            "sha256": "ab" * 32, "status": "uploaded"}),
            encoding="utf-8")
        suite.check(not net.network_archive_confirmed(box.network_dir,
                                                      zst_snap),
                    "метаданные с чужой суммой не подтверждают копию "
                    "(ТЗ 6.4 п.7)")
        # перезапись конфликтующих метаданных запрещена (проверка защиты)
        suite.check(not net.write_network_metadata(box.network_dir, zst_meta,
                                                   zst_sha),
                    "конфликтующие метаданные не перезаписываются молча")
        # устранение конфликта вручную -> запись проходит
        foreign.unlink()
        suite.check(net.write_network_metadata(box.network_dir, zst_meta,
                                               zst_sha),
                    "корректные метаданные восстановлены после устранения "
                    "конфликта")
        # 6) Индекс метаданных: точное имя архива из JSON
        index = net.network_metadata_index(box.network_dir)
        suite.check(gz_snap in index and zst_snap in index,
                    "network_metadata_index связывает снапшот и точное имя "
                    "архива")
        broken_json = box.network_dir / "broken.json"
        broken_json.write_text("{no", encoding="utf-8")
        index2 = net.network_metadata_index(box.network_dir)
        suite.check(broken_json.name[:-len(".json")] not in index2,
                    "поврежденные метаданные не попадают в индекс")
        broken_json.unlink()

        # 7) Недоверенные имена из JSON не могут подтвердить архив
        # за пределами сетевого каталога (защита retention от path traversal).
        outside_archive = box.root / "outside-retention.gpg"
        outside_archive.write_bytes(b"outside-network-share")
        outside_sha = vr.sha256_file(outside_archive)
        traversal_snapshot = "traversal_2026-05-01_000001_abcdef"
        traversal_meta = box.network_dir / "traversal.json"
        traversal_meta.write_text(net.json_dumps({
            "archive": "../outside-retention.gpg",
            "snapshot": traversal_snapshot,
            "sha256": outside_sha,
            "status": "uploaded",
        }), encoding="utf-8")
        suite.check(
            not net.network_archive_confirmed(
                box.network_dir, traversal_snapshot),
            "JSON с ../ не подтверждает файл за пределами сетевой шары")
        traversal_meta.unlink()

        # Символическая ссылка на внешний файл тоже не подтверждает копию.
        symlink_snapshot = "symlink_2026-05-01_000001_abcdef"
        symlink_archive = box.network_dir / (symlink_snapshot + ".gz.gpg")
        symlink_meta = symlink_archive.with_name(
            symlink_archive.name + ".json")
        try:
            os.symlink(outside_archive, symlink_archive)
        except (OSError, NotImplementedError):
            suite.skip("проверка symlink недоступна в этой среде")
        else:
            symlink_meta.write_text(net.json_dumps({
                "archive": symlink_archive.name,
                "snapshot": symlink_snapshot,
                "sha256": outside_sha,
                "status": "uploaded",
            }), encoding="utf-8")
            suite.check(
                not net.network_archive_confirmed(
                    box.network_dir, symlink_snapshot),
                "symlink на внешний файл не подтверждает сетевую копию")
            symlink_archive.unlink()
            symlink_meta.unlink()

        # 8) Ошибка чтения сетевого архива — консервативный отказ (ТЗ 6.4 п.8)
        real_sha_file = net.sha256_file
        def _unavailable(_p):
            raise OSError("network unavailable")
        net.sha256_file = _unavailable
        try:
            suite.check(not net.network_archive_confirmed(box.network_dir,
                                                          gz_snap),
                        "ошибка чтения сетевого архива — снапшот сохранен "
                        "(ТЗ 6.4 п.8)")
        finally:
            net.sha256_file = real_sha_file

    return suite.summary()


if __name__ == "__main__":
    sys.exit(run_main(main))
