"""Serving the panel's pages so that switching between them is quick.

The panel is a handful of HTML pages that each load their own CSS and a tree of JavaScript modules,
with no build step. Left alone, every page switch cost: a revalidation of every file (they were served
`no-cache`), a waterfall of 4-6 round trips because a module only reveals what it imports once it has
been downloaded, and a blocking request to Google Fonts. This module fixes that at the point where
the HTML leaves the server:

  * every css/js URL gets `?v=<hash of the content of all assets>`, and files requested that way are
    cached for a year (see routers/frontend.py). A changed file changes the hash, so nothing goes stale;
  * an import map points each module at its versioned URL, so `import './shell.js'` inside a module
    also hits the cache;
  * the whole module graph a page needs is announced with <link rel="modulepreload">, so the browser
    fetches it in one parallel batch;
  * the fonts are served from /fonts and the regular faces preloaded;
  * the page background is set inline, so the page is never white while the stylesheet is on its way.
"""
import functools
import hashlib
import json
import os
import re

ASSET_DIRS = ("css", "js")

# The page colour before the stylesheet has arrived. Must equal --bg in css/variables.css
# (tests/test_frontend.py checks that they do).
BG_DARK = "#08090a"
BG_LIGHT = "#fbfbfa"

_IMPORT = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*)['"]([^'"]+\.js)['"]""")
_TAG = re.compile(r"<(?:link|script)\b[^>]*>", re.I)
_ATTR = re.compile(r"""\b(href|src)=(["'])(?:\./|/)?((?:css|js)/[^"'?#]+)\2""")
_MODULE_SCRIPT = re.compile(r"""<script\s+type=["']module["'][^>]*>(.*?)</script>""", re.S | re.I)
_TITLE_END = re.compile(r"</title>", re.I)
_HEAD_START = re.compile(r"<head[^>]*>", re.I)

# (path, size, mtime) -> sha1 of the content; a file is only re-read when it changed
_content_hashes: dict[tuple, str] = {}


def asset_files(root: str) -> list[str]:
    """css/… and js/… files, relative to root, sorted."""
    out = []
    for directory in ASSET_DIRS:
        base = os.path.join(root, directory)
        for dirpath, _dirs, names in os.walk(base):
            for name in names:
                out.append(os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/"))
    return sorted(out)


def version(root: str) -> str:
    """Hash of the content of every css/js file: the same content always gives the same version."""
    parts = []
    for rel in asset_files(root):
        path = os.path.join(root, rel)
        stat = os.stat(path)
        key = (path, stat.st_size, stat.st_mtime_ns)
        digest = _content_hashes.get(key)
        if digest is None:
            with open(path, "rb") as f:
                digest = hashlib.sha1(f.read()).hexdigest()
            _content_hashes[key] = digest
        parts.append(f"{rel}:{digest}")
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:10]


def import_map(root: str, ver: str) -> dict[str, str]:
    return {f"/{rel}": f"/{rel}?v={ver}" for rel in asset_files(root) if rel.startswith("js/")}


def _resolve(spec: str, from_dir: str) -> str | None:
    """A module specifier as a path relative to root ('js/api.js'), or None for anything not ours."""
    if spec.startswith("/"):
        path = spec.lstrip("/")
    elif spec.startswith("."):
        path = os.path.normpath(os.path.join(from_dir, spec)).replace(os.sep, "/")
    else:
        return None
    return path if path.startswith("js/") else None


@functools.lru_cache(maxsize=64)
def _graph(root: str, ver: str, entries: tuple[str, ...]) -> tuple[str, ...]:
    """The modules the entries import, directly or not (static imports only), entries first."""
    seen: list[str] = []

    def visit(rel: str) -> None:
        if rel in seen:
            return
        seen.append(rel)
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as f:
                source = f.read()
        except OSError:
            return
        for spec in _IMPORT.findall(source):
            target = _resolve(spec, os.path.dirname(rel))
            if target:
                visit(target)

    for entry in entries:
        visit(entry)
    return tuple(seen)


def module_graph(root: str, entries: set[str]) -> list[str]:
    return list(_graph(root, version(root), tuple(sorted(entries))))


def font_preloads(root: str) -> list[str]:
    """The regular (latin) faces: nearly every page needs them straight away."""
    try:
        names = sorted(os.listdir(os.path.join(root, "fonts")))
    except OSError:
        return []
    return [f"/fonts/{n}" for n in names if re.match(r"^geist(-mono)?-latin\.[0-9a-f]+\.woff2$", n)]


def boot_style() -> str:
    return (f"<style>html{{background:{BG_DARK}}}html[data-theme=light]{{background:{BG_LIGHT}}}"
            f"html[data-theme=dark]{{background:{BG_DARK}}}"
            f"@media (prefers-color-scheme:light){{html:not([data-theme]){{background:{BG_LIGHT}}}}}</style>")


@functools.lru_cache(maxsize=32)
def _render(root: str, name: str, ver: str, mtime_ns: int) -> str:
    with open(os.path.join(root, name), encoding="utf-8") as f:
        html = f.read()

    # 1. versioned URLs for the stylesheets and the classic scripts
    html = _TAG.sub(lambda tag: _ATTR.sub(lambda a: f"{a.group(1)}={a.group(2)}/{a.group(3)}?v={ver}{a.group(2)}", tag.group(0)), html)

    # 2. everything the page's module scripts need, announced up front
    entries = set()
    for block in _MODULE_SCRIPT.findall(html):
        for spec in _IMPORT.findall(block):
            target = _resolve(spec, "")
            if target:
                entries.add(target)
    graph = _graph(root, ver, tuple(sorted(entries)))

    head = [boot_style(),
            '<script type="importmap">' + _json(import_map(root, ver)) + "</script>"]
    head += [f'<link rel="modulepreload" href="/{rel}?v={ver}">' for rel in graph]
    head += [f'<link rel="preload" href="{href}" as="font" type="font/woff2" crossorigin>' for href in font_preloads(root)]

    # after the title, so <meta charset> and the viewport stay at the very start of the document
    anchor = _TITLE_END.search(html) or _HEAD_START.search(html)
    if not anchor:
        return html
    at = anchor.end()
    return html[:at] + "\n  " + "\n  ".join(head) + html[at:]


def _json(value) -> str:
    return json.dumps({"imports": value}, separators=(",", ":"))


def render_page(root: str, name: str) -> str:
    """The HTML page with versioned asset URLs, an import map and preloads for what it needs."""
    mtime = os.stat(os.path.join(root, name)).st_mtime_ns
    return _render(root, name, version(root), mtime)
