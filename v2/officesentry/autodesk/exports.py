"""Reading Autodesk's export files (``api.AutodeskClient.download``) into plain records.

Autodesk documents the fields of its exports but not the files' exact headers, and renames several in 2026
(``offering_name`` becomes "Product", ``product_name`` "Feature", ``team_alias`` "Team" and so on). So a column
is found by any of the names in ``FIELDS``, compared without case, spaces or punctuation, old names first, and
the columns nobody asked for are returned so the run log can name them.

Three files:

* SUBSCRIPTIONS: one row per subscription (``Subscription``).
* USERS: one row per person, or per person and product (``Seat`` when it names a product).
* USAGE_REPORT (by product): one row per person and product over the export's dates (``Use``).

When a team's admin has chosen to hash personal data in Autodesk Account, names and email addresses come back as
codes; ``hashed`` says so, and the seat is named by the code.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime

# Each field and the headers it may have, normalised (``_norm``), in the order they're tried.
FIELDS: dict[str, tuple[str, ...]] = {
    "subscription": ("subscription_id", "subscription", "subscription_number", "reporting_entity_id", "serial_number"),
    "product": ("offering_name", "product", "offering", "product_line", "product_name", "feature"),
    "seats": ("seat_quantity", "seats", "quantity", "seats_purchased", "purchased_seats", "total_seats"),
    "assigned": ("seats_assigned", "assigned_seats", "seats_in_use", "assigned_users", "used_seats", "assigned"),
    "status": ("subscription_status", "status"),
    "start": ("start_date", "subscription_start_date", "term_start_date", "subscription_start"),
    "end": (
        "end_date",
        "subscription_end_date",
        "term_end_date",
        "subscription_end",
        "renewal_date",
        "expiration_date",
    ),
    "term": ("term", "billing_term", "subscription_term", "contract_term"),
    "team": ("team_alias", "team", "team_name", "context_name"),
    "team_id": ("team_id", "context_id"),
    "email": ("email", "email_address", "user_email"),
    "first": ("first_name", "firstname"),
    "last": ("last_name", "lastname", "surname"),
    "name": ("full_name", "name", "user_name", "display_name"),
    "user_id": ("user_id", "o2_id", "oxygen_id", "hashed_user_id", "hashed_id"),
    "user_status": ("account_status", "user_status", "status"),
    "assigned_on": ("assigned_date", "assignment_date", "date_assigned", "seat_assigned_date"),
    "last_used": ("last_accessed", "last_access", "last_used", "last_usage_date", "latest_usage_date"),
    "days_used": ("days_used", "total_days_used", "total_unique_days", "usage_days"),
    "seat": ("seat_assignment", "assignment", "assigned"),
    "last_sign_in": ("last_login", "last_sign_in", "last_signin"),
}
HASHED = re.compile(r"^[0-9a-f]{32,64}$")
DATE_FORMATS = ("%m/%d/%Y", "%d-%b-%Y", "%d %b %Y", "%Y/%m/%d")


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (header or "").strip().lower()).strip("_")


def day(value: str) -> str:
    """A date as YYYY-MM-DD when it reads as one (Autodesk writes ISO dates or US month/day/year), else ""."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value.split(" ")[0] if "/" in value else value, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def number(value: str) -> int | None:
    value = (value or "").replace(",", "").strip()
    try:
        return int(float(value)) if value else None
    except ValueError:
        return None


@dataclass
class Table:
    """A file's rows as {field: value}, and the headers no field took."""

    rows: list[dict[str, str]] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def read(data: bytes) -> Table:
    """A CSV file's rows by field (``FIELDS``). Anything that isn't a CSV file gives no rows."""
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    try:
        headers = next(reader)
    except (StopIteration, csv.Error):
        return Table()
    names = [_norm(h) for h in headers]
    columns: dict[str, int] = {}
    for key, aliases in FIELDS.items():
        for alias in aliases:
            if alias in names:
                columns[key] = names.index(alias)
                break
    taken = set(columns.values())
    table = Table(unknown=[headers[i].strip() for i in range(len(headers)) if i not in taken and headers[i].strip()])
    try:
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            table.rows.append({key: row[i].strip() if i < len(row) else "" for key, i in columns.items()})
    except csv.Error:
        pass  # a broken line ends the file: keep what was read
    return table


def hashed(value: str) -> bool:
    return bool(HASHED.match((value or "").lower()))


def person(row: dict[str, str]) -> str:
    """Who a row is about: their email address, lower-cased, else Autodesk's ID or code for them."""
    email = row.get("email", "").strip().lower()
    if email and "@" in email:
        return email
    return (row.get("user_id") or email).strip().lower()


def _name(row: dict[str, str]) -> str:
    whole = row.get("name", "").strip()
    if whole and not hashed(whole):
        return whole
    joined = " ".join(p for p in (row.get("first", "").strip(), row.get("last", "").strip()) if p)
    return "" if hashed(joined.replace(" ", "")) else joined


@dataclass
class Subscription:
    id: str
    product: str
    seats: int | None
    assigned: int | None
    status: str
    start: str
    end: str
    term: str


@dataclass
class Seat:
    person: str
    name: str
    product: str
    subscription: str
    assigned_on: str
    status: str
    last_sign_in: str


@dataclass
class Use:
    person: str
    name: str
    product: str
    assigned_on: str
    last_used: str
    days_used: int | None
    assigned: bool | None


def subscriptions(table: Table) -> list[Subscription]:
    out = []
    for i, r in enumerate(table.rows):
        product = r.get("product", "")
        if not product and not r.get("subscription"):
            continue
        out.append(
            Subscription(
                id=r.get("subscription") or f"row-{i + 1}",
                product=product or "Unnamed product",
                seats=number(r.get("seats", "")),
                assigned=number(r.get("assigned", "")),
                status=r.get("status", "").capitalize(),
                start=day(r.get("start", "")),
                end=day(r.get("end", "")),
                term=r.get("term", ""),
            )
        )
    return out


def seats(table: Table) -> list[Seat]:
    out = []
    for r in table.rows:
        who = person(r)
        if not who:
            continue
        out.append(
            Seat(
                person=who,
                name=_name(r),
                product=r.get("product", ""),
                subscription=r.get("subscription", ""),
                assigned_on=day(r.get("assigned_on", "")),
                status=r.get("user_status", "").capitalize(),
                last_sign_in=day(r.get("last_sign_in", "")),
            )
        )
    return out


def _assigned(value: str) -> bool | None:
    value = value.strip().lower()
    if not value:
        return None
    if value in ("unassigned", "not assigned", "no", "false", "0"):
        return False
    return True


def uses(table: Table) -> list[Use]:
    out = []
    for r in table.rows:
        who = person(r)
        if not who or not r.get("product"):
            continue
        out.append(
            Use(
                person=who,
                name=_name(r),
                product=r["product"],
                assigned_on=day(r.get("assigned_on", "")),
                last_used=day(r.get("last_used", "")),
                days_used=number(r.get("days_used", "")),
                assigned=_assigned(r.get("seat", "")),
            )
        )
    return out
