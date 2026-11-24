"""
Тесты восстановления (ТЗ 11, 20.6): из архивов gzip/zstd, из read-only
снапшота, защита от перезаписи, частичные результаты.
ТЗ 1.4.8.2: §3.1 (D-1) — очистка частичного subvolume напрямую,
без property set; §3.3 (D-4) — stdout btrfs receive не попадает в
терминал родительского процесса.
Запуск: python3 tests/test_restore.py
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    PASSWORD, PROJECT, Sandbox, Suite, make_archive, run_main,
)
import config  # noqa: E402
from core import RestoreError  # noqa: E402
from core import restore as rs  # noqa: E402
from core import snapshots as sm  # noqa: E402


def main():
    suite = Suite("restore")
    payload = os.urandom(150000) + b"RESTORE-MARKER" * 200

    # --- 20.6-1: восстановление gzip-архива, сравнение данных ---
    with Sandbox() as box:
        path, _snap, _sha = make_archive(box, "root", payload,
                                         compression="gzip")
        box.restore_dir.mkdir(exist_ok=True)
        dest = rs.receive_archive(path, box.restore_dir,
                                  gpg_binary=config.GPG_BINARY,
                                  password=PASSWORD)
        got = (dest / ".shim-data").read_bytes()
        suite.check(got == payload,
                    "20.6-1: gzip-архив восстановлен, данные идентичны "
                    "(не только существование файла)")
        suite.check(sm.is_subvolume(dest) and
                    sm.subvolume_ro_property(dest) is True,
                    "восстановленный subvolume read-only (как из send)")

    # --- 20.6-2: восстановление zstd-архива при COMPRESSION=gzip ---
    with Sandbox() as box:
        config.COMPRESSION = "gzip"  # смена конфигурации после создания
        try:
            path, _snap, _sha = make_archive(box, "root", payload,
                                             compression="zstd")
            dest = rs.receive_archive(path, box.restore_dir,
                                      gpg_binary=config.GPG_BINARY,
                                      password=PASSWORD)
            suite.check((dest / ".shim-data").read_bytes() == payload,
                        "20.6-2/20.2-9: zstd-архив восстановлен при "
                        "COMPRESSION=gzip (по расширению архива)")
        finally:
            config.COMPRESSION = "zstd"

    # --- 20.6-3: неверный пароль -> btrfs receive НЕ выполняется ---
    with Sandbox() as box:
        path, _snap, _sha = make_archive(box, "root", payload,
                                         compression="gzip")
        suite.expect_raises(
            lambda: rs.receive_archive(path, box.restore_dir,
                                       gpg_binary=config.GPG_BINARY,
                                       password="wrong-pass"),
            RestoreError, "неверный пароль -> восстановление отменено")
        suite.check(not any(box.restore_dir.iterdir()),
                    "при неверном пароле btrfs receive НЕ выполнялся "
                    "(целевой каталог пуст)")

    # --- 20.6-4: поврежденный архив -> receive не выполняется ---
    with Sandbox() as box:
        path, _snap, _sha = make_archive(box, "root", payload,
                                         compression="gzip")
        data = path.read_bytes()
        path.write_bytes(data[:len(data) // 3])
        suite.expect_raises(
            lambda: rs.receive_archive(path, box.restore_dir,
                                       gpg_binary=config.GPG_BINARY,
                                       password=PASSWORD),
            RestoreError, "поврежденный архив -> восстановление отменено "
            "(receive не выполнялся)")
        suite.check(not any(box.restore_dir.iterdir()),
                    "поврежденный архив не создал частичный subvolume")

    # --- 20.6-5: недостаточно свободного места ---
    with Sandbox() as box:
        path, _snap, _sha = make_archive(box, "root", payload,
                                         compression="gzip")
        real_free = rs.free_space
        rs.free_space = lambda p: 10  # 10 байт свободно
        try:
            suite.expect_raises(
                lambda: rs.check_free_space(box.restore_dir,
                                            path.stat().st_size),
                RestoreError, "20.6-5: свободного места меньше архива -> "
                "отказ до восстановления")
        finally:
            rs.free_space = real_free

    # --- 20.6-6: существующий целевой subvolume -> отказ без перезаписи ---
    with Sandbox() as box:
        path, _snap, _sha = make_archive(box, "root", payload,
                                         compression="gzip")
        # Первый receive создает subvolume
        dest1 = rs.receive_archive(path, box.restore_dir,
                                   gpg_binary=config.GPG_BINARY,
                                   password=PASSWORD)
        suite.expect_raises(
            lambda: rs.receive_archive(path, box.restore_dir,
                                       gpg_binary=config.GPG_BINARY,
                                       password=PASSWORD),
            RestoreError, "20.6-6: повторное восстановление в тот же каталог "
            "запрещено (перезапись)")
        suite.check((dest1 / ".shim-data").read_bytes() == payload,
                    "существующие данные не повреждены отказом")

    # --- 20.6-7/8/9: восстановление из read-only снапшота ---
    with Sandbox() as box:
        src = box.make_subvolume("src-root", b"snapshot-restore" * 100)
        snap = sm.create_readonly_snapshot(src, box.snapshot_dir, "root",
                                           "2026-03-05_000001_aa0001")
        dest = rs.restore_snapshot_writable(snap, box.restore_dir)
        suite.check(sm.is_subvolume(dest) and
                    sm.subvolume_ro_property(dest) is False,
                    "20.6-8: создан ЗАПИСЫВАЕМЫЙ снапшот")
        suite.check((dest / ".shim-data").read_bytes() == b"snapshot-restore" * 100,
                    "20.6-7: данные восстановлены из снапшота")
        suite.check(sm.subvolume_ro_property(snap) is True and snap.exists(),
                    "20.6-9: исходный read-only снапшот не изменен")

    # --- ТЗ 4.4 (исправление №2): fail-closed при восстановлении из снапшота ---
    with Sandbox() as box:
        src = box.make_subvolume("src-root", b"ro-check" * 100)
        snap = sm.create_readonly_snapshot(src, box.snapshot_dir, "root",
                                           "2026-03-05_000002_aa0002")
        # ro=None (свойство определить не удалось) — операция ЗАПРЕЩЕНА
        # (restore.py импортирует функцию напрямую — патчим ссылку в rs)
        real_ro = sm.subvolume_ro_property
        sm.subvolume_ro_property = lambda _p: None
        rs.subvolume_ro_property = lambda _p: None
        try:
            suite.expect_raises(
                lambda: rs.restore_snapshot_writable(snap, box.restore_dir),
                RestoreError,
                "ro=None: восстановление из снапшота запрещено (fail-closed)")
            # ro=False (источник доступен на запись) — операция ЗАПРЕЩЕНА
            sm.subvolume_ro_property = lambda _p: False
            rs.subvolume_ro_property = lambda _p: False
            suite.expect_raises(
                lambda: rs.restore_snapshot_writable(snap, box.restore_dir),
                RestoreError,
                "ro=False: источник на запись — операция запрещена")
        finally:
            sm.subvolume_ro_property = real_ro
            rs.subvolume_ro_property = real_ro
        suite.check(not list(box.restore_dir.iterdir()),
                    "при отказе проверки RO снапшот-копия не создана")
        suite.check(sm.subvolume_ro_property(snap) is True,
                    "исходный снапшот остался read-only")

    # --- 20.6-10/11: ошибка btrfs receive — диагностика частичного результата ---
    with Sandbox() as box:
        path, _snap, _sha = make_archive(box, "root", payload,
                                         compression="gzip")

        def failing_receive_cmd(target):
            # Имитация реального обрыва receive: subvolume с именем потока
            # уже создан, данные записаны частично, затем ошибка.
            script = (
                'IFS= read -r first; '
                'case "$first" in '
                '  STREAM-NAME=*) name="${first#STREAM-NAME=}";; '
                '  *) name="unknown";; '
                'esac; '
                'mkdir -p "$1/$name"; '
                'dd if=/dev/zero of="$1/$name/blob" bs=1k count=4 '
                '2>/dev/null; '
                "echo 'ERROR: simulated receive failure' >&2; exit 1"
            )
            return ["sh", "-c", script, "btrfs-receive", str(target)]

        raised = False
        try:
            rs.receive_archive(
                path, box.restore_dir, gpg_binary=config.GPG_BINARY,
                password=PASSWORD, precheck=False,
                receive_cmd_builder=failing_receive_cmd)
        except RestoreError as exc:
            raised = True
            message = str(exc)
        suite.check(raised and "частичный результат" in message,
                    "20.6-11: частичный восстановленный subvolume "
                    "обнаружен и не принят за успех")
        suite.check("Автоматическое удаление НЕ выполняется" in message,
                    "программа не удаляет пользовательские данные "
                    "автоматически")
        partial = next(box.restore_dir.iterdir())
        suite.check(partial.exists(),
                    "частичный результат сохранен для ручной проверки")

        # Очистка частичного результата (ТЗ 1.4.8.2 §3.1, D-1): только
        # Btrfs subvolume, прямой потомок целевого каталога, объект
        # текущей операции, без property set ro false
        suite.expect_raises(
            lambda: rs.cleanup_partial_subvolume(
                partial, target_dir=box.restore_dir),
            RestoreError, "обычный каталог НЕ удаляется как subvolume")
        suite.check(partial.exists(), "обычный каталог сохранен")
        # Имя не совпало с ожидаемым — отказ
        suite.expect_raises(
            lambda: rs.cleanup_partial_subvolume(
                partial, target_dir=box.restore_dir,
                expected_name="другое_имя"),
            RestoreError, "несовпадение имени запрещает удаление")
        # Настоящий subvolume ВНЕ разрешенного целевого каталога — отказ
        suite.expect_raises(
            lambda: rs.cleanup_partial_subvolume(
                box.root / "tmp-src-root", target_dir=box.restore_dir,
                expected_name="tmp-src-root"),
            RestoreError, "объект вне целевого каталога НЕ удаляется")
        suite.check((box.root / "tmp-src-root").exists(),
                    "объект вне целевого каталога сохранен")
        # Вложенный путь (не прямой потомок целевого каталога) — отказ
        nested_parent = box.restore_dir / "sub_outer"
        nested_parent.mkdir(exist_ok=True)
        nested = nested_parent / "sub_inner"
        nested.mkdir()
        (nested / ".shim-subvolume").touch()
        suite.expect_raises(
            lambda: rs.cleanup_partial_subvolume(
                nested, target_dir=box.restore_dir,
                expected_name="sub_inner", preexisting=set()),
            RestoreError, "вложенный объект (не прямой потомок) НЕ "
            "удаляется")
        suite.check(nested.exists(), "вложенный объект сохранен")

        # Успешная очистка: read-only subvolume С received_uuid (как
        # создает btrfs receive) удаляется НАПРЯМУЮ. Эмулятор отклоняет
        # `property set ro false` для received — как реальный btrfs-progs
        # >= 5.14.2 ("cannot flip ro->rw with received_uuid set") —
        # поэтому успех очистки доказывает, что смена ro НЕ выполняется.
        subvol_partial = box.restore_dir / "sub_part"
        subvol_partial.mkdir(exist_ok=True)
        (subvol_partial / ".shim-subvolume").touch()
        (subvol_partial / ".shim-readonly").touch()
        (subvol_partial / ".shim-received").touch()
        (subvol_partial / ".shim-data").write_bytes(b"partial")
        rs.cleanup_partial_subvolume(subvol_partial,
                                     target_dir=box.restore_dir,
                                     expected_name="sub_part",
                                     preexisting=set())
        suite.check(not subvol_partial.exists(),
                    "D-1: read-only received subvolume удален НАПРЯМУЮ "
                    "btrfs subvolume delete (property set ro false не "
                    "выполнялся — эмулятор его отклонил бы)")

        # Объект, существовавший ДО операции (в перечне preexisting), —
        # отказ: существующие пользовательские данные не затрагиваются
        existed = box.restore_dir / "existed_before"
        existed.mkdir(exist_ok=True)
        (existed / ".shim-subvolume").touch()
        suite.expect_raises(
            lambda: rs.cleanup_partial_subvolume(
                existed, target_dir=box.restore_dir,
                expected_name="existed_before",
                preexisting={"existed_before"}),
            RestoreError, "объект, существовавший до операции, НЕ "
            "удаляется (preexisting)")
        suite.check(existed.exists(),
                    "существовавший до операции объект сохранен")
        # preexisting=None (состояние каталога до операции неизвестно) —
        # отказ (fail-closed)
        suite.expect_raises(
            lambda: rs.cleanup_partial_subvolume(
                existed, target_dir=box.restore_dir,
                expected_name="existed_before", preexisting=None),
            RestoreError, "preexisting=None — отказ (fail-closed)")
        suite.check(existed.exists(), "объект сохранен при отказе")
        # Ошибка btrfs subvolume delete НЕ маскируется успехом
        existed2 = box.restore_dir / "existed2"
        existed2.mkdir()
        (existed2 / ".shim-subvolume").touch()
        real_run_command = rs.run_command

        def failing_delete(*_args, **_kwargs):
            raise RestoreError("симуляция отказа btrfs subvolume delete")

        rs.run_command = failing_delete
        try:
            suite.expect_raises(
                lambda: rs.cleanup_partial_subvolume(
                    existed2, target_dir=box.restore_dir,
                    expected_name="existed2", preexisting=set()),
                RestoreError, "отказ btrfs subvolume delete — ошибка "
                "очистки, НЕ успех")
        finally:
            rs.run_command = real_run_command
        suite.check(existed2.exists(),
                    "объект сохранен при отказе удаления")

    # --- ТЗ 1.4.8.2 §3.1 (D-1): полный цикл partial_info -> очистка ---
    # Обрыв receive посреди приема (read-only received subvolume создан),
    # ошибка несет partial_info (целевой каталог, ожидаемый subvolume,
    # перечень preexisting), очистка по этому перечню удаляет ТОЛЬКО
    # объект текущей операции, повторное восстановление после очистки
    # проходит.
    with Sandbox() as box:
        path, _snap, _sha = make_archive(box, "root", payload,
                                         compression="gzip")
        # Существовавший ДО операции subvolume в целевом каталоге:
        # не должен попасть в удаление
        keeper = box.restore_dir / "user_subvol"
        keeper.mkdir()
        (keeper / ".shim-subvolume").touch()
        (keeper / ".shim-data").write_bytes(b"user data")
        expected_name = path.name[:-len(".gz.gpg")]
        os.environ["BBM_SHIM_RECEIVE_PARTIAL"] = "1"
        raised_exc = None
        try:
            rs.receive_archive(path, box.restore_dir,
                               gpg_binary=config.GPG_BINARY,
                               password=PASSWORD)
        except RestoreError as exc:
            raised_exc = exc
        finally:
            os.environ.pop("BBM_SHIM_RECEIVE_PARTIAL", None)
        suite.check(raised_exc is not None,
                    "D-1: обрыв receive — RestoreError")
        info = getattr(raised_exc, "partial_info", None)
        suite.check(info is not None and
                    info["target"] == str(box.restore_dir) and
                    info["dest"] == str(box.restore_dir / expected_name) and
                    info["preexisting"] == ["user_subvol"],
                    "D-1: partial_info содержит целевой каталог, ожидаемый "
                    "subvolume и перечень существовавших (preexisting)")
        msg = str(raised_exc)
        suite.check("btrfs subvolume delete" in msg and
                    "property set" not in msg,
                    "D-1: подсказка — прямое btrfs subvolume delete без "
                    "предварительной смены ro")
        partial = Path(info["dest"])
        suite.check(partial.exists() and sm.is_subvolume(partial) and
                    sm.subvolume_ro_property(partial) is True,
                    "D-1: частичный read-only received subvolume создан")
        # Очистка через partial_info: удаляет частичный объект,
        # существовавший user_subvol не затронут
        rs.cleanup_partial_subvolume(
            partial, target_dir=info["target"],
            expected_name=expected_name,
            preexisting=set(info["preexisting"]))
        suite.check(not partial.exists(),
                    "D-1: частичный received subvolume удален НАПРЯМУЮ "
                    "(эмулятор отклонил бы property set ro false)")
        suite.check(keeper.exists() and
                    (keeper / ".shim-data").read_bytes() == b"user data",
                    "D-1: существовавший до операции subvolume не затронут")
        # После очистки повторное восстановление проходит
        dest = rs.receive_archive(path, box.restore_dir,
                                  gpg_binary=config.GPG_BINARY,
                                  password=PASSWORD)
        suite.check(sm.is_subvolume(dest) and
                    (dest / ".shim-data").read_bytes() == payload,
                    "D-1: после очистки повторное восстановление успешно")

    # --- ТЗ 1.4.8.2 §3.3 (D-4): stdout btrfs receive не попадает в
    # терминал родительского процесса ---
    # Дочерний процесс запускает receive_archive со stage receive,
    # пишущей маркер в stdout. До исправления D-4 stdout наследовался
    # от родителя и попадал бы в вывод; теперь перенаправлен во
    # временный файл. Родитель проверяет: маркер в stdout дочернего
    # процесса ОТСУТСТВУЕТ, восстановление при этом завершилось успешно.
    child_script = textwrap.dedent("""
        import sys
        sys.path.insert(0, {project!r})
        sys.path.insert(0, {tests!r})
        from common import PASSWORD, Sandbox, make_archive
        import config
        from core import restore as rs

        def noisy_receive(target):
            return ["sh", "-c",
                    'echo NOISE-STDOUT-MARKER; exec btrfs receive "$1"',
                    "noisy-receive", str(target)]

        with Sandbox() as box:
            path, _s, _h = make_archive(box, "root", b"d4-payload" * 500,
                                        compression="gzip")
            dest = rs.receive_archive(
                path, box.restore_dir, gpg_binary=config.GPG_BINARY,
                password=PASSWORD, receive_cmd_builder=noisy_receive)
            assert (dest / ".shim-data").read_bytes() == b"d4-payload" * 500
        print("CHILD-OK")
    """).format(project=str(PROJECT), tests=str(PROJECT / "tests"))
    proc = subprocess.run(
        [sys.executable, "-c", child_script],
        capture_output=True, text=True, timeout=180,
        cwd=str(PROJECT),
    )
    suite.check("CHILD-OK" in (proc.stdout or ""),
                "D-4: восстановление с шумной receive-стадией завершено "
                "успешно (rc={}){}".format(
                    proc.returncode,
                    (": " + (proc.stderr or "").strip()[-300:])
                    if "CHILD-OK" not in (proc.stdout or "") else ""))
    suite.check("NOISE-STDOUT-MARKER" not in (proc.stdout or ""),
                "D-4: stdout receive НЕ попадает в терминал родителя "
                "(TUI не нарушается)")

    # --- validate_restore_target ---
    with Sandbox() as box:
        missing = box.root / "no-such-target"
        suite.expect_raises(
            lambda: rs.validate_restore_target(missing),
            RestoreError, "несуществующий целевой каталог -> ошибка")
        f = box.root / "file-target"
        f.write_text("x")
        suite.expect_raises(
            lambda: rs.validate_restore_target(f),
            RestoreError, "целевой путь-файл -> ошибка")

    return suite.summary()


if __name__ == "__main__":
    sys.exit(run_main(main))
