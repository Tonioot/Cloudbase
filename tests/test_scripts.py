"""start.sh and install.sh run small Python snippets against the backend (password reset,
first-run credentials). Nothing else imports those names, so moving a module silently breaks them.
This test pulls the snippets out of the scripts and checks that every import still resolves."""
import importlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = [ROOT / "start.sh", ROOT / "install.sh", *sorted((ROOT / "scripts").glob("*.sh")),
           ROOT / "scripts" / "cloudbase-issue-cert"]

HEREDOC = re.compile(r"<<-?\s*['\"]?(\w*PY\w*)['\"]?[^\n]*\n(.*?)\n\s*\1\b", re.S)
INLINE = re.compile(r"python3?\s+-c\s+(?:\"((?:[^\"\\]|\\.)*)\"|'([^']*)')", re.S)
FROM_IMPORT = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+([\w, ]+)", re.M)
IMPORT = re.compile(r"(?:^|;)\s*import\s+([\w.]+(?:\s*,\s*[\w.]+)*)", re.M)


def _snippets(text: str):
    for m in HEREDOC.finditer(text):
        yield m.group(2)
    for m in INLINE.finditer(text):
        yield m.group(1) or m.group(2)


def _imports(code: str):
    for m in FROM_IMPORT.finditer(code):
        names = [n.strip() for n in m.group(2).split(",") if n.strip()]
        yield m.group(1), names
    for m in IMPORT.finditer(code):
        for mod in m.group(1).split(","):
            yield mod.strip(), []


CASES = []
for script in SCRIPTS:
    if script.exists():
        text = script.read_text(encoding="utf-8", errors="replace")
        for code in _snippets(text):
            for module, names in _imports(code):
                CASES.append((script.name, module, tuple(names)))


def test_the_scripts_do_contain_backend_imports():
    # guards the extraction itself: start.sh/install.sh are known to import backend modules
    assert any(m.startswith("core") for _, m, _ in CASES), CASES


def _is_submodule(package: str, name: str) -> bool:
    try:
        importlib.import_module(f"{package}.{name}")
        return True
    except ModuleNotFoundError:
        return False


@pytest.mark.parametrize("script,module,names", sorted(set(CASES)))
def test_script_import_resolves(script, module, names):
    mod = importlib.import_module(module)
    for name in names:
        assert hasattr(mod, name) or _is_submodule(module, name), f"{script}: cannot import {name} from {module}"
