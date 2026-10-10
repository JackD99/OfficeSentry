"""How the modules depend on each other: no module-level import cycles, and reports never import ``web``.

A cycle works only while every module in it happens to be imported in the right order, and it hides
which module is the base. Split the shared part into its own module (as ``checks/base.py`` and
``reports/registry.py`` do) rather than importing late or at the bottom of a file.
"""

import ast
from pathlib import Path

import officesentry

SOURCE = Path(officesentry.__file__).parent


def _modules() -> dict[str, Path]:
    out = {}
    for path in SOURCE.rglob("*.py"):
        parts = list(path.relative_to(SOURCE.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        out[".".join(parts)] = path
    return out


def _imports(name: str, path: Path, modules: dict[str, Path], top_level: bool) -> set[str]:
    """The project modules ``name`` imports: at module level only, or anywhere."""
    package = name.split(".") if path.name == "__init__.py" else name.split(".")[:-1]
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body if top_level else ast.walk(tree):
        if top_level and isinstance(node, ast.If | ast.Try):  # imports guarded at module level still run
            nodes.extend(ast.walk(node))
        else:
            nodes.append(node)
    found = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name in modules)
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            base = package[:len(package) - node.level + 1] if node.level else []
            source = ".".join(base + ([node.module] if node.module else [])) if node.level else node.module
            for alias in node.names:
                if f"{source}.{alias.name}" in modules:
                    found.add(f"{source}.{alias.name}")
                elif source in modules:
                    found.add(source)
    return found - {name}


def _cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected groups of more than one module (Tarjan)."""
    index, low, stack, on_stack, groups = {}, {}, [], set(), []

    def visit(v):
        index[v] = low[v] = len(index)
        stack.append(v)
        on_stack.add(v)
        for w in graph.get(v, ()):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            group = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                group.append(w)
                if w == v:
                    break
            if len(group) > 1:
                groups.append(sorted(group))

    for v in sorted(graph):
        if v not in index:
            visit(v)
    return groups


def test_no_module_level_import_cycles():
    modules = _modules()
    graph = {name: _imports(name, path, modules, top_level=True) for name, path in modules.items()}
    assert _cycles(graph) == []


def test_the_scan_finds_a_cycle():
    assert _cycles({"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": {"a"}}) == [["a", "b", "c"]]


def test_reports_and_checks_never_import_the_web_package():
    modules = _modules()
    for name, path in modules.items():
        if name.startswith(("officesentry.reports", "officesentry.checks")) or name == "officesentry.formatting":
            web = {m for m in _imports(name, path, modules, top_level=False) if m.startswith("officesentry.web")}
            assert not web, f"{name} imports {sorted(web)}"


def test_old_module_names_still_work():
    """Branches written before the split import from ``rules`` and ``user_profile``."""
    from officesentry import checks, rules
    from officesentry.checks.identity import mfa_for_all_policies
    from officesentry.reports import profile, user_profile
    from officesentry.reports.profile.sign_ins import sign_ins_part

    assert rules.RULES is checks.RULES and rules.evaluate is checks.evaluate
    assert rules.mfa_for_all_policies is mfa_for_all_policies
    assert user_profile.build is profile.build and user_profile.sign_ins_part is sign_ins_part
