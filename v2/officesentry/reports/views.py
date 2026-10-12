"""Table views on detail reports: filter rows by value or age, hide columns, sort.

A view lives in the query string, so it can be linked to, saved and exported:

    f=<table>/<column>:<value>      keep rows with that value (repeat: any of the values)
    age=<table>/<column>:<preset>   date columns, a key of ``AGES``
    hide=<table>/<column>           hide a column
    sort=<table>/<column>:asc|desc  sort a table

``<table>`` and ``<column>`` are slugs of the section title and column name.
The flag column is ``flag``, with values critical, high, medium, low and none.
The page, PDF, XLSX and CSV all apply the same view, so a download matches
what's on screen. A headline figure points at its rows with ``data_reports.Rows``.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlencode

from ..text import fold
from . import charts
from .data_reports import FLAG_ORDER, DataReport, Many, Rows, Section

PARAMS = ("f", "age", "hide", "sort")
FLAG = "flag"
FLAGS = {"critical": "Critical", "high": "High", "medium": "Medium", "low": "Low", "info": "Info", "none": "No flag"}
REVIEW = ("critical", "high", "medium")  # what "to review" counts
AGES = {
    "lt30": "In the last 30 days",
    "gt30": "Older than 30 days",
    "gt90": "Older than 90 days",
    "gt180": "Older than 180 days",
    "gt365": "Older than a year",
    "future": "After today",
    "none": "Never or blank",
}
MAX_VALUES = 30  # a column with more distinct values than this gets no value filter
# Columns that explain a row rather than describe it. On the page, a table this wide draws them as a line
# under their row, so the facts stay side by side; downloads keep them as columns.
NOTE_COLUMNS = frozenset(
    {
        "What it means",
        "What to check",
        "What to do",
        "What to fix",
        "What happens",
        "Why it matters",
        "Why",
        "Why we think it's break-glass",
        "How to free it",
    }
)
NOTES_FROM = 6  # columns shown before the notes move under their rows
MAX_PARAMS = 60
_SLUG = re.compile(r"^[a-z0-9-]{1,80}$")
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
# "3 Sep 2026", as identity reports write dates; read so their columns get age presets and sort by date.
_PROSE_DATE = re.compile(r"^(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{4})\b")
_MONTHS = {
    m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)
}
# Sizes, percentages and numbers may carry a sign, - or the minus sign U+2212 for a fall: a change ("+21.1 GB").
_SIZE = re.compile(r"^([+\u2212-]?[\d.,]+)\s*(bytes|KB|MB|GB|TB)$")
_PERCENT = re.compile(r"^[+\u2212-]?[\d.,]+%$")
_NUMBER = re.compile(r"^[+\u2212-]?\d[\d,]*(?:\.\d+)?%?$")  # strict: "Nan", "inf" and "1e3" stay text
_SIGNED = re.compile(r"^[+\u2212-]\d[\d,]*(?:\.\d+)?$")
NO_CHANGE = "No change"  # what a change column says for zero
_SIZES = {"bytes": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}

Key = tuple[str, str]  # (table slug, column slug)
# Column titles that were renamed: views saved (and links bookmarked) under the old slug keep working.
RENAMED = {"tenant": "client"}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:80] or "x"


def _unique(names: list[str]) -> list[str]:
    seen: Counter = Counter()
    out = []
    for name in names:
        s = slug(name)
        seen[s] += 1
        out.append(s if seen[s] == 1 else f"{s}-{seen[s]}")
    return out


def table_slugs(report: DataReport) -> list[str]:
    return _unique([s.title for s in report.sections])


def column_slugs(section: Section) -> list[str]:
    return _unique(section.columns)


# -- The view itself ----------------------------------------------------------------


@dataclass
class View:
    filters: dict[Key, list[str]] = field(default_factory=dict)
    ages: dict[Key, str] = field(default_factory=dict)
    hidden: list[Key] = field(default_factory=list)
    sorts: dict[str, tuple[str, bool]] = field(default_factory=dict)  # table -> (column, descending)

    @classmethod
    def parse(cls, args) -> View:
        """From request args (a MultiDict), a list of (name, value) pairs or a query string."""
        if isinstance(args, str):
            from urllib.parse import parse_qsl

            args = parse_qsl(args, keep_blank_values=True)
        pairs = list(args.items(multi=True)) if hasattr(args, "items") and hasattr(args, "getlist") else list(args)
        view = cls()
        for name, raw in [p for p in pairs if p[0] in PARAMS][:MAX_PARAMS]:
            target, _, value = str(raw).partition(":") if name != "hide" else (str(raw), "", "")
            table, _, column = target.partition("/")
            if not (_SLUG.match(table) and _SLUG.match(column)):
                continue
            column = RENAMED.get(column, column)
            key = (table, column)
            if name == "f":
                values = view.filters.setdefault(key, [])
                if value[:200] not in values:
                    values.append(value[:200])
            elif name == "age" and value in AGES:
                view.ages[key] = value
            elif name == "hide" and key not in view.hidden:
                view.hidden.append(key)
            elif name == "sort" and value in ("asc", "desc"):
                view.sorts[table] = (column, value == "desc")
        return view

    def params(self) -> list[tuple[str, str]]:
        out = [("f", f"{t}/{c}:{v}") for (t, c), values in self.filters.items() for v in values]
        out += [("age", f"{t}/{c}:{a}") for (t, c), a in self.ages.items()]
        out += [("hide", f"{t}/{c}") for t, c in self.hidden]
        out += [("sort", f"{t}/{c}:{'desc' if d else 'asc'}") for t, (c, d) in self.sorts.items()]
        return out

    def query(self) -> str:
        return urlencode(self.params())

    def __bool__(self) -> bool:
        return bool(self.filters or self.ages or self.hidden or self.sorts)

    def tables(self) -> set[str]:
        return {t for t, _ in [*self.filters, *self.ages, *self.hidden]} | set(self.sorts)

    # Each edit returns a new view, so the page can build a link for every option.

    def _copy(self) -> View:
        return View({k: list(v) for k, v in self.filters.items()}, dict(self.ages), list(self.hidden), dict(self.sorts))

    def toggle_value(self, key: Key, value: str) -> View:
        v = self._copy()
        values = v.filters.setdefault(key, [])
        if value in values:
            values.remove(value)
        else:
            values.append(value)
        if not values:
            del v.filters[key]
        return v

    def toggle_age(self, key: Key, preset: str) -> View:
        v = self._copy()
        if v.ages.get(key) == preset:
            del v.ages[key]
        else:
            v.ages[key] = preset
        return v

    def toggle_hidden(self, key: Key) -> View:
        v = self._copy()
        if key in v.hidden:
            v.hidden.remove(key)
        else:
            v.hidden.append(key)
        return v

    def sorted_by(self, table: str, column: str, descending: bool) -> View:
        v = self._copy()
        v.sorts[table] = (column, descending)
        return v

    def without(self, key: Key) -> View:
        v = self._copy()
        v.filters.pop(key, None)
        v.ages.pop(key, None)
        return v

    def without_filters(self, table: str) -> View:
        v = self._copy()
        v.filters = {k: x for k, x in v.filters.items() if k[0] != table}
        v.ages = {k: x for k, x in v.ages.items() if k[0] != table}
        return v

    def showing_all(self, table: str) -> View:
        v = self._copy()
        v.hidden = [k for k in v.hidden if k[0] != table]
        return v


# -- Cell values ---------------------------------------------------------------------


def values_of(cell) -> list[str]:
    """What a cell offers to a value filter: each item of a list cell, else its text."""
    if isinstance(cell, Many):
        return list(cell.parts) or [""]
    return [str(cell)]


def as_date(cell) -> date | None:
    text = str(cell)
    try:
        if m := _DATE.match(text):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := _PROSE_DATE.match(text):
            return date(int(m[3]), _MONTHS[m[2]], int(m[1]))
    except ValueError:
        return None
    return None


def _is_number(cell) -> bool:
    return isinstance(cell, (int, float)) and not isinstance(cell, bool)


def _float(text: str) -> float:
    return float(text.replace(",", "").replace("\u2212", "-"))


def sort_key(cell):
    """Numbers (and sizes, percentages and changes) as numbers, everything else as text without case or accents;
    blanks are None."""
    if _is_number(cell):
        return float(cell)
    text = str(cell).strip()
    if text in ("", "–", "-"):
        return None
    if text == NO_CHANGE:
        return 0.0
    m = _SIZE.match(text)
    if m:
        try:
            return _float(m[1]) * _SIZES[m[2]]
        except ValueError:
            pass
    if _NUMBER.match(text):
        return _float(text.rstrip("%"))
    if _PROSE_DATE.match(text) and (d := as_date(text)):
        return d.isoformat()  # "3 Sep 2026" sorts by date, not alphabetically
    return fold(text)


def _age_matches(cell, preset: str, today: date) -> bool:
    d = as_date(cell)
    if preset == "none":
        return d is None
    if d is None:
        return False
    days = (today - d).days
    if preset == "future":
        return days < 0
    if preset == "lt30":
        return 0 <= days <= 30
    return days > int(preset[2:])


def _is_measure(cell) -> bool:
    """A number, a size ("105.4 GB"), a percentage ("88%") or a change ("+2.2", "+21.1 GB", "No change"):
    right-aligned and largest first."""
    if _is_number(cell):
        return True
    text = str(cell).strip()
    return bool(_SIZE.match(text) or _PERCENT.match(text) or _SIGNED.match(text) or text == NO_CHANGE)


def _kind(cells: list) -> str:
    filled = [c for c in cells if str(c).strip() not in ("", "–")]
    if filled and all(_is_measure(c) for c in filled):
        return "num"
    dates = sum(1 for c in filled if as_date(c))
    return "date" if dates and dates * 2 >= len(filled) else "text"


# -- Applying a view to one table -----------------------------------------------------


def _matching_rows(section: Section, table: str, view: View, today: date) -> list[int]:
    cols = column_slugs(section)
    index = {s: i for i, s in enumerate(cols)}
    rows = list(range(len(section.rows)))
    flags = section.flags or [None] * len(section.rows)
    for (t, c), wanted in view.filters.items():
        if t != table:
            continue
        if c == FLAG:
            rows = [r for r in rows if (flags[r] or "none") in wanted]
        elif c in index:
            i = index[c]
            rows = [r for r in rows if set(values_of(section.rows[r][i])) & set(wanted)]
    for (t, c), preset in view.ages.items():
        if t == table and c in index:
            i = index[c]
            rows = [r for r in rows if _age_matches(section.rows[r][i], preset, today)]
    if table in view.sorts:
        column, descending = view.sorts[table]
        if column == FLAG:
            rows.sort(key=lambda r: FLAG_ORDER.get(flags[r], 5), reverse=descending)
        elif column in index:
            i = index[column]
            keyed = [(sort_key(section.rows[r][i]), r) for r in rows]
            # Numbers before text when a column mixes them, either way round; blanks always last.
            numbers = sorted((x for x in keyed if isinstance(x[0], float)), key=lambda x: x[0], reverse=descending)
            texts = sorted((x for x in keyed if isinstance(x[0], str)), key=lambda x: x[0], reverse=descending)
            rows = [r for _, r in numbers + texts] + [r for k, r in keyed if k is None]
    return rows


def _visible(section: Section, table: str, view: View) -> list[int]:
    cols = column_slugs(section)
    shown = [i for i, s in enumerate(cols) if (table, s) not in view.hidden]
    return shown or [0]  # never hide every column


def describe(report: DataReport, view: View) -> list[str]:
    """The view in words, for a filtered PDF or XLSX."""
    out = []
    for table, section in zip(table_slugs(report), report.sections, strict=True):
        if table not in view.tables():
            continue
        names = dict(zip(column_slugs(section), section.columns, strict=True))
        names[FLAG] = section.flag_name
        parts = []
        for (t, c), values in view.filters.items():
            if t == table and c in names:
                shown = [FLAGS.get(v, v) for v in values] if c == FLAG else [v or "blank" for v in values]
                parts.append(f"{names[c]} is {' or '.join(shown)}")
        for (t, c), preset in view.ages.items():
            if t == table and c in names:
                parts.append(f"{names[c]}: {AGES[preset].lower()}")
        hidden = sum(1 for t, c in view.hidden if t == table and c in names)
        if hidden:
            parts.append(f"{hidden} column{'s' if hidden > 1 else ''} hidden")
        if parts:
            parts.insert(
                0,
                f"{len(_matching_rows(section, table, view, report.generated_at.date()))} of {len(section.rows)} rows",
            )
            out.append(f"{section.title}: {', '.join(parts)}.")
    return out


def apply(report: DataReport, view: View) -> DataReport:
    """A copy of the report with the view applied to its tables (and their charts drawn), for downloads."""
    if not view or not report.sections:
        return figures(report)
    out = copy.copy(report)
    today = report.generated_at.date()
    out.sections = []
    for table, section in zip(table_slugs(report), report.sections, strict=True):
        rows = _matching_rows(section, table, view, today)
        cols = _visible(section, table, view)
        flags = section.flags or [None] * len(section.rows)
        out.sections.append(
            Section(
                section.title,
                [section.columns[i] for i in cols],
                rows=[[section.rows[r][i] for i in cols] for r in rows],
                flags=[flags[r] for r in rows],
                intro=section.intro,
                empty="No rows match the filters." if section.rows else section.empty,
                compare=section.compare,
                flag_name=section.flag_name,
                group=section.group,
                group_ids=section.group_ids,
                filter_columns=section.filter_columns,
                keys=[section.keys[r] for r in rows] if section.keys else [],
                review=section.review,
                chart=section.chart,
                figure=chart(section, table, rows),
            )
        )
    out.filtered = describe(report, view)
    return out


def figures(report: DataReport) -> DataReport:
    """The report with each table's chart drawn for a download (no view: every row)."""
    for table, section in zip(table_slugs(report), report.sections, strict=True):
        if section.chart and not section.figure:
            section.figure = chart(section, table, list(range(len(section.rows))))
    return report


# -- Charts that follow the table ------------------------------------------------------------


def _amount(cell) -> tuple[float, str] | None:
    """A number cell as ``(value, kind)``: kind is "count", "size", "money" or "number"; None if it isn't one."""
    if hasattr(cell, "currency"):  # identity_reports.Money
        return float(cell), "money"
    if isinstance(cell, int) and not isinstance(cell, bool):
        return float(cell), "count"
    if _is_number(cell):
        return float(cell), "number"
    text = str(cell).strip()
    m = _SIZE.match(text)
    if m:
        try:
            return _float(m[1]) * _SIZES[m[2]], "size"
        except ValueError:
            return None
    if re.fullmatch(r"\d{1,3}(,\d{3})*|\d+", text):
        return float(text.replace(",", "")), "count"
    return None


def _shown(value: float, kind: str, like=None) -> str:
    if kind == "size":
        from .sharepoint import size

        return size(value)
    if kind == "money" and like is not None:
        return str(type(like)(value, like.currency))
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"


def chart(section: Section, table: str, rows: list[int], view: View | None = None, path: str = "") -> str:
    """The section's chart (``Section.chart``) of the given rows, which are the ones the view leaves.

    With a ``view`` (the page), each bar links to the table filtered to its value, or back to every row
    when it's the filter already; without one (a PDF) the bars are plain.
    """
    spec = section.chart
    if spec is None or not rows:
        return ""
    slugs = column_slugs(section)
    flags = section.flags or [None] * len(section.rows)
    if spec.by == FLAG:
        column, name = FLAG, section.flag_name
    elif spec.by in section.columns:
        at = section.columns.index(spec.by)
        column, name = slugs[at], spec.by
    else:
        return ""
    measures = [section.columns.index(c) for c in spec.series if c in section.columns]
    total_at = section.columns.index(spec.total) if spec.total in section.columns else None
    sums: dict[str, list[float]] = {}
    shown_totals: dict[str, float] = {}
    kind, like = "count", None
    for r in rows:
        keys = [flags[r] or "none"] if column == FLAG else values_of(section.rows[r][at])
        amounts = []
        for i in measures:
            got = _amount(section.rows[r][i])
            amounts.append(got[0] if got else 0.0)
            if got and got[1] != "count":
                kind, like = got[1], section.rows[r][i] if got[1] == "money" else like
        stated = _amount(section.rows[r][total_at]) if total_at is not None else None
        for key in dict.fromkeys(keys):
            totals = sums.setdefault(key, [0.0] * max(len(measures), 1))
            for n, amount in enumerate(amounts or [1.0]):
                totals[n] += amount
            if stated:
                shown_totals[key] = shown_totals.get(key, 0.0) + stated[0]
    if not measures:
        kind = "count"
    order = list(sums)
    if spec.ranked:
        order.sort(
            key=lambda k: (-(shown_totals.get(k, 0.0) if total_at is not None else sum(sums[k])), str(k).lower())
        )
    shown, rest = order[: spec.top], order[spec.top :]
    wanted = view.filters.get((table, column), []) if view is not None else []
    series = [(c, tone) for c, tone in zip([section.columns[i] for i in measures], spec.tones, strict=False)]

    def value(keys: list[str], totals: list[float]) -> float:
        return sum(shown_totals.get(k, 0.0) for k in keys) if total_at is not None else sum(totals)

    def detail(label: str, keys: list[str], totals: list[float]) -> str:
        head = f"{label}: "
        if total_at is not None:
            head += f"{_shown(value(keys, totals), kind, like)} {spec.total.lower()}: "
        if not measures:
            return head + plural_rows(int(totals[0]))
        if len(measures) == 1 and total_at is None:
            return head + _shown(totals[0], kind, like)
        return head + ", ".join(
            f"{_shown(v, kind, like)} {c.lower()}" for v, (c, _) in zip(totals, series, strict=False)
        )

    if total_at is not None and measures:  # what the series leave of the stated total, as a grey "Other" part
        left = {k: max(shown_totals.get(k, 0.0) - sum(sums[k]), 0.0) for k in sums}
        if any(v > 0.005 * max(shown_totals.values(), default=0) for v in left.values()):
            for k in sums:
                sums[k] = [*sums[k], left[k]]
            series.append((spec.rest, "muted"))

    items = []
    for key in shown:
        label = FLAGS.get(key, key) if column == FLAG else (key or "Blank")
        href = link(path, view.toggle_value((table, column), key), anchor(table)) if view is not None else ""
        items.append(
            charts.Bar(
                label,
                sums[key],
                _shown(value([key], sums[key]), kind, like),
                href,
                key in wanted,
                detail(label, [key], sums[key]),
            )
        )
    if rest and spec.other:
        totals = [sum(sums[k][n] for k in rest) for n in range(len(sums[rest[0]]))]
        label = f"Other ({len(rest)})"
        items.append(
            charts.Bar(label, totals, _shown(value(rest, totals), kind, like), detail=detail(label, rest, totals))
        )
    title = spec.title or f"By {name.lower() if name != name.upper() else name}"
    if not spec.title and title.lower() in section.title.lower():  # "Sign-ins by country" says it already
        title = ""
    return charts.bars(
        items,
        label=f"{section.title}: {title}" if title else section.title,
        series=series if len(series) > 1 else None,
        title=title,
    )


def plural_rows(n: int) -> str:
    return f"{n:,} row" if n == 1 else f"{n:,} rows"


# -- What the page draws ----------------------------------------------------------------


@dataclass
class Option:
    label: str
    count: int
    active: bool
    href: str


@dataclass
class Column:
    name: str
    slug: str
    index: int
    kind: str  # text | date | num | flag
    hidden: bool = False
    toggle_href: str = ""  # show or hide it
    sort: str | None = None  # asc | desc
    sort_href: str = ""
    options: list[Option] = field(default_factory=list)  # values, or date presets
    filtered: bool = False
    width: str = "m"  # s | m | l, from its longest value (``_width``): how wide the page lets it be

    @property
    def heading(self) -> str:
        """The name for a heading that may wrap: never at a hyphen ("Sign-in" stays whole)."""
        return self.name.replace("-", "\N{NON-BREAKING HYPHEN}")


SHORT_TEXT = 16  # a column whose values are all this short never wraps
LONG_TEXT = 48  # one with a value longer than this gets room to wrap at a readable width


def _width(cells: list, kind: str) -> str:
    """How much room a column needs: "s" short values on one line, "l" long text, "m" in between."""
    if kind in ("num", "date"):
        return "s"
    longest = max((len(c.short or c) if isinstance(c, Many) else len(str(c)) for c in cells), default=0)
    return "s" if longest <= SHORT_TEXT else "l" if longest > LONG_TEXT else "m"


@dataclass
class Chip:
    label: str
    href: str  # the same view without this filter


@dataclass
class Group:
    """Rows that share a value of the section's ``group`` column, under one collapsible heading."""

    label: str
    anchor: str
    rows: list[tuple[list, str | None, object]]  # (cells of the shown columns, flag, row key)

    @property
    def flag(self) -> str | None:
        """The worst flag in the group."""
        return min((f for _, f, _ in self.rows), key=lambda f: FLAG_ORDER.get(f, 5), default=None)

    @property
    def words(self) -> str:
        """Every row's text, so a row search keeps the heading while any of its rows match."""
        return " ".join([self.label] + [" ".join(map(str, cells)) + f" {flag or ''}" for cells, flag, _ in self.rows])


@dataclass
class TableView:
    section: Section
    slug: str
    columns: list[Column]
    flag: Column | None
    rows: list[tuple[list, str | None]]  # (cells of the visible columns, flag)
    chips: list[Chip]
    clear_href: str
    show_all_href: str
    keys: list = field(default_factory=list)  # each shown row's key (Section.keys), for row actions
    # Rows under collapsible headings when the section has a group column and the table isn't sorted;
    # the group column is then left out of the rows, since the heading says it.
    groups: list[Group] = field(default_factory=list)
    group_column: Column | None = None
    chart: str = ""  # the section's chart of the rows shown, each bar opening its rows

    @property
    def visible(self) -> list[Column]:
        return [c for c in self.columns if not c.hidden]

    @property
    def shown(self) -> list[Column]:
        """The columns each row draws: the visible ones, less the group column while grouped."""
        return [c for c in self.visible if not (self.groups and c is self.group_column)]

    @property
    def notes(self) -> list[int]:
        """Indexes into ``shown`` of the columns the page draws as a line under each row (``NOTE_COLUMNS``)."""
        shown = self.shown
        if len(shown) < NOTES_FROM:
            return []
        return [i for i, c in enumerate(shown) if i and c.name in NOTE_COLUMNS]

    @property
    def filters(self) -> list[Column]:
        return [c for c in ([self.flag] if self.flag else []) + self.columns if c.options]

    @property
    def hidden_count(self) -> int:
        return sum(c.hidden for c in self.columns)

    @property
    def total(self) -> int:
        return len(self.section.rows)

    @property
    def flagged_href(self) -> str:
        review = [o for o in self.flag.options if o.label == "To review"] if self.flag else []
        return review[0].href if review else ""


def link(path: str, view: View, anchor: str = "") -> str:
    query = view.query()
    joined = ("&" if "?" in path else "?") + query if query else ""  # the path may carry its own (a past day)
    return f"{path}{joined}{'#' + anchor if anchor else ''}"


def anchor(table: str) -> str:
    return f"t-{table}"


def _options(
    cells: list, key: Key, view: View, path: str, table: str, kind: str, today: date, always: bool = False
) -> list[Option]:
    if kind == "date":
        counts = {p: sum(1 for c in cells if _age_matches(c, p, today)) for p in AGES}
        active = view.ages.get(key)
        return [
            Option(label, counts[p], active == p, link(path, view.toggle_age(key, p), anchor(table)))
            for p, label in AGES.items()
            if counts[p] or active == p
        ]
    counts: Counter = Counter(v for c in cells for v in values_of(c))
    wanted = view.filters.get(key, [])
    if (
        not wanted
        and not (2 <= len(counts) <= MAX_VALUES and len(counts) < len(cells))
        and not (always and len(counts) >= 2)
    ):
        return []
    values = sorted(counts, key=lambda v: (v == "", sort_key(v) is None, str(sort_key(v) or "")))
    values += [v for v in wanted if v not in counts]
    return [
        Option(v or "Blank", counts.get(v, 0), v in wanted, link(path, view.toggle_value(key, v), anchor(table)))
        for v in values
    ]


def table_view(report: DataReport, index: int, view: View, path: str) -> TableView:
    section = report.sections[index]
    table = table_slugs(report)[index]
    today = report.generated_at.date()
    slugs = column_slugs(section)
    rows = _matching_rows(section, table, view, today)
    shown = set(_visible(section, table, view))
    sort = view.sorts.get(table)
    columns = []
    for i, (name, s) in enumerate(zip(section.columns, slugs, strict=True)):
        key = (table, s)
        cells = [r[i] for r in section.rows]
        kind = _kind(cells)
        col = Column(name, s, i, kind, hidden=i not in shown, filtered=key in view.filters or key in view.ages)
        col.width = _width(cells, kind)
        col.toggle_href = link(path, view.toggle_hidden(key), anchor(table))
        col.sort = ("desc" if sort[1] else "asc") if sort and sort[0] == s else None
        # First click: text A to Z, numbers largest first; then flip.
        descending = col.sort == "asc" if col.sort else (kind == "num" and i > 0)
        col.sort_href = link(path, view.sorted_by(table, s, descending), anchor(table))
        col.options = _options(cells, key, view, path, table, kind, today, always=name in section.filter_columns)
        columns.append(col)
    flag = None
    if any(section.flags):
        key = (table, FLAG)
        counts = Counter(f or "none" for f in section.flags)
        wanted = view.filters.get(key, [])
        flag = Column(section.flag_name, FLAG, -1, "flag", filtered=key in view.filters)
        flag.sort = ("desc" if sort[1] else "asc") if sort and sort[0] == FLAG else None
        flag.sort_href = link(path, view.sorted_by(table, FLAG, flag.sort == "asc"), anchor(table))
        review = [f for f in REVIEW if counts[f]] if len(counts) > 1 or wanted else []
        if review:  # one option for everything flagged for review, then each level
            reviewing = sorted(wanted) == sorted(review)
            base = view.without(key)
            target = base if reviewing else View({**base.filters, key: review}, base.ages, base.hidden, base.sorts)
            flag.options.append(
                Option("To review", sum(counts[f] for f in review), reviewing, link(path, target, anchor(table)))
            )
        flag.options += [
            Option(label, counts[f], f in wanted, link(path, view.toggle_value(key, f), anchor(table)))
            for f, label in FLAGS.items()
            if (counts[f] and len(counts) > 1) or f in wanted
        ]
    names = {c.slug: c.name for c in columns} | {FLAG: section.flag_name}
    chips = []
    for (t, c), values in view.filters.items():
        if t == table and c in names:
            for v in values:
                label = FLAGS.get(v, v) if c == FLAG else (v or "Blank")
                chips.append(Chip(f"{names[c]}: {label}", link(path, view.toggle_value((t, c), v), anchor(table))))
    for (t, c), preset in view.ages.items():
        if t == table and c in names:
            chips.append(Chip(f"{names[c]}: {AGES[preset].lower()}", link(path, view.without((t, c)), anchor(table))))
    flags = section.flags or [None] * len(section.rows)
    visible = [i for i in range(len(section.columns)) if i in shown]
    keys = [section.keys[r] for r in rows] if len(section.keys) == len(section.rows) else []
    tv = TableView(
        section,
        table,
        columns,
        flag,
        rows=[([section.rows[r][i] for i in visible], flags[r]) for r in rows],
        chips=chips,
        clear_href=link(path, view.without_filters(table), anchor(table)),
        show_all_href=link(path, view.showing_all(table), anchor(table)),
        keys=keys,
        chart=chart(section, table, rows, view, path),
    )
    if section.group in section.columns and table not in view.sorts:
        at = section.columns.index(section.group)
        tv.group_column = columns[at]
        cells_of = [i for i in visible if i != at]
        groups: dict[str, Group] = {}
        for n, r in enumerate(rows):  # in row order, so the builder's order of groups holds
            label = str(section.rows[r][at])
            if label not in groups:
                groups[label] = Group(label, section.group_ids.get(label) or f"{table}-g-{slug(label)}", [])
            groups[label].rows.append(([section.rows[r][i] for i in cells_of], flags[r], keys[n] if keys else None))
        tv.groups = list(groups.values())
    return tv


def table_views(report: DataReport, view: View, path: str) -> list[TableView]:
    return [table_view(report, i, view, path) for i in range(len(report.sections))]


# -- Headline figures that open their rows -----------------------------------------------


def rows_view(report: DataReport, rows: Rows) -> tuple[View, str]:
    """The view (and table anchor) that shows the rows behind a headline figure."""
    slugs = table_slugs(report)
    titles = [s.title for s in report.sections]
    if rows.table not in titles:
        return View(), ""
    index = titles.index(rows.table)
    table, section = slugs[index], report.sections[index]
    names = dict(zip(section.columns, column_slugs(section), strict=True))
    if any(section.flags):  # the flag column by its name ("Severity" on Findings), values as flags
        names.setdefault(section.flag_name, FLAG)
    view = View()
    for column, value in rows.where.items():
        if column in names:
            view.filters[(table, names[column])] = list(value) if isinstance(value, (list, tuple)) else [str(value)]
    if rows.flagged:  # nothing to review still filters, so the table shows none rather than everything
        view.filters[(table, FLAG)] = [f for f in REVIEW if f in section.flags] or list(REVIEW)
    if rows.age and rows.age[0] in names and rows.age[1] in AGES:
        view.ages[(table, names[rows.age[0]])] = rows.age[1]
    return view, anchor(table)


def rows_link(report: DataReport, rows: Rows | None, path: str = "") -> str:
    """``path?query#anchor`` for a headline figure, or "" when it has nothing to point at."""
    if rows is None:
        return ""
    view, where = rows_view(report, rows)
    return link(path, view, where) if where else ""


def headline_links(report: DataReport, path: str = "") -> dict[str, list[str]]:
    """Where each tile and highlight leads. A figure named like a table leads to that table."""
    titles = {s.title for s in report.sections}

    def target(item) -> str:
        rows = getattr(item, "rows", None)
        if rows is None and item.label in titles:
            rows = Rows(item.label)
        return rows_link(report, rows, path)

    return {"tiles": [target(t) for t in report.tiles], "highlights": [target(h) for h in report.highlights]}


def flagged_link(report: DataReport, path: str = "") -> str:
    """Every row flagged for review, in every table."""
    view = View()
    first = ""
    for table, section in zip(table_slugs(report), report.sections, strict=True):
        review = [f for f in REVIEW if f in section.flags]
        if review:
            view.filters[(table, FLAG)] = review
            first = first or anchor(table)
    return link(path, view, first) if first else ""
