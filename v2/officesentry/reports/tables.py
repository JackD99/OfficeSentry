"""Plain tables and their CSV and XLSX exports, shared by every report and rollup.

Collected values come from client tenants (display names, rule names, app
names), so exports must never let one become a spreadsheet formula. CSV cells
that start with a formula character are prefixed with ``'``; XLSX cells are
always written as text or numbers, never formulas.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from io import StringIO

FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n")
# A change that is only a signed number, with or without a unit ("+2", "-1.5", "+4.6%", "+£23.00", "+6.5 GB",
# "+4.6 points"), can't be a formula: no prefix, since Excel shows the prefix as a stray apostrophe.
SIGNED_NUMBER = re.compile(r"[+-][£$€]?\d[\d,]*(?:\.\d+)?%?(?: [A-Za-z]+){0,3}")
RESERVED_SHEET = "history"  # Excel keeps this sheet name for itself and offers to repair a workbook that uses it
SHEET_NAME_MAX = 31  # Excel's limit


@dataclass
class Table:
    title: str
    columns: list[str]
    rows: list[list] = field(default_factory=list)


def csv_cell(value) -> str | int | float:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple, set)):
        value = ", ".join(str(v) for v in value)
    text = str(value)
    if text.startswith(FORMULA_START) and not SIGNED_NUMBER.fullmatch(text):
        return "'" + text
    return text


def to_csv(table: Table) -> bytes:
    out = StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow([csv_cell(c) for c in table.columns])
    for row in table.rows:
        writer.writerow([csv_cell(v) for v in row])
    return ("﻿" + out.getvalue()).encode("utf-8")  # BOM so Excel reads UTF-8


def fit(text: str, limit: int) -> str:
    """``text`` within ``limit`` characters: whole when it fits, else cut at a word and ended with an ellipsis
    ("Large mailboxes without an…"), never mid-word ("Large mailboxes without an arch")."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    at_word = cut.rsplit(" ", 1)[0].rstrip(" ,;:(-") if " " in cut else ""
    return (at_word if len(at_word) >= limit // 2 else cut.rstrip()) + "…"


def sheet_name(title: str, used: set[str]) -> str:
    """A sheet's name for ``title``: within Excel's 31 characters, cut at a word, and unlike any in ``used``
    (which it joins). The Summary sheet maps every sheet to its table's full title."""
    name = fit(re.sub(r"[\[\]:*?/\\\x00-\x1f]", " ", title).strip(), SHEET_NAME_MAX) or "Sheet"
    base, n = name, 2
    while name.lower() in used or name.lower() == RESERVED_SHEET:
        suffix = f" ({n})"
        name = fit(base, SHEET_NAME_MAX - len(suffix)) + suffix
        n += 1
    used.add(name.lower())
    return name


def to_xlsx(tables: list[Table], cover=None, accent: str = "") -> bytes:
    """One styled sheet per table (see ``workbook``), after a summary sheet when ``cover`` is given."""
    from .workbook import Book

    book = Book(accent or (cover.accent if cover else ""))
    for table in tables or [Table("No data", ["Note"], [["Nothing to export."]])]:
        book.table(table.title, table.columns, table.rows)
    return book.save(cover)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "report"
