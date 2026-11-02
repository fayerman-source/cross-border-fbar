"""The tool must run on Windows, where the default text encoding is not UTF-8 and fcntl does not exist."""

import ast
import multiprocessing
from pathlib import Path

from cross_border_fbar.filelock import exclusive

ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted((ROOT / "src").rglob("*.py"))


def _calls_without_encoding(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in ("read_text", "write_text", "open"):
            continue
        if isinstance(func, ast.Attribute) and getattr(func.value, "id", "") == "Image":
            continue
        modes = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if name == "open" and any("b" in m for m in modes):
            continue
        if not any(k.arg == "encoding" for k in node.keywords):
            yield node.lineno


def test_all_text_io_names_its_encoding():
    """Korean and Thai data crashed or garbled wherever the platform default isn't UTF-8."""
    missing = [
        f"{path.relative_to(ROOT)}:{line}"
        for path in SOURCES
        for line in _calls_without_encoding(ast.parse(path.read_bytes()))
    ]
    assert missing == []


def test_no_platform_only_imports_outside_the_lock():
    for path in SOURCES:
        if path.name == "filelock.py":
            continue
        for node in ast.walk(ast.parse(path.read_bytes())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not {"fcntl", "msvcrt"} & set(names), path


def _hold(path, entered, release):
    with exclusive(Path(path)):
        entered.set()
        release.wait(30)


def test_lock_excludes_another_process_and_releases(tmp_path):
    lock = tmp_path / "nested" / "ledger.lock"
    ctx = multiprocessing.get_context("spawn")
    entered, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold, args=(str(lock), entered, release))
    holder.start()
    assert entered.wait(60)
    other_entered = ctx.Event()
    other_release = ctx.Event()
    waiter = ctx.Process(target=_hold, args=(str(lock), other_entered, other_release))
    waiter.start()
    assert not other_entered.wait(2)  # still held by the first process
    release.set()
    assert other_entered.wait(60)  # free once released
    other_release.set()
    holder.join(30)
    waiter.join(30)
    with exclusive(lock):
        pass
