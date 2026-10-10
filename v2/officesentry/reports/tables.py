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
    if text.startswith(FORMULA_START):
        return "'" + text
    return text


def to_csv(table: Table) -> bytes:
    out = StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow([csv_cell(c) for c in table.columns])
    for row in table.rows:
        writer.writerow([csv_cell(v) for v in row])
    return ("﻿" + out.getvalue()).encode("utf-8")  # BOM so Excel reads UTF-8


def sheet_name(title: str, used: set[str]) -> str:
    name = re.sub(r"[\[\]:*?/\\]", " ", title).strip()[:31] or "Sheet"
    base, n = name, 2
    while name.lower() in used:
        suffix = f" ({n})"
        name = base[: 31 - len(suffix)] + suffix
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
