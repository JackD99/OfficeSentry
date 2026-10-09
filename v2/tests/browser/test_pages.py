"""Every page in pages.py, at a laptop and a phone width, light and dark: a screenshot compared with its
baseline in baselines/, and an axe-core accessibility check at the laptop width.

Baselines are 256-colour PNGs (a few MB for all of them). A capture is mapped onto its baseline's colours
before comparing, so the same page compares exactly; it fails when more than MAX_CHANGED of its pixels
differ by more than THRESHOLD in any colour channel, and the capture, the baseline and a picture of the
difference go to output/ (``--save-all`` puts every capture there). axe fails on
"serious" and "critical" WCAG 2.1 A and AA problems.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from .conftest import THEMES, VIEWPORTS
from .pages import PAGES

HERE = Path(__file__).resolve().parent
BASELINES = HERE / "baselines"
OUTPUT = HERE / "output"
AXE = (HERE / "vendor" / "axe-core" / "axe.min.js").read_text(encoding="utf-8")

# Long pages are compared down to here: the top of a page is what people see, and phones run long.
MAX_HEIGHT = {"laptop": 2400, "phone": 3200}
THRESHOLD = 16  # per colour channel, out of 255
MAX_CHANGED = 0.0005  # of the pixels: a few stray anti-aliased edges, not a moved line of text
# Differs on every run or every release, so painted over.
ALWAYS_MASK = (".powered-by:visible",)
MASK_COLOUR = "#0f172a"  # the sidebar's, where the version sits
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("viewport", list(VIEWPORTS))
@pytest.mark.parametrize("spec", PAGES, ids=lambda p: p.name)
def test_page(spec, viewport, theme, site, contexts, request):
    server, ids, _ = site
    page = contexts(spec.user, viewport, theme).new_page()
    try:
        response = page.goto(server.url + spec.path.format(t=ids[0], o=ids[-1]))
        assert response is not None and response.status == 200, f"{spec.path} returned {response and response.status}"
        page.wait_for_load_state("networkidle")
        page.evaluate("document.fonts.ready")
        problems = []
        name = f"{spec.name}-{viewport}-{theme}"
        problem = _check_screenshot(page, name, viewport, spec, request.config)
        if problem:
            problems.append(problem)
        if viewport == "laptop":
            problems += _axe(page)
        assert not problems, f"{name}:\n" + "\n".join(problems)
    finally:
        page.close()


def _check_screenshot(page, name: str, viewport: str, spec, config) -> str | None:
    height = page.evaluate("document.documentElement.scrollHeight")
    width = VIEWPORTS[viewport]["width"]
    png = page.screenshot(full_page=True, animations="disabled", caret="hide",
                          clip={"x": 0, "y": 0, "width": width,
                                "height": height if spec.full else min(height, MAX_HEIGHT[viewport])},
                          mask=[page.locator(s) for s in ALWAYS_MASK + spec.mask], mask_color=MASK_COLOUR)
    baseline = BASELINES / f"{name}.png"
    if config.getoption("--save-all"):
        _save(name, png)
    if config.getoption("--update"):
        BASELINES.mkdir(exist_ok=True)
        _save_baseline(png, baseline)
        return None
    if not baseline.exists():
        _save(name, png)
        return f"no baseline {baseline.name}: run with --update to make one (output/{name}.actual.png)"
    return _compare(name, baseline, png)


def _save_baseline(png: bytes, path: Path) -> None:
    """A 256-colour PNG in which every pixel is the palette colour nearest its real one (what _compare does
    to a capture), so an unchanged page maps onto it exactly."""
    from PIL import Image

    image = Image.open(BytesIO(png)).convert("RGB")
    palette = image.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    image.quantize(palette=palette, dither=Image.Dither.NONE).save(path, optimize=True)


def _compare(name: str, baseline: Path, png: bytes) -> str | None:
    from PIL import Image, ImageChops

    stored = Image.open(baseline)
    expected = stored.convert("RGB")
    actual = Image.open(BytesIO(png)).convert("RGB")
    if actual.size == expected.size and stored.mode == "P":
        actual = actual.quantize(palette=stored, dither=Image.Dither.NONE).convert("RGB")
    if actual.size != expected.size:
        _save(name, png, expected)
        return f"size changed: {actual.size[0]}x{actual.size[1]}, baseline {expected.size[0]}x{expected.size[1]}"
    diff = ImageChops.difference(actual, expected)
    r, g, b = diff.split()
    changed = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v > THRESHOLD else 0)
    count = changed.histogram()[255]
    share = count / (actual.size[0] * actual.size[1])
    if share <= MAX_CHANGED:
        return None
    _save(name, png, expected, changed)
    return f"{count} pixels ({share:.2%}) differ from {baseline.name}; see output/{name}.diff.png"


def _save(name: str, png: bytes, expected=None, changed=None) -> None:
    from PIL import Image

    OUTPUT.mkdir(exist_ok=True)
    (OUTPUT / f"{name}.actual.png").write_bytes(png)
    if expected is not None:
        expected.save(OUTPUT / f"{name}.baseline.png")
    if changed is not None:
        # The actual page, faded, with what changed in red.
        faded = Image.blend(Image.open(BytesIO(png)).convert("RGB"), Image.new("RGB", expected.size, "white"), .7)
        faded.paste(Image.new("RGB", expected.size, (220, 30, 30)), mask=changed)
        faded.save(OUTPUT / f"{name}.diff.png")


def _axe(page) -> list[str]:
    if not page.evaluate("typeof window.axe !== 'undefined'"):
        page.evaluate(AXE)
    violations = page.evaluate(
        "tags => axe.run(document, {runOnly: {type: 'tag', values: tags}, resultTypes: ['violations']})"
        ".then(r => r.violations.map(v => ({id: v.id, impact: v.impact, help: v.help,"
        " targets: v.nodes.map(n => n.target.join(' ')).slice(0, 3), count: v.nodes.length})))", AXE_TAGS)
    return [f"axe {v['impact']} {v['id']}: {v['help']} ({v['count']}x, e.g. {'; '.join(v['targets'])})"
            for v in violations if v["impact"] in ("serious", "critical")]


def test_every_baseline_belongs_to_a_page():
    expected = {f"{p.name}-{v}-{t}.png" for p in PAGES for v in VIEWPORTS for t in THEMES}
    stale = sorted(f.name for f in BASELINES.glob("*.png") if f.name not in expected)
    assert not stale, f"baselines for pages no longer in pages.py; delete them: {', '.join(stale)}"
