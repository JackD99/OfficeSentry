"""How table cells read in client-facing exports: PDF tables and XLSX.

Builders return plain values, often ISO dates ("2026-08-20", "2026-10-01 22:42",
"2026-08-20 (45d)") because those sort and filter well. People reading a PDF
want "20 Aug 2026 (45 days ago)", and a spreadsheet wants a real date it can
sort and filter. Nothing here changes what the page, views or CSV see.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from markupsafe import Markup, escape

# 2026-08-20 | 2026-08-20 22:42 | 2026-08-20T22:42:05Z, optionally followed by " (45d)" or " (expired)".
DATE_CELL = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?Z?)?"
    r"(?: \((\d+)d\)| \((expired)\))?$"
)
# 6 Feb 2026, as identity reports and account pages write dates.
PROSE_DATE = re.compile(r"^(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{4})$")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
PERCENT = re.compile(r"^([+-]?\d+(?:\.\d+)?)%$")
SIGNED = re.compile(r"^[+-]\d{1,9}(\.\d+)?$")  # a change: "+4", "-1.5"
WHOLE = re.compile(r"^(0|[1-9]\d{0,8})$")
# Control characters XML can't hold (openpyxl's ILLEGAL_CHARACTERS_RE): tenant text can carry them (an inbox
# rule named with a vertical tab), and a spreadsheet or Word document refuses them. Tab and newlines stay.
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# Long unbroken text (addresses, URLs, paths) gets break opportunities after these characters.
BREAK_AFTER = re.compile(r"([@/\\._=&?:-])")
LONG_TOKEN = 16
WBR = Markup("<wbr>")


def parse_date(value) -> tuple[date | datetime, str] | None:
    """``(when, suffix)`` for a date-shaped string, else None. ``suffix`` is "", "45" (days) or "expired"."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    if m := PROSE_DATE.match(value):
        try:
            return date(int(m[3]), MONTHS.index(m[2]) + 1, int(m[1])), ""
        except ValueError:
            return None
    m = DATE_CELL.match(value)
    if not m:
        return None
    y, mo, d, hh, mm, ss, days, expired = m.groups()
    try:
        when = (
            datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss or 0)) if hh else date(int(y), int(mo), int(d))
        )
    except ValueError:
        return None
    return when, days or expired or ""


def _day(d: date) -> str:
    return f"{d.day} {d:%b %Y}"


def display(value) -> str:
    """A cell as people read it: dates in words, everything else as it is."""
    parsed = parse_date(value)
    if parsed is None:
        return "" if value is None else str(value)
    when, suffix = parsed
    text = f"{_day(when)}, {when:%H:%M}" if isinstance(when, datetime) else _day(when)
    if suffix == "expired":
        return f"{text} (expired)"
    if suffix and int(suffix):  # "(0d)" is under a day: the date says enough
        n = int(suffix)
        return f"{text} ({'1 day ago' if n == 1 else f'{n} days ago'})"
    return text


def breakable(text: str) -> Markup:
    """Escaped ``text`` with line-break opportunities inside long words, so an email address or URL
    wraps at its dots and slashes instead of widening its column (or splitting at any letter)."""
    out: list[Markup] = []
    for word in re.split(r"(\s+)", str(text)):
        if len(word) < LONG_TOKEN or word.isspace():
            out.append(escape(word))
            continue
        for piece in BREAK_AFTER.split(word):
            if BREAK_AFTER.fullmatch(piece):
                out.append(escape(piece) + WBR)
            else:  # a run with nowhere to break (a code or hash) gets a chance every LONG_TOKEN characters
                out.append(WBR.join(escape(piece[i : i + LONG_TOKEN]) for i in range(0, len(piece), LONG_TOKEN)))
    return Markup().join(out)


WRAP_AFTER = re.compile(r"([@/\\])")
WRAP_LONG = 30  # an address this long or longer may wrap on screen; most sign-in names stay whole
WRAP_PART = 20  # and a part of it this long (between those breaks) may also wrap after its dots
AFTER_DOT = re.compile(r"(?<=\.)(?=.)")


def wrappable(text: str) -> Markup:
    """Escaped ``text`` for a table cell on screen: a long address or path may wrap after its "@" or a slash,
    so it doesn't hold its column wide, while names and domains stay whole ("anna@" / "contoso.com"). Only a
    part that is itself long ("bartholomew.fitzwilliam-okonkwo@", a mail server's host name) may also wrap after
    its dots."""
    out: list[Markup] = []
    for word in re.split(r"(\s+)", str(text)):
        if len(word) < WRAP_LONG or word.isspace():
            out.append(escape(word))
            continue
        for part in WRAP_AFTER.split(word):
            if WRAP_AFTER.fullmatch(part):
                out.append(escape(part) + WBR)
            elif len(part) >= WRAP_PART:
                out.append(WBR.join(escape(p) for p in AFTER_DOT.split(part)))
            else:
                out.append(escape(part))
    return Markup().join(out)


def pdf_cell(value) -> Markup:
    """A table cell for a PDF: dates in words, long values able to wrap."""
    return breakable(display(value))


def printable(text: str) -> str:
    """``text`` without the control characters a spreadsheet or Word document can't hold."""
    return CONTROL.sub("", text)


def xlsx_value(value):
    """A cell for a spreadsheet: real dates and numbers where the text is one, never a formula."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float, date)):
        return value
    if isinstance(value, (list, tuple, set)):
        return printable(", ".join(str(v) for v in value))
    text = printable(str(value))
    parsed = parse_date(text)
    if parsed is not None:
        return parsed[0]
    if m := PERCENT.match(text):
        return float(m.group(1)) / 100
    if m := SIGNED.match(text):
        return float(text) if m[1] else int(text)
    if WHOLE.match(text):
        return int(text)
    return text
