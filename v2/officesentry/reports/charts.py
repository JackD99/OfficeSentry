"""Small server-side SVG charts. They render identically in the browser and in PDFs."""

from __future__ import annotations

from dataclasses import dataclass
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
               width: int = 640, height: int = 180, forecast: list[tuple[date, float]] | None = None,
               limit: tuple[float, str] | None = None) -> str:
    """Single-series line chart (no legend: the caller's heading names the series).

    Thin 2px line, recessive grid, a labelled end point, and a <title> on each
    point for hover. Colours are attributes for PDF rendering; CSS classes
    restyle them in the browser (dark mode).

    ``forecast`` continues the line, dashed, from the last point (where it's
    heading at the current rate), and ``limit`` is ``(value, label)`` for a
    ceiling such as the storage quota, drawn as a labelled line.
    """
    if len(points) < 2:
        return ""
    forecast = [p for p in forecast or [] if p[0] > points[-1][0]]
    pad_l, pad_r, pad_t, pad_b = 44, 56, 12, 24
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    xs = [p[0].toordinal() for p in points]
    x0, x1 = min(xs), max([*xs, *(d.toordinal() for d, _ in forecast)])
    values = [p[1] for p in points]
    every = values + [v for _, v in forecast] + ([limit[0]] if limit else [])
    top = y_max if y_max is not None else max(every) * 1.1 or 1
    top = max(top, max(every))

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
    end_day = forecast[-1][0] if forecast else points[-1][0]
    for d, anchor in ((points[0][0], "start"), (end_day, "end")):
        parts.append(f'<text class="axis" x="{px(d.toordinal()):.1f}" y="{height - 6}" text-anchor="{anchor}" {AXIS}>'
                     f'{d.day} {d:%b %Y}</text>')
    if limit:
        y = py(limit[0])
        parts.append(f'<line class="limit" x1="{pad_l}" x2="{pad_l + w}" y1="{y:.1f}" y2="{y:.1f}" '
                     f'stroke="#5d6676" stroke-width="1"/>')
        parts.append(f'<text class="axis" x="{pad_l + 4}" y="{y - 4:.1f}" {AXIS}>{escape(limit[1])}</text>')
    path = " ".join(f'{"M" if i == 0 else "L"}{px(x):.1f},{py(v):.1f}'
                    for i, (x, v) in enumerate(zip(xs, values, strict=True)))
    base = py(0)
    parts.append(f'<path class="area" d="{path} L{px(xs[-1]):.1f},{base:.1f} L{px(xs[0]):.1f},{base:.1f} Z" '
                 f'fill="#2a78d6" fill-opacity="0.08" stroke="none"/>')
    parts.append(f'<path class="series" d="{path}" {SERIES}/>')
    if forecast:
        ahead = " ".join(f"L{px(d.toordinal()):.1f},{py(v):.1f}" for d, v in forecast)
        parts.append(f'<path class="forecast" d="M{px(xs[-1]):.1f},{py(values[-1]):.1f} {ahead}" {SERIES} '
                     f'stroke-dasharray="5 4"/>')
        d, v = forecast[-1]
        parts.append(f'<circle class="hit" cx="{px(d.toordinal()):.1f}" cy="{py(v):.1f}" r="6" fill="#2a78d6" '
                     f'fill-opacity="0"><title>Forecast {d.isoformat()}: {v:g}{escape(unit)}</title></circle>')
    for (d, v), x in zip(points, xs, strict=True):
        parts.append(f'<circle class="hit" cx="{px(x):.1f}" cy="{py(v):.1f}" r="6" fill="#2a78d6" fill-opacity="0">'
                     f'<title>{d.isoformat()}: {v:g}{escape(unit)}</title></circle>')
    last_x, last_v = px(xs[-1]), values[-1]
    parts.append(f'<circle class="end" cx="{last_x:.1f}" cy="{py(last_v):.1f}" r="4" {END}/>')
    if forecast:  # the end label would sit on the dashed line, so it goes above the point
        parts.append(f'<text class="value" x="{last_x:.1f}" y="{py(last_v) - 10:.1f}" text-anchor="middle" {VALUE}>'
                     f'{last_v:g}{escape(unit)}</text>')
    else:
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


def _nice(value: float) -> float:
    """The smallest 1, 2 or 5 times a power of ten at or above ``value``."""
    if value <= 0:
        return 1
    step = 10 ** (len(str(int(value))) - 1)
    for m in (1, 2, 5, 10):
        if value <= m * step:
            return m * step
    return 10 * step


def daily_bars(days: list[tuple[date, int | None, int | None, str]], *, label: str, width: int = 720,
               height: int = 180) -> str:
    """A column per day, stacked: ``(day, good, bad, hover text)``, oldest first. ``None`` marks a day there's
    no data for, drawn as a faint track so it doesn't read as zero.

    Two series (good in the accent colour at the base, bad in the failure colour on top) with a 2px gap
    between them and between days. The caller prints the legend as text. Each day's hover text is a
    <title> on a full-height target, wider than the bars.
    """
    if not days:
        return ""
    pad_l, pad_r, pad_t, pad_b = 44, 8, 10, 24
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    top = _nice(max(((a or 0) + (b or 0) for _, a, b, _ in days), default=0))
    slot = w / len(days)
    bar = max(slot - 2, 1)

    def py(v):
        return pad_t + h - h * v / top

    parts = [f'<svg class="chart bars" viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="{escape(label)}" xmlns="http://www.w3.org/2000/svg">']
    for frac in (0, 0.5, 1):
        y = py(top * frac)
        parts.append(f'<line class="grid" x1="{pad_l}" x2="{pad_l + w}" y1="{y:.1f}" y2="{y:.1f}" {GRID}/>')
        parts.append(f'<text class="axis" x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" {AXIS}>'
                     f'{top * frac:,.0f}</text>')
    for i, anchor in ((0, "start"), (len(days) // 2, "middle"), (len(days) - 1, "end")):
        d = days[i][0]
        x = pad_l + slot * i + (slot / 2 if anchor == "middle" else (0 if anchor == "start" else slot))
        parts.append(f'<text class="axis" x="{x:.1f}" y="{height - 6}" text-anchor="{anchor}" {AXIS}>'
                     f'{d.day} {d:%b}</text>')
    base = py(0)
    for i, (_, good, bad, text) in enumerate(days):
        x = pad_l + slot * i + 1
        if good is None and bad is None:
            parts.append(f'<rect class="nodata" x="{x:.1f}" y="{base - 3:.1f}" width="{bar:.1f}" height="3" '
                         f'fill="{TRACK}"/>')
        else:
            y = base
            for value, cls, fill in ((good or 0, "good", "#2a78d6"), (bad or 0, "bad", STATUS_FILL["critical"])):
                if not value:
                    continue
                size = max(h * value / top, 2)
                parts.append(f'<rect class="{cls}" x="{x:.1f}" y="{y - size:.1f}" width="{bar:.1f}" '
                             f'height="{size:.1f}" rx="{min(1.5, bar / 4):.1f}" fill="{fill}"/>')
                y -= size + 2
        parts.append(f'<rect class="hit" x="{pad_l + slot * i:.1f}" y="{pad_t}" width="{slot:.1f}" height="{h}" '
                     f'fill="#000" fill-opacity="0"><title>{escape(text)}</title></rect>')
    parts.append("</svg>")
    return "".join(parts)


# -- Bars that open their rows, sparklines and forecasts ---------------------------------------------

# Series fills, in a fixed order (never cycled): the chart blue, then orange and aqua from the same validated
# categorical palette (tokens --chart-series, --chart-2, --chart-3), and the failure red for a series that
# counts failures. A chart has three series at most, always with a text legend.
TONE_FILL = {"1": "#2a78d6", "2": "#eb6834", "3": "#1baf7a", "bad": STATUS_FILL["critical"],
             "muted": STATUS_FILL["low"]}


@dataclass
class Bar:
    """One bar of ``bars()``: a category, its value per series, and the rows it opens."""
    label: str
    values: list[float]
    text: str  # the total as people read it ("409", "242.5 GB")
    href: str = ""  # the table filtered to this category; "" for "Other" and in PDFs
    active: bool = False  # the table is filtered to this category now
    detail: str = ""  # hover text: each series' value


def bars(items: list[Bar], *, label: str, series: list[tuple[str, str]] | None = None, title: str = "") -> str:
    """Ranked horizontal bars, one per category, each a link to the rows behind it.

    Text (labels, values, legend) is HTML so it stays readable at any width and in the PDF; each bar is a
    small SVG whose parts are percentages of the longest bar, so it scales without distorting. ``series``
    is ``[(name, tone)]`` for stacked parts (tones key ``TONE_FILL``); one series needs no legend, since the
    chart's ``title`` names it. Parts are split by a 2px gap in the surface colour.
    """
    if not items:
        return ""
    series = series or [("", "1")]
    top = max((sum(b.values) for b in items), default=0) or 1
    out = [f'<figure class="barchart" aria-label="{escape(label)}">']
    if title or len(series) > 1:
        out.append(f'<figcaption><span class="chart-title">{escape(title)}</span>')
    if len(series) > 1:
        out.append('<span class="legend">' + "".join(
            f'<span><i class="sw tone-{escape(tone)}"></i>{escape(name)}</span>' for name, tone in series) + "</span>")
    if title or len(series) > 1:
        out.append("</figcaption>")
    out.append('<ol class="bars">')
    for b in items:
        width = 100 * sum(b.values) / top
        svg = ['<svg class="bar-track" width="100%" height="10" aria-hidden="true" '
               'xmlns="http://www.w3.org/2000/svg">']
        x = 0.0
        for value, (_, tone) in zip(b.values, series, strict=False):
            if value <= 0:
                continue
            w = 100 * value / top
            if x:  # the surface gap before every part after the first
                svg.append(f'<rect class="gap" x="{x:.2f}%" y="0" width="2" height="10" fill="#ffffff"/>')
            svg.append(f'<rect class="part tone-{escape(tone)}" x="{x:.2f}%" y="0" width="{max(w, 0.4):.2f}%" '
                       f'height="10" rx="2" fill="{TONE_FILL.get(tone, TONE_FILL["1"])}"/>')
            x += w
        if not width:
            svg.append('<rect class="zero" x="0" y="4" width="2" height="2" fill="#c4cad4"/>')
        svg.append("</svg>")
        body = (f'<span class="bar-label">{escape(b.label)}</span>{"".join(svg)}'
                f'<span class="bar-value">{escape(b.text)}</span>')
        title = escape(b.detail or f"{b.label}: {b.text}")
        if b.href:
            hint = "show every row" if b.active else "show these rows"
            out.append(f'<li><a class="bar{" on" if b.active else ""}" href="{escape(b.href)}" data-view '
                       f'aria-pressed="{"true" if b.active else "false"}" title="{title}. Click to {hint}.">'
                       f'{body}</a></li>')
        else:
            out.append(f'<li><div class="bar" title="{title}">{body}</div></li>')
    out.append("</ol></figure>")
    return "".join(out)


def sparkline(values: list[float], *, label: str, width: int = 72, height: int = 22) -> str:
    """A small trend line for a headline figure: a muted line with the latest point in the accent colour.

    It isn't coloured good or bad: the shape is the message and the figure beside it says the value. Fewer
    than three points draw nothing. ``label`` (the <title> and accessible name) should say the range.
    """
    if len(values) < 3:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    pad = 3

    def py(v):
        return pad + (height - 2 * pad) * (1 - (v - lo) / span) if hi != lo else height / 2

    step = (width - 2 * pad) / (len(values) - 1)
    path = " ".join(f'{"M" if i == 0 else "L"}{pad + step * i:.1f},{py(v):.1f}' for i, v in enumerate(values))
    first, last = values[0], values[-1]
    direction = "flat" if first == last else ("up" if last > first else "down")
    return (f'<svg class="spark spark-{direction}" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'role="img" aria-label="{escape(label)}" xmlns="http://www.w3.org/2000/svg"><title>{escape(label)}</title>'
            f'<path class="spark-line" d="{path}" fill="none" stroke="#98a2b3" stroke-width="1.5" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle class="spark-end" cx="{pad + step * (len(values) - 1):.1f}" cy="{py(last):.1f}" r="2.5" '
            f'fill="#2a78d6"/></svg>')
