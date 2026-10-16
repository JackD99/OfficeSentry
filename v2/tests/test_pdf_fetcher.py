"""PDF rendering only reads the app's own static files and data: URIs, never other files or the network."""

import pytest

from officesentry.reports import export

pytestmark = pytest.mark.skipif(not export.pdf_available(), reason="WeasyPrint isn't installed")


def _embeds_a_file(pdf: bytes) -> bool:
    # <a rel="attachment"> embeds the target in the PDF (the way a file:// read would leak).
    return b"/EmbeddedFile" in pdf


def test_pdf_html_cant_read_files_or_the_network(tmp_path, monkeypatch):
    secret = tmp_path / "secrets.json"
    secret.write_text('{"secret_key": "do-not-embed"}', encoding="utf-8")
    fetched = []
    real = export.allowed_url
    monkeypatch.setattr(export, "allowed_url", lambda url: fetched.append(url) or real(url))
    html = (
        f'<a rel="attachment" href="{secret.as_uri()}">key</a>'
        '<a rel="attachment" href="file:///etc/passwd">passwd</a>'
        '<img src="file:///etc/hostname"><img src="http://169.254.169.254/latest/meta-data/">'
        '<link rel="stylesheet" href="https://example.com/x.css"><p>Report</p>'
    )
    pdf = export.to_pdf(html, export.STATIC_DIR.as_uri() + "/")
    assert pdf.startswith(b"%PDF") and not _embeds_a_file(pdf)
    assert {
        secret.as_uri(),
        "file:///etc/passwd",
        "http://169.254.169.254/latest/meta-data/",
        "https://example.com/x.css",
    } <= set(fetched)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1:8000/admin",
        "https://example.com/logo.png",
        "ftp://example.com/x",
        export.STATIC_DIR.as_uri() + "/../../../../etc/passwd",
        export.STATIC_DIR.as_uri() + "/../__main__.py",
        "file://evil.example/share/x.css",
        (export.STATIC_DIR.parent / "templates" / "base.html").as_uri(),
    ],
)
def test_fetcher_refuses(url):
    assert not export.allowed_url(url)
    with pytest.raises(ValueError):
        export.url_fetcher()(url)


def test_fetcher_serves_static_files_and_data_uris():
    assert export.allowed_url((export.STATIC_DIR / "report.css").as_uri())
    assert export.allowed_url("data:image/png;base64,iVBORw0KGgo=")
    html = (
        f'<link rel="stylesheet" href="{(export.STATIC_DIR / "report.css").as_uri()}">'
        '<a rel="attachment" href="data:text/plain;base64,aGVsbG8=">note</a><p>Report</p>'
    )
    pdf = export.to_pdf(html, export.STATIC_DIR.as_uri() + "/")
    assert _embeds_a_file(pdf)  # the data: attachment is allowed


def test_bundled_fonts_are_unpacked_once():
    """WeasyPrint gets the bundled WOFF2 fonts already unpacked, once per process, rather than unpacking
    every face for every PDF (most of a short PDF's time)."""
    font = (export.STATIC_DIR / "fonts" / "SourceSans3-Regular.woff2").as_uri()
    assert export._font(font)[:4] in (b"\x00\x01\x00\x00", b"OTTO")  # plain OpenType
    assert export._font((export.STATIC_DIR / "report.css").as_uri()) is None
    export._unpacked_font.cache_clear()
    html = f'<link rel="stylesheet" href="{(export.STATIC_DIR / "report.css").as_uri()}"><h1>Report</h1><p>Text</p>'
    export.to_pdf(html, export.STATIC_DIR.as_uri() + "/")
    unpacked = export._unpacked_font.cache_info().currsize
    assert export.to_pdf(html, export.STATIC_DIR.as_uri() + "/").startswith(b"%PDF")
    info = export._unpacked_font.cache_info()
    assert unpacked > 0 and info.currsize == unpacked and info.hits >= unpacked
