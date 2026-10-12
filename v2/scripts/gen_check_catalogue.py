"""Generate docs/checks.md: every check, data source, detail report and permission.

Run from the v2 directory:

    PYTHONPATH=. python scripts/gen_check_catalogue.py > docs/checks.md

A development script, not part of the package. The output is deterministic
(everything sorted), so a regenerated file only changes when the code does.

For each check it lists the collectors whose snapshots it reads. That is found
by reading the check's source (and the source of every Office Sentry function
it calls) for ``snap.objects("<collector>", ...)`` calls, so it stays true as
checks change.
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
import textwrap
import types
from collections.abc import Callable

from officesentry.checks import RULES
from officesentry.collectors import REGISTRY, core
from officesentry.entra import PERMISSIONS
from officesentry.reports.data_reports import REPORTS

PACKAGE = "officesentry"
SNAPSHOT_READS = {"objects", "has"}
# Text in a check's "not checked" reasons that names a licence requirement.
LICENCE_RE = re.compile(
    r"Entra ID P[12]|\b(?:no|needs?|without)\b[^.;]*?\b(?:Intune|Exchange|SharePoint|Defender|Entra|Microsoft 365)\b"
    r"[^.;]*?licen[cs]e",
    re.IGNORECASE,
)


def _tree(fn: Callable) -> ast.AST | None:
    """The function's syntax tree, without its decorators (a check's title and recommendation)."""
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError):
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.decorator_list = []
    return tree


def _resolve(node: ast.expr, namespace: dict) -> object | None:
    """The object a call's target names, for ``name(...)`` and ``module.name(...)``."""
    if isinstance(node, ast.Name):
        return namespace.get(node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        owner = namespace.get(node.value.id)
        if isinstance(owner, types.ModuleType):
            return getattr(owner, node.attr, None)
    return None


def _own_function(obj: object) -> bool:
    return isinstance(obj, types.FunctionType) and (obj.__module__ or "").split(".")[0] == PACKAGE


def _walk(fn: Callable, seen: set) -> list[tuple[Callable, ast.AST]]:
    """``fn`` and every Office Sentry function it calls, directly or indirectly."""
    if fn in seen:
        return []
    seen.add(fn)
    tree = _tree(fn)
    if tree is None:
        return []
    found = [(fn, tree)]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = _resolve(node.func, fn.__globals__)
            if _own_function(target):
                found.extend(_walk(target, seen))
    return found


def collectors_read(fn: Callable) -> list[str]:
    """Collector keys whose snapshots ``fn`` (or anything it calls) reads."""
    keys = set()
    for _, tree in _walk(fn, set()):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in SNAPSHOT_READS
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value in REGISTRY
            ):
                keys.add(node.args[0].value)
    return sorted(keys)


def licence_notes(fn: Callable) -> list[str]:
    """Reasons the check gives for being "not checked" that name a licence."""
    notes = set()
    for helper, tree in _walk(fn, set()):
        if helper.__module__ != fn.__module__:
            continue
        verdicts = {id(c) for node in ast.walk(tree) if _is_verdict(node) for c in ast.walk(node)}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in verdicts
                and LICENCE_RE.search(node.value)
            ):
                notes.add(node.value.strip())
    return sorted(notes)


def _is_verdict(node: ast.AST) -> bool:
    """A finding or a ``("pass" | "fail", ...)`` result: its text is a result, not a "not checked" reason."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id == "Finding"
    return (
        isinstance(node, ast.Tuple)
        and bool(node.elts)
        and isinstance(node.elts[0], ast.Constant)
        and node.elts[0].value in ("pass", "fail")
    )


def _cell(text: object) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _codes(items) -> str:
    return ", ".join(f"`{i}`" for i in items) or "none"


def main_reports() -> dict:
    """The detail reports built from the main app's data (an add-on's report is documented with the add-on)."""
    sources = set(core())
    return {key: value for key, value in REPORTS.items() if sources & set(value[2])}


def permission_users() -> dict[str, list[str]]:
    users: dict[str, list[str]] = {p: [] for p in PERMISSIONS}
    for key in sorted(core()):
        for permission in REGISTRY[key].permissions:
            users.setdefault(permission, []).append(key)
    return users


def render() -> str:
    out: list[str] = [
        "# Check catalogue",
        "",
        "<!-- Generated by scripts/gen_check_catalogue.py. Don't edit by hand. -->",
        "",
        "Generated from the code. To refresh it, run this from `v2/`:",
        "",
        "```bash",
        "PYTHONPATH=. python scripts/gen_check_catalogue.py > docs/checks.md",
        "```",
        "",
        f"{len(RULES)} checks, {len(core())} data sources (collectors) and {len(main_reports())} detail reports.",
        "",
        "A check reads only the data its collectors stored. When that data is missing, or the tenant's licence "
        "can't provide it, the check shows as **not checked** with the reason, instead of passing or failing.",
        "",
        "## Checks",
        "",
    ]
    categories: dict[str, list] = {}
    for r in RULES:
        categories.setdefault(r.category, []).append(r)
    for category in sorted(categories):
        out += [
            f"### {category}",
            "",
            "| Check | Reads data from | Licence note | Recommendation |",
            "|---|---|---|---|",
        ]
        for r in sorted(categories[category], key=lambda r: r.key):
            notes = "; ".join(licence_notes(r.check))
            out.append(
                f"| **{_cell(r.title)}**<br>`{r.key}` | {_codes(collectors_read(r.check))} "
                f"| {_cell(notes)} | {_cell(r.recommendation)} |"
            )
        out.append("")

    out += [
        "## Data sources (collectors)",
        "",
        "Each collector reads one area of a tenant and stores a snapshot. They run every night unless marked "
        "*on demand*.",
        "",
        "| Key | Title | What it reads | Permissions |",
        "|---|---|---|---|",
    ]
    for key in sorted(core()):
        cls = REGISTRY[key]
        title = _cell(cls.title) + ("" if cls.nightly else " (*on demand*)")
        out.append(f"| `{key}` | {title} | {_cell(cls.description)} | {_codes(cls.permissions)} |")
    out.append("")

    out += [
        "## Detail reports",
        "",
        "Each one is a page at `/tenants/<id>/data/<key>` with an XLSX export.",
        "",
        "| Key | Title | Description | Data from |",
        "|---|---|---|---|",
    ]
    for key, (title, description, sources, _builder) in sorted(main_reports().items()):
        out.append(f"| `{key}` | {_cell(title)} | {_cell(description)} | {_codes(sorted(sources))} |")
    out.append("")

    out += [
        "## Permissions",
        "",
        "Every application permission the app requests (all read-only) and the collectors that use it.",
        "",
        "| Permission | API | Purpose | Used by |",
        "|---|---|---|---|",
    ]
    users = permission_users()
    for permission in sorted(users):
        if permission in PERMISSIONS:
            resource, name, purpose = PERMISSIONS[permission]
            api = resource.name.removeprefix("Office 365 ")
        else:
            api, name, purpose = "?", permission, "Not in the app's permission list"
        used = _codes(users[permission]) if users[permission] else "not used by a collector yet"
        out.append(f"| `{name}` | {api} | {_cell(purpose)} | {used} |")
    out.append("")
    return "\n".join(out)


def main() -> None:
    sys.stdout.write(render())


if __name__ == "__main__":
    main()
