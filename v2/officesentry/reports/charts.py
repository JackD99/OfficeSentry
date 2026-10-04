"""Small server-side SVG charts. They render identically in the browser and in PDFs."""

from __future__ import annotations

from datetime import date
from html import escape

# Presentation attributes for renderers without CSS (WeasyPrint's SVG, image
# viewers). In the browser, report.css overrides them for dark mode.
GRID = 'stroke="#e3e6eb" stroke-width="1"'
AXIS = 'fill="#5d6676" font-size="11" font-family="sans-serif"'
SERIES = 'fill="none" stroke="#2a78d6" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"'
END = 'fill="#2a78d6" stroke="#ffffff" stroke-width="2"'
VALUE = 'fill="#1d2330" font-size="12" font-weight="700" font-family="sans-serif"'


def line_chart(points: list[tuple[date, float]], *, label: str, unit: str = "%", y_max: float | None = 100,
               width: int = 640, height: int = 180) -> str:
    """Single-series line chart (no legend: the caller's heading names the series).

    Thin 2px line, recessive grid, a labelled end point, and a <title> on each
    point for hover. Colours are attributes for PDF rendering; CSS classes
    restyle them in the browser (dark mode).
    """
    if len(points) < 2:
        return ""
    pad_l, pad_r, pad_t, pad_b = 44, 56, 12, 24
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    xs = [p[0].toordinal() for p in points]
    x0, x1 = min(xs), max(xs)
    values = [p[1] for p in points]
    top = y_max if y_max is not None else max(values) * 1.1 or 1
    top = max(top, max(values))

    def px(x):
        return pad_l + (w * (x - x0) / (x1 - x0) if x1 != x0 else w)

    def py(v):
        return pad_t + h - h * v / top

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="{escape(label)}" xmlns="http://www.w3.org/2000/svg">']
    for frac in (0, 0.5, 1):
        y = py(top * frac)
        parts.append(f'<line class="grid" x1="{pad_l}" x2="{pad_l + w}" y1="{y:.1f}" y2="{y:.1f}" {GRID}/>')
        parts.append(f'<text class="axis" x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" {AXIS}>'
                     f'{top * frac:.0f}{escape(unit)}</text>')
    for d in (points[0][0], points[-1][0]):
        anchor = "start" if d == points[0][0] else "end"
        parts.append(f'<text class="axis" x="{px(d.toordinal()):.1f}" y="{height - 6}" text-anchor="{anchor}" {AXIS}>'
                     f'{d.day} {d:%b %Y}</text>')
    path = " ".join(f'{"M" if i == 0 else "L"}{px(x):.1f},{py(v):.1f}'
                    for i, (x, v) in enumerate(zip(xs, values, strict=True)))
    base = py(0)
    parts.append(f'<path class="area" d="{path} L{px(xs[-1]):.1f},{base:.1f} L{px(xs[0]):.1f},{base:.1f} Z" '
                 f'fill="#2a78d6" fill-opacity="0.08" stroke="none"/>')
    parts.append(f'<path class="series" d="{path}" {SERIES}/>')
    for (d, v), x in zip(points, xs, strict=True):
        parts.append(f'<circle class="hit" cx="{px(x):.1f}" cy="{py(v):.1f}" r="6" fill="#2a78d6" fill-opacity="0">'
                     f'<title>{d.isoformat()}: {v:g}{escape(unit)}</title></circle>')
    last_x, last_v = px(xs[-1]), values[-1]
    parts.append(f'<circle class="end" cx="{last_x:.1f}" cy="{py(last_v):.1f}" r="4" {END}/>')
    parts.append(f'<text class="value" x="{last_x + 8:.1f}" y="{py(last_v) + 4:.1f}" {VALUE}>'
                 f'{last_v:g}{escape(unit)}</text>')
    parts.append("</svg>")
    return "".join(parts)


# Status fills shared by severity and answer bars. Mid-tone steps that read on
# white paper and on the dark report surface; always shown with a text label.
STATUS_FILL = {
    "critical": "#d03b3b", "high": "#ec835a", "medium": "#fab219", "low": "#98a2b3", "info": "#c4cad4",
    "ok": "#0ca30c", "none": "#c4cad4",
}
TRACK = "#e8ebf0"


def stacked_bar(segments: list[tuple[str, float, str]], *, label: str, width: int = 640, height: int = 12) -> str:
    """One horizontal bar split into labelled parts, e.g. checks by severity.

    ``segments`` are ``(label, value, status)`` where status keys ``STATUS_FILL``.
    Parts are separated by a 2px gap; the caller prints the legend as text, so
    colour never carries the meaning alone.
    """
    parts = [f'<svg class="bar" viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" '
             f'aria-label="{escape(label)}" xmlns="http://www.w3.org/2000/svg">']
    shown = [(name, value, status) for name, value, status in segments if value > 0]
    total = sum(v for _, v, _ in shown)
    if not total:
        parts.append(f'<rect class="track" x="0" y="0" width="{width}" height="{height}" rx="{height / 2}" '
                     f'fill="{TRACK}"/></svg>')
        return "".join(parts)
    gap = 2
    usable = width - gap * (len(shown) - 1)
    x = 0.0
    for name, value, status in shown:
        w = max(usable * value / total, 4)
        fill = STATUS_FILL.get(status, STATUS_FILL["none"])
        parts.append(f'<rect class="seg seg-{escape(status)}" x="{x:.1f}" y="0" width="{w:.1f}" height="{height}" '
                     f'rx="3" fill="{fill}"><title>{escape(name)}: {value:g}</title></rect>')
        x += w + gap
    parts.append("</svg>")
    return "".join(parts)


def meter(value: float | None, *, maximum: float = 100, color: str = "#2a78d6", width: int = 200,
          height: int = 6) -> str:
    """A thin progress bar for a percentage KPI."""
    if value is None:
        return ""
    frac = max(0.0, min(1.0, value / maximum if maximum else 0))
    r = height / 2
    fill = (f'<rect class="fill" x="0" y="0" width="{max(width * frac, height):.1f}" height="{height}" rx="{r}" '
            f'fill="{escape(color)}"/>' if frac else "")
    return (f'<svg class="meter" viewBox="0 0 {width} {height}" preserveAspectRatio="none" aria-hidden="true" '
            f'xmlns="http://www.w3.org/2000/svg"><rect class="track" x="0" y="0" width="{width}" height="{height}" '
            f'rx="{r}" fill="{TRACK}"/>{fill}</svg>')
