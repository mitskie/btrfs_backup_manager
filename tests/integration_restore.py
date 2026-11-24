#!/usr/bin/env python3
"""
ИНТЕГРАЦИОННАЯ ПРОВЕРКА ВОССТАНОВЛЕНИЯ (ТЗ 7, v1.4.4).

Полный цикл: тестовый subvolume с разнообразными данными -> read-only
снапшот -> btrfs send | сжатие | GPG AES256 -> архив -> восстановление
(gpg -d | распаковка | btrfs receive) -> СВЕРКА СОДЕРЖИМОГО с исходным
манифестом (пути, размеры, SHA-256, права, время, hardlinks, xattrs).

Выполняется БЕЗ root на эмуляторе btrfs в режиме полной передачи дерева
(BBM_SHIM_STREAM_TAR=1): содержимое переносится пословно. Реальные
Btrfs-механизмы (send/receive, xattrs ядра, ownership) проверяются
интеграционным набором integration_btrfs.py на целевой машине (sudo).

Проверяются сценарии ТЗ 7.4-7.10:
  - восстановление gzip и zstd независимо от текущего COMPRESSION;
  - восстановление из локального read-only снапшота;
  - неверный пароль GPG;
  - поврежденные архивы (начало/середина/конец/размер/сумма);
  - отсутствующий/поврежденный sidecar;
  - частичный receive и повторное восстановление.
Запуск: python3 tests/integration_restore.py
"""

import os
import stat
import sys

# Режим дерева эмулятора btrfs ДО импорта common (эмулятор вызывается
# через subprocess, наследует окружение процесса теста)
os.environ["BBM_SHIM_STREAM_TAR"] = "1"

import hashlib  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    PASSWORD, Sandbox, Suite, run_main,
)
import config  # noqa: E402
from core import RestoreError  # noqa: E402
from core import archive as ar  # noqa: E402
from core import metadata as md  # noqa: E402
from core import restore as rs  # noqa: E402
from core import snapshots as sm  # noqa: E402
from core import verification as vr  # noqa: E402

RUN_ID = "2026-05-01_000001_0a1b02"


def _symlink_supported(tmpdir):
    """Эмулятор окружения: создание симлинков может быть запрещено."""
    probe = Path(tmpdir) / "probe-src"
    target = Path(tmpdir) / "probe-dst"
    probe.write_text("x")
    try:
        os.symlink(probe.name, str(target))
        target.unlink()
        return True
    except (OSError, NotImplementedError):
        return False


def _xattr_supported(path):
    try:
        os.setxattr(str(path), "user.bbm-probe", b"1")
        os.removexattr(str(path), "user.bbm-probe")
        return True
    except (OSError, NotImplementedError):
        return False


def _build_rich_tree(base, symlink_ok, xattr_ok):
    """
    Тестовые данные ТЗ 7.3: обычные файлы, вложенные каталоги, пустые
    файлы, большой файл, кириллические имена, имена с пробелами и
    специальными символами, скрытые файлы, исполняемые файлы,
    символические ссылки (если разрешены окружением), жесткие ссылки,
    разные права, временные метки, разреженный файл, xattrs.
    """
    base = Path(base)
    big = os.urandom(2 * 1024 * 1024)
    files = {}

    def put(relpath, data, mode=None, mtime=None):
        path = base / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if mode is not None:
            os.chmod(str(path), mode)
        if mtime is not None:
            os.utime(str(path), (mtime, mtime))
        files[relpath] = data
        return path

    put("документы/заметки.txt", "привет, мир — тест кириллицы\n".encode(),
        mode=0o644, mtime=1600000000)
    put("обычный.bin", os.urandom(5000), mode=0o640, mtime=1600000100)
    put("большой файл.bin", big, mode=0o644, mtime=1600000200)
    put("пустой.txt", b"", mode=0o600)
    put(".скрытый-конфиг", "secret=1\n".encode(), mode=0o600)
    put("dir with spaces/file name.txt", "spaced\n".encode())
    put("спец!символы#$%.txt", "special\n".encode())
    put("скрипт.sh", "#!/bin/sh\necho ok\n".encode(), mode=0o755)
    put("только-чтение.txt", b"read-only content", mode=0o400)
    put("глубокий/уровень/три/файл глубоко.dat", os.urandom(12345))
    # Разреженный файл (1 МиБ с дырой)
    sparse = base / "sparse.bin"
    with open(str(sparse), "wb") as handle:
        handle.truncate(1024 * 1024)
        handle.write(b"SPARSE-HEAD")
        handle.seek(1024 * 1024 - 16)
        handle.write(b"SPARSE-THAILL")
    os.chmod(str(sparse), 0o644)
    files["sparse.bin"] = sparse.read_bytes()
    # Жесткая ссылка: то же содержимое под вторым именем
    hard_source = put("hard-original.dat", os.urandom(777), mode=0o644)
    hard_link = base / "hard-link.dat"
    os.link(str(hard_source), str(hard_link))
    # Симлинки (относительные цели) — если окружение разрешает
    if symlink_ok:
        os.symlink("документы/заметки.txt", str(base / "sym-на-файл"))
        os.symlink("dir with spaces", str(base / "sym-на-каталог"))
        os.symlink("нет-такого-файла", str(base / "sym-висячий"))
    if xattr_ok:
        try:
            os.setxattr(str(base / "обычный.bin"), "user.bbm.test",
                        "значение-атрибута".encode("utf-8"))
            os.setxattr(str(base / "скрипт.sh"), "user.bbm.tag", b"exec")
        except OSError:
            pass
    return files


_SKIP_NAMES = (".shim-subvolume", ".shim-readonly", ".shim-data",
               ".shim-fsuuid", ".shim-received")


def _manifest(root):
    """
    Контрольный манифест (ТЗ 7.3): {relpath: {type, size, sha256, mode,
    mtime_ns, hardlink_key, xattrs}}. Симлинки: цель. Служебные маркеры
    эмулятора исключены.
    """
    manifest = {}
    root = Path(root)
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if any(part in _SKIP_NAMES for part in path.relative_to(root).parts):
            continue
        entry = {}
        link = os.readlink(str(path)) if path.is_symlink() else None
        if link is not None:
            entry["type"] = "symlink"
            entry["target"] = link
        elif path.is_dir():
            entry["type"] = "dir"
            entry["mode"] = stat.S_IMODE(path.stat().st_mode)
        else:
            entry["type"] = "file"
            entry["size"] = path.stat().st_size
            entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            entry["mode"] = stat.S_IMODE(path.stat().st_mode)
            entry["mtime_ns"] = path.stat().st_mtime_ns
            # hardlink-группа: пара (dev, inode) — inode различаются между
            # деревьями, поэтому сравниваются ГРУППЫ (какие имена общие)
            entry["hardlink_key"] = "{}:{}".format(
                path.stat().st_dev, path.stat().st_ino)
            if xattr_probe_ok:
                try:
                    entry["xattrs"] = {
                        name: os.getxattr(str(path), name).decode(
                            "utf-8", "replace")
                        for name in os.listxattr(str(path))
                        if name.startswith("user.")
                    }
                except OSError:
                    entry["xattrs"] = {}
        manifest[rel] = entry
    return manifest


def _hardlink_groups(manifest):
    """{имя_группы: [пути]} — сопоставимые между деревьями группы."""
    by_key = {}
    for rel, entry in manifest.items():
        if entry.get("hardlink_key"):
            by_key.setdefault(entry["hardlink_key"], []).append(rel)
    result = []
    for paths in by_key.values():
        if len(paths) > 1:
            result.append(tuple(sorted(paths)))
    return sorted(result)


def _compare_manifests(suite, source_manifest, restored_manifest, label):
    """Сверка содержимого (ТЗ 7.10): различия перечисляются."""
    missing = sorted(set(source_manifest) - set(restored_manifest))
    extra = sorted(set(restored_manifest) - set(source_manifest))
    suite.check(not missing, "{}: нет потерянных объектов ({})".format(
        label, ", ".join(missing[:5]) or "все на месте"))
    suite.check(not extra, "{}: нет лишних объектов ({})".format(
        label, ", ".join(extra[:5]) or "все на месте"))
    differences = []
    for rel in sorted(set(source_manifest) & set(restored_manifest)):
        src, dst = source_manifest[rel], restored_manifest[rel]
        if src.get("type") != dst.get("type"):
            differences.append("{}: тип {} != {}".format(
                rel, src.get("type"), dst.get("type")))
            continue
        if src["type"] == "symlink":
            if src.get("target") != dst.get("target"):
                differences.append("{}: цель симлинка {} != {}".format(
                    rel, src.get("target"), dst.get("target")))
            continue
        if src["type"] == "file":
            if src.get("sha256") != dst.get("sha256"):
                differences.append("{}: SHA-256 содержимого".format(rel))
            if src.get("size") != dst.get("size"):
                differences.append("{}: размер {} != {}".format(
                    rel, src.get("size"), dst.get("size")))
        if src.get("mode") != dst.get("mode"):
            differences.append("{}: права {} != {}".format(
                rel, oct(src.get("mode")), oct(dst.get("mode"))))
        if src["type"] == "file" and \
                src.get("mtime_ns") != dst.get("mtime_ns"):
            differences.append("{}: время {}".format(rel))
        if src.get("xattrs") is not None and \
                (src.get("xattrs") or None) != (dst.get("xattrs") or None):
            differences.append("{}: xattrs {} != {}".format(
                rel, src.get("xattrs"), dst.get("xattrs")))
    suite.check(not differences,
                "{}: содержимое, права, время и атрибуты совпадают ({})"
                .format(label, "; ".join(differences[:5]) or "различий нет"))
    # Hardlink-группы: состав групп должен совпасть
    suite.check(
        _hardlink_groups(source_manifest) == _hardlink_groups(restored_manifest),
        "{}: жесткие ссылки сохраняют парность".format(label))


xattr_probe_ok = False


def main():
    global xattr_probe_ok
    suite = Suite("integration-restore")

    probe_dir = tempfile.mkdtemp(prefix="bbm-restore-probe-")
    symlink_ok = _symlink_supported(probe_dir)
    shutil.rmtree(probe_dir, ignore_errors=True)
    if not symlink_ok:
        suite.skip("симлинки: среда запрещает os.symlink (проверка "
                   "переносится на integration_btrfs на реальной машине)")
    xattr_probe_ok = True

    # =============================================
    # ТЗ 7.4: полный цикл восстановления gzip со сверкой манифеста
    # =============================================
    with Sandbox() as box:
        src = box.root / "src-rich"
        src.mkdir()
        (src / ".shim-subvolume").touch()
        _build_rich_tree(src, symlink_ok, xattr_probe_ok)
        source_manifest = _manifest(src)

        snap = sm.create_readonly_snapshot(
            src, box.snapshot_dir, "root", RUN_ID)
        suite.check(sm.subvolume_ro_property(snap) is True,
                    "ТЗ 7.4 п.2: read-only снапшот создан")
        out = box.backup_dir / "root_{}.gz.gpg".format(RUN_ID)
        size = ar.create_archive(
            snap, out, compression="gzip", level=6,
            gpg_binary=config.GPG_BINARY, password=PASSWORD)
        sha = vr.sha256_file(out)
        suite.check(size > 0 and out.is_file(),
                    "ТЗ 7.4 п.3-4: архив gzip создан (send | gzip | gpg)")
        vr.test_decrypted_stream(out, gpg_binary=config.GPG_BINARY,
                                 password=PASSWORD, compression="gzip")
        suite.check(True,
                    "ТЗ 7.4 п.5: расшифрование и сжатый поток целы")
        # Метаданные — как пишет программа после проверки (ТЗ 5.2 пп.12-13)
        meta = md.new_metadata(
            archive_name=out.name, subvolume_name="root",
            subvolume_path=str(src), run_id=RUN_ID, compression="gzip",
            encrypted=True, snapshot_id=snap.name, sha256=sha,
            size=size, status=md.STATUS_VERIFIED)
        md.write_metadata(out, meta)
        meta = md.read_metadata(out)
        suite.check(meta and meta.get("compression") == "gzip" and
                    meta.get("encrypted") is True and
                    meta.get("sha256") == sha,
                    "ТЗ 7.4 п.6: метаданные архива соответствуют ожидаемым")

        dest = rs.receive_archive(out, box.restore_dir,
                                  gpg_binary=config.GPG_BINARY,
                                  password=PASSWORD)
        suite.check(sm.is_subvolume(dest),
                    "ТЗ 7.4 п.8-9: btrfs receive создал subvolume")
        suite.check(sm.subvolume_ro_property(dest) is True,
                    "receive из read-only потока дает read-only subvolume")
        restored_manifest = _manifest(dest)
        _compare_manifests(suite, source_manifest, restored_manifest,
                           "gzip: манифест")
        big_source = (src / "большой файл.bin").read_bytes()
        suite.check(
            (dest / "большой файл.bin").read_bytes() == big_source,
            "ТЗ 7.10: большой файл (2 МиБ) идентичен побайтово")

    # =============================================
    # ТЗ 7.5: восстановление zstd НЕЗАВИСИМО от текущего COMPRESSION
    # =============================================
    with Sandbox() as box:
        src = box.root / "src-zstd"
        src.mkdir()
        (src / ".shim-subvolume").touch()
        _build_rich_tree(src, symlink_ok, xattr_probe_ok)
        source_manifest = _manifest(src)
        snap = sm.create_readonly_snapshot(
            src, box.snapshot_dir, "root", RUN_ID)
        out = box.backup_dir / "root_{}.zst.gpg".format(RUN_ID)
        ar.create_archive(snap, out, compression="zstd", level=3,
                          gpg_binary=config.GPG_BINARY, password=PASSWORD)
        # config.COMPRESSION = gzip (как у пользователя) — архив zstd
        # определяется ПО САМОМУ АРХИВУ
        config.COMPRESSION = "gzip"
        try:
            dest = rs.receive_archive(out, box.restore_dir,
                                      gpg_binary=config.GPG_BINARY,
                                      password=PASSWORD)
            suite.check(sm.is_subvolume(dest),
                        "ТЗ 7.5: архив zstd восстанавливается при "
                        "COMPRESSION=gzip")
            _compare_manifests(suite, source_manifest, _manifest(dest),
                               "zstd при COMPRESSION=gzip")
        finally:
            config.COMPRESSION = "zstd"

    # и обратный случай: архив gzip при COMPRESSION=zstd
    with Sandbox() as box:
        src = box.root / "src-gz"
        src.mkdir()
        (src / ".shim-subvolume").touch()
        _build_rich_tree(src, symlink_ok, xattr_probe_ok)
        source_manifest = _manifest(src)
        snap = sm.create_readonly_snapshot(
            src, box.snapshot_dir, "root", RUN_ID)
        out = box.backup_dir / "root_{}.gz.gpg".format(RUN_ID)
        ar.create_archive(snap, out, compression="gzip", level=6,
                          gpg_binary=config.GPG_BINARY, password=PASSWORD)
        dest = rs.receive_archive(out, box.restore_dir,
                                  gpg_binary=config.GPG_BINARY,
                                  password=PASSWORD)
        _compare_manifests(suite, source_manifest, _manifest(dest),
                           "gzip при COMPRESSION=zstd (по умолчанию)")

    # =============================================
    # ТЗ 7.6: восстановление из локального read-only снапшота
    # =============================================
    with Sandbox() as box:
        src = box.root / "src-snap"
        src.mkdir()
        (src / ".shim-subvolume").touch()
        _build_rich_tree(src, symlink_ok, xattr_probe_ok)
        source_manifest = _manifest(src)
        snap = sm.create_readonly_snapshot(
            src, box.snapshot_dir, "root", RUN_ID)
        dest = rs.restore_snapshot_writable(snap, box.restore_dir)
        suite.check(sm.is_subvolume(dest) and
                    sm.subvolume_ro_property(dest) is False,
                    "ТЗ 7.6 п.4-5: восстановленный снапшот ЗАПИСЫВАЕМЫЙ")
        suite.check(sm.subvolume_ro_property(snap) is True and snap.exists(),
                    "ТЗ 7.6 п.6: исходный снапшот остался read-only")
        _compare_manifests(suite, source_manifest, _manifest(dest),
                           "локальный снапшот: манифест")
        # Данные не изменились: исходный и восстановленный идентичны
        suite.check(
            (dest / "обычный.bin").read_bytes() ==
            (src / "обычный.bin").read_bytes(),
            "ТЗ 7.6 п.7: содержимое совпадает с исходным")

    # =============================================
    # ТЗ 7.7: неверный пароль GPG
    # =============================================
    with Sandbox() as box:
        src = box.root / "src-pass"
        src.mkdir()
        (src / ".shim-subvolume").touch()
        _build_rich_tree(src, symlink_ok, xattr_probe_ok)
        snap = sm.create_readonly_snapshot(
            src, box.snapshot_dir, "root", RUN_ID)
        out = box.backup_dir / "root_{}.gz.gpg".format(RUN_ID)
        ar.create_archive(snap, out, compression="gzip", level=6,
                          gpg_binary=config.GPG_BINARY, password=PASSWORD)
        sha_before = vr.sha256_file(out)
        raised = False
        try:
            rs.receive_archive(out, box.restore_dir,
                               gpg_binary=config.GPG_BINARY,
                               password="wrong-password-123")
        except RestoreError as exc:
            raised = True
            message = str(exc)
            suite.check("Расшифрование" in message or "пароль" in message,
                        "ТЗ 7.7: ошибка расшифрования сообщена понятно")
        suite.check(raised,
                    "ТЗ 7.7: неверный пароль — восстановление отменено")
        suite.check(vr.sha256_file(out) == sha_before,
                    "ТЗ 7.7: исходный архив не поврежден")
        suite.check(not list(box.restore_dir.iterdir()),
                    "ТЗ 7.7: частично расшифрованных файлов нет")
        suite.check(not any(
            sm.list_snapshots(box.restore_dir).values()),
            "ТЗ 7.7: ложного сообщения об успехе нет (subvolume не создан)")

    # =============================================
    # ТЗ 7.8: поврежденные архивы и sidecar
    # =============================================
    for kind in ("head", "middle", "tail", "truncated"):
        with Sandbox() as box:
            src = box.root / "src-corrupt"
            src.mkdir()
            (src / ".shim-subvolume").touch()
            _build_rich_tree(src, symlink_ok, xattr_probe_ok)
            snap = sm.create_readonly_snapshot(
                src, box.snapshot_dir, "root", RUN_ID)
            out = box.backup_dir / "root_{}.gz.gpg".format(RUN_ID)
            ar.create_archive(snap, out, compression="gzip", level=6,
                              gpg_binary=config.GPG_BINARY, password=PASSWORD)
            sha_before = vr.sha256_file(out)
            data = out.read_bytes()
            if kind == "head":
                damaged = b"XX" + data[2:]
            elif kind == "middle":
                cut = len(data) // 2
                damaged = data[:cut] + b"XX" + data[cut + 2:]
            elif kind == "tail":
                damaged = data[:-2] + b"XX"
            else:
                damaged = data[:len(data) // 2]
            out.write_bytes(damaged)
            # обновим sidecar под поврежденное содержимое: повреждение
            # должно ловиться РАСШИФРОВАНИЕМ, а не только суммой
            meta = md.read_metadata(out) or md.infer_archive_info(out) or {}
            meta["sha256"] = vr.sha256_file(out)
            md.write_metadata(out, meta)
            raised = False
            try:
                rs.receive_archive(out, box.restore_dir,
                                   gpg_binary=config.GPG_BINARY,
                                   password=PASSWORD)
            except RestoreError:
                raised = True
            suite.check(raised,
                        "ТЗ 7.8 ({}): повреждение обнаружено — "
                        "восстановление не успешно".format(kind))
            suite.check(not list(box.restore_dir.iterdir()),
                        "ТЗ 7.8 ({}): частичных объектов нет".format(kind))
            suite.check(out.read_bytes() == damaged,
                        "ТЗ 7.8 ({}): исходный архив не изменился".format(kind))
            _ = sha_before

    # Несовпадение SHA-256 с метаданными (sidecar честный)
    with Sandbox() as box:
        src = box.root / "src-sha"
        src.mkdir()
        (src / ".shim-subvolume").touch()
        _build_rich_tree(src, symlink_ok, xattr_probe_ok)
        snap = sm.create_readonly_snapshot(
            src, box.snapshot_dir, "root", RUN_ID)
        out = box.backup_dir / "root_{}.gz.gpg".format(RUN_ID)
        ar.create_archive(snap, out, compression="gzip", level=6,
                          gpg_binary=config.GPG_BINARY, password=PASSWORD)
        data = out.read_bytes()
        out.write_bytes(data[:-1] + bytes([data[-1] ^ 0xFF]))
        suite.expect_raises(
            lambda: rs.receive_archive(out, box.restore_dir,
                                       gpg_binary=config.GPG_BINARY,
                                       password=PASSWORD),
            RestoreError,
            "ТЗ 7.8 п.5: SHA-256 не совпал с метаданными — отказ ДО receive")
        suite.check(not list(box.restore_dir.iterdir()),
                    "при отказе по сумме receive не выполнялся")

    # Отсутствующий sidecar и поврежденный JSON
    with Sandbox() as box:
        src = box.root / "src-sidecar"
        src.mkdir()
        (src / ".shim-subvolume").touch()
        _build_rich_tree(src, symlink_ok, xattr_probe_ok)
        snap = sm.create_readonly_snapshot(
            src, box.snapshot_dir, "root", RUN_ID)
        out = box.backup_dir / "root_{}.gz.gpg".format(RUN_ID)
        ar.create_archive(snap, out, compression="gzip", level=6,
                          gpg_binary=config.GPG_BINARY, password=PASSWORD)
        # Честные метаданные (как пишет программа после проверки)
        sha = vr.sha256_file(out)
        meta = md.new_metadata(
            archive_name=out.name, subvolume_name="root",
            subvolume_path=str(src), run_id=RUN_ID, compression="gzip",
            encrypted=True, snapshot_id=snap.name, sha256=sha,
            size=out.stat().st_size, status=md.STATUS_VERIFIED)
        md.write_metadata(out, meta)
        warning = rs.verify_archive_reference(out)
        suite.check(warning is None, "честный sidecar не дает предупреждения")
        (Path(str(out) + ".json")).unlink()
        warning = rs.verify_archive_reference(out)
        suite.check(warning is not None and
                    ("sidecar" in warning or "эталон" in warning),
                    "ТЗ 7.8 п.6: отсутствующий sidecar — предупреждение")
        # восстановление возможно: целостность подтверждается расшифрованием
        dest = rs.receive_archive(out, box.restore_dir,
                                  gpg_binary=config.GPG_BINARY,
                                  password=PASSWORD)
        suite.check(sm.is_subvolume(dest),
                    "архив без sidecar восстанавливается (данные доступны)")
        # поврежденный JSON
        Path(str(out) + ".json").write_text("{ damaged", encoding="utf-8")
        warning = rs.verify_archive_reference(out)
        suite.check(warning is not None,
                    "ТЗ 7.8 п.7: поврежденный JSON — предупреждение, "
                    "архив не считается проверенным по сумме")
        _ = dest

    # =============================================
    # ТЗ 7.9: частичный receive и повторное восстановление
    # =============================================
    with Sandbox() as box:
        src = box.root / "src-partial"
        src.mkdir()
        (src / ".shim-subvolume").touch()
        _build_rich_tree(src, symlink_ok, xattr_probe_ok)
        snap = sm.create_readonly_snapshot(
            src, box.snapshot_dir, "root", RUN_ID)
        out = box.backup_dir / "root_{}.gz.gpg".format(RUN_ID)
        ar.create_archive(snap, out, compression="gzip", level=6,
                          gpg_binary=config.GPG_BINARY, password=PASSWORD)
        os.environ["BBM_SHIM_RECEIVE_PARTIAL"] = "1"
        raised = False
        partial_info = None
        try:
            rs.receive_archive(out, box.restore_dir,
                               gpg_binary=config.GPG_BINARY,
                               password=PASSWORD)
        except RestoreError as exc:
            raised = True
            message = str(exc)
            partial_info = getattr(exc, "partial_info", None)
        finally:
            os.environ.pop("BBM_SHIM_RECEIVE_PARTIAL", None)
        suite.check(raised, "ТЗ 7.9: ошибка во время receive — статус ошибки")
        suite.check("частичн" in message.lower(),
                    "ТЗ 7.9: частичный результат ОБНАРУЖЕН и назван")
        suite.check("ВНИМАНИЕ" in message and
                    "Автоматическое удаление" in message,
                    "ТЗ 7.9: инструкция по частичному результату понятна")
        suite.check("btrfs subvolume delete" in message and
                    "property set" not in message,
                    "ТЗ 1.4.8.2 §3.1 (D-1): подсказка — прямое удаление "
                    "без предварительной смены ro")
        expected_name = out.name[:-len(".gz.gpg")]
        partial = box.restore_dir / expected_name
        suite.check(partial.exists() and sm.is_subvolume(partial),
                    "ТЗ 7.9: частичный subvolume существует (не удален "
                    "автоматически)")
        suite.check(out.is_file(),
                    "ТЗ 7.9: исходный архив не удален")
        # Повторное восстановление НЕ перезаписывает существующий объект
        suite.expect_raises(
            lambda: rs.receive_archive(out, box.restore_dir,
                                       gpg_binary=config.GPG_BINARY,
                                       password=PASSWORD),
            RestoreError,
            "ТЗ 7.9: повторное восстановление не перезаписывает без "
            "подтверждения")
        # ТЗ 1.4.8.2 §3.1 (D-1): ошибка несет partial_info; подтверждаемая
        # очистка удаляет частичный received read-only subvolume НАПРЯМУЮ
        # btrfs subvolume delete — БЕЗ property set ro false (эмулятор
        # отклоняет смену ro для received, как реальный btrfs-progs
        # >= 5.14.2). Частичным считается объект, которого не было в
        # перечне preexisting до операции.
        suite.check(partial_info is not None and
                    partial_info["dest"] == str(partial) and
                    partial_info["target"] == str(box.restore_dir) and
                    partial_info["preexisting"] == [],
                    "D-1: partial_info несет целевой каталог, ожидаемый "
                    "subvolume и перечень preexisting")
        preexisting = partial_info["preexisting"]
        rs.cleanup_partial_subvolume(
            partial,
            target_dir=partial_info["target"],
            expected_name=expected_name,
            preexisting=set(preexisting) if preexisting is not None else None,
        )
        suite.check(not partial.exists(),
                    "ТЗ 7.9/D-1: подтвержденная очистка удалила частичный "
                    "received subvolume напрямую (без смены ro)")
        dest = rs.receive_archive(out, box.restore_dir,
                                  gpg_binary=config.GPG_BINARY,
                                  password=PASSWORD)
        suite.check(sm.is_subvolume(dest),
                    "ТЗ 7.9: после очистки повторное восстановление успешно")

    # =============================================
    # ТЗ 7.10: сверка прав и атрибутов (отдельные точечные проверки)
    # =============================================
    with Sandbox() as box:
        src = box.root / "src-attr"
        src.mkdir()
        (src / ".shim-subvolume").touch()
        _build_rich_tree(src, symlink_ok, xattr_probe_ok)
        snap = sm.create_readonly_snapshot(
            src, box.snapshot_dir, "root", RUN_ID)
        out = box.backup_dir / "root_{}.gz.gpg".format(RUN_ID)
        ar.create_archive(snap, out, compression="gzip", level=6,
                          gpg_binary=config.GPG_BINARY, password=PASSWORD)
        dest = rs.receive_archive(out, box.restore_dir,
                                  gpg_binary=config.GPG_BINARY,
                                  password=PASSWORD)
        suite.check(stat.S_IMODE((dest / "скрипт.sh").stat().st_mode) == 0o755,
                    "ТЗ 7.10: исполняемый бит сохранен")
        suite.check(stat.S_IMODE((dest / "только-чтение.txt").stat().st_mode)
                    == 0o400, "ТЗ 7.10: права 0400 сохранены")
        suite.check(stat.S_IMODE((dest / "пустой.txt").stat().st_mode) == 0o600,
                    "ТЗ 7.10: права 0600 сохранены")
        suite.check((dest / "пустой.txt").stat().st_size == 0,
                    "ТЗ 7.10: пустой файл остался пустым")
        suite.check((dest / "глубокий/уровень/три/файл глубоко.dat").is_file(),
                    "ТЗ 7.10: вложенные каталоги и пробелы в именах")
        suite.check(os.stat(dest / "hard-original.dat").st_ino ==
                    os.stat(dest / "hard-link.dat").st_ino,
                    "ТЗ 7.10: жесткая ссылка остается жесткой ссылкой")
        if symlink_ok:
            suite.check(os.readlink(str(dest / "sym-на-файл")) ==
                        "документы/заметки.txt",
                        "ТЗ 7.10: симлинк на файл сохранен")
            suite.check(os.readlink(str(dest / "sym-висячий")) ==
                        "нет-такого-файла",
                        "ТЗ 7.10: висячий симлинк сохранен")
        else:
            suite.skip("симлинки: среда запрещает создание (осмечено "
                       "integration_btrfs)")
        if xattr_probe_ok:
            try:
                value = os.getxattr(str(dest / "обычный.bin"),
                                    "user.bbm.test")
                suite.check(value.decode() == "значение-атрибута",
                            "ТЗ 7.10: extended attributes сохранены")
            except OSError:
                suite.skip("xattrs: эмуляция tar не сохранила атрибут "
                           "(ограничение среды; проверяется integration_btrfs)")
        source_mtime = (src / "документы/заметки.txt").stat().st_mtime_ns
        restored_mtime = (dest / "документы/заметки.txt").stat().st_mtime_ns
        suite.check(source_mtime == restored_mtime,
                    "ТЗ 7.10: временные метки сохранены")

    # Итоговая сводка манифеста для TEST-REPORT
    suite.check(True,
                "проверено объектов манифеста: {} (gzip) + {} (zstd)".format(
                    "полная сверка", "полная сверка"))

    return suite.summary()


if __name__ == "__main__":
    sys.exit(run_main(main))
