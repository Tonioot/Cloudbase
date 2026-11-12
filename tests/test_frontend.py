"""How the panel's pages are served: versioned, cacheable, preloaded, and self-contained."""
import hashlib
import os
import re
from pathlib import Path

import pytest

from core import frontend_assets

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
PAGES = ["/", "/app", "/node", "/database", "/audit", "/notifications", "/settings", "/guides", "/login", "/status"]
VERSION_IN_URL = re.compile(r"\?v=([0-9a-f]{10})")


@pytest.fixture(scope="module")
def pages(client):
    return {path: client.get(path) for path in PAGES}


def test_every_page_is_served(pages):
    for path, r in pages.items():
        assert r.status_code == 200 and "text/html" in r.headers["content-type"], path


def test_every_stylesheet_and_script_carries_the_same_version(pages):
    for path, r in pages.items():
        tags = re.findall(r"<(?:link|script)\b[^>]*(?:href|src)=\"[^\"]*/(?:css|js)/[^\"]*\"[^>]*>", r.text)
        assert tags, path
        for tag in tags:
            assert VERSION_IN_URL.search(tag), f"{path}: {tag}"
        assert len(set(VERSION_IN_URL.findall(r.text))) == 1, path


def test_the_import_map_comes_before_any_module_and_covers_all_modules(pages):
    js_files = {f"/js/{p.name}" for p in (FRONTEND / "js").glob("*.js")}
    for path, r in pages.items():
        html = r.text
        assert '<script type="importmap">' in html, path
        first_module = html.find('<script type="module"')
        if first_module != -1:
            assert html.index('<script type="importmap">') < first_module, path
        mapped = set(re.findall(r'"(/js/[^"]+\.js)":"/js/', html))
        assert mapped == js_files, path


def test_the_module_graph_is_preloaded_so_it_loads_in_one_round(pages):
    app = pages["/app"].text
    preloads = set(re.findall(r'rel="modulepreload" href="(/js/[^"?]+)', app))
    # api.js, sidebar.js and app.js are imported by the page; shell.js only by those: a chain of imports
    assert {"/js/api.js", "/js/app.js", "/js/sidebar.js", "/js/shell.js"} <= preloads
    for href in preloads:
        assert (FRONTEND / href.lstrip("/")).exists(), href


def test_the_charset_stays_at_the_very_start_of_the_document(pages):
    for path, r in pages.items():
        assert r.text.index('<meta charset="UTF-8"') < 200, path
        assert r.text.index('<meta charset="UTF-8"') < r.text.index("importmap"), path


def test_nothing_is_loaded_from_another_server(pages):
    for path, r in pages.items():
        assert not re.search(r"<(?:link|script)[^>]+(?:href|src)=\"https?://", r.text), path
        assert "googleapis" not in r.text and "gstatic" not in r.text, path
    for css in (FRONTEND / "css").glob("*.css"):
        assert not re.search(r"@import|url\(\s*['\"]?https?://", css.read_text(encoding="utf-8")), css.name


def test_the_fonts_are_served_by_the_panel(pages, client):
    assert "/css/fonts.css?v=" in pages["/app"].text
    preloaded = re.findall(r'rel="preload" href="(/fonts/[^"]+\.woff2)"', pages["/app"].text)
    assert len(preloaded) == 2                                  # sans and mono, latin
    for href in preloaded:
        r = client.get(href)
        assert r.status_code == 200 and r.headers["content-type"] in ("font/woff2", "application/octet-stream")


def test_font_files_are_named_after_their_content_so_caching_them_forever_is_safe():
    css = (FRONTEND / "css" / "fonts.css").read_text(encoding="utf-8")
    files = set(re.findall(r"url\(\.\./fonts/([^)]+\.woff2)\)", css))
    assert files
    for name in files:
        path = FRONTEND / "fonts" / name
        assert path.exists(), name
        assert hashlib.sha1(path.read_bytes()).hexdigest()[:10] == name.split(".")[-2], name


# ── caching ───────────────────────────────────────────────────────────────────
def test_versioned_assets_are_cached_for_a_year_and_everything_else_is_revalidated(client, pages):
    ver = VERSION_IN_URL.search(pages["/app"].text).group(1)
    for url in (f"/js/api.js?v={ver}", f"/css/base.css?v={ver}"):
        r = client.get(url)
        assert r.status_code == 200 and "immutable" in r.headers["cache-control"] and "max-age=31536000" in r.headers["cache-control"], url
    font = re.search(r'href="(/fonts/[^"]+\.woff2)"', pages["/app"].text).group(1)
    assert "immutable" in client.get(font).headers["cache-control"]

    assert client.get("/js/api.js").headers["cache-control"] == "no-cache"            # no version: never trust a copy
    assert pages["/app"].headers["cache-control"] == "no-cache"                        # pages always revalidate
    assert client.get("/api/health").headers.get("cache-control") != "public, max-age=31536000, immutable"


def test_a_page_is_revalidated_with_its_etag(client, pages):
    etag = pages["/settings"].headers["etag"]
    again = client.get("/settings", headers={"if-none-match": etag})
    assert again.status_code == 304 and again.content == b""
    assert client.get("/settings", headers={"if-none-match": 'W/"something else"'}).status_code == 200


# ── the version ───────────────────────────────────────────────────────────────
def _site(tmp_path):
    for sub in ("css", "js", "fonts"):
        (tmp_path / sub).mkdir()
    (tmp_path / "css" / "a.css").write_text("body{}")
    (tmp_path / "js" / "main.js").write_text("import './dep.js';")
    (tmp_path / "js" / "dep.js").write_text("export const x = 1;")
    (tmp_path / "page.html").write_text(
        '<html><head><meta charset="UTF-8"><title>T</title><link rel="stylesheet" href="css/a.css">'
        '<script type="module">import "./js/main.js";</script></head><body></body></html>')
    return str(tmp_path)


def test_the_version_follows_the_content_not_the_timestamp(tmp_path):
    root = _site(tmp_path)
    v1 = frontend_assets.version(root)
    assert v1 == frontend_assets.version(root)
    os.utime(tmp_path / "css" / "a.css", (1_000_000_000, 1_000_000_000))        # touched, same content
    assert frontend_assets.version(root) == v1
    (tmp_path / "js" / "dep.js").write_text("export const x = 2;")              # changed
    assert frontend_assets.version(root) != v1


def test_a_page_is_rebuilt_when_an_asset_changes(tmp_path):
    root = _site(tmp_path)
    first = frontend_assets.render_page(root, "page.html")
    assert '/css/a.css?v=' in first and 'rel="modulepreload" href="/js/dep.js?v=' in first   # found through main.js
    (tmp_path / "css" / "a.css").write_text("body{color:red}")
    second = frontend_assets.render_page(root, "page.html")
    assert VERSION_IN_URL.search(first).group(1) != VERSION_IN_URL.search(second).group(1)


# ── the files themselves ──────────────────────────────────────────────────────
def test_every_import_in_the_javascript_points_at_a_file_that_exists():
    spec = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*)['"](\.{1,2}/[^'"]+)['"]""")
    for js in (FRONTEND / "js").glob("*.js"):
        for target in spec.findall(js.read_text(encoding="utf-8")):
            assert (js.parent / target).resolve().exists(), f"{js.name} imports {target}"


def test_the_page_colour_before_the_stylesheet_matches_the_theme():
    css = (FRONTEND / "css" / "variables.css").read_text(encoding="utf-8")
    dark = re.search(r":root\s*\{[^}]*?--bg:\s*(#[0-9a-fA-F]{6})", css, re.S).group(1).lower()
    light = re.search(r':root\[data-theme="light"\]\s*\{[^}]*?--bg:\s*(#[0-9a-fA-F]{6})', css, re.S).group(1).lower()
    assert (frontend_assets.BG_DARK, frontend_assets.BG_LIGHT) == (dark, light)


def test_a_session_ends_what_the_sidebar_remembered():
    """The lists and hidden menu items are kept per tab; they must go when someone logs out or in."""
    for name, needle in (("sidebar.js", "removeItem('cb-index')"), ("theme.js", "removeItem('cb-index')")):
        assert needle in (FRONTEND / "js" / name).read_text(encoding="utf-8"), name


def test_each_page_has_one_address_and_there_are_no_api_docs(admin):
    for path in ("/app?id=1", "/node?id=1", "/settings", "/login"):
        assert admin.get(path).status_code == 200, path
    for path in ("/app.html", "/settings.html", "/docs", "/redoc", "/openapi.json"):
        assert admin.get(path).status_code == 404, path
