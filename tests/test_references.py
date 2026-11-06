"""Static check for references the import tests cannot see.

Moving code around turns `from apps import git` + `git._sync_app_source(...)` into an AttributeError
only at the moment that line runs. This test resolves every `from X import name` and every
`module.name` (for modules imported from the backend) against what that module really defines.
"""
import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"


def _module_files():
    out = {}
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND)
        if "venv" in rel.parts or "__pycache__" in rel.parts:
            continue
        parts = rel.with_suffix("").parts
        out[".".join(parts[:-1] if parts[-1] == "__init__" else parts)] = path
    return out


FILES = _module_files()
TREES = {m: ast.parse(p.read_text(encoding="utf-8")) for m, p in FILES.items()}


def _top_level_names(tree) -> set[str]:
    names = set()

    def visit(body):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
            elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    names.update(x.id for x in ast.walk(t) if isinstance(x, ast.Name))
            elif isinstance(n, ast.Import):
                names.update((a.asname or a.name.split(".")[0]) for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                names.update((a.asname or a.name) for a in n.names)
            elif isinstance(n, (ast.Try,)):
                visit(n.body); [visit(h.body) for h in n.handlers]; visit(n.orelse); visit(n.finalbody)
            elif isinstance(n, (ast.If, ast.For, ast.While, ast.With)):
                visit(n.body); visit(getattr(n, "orelse", []))
    visit(tree.body)
    return names


DEFINED = {m: _top_level_names(t) for m, t in TREES.items()}


def _submodules(pkg: str) -> set[str]:
    return {m.split(".")[-1] for m in FILES if m.startswith(pkg + ".") and m.count(".") == pkg.count(".") + 1}


def _problems(mod: str) -> list[str]:
    tree, out = TREES[mod], []
    bound_modules: dict[str, str] = {}   # local alias -> backend module it refers to
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module in FILES:
                for a in node.names:
                    local = a.asname or a.name
                    if a.name in DEFINED[node.module]:
                        continue
                    if f"{node.module}.{a.name}" in FILES:        # `from pkg import submodule`
                        bound_modules[local] = f"{node.module}.{a.name}"
                    elif a.name in _submodules(node.module):
                        bound_modules[local] = f"{node.module}.{a.name}"
                    else:
                        out.append(f"{mod}:{node.lineno}: cannot import '{a.name}' from {node.module}")
                    continue
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.asname and a.name in FILES:
                    bound_modules[a.asname] = a.name
    # a local variable or parameter with the same name hides the module: ignore such names
    locals_ = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a_ = fn.args
            locals_ |= {x.arg for x in a_.args + a_.kwonlyargs + a_.posonlyargs}
            locals_ |= {x.arg for x in (a_.vararg, a_.kwarg) if x}
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            locals_.add(n.id)
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in bound_modules:
            alias = n.value.id
            if alias in locals_ and alias not in {x for x in bound_modules if x not in locals_}:
                continue
            target = bound_modules[alias]
            if n.attr not in DEFINED[target] and n.attr not in _submodules(target):
                out.append(f"{mod}:{n.lineno}: {alias}.{n.attr} does not exist in {target}")
    return out


@pytest.mark.parametrize("module", sorted(FILES))
def test_references_resolve(module):
    problems = _problems(module)
    assert not problems, "\n".join(problems)
