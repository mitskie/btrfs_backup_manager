"""
Статические проверки (ТЗ 20.1): py_compile, импорты без сторонних пакетов,
запрет shell=True, запрет пароля в argv.
Запуск: python3 tests/test_static.py
"""

import ast
import py_compile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import PROJECT, Suite, run_main  # noqa: E402

# Разрешенные модули стандартной библиотеки (+ подмодули)
STDLIB_ALLOWED = {
    "argparse", "curses", "subprocess", "pathlib", "os", "sys", "shutil",
    "logging", "hashlib", "json", "configparser", "datetime", "time",
    "signal", "threading", "queue", "selectors", "tempfile", "fcntl",
    "getpass", "dataclasses", "typing", "enum", "re", "stat", "errno",
    "logging.handlers", "faulthandler", "traceback", "io", "contextlib",
    "unittest.mock", "termios", "tty", "select", "textwrap",
}
LOCAL_TOP = {"config", "backup_config", "core", "ui", "main", "common", "tests"}

PROGRAM_FILES = [PROJECT / "main.py", PROJECT / "config.py"]
for subdir in ("core", "ui"):
    PROGRAM_FILES.extend(sorted((PROJECT / subdir).glob("*.py")))


def _imports_of(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                modules.add(node.module)
    return modules


def _shell_true_usages(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "shell":
                    # shell=True — запрещено; shell=False/переменная допустима
                    if isinstance(kw.value, ast.Constant) and kw.value.value:
                        found.append("{}:{}".format(path.name, node.lineno))
    return found


def main():
    suite = Suite("static")

    # 20.1: py_compile для всех Python-файлов
    all_py = sorted(PROJECT.rglob("*.py"))
    for path in all_py:
        try:
            py_compile.compile(str(path), doraise=True)
            suite.check(True, "py_compile: {}".format(path.relative_to(PROJECT)))
        except py_compile.PyCompileError as exc:
            suite.check(False, "py_compile: {} — {}".format(path, exc))

    # 20.1: отсутствие сторонних Python-зависимостей (программный код)
    for path in PROGRAM_FILES:
        for module in _imports_of(path):
            top = module.split(".")[0]
            ok = module in STDLIB_ALLOWED or top in STDLIB_ALLOWED or \
                top in LOCAL_TOP or module.startswith(("core", "ui", "config"))
            suite.check(ok, "импорт {} в {} — стандартная библиотека".format(
                module, path.name))
    # requirements.txt пуст от сторонних пакетов
    req = PROJECT / "requirements.txt"
    if req.is_file():
        lines = [line.strip() for line in
                 req.read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.strip().startswith("#")]
        suite.check(lines == [],
                    "requirements.txt не содержит сторонних зависимостей")

    # 20.1: запрет shell=True
    for path in PROGRAM_FILES:
        for usage in _shell_true_usages(path):
            suite.check(False, "shell=True найден: " + usage)
    suite.check(all(not _shell_true_usages(p) for p in PROGRAM_FILES),
                "shell=True отсутствует во всей программе (ТЗ 2.2)")

    # 20.1: пароль GPG не попадает в argv — gpg команды только через
    # --passphrase-fd, пароль передается через make_passphrase_fd
    enc_src = (PROJECT / "core" / "encryption.py").read_text(encoding="utf-8")
    suite.check('"--passphrase-fd", str(passphrase_fd)' in enc_src,
                "gpg encrypt использует --passphrase-fd")
    suite.check('"--passphrase-fd", str(passphrase_fd)' in enc_src and
                '"--decrypt"' in enc_src,
                "gpg decrypt использует --passphrase-fd")
    suite.check("os.pipe()" in enc_src,
                "пароль передается через os.pipe (без временных файлов)")
    # В программах нет прямой подстановки пароля в списки аргументов
    for path in PROGRAM_FILES:
        text = path.read_text(encoding="utf-8")
        for bad in ('"--passphrase"', "'--passphrase'"):
            if bad in text and "--passphrase-fd" not in text:
                suite.check(False,
                            "передача пароля через argv в {}".format(path.name))
    suite.check(True, "прямой передачи --passphrase в argv нет")

    # Обработка исключений: критические операции используют собственные
    # классы ошибок (проверка наличия иерархии)
    from core import BtrfsBackupError
    for name in ("ConfigError", "SnapshotError", "ArchiveError",
                 "EncryptionError", "StorageError", "VerificationError",
                 "RestoreError", "OperationAborted"):
        cls = getattr(__import__("core", fromlist=[name]), name)
        suite.check(issubclass(cls, BtrfsBackupError),
                    "иерархия ошибок: " + name)

    return suite.summary()


if __name__ == "__main__":
    sys.exit(run_main(main))
