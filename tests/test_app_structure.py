"""Safety nets for restructuring the backend.

- every module still imports
- the set of HTTP/WebSocket routes does not change by accident (tests/routes_snapshot.txt)
- no route is shadowed by an earlier one (include order matters in FastAPI)
- the import layering only gets cleaner (tests/layering_baseline.json is a ratchet)

After an intended change, review the diff and regenerate the snapshots with
    UPDATE_SNAPSHOTS=1 pytest tests/test_app_structure.py
"""
import ast
import importlib
import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
TESTS = Path(__file__).resolve().parent
UPDATE = bool(os.environ.get("UPDATE_SNAPSHOTS"))


def _backend_modules() -> dict[str, Path]:
    """dotted module name -> file ("pkg" -> pkg/__init__.py)."""
    mods = {}
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND)
        if rel.parts[0] in ("venv", "__pycache__") or "venv" in rel.parts:
            continue
        parts = rel.with_suffix("").parts
        mods[".".join(parts[:-1] if parts[-1] == "__init__" else parts)] = path
    return mods


MODULE_FILES = _backend_modules()
MODULES = list(MODULE_FILES)


# ── imports ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module):
    importlib.import_module(module)


# ── routes ────────────────────────────────────────────────────────────────────
def _flatten(routes):
    """All routes in registration order, whatever FastAPI/Starlette version wraps them."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:                      # an included router (newer FastAPI wraps it)
            yield from _flatten(inner.routes)
        elif type(route).__name__ == "Mount":
            yield route
        else:
            yield route


def _describe(route) -> str:
    kind = type(route).__name__
    path = getattr(route, "path", "?")
    if "WebSocket" in kind:
        return f"WS    {path}"
    if kind == "Mount":
        return f"MOUNT {path}"
    methods = ",".join(sorted(m for m in (getattr(route, "methods", None) or []) if m != "HEAD"))
    return f"{methods:<7} {path}"


@pytest.fixture(scope="module")
def routes():
    import main
    return list(_flatten(main.app.routes))


def test_route_snapshot(routes):
    current = sorted({_describe(r) for r in routes})
    snapshot = TESTS / "routes_snapshot.txt"
    if UPDATE or not snapshot.exists():
        snapshot.write_text("\n".join(current) + "\n", encoding="utf-8")
        if not UPDATE:
            pytest.skip("routes_snapshot.txt created")
    expected = snapshot.read_text(encoding="utf-8").splitlines()
    added = sorted(set(current) - set(expected))
    removed = sorted(set(expected) - set(current))
    assert not added and not removed, f"routes changed.\n  added: {added}\n  removed: {removed}"


def test_no_route_is_shadowed_by_an_earlier_one(routes):
    """A request must reach the route it was written for, not an earlier catch-all."""
    http = [r for r in routes if hasattr(r, "path_regex") and "WebSocket" not in type(r).__name__]
    shadowed = []
    for target in http:
        concrete = re.sub(r"\{[^}]+\}", "x", target.path)
        for method in (getattr(target, "methods", None) or []):
            if method == "HEAD":
                continue
            for route in http:
                if method in (getattr(route, "methods", None) or []) and route.path_regex.match(concrete):
                    if route is not target and route.path != target.path:
                        shadowed.append(f"{method} {target.path} is answered by {route.path}")
                    break
    assert not shadowed, "shadowed routes:\n  " + "\n  ".join(sorted(set(shadowed)))


# ── layering ──────────────────────────────────────────────────────────────────
def _local_imports(path: Path, module_names: set[str]):
    """Yield (imported backend modules, is_inside_function), once per import statement."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    toplevel = {id(n) for n in tree.body}
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            targets = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        found = {t for t in targets if t in module_names}
        if found:
            yield found, id(node) not in toplevel


# modules whose job is to wire routers together
WIRING = {"main", "lifespan"}


def _area(module: str) -> str:
    """routers.applications.crud -> routers.applications; anything else is its own area."""
    parts = module.split(".")
    return ".".join(parts[:2]) if parts[0] == "routers" and len(parts) > 1 else module


def _layering() -> dict:
    names = set(MODULES)
    upward, lazy = [], {}
    for mod in MODULES:
        for targets, inside_function in _local_imports(MODULE_FILES[mod], names):
            targets = targets - {mod}
            if not targets:
                continue
            if inside_function:
                lazy[mod] = lazy.get(mod, 0) + 1
            # Logic must not depend on HTTP routers, and routers must not depend on each
            # other: shared logic belongs in a service. Only the wiring modules (main, lifespan) may import routers.
            for target in targets:
                if target.startswith("routers.") and mod not in WIRING and _area(target) != _area(mod):
                    upward.append(f"{mod} -> {_area(target)}")
    return {"upward_imports": sorted(set(upward)), "function_level_imports": dict(sorted(lazy.items()))}


def test_layering_only_gets_cleaner():
    current = _layering()
    baseline_file = TESTS / "layering_baseline.json"
    if UPDATE or not baseline_file.exists():
        baseline_file.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        if not UPDATE:
            pytest.skip("layering_baseline.json created")
    baseline = json.loads(baseline_file.read_text(encoding="utf-8"))

    new_upward = sorted(set(current["upward_imports"]) - set(baseline["upward_imports"]))
    fixed_upward = sorted(set(baseline["upward_imports"]) - set(current["upward_imports"]))
    worse = {m: (baseline["function_level_imports"].get(m, 0), n)
             for m, n in current["function_level_imports"].items()
             if n > baseline["function_level_imports"].get(m, 0)}
    better = {m: (n, current["function_level_imports"].get(m, 0))
              for m, n in baseline["function_level_imports"].items()
              if current["function_level_imports"].get(m, 0) < n}

    assert not new_upward, f"modules must not import routers (move shared logic to a service): {new_upward}"
    assert not worse, f"more imports inside functions (hidden dependencies) in: {worse}"
    # ratchet: progress must be locked in, otherwise it can silently regress later
    assert not fixed_upward and not better, (
        "layering improved, lock it in with UPDATE_SNAPSHOTS=1 pytest tests/test_app_structure.py\n"
        f"  upward imports fixed: {fixed_upward}\n  fewer function-level imports: {better}")
