"""Onboarding many client tenants: CSV import, where each one has got, and confirming consent.

* ``parse()`` reads a CSV of tenants (name, tenant ID or domain, group, contact, cloud) into rows to
  show before anything is created; ``create()`` adds the new ones in one transaction. Imported tenants
  are ordinary tenants: admins see them, analysts only when granted them (one by one, or through a
  group the import put them in, see ``access._GRANTED``).
* ``audit_log_on()`` says whether a client's Microsoft 365 audit log is on, from its latest mailbox collection, for
  the tracker's Audit log column.
* ``state()`` says where a tenant has got (``STATES``: not sent, sent, approved, consented, Global
  Reader missing, new permissions to approve, sent or approved, connected), from stored state only: the
  tenant row, its ``onboarding`` row, its ``permission_requests`` row for the current batch and the
  ``connection`` collector's latest result. Pages never call Microsoft for it.
* A connected client is asked for a new batch of permissions (``permission_batch``) with the same link:
  marking it sent and coming back through it are recorded per batch, and the next connection check reads
  what was granted.
* Consent is only ever recorded by signing in. ``confirm()`` asks Microsoft for a token as the app and
  reads ``/organization`` (``connection_check.check``, one GET); when that works the tenant is consented
  and its first collection queued. It runs as a ``consent_check`` task: queued when a client's admin
  comes back from the consent link they were sent (``web/onboarding.client_returned``), from the
  tracker's "Check now", and by the ``consent_checks`` periodic job for tenants still waiting.

Nothing here writes to any tenant, and nothing calls Partner Center.
"""

from __future__ import annotations

import codecs
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import clouds, connection_check, lifecycle, permission_batch, store, tasks, tenant_groups, tenant_health
from .db import audit, transaction, utcnow
from .devices import parse_ts
from .discovery import tenant_id_for_domain
from .mailer import valid_email
from .schedule import periodic

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-zA-Z0-9-]{1,63}\.)+[a-zA-Z]{2,63}$")

MAX_ROWS = 1000
MAX_BYTES = 400_000  # under Werkzeug's 500 kB limit on a form field, since the preview posts the text back
NAME_MAX = 200
CONTACT_MAX = 200

# Column headings people use, matched without case, spaces, dashes or underscores.
COLUMNS: dict[str, tuple[str, ...]] = {
    "name": (
        "name",
        "client",
        "client name",
        "company",
        "company name",
        "customer",
        "customer name",
        "display name",
        "tenant name",
        "organisation",
        "organization",
    ),
    "tenant": (
        "tenant",
        "tenant id",
        "tenantid",
        "tenant domain",
        "tenant id or domain",
        "domain",
        "primary domain",
        "default domain",
        "directory id",
        "directory tenant id",
        "tenant ref",
    ),
    "group": ("group", "groups", "tenant group", "tenant groups"),
    "contact": ("contact", "client contact"),
    "contact_name": ("contact name", "client contact name"),
    "contact_email": ("contact email", "client contact email", "email", "email address", "e mail"),
    "cloud": ("cloud", "microsoft cloud"),
}
CLOUD_WORDS = {
    "": "commercial",
    "commercial": "commercial",
    "gcc": "commercial",
    "worldwide": "commercial",
    "gcc high": "gcc_high",
    "gcchigh": "gcc_high",
    "dod": "dod",
    "china": "china",
    "21vianet": "china",
}
TEMPLATE = (
    "name,tenant,group,contact\r\n"
    "Contoso Ltd,contoso.onmicrosoft.com,Tier 1,Jane Smith <jane@contoso.com>\r\n"
    "Fabrikam Inc,00000000-0000-0000-0000-000000000000,,it@fabrikam.com\r\n"
)

# How often a tenant still waiting for consent is checked by signing in (``due``).
CHECK_SOON = timedelta(minutes=2)  # just after the client came back from the consent page...
SOON_FOR = timedelta(hours=1)  # ...for this long (Microsoft can take a few minutes to apply consent),
CHECK_APPROVED = timedelta(minutes=30)  # then this often...
APPROVED_FOR = timedelta(days=7)  # ...for this long.
CHECK_SENT = timedelta(hours=1)  # A sent link nobody has come back from yet...
SENT_FOR = timedelta(days=30)  # ...for this long (consent can also be given another way, through GDAP say).


# The line Windows PowerShell's ConvertTo-Csv and Export-Csv write above the headings.
TYPE_LINE = re.compile(r"\N{BYTE ORDER MARK}?#TYPE [^\r\n]*(\r\n|\n|\r|$)")


def _heading(text: str) -> str:
    return " ".join(re.sub(r"[\s_\-()/]+", " ", text.replace("﻿", "")).lower().split())


HEADINGS = {_heading(alias): key for key, aliases in COLUMNS.items() for alias in aliases}


# -- Reading a CSV ----------------------------------------------------------------------------


@dataclass
class Row:
    line: int
    name: str
    tenant_ref: str
    cloud: str = clouds.DEFAULT
    groups: list[str] = field(default_factory=list)
    contact_name: str = ""
    contact_email: str = ""
    problems: list[str] = field(default_factory=list)
    skip: str = ""  # why a valid row is left out (the tenant is already in Office Sentry)

    @property
    def ok(self) -> bool:
        return not self.problems and not self.skip


@dataclass
class Parsed:
    rows: list[Row] = field(default_factory=list)
    error: str | None = None  # a problem with the whole file
    new_groups: list[str] = field(default_factory=list)  # groups the import will create

    @property
    def new(self) -> list[Row]:
        return [r for r in self.rows if r.ok]

    @property
    def skipped(self) -> list[Row]:
        return [r for r in self.rows if r.skip and not r.problems]

    @property
    def broken(self) -> list[Row]:
        return [r for r in self.rows if r.problems]


def decode(data: bytes) -> str:
    """An uploaded file's text: UTF-16 when it starts with UTF-16's byte-order mark (Excel's "Unicode Text",
    Windows PowerShell's ``ConvertTo-Csv > clients.csv``), else UTF-8 (with or without Excel's BOM), else
    Windows-1252, which is what Excel on Windows saves as "CSV" by default."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def split_contact(text: str) -> tuple[str, str]:
    """ "Jane Smith <jane@contoso.com>", "jane@contoso.com" or "Jane Smith" -> (name, email)."""
    text = " ".join(text.split())
    m = re.fullmatch(r"(.*?)\s*<([^<>]+)>", text)
    if m:
        return m.group(1).strip(" \"'"), m.group(2).strip()
    if "@" in text and " " not in text:
        return "", text
    return text, ""


def _cells(text: str) -> list[tuple[int, list[str]]]:
    """The rows that hold anything, each with the line of the file it starts on (blank lines count, so the
    preview's line numbers are the file's). Windows PowerShell's ``#TYPE ...`` line above the headings is
    left out."""
    typed = TYPE_LINE.match(text)
    sample = text[typed.end() if typed else 0 :][:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows, line = [], 1
    for row in reader:
        if any(c.strip() for c in row) and not (typed and line == 1):
            rows.append((line, row))
        line = reader.line_num + 1
    return rows


def parse(conn, text: str, sees: set[int] | None = None, groups_allowed: bool = True) -> Parsed:
    """Rows to show before importing, each with its problems in plain words. Nothing is written.

    ``sees`` is the tenant ids the person importing may see (None: every client): a client already added
    that they can't see is only said to be there, never named. Without ``groups_allowed`` (someone who
    doesn't manage groups, ``access.manages_groups``) a row naming a group is refused, since a group can
    hand its clients to the analysts it's granted to."""
    if len(text.encode("utf-8", errors="replace")) > MAX_BYTES:
        return Parsed(error="That file is over 400 kB. Split it into smaller files.")
    try:
        cells = _cells(text.replace("\x00", ""))
    except csv.Error as exc:
        return Parsed(error=f"That doesn't read as a CSV file ({exc}).")
    if not cells:
        return Parsed(error="The file is empty.")
    header = [HEADINGS.get(_heading(h)) for h in cells[0][1]]
    if "name" not in header or "tenant" not in header:
        return Parsed(
            error="The first row needs column names, including name and tenant (the tenant ID or a "
            "domain). Download the template to see the layout."
        )
    body = cells[1:]
    if not body:
        return Parsed(error="The file has column names but no clients under them.")
    if len(body) > MAX_ROWS:
        return Parsed(error=f"That's {len(body):,} rows; import up to {MAX_ROWS:,} at a time.")

    existing = {
        r["tenant_ref"].lower(): r["name"] if sees is None or r["id"] in sees else None
        for r in conn.execute("SELECT id, tenant_ref, name FROM tenants")
    }
    groups = {gr["name"].lower(): gr["name"] for gr in tenant_groups.all_groups(conn)}
    new_groups: dict[str, str] = {}
    seen: dict[str, int] = {}
    rows = []
    for line, cells_row in body:
        values: dict[str, str] = {}
        for key, cell in zip(header, cells_row, strict=False):
            if key and key not in values:
                values[key] = cell.strip()
        name = " ".join(values.get("name", "").split())
        ref = values.get("tenant", "").strip().lower()
        row = Row(line=line, name=name[:NAME_MAX], tenant_ref=ref)
        if not name:
            row.problems.append("No name.")
        elif len(name) > NAME_MAX:
            row.problems.append(f"The name is longer than {NAME_MAX} characters.")
        if not ref:
            row.problems.append("No tenant ID or domain.")
        elif not (GUID_RE.match(ref) or DOMAIN_RE.match(ref)):
            row.problems.append(f"“{ref[:80]}” isn't a tenant ID or a domain like contoso.onmicrosoft.com.")
        elif ref in seen:
            row.problems.append(f"The same client as line {seen[ref]}.")
        elif ref in existing:
            row.skip = f"Already in Office Sentry as {existing[ref]}." if existing[ref] else "Already in Office Sentry."
        if ref:
            seen.setdefault(ref, line)

        cloud = CLOUD_WORDS.get(_heading(values.get("cloud", "")))
        if cloud is None:
            row.problems.append("The cloud should be Commercial, GCC High, DoD or China (or left empty).")
        else:
            row.cloud = cloud

        contact_name, contact_email = split_contact(values.get("contact", ""))
        row.contact_name = (values.get("contact_name") or contact_name)[:CONTACT_MAX]
        row.contact_email = (values.get("contact_email") or contact_email)[:CONTACT_MAX]
        if row.contact_email and not valid_email(row.contact_email):
            row.problems.append(f"“{row.contact_email[:80]}” isn't an email address.")

        for raw in re.split(r"[;|]", values.get("group", "")):
            group = " ".join(raw.split())[: tenant_groups.NAME_MAX]
            if not group:
                continue
            if not groups_allowed:
                row.problems.append(
                    "Only someone who sees every client puts clients in groups. Leave the group column empty."
                )
                break
            known = groups.get(group.lower()) or new_groups.get(group.lower())
            if known is None:
                new_groups[group.lower()] = known = group
            if known not in row.groups:
                row.groups.append(known)
        rows.append(row)
    wanted = {g.lower() for r in rows if r.ok for g in r.groups}
    return Parsed(rows=rows, new_groups=[g for k, g in new_groups.items() if k in wanted])


def connection_for(connections, cloud: str):
    """The app connection a new tenant in ``cloud`` uses: that cloud's ready one, else its first, else None."""
    same = [c for c in connections if clouds.of(c).key == cloud]
    ready = [c for c in same if c["client_id"]]
    return (ready or same or [None])[0]


def create(conn, parsed: Parsed, user: str) -> list[int]:
    """Add every importable row as a tenant, in one transaction. Returns the new tenant ids."""
    connections = store.list_connections(conn)
    now = utcnow()
    ids = []
    with transaction(conn, immediate=True):
        group_ids = {gr["name"].lower(): gr["id"] for gr in tenant_groups.all_groups(conn)}
        for name in parsed.new_groups:
            if name.lower() not in group_ids:
                values, _ = tenant_groups.clean(name, "", "")
                group_ids[name.lower()] = tenant_groups.create(conn, values, user)
                audit(conn, user, "group_created", detail=f"{values['name']} (CSV import)")
        for row in parsed.new:
            if conn.execute("SELECT 1 FROM tenants WHERE tenant_ref = ?", (row.tenant_ref,)).fetchone():
                continue  # added since the preview
            connection = connection_for(connections, row.cloud)
            tenant_id = store.create_tenant(
                conn, row.name, row.tenant_ref, connection["id"] if connection else None, row.cloud
            )
            tenant_groups.set_tenant_groups(conn, tenant_id, {group_ids[g.lower()] for g in row.groups})
            conn.execute(
                "INSERT INTO onboarding (tenant_id, contact_name, contact_email, imported_at, imported_by)"
                " VALUES (?, ?, ?, ?, ?)",
                (tenant_id, row.contact_name, row.contact_email, now, user),
            )
            if row.contact_email:
                conn.execute(
                    "INSERT OR IGNORE INTO contacts (tenant_id, name, email, created_at) VALUES (?, ?, ?, ?)",
                    (tenant_id, row.contact_name, row.contact_email, now),
                )
            audit(
                conn,
                user,
                "tenant_created",
                tenant_id,
                row.tenant_ref + ("" if row.cloud == clouds.DEFAULT else f" ({row.cloud})") + " (CSV import)",
            )
            ids.append(tenant_id)
        audit(conn, user, "tenants_imported", detail=f"{len(ids)} added, {len(parsed.skipped)} already there")
    return ids


# -- Where each tenant has got ----------------------------------------------------------------


@dataclass(frozen=True)
class State:
    key: str
    label: str
    tone: str  # pill tone
    detail: str  # one sentence: what's happening or what's next


STATES = {  # key -> (label, tone), in the order a tenant moves through them
    "not_sent": ("Not sent", "info"),
    "sent": ("Sent", "info"),
    "approved": ("Approved, confirming", "accent"),
    "consented": ("Consented", "accent"),
    "global_reader": ("Global Reader missing", "warn"),
    "new_permissions": ("New permissions to approve", "warn"),
    "new_sent": ("New permissions sent", "info"),
    "new_approved": ("Approved, re-checking", "accent"),
    "connected": ("Connected", "ok"),
}
NEW_STATES = {"new_permissions", "new_sent", "new_approved"}


def _state(key: str, detail: str) -> State:
    return State(key, *STATES[key], detail)


def row_for(conn, tenant_id: int):
    return conn.execute("SELECT * FROM onboarding WHERE tenant_id = ?", (tenant_id,)).fetchone()


def rows_for(conn, tenant_ids) -> dict:
    ids = list(tenant_ids)
    if not ids:
        return {}
    return {
        r["tenant_id"]: r
        for r in conn.execute(f"SELECT * FROM onboarding WHERE tenant_id IN ({','.join('?' * len(ids))})", ids)
    }


def app_name(connection, result: dict | None) -> str:
    """The app's name as the client's admin finds it in their tenant."""
    return (result or {}).get("app_name") or (connection["name"] if connection else "") or "Office Sentry"


def state(tenant, row, result: dict | None, request=None) -> State:
    """Where ``tenant`` has got, from its ``onboarding`` row (or None), the connection collector's result and
    its ``permission_requests`` row for the current batch (or None)."""
    r = result or {}
    if tenant["consented_at"]:
        if not r.get("roles_checked"):
            return _state("consented", "Consent confirmed. Global Reader is checked with the first collection.")
        if not r.get("global_reader"):
            return _state(
                "global_reader",
                "Consent confirmed. Next: the client gives the app the Global Reader role, for mailbox reports.",
            )
        new = permission_batch.waiting(r)
        if not new:
            return _state("connected", "Consent and Global Reader are in place.")
        count = f"{len(new)} new read-only permission{'s' if len(new) > 1 else ''}"
        approved = request["approved_at"] if request else None
        if approved and approved >= (r.get("checked_at") or ""):
            return _state("new_approved", f"The client approved the {count}. Office Sentry is re-reading them.")
        if approved:
            return _state(
                "new_sent",
                f"The client approved, but the {count} {'is' if len(new) == 1 else 'are'} still not granted. Check "
                "your app registration asks for them (App connection), then send the link again.",
            )
        if request and request["sent_at"]:
            return _state("new_sent", f"Waiting for the client to approve the {count}.")
        return _state("new_permissions", f"Everything works. Send the client the link to approve the {count}.")
    approved = row["approved_at"] if row else None
    if approved:
        if row["check_error"] and (row["checked_at"] or "") >= approved:
            return _state("approved", f"The client approved, but Office Sentry can't sign in yet. {row['check_error']}")
        return _state("approved", "The client approved. Office Sentry is signing in to confirm.")
    if row and row["sent_at"]:
        return _state("sent", "Waiting for the client's Global Administrator to approve.")
    return _state("not_sent", "Send the client their consent link.")


@dataclass
class Entry:
    tenant: object
    row: object  # onboarding row or None
    state: State
    connection: object
    app: str
    problem: str | None  # why no consent link can be made (no app connection for its cloud), or None
    request: object = None  # permission_requests row for the current batch, or None
    audit_log: bool | None = None  # whether the client's Microsoft 365 audit log is on (``audit_log_on``)

    @property
    def needs_link(self) -> bool:
        """The client has something to approve: the app itself, or a new batch of permissions."""
        return not self.tenant["consented_at"] or self.state.key in NEW_STATES

    @property
    def sent_at(self) -> str | None:
        """When the link for what the client has to approve now went out."""
        if self.state.key in NEW_STATES:
            return self.request["sent_at"] if self.request else None
        return self.row["sent_at"] if self.row else None


def tracker(conn, tenants) -> list[Entry]:
    """Every tenant's state, for the onboarding page. ``tenants`` are the ones the person sees."""
    rows = rows_for(conn, [t["id"] for t in tenants])
    requests = permission_batch.requests_for(conn, [t["id"] for t in tenants])
    connections = {c["id"]: c for c in store.list_connections(conn)}
    entries = []
    for t in tenants:
        connection = connections.get(t["connection_id"])
        result = tenant_health.connection_result(conn, t["id"])
        problem = None
        if connection is None or not connection["client_id"] or clouds.of(connection).key != clouds.of(t).key:
            problem = (
                "No app connection is ready for this client's Microsoft cloud. Choose one under Manage."
                if connection is None or clouds.of(connection).key != clouds.of(t).key
                else f"The app connection {connection['name']} has no application (client) ID yet."
            )
        entries.append(
            Entry(
                t,
                rows.get(t["id"]),
                state(t, rows.get(t["id"]), result, requests.get(t["id"])),
                connection,
                app_name(connection, result),
                problem,
                requests.get(t["id"]),
                audit_log_on(conn, t["id"]),
            )
        )
    return entries


def audit_log_on(conn, tenant_id: int) -> bool | None:
    """Whether the client's Microsoft 365 audit log is recording, from its latest mailbox collection
    (``Get-AdminAuditLogConfig``), or None when that hasn't been read. The audit log add-on has nothing to
    read until it's on, so the tracker says so before anyone asks the client to approve it."""
    run = store.latest_run(conn, tenant_id, "mailboxes")
    found = store.find_object(conn, run["id"], "mail_settings", "org") if run else None
    value = found.get("UnifiedAuditLogIngestionEnabled") if isinstance(found, dict) else None
    return value if isinstance(value, bool) else None


def mark_sent(conn, tenant_ids, user: str, sent: bool = True) -> int:
    """Record that the consent link went to these tenants (or, with ``sent=False``, take it back). For a client
    that already consented, the link asks for the current batch of new permissions, so that's recorded."""
    ids = list(tenant_ids)
    if not ids:
        return 0
    now = utcnow()
    with transaction(conn, immediate=True):
        marks = ",".join("?" * len(ids))
        consented = [
            r["id"]
            for r in conn.execute(f"SELECT id FROM tenants WHERE id IN ({marks}) AND consented_at IS NOT NULL", ids)
        ]
        permission_batch.mark_sent(conn, consented, user, sent)
        ids = [t for t in ids if t not in consented]
        if sent:
            conn.executemany(
                "INSERT INTO onboarding (tenant_id, sent_at, sent_by) VALUES (?, ?, ?)"
                " ON CONFLICT (tenant_id) DO UPDATE SET sent_at = excluded.sent_at,"
                " sent_by = excluded.sent_by",
                [(t, now, user) for t in ids],
            )
        else:
            conn.executemany(
                "UPDATE onboarding SET sent_at = NULL, sent_by = NULL WHERE tenant_id = ?", [(t,) for t in ids]
            )
        for t in ids:
            audit(conn, user, "consent_link_sent" if sent else "consent_link_unsent", t)
    return len(ids) + len(consented)


def save_contact(conn, tenant_id: int, name: str, email: str) -> None:
    conn.execute(
        "INSERT INTO onboarding (tenant_id, contact_name, contact_email) VALUES (?, ?, ?)"
        " ON CONFLICT (tenant_id) DO UPDATE SET contact_name = excluded.contact_name,"
        " contact_email = excluded.contact_email",
        (tenant_id, name[:CONTACT_MAX], email[:CONTACT_MAX]),
    )


def client_approved(conn, tenant) -> None:
    """Someone came back from the consent link sent to this client saying they approved. Not proof: note it
    and queue a sign-in to confirm. For a tenant that already consented (approving again after new
    permissions), note it for the current batch and re-read its permissions instead."""
    if tenant["consented_at"]:
        permission_batch.note_approved(conn, tenant["id"])
        if tenant["enabled"] and not tenant["archived_at"]:
            store.enqueue_run(conn, tenant["id"], "connection", requested_by="Client consent")
        return
    conn.execute(
        "INSERT INTO onboarding (tenant_id, approved_at) VALUES (?, ?)"
        " ON CONFLICT (tenant_id) DO UPDATE SET approved_at = excluded.approved_at",
        (tenant["id"], utcnow()),
    )
    queue_check(conn, tenant["id"], priority=tasks.PRIORITY_WAITING)


# -- Confirming consent by signing in ---------------------------------------------------------


def queue_check(conn, tenant_id: int, priority: int = tasks.PRIORITY_BACKGROUND, user: str | None = None) -> int:
    return tasks.enqueue(
        conn, "consent_check", key=f"consent_check:{tenant_id}", tenant_id=tenant_id, priority=priority, user=user
    )


@dataclass
class Outcome:
    ok: bool
    message: str
    swapped_to: str | None = None
    queued: int = 0


def _note_check(conn, tenant_id: int, error: str | None) -> None:
    conn.execute(
        "INSERT INTO onboarding (tenant_id, checked_at, check_error) VALUES (?, ?, ?)"
        " ON CONFLICT (tenant_id) DO UPDATE SET checked_at = excluded.checked_at,"
        " check_error = excluded.check_error",
        (tenant_id, utcnow(), error),
    )


def confirm(conn, vault, tenant, actor: str = "Onboarding", session=None) -> Outcome:
    """Sign in to ``tenant`` as the app. When that works, record the consent (replacing a domain with the tenant
    ID Microsoft publishes for it, if no other tenant has it) and queue the first collection."""
    connection = store.get_connection(conn, tenant["connection_id"]) if tenant["connection_id"] else None
    cloud = clouds.of(tenant)
    if connection is None or not connection["client_id"] or clouds.of(connection).key != cloud.key:
        message = "No app connection is ready for this client."
        _note_check(conn, tenant["id"], message)
        return Outcome(False, message)
    result = connection_check.check(
        tenant["tenant_ref"],
        connection["client_id"],
        vault.decrypt(connection["key_enc"]),
        connection["thumbprint"],
        cloud.key,
    )
    if not result.ok:
        message = f"{result.title} {result.fix}".strip()[:500]
        _note_check(conn, tenant["id"], message)
        return Outcome(False, message)
    swap = None
    if not GUID_RE.match(tenant["tenant_ref"]):
        swap = tenant_id_for_domain(tenant["tenant_ref"].lower(), session=session, cloud=cloud.key)
    with transaction(conn, immediate=True):
        current = store.get_tenant(conn, tenant["id"])
        if current is None:
            return Outcome(False, "The client was deleted.")
        if swap and (
            current["tenant_ref"] != tenant["tenant_ref"]
            or conn.execute("SELECT 1 FROM tenants WHERE tenant_ref = ? AND id <> ?", (swap, tenant["id"])).fetchone()
        ):
            swap = None
        if swap:
            conn.execute("UPDATE tenants SET tenant_ref = ? WHERE id = ?", (swap, tenant["id"]))
        newly = not current["consented_at"]
        conn.execute(
            "UPDATE tenants SET consented_at = COALESCE(consented_at, ?) WHERE id = ?", (utcnow(), tenant["id"])
        )
        _note_check(conn, tenant["id"], None)
        if newly:
            audit(
                conn,
                actor,
                "consent_confirmed",
                tenant["id"],
                (result.organisation or "signed in") + (f"; tenant ID {swap}" if swap else ""),
            )
    queued = 0
    if newly and current["enabled"] and not current["archived_at"]:
        queued = lifecycle.queue_first_collection(conn, tenant["id"], requested_by=actor)
    return Outcome(True, result.title, swap, queued)


@tasks.handler("consent_check")
def _consent_check(ctx) -> dict:
    tenant = ctx.tenant
    if tenant is None or tenant["archived_at"]:
        return {"skipped": "gone or archived"}
    if tenant["consented_at"]:
        return {"skipped": "already consented"}
    outcome = confirm(ctx.conn, ctx.vault, tenant, actor="Consent check")
    return {"ok": outcome.ok, "queued": outcome.queued}


def due(conn, now: datetime) -> list[int]:
    """Tenants still waiting for consent whose next sign-in check is due (``CHECK_*``), active ones only."""
    out = []
    for r in conn.execute(
        "SELECT o.* FROM onboarding o JOIN tenants t ON t.id = o.tenant_id WHERE t.consented_at IS NULL"
        " AND t.enabled = 1 AND t.archived_at IS NULL AND (o.approved_at IS NOT NULL OR o.sent_at IS NOT NULL)"
    ):
        approved, sent, checked = parse_ts(r["approved_at"]), parse_ts(r["sent_at"]), parse_ts(r["checked_at"])
        if approved and now - approved <= APPROVED_FOR:
            every = CHECK_SOON if now - approved <= SOON_FOR else CHECK_APPROVED
        elif sent and now - sent <= SENT_FOR:
            every = CHECK_SENT
        else:
            continue
        if checked is None or now - checked >= every:
            out.append(r["tenant_id"])
    return out


@periodic("consent_checks")
def _check_job(ctx) -> None:
    """Registered with the worker through ``worker.JOB_MODULES``. Queues the checks; task threads sign in."""
    for tenant_id in due(ctx.conn, ctx.now if ctx.now.tzinfo else ctx.now.replace(tzinfo=UTC)):
        queue_check(ctx.conn, tenant_id)


# -- What the client is approving, in their words (the consent pack) --------------------------

# Permission -> what it lets Office Sentry read, for the client. docs/what-office-sentry-reads.md has the
# long version; test_bulk_onboarding.py checks every permission the app asks for is here.
CLIENT_WORDS: dict[str, str] = {
    "Organization.Read.All": "Your organisation's name, domains and which licences you have.",
    "User.Read.All": "User and guest accounts: names, email addresses, licences and password age. Never passwords.",
    "Directory.Read.All": "Groups and their members, devices, domains, subscriptions and recently deleted accounts.",
    "AuditLog.Read.All": "Sign-in and audit logs: who signed in, when, from where and with which app, and changes "
    "made in Entra ID. Kept beyond Microsoft's 30 days: every sign-in for 180 days and every "
    "audit event for 7 years, unless your IT provider sets otherwise.",
    "Reports.Read.All": "Microsoft 365 usage reports: who uses email, Teams, OneDrive and the Office apps.",
    "Policy.Read.All": "Sign-in policies such as Conditional Access and Security Defaults, and guest settings.",
    "RoleManagement.Read.Directory": "Who holds each admin role.",
    "UserAuthenticationMethod.Read.All": "Which kinds of sign-in method people have set up (not the codes).",
    "Application.Read.All": "Apps connected to your tenant, what they can do, and when their credentials expire.",
    "SecurityEvents.Read.All": "Your Microsoft Secure Score.",
    "IdentityRiskyUser.Read.All": "Accounts Microsoft has flagged as risky.",
    "DeviceManagementManagedDevices.Read.All": "Devices managed by Intune and whether they're compliant.",
    "SharePointTenantSettings.Read.All": "SharePoint's sharing settings.",
    "Sites.Read.All": "Names, sizes and sharing of files in SharePoint and OneDrive. Never what's inside them.",
    "MailboxSettings.Read": "Mailbox settings such as inbox rules. Never emails.",
    "Exchange.ManageAsApp": "Mailbox settings, phones that sync mail and distribution lists from Exchange Online, "
    "using read-only commands only.",
    # Added in 2.2 (permission_batch.BATCHES)
    "Channel.ReadBasic.All": "The names and kinds of channels in each team (standard, private, shared). Never "
    "the messages in them.",
    "ChannelMember.Read.All": "Who is a member of each channel, and whether they're a guest or from another "
    "organisation.",
    "TeamsAppInstallation.ReadForTeam.All": "Which apps are added to each team.",
    "ReportSettings.Read.All": "Whether your Microsoft 365 usage reports hide people's names.",
    "PrivilegedEligibilitySchedule.Read.AzureADGroup": "Who can activate membership of a group that grants admin "
    "rights (Privileged Identity Management).",
    "BitLockerKey.ReadBasic.All": "Whether each device has a BitLocker recovery key saved in Entra ID. Never the "
    "key itself.",
    "DeviceManagementConfiguration.Read.All": "Intune configuration and compliance policies, such as firewall, "
    "screen lock and update settings.",
    "DeviceManagementApps.Read.All": "Intune app protection policies and which managed apps are deployed.",
    "SharePoint Sites.Read.All": "Who has access to each SharePoint site and list, through SharePoint's own "
    "permission lists. Never what's in the files.",
}
NEVER_READS = (
    "Email, attachments, calendars or contacts.",
    "What's inside files in OneDrive, SharePoint or Teams.",
    "Teams chats or channel messages.",
    "Passwords or MFA codes: Microsoft never gives these to any program.",
)
