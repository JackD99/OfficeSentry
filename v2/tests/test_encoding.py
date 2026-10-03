"""Text files are read and written as UTF-8 on every platform.

Without an explicit encoding Python uses the locale's, which is cp1252 on most
Windows machines: the live-test fixture and our own source files then fail to
read. CI runs on Linux, where the default happens to be UTF-8, so check the code.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT_IO = {"read_text", "write_text"}


def _is_binary(call: ast.Call) -> bool:
    modes = [kw.value for kw in call.keywords if kw.arg == "mode"] + call.args[1:2]
    return any(isinstance(m, ast.Constant) and isinstance(m.value, str) and "b" in m.value for m in modes)


def _missing_encoding(call: ast.Call) -> bool:
    if any(kw.arg == "encoding" for kw in call.keywords):
        return False
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in TEXT_IO:
        return True
    is_builtin_open = isinstance(func, ast.Name) and func.id == "open"
    is_fdopen = isinstance(func, ast.Attribute) and func.attr == "fdopen"
    return (is_builtin_open or is_fdopen) and not _is_binary(call)


def test_text_files_are_opened_as_utf8():
    missing = []
    for folder in ("officesentry", "tests", "scripts"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            missing += [f"{path.relative_to(ROOT)}:{node.lineno}" for node in ast.walk(tree)
                        if isinstance(node, ast.Call) and _missing_encoding(node)]
    assert not missing, "Pass encoding=\"utf-8\" to: " + ", ".join(missing)
