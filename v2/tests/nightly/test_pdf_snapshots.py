"""The example PDFs (``examples/``, the reports clients receive) look as they did.

The demo site from ``scripts/screenshots.py`` is built with the clock stopped at the browser tests' date, so
every date in the reports reads the same on every run. Each example PDF is downloaded from it, every page is
drawn at a low resolution in grey (``pdftoppm``) and compared with its baseline in ``pdf_baselines/``. A page
fails when more than MAX_CHANGED of its pixels differ by more than THRESHOLD: a moved line, a table that
lost a column or a chart that broke, not anti-aliasing. A different number of pages fails too.

Only the fonts the Docker image has are used (``fonts.conf``), so the baselines match what clients get.
After a change you meant, refresh the baselines with ``--update`` (README.md) and look at the new images.
A failure leaves the page, its baseline and a picture of the difference in ``output/``.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # noqa: S404 (pdftoppm on PDFs this test downloaded)
from datetime import UTC, datetime
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
# Before anything renders: fontconfig reads this once, the first time a font is looked up.
os.environ["FONTCONFIG_FILE"] = str(HERE / "fonts.conf")

import screenshots  # noqa: E402

BASELINES = HERE / "pdf_baselines"
OUTPUT = HERE / "output"
FROZEN = datetime(2026, 10, 5, 9, 30, tzinfo=UTC)  # tests/browser/conftest.py's date
DPI = 40  # enough to see layout; keeps the baselines small
THRESHOLD = 64  # grey levels, out of 255
MAX_CHANGED = 0.002  # of a page's pixels
PDFS = [name for name in screenshots.EXAMPLES if name.endswith(".pdf")]


@pytest.fixture(scope="module")
def pdfs(tmp_path_factory) -> dict[str, bytes]:
    time_machine = pytest.importorskip("time_machine", reason="needs time-machine (tests/nightly/requirements.txt)")
    if not shutil.which("pdftoppm"):
        pytest.skip("needs pdftoppm (poppler-utils)")
    traveller = time_machine.travel(FROZEN, tick=False)
    traveller.start()
    try:
        cfg, ids, passwords, _ = screenshots.build_site(tmp_path_factory.mktemp("pdf") / "data")
        with screenshots.Server(cfg) as server:
            server.beat()
            return screenshots.download(
                server, passwords, {n: screenshots.EXAMPLES[n][0].format(t=ids[0]) for n in PDFS}
            )
    finally:
        traveller.stop()


def _pages(pdf: bytes, tmp: Path) -> list:
    from PIL import Image

    (tmp / "report.pdf").write_bytes(pdf)
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "pdftoppm",
            "-gray",
            "-png",
            "-r",
            str(DPI),
            str(tmp / "report.pdf"),
            str(tmp / "page"),
        ],
        check=True,
    )
    return [Image.open(p).convert("L") for p in sorted(tmp.glob("page-*.png"))]


@pytest.mark.parametrize("name", PDFS)
def test_pdf_looks_as_before(name, pdfs, tmp_path, request, summary):
    assert name in pdfs, f"{name} wasn't made (is WeasyPrint installed, with Pango?)"
    stem = name.removesuffix(".pdf")
    pages = _pages(pdfs[name], tmp_path)
    if request.config.getoption("--update"):
        BASELINES.mkdir(exist_ok=True)
        for old in BASELINES.glob(f"{stem}-p*.png"):
            old.unlink()
        for i, page in enumerate(pages, 1):
            page.save(BASELINES / f"{stem}-p{i}.png", optimize=True)
        return
    stored = sorted(BASELINES.glob(f"{stem}-p*.png"), key=lambda p: int(p.stem.rsplit("-p", 1)[1]))
    problems = []
    if len(pages) != len(stored):
        problems.append(f"{len(pages)} pages, the baseline has {len(stored)}")
    for i, (page, baseline) in enumerate(zip(pages, stored, strict=False), 1):
        problem = _compare(f"{stem}-p{i}", page, baseline)
        if problem:
            problems.append(f"page {i}: {problem}")
    for i in range(len(stored) + 1, len(pages) + 1):
        _save(f"{stem}-p{i}", pages[i - 1])
    if problems:
        summary.append(f"- PDF `{name}`: " + "; ".join(problems))
    assert not problems, f"{name} looks different:\n" + "\n".join(problems)


def _compare(name: str, page, baseline: Path) -> str | None:
    from PIL import Image, ImageChops

    expected = Image.open(baseline).convert("L")
    if page.size != expected.size:
        _save(name, page, expected)
        return f"size {page.size[0]}x{page.size[1]}, baseline {expected.size[0]}x{expected.size[1]}"
    changed = ImageChops.difference(page, expected).point(lambda v: 255 if v > THRESHOLD else 0)
    count = changed.histogram()[255]
    share = count / (page.size[0] * page.size[1])
    if share <= MAX_CHANGED:
        return None
    _save(name, page, expected, changed)
    return f"{share:.2%} of it changed; see output/{name}.diff.png"


def _save(name: str, page, expected=None, changed=None) -> None:
    from PIL import Image

    OUTPUT.mkdir(exist_ok=True)
    page.save(OUTPUT / f"{name}.actual.png")
    if expected is not None:
        expected.save(OUTPUT / f"{name}.baseline.png")
    if changed is not None:
        # The new page, faded, with what changed in red.
        faded = Image.blend(page.convert("RGB"), Image.new("RGB", page.size, "white"), 0.7)
        faded.paste(Image.new("RGB", page.size, (220, 30, 30)), mask=changed)
        faded.save(OUTPUT / f"{name}.diff.png")


def test_every_baseline_belongs_to_a_pdf():
    stems = {name.removesuffix(".pdf") for name in PDFS}
    stray = [p.name for p in BASELINES.glob("*.png") if p.stem.rsplit("-p", 1)[0] not in stems]
    assert not stray, f"baselines for PDFs that are no longer made (delete them): {stray}"
