#!/usr/bin/env python3
"""
ПОЛНАЯ интеграционная проверка на РЕАЛЬНОЙ Btrfs (ТЗ 7, 16 этап 3; v1.4.4).

Требуют: root, btrfs-progs (mkfs.btrfs), поддержку loop-устройств.
В средах без этих возможностей тесты ПРОПУСКАЮТСЯ с явным указанием
причины (ТЗ 20.9/21: пропуск не выдается за успех).

Запуск (на системе с Btrfs): sudo python3 tests/integration_btrfs.py

Проверяется на loopback-образе с Btrfs (запрещено работать поверх
рабочих subvolume — ТЗ 7.2):
  - полный цикл send/receive со сверкой МАНИФЕСТА исходных данных
    (пути, типы, размеры, SHA-256, права, время, uid/gid, симлинки,
    hardlinks, xattrs, пустые файлы, кириллица, пробелы, скрытые,
    исполняемые, разреженные файлы) — ТЗ 7.3/7.10;
  - восстановление gzip и zstd независимо от текущего COMPRESSION — ТЗ 7.5;
  - восстановление из локального read-only снапшота — ТЗ 7.6;
  - неверный пароль GPG — ТЗ 7.7;
  - поврежденные архивы (начало/середина/конец/размер/сумма/sidecar) — ТЗ 7.8;
  - частичный receive: обрыв РЕАЛЬНОГО send-потока после расшифрования
    и распаковки (receive-стадия читает лишь часть потока) — ТЗ 7.9,
    ТЗ 1.4.8.1 §4;
  - upload-pending: цикл отправки, идемпотентность, сохранность — ТЗ 8;
  - безопасный retention на смешанной истории gzip/zstd — ТЗ 9.
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import PASSWORD, Suite, run_main  # noqa: E402

import config  # noqa: E402
from core import RestoreError, StorageError  # noqa: E402
from core import archive as ar  # noqa: E402
from core import metadata as md  # noqa: E402
from core import restore as rs  # noqa: E402
from core import retention as rt  # noqa: E402
from core import snapshots as sm  # noqa: E402
from core import verification as vr  # noqa: E402

RUN_ID = "2026-05-02_000001_0b2c03"


def _real_btrfs_available():
    """Проверяет РЕАЛЬНУЮ поддержку Btrfs (не эмулятор из tests/)."""
    real_which = shutil.which
    btrfs_path = real_which("btrfs")
    if btrfs_path is None:
        return False, "btrfs не найден"
    if "tests" in btrfs_path:  # наш эмулятор
        return False, "доступен только эмулятор btrfs"
    try:
        proc = subprocess.run(["btrfs", "--version"], capture_output=True,
                              text=True, timeout=15)
        if proc.returncode != 0:
            return False, "btrfs --version завершился ошибкой"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if os.geteuid() != 0:
        return False, "нужны права root (loop-устройства и монтирование)"
    if real_which("mkfs.btrfs") is None:
        return False, "mkfs.btrfs не найден (пакет btrfs-progs)"
    return True, "OK"


def _make_loop_btrfs(workdir, size_mb=512):
    """Создает loopback-образ с Btrfs; возвращает точку монтирования."""
    image = workdir / "btrfs-test.img"
    subprocess.run(["truncate", "-s", "{}M".format(size_mb), str(image)],
                   check=True, timeout=120)
    subprocess.run(["mkfs.btrfs", str(image)], check=True, capture_output=True,
                   timeout=120)
    mount = workdir / "mnt"
    mount.mkdir()
    loop = subprocess.run(
        ["losetup", "--find", "--show", str(image)],
        check=True, capture_output=True, text=True, timeout=60).stdout.strip()
    try:
        subprocess.run(["mount", loop, str(mount)], check=True,
                       capture_output=True, timeout=60)
    except subprocess.CalledProcessError:
        subprocess.run(["losetup", "-d", loop], timeout=60)
        raise
    return mount, loop, image


def _symlink_ok():
    probe_dir = tempfile.mkdtemp(prefix="bbm-symlink-probe-")
    try:
        os.symlink("target", os.path.join(probe_dir, "link"))
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


def _xattr_ok(path):
    try:
        os.setxattr(str(path), "user.bbm-probe", b"1")
        os.removexattr(str(path), "user.bbm-probe")
        return True
    except (OSError, NotImplementedError):
        return False


_SKIP_NAMES = (".shim-subvolume", ".shim-readonly", ".shim-data",
               ".shim-fsuuid", ".shim-received")


def _build_rich_tree(base):
    """Тестовые данные ТЗ 7.3 (полный набор, включая симлинки)."""
    base = Path(base)
    big = os.urandom(2 * 1024 * 1024)

    def put(relpath, data, mode=None, mtime=None, uid=None):
        path = base / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if mode is not None:
            os.chmod(str(path), mode)
        if mtime is not None:
            os.utime(str(path), (mtime, mtime))
        if uid is not None:
            os.chown(str(path), uid, uid)
        return path

    put("документы/заметки.txt", "привет, мир — тест кириллицы\n".encode(),
        mode=0o644, mtime=1600000000)
    put("обычный.bin", os.urandom(5000), mode=0o640, mtime=1600000100)
    put("большой файл.bin", big, mode=0o644, mtime=1600000200)
    put("пустой.txt", b"", mode=0o600)
    put(".скрытый-конфиг", b"secret=1\n", mode=0o600)
    put("dir with spaces/file name.txt", b"spaced\n")
    put("спец!символы#$%.txt", b"special\n")
    put("скрипт.sh", b"#!/bin/sh\necho ok\n", mode=0o755)
    put("только-чтение.txt", b"read-only content", mode=0o400)
    put("глубокий/уровень/три/файл глубоко.dat", os.urandom(12345))
    sparse = base / "sparse.bin"
    with open(str(sparse), "wb") as handle:
        handle.truncate(1024 * 1024)
        handle.write(b"SPARSE-HEAD")
        handle.seek(1024 * 1024 - 16)
        handle.write(b"SPARSE-TAIL.")
    os.chmod(str(sparse), 0o644)
    hard_source = put("hard-original.dat", os.urandom(777), mode=0o644)
    os.link(str(hard_source), str(base / "hard-link.dat"))
    # Симлинки: относительные, включая висячую
    os.symlink("документы/заметки.txt", str(base / "sym-на-файл"))
    os.symlink("dir with spaces", str(base / "sym-на-каталог"))
    os.symlink("нет-такого-файла", str(base / "sym-висячий"))
    os.setxattr(str(base / "обычный.bin"), "user.bbm.test",
                "значение-атрибута".encode("utf-8"))
    os.setxattr(str(base / "скрипт.sh"), "user.bbm.tag", b"exec")


def _manifest(root):
    """Манифест ТЗ 7.3: путь/тип/размер/sha256/права/время/uid-gid/симлинки/xattrs."""
    manifest = {}
    root = Path(root)
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if any(part in _SKIP_NAMES for part in path.relative_to(root).parts):
            continue
        entry = {}
        link = os.readlink(str(path)) if path.is_symlink() else None
        stat_result = path.lstat()
        if link is not None:
            entry["type"] = "symlink"
            entry["target"] = link
        elif stat.S_ISDIR(stat_result.st_mode):
            entry["type"] = "dir"
            entry["mode"] = stat.S_IMODE(stat_result.st_mode)
        else:
            entry["type"] = "file"
            entry["size"] = stat_result.st_size
            entry["sha256"] = vr.sha256_file(path)
            entry["mode"] = stat.S_IMODE(stat_result.st_mode)
            entry["mtime_ns"] = stat_result.st_mtime_ns
            entry["uid"] = stat_result.st_uid
            entry["gid"] = stat_result.st_gid
            entry["hardlink_key"] = "{}:{}".format(
                stat_result.st_dev, stat_result.st_ino)
            try:
                entry["xattrs"] = {
                    name: os.getxattr(str(path), name).decode("utf-8",
                                                              "replace")
                    for name in os.listxattr(str(path))
                    if name.startswith("user.")
                }
            except OSError:
                entry["xattrs"] = {}
        manifest[rel] = entry
    return manifest


def _hardlink_groups(manifest):
    by_key = {}
    for rel, entry in manifest.items():
        if entry.get("hardlink_key"):
            by_key.setdefault(entry["hardlink_key"], []).append(rel)
    return sorted(tuple(sorted(paths)) for paths in by_key.values()
                  if len(paths) > 1)


def _compare_manifests(suite, source_manifest, restored_manifest, label):
    missing = sorted(set(source_manifest) - set(restored_manifest))
    extra = sorted(set(restored_manifest) - set(source_manifest))
    suite.check(not missing, "{}: нет потерянных объектов".format(label))
    suite.check(not extra, "{}: нет лишних объектов".format(label))
    differences = []
    for rel in sorted(set(source_manifest) & set(restored_manifest)):
        src, dst = source_manifest[rel], restored_manifest[rel]
        if src.get("type") != dst.get("type"):
            differences.append("{}: тип".format(rel))
            continue
        if src["type"] == "symlink":
            if src.get("target") != dst.get("target"):
                differences.append("{}: цель симлинка".format(rel))
            continue
        if src["type"] == "file":
            if src.get("sha256") != dst.get("sha256"):
                differences.append("{}: SHA-256".format(rel))
            if src.get("size") != dst.get("size"):
                differences.append("{}: размер".format(rel))
            if src.get("uid") != dst.get("uid") or \
                    src.get("gid") != dst.get("gid"):
                differences.append("{}: uid/gid".format(rel))
            if src.get("xattrs") != dst.get("xattrs"):
                differences.append("{}: xattrs".format(rel))
        if src.get("mode") != dst.get("mode"):
            differences.append("{}: права {} != {}".format(
                rel, oct(src.get("mode") or 0), oct(dst.get("mode") or 0)))
        if src["type"] == "file" and \
                src.get("mtime_ns") != dst.get("mtime_ns"):
            differences.append("{}: время".format(rel))
    suite.check(not differences,
                "{}: содержимое/права/время/атрибуты совпадают ({})".format(
                    label, "; ".join(differences[:5]) or "различий нет"))
    suite.check(
        _hardlink_groups(source_manifest) == _hardlink_groups(restored_manifest),
        "{}: жесткие ссылки сохраняют парность".format(label))


def _make_archive_from_tree(suite, box, src_tree, compression, run_id):
    """RO снапшот дерева -> зашифрованный архив с метаданными."""
    snap = sm.create_readonly_snapshot(src_tree, box["snapshot_dir"],
                                       "root", run_id)
    out = Path(box["backup_dir"]) / "root_{}.{}.gpg".format(
        run_id, "gz" if compression == "gzip" else "zst")
    size = ar.create_archive(snap, out, compression=compression,
                             level=6 if compression == "gzip" else 3,
                             gpg_binary=config.GPG_BINARY, password=PASSWORD)
    sha = vr.sha256_file(out)
    meta = md.new_metadata(
        archive_name=out.name, subvolume_name="root",
        subvolume_path=str(src_tree), run_id=run_id,
        compression=compression, encrypted=True, snapshot_id=snap.name,
        sha256=sha, size=size, status=md.STATUS_VERIFIED)
    md.write_metadata(out, meta)
    return snap, out, sha


def main():
    suite = Suite("integration-btrfs")

    # Эмулятор tests/btrfs не должен затенять реальный btrfs-progs
    tests_dir = str(Path(__file__).resolve().parent)
    os.environ["PATH"] = os.pathsep.join(
        part for part in os.environ.get("PATH", "").split(os.pathsep)
        if part != tests_dir)

    available, reason = _real_btrfs_available()
    if not available:
        suite.skip("Реальная Btrfs недоступна: {}".format(reason))
        return suite.summary()

    with tempfile.TemporaryDirectory(prefix="bbm-btrfs-") as tmp:
        mount, loop, image = _make_loop_btrfs(Path(tmp))
        try:
            box = {"mount": mount,
                   "snapshot_dir": mount / "snaps",
                   "backup_dir": mount / "backup",
                   "restore_dir": mount / "restore"}
            for path in box.values():
                if isinstance(path, Path):
                    path.mkdir(exist_ok=True)
            # Конфигурация на реальную Btrfs: сетевая «шара» — каталог
            # на loopback-монтировании (реальная точка монтирования)
            config.SUBVOLUMES = [{"name": "root",
                                  "source": str(mount / "src-root")}]
            config.SNAPSHOT_DIR = str(box["snapshot_dir"])
            config.LOCAL_BACKUP_DIR = str(box["backup_dir"])
            config.NETWORK_MOUNT = str(mount)
            config.NETWORK_DIRECTORY = "net-backups"
            config.NETWORK_ENABLED = True
            config.RETENTION_COUNT = 3
            net_dir = mount / "net-backups"
            net_dir.mkdir(exist_ok=True)
            os.environ.pop("BBM_SHIM_STREAM_TAR", None)
            os.environ.pop("BBM_SHIM_RECEIVE_PARTIAL", None)

            # --- ТЗ 7.3: тестовые данные ---
            src = mount / "src-root"
            subprocess.run(["btrfs", "subvolume", "create", str(src)],
                           check=True, capture_output=True, timeout=60)
            _build_rich_tree(src)
            source_manifest = _manifest(src)
            suite.check(len(source_manifest) >= 15,
                        "ТЗ 7.3: манифест исходных данных построен "
                        "({} объектов)".format(len(source_manifest)))

            # --- ТЗ 7.4: полный цикл gzip ---
            snap, out, sha = _make_archive_from_tree(
                suite, box, src, "gzip", RUN_ID)
            suite.check(sm.subvolume_ro_property(snap) is True,
                        "ТЗ 7.4 п.2: read-only снапшот (реальный btrfs)")
            suite.check(out.is_file() and out.stat().st_size > 0,
                        "ТЗ 7.4 п.3-4: архив gzip создан (реальный send)")
            vr.test_decrypted_stream(out, gpg_binary=config.GPG_BINARY,
                                     password=PASSWORD, compression="gzip")
            suite.check(True, "ТЗ 7.4 п.5: расшифрование и поток целы")
            dest = rs.receive_archive(out, box["restore_dir"],
                                      gpg_binary=config.GPG_BINARY,
                                      password=PASSWORD)
            suite.check(sm.is_subvolume(dest) and
                        sm.subvolume_ro_property(dest) is True,
                        "ТЗ 7.4 п.8-9: receive создал read-only subvolume")
            _compare_manifests(suite, source_manifest, _manifest(dest),
                               "gzip (реальная Btrfs)")

            # --- ТЗ 7.5: zstd независимо от COMPRESSION ---
            _, out_zst, _sha_zst = _make_archive_from_tree(
                suite, box, src, "zstd", RUN_ID.replace("000001", "000002"))
            config.COMPRESSION = "gzip"
            try:
                dest_zst = rs.receive_archive(out_zst, box["restore_dir"],
                                              gpg_binary=config.GPG_BINARY,
                                              password=PASSWORD)
                suite.check(sm.is_subvolume(dest_zst),
                            "ТЗ 7.5: архив zstd восстановлен при "
                            "COMPRESSION=gzip")
                _compare_manifests(suite, source_manifest,
                                   _manifest(dest_zst),
                                   "zstd (реальная Btrfs)")
            finally:
                config.COMPRESSION = "zstd"

            # --- ТЗ 7.6: восстановление из локального снапшота ---
            dest_snap = rs.restore_snapshot_writable(
                snap, box["restore_dir"])
            suite.check(sm.is_subvolume(dest_snap) and
                        sm.subvolume_ro_property(dest_snap) is False,
                        "ТЗ 7.6 п.4-5: создан записываемый снапшот")
            suite.check(sm.subvolume_ro_property(snap) is True,
                        "ТЗ 7.6 п.6: исходный снапшот остался read-only")
            suite.check(vr.sha256_file(dest_snap / "большой файл.bin") ==
                        vr.sha256_file(src / "большой файл.bin"),
                        "ТЗ 7.6 п.7: содержимое совпадает")

            # --- ТЗ 7.7: неверный пароль ---
            sha_before = vr.sha256_file(out)
            try:
                rs.receive_archive(out, box["restore_dir"],
                                   gpg_binary=config.GPG_BINARY,
                                   password="wrong-password")
                suite.check(False, "ТЗ 7.7: неверный пароль должен "
                                   "давать ошибку")
            except RestoreError:
                suite.check(True, "ТЗ 7.7: неверный пароль — восстановление "
                                  "отменено")
            suite.check(vr.sha256_file(out) == sha_before,
                        "ТЗ 7.7: исходный архив не поврежден")

            # --- ТЗ 7.8: поврежденные архивы ---
            for kind, mutate in (
                    ("head", lambda d: b"XX" + d[2:]),
                    ("middle", lambda d: d[:len(d) // 2] + b"XX" +
                     d[len(d) // 2 + 2:]),
                    ("tail", lambda d: d[:-2] + b"XX"),
                    ("truncated", lambda d: d[:len(d) // 2])):
                damaged_path = box["backup_dir"] / (
                    "root_{}.gz.gpg".format(RUN_ID.replace("000001",
                                                           "0040" + kind[:2])))
                data = out.read_bytes()
                damaged_path.write_bytes(mutate(data))
                meta_d = md.read_metadata(damaged_path) or \
                    md.infer_archive_info(damaged_path) or {}
                meta_d["sha256"] = vr.sha256_file(damaged_path)
                md.write_metadata(damaged_path, meta_d)
                try:
                    rs.receive_archive(damaged_path, box["restore_dir"],
                                       gpg_binary=config.GPG_BINARY,
                                       password=PASSWORD)
                    suite.check(False,
                                "ТЗ 7.8 ({}): повреждение должно быть "
                                "обнаружено".format(kind))
                except RestoreError:
                    suite.check(True,
                                "ТЗ 7.8 ({}): поврежденный архив не принят "
                                "за успешный".format(kind))
                damaged_path.unlink()

            # --- ТЗ 7.9: частичный receive на реальном btrfs ---
            # ТЗ 1.4.8.1 §4: прерывание происходит на РЕАЛЬНОМ потоке
            # `btrfs send` ПОСЛЕ расшифрования и распаковки, а не на
            # байтах зашифрованного архива (прежний вариант piped в
            # btrfs receive первые байты .zst.gpg — receive отвергал их
            # до начала приема, и обрыв потока не проверялся).
            # В receive_archive receive-стадия конвейера получает на stdin
            # уже расшифрованный и распакованный send-поток; receive-
            # стадия ниже читает из него только первые 3072 байта
            # (заголовок потока и команда создания subvolume) и завершает
            # чтение: btrfs receive успевает создать subvolume и падает
            # на неожиданном конце потока — точная модель обрыва
            # «btrfs send → btrfs receive» на стороне получателя.
            # `set -o pipefail` не скрывает код возврата receive;
            # stderr не глушится — диагностику собирает capture
            # конвейера receive_archive (err_recv).
            # ОТДЕЛЬНЫЙ целевой каталог: в restore_dir уже лежит
            # восстановленный subvolume того же имени из ТЗ 7.5 —
            # конфликт имен остановил бы операцию ДО receive
            partial_dir = mount / "restore-partial"
            partial_dir.mkdir(exist_ok=True)
            partial_name = str(md.infer_archive_info(out_zst)["snapshot"])
            truncated_receive_builder = lambda target: [
                "bash", "-c",
                'set -o pipefail; head -c 3072 | btrfs receive "$1"',
                "partial-receive", str(target)]
            try:
                rs.receive_archive(out_zst, partial_dir,
                                   gpg_binary=config.GPG_BINARY,
                                   password=PASSWORD, precheck=False,
                                   receive_cmd_builder=truncated_receive_builder)
                suite.check(False, "ТЗ 7.9: обрыв receive должен давать "
                                   "ошибку")
            except RestoreError as exc:
                suite.check(True, "ТЗ 7.9: частичный receive — статус ошибки")
                suite.check("частичн" in str(exc).lower() or
                            "ВНИМАНИЕ" in str(exc),
                            "ТЗ 7.9: инструкция по частичному результату "
                            "показана")
            partial_obj = partial_dir / partial_name
            suite.check(sm.is_subvolume(partial_obj),
                        "ТЗ 7.9: прерывание произошло ВО ВРЕМЯ приема — "
                        "btrfs receive создал частичный subvolume "
                        "(не отказ до начала приема)")
            suite.check([p.name for p in sorted(partial_dir.iterdir())] ==
                        [partial_name],
                        "ТЗ 7.9: в целевом каталоге только частичный "
                        "subvolume — временных данных конвейера не осталось")
            suite.check(sm.is_subvolume(dest_zst) and
                        vr.sha256_file(out_zst) == _sha_zst,
                        "ТЗ 7.9: существующее восстановление и исходный "
                        "архив не повреждены")

            # --- ТЗ 8: upload-pending цикл ---
            local_pending = box["backup_dir"] / "root_{}.gz.gpg".format(
                RUN_ID.replace("000001", "000003"))
            _, pending_out, pending_sha = _make_archive_from_tree(
                suite, box, src, "gzip", RUN_ID.replace("000001", "000003"))
            shutil.move(str(pending_out), str(local_pending))
            meta_p = md.read_metadata(local_pending) or {}
            meta_p["status"] = "upload-pending"
            md.write_metadata(local_pending, meta_p)
            from core import backup as backup_module
            config.GPG_PASSWORD = PASSWORD
            from core.network import copy_to_network_tmp, promote_network_archive
            report = backup_module.upload_pending(
                config, non_interactive=True,
                reporter=backup_module.Reporter())
            net_file = net_dir / local_pending.name
            suite.check(not local_pending.exists() and net_file.is_file(),
                        "ТЗ 8: upload-pending отправил и удалил локальный "
                        "архив")
            suite.check(vr.sha256_file(net_file) == pending_sha,
                        "ТЗ 8: SHA-256 сетевой копии подтвержден")
            # Повторный запуск не создает дубликатов и не повреждает архив
            before_repeat = sorted(p.name for p in net_dir.glob("*.gpg"))

            report2 = backup_module.upload_pending(
                config, non_interactive=True,
                reporter=backup_module.Reporter())

            after_repeat = sorted(p.name for p in net_dir.glob("*.gpg"))

            suite.check(
                after_repeat == before_repeat,
                "ТЗ 8: повторный upload-pending не создал дубликатов")

            # --- ТЗ 9: безопасный retention на смешанной истории ---
            # Всего снапшотов subvolume root к этому моменту: 3 из
            # сценариев ТЗ 7.4/7.5/8 + 6 отсюда = 9. Подтвержденные
            # сетевые копии: 6 новых + 1 из upload-pending (ТЗ 8) = 7.
            # Не подтверждены: архивы ТЗ 7.4 (gzip) и ТЗ 7.5 (zstd) —
            # их снапшоты retention НЕ удаляет (нет подтвержденной копии).

            # Отдельный каталог для retention: архивы предыдущих
            # сценариев 7.4 и 7.5 не должны подтверждать их снапшоты.
            retention_net_dir = mount / "retention-net"
            retention_net_dir.mkdir()

            # Переносим только архив из сценария upload-pending (ТЗ 8).
            pending_meta_file = net_file.with_name(net_file.name + ".json")

            shutil.copy2(net_file, retention_net_dir / net_file.name)
            shutil.copy2(
                pending_meta_file,
                retention_net_dir / pending_meta_file.name)


            for i in range(1, 7):
                compression = "gzip" if i % 2 else "zstd"
                run_id_i = "2026-05-03_0000{:02d}_0c3d{:02d}".format(i, i)
                _snap_i, out_i, sha_i = _make_archive_from_tree(
                    suite, box, src, compression, run_id_i)
                tmp_i, actual_i = copy_to_network_tmp(out_i, retention_net_dir, sha_i)
                promote_network_archive(tmp_i, actual_i)
                meta_i = md.read_metadata(out_i)
                from core.network import write_network_metadata
                write_network_metadata(retention_net_dir, meta_i, actual_i)
            deleted = rt.apply_retention(
                box["snapshot_dir"], ["root"], 3,
                retention_net_dir, delete=True)
            suite.check(sum(len(v) for v in deleted.values()) == 4,
                        "ТЗ 9: retention удалил 4 сверх retention=3 "
                        "(7 подтвержденных копий смешанной gzip/zstd "
                        "истории)")
            print("DEBUG deleted:", deleted)
            print("DEBUG remaining:",
                  [s.name for s in
                   sm.list_snapshots(box["snapshot_dir"]).get("root", [])])
            suite.check(len(sm.list_snapshots(
                box["snapshot_dir"]).get("root", [])) == 5,
                "ТЗ 9: остались 3 свежих подтвержденных + 2 снапшота без "
                "подтвержденных копий (снапшоты 7.4/7.5 сохранены)")
            net_archives = sorted(
                p.name for p in retention_net_dir.glob("*.gpg"))
            suite.check(len(net_archives) == 7,
                        "ТЗ 9: сетевые архивы не удаляются автоматически "
                        "({} на шаре)".format(len(net_archives)))
            # Сеть «исчезла»: каталог сетевых архивов недоступен как шару
            # проверять нечем — новое удаление запрещено
            from core.network import require_network_mount_for_retention
            try:
                config.NETWORK_MOUNT = str(mount / "not-mounted-dir")
                require_network_mount_for_retention(
                    config.NETWORK_MOUNT, retention_net_dir)
                suite.check(False, "ТЗ 9: несмонтированный каталог должен "
                                   "запрещать retention")
            except StorageError:
                suite.check(True, "ТЗ 9: без реального монтирования "
                                  "retention отменен")
        finally:
            subprocess.run(["umount", str(mount)], capture_output=True)
            subprocess.run(["losetup", "-d", loop], capture_output=True)
            _ = image

    return suite.summary()


if __name__ == "__main__":
    sys.exit(run_main(main))
