"""
Тесты retention (ТЗ 9, 20.5): независимые истории, подтвержденные копии,
запрет удаления без сети.
Запуск: python3 tests/test_retention.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    Sandbox, Suite, cleanup_network_files, make_archive, run_main,
)
import config  # noqa: E402
from core import metadata as md  # noqa: E402
from core import network as net  # noqa: E402
from core import retention as rt  # noqa: E402
from core import snapshots as sm  # noqa: E402
from core import verification as vr  # noqa: E402


def _snapshot_names(box, subvolume):
    groups = sm.list_snapshots(box.snapshot_dir, {subvolume})
    return [s.name for s in groups.get(subvolume, [])]


def main():
    suite = Suite("retention")

    with Sandbox() as box:
        config.RETENTION_COUNT = 3
        payload = os.urandom(60000) + b"RETENTION" * 20

        # 7 копий subvolume root с подтвержденными сетевыми копиями
        run_ids = ["2026-03-01_0000{:02d}_{:06x}".format(i, i)
                   for i in range(1, 8)]
        for i, run_id in enumerate(run_ids, 1):
            path, snap, sha = make_archive(
                box, "root", payload + bytes([i]), compression="gzip",
                run_id=run_id)
            # отправка на "шару": tmp -> promote -> метаданные JSON
            # (как делает программа, ТЗ 7.3)
            tmp, actual = net.copy_to_network_tmp(path, box.network_dir, sha)
            net.promote_network_archive(tmp, actual)
            meta = md.read_metadata(path)
            net.write_network_metadata(box.network_dir, meta, actual)
            os.unlink(path)  # локальная копия удалена после подтверждения
            os.unlink(Path(str(path) + ".sha256"))
            jf = Path(str(path) + ".json")
            if jf.exists():
                os.unlink(jf)

        names = _snapshot_names(box, "root")
        suite.check(len(names) == 7, "создано 7 снапшотов")

        # 20.5-8: после успешной отправки retention работает корректно
        plan = rt.retention_plan(box.snapshot_dir, ["root"], 3,
                                 box.network_dir)
        suite.check(len(plan.get("root", [])) == 4,
                    "план: 4 старейших снапшота сверх retention 3")
        suite.check(set(plan["root"]) == set(names[3:]),
                    "к удалению назначены именно СТАРЕЙшие подтвержденные")

        deleted = rt.apply_retention(box.snapshot_dir, ["root"], 3,
                                     box.network_dir, delete=True)
        suite.check(len(deleted.get("root", [])) == 4,
                    "удалено 4 снапшота")
        suite.check(len(_snapshot_names(box, "root")) == 3,
                    "осталось ровно RETENTION_COUNT снапшотов")
        # 20.5-9: сетевые архивы никогда не удаляются автоматически
        net_archives = sorted(p.name for p in box.network_dir.glob("*.gpg"))
        suite.check(len(net_archives) == 7,
                    "сетевые архивы не удалены (7 остаются на шаре)")

        # 20.5-1: независимые истории по subvolume
        for i, run_id in enumerate(["2026-03-02_0000{:02d}_aaaa0{}".format(i, i)
                                    for i in range(2)], 1):
            make_archive(box, "home", b"home-data-" + bytes([i]),
                         compression="gzip", run_id=run_id)
        # home копии НЕ отправлены на шару
        plan = rt.retention_plan(box.snapshot_dir, ["root", "home"], 3,
                                 box.network_dir)
        suite.check("home" not in plan or not plan["home"],
                    "20.5-1: история home независима; без подтвержденных "
                    "копий home не удаляется")
        suite.check(len(_snapshot_names(box, "home")) == 2,
                    "снапшоты home сохранены")

        # 20.5-5/6: сеть недоступна — опасное удаление не выполняется
        import common
        common.NETMOUNT_IS_MOUNTED[0] = False
        before = _snapshot_names(box, "root")
        try:
            rt.retention_safety_check(config, box.network_dir)
            suite.check(False, "без монтирования проверка должна падать")
        except Exception:
            suite.check(True, "retention: без активного монтирования "
                              "удаление отменено (ТЗ 9.3)")
        common.NETMOUNT_IS_MOUNTED[0] = True

        # Подтвержденность исчезла — снапшоты не удаляются (20.5-2/3)
        cleanup_network_files(box)
        plan = rt.retention_plan(box.snapshot_dir, ["root"], 3,
                                 box.network_dir)
        suite.check(not plan.get("root"),
                    "без подтвержденных копий план удаления пуст "
                    "(20.5-6: snapshot сохраняется)")
        deleted = rt.apply_retention(box.snapshot_dir, ["root"], 3,
                                     box.network_dir, delete=True)
        suite.check(not deleted.get("root") and
                    len(_snapshot_names(box, "root")) == 3,
                    "ни один снапшот не удален без подтвержденной копии")

        # 20.5-10: ошибка retention не прерывает сохранность новых архивов
        # (создание архива не зависит от retention)
        path, _snap, sha = make_archive(
            box, "root", b"new-arch", compression="gzip",
            run_id="2026-03-03_000001_bbbb01")
        suite.check(path.is_file(),
                    "20.5-10: новый архив создан независимо от retention")
        new_meta = md.read_metadata(path) or {}
        suite.check(new_meta.get("sha256"),
                    "v1.3: SHA-256 нового архива хранится в JSON-метаданных")

        # Поддельный sidecar в сети не подтверждает копию (20.5-4)
        snap_name = path.name.replace(".gz.gpg", "").replace(".zst.gpg", "")
        net_final = box.network_dir / path.name
        import shutil as _sh
        _sh.copy(path, net_final)
        (box.network_dir / (net_final.name + ".sha256")).write_text(
            "00" * 32 + "  fake\n", encoding="utf-8")
        suite.check(not net.network_archive_confirmed(box.network_dir,
                                                      snap_name),
                    "20.5-4: поддельный sidecar не подтверждает резервную "
                    "копию")

    # =============================================
    # ТЗ 6.4 (исправление №4): смешанная история gzip/zstd при СМЕНЕННОЙ
    # конфигурации сжатия.
    # =============================================
    with Sandbox() as box:
        import common
        config.RETENTION_COUNT = 2
        # 6 копий: чередование gzip и zstd; все отправлены и подтверждены
        run_ids = ["2026-04-01_0000{:02d}_c0ff{:02d}".format(i, i)
                   for i in range(1, 7)]
        for i, run_id in enumerate(run_ids, 1):
            compression = "gzip" if i % 2 else "zstd"
            path, snap, sha = make_archive(box, "mix", payload + bytes([i]),
                                           compression=compression,
                                           run_id=run_id)
            tmp, actual = net.copy_to_network_tmp(path, box.network_dir, sha)
            net.promote_network_archive(tmp, actual)
            meta = md.read_metadata(path)
            suite.check(net.write_network_metadata(box.network_dir, meta,
                                                   actual),
                        "метаданные {}-архива записаны".format(compression))
            os.unlink(path)
            jf = Path(str(path) + ".json")
            if jf.exists():
                os.unlink(jf)
        names = _snapshot_names(box, "mix")
        suite.check(len(names) == 6, "создано 6 снапшотов (смешанная история)")
        # Текущее COMPRESSION менялось после создания архивов — retention
        # НЕ должен терять старые копии (ТЗ 6.4 п.4)
        config.COMPRESSION = "zstd"
        plan = rt.retention_plan(box.snapshot_dir, ["mix"], 2,
                                 box.network_dir, compression="zstd")
        suite.check(len(plan.get("mix", [])) == 4,
                    "ТЗ 6.4 п.1/4: при COMPRESSION=zstd старые gzip-архивы "
                    "учтены (4 кандидата)")
        config.COMPRESSION = "gzip"
        plan2 = rt.retention_plan(box.snapshot_dir, ["mix"], 2,
                                  box.network_dir, compression="gzip")
        suite.check(set(plan2.get("mix", [])) == set(plan.get("mix", [])),
                    "ТЗ 6.4 п.2/4: при COMPRESSION=gzip zstd-архивы учтены, "
                    "план идентичен")
        # Удаление при смешанной истории: только сверх retention
        deleted = rt.apply_retention(box.snapshot_dir, ["mix"], 2,
                                     box.network_dir, delete=True)
        suite.check(len(deleted.get("mix", [])) == 4,
                    "ТЗ 6.4 п.3: смешанная история удаляется корректно")
        suite.check(len(_snapshot_names(box, "mix")) == 2,
                    "осталось RETENTION_COUNT=2 снапшотов")
        net_names = sorted(p.name for p in box.network_dir.glob("*.gpg"))
        suite.check(len(net_names) == 6 and
                    sum(n.endswith(".gz.gpg") for n in net_names) == 3 and
                    sum(n.endswith(".zst.gpg") for n in net_names) == 3,
                    "сетевые архивы gzip и zstd не удалены и не потеряны")

    # ТЗ 6.4 п.5: сетевая копия gzip отсутствует — снапшот не удаляется
    with Sandbox() as box:
        config.RETENTION_COUNT = 1
        path, _snap, sha = make_archive(box, "ghost", b"no-network-copy" * 100,
                                        compression="gzip",
                                        run_id="2026-04-02_000001_d1e2f1")
        # отправки НЕ было: локальный архив остался, но даже при наличии
        # локальной копии снапшот без подтвержденной сетевой не удаляется
        deleted = rt.apply_retention(box.snapshot_dir, ["ghost"], 1,
                                     box.network_dir, delete=True)
        suite.check(not deleted.get("ghost"),
                    "ТЗ 6.4 п.5: без сетевой копии gzip снапшот не удаляется")
        suite.check(len(_snapshot_names(box, "ghost")) == 1,
                    "снапшот сохранен")

    # ТЗ 6.4 п.6: поврежденная сетевая копия zstd — снапшот не удаляется
    with Sandbox() as box:
        config.RETENTION_COUNT = 0
        path, _snap, sha = make_archive(box, "corrupt", b"corrupt-me" * 100,
                                        compression="zstd",
                                        run_id="2026-04-03_000001_e2f3a1")
        tmp, actual = net.copy_to_network_tmp(path, box.network_dir, sha)
        net.promote_network_archive(tmp, actual)
        meta = md.read_metadata(path)
        net.write_network_metadata(box.network_dir, meta, actual)
        saved = (box.network_dir / path.name).read_bytes()
        (box.network_dir / path.name).write_bytes(saved[:-50])
        deleted = rt.apply_retention(box.snapshot_dir, ["corrupt"], 0,
                                     box.network_dir, delete=True)
        suite.check(not deleted.get("corrupt"),
                    "ТЗ 6.4 п.6: поврежденная копия zstd не подтверждает "
                    "удаление")
        suite.check(len(_snapshot_names(box, "corrupt")) == 1,
                    "снапшот при поврежденной копии сохранен")
        (box.network_dir / path.name).write_bytes(saved)
        # после восстановления копии удаление возможно (retention 0)
        deleted = rt.apply_retention(box.snapshot_dir, ["corrupt"], 0,
                                     box.network_dir, delete=True)
        suite.check(len(deleted.get("corrupt", [])) == 1,
                    "восстановленная копия подтверждает удаление")

    # ТЗ 6.4 п.8 / ТЗ 9: сеть отключилась перед/во время проверки —
    # потенциально опасное удаление не выполняется
    with Sandbox() as box:
        config.RETENTION_COUNT = 0
        for i, run_id in enumerate(
                ["2026-04-04_0000{:02d}_f3e4{:02d}".format(i, i)
                 for i in range(1, 3)], 1):
            path, _snap, sha = make_archive(box, "offline", b"net-drop" * 100,
                                            compression="gzip", run_id=run_id)
            tmp, actual = net.copy_to_network_tmp(path, box.network_dir, sha)
            net.promote_network_archive(tmp, actual)
            meta = md.read_metadata(path)
            net.write_network_metadata(box.network_dir, meta, actual)
        # 7. Ошибка получения read-only свойства — удаление запрещено
        real_ro = sm.subvolume_ro_property
        sm.subvolume_ro_property = lambda _p: None
        try:
            try:
                rt.apply_retention(box.snapshot_dir, ["offline"], 0,
                                   box.network_dir, delete=True)
                suite.check(False, "ошибка ro должна запрещать удаление")
            except Exception:
                suite.check(True,
                            "ТЗ 9 п.10: ошибка read-only свойства — "
                            "удаление запрещено")
            suite.check(len(_snapshot_names(box, "offline")) == 2,
                        "ни один снапшот не удален при ошибке свойства")
        finally:
            sm.subvolume_ro_property = real_ro
        # 8. Сеть исчезла во время подтверждения копий
        for entry in box.network_dir.iterdir():
            entry.unlink()
        deleted = rt.apply_retention(box.snapshot_dir, ["offline"], 0,
                                     box.network_dir, delete=True)
        suite.check(not deleted.get("offline"),
                    "ТЗ 6.4 п.8: сеть недоступна — retention не удаляет "
                    "снапшоты, требующие подтвержденной копии")
        suite.check(len(_snapshot_names(box, "offline")) == 2,
                    "все снапшоты сохранены при недоступной сети")

    return suite.summary()


if __name__ == "__main__":
    sys.exit(run_main(main))
