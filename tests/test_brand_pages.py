"""White label: with a brand set, the page for an unknown hostname carries the brand and says nothing about Cloudbase,
the server or where its panel is. Without one, Cloudbase's own page as before."""
from nginx import pages


def test_unknown_host_without_brand_is_cloudbase():
    html = pages.generate_cloudbase_unknown_host_html("panel.example.test")
    assert "Cloudbase" in html
    assert "panel.example.test" in html


def test_unknown_host_with_brand_hides_the_engine():
    logo = "data:image/png;base64,iVBORw0KGgo="
    html = pages.generate_cloudbase_unknown_host_html("panel.example.test", {"name": "Acme", "logo_data": logo})
    assert "cloudbase" not in html.lower().replace("/var/www/cloudbase", "")
    assert "panel.example.test" not in html
    assert "Site not found" in html
    assert "— Acme" in html and logo in html


def test_brand_texts_are_escaped_and_bad_logos_dropped():
    html = pages.generate_cloudbase_unknown_host_html(None, {
        "name": "<b>x</b>", "unknown_host_title": "<script>", "logo_data": "javascript:alert(1)"})
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "javascript:" not in html
    assert "<svg" not in html          # no logo and a brand: no Cloudbase cloud either
