"""
Тесты upload-pending (ТЗ 7.5): идемпотентность, сохранность при ошибках,
повторная отправка.
Запуск: python3 tests/test_upload_pending.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    PASSWORD, Sandbox, Suite, make_archive, run_main, silent_reporter,
)
import config  # noqa: E402
from core import StorageError  # noqa: E402
from core import backup as bk  # noqa: E402
from core import metadata as md  # noqa: E402
from core import verification as vr  # noqa: E402

import common  # noqa: E402


def main():
    suite = Suite("upload-pending")

    # --- 20.4-6: повторная отправка после ошибки проходит успешно ---
    with Sandbox() as box:
        payload = os.urandom(80000) + b"PENDING" * 100
        path, _snap, sha = make_archive(box, "root", payload,
                                        compression="gzip")
        common.NETMOUNT_IS_MOUNTED[0] = True

        # Первая отправка падает (сеть отвалена во время копирования)
        real_copy = bk.copy_to_network_tmp
        def failing_copy(local_archive, net_dir, expected_sha):
            raise StorageError("Сеть недоступна во время отправки")
        bk.copy_to_network_tmp = failing_copy
        try:
            bk.upload_pending(config, non_interactive=True,
                              reporter=silent_reporter())
            suite.check(False, "ошибка сети должна давать ошибку")
        except StorageError:
            suite.check(True, "ошибка сети -> код ошибки отправки (7)")
        finally:
            bk.copy_to_network_tmp = real_copy
        suite.check(path.is_file(),
                    "20.4-3: локальный архив сохранен после ошибки")
        suite.check(not any(box.network_dir.glob("*.gpg")),
                    "на шаре нет частичных артефактов")

        # Сеть восстановлена — повторная отправка успешна
        report = bk.upload_pending(config, non_interactive=True,
                                   reporter=silent_reporter())
        net_files = sorted(box.network_dir.glob("*.gpg"))
        suite.check(len(net_files) == 1 and not path.exists(),
                    "20.4-6: повторная отправка успешна, локальная удалена")
        suite.check(vr.sha256_file(net_files[0]) == sha,
                    "SHA-256 сетевой копии совпадает с локальным эталоном")

        # --- 20.4-7: повторный запуск не создает дубликатов ---
        report = bk.upload_pending(config, non_interactive=True,
                                   reporter=silent_reporter())
        suite.check(sorted(box.network_dir.glob("*.gpg")) == net_files,
                    "20.4-7: повторный запуск идемпотентен (без дубликатов)")

        # --- 20.4-8: уже отправленный архив не отправляется без нужды ---
        # (локальной копии больше нет — нечего отправлять)
        report = bk.upload_pending(config, non_interactive=True,
                                   reporter=silent_reporter())
        suite.check(report.archives_uploaded == 0,
                    "20.4-8: подтвержденные архивы не переотправляются")

    # --- upload-pending без сети: монтирование обязательно ---
    with Sandbox() as box:
        path, _snap, sha = make_archive(box, "root", b"data" * 500,
                                        compression="gzip")
        common.NETMOUNT_IS_MOUNTED[0] = False
        suite.check(path.is_file(), "локальный архив на месте до попытки")
        try:
            bk.upload_pending(config, non_interactive=True,
                              reporter=silent_reporter())
            suite.check(False, "без шары upload-pending должен дать ошибку")
        except StorageError:
            suite.check(True, "upload-pending без активного монтирования "
                              "-> отказ (независимо от CHECK_NETWORK_MOUNT)")
        suite.check(path.is_file(),
                    "архив сохранен после отказа upload-pending")

    # --- Архив без .sha256: целостность подтверждается проверкой потока ---
    with Sandbox() as box:
        path, _snap, sha = make_archive(box, "root", b"nosha" * 300,
                                        compression="gzip")
        common.NETMOUNT_IS_MOUNTED[0] = True
        Path(str(path) + ".sha256").unlink()
        report = bk.upload_pending(config, non_interactive=True,
                                   reporter=silent_reporter())
        net_files = sorted(box.network_dir.glob("*.gpg"))
        suite.check(len(net_files) == 1,
                    "архив без .sha256 отправлен после полной проверки "
                    "(расшифрование + тест потока)")
        suite.check(vr.sha256_file(net_files[0]) == sha,
                    "SHA-256 вычислен фактически и совпал на шаре")

    # --- Поврежденный архив НЕ отправляется ---
    with Sandbox() as box:
        path, _snap, sha = make_archive(box, "root", b"broken" * 300,
                                        compression="gzip")
        common.NETMOUNT_IS_MOUNTED[0] = True
        data = path.read_bytes()
        path.write_bytes(data[:len(data) // 2])
        try:
            bk.upload_pending(config, non_interactive=True,
                              reporter=silent_reporter())
            suite.check(False, "поврежденный архив не должен отправляться")
        except StorageError:
            suite.check(True, "поврежденный архив -> ошибка отправки")
        suite.check(path.is_file(),
                    "поврежденный архив сохранен локально (не удален)")
        suite.check(not any(box.network_dir.glob("*.gpg")),
                    "поврежденный архив не попал на шару")

    # --- NETWORK_ENABLED=False: upload-pending отказывает (шара не нужна,
    #     но команда отправки работает только с шарой) ---
    with Sandbox() as box:
        path, _snap, sha = make_archive(box, "root", b"nen" * 300,
                                        compression="gzip")
        common.NETMOUNT_IS_MOUNTED[0] = True
        config.NETWORK_ENABLED = False
        try:
            bk.upload_pending(config, non_interactive=True,
                              reporter=silent_reporter())
            suite.check(False, "при NETWORK_ENABLED=false отправка невозможна")
        except StorageError:
            suite.check(True, "NETWORK_ENABLED=false -> отправка отказана")
        finally:
            config.NETWORK_ENABLED = True
        suite.check(path.is_file(), "архив сохранен")

    # --- ТЗ 3.5 (исправление №1): обрыв сети на этапах upload-pending ---
    for stage, label in (
            (1, "перед копированием"),
            (2, "перед финализацией"),
            (3, "перед записью метаданных"),
            (4, "перед удалением локального архива")):
        with Sandbox() as box:
            path, _snap, sha = make_archive(box, "root", b"stage" * 300,
                                            compression="gzip")
            common.NETMOUNT_IS_MOUNTED[0] = True
            real_recheck = bk.recheck_network_mount
            state = {"calls": 0}
            def _flaky(*_a, **_k):
                state["calls"] += 1
                if state["calls"] == stage:
                    raise StorageError("сеть отключилась (имитация)")
                return real_recheck(*_a, **_k)
            bk.recheck_network_mount = _flaky
            try:
                try:
                    bk.upload_pending(config, non_interactive=True,
                                      reporter=silent_reporter())
                    suite.check(False,
                                "обрыв {} должен давать ошибку".format(label))
                except StorageError:
                    suite.check(True,
                                "ТЗ 3.5: обрыв {} -> ошибка".format(label))
            finally:
                bk.recheck_network_mount = real_recheck
            suite.check(path.is_file(),
                        "ТЗ 3.5 ({}): локальный архив сохранен".format(label))
            if stage < 4:
                suite.check(not list(box.network_dir.glob("*.json")),
                            "ТЗ 3.5 ({}): подтвержденных метаданных нет "
                            "→ удаление запрещено".format(label))

    # --- ТЗ 5.4 п.2 (исправление №3): отказ записи метаданных сохраняет
    #     локальный архив и не объявляет отправку успешной ---
    with Sandbox() as box:
        path, _snap, sha = make_archive(box, "root", b"meta-guard" * 300,
                                        compression="gzip")
        common.NETMOUNT_IS_MOUNTED[0] = True
        real_wnm = bk.write_network_metadata
        bk.write_network_metadata = lambda *_a, **_k: False
        try:
            try:
                bk.upload_pending(config, non_interactive=True,
                                  reporter=silent_reporter())
                suite.check(False, "отказ метаданных должен давать ошибку")
            except StorageError:
                suite.check(True, "ТЗ 5.4 п.2: отказ метаданных -> ошибка "
                                  "(отправка не завершена)")
        finally:
            bk.write_network_metadata = real_wnm
        suite.check(path.is_file(),
                    "ТЗ 5.4 п.2: локальный архив сохранен")
        suite.check(list(box.network_dir.glob("*.gpg")) and
                    not list(box.network_dir.glob("*.json")),
                    "сетевой архив есть, метаданных нет")
        # Повторная отправка после устранения — успешна, без дубликатов
        report = bk.upload_pending(config, non_interactive=True,
                                   reporter=silent_reporter())
        suite.check(not path.exists() and
                    len(list(box.network_dir.glob("*.gpg"))) == 1,
                    "ТЗ 5.4 п.7: повторная отправка успешна (без дубликатов)")
        suite.check(len(list(box.network_dir.glob("*.json"))) == 1,
                    "ТЗ 5.4 п.7: метаданные записаны при повторе")



    # --- Выбранный архив: совпадающая копия на шаре не передается повторно ---
    with Sandbox() as box:
        path, _snap, sha = make_archive(
            box, "root", b"already-on-share" * 900, compression="gzip")
        common.NETMOUNT_IS_MOUNTED[0] = True
        remote = box.network_dir / path.name
        remote.write_bytes(path.read_bytes())
        local_meta = md.read_metadata(path)
        assert bk.write_network_metadata(box.network_dir, local_meta, sha)
        real_copy = bk.copy_to_network_tmp
        def forbidden_copy(*args, **kwargs):
            raise AssertionError("повторное копирование на шару недопустимо")
        bk.copy_to_network_tmp = forbidden_copy
        try:
            report = bk.upload_pending(
                config, non_interactive=False, archive_path=path,
                password=PASSWORD, reporter=silent_reporter())
            suite.check(report.archives_uploaded == 1,
                        "выбранный архив уже на шаре: подтвержден без передачи")
            suite.check(not path.exists(),
                        "совпадающая сетевая копия позволяет штатно удалить локальную")
            suite.check(vr.sha256_file(remote) == sha,
                        "содержимое существующего сетевого архива не изменено")
            suite.check((box.network_dir / (path.name + ".json")).is_file(),
                        "метаданные существующего сетевого архива подтверждены")
        finally:
            bk.copy_to_network_tmp = real_copy

    # --- Совпадающий сетевой архив без JSON sidecar: повторно не копируем ---
    with Sandbox() as box:
        path, _snap, sha = make_archive(
            box, "root", b"orphan-on-share" * 600, compression="gzip")
        common.NETMOUNT_IS_MOUNTED[0] = True
        remote = box.network_dir / path.name
        remote.write_bytes(path.read_bytes())
        before = remote.read_bytes()
        real_copy = bk.copy_to_network_tmp
        def forbidden_copy(*args, **kwargs):
            raise AssertionError("повторное копирование на шару недопустимо")
        bk.copy_to_network_tmp = forbidden_copy
        try:
            report = bk.upload_pending(
                config, non_interactive=False, archive_path=path,
                verify_integrity=False, reporter=silent_reporter())
            suite.check(report.archives_uploaded == 1,
                        "совпадающий архив без sidecar считается уже переданным")
        finally:
            bk.copy_to_network_tmp = real_copy
        suite.check(remote.read_bytes() == before,
                    "совпадающий сетевой архив без sidecar не перезаписан")
        suite.check(not path.exists(),
                    "локальная копия удалена после подтверждения совпадающего файла на NAS")

    # --- Выбранный архив: конфликт содержимого запрещает перезапись ---
    with Sandbox() as box:
        path, _snap, _sha = make_archive(
            box, "root", b"local-content" * 700, compression="gzip")
        common.NETMOUNT_IS_MOUNTED[0] = True
        remote = box.network_dir / path.name
        remote.write_bytes(b"pre-existing remote content")
        before = remote.read_bytes()
        try:
            bk.upload_pending(
                config, non_interactive=False, archive_path=path,
                password=PASSWORD, reporter=silent_reporter())
            suite.check(False, "конфликт на шаре должен блокировать отправку")
        except StorageError:
            suite.check(True, "конфликт SHA-256 блокирует отправку")
        suite.check(path.is_file(),
                    "локальный архив сохранен при конфликте")
        suite.check(remote.read_bytes() == before,
                    "существующий сетевой файл не перезаписан")

    # --- Отправка выбранного архива не затрагивает другие pending-архивы ---
    with Sandbox() as box:
        first, *_ = make_archive(box, "first", b"first" * 400,
                                 compression="gzip")
        second, *_ = make_archive(box, "second", b"second" * 400,
                                  compression="gzip")
        common.NETMOUNT_IS_MOUNTED[0] = True
        report = bk.upload_pending(
            config, non_interactive=False, archive_path=first,
            password=PASSWORD, reporter=silent_reporter())
        suite.check(not first.exists() and second.exists(),
                    "ручная отправка затрагивает только выбранный архив")
        suite.check(len(list(box.network_dir.glob("*.gpg"))) == 1,
                    "на шару отправлен только выбранный архив")



    # --- Ручная пакетная отправка без пароля GPG ---
    with Sandbox() as box:
        first, *_ = make_archive(box, "batch-one", b"batch-one" * 500,
                                 compression="gzip")
        second, *_ = make_archive(box, "batch-two", b"batch-two" * 500,
                                  compression="gzip")
        common.NETMOUNT_IS_MOUNTED[0] = True
        ready, already, blocked = bk.preflight_selected_uploads(
            config, [first, second])
        suite.check(len(ready) == 2 and not already and not blocked,
                    "предварительная проверка допускает оба новых архива")
        real_test = bk.verification.test_decrypted_stream
        real_resolve = bk.encryption_module.resolve_decryption_password
        def forbidden_gpg(*_a, **_k):
            raise AssertionError("ручная передача не должна запрашивать пароль")
        bk.verification.test_decrypted_stream = forbidden_gpg
        bk.encryption_module.resolve_decryption_password = forbidden_gpg
        try:
            report = bk.upload_pending(
                config, non_interactive=False,
                archive_path=[first, second], verify_integrity=False,
                reporter=silent_reporter())
            suite.check(report.archives_uploaded == 2,
                        "пакетная ручная отправка передала оба выбранных архива")
            suite.check(not first.exists() and not second.exists(),
                        "локальные копии удалены только после подтверждения передачи")
        finally:
            bk.verification.test_decrypted_stream = real_test
            bk.encryption_module.resolve_decryption_password = real_resolve

    # --- Предварительная проверка показывает конфликт и не разрешает перезапись ---
    with Sandbox() as box:
        conflict, *_ = make_archive(box, "batch-conflict", b"local" * 500,
                                    compression="gzip")
        valid, *_ = make_archive(box, "batch-valid", b"valid" * 500,
                                 compression="gzip")
        common.NETMOUNT_IS_MOUNTED[0] = True
        remote = box.network_dir / conflict.name
        remote.write_bytes(b"different pre-existing data")
        ready, already, blocked = bk.preflight_selected_uploads(
            config, [conflict, valid])
        suite.check(valid in ready and not already and
                    any(name == conflict.name for name, _reason in blocked),
                    "предварительная проверка отделяет конфликт от доступного архива")
        suite.check(remote.read_bytes() == b"different pre-existing data",
                    "предварительная проверка не меняет существующий сетевой файл")

    # --- Ручной выбор допускает архив из любого каталога списка TUI ---
    with Sandbox() as box:
        source, _snap, _sha = make_archive(
            box, "outside-config", b"selected-outside-config" * 200,
            compression="gzip")
        other_dir = box.root / "another-local-archive-dir"
        other_dir.mkdir()
        moved = other_dir / source.name
        moved.write_bytes(source.read_bytes())
        for suffix in (".json", ".sha256"):
            sidecar = Path(str(source) + suffix)
            if sidecar.exists():
                Path(str(moved) + suffix).write_bytes(sidecar.read_bytes())
        common.NETMOUNT_IS_MOUNTED[0] = True
        ready, already, blocked = bk.preflight_selected_uploads(
            config, [moved])
        suite.check(ready == [moved.resolve()] and not already and not blocked,
                    "ручной выбор допускает файл вне каталогов фонового поиска")

    return suite.summary()


if __name__ == "__main__":
    sys.exit(run_main(main))
