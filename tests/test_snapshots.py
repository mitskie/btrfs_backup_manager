"""
Тесты снапшотов (ТЗ 5.2, 9, 18) на эмуляторе btrfs.
Запуск: python3 tests/test_snapshots.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Sandbox, Suite, run_main  # noqa: E402
from core import SnapshotError  # noqa: E402
from core import snapshots as sm  # noqa: E402


def main():
    suite = Suite("snapshots")

    # Шаблоны имен всех поколений (ТЗ 19)
    for name, subvol, ts in (
            ("root_2026-11-06_120000_a1b2c3", "root", "2026-11-06_120000"),
            ("home_2026-03-01_120000_123456", "home", "2026-03-01_120000"),
            ("var_2026-03-01_120000", "var", "2026-03-01_120000")):
        match = sm.SNAPSHOT_NAME_RE.match(name)
        suite.check(match is not None and match.group("name") == subvol,
                    "шаблон имени распознает {}".format(name))
    suite.check(sm.SNAPSHOT_NAME_RE.match("not-a-snapshot") is None,
                "посторонние каталоги не считаются снапшотами")

    with Sandbox() as box:
        src = box.make_subvolume("src-root", b"payload-1" * 100)

        # Создание read-only снапшота
        snap = sm.create_readonly_snapshot(src, box.snapshot_dir, "root",
                                           "2026-11-06_120000_abc123")
        suite.check(snap.name == "root_2026-11-06_120000_abc123",
                    "снапшот создан с именем <subvol>_<run id>")
        suite.check(sm.subvolume_ro_property(snap) is True,
                    "снапшот read-only (btrfs property get ro=true)")

        # Защита от перезаписи (ТЗ 5)
        suite.expect_raises(
            lambda: sm.create_readonly_snapshot(
                src, box.snapshot_dir, "root", "2026-11-06_120000_abc123"),
            SnapshotError, "перезапись существующего имени запрещена")

        # Источник не subvolume
        plain = box.root / "plain"
        plain.mkdir()
        suite.expect_raises(
            lambda: sm.create_readonly_snapshot(
                plain, box.snapshot_dir, "plain", "2026-11-06_120001_abc124"),
            SnapshotError, "источник не Btrfs subvolume -> ошибка")
        suite.expect_raises(
            lambda: sm.create_readonly_snapshot(
                box.root / "missing", box.snapshot_dir, "x",
                "2026-11-06_120002_abc125"),
            SnapshotError, "несуществующий источник -> ошибка")

        # Список снапшотов и сортировка
        box.make_subvolume("src-root-2", b"x")
        sm.create_readonly_snapshot(src, box.snapshot_dir, "root",
                                    "2026-11-06_120003_abc126")
        groups = sm.list_snapshots(box.snapshot_dir, {"root"})
        suite.check(len(groups.get("root", [])) == 2,
                    "list_snapshots находит снапшоты subvolume")
        keys = [s.timestamp for s in groups["root"]]
        suite.check(keys == sorted(keys, reverse=True),
                    "снапшоты отсортированы по времени по убыванию")
        suite.check(all("home" not in g for g in groups),
                    "чужие subvolume в список не попадают")

        # select_for_deletion
        ordered = groups["root"]
        suite.check(sm.select_for_deletion(ordered, 5) == [],
                    "retention 5: меньше 5 снапшотов — удалять нечего")
        suite.check(sm.select_for_deletion(ordered, 1) == ordered[1:],
                    "retention 1: старшие снапшоты — кандидаты")

        # Удаление: защиты (ТЗ 18)
        suite.expect_raises(
            lambda: sm.delete_snapshot(plain, box.snapshot_dir),
            SnapshotError, "удаление НЕ-subvolume запрещено")
        suite.expect_raises(
            lambda: sm.delete_snapshot(box.root / "root_2026-01-01_000000_x",
                                       box.snapshot_dir),
            SnapshotError, "удаление вне каталога снапшотов запрещено")
        suite.expect_raises(
            lambda: sm.delete_snapshot(snap, box.root / "elsewhere"),
            SnapshotError, "снапшот вне своего каталога не удаляется")

        # Успешное удаление read-only снапшота
        snap2 = groups["root"][1].path
        sm.delete_snapshot(snap2, box.snapshot_dir)
        suite.check(not snap2.exists(), "read-only снапшот удален")

        # --- ТЗ 4.4 (исправление №2): fail-closed при удалении ---
        # ro=False (доступен на запись) — удаление ЗАПРЕЩЕНО
        writable = box.make_subvolume(
            "w_2026-11-06_100000_a1b2c1", b"writable-target")
        import shutil as _shutil
        writable_in_snaps = box.snapshot_dir / writable.name
        _shutil.move(str(writable), str(writable_in_snaps))
        suite.expect_raises(
            lambda: sm.delete_snapshot(writable_in_snaps, box.snapshot_dir),
            SnapshotError, "ro=False: удаляемый на запись объект запрещен")
        suite.check(writable_in_snaps.exists(),
                    "ro=False: объект не удален")
        # ro=None (свойство определить не удалось) — удаление ЗАПРЕЩЕНО
        _real_ro = sm.subvolume_ro_property
        sm.subvolume_ro_property = lambda _p: None
        try:
            suite.expect_raises(
                lambda: sm.delete_snapshot(writable_in_snaps, box.snapshot_dir),
                SnapshotError, "ro=None: неизвестное состояние — удаление "
                "запрещено (fail-closed)")
        finally:
            sm.subvolume_ro_property = _real_ro
        suite.check(writable_in_snaps.exists(),
                    "ro=None: объект не удален")
        # Ошибка получения свойства — операция запрещена
        def _boom(_p):
            raise RuntimeError("simulated property failure")
        sm.subvolume_ro_property = _boom
        try:
            suite.expect_raises(
                lambda: sm.delete_snapshot(writable_in_snaps, box.snapshot_dir),
                SnapshotError, "ошибка btrfs property get — удаление запрещено")
        finally:
            sm.subvolume_ro_property = _real_ro
        suite.check(writable_in_snaps.exists(),
                    "ошибка свойства: объект не удален")
        # Исходный рабочий subvolume (protected_paths) — удаление запрещено
        suite.expect_raises(
            lambda: sm.delete_snapshot(
                writable_in_snaps, box.snapshot_dir,
                protected_paths=[str(writable_in_snaps)]),
            SnapshotError, "protected_paths: исходный рабочий subvolume "
            "не удаляется")
        # Неразрешимый защищенный путь — консервативный отказ (fail-closed)
        suite.expect_raises(
            lambda: sm.delete_snapshot(
                writable_in_snaps, box.snapshot_dir,
                protected_paths=["\x00-invalid-path-\x00"]),
            SnapshotError, "protected_paths: неразрешимый путь — отказ")
        suite.check(writable_in_snaps.exists(),
                    "protected_paths: объект не удален")
        # Вложенный объект (не прямой потомок каталога снапшотов) — отказ
        nested_dir = box.snapshot_dir / "nested-dir"
        nested_dir.mkdir(exist_ok=True)
        nested = box.make_subvolume("root_2026-11-06_200000_a1b2c2", b"n")
        _shutil.move(str(nested), str(nested_dir / nested.name))
        suite.expect_raises(
            lambda: sm.delete_snapshot(nested_dir / nested.name,
                                       box.snapshot_dir),
            SnapshotError, "вложенный объект (не прямой потомок) — отказ")
        suite.check((nested_dir / nested.name).exists(),
                    "вложенный объект не удален")
        # Подмена состояния между подтверждением копии и удалением
        # (TOCTOU): подтверждение копии в retention НЕ заменяет
        # проверку read-only непосредственно перед удалением
        from core.retention import apply_retention as _apply_retention
        try:
            _apply_retention(
                box.snapshot_dir, ["w"], 0, box.network_dir, delete=True,
                confirmed_fn=lambda _name: True)
            suite.check(False, "TOCTOU: retention должен отказаться "
                               "удалять writable снапшот")
        except SnapshotError:
            suite.check(True, "TOCTOU: retention не удаляет writable "
                              "снапшот (fail-closed перед удалением)")
        suite.check(writable_in_snaps.exists(),
                    "TOCTOU: объект не удален")

        # Одна ФС: источник и каталог снапшотов (эмулятор .shim-fsuuid)
        sm.ensure_same_btrfs_filesystem(src, box.snapshot_dir, label="root")
        suite.check(True, "источник и каталог снапшотов на одной ФС")

        # Разные эмулируемые ФС -> ошибка ДО создания снапшотов
        other_fs = box.root / "other-fs"
        (other_fs).mkdir(exist_ok=True)
        (other_fs / ".shim-fsuuid").write_text(
            "11223344-0000-0000-0000-000000000002")
        other_snap_dir = other_fs / "snaps"
        other_snap_dir.mkdir(exist_ok=True)
        try:
            sm.ensure_same_btrfs_filesystem(src, other_snap_dir, label="root")
            suite.check(False, "разные ФС должны давать ошибку")
        except SnapshotError:
            suite.check(True, "разные ФС -> отказ до создания снапшотов")

    # =============================================
    # v1.3.1: обнаружение subvolume системы (замечание пользователя:
    # "нет выбора разделов, с которых делать снапшот и бэкап" — при одном
    # разделе в конфигурации выбирать было не из чего)
    # =============================================
    suite.check(sm.parse_mount_options(
        "rw,relatime,subvolid=257,subvol=/@home") ==
        {"rw": "", "relatime": "", "subvolid": "257", "subvol": "/@home"},
        "разбор опций монтирования")
    mounts = sm.btrfs_mounts([
        "/dev/sda2[/@] / btrfs rw,subvol=/@ 0 0",
        "/dev/sda2[/@home] /home btrfs rw,subvol=/@home 0 0",
        "/dev/sdb1 /mnt/ext4 ext4 rw 0 0",
    ])
    suite.check(mounts == [
        ("/", "/dev/sda2[/@]", "/@"),
        ("/home", "/dev/sda2[/@home]", "/@home")],
        "извлекаются только btrfs-монтирования с их subvol")
    # Выдача `btrfs subvolume list /` — РЕАЛЬНЫЙ формат вывода btrfs-progs
    # (метки колонок: «ID 256 gen 39 top level 5 path boot»;
    # ТЗ 1.4.8 §4.2: разбор обязан соответствовать реальному выводу).
    listing = (
        "ID 257 gen 123 top level 5 path home\n"
        "ID 258 gen 122 top level 5 path var/lib/docker\n"
        "ID 259 gen 121 top level 5 path var/lib/docker/deep/one/two/three\n"
        "ID 260 gen 120 top level 5 path opt\n"
        "ID 261 gen 119 top level 5 path srv/my data\n"
    )
    suite.check(sm.reachable_subvolume_paths(listing, "/", "/") == [
        "/home", "/var/lib/docker", "/opt", "/srv/my data"],
        "корень ФС: реальный формат с метками; видны subvolume глубины "
        "<= 3, docker-глубина отсечена; пробелы в имени сохранены")
    suite.check(sm.reachable_subvolume_paths(listing, "/", "/var") == [
        "/lib/docker"],
        "монтирование /var: только его поддерево, путь относительно "
        "точки монтирования")
    suite.check(sm.reachable_subvolume_paths(
        "ID 258 gen 122 top level 5 path var/lib/docker\n",
        "/mnt/b", "/") == ["/mnt/b/var/lib/docker"],
        "префикс точки монтирования добавляется к путям")
    suite.check(sm.reachable_subvolume_paths(
        "ID 260 gen 120 top level 5 path opt\n", "/mnt/b", "/opt") == ["/mnt/b"],
        "смонтированный subvolume сам доступен по точке монтирования")
    suite.check(sm._parse_subvolume_lines(
        "garbage line without labels\n"
        "ID xx gen 1 top level 5 path notanid\n"
        "ID 262 gen 5 top level 5 path ok\n") == [("262", "/ok")],
        "строки не реального формата (без меток, нечисловой ID) пропускаются")
    # ТЗ 1.4.8.1 §3: путь после структурной метки «path » извлекается
    # ДОСЛОВНО (раньше — токены, склеенные одиночными пробелами, что
    # разрушало имена). Примеры «ID … path boot/var/lib/docker/srv/my
    # data» — формат реального вывода btrfs-progs (btrfs-subvolume.html,
    # подтвержден на 4.4-6.x); имена с пробелами возможны на реальной
    # системе (btrfs запрещает в имени только '/' и NUL). Случаи с
    # несколькими подряд пробелами, табуляцией и начальным пробелом —
    # ИСКУССТВЕННЫЕ тестовые случаи: допустимые btrfs-имена, вывод
    # реальной утилиты с ними в среде разработки недоступен
    # (ТЗ 1.4.8.1 §10 — на пользовательской ФС операции запрещены),
    # btrfs-progs печатает путь без экранирования, поэтому он обязан
    # сохраняться дословно.
    suite.check(sm._parse_subvolume_lines(
        "ID 263 gen 9 top level 5 path my  double  spaces\n")
        == [("263", "/my  double  spaces")],
        "ТЗ 1.4.8.1 §3: несколько подряд пробелов имени сохраняются "
        "(искусственный случай, реальный формат строки)")
    suite.check(sm._parse_subvolume_lines(
        "ID 264 gen 9 top level 5 path my\ttab\tname\n")
        == [("264", "/my\ttab\tname")],
        "ТЗ 1.4.8.1 §3: табуляция имени сохраняется "
        "(искусственный случай, реальный формат строки)")
    suite.check(sm._parse_subvolume_lines(
        "ID 265 gen 9 top level 5 path  leading space\n")
        == [("265", "/ leading space")],
        "ТЗ 1.4.8.1 §3: только один разделитель после «path» съедается, "
        "имя с начальным пробелом сохраняется (искусственный случай)")
    suite.check(sm._parse_subvolume_lines(
        "ID 266 gen 9 top level 5 path path data path\n")
        == [("266", "/path data path")],
        "ТЗ 1.4.8.1 §3: слово 'path' внутри имени не путается со "
        "структурной меткой (позиция метки фиксирована форматом)")
    # Диагностика при пропуске некорректной строки (ТЗ 1.4.8.1 §3).
    import logging as _logging
    class _CapHandler(_logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []
        def emit(self, record):
            self.records.append(record.getMessage())
    _cap = _CapHandler()
    _lg = _logging.getLogger("btrfs_backup.snapshots")
    _lg.addHandler(_cap)
    try:
        parsed = sm._parse_subvolume_lines(
            "ID not digits gen 1 top level 5 path x\n"
            "ID 267 gen 5 top level 5 path kept\n")
    finally:
        _lg.removeHandler(_cap)
    suite.check(parsed == [("267", "/kept")],
                "ТЗ 1.4.8.1 §3: некорректная строка не превращается "
                "в фиктивный результат")
    suite.check(any("пропущена" in msg for msg in _cap.records),
                "ТЗ 1.4.8.1 §3: пропуск некорректной строки сопровождается "
                "диагностикой в журнал")
    suite.check(sm.subvolume_label("/@home") == "athome" and
                sm.subvolume_label("/@") == "at" and
                sm.subvolume_label("/srv/my data") == "my_data",
                "имя subvolume из пути: '@'->at, недопустимые символы->_")
    suite.check(sm.subvolume_label("/") == "root" and
                sm.subvolume_label("/2var") == "2var",
                "имя из пути: корень -> root, цифра первым символом допустима")
    suite.check(sm._is_excluded("/snaps/backup/x", ["/snaps"]) and
                not sm._is_excluded("/snaps2", ["/snaps"]),
                "исключается каталог и всё внутри него, но не имена-префиксы")

    # =============================================
    # v1.4: ВСЕ разделы в списке выбора (замечание пользователя:
    # «у меня 4 раздела, скрипт все еще предлагает мне только один /» —
    # раздел без вложенных subvolume не должен исчезать из списка)
    # =============================================
    suite.check(sm.device_base_name("/dev/sda2[/@]") == "sda2" and
                sm.device_base_name("/dev/nvme0n1p2") == "nvme0n1p2" and
                sm.device_base_name("sda3") == "sda3",
                "имя устройства из поля /proc/self/mounts (с суффиксом subvol)")
    parts = sm.parse_lsblk_partitions([
        "nvme0n1p2 part btrfs",
        "sda1 part vfat /boot/efi",
        "sda2 part",
        "sda disk",
        "zram0 disk zram",
        "loop0 loop squashfs",
        "sr0 rom iso9660",
        "sdd9 part SWAP",
    ])
    suite.check([p["name"] for p in parts] ==
                ["nvme0n1p2", "sda1", "sdd9"],
                "lsblk: part/disk с ФС (включая swap — покажется как "
                "«не Btrfs»); loop/rom/пустые отсечены")
    suite.check(parts[0]["device"] == "/dev/nvme0n1p2" and
                parts[0]["fstype"] == "btrfs",
                "lsblk: устройство получает путь /dev/..., fstype — первый токен")

    with Sandbox() as box:
        m_root = box.root / "mnt-root"
        m_root.mkdir()
        (m_root / "nested").mkdir()
        m_data = box.root / "mnt-data"
        m_data.mkdir()
        mounts = [
            (str(m_root), "btrfs", "/dev/sda2"),
            (str(m_data), "btrfs", "/dev/sdb1"),
        ]
        detected_parts = sm.list_partitions(
            lsblk_lines=["sda2 part btrfs", "sdb1 part btrfs",
                         "sdc1 part ext4"],
            mounts=mounts)
        by_name = {p["name"]: p for p in detected_parts}
        suite.check(by_name["sda2"]["mounted"] and
                    by_name["sda2"]["mount_points"] == [str(m_root)],
                    "list_partitions: монтирование берется из /proc/self/mounts")
        suite.check(not by_name["sdc1"]["mounted"] and
                    by_name["sdc1"]["mount_points"] == [],
                    "list_partitions: без монтирования mounted=False")

        # discover_sources: точка монтирования сама — кандидат,
        # даже если `btrfs subvolume list` ничего не вернул для раздела
        real_run = sm.subprocess.run

        def fake_run(cmd, **kwargs):
            class Proc:
                returncode = 0
                stdout = ("ID 257 gen 88 top level 5 path @/nested\n"
                          if cmd[-1] == str(m_root) else "")
                stderr = ""
            return Proc()

        mount_lines = [
            "/dev/sda2[/@] {} btrfs rw,subvol=/@ 0 0".format(m_root),
            "/dev/sdb1 {} btrfs rw 0 0".format(m_data),
        ]
        try:
            sm.subprocess.run = fake_run
            found = sm.discover_sources(
                mount_lines=mount_lines)["sources"]
        finally:
            sm.subprocess.run = real_run
        sources = sorted(e["source"] for e in found)
        suite.check(str(m_root) in sources and str(m_data) in sources,
                    "v1.4: точки монтирования btrfs сами входят в список "
                    "(раздел без вложенных subvolume не теряется)")
        suite.check(str(m_root / "nested") in sources,
                    "v1.4: вложенные subvolume по-прежнему обнаруживаются")
        suite.check(len([s for s in sources if s == str(m_root)]) == 1,
                    "v1.4: точка монтирования не дублируется с найденным "
                    "по списку subvolume")

    return suite.summary()


if __name__ == "__main__":
    sys.exit(run_main(main))
