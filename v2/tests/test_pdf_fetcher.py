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
    html = (f'<a rel="attachment" href="{secret.as_uri()}">key</a>'
            '<a rel="attachment" href="file:///etc/passwd">passwd</a>'
            '<img src="file:///etc/hostname"><img src="http://169.254.169.254/latest/meta-data/">'
            '<link rel="stylesheet" href="https://example.com/x.css"><p>Report</p>')
    pdf = export.to_pdf(html, export.STATIC_DIR.as_uri() + "/")
    assert pdf.startswith(b"%PDF") and not _embeds_a_file(pdf)
    assert {secret.as_uri(), "file:///etc/passwd", "http://169.254.169.254/latest/meta-data/",
            "https://example.com/x.css"} <= set(fetched)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://127.0.0.1:8000/admin", "https://example.com/logo.png", "ftp://example.com/x",
    export.STATIC_DIR.as_uri() + "/../../../../etc/passwd", export.STATIC_DIR.as_uri() + "/../__main__.py",
    "file://evil.example/share/x.css", (export.STATIC_DIR.parent / "templates" / "base.html").as_uri()])
def test_fetcher_refuses(url):
    assert not export.allowed_url(url)
    with pytest.raises(ValueError):
        export.url_fetcher()(url)


def test_fetcher_serves_static_files_and_data_uris():
    assert export.allowed_url((export.STATIC_DIR / "report.css").as_uri())
    assert export.allowed_url("data:image/png;base64,iVBORw0KGgo=")
    html = (f'<link rel="stylesheet" href="{(export.STATIC_DIR / "report.css").as_uri()}">'
            '<a rel="attachment" href="data:text/plain;base64,aGVsbG8=">note</a><p>Report</p>')
    pdf = export.to_pdf(html, export.STATIC_DIR.as_uri() + "/")
    assert _embeds_a_file(pdf)  # the data: attachment is allowed
