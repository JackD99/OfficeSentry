"""Reading a reseller's price list: a pasted or uploaded CSV of what each licence costs you.

Microsoft's list prices only ship for US dollars (``list_prices``), and an MSP buying through a reseller
pays that reseller's rates anyway, so the other way to fill the prices page is the price list the reseller
exports. Every reseller's export looks different, so nothing here asks for a fixed layout: it picks the
licence column and the price column out of the headings, matches each row to a licence by its part number
or its product name, and hands back what it found. Nothing is written until the page has shown it.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from .db import audit, utcnow
from .licences import SKU_NAMES

MAX_BYTES = 400_000  # a price list is a few hundred rows; this is room for thousands
MAX_ROWS = 5_000

# Headings a price list uses for the money, the part number and the product's name. A heading matches
# when it holds one of these words, so "Unit Price (GBP)" and "Your cost per month" both count.
PRICE_WORDS = ("price", "cost", "rate", "erp", "msrp", "rrp", "amount")
RETAIL_WORDS = ("list", "retail", "rrp", "msrp", "customer", "recommended")  # not what you pay
PART_WORDS = ("sku", "part", "product code", "product id", "offer id", "item code", "material")
NAME_WORDS = ("product", "name", "description", "offer", "licence", "license", "item", "plan", "service")
YEARLY_WORDS = ("year", "annual", "yr", "p1y", "12 month", "12m")

CURRENCIES = {"£": "GBP", "$": "USD", "€": "EUR"}
CURRENCY_CODES = ("GBP", "USD", "EUR", "AUD", "CAD", "NZD")

# Words a reseller adds to a product's name that say nothing about which licence it is.
NOISE = {
    "annual", "annually", "monthly", "yearly", "month", "months", "year", "yr", "nce", "csp", "commercial",
    "subscription", "subscriptions", "licence", "licences", "license", "licenses", "user", "users", "seat",
    "seats", "per", "prepaid", "commitment", "term", "billed", "new", "commerce", "experience", "sub",
}  # fmt: skip
UNITS = ("user", "users", "seat", "seats")  # "1 user" says how it's counted, not which licence it is

# What resellers call licences Microsoft has renamed, or names them another way.
ALIASES = {
    "Azure Active Directory Premium P1": "AAD_PREMIUM",
    "Azure Active Directory Premium Plan 1": "AAD_PREMIUM",
    "Azure AD Premium P1": "AAD_PREMIUM",
    "Entra ID P1": "AAD_PREMIUM",
    "Azure Active Directory Premium P2": "AAD_PREMIUM_P2",
    "Azure Active Directory Premium Plan 2": "AAD_PREMIUM_P2",
    "Azure AD Premium P2": "AAD_PREMIUM_P2",
    "Entra ID P2": "AAD_PREMIUM_P2",
    "Office 365 Business Essentials": "O365_BUSINESS_ESSENTIALS",
    "Office 365 Business Premium": "O365_BUSINESS_PREMIUM",
    "Microsoft 365 Business Standard (no Teams) EEA": "Microsoft_365_Business_Standard_EEA_(no_Teams)",
    "Office 365 Advanced Threat Protection (Plan 1)": "ATP_ENTERPRISE",
    "Microsoft Defender for Office 365 P1": "ATP_ENTERPRISE",
    "Microsoft Defender for Office 365 P2": "THREAT_INTELLIGENCE",
    "Microsoft Intune": "INTUNE_A",
    "Intune Plan 1": "INTUNE_A",
    "Enterprise Mobility and Security E3": "EMS",
    "Enterprise Mobility and Security E5": "EMSPREMIUM",
    "Project Professional": "PROJECTPROFESSIONAL",
    "Visio Online Plan 2": "VISIOCLIENT",
    "Visio Professional": "VISIOCLIENT",
    "Copilot for Microsoft 365": "Microsoft_365_Copilot",
    "Teams Phone": "MCOEV",
    "Power BI Professional": "POWER_BI_PRO",
}


def heading(text: str) -> str:
    """A column heading or product name, flattened: lower case, punctuation as spaces, one space between."""
    return " ".join(re.sub(r"[^0-9a-z+]+", " ", text.replace("﻿", "").lower()).split())


def normalise(name: str) -> str:
    """A product's name with the reseller's extras taken off, for matching: "Microsoft 365 Business Premium
    (NCE, Annual commitment)" and "Microsoft 365 Business Premium" come out the same."""
    words = heading(name).replace("+", " ").split()
    keep = []
    for n, word in enumerate(words):
        following = words[n + 1] if n + 1 < len(words) else ""
        if word not in NOISE and not (word.isdigit() and following in UNITS):
            keep.append(word)
    return " ".join(keep)


def _names() -> dict[str, list[str]]:
    """Every name a licence can be written as, flattened, to the part numbers it could mean. "Microsoft" is
    dropped as well as kept, since a reseller may leave it off or put it in front of an Office 365 plan."""
    out: dict[str, list[str]] = {}
    for part, name in list(SKU_NAMES.items()) + [(p, a) for a, p in ALIASES.items()]:
        for key in {normalise(name), normalise(name).replace("microsoft ", "")}:
            if key:
                out.setdefault(key, []).append(part)
    return out


NAMES = _names()


def part_for(text: str, known: set[str] = frozenset()) -> tuple[str | None, str]:
    """The licence a cell names, and how it was matched: its part number, else its product name. ``known``
    is the part numbers already seen at a client, which settles a name two licences share."""
    cell = text.strip()
    if not cell:
        return None, ""
    for part in (cell, cell.upper().replace(" ", "_"), cell.replace(" ", "_")):
        if part in SKU_NAMES or part in known:
            return part, "part number"
    flat = normalise(cell)
    parts = NAMES.get(flat) or NAMES.get(flat.replace("microsoft ", "")) or []
    for part in parts:
        if part in known:
            return part, "name"
    return (parts[0], "name") if parts else (None, "")


def money(text: str) -> float | None:
    """A price cell as a number: "£18.10", "18,10", "1,234.56" and "7.00 USD" all read, "-" and "Included"
    don't."""
    digits = re.sub(r"[^\d,.\-]", "", text.strip())
    if not re.search(r"\d", digits):
        return None
    if "," in digits and "." in digits:  # the last separator is the decimal point
        digits = digits.replace("," if digits.rindex(".") > digits.rindex(",") else ".", "").replace(",", ".")
    elif "," in digits:
        digits = digits.replace(",", "." if re.search(r",\d{1,2}$", digits) else "")
    try:
        value = float(digits)
    except ValueError:
        return None
    return round(value, 2) if 0 <= value <= 100_000 else None


@dataclass
class Row:
    """One line of the price list, matched to a licence or not."""

    line: int
    text: str  # what the file called the licence
    part: str | None = None
    how: str = ""  # "part number" or "name"
    price: float | None = None
    was: float | None = None  # the price Office Sentry holds now
    problem: str = ""

    @property
    def name(self) -> str:
        return SKU_NAMES.get(self.part or "", self.part or "")

    @property
    def ok(self) -> bool:
        return bool(self.part) and self.price is not None and not self.problem

    @property
    def changes(self) -> bool:
        return self.ok and self.price != self.was


@dataclass
class Parsed:
    rows: list[Row] = field(default_factory=list)
    error: str | None = None  # a problem with the whole file
    columns: tuple[str, str] = ("", "")  # the headings it read the licence and the price from
    currency: str | None = None  # the currency the file names, if it names one
    yearly: bool = False  # the price column is a year's price

    @property
    def matched(self) -> list[Row]:
        return [r for r in self.rows if r.ok]

    @property
    def changing(self) -> list[Row]:
        return [r for r in self.rows if r.changes]

    @property
    def unmatched(self) -> list[Row]:
        return [r for r in self.rows if not r.ok]


def _dialect(text: str):
    try:
        return csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def _columns(header: list[str]) -> tuple[int | None, list[int]]:
    """The price column and the columns that could name a licence, best first."""
    flat = [heading(h) for h in header]
    prices = [i for i, h in enumerate(flat) if any(w in h for w in PRICE_WORDS)]
    yours = [i for i in prices if not any(w in flat[i] for w in RETAIL_WORDS)]
    licences = [i for i, h in enumerate(flat) if any(w in h for w in PART_WORDS)]
    licences += [i for i, h in enumerate(flat) if any(w in h for w in NAME_WORDS) and i not in licences]
    price = (yours or prices or [None])[0]
    return price, [i for i in licences if i != price]


def parse(conn, text: str, yearly: bool | None = None, known: set[str] | None = None) -> Parsed:
    """What a pasted or uploaded price list holds, row by row, without writing anything. ``yearly`` divides
    the prices by 12 (None: whatever the price column's heading says)."""
    if len(text.encode("utf-8", errors="replace")) > MAX_BYTES:
        return Parsed(error=f"That price list is over {MAX_BYTES // 1000} kB. Export just the licence rows.")
    try:
        rows = [
            r for r in csv.reader(io.StringIO(text.replace("\x00", "")), _dialect(text)) if any(c.strip() for c in r)
        ]
    except csv.Error as exc:
        return Parsed(error=f"That doesn't read as a CSV file ({exc}).")
    if len(rows) < 2:
        return Parsed(error="Paste the price list with its headings, or choose the file your reseller exported.")
    if len(rows) > MAX_ROWS:
        return Parsed(error=f"That price list has over {MAX_ROWS:,} rows. Export just the licence rows.")
    header, body = rows[0], rows[1:]
    price_col, licence_cols = _columns(header)
    if price_col is None or not licence_cols:
        seen = ", ".join(h.strip() for h in header if h.strip()) or "none"
        return Parsed(
            error="I couldn't find both a licence column and a price column. A price list needs a column of"
            f" product names or part numbers and a column of prices; the headings on the first row are: {seen}."
        )
    if known is None:
        known = {r["sku_part_number"] for r in conn.execute("SELECT sku_part_number FROM sku_prices")}
    held = {r["sku_part_number"]: r["monthly_price"] for r in conn.execute("SELECT * FROM sku_prices")}
    per_year = yearly if yearly is not None else any(w in heading(header[price_col]) for w in YEARLY_WORDS)
    licence_heads = " and ".join(header[i].strip() for i in licence_cols if header[i].strip())
    out = Parsed(columns=(licence_heads, header[price_col].strip()), yearly=per_year)
    first: dict[str, int] = {}  # a licence's first priced row, so a list holding both terms takes one
    for n, row in enumerate(body, start=2):
        cell = {i: row[i].strip() if i < len(row) else "" for i in licence_cols}
        cells = [cell[i] for i in licence_cols]  # part numbers first, then names, for matching
        raw = row[price_col] if price_col < len(row) else ""
        part, how = next(((p, h) for p, h in (part_for(c, known) for c in cells) if p), (None, ""))
        price = money(raw)
        if price is not None and per_year:
            price = round(price / 12, 2)
        item = Row(
            line=n,
            text=next((cell[i] for i in sorted(licence_cols) if cell[i]), ""),  # as the file reads
            part=part,
            how=how,
            price=price,
            was=held.get(part or ""),
        )
        if part and price is None:
            item.problem = "No price I could read" if raw.strip() else "No price"
        elif not part:
            item.problem = "Not a licence Office Sentry reports on"
        elif part in first:
            item.problem = f"The same licence as row {first[part]}"
        else:
            first[part] = n
        out.rows.append(item)
        out.currency = out.currency or _currency(raw) or _currency(header[price_col])
    return out


def _currency(text: str) -> str | None:
    for symbol, code in CURRENCIES.items():
        if symbol in text:
            return code
    for code in CURRENCY_CODES:
        if re.search(rf"\b{code}\b", text, re.I):
            return code
    return None


def save(conn, parsed: Parsed, username: str) -> int:
    """Write the prices this list matched, and say how many changed. The caller holds the transaction."""
    changed = parsed.changing
    for row in changed:
        conn.execute(
            "INSERT INTO sku_prices (sku_part_number, monthly_price, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(sku_part_number) DO UPDATE SET monthly_price = excluded.monthly_price,"
            " updated_at = excluded.updated_at",
            (row.part, row.price, utcnow()),
        )
    if changed:
        audit(conn, username, "prices_imported", detail=f"{len(changed)} from a price list")
    return len(changed)
