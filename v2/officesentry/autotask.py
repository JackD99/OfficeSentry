"""Autotask PSA: a ticket in the MSP's own Autotask for the Office Sentry alerts they choose.

An admin connects it under Settings, Autotask PSA (``web/autotask.py``) with an Autotask API user: its user name,
its secret and its tracking identifier (in Autotask, Admin, Resources (Users), New API User, with "Custom
(Internal Integration)" as the integration vendor). The secret and the tracking identifier are encrypted with the
vault and never shown again. Checking the connection (``check``) finds the Autotask database's zone
(``find_zone``), reads the choices for the ticket fields (queues, statuses, priorities, types and sources) and the
active companies, which the admin matches to clients (``autotask_companies``).

Every alert goes to each ready alert destination (``alerts.deliver``), and Autotask is one (``_ready``,
``_send``). It creates one ticket for an alert in the client's company, or in the MSP's own company for Office
Sentry's own problems (and, if chosen, for clients without a company): the alert's title, its text and items and
the link back to the portal, with the priority chosen for the alert's worst severity. While a ticket made for an
alert's key is open here, the same alert doesn't make another one (system alerts repeat daily while a problem
lasts); the alert that clears it (``Message.resolves``) adds a note to that ticket and closes it here, never in
Autotask. A ticket Autotask didn't answer, or refused for a reason that can pass (``AutotaskError.retry``), is
tried again by the worker (``retry_due``, ``RETRY_AFTER``).

Every request goes to Autotask's own hosts (``*.autotask.net``), and the only writes are new tickets and their
notes, in the MSP's own Autotask. Nothing here reads or writes a client's Microsoft 365 tenant
(``tests/test_no_writes.py`` lists this file for those two POSTs).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import requests

from . import alerts
from .checks import SEVERITIES, SEVERITY_RANK
from .crypto import Vault
from .db import audit, transaction, utcnow
from .schedule import periodic
from .text import duration

log = logging.getLogger(__name__)

ZONE_LOOKUP = "https://webservices.autotask.net/atservicesrest/v1.0/zoneInformation"
TIMEOUT = 20
PAGES = 40  # pages of companies read at most (Autotask returns 500 a page)
TITLE_MAX = 255
DESCRIPTION_MAX = 8000
NOTE_TITLE_MAX = 250
ITEMS_LISTED = 50
# How long after each failed try a ticket is sent again; after the last it's left failed on the settings page.
RETRY_AFTER = (timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2), timedelta(hours=8))
KEEP_DAYS = 400  # ticket records kept here; the tickets themselves stay in Autotask
OWN_COMPANY = 0  # the MSP's own company, 0 in every Autotask database
OPEN_TICKET = "Autotask/AutotaskExtend/ExecuteCommand.aspx?Code=OpenTicketDetail&TicketID="  # after the web address

# The alerts that can make a ticket, as the settings page offers them (``kind``).
EVENTS = {
    "findings.new": "New findings",
    "activity.changes": "Risky changes in a client's tenant",
    "collection.failed": "A collection fails after working last time",
    "system": "Problems with Office Sentry itself (in your own company)",
}
PRIORITY_SEVERITIES = ("critical", "high", "medium", "low")
# The ticket fields whose choices are read from Autotask, and the setting each one fills.
TICKET_FIELDS = {"queueID": "queue", "status": "status", "ticketType": "ticket_type", "source": "source"}
NOTE_FIELDS = ("noteType", "publish")
DEFAULTS = {
    "autotask.enabled": "0",
    "autotask.username": "",
    "autotask.secret_enc": "",
    "autotask.code_enc": "",
    # Found from the user name (``find_zone``): the REST API's address and the web interface's.
    "autotask.zone_api": "",
    "autotask.zone_web": "",
    "autotask.checked_at": "",
    # JSON: {field: [[value, label, is_default], ...]} for TICKET_FIELDS, "priority" and "note.<NOTE_FIELDS>".
    "autotask.choices": "",
    "autotask.events": ",".join(EVENTS),
    "autotask.min_severity": "high",
    "autotask.queue": "",
    "autotask.status": "",
    "autotask.ticket_type": "",
    "autotask.source": "",
    **{f"autotask.priority_{s}": "" for s in PRIORITY_SEVERITIES},
    # Hours from now for the ticket's due date; empty leaves it to the ticket category.
    "autotask.due_hours": "",
    # A client without a company: "own" files its tickets in the MSP's company, "skip" makes none.
    "autotask.unmapped": "own",
}
# Settings encrypted with the vault; rotate-keys re-encrypts them (__main__.ENCRYPTED_SETTINGS).
ENCRYPTED_KEYS = ("autotask.secret_enc", "autotask.code_enc")
_SUFFIXES = re.compile(
    r"\b(the|ltd|limited|llc|llp|lp|inc|incorporated|plc|gmbh|pty|corp|corporation|co|company|bv|sa|ag)\b"
)


class AutotaskError(alerts.AlertError):
    """Why Autotask couldn't be reached or refused, in words for people. Never holds the secret. ``retry`` is
    whether trying again later can help (no answer, the hourly limit, a fault on Autotask's side)."""

    def __init__(self, message: str, retry: bool = False):
        super().__init__(message)
        self.retry = retry


# -- Settings ----------------------------------------------------------------


def settings(conn) -> dict:
    keys = tuple(DEFAULTS)
    rows = conn.execute(f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(keys))})", keys)
    return {**DEFAULTS, **{r["key"]: r["value"] for r in rows}}


def save_settings(conn, values: dict) -> None:
    for key, value in values.items():
        if key not in DEFAULTS:
            raise KeyError(key)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def choices(s: dict) -> dict[str, list]:
    try:
        return json.loads(s.get("autotask.choices") or "{}")
    except ValueError:
        return {}


def chosen_events(s: dict) -> set[str]:
    return {k for k in (s.get("autotask.events") or "").split(",") if k in EVENTS}


def credentials_saved(s: dict) -> bool:
    return all(s.get(k) for k in ("autotask.username", "autotask.secret_enc", "autotask.code_enc"))


def connected(s: dict) -> bool:
    """The connection is saved and was checked."""
    return credentials_saved(s) and bool(s.get("autotask.zone_api"))


def missing(s: dict) -> list[str]:
    """What still has to be chosen before tickets can be created, in words."""
    if not connected(s):
        return ["the connection (save it, then check it)"]
    gaps = [] if s.get("autotask.status") else ["a ticket status"]
    if not all(s.get(f"autotask.priority_{p}") for p in PRIORITY_SEVERITIES):
        gaps.append("a priority for each severity")
    return gaps


def ready(s: dict) -> bool:
    return s.get("autotask.enabled") == "1" and not missing(s)


# -- Talking to Autotask -------------------------------------------------------


@dataclass(frozen=True)
class Zone:
    api: str  # https://webservices5.autotask.net/ATServicesRest/
    web: str  # https://ww5.autotask.net/


def _autotask_url(url) -> str:
    """An address from Autotask's zone lookup, refused unless it's https on autotask.net."""
    try:
        parts = urlsplit(str(url or ""))
        port = parts.port
    except ValueError:
        port, parts = -1, None
    host = (parts.hostname or "").lower() if parts else ""
    if (
        parts is None
        or parts.scheme != "https"
        or port not in (None, 443)
        or parts.username
        or not (host == "autotask.net" or host.endswith(".autotask.net"))
    ):
        raise AutotaskError("Autotask's zone lookup gave an address outside autotask.net, so it wasn't used.")
    return f"https://{host}{parts.path.rstrip('/')}/"


def _errors(resp) -> str:
    """Autotask's own reasons ({"errors": [...]}), shortened."""
    try:
        errors = resp.json().get("errors") or []
    except (ValueError, AttributeError):
        return ""
    text = "; ".join(str(e) for e in errors if e)
    return text if len(text) <= 300 else text[:299] + "…"


def find_zone(username: str) -> Zone:
    """Which Autotask zone the API user's database is in. Needs no credentials."""
    try:
        resp = requests.get(ZONE_LOOKUP, params={"user": username}, timeout=TIMEOUT, allow_redirects=False)
    except requests.RequestException as exc:
        raise AutotaskError(f"Couldn't reach Autotask ({type(exc).__name__}).", retry=True) from exc
    if resp.status_code != 200:
        raise AutotaskError(
            "Autotask doesn't know that user name. Enter the API user's user name as Autotask shows it."
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise AutotaskError("Autotask's zone lookup didn't answer in JSON.", retry=True) from exc
    return Zone(_autotask_url(data.get("url")), _autotask_url(data.get("webUrl")))


class Client:
    """Autotask's REST API as one API user."""

    def __init__(self, api: str, username: str, secret: str, code: str):
        self.api = _autotask_url(api)
        self.host = urlsplit(self.api).hostname
        self.headers = {
            "Username": username,
            "Secret": secret,
            "ApiIntegrationCode": code,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "OfficeSentry",
        }

    def _url(self, path: str) -> str:
        return f"{self.api}v1.0/{path}"

    def _call(self, method: str, url: str, **kwargs) -> dict:
        try:
            resp = requests.request(method, url, headers=self.headers, timeout=TIMEOUT, allow_redirects=False, **kwargs)
        except requests.Timeout as exc:
            raise AutotaskError(f"Autotask didn't answer within {TIMEOUT} seconds.", retry=True) from exc
        except requests.RequestException as exc:
            # The exception's text can name the request: keep only its type.
            raise AutotaskError(f"Couldn't reach Autotask ({type(exc).__name__}).", retry=True) from exc
        code = resp.status_code
        if code == 401:
            raise AutotaskError(
                "Autotask refused the API user (HTTP 401): check its user name, secret and tracking identifier."
            )
        if code == 403:
            raise AutotaskError(
                "Autotask didn't let the API user do this (HTTP 403): give it the API User security level."
            )
        if code == 429:
            raise AutotaskError("Autotask's limit on requests was reached (HTTP 429).", retry=True)
        if code >= 300:
            reasons = _errors(resp)
            # Autotask answers HTTP 500 with its reasons when it refuses a ticket (a missing field): sending the
            # same ticket again won't help. A 5xx without reasons is a fault on its side.
            raise AutotaskError(
                f"Autotask answered HTTP {code}" + (f": {reasons}" if reasons else "."),
                retry=code >= 500 and not reasons,
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise AutotaskError("Autotask's answer wasn't JSON.", retry=True) from exc

    def query(self, entity: str, filters: list[dict], fields: tuple[str, ...] = ()) -> list[dict]:
        search = {"filter": filters} | ({"IncludeFields": list(fields)} if fields else {})
        url, params = self._url(f"{entity}/query"), {"search": json.dumps(search, separators=(",", ":"))}
        items = []
        for _ in range(PAGES):
            data = self._call("GET", url, params=params)
            items += data.get("items") or []
            following = (data.get("pageDetails") or {}).get("nextPageUrl")
            if not following:
                break
            if urlsplit(following).scheme != "https" or urlsplit(following).hostname != self.host:
                raise AutotaskError("Autotask's next page was on another host, so it wasn't read.")
            url, params = following, None
        return items

    def choices(self) -> dict[str, list]:
        """The active choices for the ticket fields Office Sentry fills, and for a ticket note's type and
        audience: {field: [[value, label, is_default], ...]} in Autotask's order."""
        found = {}
        for entity, fields, prefix in (
            ("Tickets", (*TICKET_FIELDS, "priority"), ""),
            ("TicketNotes", NOTE_FIELDS, "note."),
        ):
            data = self._call("GET", self._url(f"{entity}/entityInformation/fields"))
            for field in data.get("fields") or []:
                if field.get("name") not in fields:
                    continue
                values = sorted(
                    (v for v in field.get("picklistValues") or [] if v.get("isActive", True)),
                    key=lambda v: (v.get("sortOrder") or 0, str(v.get("label") or "")),
                )
                found[prefix + field["name"]] = [
                    [int(v["value"]), str(v.get("label") or v["value"]), bool(v.get("isDefaultValue"))]
                    for v in values
                    if str(v.get("value", "")).lstrip("-").isdigit()
                ]
        return found

    def companies(self) -> list[tuple[int, str, int | None]]:
        rows = self.query(
            "Companies", [{"op": "eq", "field": "isActive", "value": True}], ("id", "companyName", "companyType")
        )
        return [(int(r["id"]), str(r.get("companyName") or f"Company {r['id']}"), r.get("companyType")) for r in rows]

    def create_ticket(self, ticket: dict) -> int:
        data = self._call("POST", self._url("Tickets"), data=json.dumps(ticket))
        if not isinstance(data.get("itemId"), int):
            raise AutotaskError("Autotask didn't say which ticket it created.")
        return data["itemId"]

    def ticket_number(self, ticket_id: int) -> str | None:
        item = self._call("GET", self._url(f"Tickets/{int(ticket_id)}")).get("item") or {}
        return item.get("ticketNumber")

    def add_note(self, ticket_id: int, title: str, text: str, note_type: int, publish: int) -> int:
        note = {
            "ticketID": int(ticket_id),
            "title": title[:NOTE_TITLE_MAX],
            "description": text,
            "noteType": note_type,
            "publish": publish,
        }
        return self._call("POST", self._url("TicketNotes"), data=json.dumps(note)).get("itemId")


def client_for(vault: Vault, s: dict) -> Client:
    try:
        secret = vault.decrypt(s["autotask.secret_enc"])
        code = vault.decrypt(s["autotask.code_enc"])
    except Exception as exc:
        raise AutotaskError("The saved Autotask secret can't be decrypted. Enter it again.") from exc
    return Client(s["autotask.zone_api"], s["autotask.username"], secret, code)


def ticket_link(s: dict, ticket_id: int | None) -> str | None:
    """Opens the ticket in Autotask's web interface (its ExecuteCommand URL)."""
    if not ticket_id or not s.get("autotask.zone_web"):
        return None
    return f"{s['autotask.zone_web']}{OPEN_TICKET}{int(ticket_id)}"


# -- Checking the connection ---------------------------------------------------


@dataclass
class Checked:
    companies: int
    queues: int


def _default(values: list, *labels: str) -> str:
    """The choice whose label is one of ``labels``, else Autotask's default, as a setting value."""
    for label in labels:
        for value, name, _ in values:
            if name.strip().lower() == label:
                return str(value)
    return next((str(value) for value, _, default in values if default), "")


def check(conn, vault: Vault, s: dict | None = None) -> Checked:
    """Find the zone, read the ticket choices and the companies, and keep them. Raises AutotaskError."""
    s = s or settings(conn)
    if not credentials_saved(s):
        raise AutotaskError("Save the API user's user name, secret and tracking identifier first.")
    zone = find_zone(s["autotask.username"])
    client = client_for(vault, s | {"autotask.zone_api": zone.api})
    found = client.choices()
    companies = client.companies()
    values = {
        "autotask.zone_api": zone.api,
        "autotask.zone_web": zone.web,
        "autotask.choices": json.dumps(found, separators=(",", ":")),
        "autotask.checked_at": utcnow(),
    }
    # Fill in what nobody chose yet: Autotask's default status, and the priority named like each severity.
    if not s["autotask.status"]:
        values["autotask.status"] = _default(found.get("status", []), "new")
    for severity in PRIORITY_SEVERITIES:
        if not s[f"autotask.priority_{severity}"]:
            labels = (severity, "urgent") if severity == "critical" else (severity,)
            values[f"autotask.priority_{severity}"] = _default(found.get("priority", []), *labels)
    with transaction(conn):
        save_settings(conn, values)
        conn.execute("DELETE FROM autotask_company_list")
        conn.executemany(
            "INSERT OR REPLACE INTO autotask_company_list (company_id, name, company_type) VALUES (?, ?, ?)",
            companies,
        )
        # A client's company that was deleted or made inactive in Autotask keeps its mapping (and its name), so
        # the page can say so instead of quietly filing tickets elsewhere.
    return Checked(len(companies), len(found.get("queueID", [])))


def company_list(conn) -> list:
    return conn.execute("SELECT * FROM autotask_company_list ORDER BY name COLLATE NOCASE, company_id").fetchall()


def mappings(conn) -> dict[int, dict]:
    rows = conn.execute(
        "SELECT m.*, l.company_id IS NOT NULL AS listed FROM autotask_companies m"
        " LEFT JOIN autotask_company_list l ON l.company_id = m.company_id"
    )
    return {r["tenant_id"]: dict(r) for r in rows}


def _normalised(name: str) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", name.lower().replace("&", " and "))
    return " ".join(_SUFFIXES.sub(" ", text).split())


def suggestions(conn, tenants) -> dict[int, dict]:
    """A company for each client that has none, where exactly one company's name matches the client's once
    "Ltd", "Inc" and punctuation are left out."""
    by_name: dict[str, list] = {}
    for row in company_list(conn):
        by_name.setdefault(_normalised(row["name"]), []).append(row)
    mapped = mappings(conn)
    found = {}
    for tenant in tenants:
        if tenant["id"] in mapped:
            continue
        matches = by_name.get(_normalised(tenant["name"]), [])
        if len(matches) == 1:
            found[tenant["id"]] = dict(matches[0])
    return found


def set_company(conn, tenant_id: int, company: dict | None, by: str) -> bool:
    """Map one client to a company (None removes it). Returns whether anything changed."""
    current = conn.execute("SELECT company_id FROM autotask_companies WHERE tenant_id = ?", (tenant_id,)).fetchone()
    if company is None:
        if current is None:
            return False
        conn.execute("DELETE FROM autotask_companies WHERE tenant_id = ?", (tenant_id,))
        return True
    if current is not None and current["company_id"] == company["company_id"]:
        return False
    conn.execute(
        "INSERT INTO autotask_companies (tenant_id, company_id, company_name, mapped_by, mapped_at)"
        " VALUES (?, ?, ?, ?, ?) ON CONFLICT(tenant_id) DO UPDATE SET company_id = excluded.company_id,"
        " company_name = excluded.company_name, mapped_by = excluded.mapped_by, mapped_at = excluded.mapped_at",
        (tenant_id, company["company_id"], company["name"], by, utcnow()),
    )
    return True


# -- Tickets -------------------------------------------------------------------


def kind(event: str) -> str | None:
    """Which EVENTS choice an alert falls under."""
    if event.startswith("system.") or event == "certificate.expiry":
        return "system"
    if event == "collection.recovered":
        return "collection.failed"
    return event if event in EVENTS else None


def _rank(severity: str | None) -> int:
    return SEVERITY_RANK.get(severity or "", len(SEVERITIES))


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(value, limit: int) -> str:
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def description(message: alerts.Message) -> str:
    """The ticket's text: the alert's sentence, the link back, then its items (plain text: Autotask's API
    doesn't take rich text)."""
    items = message.items
    lines = [_clean(message.text, 2000), "", f"{message.button}: {message.link}"]
    if items:
        lines.append("")
    for item in items[:ITEMS_LISTED]:
        head = f"{(item.label or item.severity or '').capitalize()}: {item.title}"
        lines.append("- " + _clean(head + (f" ({item.subject})" if item.subject else ""), 400))
        if item.detail:
            lines.append("  " + _clean(item.detail, 400))
    if len(items) > ITEMS_LISTED:
        lines.append(f"- and {len(items) - ITEMS_LISTED} more in Office Sentry")
    lines += ["", "Created by Office Sentry."]
    text = "\n".join(lines)
    return text if len(text) <= DESCRIPTION_MAX else text[: DESCRIPTION_MAX - 1] + "…"


def priority_severity(message: alerts.Message) -> str:
    """Which of PRIORITY_SEVERITIES sets the ticket's priority: the alert's severity (its worst item's), low for
    one that says nothing is wrong."""
    if message.severity in PRIORITY_SEVERITIES:
        return message.severity
    return "low" if message.severity == alerts.INFO else "medium"


def build_ticket(s: dict, message: alerts.Message, company: int, now: datetime) -> dict:
    """The ticket for one alert: its title, text, company, status, the priority for its severity and the chosen
    queue, type, source and due date."""
    ticket = {
        "companyID": company,
        "title": _clean(message.title, TITLE_MAX),
        "description": description(message),
        "status": int(s["autotask.status"]),
        "priority": int(s[f"autotask.priority_{priority_severity(message)}"]),
    }
    for key, field in (("queue", "queueID"), ("ticket_type", "ticketType"), ("source", "source")):
        if s.get(f"autotask.{key}"):
            ticket[field] = int(s[f"autotask.{key}"])
    if s.get("autotask.due_hours"):
        ticket["dueDateTime"] = _iso(now + timedelta(hours=int(s["autotask.due_hours"])))
    return ticket


def company_for(conn, s: dict, tenant: dict | None) -> int | None:
    """The client's company, the MSP's own for an alert about Office Sentry itself, or None when the client
    has none and the settings say not to file its tickets in the MSP's company."""
    if tenant is None:
        return OWN_COMPANY
    row = conn.execute("SELECT company_id FROM autotask_companies WHERE tenant_id = ?", (tenant["id"],)).fetchone()
    if row is not None:
        return row["company_id"]
    return OWN_COMPANY if s.get("autotask.unmapped") == "own" else None


def _open_ticket(conn, key: str):
    """The ticket made for this alert key that's still open here: created, or still being tried."""
    return conn.execute(
        "SELECT * FROM autotask_tickets WHERE alert_key = ? AND resolved_at IS NULL"
        " AND (ticket_id IS NOT NULL OR retry_at IS NOT NULL) ORDER BY id DESC LIMIT 1",
        (key,),
    ).fetchone()


def _attempt(conn, client: Client, row, now: datetime) -> str:
    """Send one recorded ticket. Returns its number; raises AutotaskError, saying when it's tried again."""
    attempts = row["attempts"] + 1
    try:
        ticket_id = client.create_ticket(json.loads(row["payload"]))
    except AutotaskError as exc:
        again = exc.retry and attempts <= len(RETRY_AFTER)
        retry_at = _iso(now + RETRY_AFTER[attempts - 1]) if again else None
        conn.execute(
            "UPDATE autotask_tickets SET attempts = ?, error = ?, retry_at = ? WHERE id = ?",
            (attempts, str(exc)[:500], retry_at, row["id"]),
        )
        if again:
            raise AutotaskError(
                f"{exc} Trying again in {duration(RETRY_AFTER[attempts - 1].total_seconds())}.", retry=True
            ) from exc
        raise
    try:
        number = client.ticket_number(ticket_id)
    except AutotaskError:
        number = None  # created all the same: the page links it by its ID
    conn.execute(
        "UPDATE autotask_tickets SET attempts = ?, ticket_id = ?, ticket_number = ?, error = NULL, retry_at = NULL,"
        " sent_at = ? WHERE id = ?",
        (attempts, ticket_id, number, _iso(now), row["id"]),
    )
    return number or f"ticket {ticket_id}"


def _record(conn, message: alerts.Message, ticket: dict, now: datetime) -> int:
    return conn.execute(
        "INSERT INTO autotask_tickets (tenant_id, event, alert_key, title, company_id, payload, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            message.tenant["id"] if message.tenant else None,
            message.event,
            message.key or None,
            ticket["title"],
            ticket["companyID"],
            json.dumps(ticket, separators=(",", ":")),
            _iso(now),
        ),
    ).lastrowid


def create(conn, vault: Vault, s: dict, message: alerts.Message, now: datetime | None = None) -> str:
    """Record and send the ticket for one alert. Returns the ticket's number; raises AlertError."""
    now = now or datetime.now(UTC)
    company = company_for(conn, s, message.tenant)
    if company is None:
        raise AutotaskError("The client has no Autotask company.")
    if missing(s):
        raise AutotaskError("Choose " + " and ".join(missing(s)) + " under Settings, Autotask PSA.")
    ticket = build_ticket(s, message, company, now)
    row_id = _record(conn, message, ticket, now)
    row = conn.execute("SELECT * FROM autotask_tickets WHERE id = ?", (row_id,)).fetchone()
    return _attempt(conn, client_for(vault, s), row, now)


def _note_choice(found: dict, field: str, *labels: str) -> int | None:
    value = _default(found.get(f"note.{field}", []), *labels)
    return int(value) if value else None


def resolve(conn, vault: Vault, s: dict, message: alerts.Message, now: datetime | None = None) -> bool:
    """Close the open ticket for a cleared alert here and add a note to it in Autotask. Returns False when no
    ticket was open for it; raises AlertError when the note couldn't be added (the ticket is closed here)."""
    row = _open_ticket(conn, message.key) if message.key else None
    if row is None:
        return False
    now = now or datetime.now(UTC)
    conn.execute("UPDATE autotask_tickets SET resolved_at = ?, retry_at = NULL WHERE id = ?", (_iso(now), row["id"]))
    if not row["ticket_id"]:
        return True  # never created: stop trying
    found = choices(s)
    note_type = _note_choice(found, "noteType", "task summary", "summary")
    publish = _note_choice(found, "publish", "all autotask users", "all")
    if note_type is None or publish is None:
        raise AutotaskError("Autotask's note types weren't read: check the connection again.")
    text = f"{_clean(message.text, 2000)}\n\n{message.button}: {message.link}\n\nAdded by Office Sentry."
    client_for(vault, s).add_note(row["ticket_id"], _clean(message.title, NOTE_TITLE_MAX), text, note_type, publish)
    return True


def _ready(conn, alert_settings: dict) -> bool:
    """alerts.Destination.ready: connected, turned on and every choice made."""
    return ready(settings(conn))


def _min_severity(conn, alert_settings: dict) -> str:
    """alerts.Destination.min_severity: new findings rated this or worse make a ticket."""
    return settings(conn)["autotask.min_severity"]


def _send(conn, alerting: alerts.Alerting, message: alerts.Message, alert_settings: dict):
    """alerts.Destination.send: a ticket for the alert, a note for the alert that clears one, or False when the
    settings want neither. Raises AlertError."""
    s = settings(conn)
    chosen = kind(message.event)
    if chosen is None or chosen not in chosen_events(s):
        return False
    if message.resolves:
        return resolve(conn, alerting.vault, s, message)
    if message.key and _open_ticket(conn, message.key) is not None:
        return False  # the same problem as an open ticket: a daily reminder, the next warning
    if company_for(conn, s, message.tenant) is None:
        return False
    create(conn, alerting.vault, s, message)
    return True


def test_ticket(conn, vault: Vault, base_url: str, username: str) -> str:
    """A ticket in the MSP's own company, for the settings page's button. Returns its number; raises AlertError."""
    s = settings(conn)
    message = alerts.Message(
        "test",
        "Office Sentry test ticket",
        f"{username} checked that Office Sentry can create tickets in Autotask. You can close this ticket.",
        base_url.rstrip("/"),
    )
    return create(conn, vault, s, message)


# -- Trying again ----------------------------------------------------------------


def retry_due(conn, vault: Vault, now: datetime | None = None) -> int:
    """Send again the tickets whose next try is due. Returns how many were tried."""
    now = now or datetime.now(UTC)
    rows = conn.execute(
        "SELECT * FROM autotask_tickets WHERE retry_at IS NOT NULL AND retry_at <= ? ORDER BY id LIMIT 20",
        (_iso(now),),
    ).fetchall()
    if not rows:
        return 0
    s = settings(conn)
    if not ready(s):
        conn.execute(
            "UPDATE autotask_tickets SET retry_at = NULL, error = 'Not sent: Autotask was turned off.'"
            " WHERE retry_at IS NOT NULL"
        )
        return 0
    client = client_for(vault, s)
    for row in rows:
        what = f"{row['event']} via autotask, tried again"
        try:
            number = _attempt(conn, client, row, now)
        except AutotaskError as exc:
            audit(conn, None, "alert_failed", row["tenant_id"], f"{what}: {exc}"[:500])
        else:
            audit(conn, None, "alert_sent", row["tenant_id"], f"{what}: {number}"[:500])
    return len(rows)


def retry_one(conn, vault: Vault, ticket_row_id: int, now: datetime | None = None) -> str:
    """Send a failed ticket again now, from the settings page. Returns its number; raises AlertError."""
    row = conn.execute(
        "SELECT * FROM autotask_tickets WHERE id = ? AND ticket_id IS NULL AND resolved_at IS NULL", (ticket_row_id,)
    ).fetchone()
    if row is None:
        raise AutotaskError("That ticket was created already, or its alert has cleared.")
    s = settings(conn)
    if not ready(s):
        raise AutotaskError("Turn Autotask on and finish its settings first.")
    return _attempt(conn, client_for(vault, s), row, now or datetime.now(UTC))


def prune(conn, now: datetime | None = None) -> int:
    cutoff = _iso((now or datetime.now(UTC)) - timedelta(days=KEEP_DAYS))
    return conn.execute("DELETE FROM autotask_tickets WHERE created_at < ?", (cutoff,)).rowcount


@periodic("autotask_retries")
def _job(ctx) -> None:
    """Registered with the worker through ``worker.JOB_MODULES``."""
    retry_due(ctx.conn, ctx.vault, ctx.now)
    if ctx.now.minute == 0:
        prune(ctx.conn, ctx.now)


def recent(conn, limit: int = 15) -> list:
    return conn.execute(
        "SELECT k.*, t.name AS tenant_name FROM autotask_tickets k LEFT JOIN tenants t ON t.id = k.tenant_id"
        " ORDER BY k.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


alerts.register(alerts.Destination("autotask", _ready, _send, _min_severity))
