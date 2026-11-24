"""
Тесты обнаружения Btrfs, проверки расположения путей и подбора каталогов
(v1.4.6, ТЗ 2.1, 3.1, 3.2): единый механизм core.pathcheck, fallback
btrfs_fs_uuid, предложения каталога снапшотов, создание отдельного
subvolume, группы retention и идентификаторы запуска по нескольким
каталогам.
Запуск: python3 tests/test_discovery.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Sandbox, Suite, run_main  # noqa: E402
import config  # noqa: E402
from core import appconfig as appconfig_module  # noqa: E402
from core import pathcheck as pc  # noqa: E402
from core import snapshots as sm  # noqa: E402
from core import runid  # noqa: E402
from core.appconfig import AppConfig, DEFAULTS  # noqa: E402


def main():
    suite = Suite("discovery")
    cfg = appconfig_module.from_module(config)
    _ORIG_MOUNT_DETAILS = sm.mount_details_for

    # =============================================
    # btrfs_fs_uuid: fallback на точку монтирования (ТЗ 3.1 п.8)
    # =============================================
    with Sandbox() as box:
        src = box.make_subvolume("src-a", b"x" * 100)
        # подмена монтирований: песочница «на Btrfs» с устройством loop0
        sm.mount_details_for = lambda p: (
            str(box.root), "btrfs", "/dev/loop0")
        os.environ["BBM_SHIM_FS_SHOW_FAIL_PLAIN"] = "1"
        try:
            uuid_sub = sm.btrfs_fs_uuid(src)
            suite.check(bool(uuid_sub),
                        "filesystem show для subvolume возвращает UUID")
            plain_dir = box.root / "plain"
            plain_dir.mkdir(exist_ok=True)
            uuid_plain = sm.btrfs_fs_uuid(plain_dir)
            suite.check(
                uuid_plain == uuid_sub and uuid_plain is not None,
                "обычный каталог: fallback на точку монтирования дает "
                "ТОТ ЖЕ UUID (исправление v1.4.6)")
            identity_src = sm.btrfs_fs_identity(src)
            identity_plain = sm.btrfs_fs_identity(plain_dir)
            suite.check(
                identity_src == identity_plain and
                (identity_src or "").startswith("uuid:"),
                "btrfs_fs_identity источника и каталога совпадают (uuid:...)")
        finally:
            os.environ.pop("BBM_SHIM_FS_SHOW_FAIL_PLAIN", None)
        sm.mount_details_for = _ORIG_MOUNT_DETAILS
        # идентичность определения: раньше обычный каталог давал
        # mount:... и «разные файловые системы» (ТЗ ЧАСТЬ I)
        suite.check(pc.equal(src, src), "equal: один и тот же путь")

    # =============================================
    # check_source: все правила расположения (ТЗ 3.1)
    # =============================================
    with Sandbox() as box:
        cfg = appconfig_module.from_module(config)
        cfg.set_value("SNAPSHOT_DIR", str(box.snapshot_dir))
        cfg.set_value("LOCAL_BACKUP_DIR", str(box.backup_dir))
        src = box.make_subvolume("src-ok", b"data" * 10)
        # Точки монтирования эмулируемых ФС различаются по владельцу
        # .shim-fsuuid (как отдельные btrfs-монтирования)
        box_uuid_owner = str(box.root)

        def routed_mount_details(path, _box=box):
            text = str(path)
            if text.startswith(str(_box.root / "other-fs")):
                return (str(_box.root / "other-fs"), "btrfs", "/dev/loop1")
            return (str(_box.root), "btrfs", "/dev/loop0")

        sm.mount_details_for = routed_mount_details
        result = pc.check_source(
            {"name": "src-ok", "source": str(src), "snapshot_dir": None,
             "local_backup_dir": None}, cfg)
        suite.check(result["ok"],
                    "допустимый источник проходит проверку: {}".format(
                        result["errors"]))
        suite.check(result["snapshot_dir"] == str(box.snapshot_dir),
                    "глобальный SNAPSHOT_DIR подставляется по умолчанию")
        suite.check(result["local_backup_dir"] == str(box.backup_dir),
                    "глобальный LOCAL_BACKUP_DIR подставляется по умолчанию")

        # 3.1-1: источник не существует
        result = pc.check_source(
            {"name": "nope", "source": str(box.root / "absent"),
             "snapshot_dir": None, "local_backup_dir": None}, cfg)
        suite.check(not result["ok"] and
                    any("не существует" in e for e in result["errors"]),
                    "несуществующий источник — ошибка")

        # 3.1-2: источник не subvolume (обычный каталог)
        plain = box.root / "plain-dir"
        plain.mkdir(exist_ok=True)
        result = pc.check_source(
            {"name": "plain", "source": str(plain), "snapshot_dir": None,
             "local_backup_dir": None}, cfg)
        suite.check(
            not result["ok"] and
            any("не является Btrfs subvolume" in e for e in result["errors"]),
            "обычный каталог не может быть источником (объяснение дано)")

        # 3.1-3: каталог снапшотов на другой файловой системе
        other_fs = box.root / "other-fs"
        other_fs.mkdir(exist_ok=True)
        (other_fs / ".shim-fsuuid").write_text(
            "ffffffff-0000-0000-0000-000000000002")
        result = pc.check_source(
            {"name": "src-ok", "source": str(src),
             "snapshot_dir": str(other_fs / ".snapshots"),
             "local_backup_dir": str(box.backup_dir)}, cfg)
        suite.check(
            not result["ok"] and
            any("РАЗНЫХ файловых системах" in e for e in result["errors"]),
            "каталог снапшотов на другой ФС Btrfs — ошибка")

        # 3.1-5: каталог снапшотов ВНУТРИ источника (обычный каталог)
        inside = src / ".snapshots"
        inside.mkdir(exist_ok=True)
        result = pc.check_source(
            {"name": "src-ok", "source": str(src),
             "snapshot_dir": str(inside),
             "local_backup_dir": str(box.backup_dir)}, cfg)
        suite.check(
            not result["ok"] and
            any("ВНУТРИ источника" in e for e in result["errors"]),
            "каталог снапшотов внутри источника (обычный каталог) — ошибка")

        # Исключение: каталог снапшотов — ОТДЕЛЬНЫЙ subvolume внутри
        # источника (классическая схема /.snapshots, ТЗ 3.3)
        sub_inside = src / "snaps"
        sub_inside.mkdir(exist_ok=True)
        (sub_inside / ".shim-subvolume").touch()
        result = pc.check_source(
            {"name": "src-ok", "source": str(src),
             "snapshot_dir": str(sub_inside),
             "local_backup_dir": str(box.backup_dir)}, cfg)
        suite.check(result["ok"],
                    "отдельный subvolume внутри источника допустим")
        suite.check(any("отдельный subvolume" in w for w in result["warnings"]),
                    "допустимость поясняется предупреждением")

        # Обратная вложенность: источник внутри каталога снапшотов
        outer_snap = box.root / "outer-snaps"
        outer_snap.mkdir(exist_ok=True)
        (outer_snap / ".shim-fsuuid").write_text(
            Path(box.root / ".shim-fsuuid").read_text())
        nested_src = outer_snap / "src-nested"
        nested_src.mkdir()
        (nested_src / ".shim-subvolume").touch()
        result = pc.check_source(
            {"name": "nested", "source": str(nested_src),
             "snapshot_dir": str(outer_snap),
             "local_backup_dir": str(box.backup_dir)}, cfg)
        suite.check(
            not result["ok"] and
            any("циклическая структура" in e for e in result["errors"]),
            "источник внутри каталога снапшотов — цикл, ошибка")

        # 3.1-7: каталог архивов внутри источника
        result = pc.check_source(
            {"name": "src-ok", "source": str(src),
             "snapshot_dir": str(box.snapshot_dir),
             "local_backup_dir": str(src / "backups")}, cfg)
        suite.check(
            not result["ok"] and
            any("включал бы сами архивы" in e for e in result["errors"]),
            "каталог архивов внутри источника — цикл, ошибка")

        # Каталог архивов == каталог снапшотов
        result = pc.check_source(
            {"name": "src-ok", "source": str(src),
             "snapshot_dir": str(box.snapshot_dir),
             "local_backup_dir": str(box.snapshot_dir)}, cfg)
        suite.check(
            not result["ok"] and
            any("совпадает с каталогом архивов" in e for e in result["errors"]),
            "каталог снапшотов == каталог архивов — ошибка")

        # 3.1-4: каталог снапшотов недоступен для записи
        locked = box.root / "locked-snaps"
        locked.mkdir(exist_ok=True)
        (locked / ".shim-fsuuid").write_text(
            Path(box.root / ".shim-fsuuid").read_text())
        os.chmod(str(locked), 0o500)
        try:
            result = pc.check_source(
                {"name": "src-ok", "source": str(src),
                 "snapshot_dir": str(locked),
                 "local_backup_dir": str(box.backup_dir)}, cfg)
            suite.check(
                not result["ok"] and
                any("недоступен для записи" in e for e in result["errors"]),
                "каталог снапшотов без записи — ошибка")
        finally:
            os.chmod(str(locked), 0o700)
        sm.mount_details_for = _ORIG_MOUNT_DETAILS

        # 3.1-6: каталог архивов недоступен для записи
        locked_bak = box.root / "locked-backup"
        locked_bak.mkdir(exist_ok=True)
        os.chmod(str(locked_bak), 0o500)
        try:
            result = pc.check_source(
                {"name": "src-ok", "source": str(src),
                 "snapshot_dir": str(box.snapshot_dir),
                 "local_backup_dir": str(locked_bak)}, cfg)
            suite.check(
                not result["ok"] and
                any("архивов {} недоступен".format(locked_bak) in e
                    for e in result["errors"]),
                "каталог архивов без записи — ошибка")
        finally:
            os.chmod(str(locked_bak), 0o700)

    # =============================================
    # check_sources: перекрестная вложенность + индивидуальные пути
    # =============================================
    with Sandbox() as box:
        cfg = appconfig_module.from_module(config)
        cfg.set_value("SNAPSHOT_DIR", str(box.snapshot_dir))
        cfg.set_value("LOCAL_BACKUP_DIR", str(box.backup_dir))
        src_a = box.make_subvolume("src-a", b"a" * 10)
        src_b = box.make_subvolume("src-b", b"b" * 10)
        own_snaps = box.root / "own-snaps"
        own_snaps.mkdir(exist_ok=True)
        results = pc.check_sources(
            [{"name": "a", "source": str(src_a),
              "snapshot_dir": str(own_snaps), "local_backup_dir": None},
             {"name": "b", "source": str(src_b),
              "snapshot_dir": None, "local_backup_dir": None}], cfg)
        suite.check(all(r["ok"] for r in results),
                    "два источника с индивидуальным каталогом одного — "
                    "оба проходят")
        suite.check(results[1]["snapshot_dir"] == str(box.snapshot_dir),
                    "второй источник использует глобальный каталог")
        suite.check(results[1]["local_backup_dir"] == str(box.backup_dir),
                    "второй источник использует глобальный каталог архивов")

    # =============================================
    # propose_snapshot_dir (ТЗ 3.2)
    # =============================================
    with Sandbox() as box:
        src = box.make_subvolume("src-p", b"p" * 10)
        sm.mount_details_for = lambda p: (
            str(box.root), "btrfs", "/dev/loop0")
        proposal = pc.propose_snapshot_dir(str(src))
        suite.check(proposal["ok"] and
                    proposal["path"] == str(box.root / ".snapshots"),
                    "предложение: <точка монтирования>/.snapshots вне "
                    "источника")
        suite.check(not proposal["needs_subvolume"],
                    "обычному источнику не требуется отдельный subvolume")
        # Источник == точка монтирования (корень ФС):
        sm.mount_details_for = lambda p: (
            str(src), "btrfs", "/dev/loop0")
        proposal = pc.propose_snapshot_dir(str(src))
        suite.check(
            proposal["ok"] and proposal["needs_subvolume"],
            "корень файловой системы: предложение требует ОТДЕЛЬНЫЙ "
            "subvolume (объяснение ограничений)")
        suite.check("ОТДЕЛЬНЫЙ subvolume" in proposal["reason"],
                    "причина объясняет, почему subvolume обязателен")
        # Источник не на Btrfs
        sm.mount_details_for = lambda p: None
        proposal = pc.propose_snapshot_dir(str(src))
        suite.check(not proposal["ok"] and proposal["path"] is None,
                    "вне Btrfs безопасный каталог не предлагается (None)")
        sm.mount_details_for = _ORIG_MOUNT_DETAILS

    # =============================================
    # ensure_snapshot_dir_as_subvolume (ТЗ 3.2)
    # =============================================
    with Sandbox() as box:
        target = box.root / "created-snaps"
        result = pc.ensure_snapshot_dir_as_subvolume(str(target))
        suite.check(Path(result).exists() and
                    (Path(result) / ".shim-subvolume").exists(),
                    "subvolume каталога снапшотов создан и подтвержден")
        suite.check(sm.is_subvolume(target),
                    "созданный объект признается subvolume")
        try:
            pc.ensure_snapshot_dir_as_subvolume(str(target))
            suite.check(False, "повторное создание должно давать ошибку")
        except Exception:
            suite.check(True, "повторное создание отклонено (уже существует)")

    # =============================================
    # runid: несколько каталогов снапшотов/архивов (ТЗ 3.3)
    # =============================================
    with Sandbox() as box:
        own_snaps = box.root / "own2"
        own_snaps.mkdir()
        run_id = "2026-03-01_000100_000001"
        (own_snaps / "src_{}".format(run_id)).mkdir()
        taken = runid.run_id_taken(run_id, [str(box.snapshot_dir),
                                            str(own_snaps)],
                                   [str(box.backup_dir)], None, ["src"])
        suite.check(taken, "run_id занят в ОДНОМ из нескольких каталогов")
        taken = runid.run_id_taken(run_id, str(box.snapshot_dir),
                                   str(box.backup_dir), None, ["src"])
        suite.check(not taken, "в глобальном каталоге идентификатор свободен")

    # =============================================
    # AppConfig: атрибуты и методы совместимости с модулем config
    # =============================================
    with Sandbox() as box:
        cfg = appconfig_module.from_module(config)
        suite.check(cfg.normalize_subvolumes()[0][0].keys() >=
                    {"name", "source", "snapshot_dir", "local_backup_dir"},
                    "нормализация модуля config дает 4 поля источника")
        suite.check(hasattr(cfg, "retention_count") and
                    hasattr(cfg, "local_backup_dir") and
                    hasattr(cfg, "is_password_placeholder"),
                    "AppConfig предоставляет интерфейс модуля config")
        suite.check(cfg.is_password_placeholder("CHANGE_ME") and
                    not cfg.is_password_placeholder("real-password"),
                    "is_password_placeholder работает у AppConfig")
        # default_lock_file/default_log_file
        cfg.set_value("LOCK_FILE", None)
        cfg.set_value("LOG_FILE", None)
        cfg.profile_name = "test-profile"
        suite.check("test-profile" in cfg.default_lock_file(),
                    "LOCK_FILE по умолчанию включает имя профиля "
                    "(изоляция блокировок, ТЗ 12)")
        suite.check("test-profile" in cfg.default_log_file(),
                    "LOG_FILE по умолчанию включает имя профиля "
                    "(изоляция журналов, ТЗ 12)")

    # =============================================
    # resolve_local_backup_dir: индивидуальный путь приоритетнее (ТЗ 3.3)
    # =============================================
    with Sandbox() as box:
        cfg = appconfig_module.from_module(config)
        cfg.set_value("SNAPSHOT_DIR", str(box.snapshot_dir))
        cfg.set_value("LOCAL_BACKUP_DIR", str(box.backup_dir))
        item = {"name": "x", "source": "/x", "snapshot_dir": "/own/snaps",
                "local_backup_dir": "/own/backups"}
        suite.check(cfg.resolve_snapshot_dir(item) == "/own/snaps" and
                    cfg.resolve_local_backup_dir(item) == "/own/backups",
                    "индивидуальные пути источника приоритетнее глобальных")
        item2 = {"name": "y", "source": "/y", "snapshot_dir": None,
                 "local_backup_dir": None}
        suite.check(cfg.resolve_snapshot_dir(item2) == str(box.snapshot_dir),
                    "без индивидуального пути — глобальный SNAPSHOT_DIR")

    # =============================================
    # §6.2: предложение из проверки несет признаки (ok/needs_subvolume)
    # =============================================
    with Sandbox() as box:
        cfg = appconfig_module.from_module(config)
        cfg.set_value("SNAPSHOT_DIR", str(box.snapshot_dir))
        cfg.set_value("LOCAL_BACKUP_DIR", str(box.backup_dir))
        fs2 = box.root / "fs2"
        fs2.mkdir()
        (fs2 / ".shim-fsuuid").write_text(
            "aabbccdd-0000-0000-0000-000000000002")
        (fs2 / ".shim-subvolume").touch()   # верхний уровень ФС2 — subvolume
        src = fs2 / "src"
        src.mkdir()
        (src / ".shim-subvolume").touch()

        def routed(path, _box=box, _fs2=fs2):
            text_path = str(path)
            fs2s = str(_fs2)
            if text_path == fs2s or text_path.startswith(fs2s + "/"):
                return (str(_fs2), "btrfs", "/dev/loop1")
            return (str(_box.root), "btrfs", "/dev/loop0")

        _orig = sm.mount_details_for
        sm.mount_details_for = routed
        try:
            # Обычный источник на ФС2, глобальный SNAPSHOT_DIR на ФС1
            result = pc.check_source({"name": "x", "source": str(src)}, cfg)
            suite.check(not result["ok"] and
                        any("ДРУГОЙ файловой системе" in e
                            for e in result["errors"]),
                        "глобальный SNAPSHOT_DIR на чужой ФС — ошибка")
            suite.check(result["proposed_snapshot_dir"] ==
                        str(fs2 / ".snapshots") and
                        result["proposal_ok"] is True,
                        "предложение передано с путем ТОЛЬКО при ok=True")
            suite.check(result["proposal_needs_subvolume"] is False and
                        result["proposal_exists"] is False,
                        "признаки needs_subvolume/exists переданы "
                        "вместе с предложением (§6.2)")

            # Источник — верхний уровень ФС2: признак needs_subvolume
            # не теряется при передаче через результат проверки
            result = pc.check_source(
                {"name": "root2", "source": str(fs2)}, cfg)
            suite.check(not result["ok"],
                        "верхний уровень при глобальном каталоге на чужой "
                        "ФС — ошибка")
            suite.check(result["proposed_snapshot_dir"] ==
                        str(fs2 / ".snapshots"),
                        "верхнему уровню предложен <точка>/.snapshots")
            suite.check(result["proposal_needs_subvolume"] is True,
                        "needs_subvolume=True передан вместе с предложением "
                        "и не теряется (§6.2)")
        finally:
            sm.mount_details_for = _orig

    # =============================================
    # §6.5: каталоги создаются ТОЛЬКО после подтверждения файловой системы
    # =============================================
    with Sandbox() as box:
        fresh_snaps = box.root / "brand-new-snaps"   # не существует (ФС1)
        cfg = appconfig_module.from_module(config)
        cfg.set_value("SNAPSHOT_DIR", str(fresh_snaps))
        cfg.set_value("LOCAL_BACKUP_DIR", None)  # -> fresh_snaps/backup
        fs2 = box.root / "fs2b"
        fs2.mkdir()
        (fs2 / ".shim-fsuuid").write_text(
            "aabbccdd-0000-0000-0000-000000000003")
        src = fs2 / "src"
        src.mkdir()
        (src / ".shim-subvolume").touch()
        _orig = sm.mount_details_for

        def routed2(path, _box=box, _fs2=fs2):
            text_path = str(path)
            fs2s = str(_fs2)
            if text_path == fs2s or text_path.startswith(fs2s + "/"):
                return (str(_fs2), "btrfs", "/dev/loop1")
            return (str(_box.root), "btrfs", "/dev/loop0")
        sm.mount_details_for = routed2
        try:
            result = pc.check_source(
                {"name": "x", "source": str(src)}, cfg, create_dirs=True)
            suite.check(not result["ok"],
                        "источник на ФС2 при SNAPSHOT_DIR на ФС1 — отказ")
            suite.check(not fresh_snaps.exists(),
                        "каталог снапшотов НЕ создан до подтверждения ФС "
                        "(§6.5: отказ не оставляет побочных каталогов)")
            suite.check(not (fresh_snaps / "backup").exists(),
                        "каталог архивов НЕ создан при отказе (§6.5)")
        finally:
            sm.mount_details_for = _orig

    # =============================================
    # §6.4: перекрестная проверка учитывает тип вложенного объекта
    # =============================================
    with Sandbox() as box:
        cfg = appconfig_module.from_module(config)
        cfg.set_value("SNAPSHOT_DIR", str(box.snapshot_dir))
        cfg.set_value("LOCAL_BACKUP_DIR", str(box.backup_dir))
        src_a = box.make_subvolume("src-a2", b"a" * 10)
        src_b = box.make_subvolume("src-b2", b"b" * 10)
        plain = src_b / "snaps-a"       # каталог снапшотов A внутри источника B
        plain.mkdir()
        items = [
            {"name": "a", "source": str(src_a),
             "snapshot_dir": str(plain), "local_backup_dir": None},
            {"name": "b", "source": str(src_b),
             "snapshot_dir": None, "local_backup_dir": None},
        ]
        # (а) обычный каталог: содержимое попадет в снапшот источника B
        results = pc.check_sources(items, cfg)
        suite.check(all(r["ok"] for r in results),
                    "оба источника проходят собственные проверки")
        suite.check(any("включит содержимое" in w
                        for w in results[0]["warnings"]),
                    "обычный каталог внутри другого источника — "
                    "предупреждение о включении в снапшот")
        # (б) тот же каталог — отдельный subvolume: НЕ попадает в снапшот B
        (plain / ".shim-subvolume").touch()
        results = pc.check_sources(items, cfg)
        suite.check(all(r["ok"] for r in results),
                    "subvolume-каталог не ломает собственные проверки")
        suite.check(any("не попадает" in w for w in results[0]["warnings"]) and
                    not any("включит содержимое" in w
                            for w in results[0]["warnings"]),
                    "отдельный subvolume внутри другого источника — в снапшот "
                    "НЕ попадает (§6.4: предупреждение соответствует Btrfs)")
        # (в) каталог — точка монтирования: смонтированное содержимое
        # в снапшот B не попадает
        (plain / ".shim-subvolume").unlink()   # (б) оставил маркер — снимаем
        _orig = sm.mount_details_for

        def routed3(path, _plain=plain, _real=_orig):
            if str(path) == str(_plain):
                return (str(_plain), "btrfs", "/dev/loop0")
            return _real(path)
        sm.mount_details_for = routed3
        try:
            results = pc.check_sources(items, cfg)
            suite.check(any("точка монтирования" in w
                            for w in results[0]["warnings"]) and
                        not any("включит содержимое" in w
                                for w in results[0]["warnings"]),
                        "точка монтирования внутри другого источника — "
                        "смонтированное содержимое в снапшот не попадает "
                        "(§6.4)")
        finally:
            sm.mount_details_for = _orig

    return suite.summary()


if __name__ == "__main__":
    sys.exit(run_main(main))
