"""One page per mailbox: everything Office Sentry has collected about one mailbox.

Size and how it has grown across retained snapshots, forwarding and inbox rules, who else can open or
send from it, the protocols it allows, its automatic reply, the phones syncing it, email activity, open
findings and what changed. Built from stored data the way the person page is (``reports/profile/``):
each area is a ``@part("mailbox", order)`` function returning a ``Part``; ``build()`` puts the page
together and ``directory()`` lists every mailbox for the Mailboxes page.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from .. import store, usage
from ..collectors.exchange import SYSTEM_MAILBOX_TYPES
from ..devices import parse_ts
from ..licences import last_sign_in
from ..mailflow import inbox_rule_risk, is_external, mailbox_forwarding, tenant_domains
from ..snapshot import TenantSnapshot
from ..text import fold, plural
from .auto_replies import OUTSIDE, _at, first_seen, state
from .data_reports import (
    PAGE_INDEXES,
    Highlight,
    PageIndex,
    Section,
    UserLink,
    _describe_actions,
    _describe_conditions,
    user_path,
)
from .data_reports import _type as mailbox_type
from .mail_reports import _account, _Directory, _grants, _size
from .mobile_devices import ACCESS, APPS, _device, platform
from .object_page import (
    ObjectPage,
    Subject,
    _day,
    _when,
    attention_tile,
    build_parts,
    findings_for,
    object_path,
    part,
    sort_attention,
    sources_for,
)
from .profile.base import HISTORY_LIMIT, Event, Part
from .profile.mailbox import PROTOCOLS
from .sharepoint import storage_chart

KIND = "mailbox"
URL = "mailboxes"
NEAR_QUOTA = 0.9
STALE_DEVICE_DAYS = 90
SOURCES = (
    "mailboxes",
    "mailbox_usage",
    "inbox_rules",
    "auto_replies",
    "mobile_devices",
    "legacy_signins",
    "usage",
    "users",
)
TYPE_WORDS = {
    "UserMailbox": "User mailbox",
    "SharedMailbox": "Shared mailbox",
    "RoomMailbox": "Room mailbox",
    "EquipmentMailbox": "Equipment mailbox",
}


def mailbox_path(tenant_id: int, mailbox_id: str) -> str:
    return object_path(tenant_id, URL, mailbox_id)


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def mailboxes(snap: TenantSnapshot) -> list[dict] | None:
    """Every mailbox people use (system mailboxes left out), or None before Mailboxes is collected."""
    boxes = snap.objects("mailboxes", "mailbox")
    if boxes is None:
        return None
    return [m for m in boxes if _text(m.get("RecipientTypeDetails")) not in SYSTEM_MAILBOX_TYPES]


def _index(snap: TenantSnapshot) -> dict[str, str]:
    """Mailbox addresses that aren't an account's (those link to the person's page already) -> page path."""
    out: dict[str, str] = {}
    accounts = {
        _text(u.get(k)).lower() for u in snap.objects("users", "user") or [] for k in ("userPrincipalName", "mail")
    }
    for m in mailboxes(snap) or []:
        address = _text(m.get("PrimarySmtpAddress")).lower()
        if address and address not in accounts:
            out.setdefault(address, mailbox_path(snap.tenant["id"], m["_id"]))
    return out


PAGE_INDEXES.append(PageIndex(_index))  # an address is unmistakable in any column


def find_mailbox(snap: TenantSnapshot, mailbox_id: str) -> dict | None:
    return next((m for m in mailboxes(snap) or [] if m["_id"] == mailbox_id), None)


class Mailbox(Subject):
    kind = KIND

    def __init__(self, snap: TenantSnapshot, box: dict, now: datetime):
        super().__init__(snap, box, now)
        self.box = box
        self.address = _text(box.get("PrimarySmtpAddress"))
        self.name = _text(box.get("DisplayName")) or self.address or self.id
        self.type = _text(box.get("RecipientTypeDetails"))
        self.users = snap.objects("users", "user") or []
        self.user = next((u for u in self.users if u["_id"] == box.get("ExternalDirectoryObjectId")), None)
        self.domains = tenant_domains(snap) or set()
        self.keys = {
            k.lower() for k in (self.address, _text(box.get("UserPrincipalName")), (self.user or {}).get("mail")) if k
        }
        sizes = snap.objects("mailbox_usage", "mailbox_size")
        self.sizes_collected = sizes is not None
        self.size = next((s for s in sizes or [] if s.get("mailboxId") == self.id), None)
        self.path = mailbox_path(self.tenant_id, self.id)

    def is_me(self, href: str) -> bool:
        return href == self.path

    def named(self, value) -> bool:
        return isinstance(value, str) and value.lower() in self.keys

    @property
    def shared(self) -> bool:
        return self.type != "UserMailbox"

    @property
    def quota(self) -> int | None:
        return (self.size or {}).get("sendQuotaBytes") or None

    @property
    def total(self) -> int | None:
        total = (self.size or {}).get("totalBytes")
        return total if isinstance(total, int) else None


# -- Parts --------------------------------------------------------------------------------


def _size_history(m: Mailbox) -> list[tuple[datetime, int]]:
    """(collected at, size) for each retained Mailbox size collection, oldest first."""
    out = []
    for finished, data in store.object_copies(m.snap.conn, m.tenant_id, "mailbox_usage", "mailbox_size", m.id):
        when = parse_ts(finished)
        if when and isinstance(data.get("totalBytes"), int):
            out.append((when, data["totalBytes"]))
    return out


def _signed(n: int) -> str:
    if not n:
        return "No change"
    return ("+" if n > 0 else "−") + _size(abs(n))


@part(KIND, 10)
def size_part(m: Mailbox) -> Part:
    result = Part("size", "Size and growth")
    if not m.sizes_collected:
        result.unavailable = "Mailbox sizes haven't been collected yet. Refresh Mailbox size on the client's page."
        return result
    s = m.size
    if s is None or m.total is None:
        result.summary, result.empty = "Not read", "Exchange didn't return this mailbox's size on the last collection."
        return result
    total, quota = m.total, m.quota
    result.summary = _size(total)
    result.facts.append(("Size", _size(total) + (f" of {_size(quota)} ({100 * total / quota:.0f}%)" if quota else "")))
    if isinstance(s.get("itemCount"), int):
        result.facts.append(("Items", f"{s['itemCount']:,}"))
    if isinstance(s.get("deletedBytes"), int) and s["deletedBytes"]:
        result.facts.append(("Recoverable deleted items", _size(s["deletedBytes"])))
    if s.get("warningQuotaBytes"):
        result.facts.append(("Warning at", _size(s["warningQuotaBytes"])))
    if quota:
        result.facts.append(("Stops sending at", _size(quota)))
    if s.get("sendReceiveQuotaBytes"):
        result.facts.append(("Stops receiving at", _size(s["sendReceiveQuotaBytes"])))
    if quota and total >= NEAR_QUOTA * quota:
        result.tone = "bad" if total >= quota else "warn"
        m.flag(
            "high" if total >= quota else "medium",
            f"The mailbox is {100 * total / quota:.0f}% full and stops sending at {_size(quota)}. "
            "Turn on the archive or clear old mail.",
            "size",
        )

    history = _size_history(m)
    if len(history) >= 2:
        points = [(when.date(), float(size)) for when, size in history]
        result.chart = storage_chart(points, "Mailbox size over time", m.now, float(quota) if quota else None)
        months: dict[tuple[int, int], int] = {}
        for when, size in history:
            months[(when.year, when.month)] = size
        table = Section("Size by month", ["Month", "Size", "Change"], intro="The last collection of each month.")
        previous = None
        for (y, mo), size in sorted(months.items()):
            table.add(
                [f"{date(y, mo, 1):%B %Y}", _size(size), _signed(size - previous) if previous is not None else ""]
            )
            previous = size
        table.rows.reverse()
        table.flags.reverse()
        result.tables.append(table)
        first, last = history[0], history[-1]
        days = (last[0] - first[0]).days
        if days >= 7 and last[1] != first[1]:
            result.note = f"{_signed(last[1] - first[1])} since {_day(first[0])} ({days} days)."
    else:
        result.note = "Growth shows once Office Sentry has two collections of this mailbox's size."
    return result


@part(KIND, 20)
def forwarding_part(m: Mailbox) -> Part:
    result = Part("forwarding", "Forwarding and inbox rules")
    target = mailbox_forwarding(m.box)
    if target:
        outside = is_external(target, m.domains)
        words = (
            target
            + (" (outside the organisation)" if outside else "")
            + ("" if m.box.get("DeliverToMailboxAndForward") else ", no copy kept in the mailbox")
        )
        result.facts.append(("Forwarding set by an admin", words))
        if outside:
            result.summary, result.tone = "Forwards outside", "bad"
            m.flag("high", f"All mail is forwarded to {target}, outside the organisation.", "forwarding")
        else:
            result.summary = "Forwards internally"
    else:
        result.facts.append(("Forwarding set by an admin", "None"))
    rules = m.snap.objects("inbox_rules", "inbox_rule")
    if rules is None:
        result.note = "Inbox rules haven't been collected yet. Refresh Inbox rules on the client's page."
        result.summary = result.summary or "No forwarding"
        return result
    mine = [r for r in rules if r.get("mailboxId") == m.id or m.named(r.get("mailbox"))]
    flagged = 0
    if mine:
        table = Section("Inbox rules", ["Rule", "Enabled", "What it does", "Why it stands out"])
        for r in sorted(mine, key=lambda r: r.get("sequence") or 0):
            risk = inbox_rule_risk(r, m.domains)
            table.add(
                [
                    r.get("displayName"),
                    r.get("isEnabled", True),
                    f"{_describe_conditions(r.get('conditions') or {})}: {_describe_actions(r)}",
                    "; ".join(risk.reasons),
                ],
                risk.severity,
            )
            if risk.severity in ("high", "medium"):
                flagged += 1
                m.flag(risk.severity, f'Inbox rule "{r.get("displayName")}": {"; ".join(risk.reasons)}.', "forwarding")
        result.tables.append(table)
    if not result.summary:
        if flagged:
            result.summary, result.tone = f"{plural(flagged, 'rule')} to check", "warn"
        elif mine:
            result.summary = plural(len(mine), "inbox rule")
        else:
            result.summary = "No forwarding"
    elif flagged:
        result.summary += f", {plural(flagged, 'rule')} to check"
    return result


@part(KIND, 30)
def access_part(m: Mailbox) -> Part:
    result = Part("access", "Who can open or send from it")
    d = _Directory(mailboxes(m.snap) or [], m.users)
    into = [g for g in _grants(m.snap, d) if g[0] is not None and g[0]["_id"] == m.id]
    if not into:
        result.summary, result.empty = "Nobody else", "No one else has full access, send as or send on behalf."
        return result
    table = Section("People with access", ["Person", "Access", "Account"])
    people = set()
    for _, _, person, user, right in sorted(into, key=lambda g: (g[2].lower(), g[4])):
        words, flag = _account(user, person, m.domains)
        table.add([person, right, words], flag)
        people.add(person.lower())
        if flag in ("high", "medium"):
            m.flag(flag, f"{person} ({words.lower()}) has {right.lower()} to this mailbox.", "access")
    result.tables.append(table)
    result.summary = plural(len(people), "person", "people")
    if any(f in ("high", "medium") for f in table.flags):
        result.tone = "warn"
    if m.shared and m.type == "SharedMailbox" and not people:
        result.note = "A shared mailbox nobody can open is probably no longer needed."
    return result


@part(KIND, 40)
def settings_part(m: Mailbox) -> Part:
    result = Part("settings", "Protocols and settings")
    cas = {c["_id"]: c for c in m.snap.objects("mailboxes", "cas_mailbox") or []}.get(m.id)
    if cas is not None:
        allowed = [name for key, name in PROTOCOLS if cas.get(key)]
        if cas.get("SmtpClientAuthenticationDisabled") is False:
            allowed.append("SMTP AUTH")
        result.facts.append(("Older protocols allowed", ", ".join(allowed) or "None"))
        result.summary = f"{plural(len(allowed), 'older protocol')} allowed" if allowed else "Modern only"
        if allowed:
            result.tone = "warn"
    else:
        result.facts.append(("Older protocols allowed", "Not read"))
    org = (m.snap.objects("mailboxes", "mail_settings") or [{}])[0]
    audit = m.box.get("AuditEnabled")
    if audit is None:
        result.facts.append(("Mailbox auditing", "Off for the organisation" if org.get("AuditDisabled") else "On"))
    else:
        result.facts.append(("Mailbox auditing", "On" if audit else "Off"))
    result.facts.append(("Hidden from the address book", "Yes" if m.box.get("HiddenFromAddressListsEnabled") else "No"))
    result.facts.append(("Archive", "On" if m.box.get("ArchiveStatus") not in (None, "", "None") else "Off"))
    result.facts.append(("Litigation hold", "On" if m.box.get("LitigationHoldEnabled") else "Off"))
    if m.box.get("WhenMailboxCreated"):
        result.facts.append(("Created", _when(m.box["WhenMailboxCreated"], m.now)))

    status = (m.snap.objects("legacy_signins", "legacy_signin_status") or [None])[0]
    if status and status.get("available"):
        mine = [
            r
            for r in m.snap.objects("legacy_signins", "legacy_signin") or []
            if r.get("userId") == m.id or m.named(r.get("user"))
        ]
        if mine:
            table = Section(
                "Legacy sign-ins in the last 30 days",
                ["Client", "Succeeded", "Failed", "Last attempt", "From"],
                intro="Sign-ins with protocols that can't do MFA.",
            )
            for r in sorted(mine, key=lambda r: -(r.get("succeeded") or 0)):
                table.add(
                    [
                        r.get("client"),
                        r.get("succeeded") or 0,
                        r.get("failed") or 0,
                        (r.get("lastAttempt") or "")[:10],
                        ", ".join(r.get("countries") or []),
                    ],
                    "medium" if r.get("succeeded") else None,
                )
            result.tables.append(table)
            if any(r.get("succeeded") for r in mine):
                result.tone = "bad"
                m.flag(
                    "medium",
                    "Signed in with legacy protocols that can't do MFA in the last 30 days. Block them for this "
                    "mailbox.",
                    "settings",
                )
    return result


@part(KIND, 50)
def auto_reply_part(m: Mailbox) -> Part | None:
    rows = m.snap.objects("auto_replies", "auto_reply")
    if rows is None:
        return None
    result = Part("auto_reply", "Automatic reply")
    reply = next((r for r in rows if r.get("mailboxId") == m.id), None)
    if reply is None:
        result.summary, result.empty = "Off", "No automatic reply is on or scheduled."
        return result
    current = state(reply, m.now)
    result.summary = current.capitalize() if isinstance(current, str) else "On"
    result.facts.append(("State", result.summary))
    audience = OUTSIDE.get(_text(reply.get("externalAudience")), _text(reply.get("externalAudience")) or "Unknown")
    result.facts.append(("Replies outside the organisation", audience))
    if _text(reply.get("status")) == "scheduled":
        for label, key in (("From", "start"), ("Until", "end")):
            when = _at(reply.get(key))
            if when:
                result.facts.append((label, f"{_day(when)}, {when:%H:%M}"))
    since = first_seen(m.snap, m.now).get(m.id)
    if since:
        result.facts.append(("First seen on", _when(since, m.now)))
    if reply.get("internalText"):
        result.facts.append(("Reply to colleagues", _text(reply["internalText"])[:300]))
    if reply.get("externalText"):
        result.facts.append(("Reply to outside senders", _text(reply["externalText"])[:300]))
    return result


@part(KIND, 60)
def devices_part(m: Mailbox) -> Part | None:
    rows = m.snap.objects("mobile_devices", "mobile_device")
    if rows is None:
        return None
    result = Part("devices", "Phones and tablets syncing it")
    mine = [d for d in rows if d.get("mailboxId") == m.id or m.named(d.get("mailbox"))]
    if not mine:
        result.summary, result.empty = "None", "No phone or tablet syncs this mailbox."
        return result
    table = Section("Devices", ["Device", "Platform", "App", "Access", "First sync", "Last sync"])
    stale = waiting = 0
    for d in sorted(mine, key=lambda d: _text(d.get("LastSuccessSync")), reverse=True):
        last = parse_ts(d.get("LastSuccessSync"))
        days = (m.now - last).days if last else None
        access = _text(d.get("DeviceAccessState"))
        flag = None
        if access == "Quarantined":
            waiting += 1
            flag = "medium"
        elif days is not None and days >= STALE_DEVICE_DAYS:
            stale += 1
            flag = "low"
        table.add(
            [
                _device(d),
                platform(d.get("DeviceOS")),
                APPS.get(_text(d.get("ClientType")), _text(d.get("ClientType")) or "Unknown"),
                ACCESS.get(access, access or "Unknown"),
                _day(d.get("FirstSyncTime")),
                _day(last) if last else "Never",
            ],
            flag,
        )
    result.tables.append(table)
    result.summary = plural(len(mine), "device")
    if waiting:
        result.tone = "warn"
        m.flag("medium", f"{plural(waiting, 'device')} waiting for an admin to allow or block it.", "devices")
    if stale:
        result.note = (
            f"{plural(stale, 'device')} hasn't synced for {STALE_DEVICE_DAYS} days; the partnership can be removed."
        )
    return result


@part(KIND, 70)
def activity_part(m: Mailbox) -> Part | None:
    if m.shared:
        return None  # Microsoft's usage reports cover people's own mailboxes only
    status = usage.status(m.snap)
    if status is None:
        return None
    result = Part("activity", "Email activity")
    if not status.get("available"):
        result.unavailable = "Microsoft's usage reports aren't available for this tenant."
        return result
    row = usage.person(m.snap, (m.user or {}).get("userPrincipalName") or m.address)
    if row is None:
        result.summary, result.empty = "Not in the usage reports", "Microsoft's usage reports don't list this mailbox."
        if status.get("concealed"):
            result.note = usage.CONCEALED_NOTICE
        return result
    last = (row.get("lastActive") or {}).get("exchange")
    result.facts.append(("Last email activity", _when(last, m.now) if last else "None in the report period"))
    email = row.get("email") or {}
    if email:
        result.facts.append(("Sent", f"{email.get('sent', 0):,}"))
        result.facts.append(("Received", f"{email.get('received', 0):,}"))
        result.facts.append(("Read", f"{email.get('read', 0):,}"))
        result.note = "Counts cover Microsoft's report period (the last 30 days)."
    result.summary = "Active" if (row.get("active") or {}).get("exchange") else "Inactive"
    result.tone = None if result.summary == "Active" else "warn"
    return result


# -- The page -----------------------------------------------------------------------------


def _history(m: Mailbox) -> tuple[list[Event], str | None]:
    """What changed for the mailbox between stored snapshots, newest first, and the oldest snapshot it's in."""
    conn, tid = m.snap.conn, m.tenant_id
    events: list[Event] = []
    copies = store.object_copies(conn, tid, "mailboxes", "mailbox", m.id)
    since = copies[0][0] if copies else None
    for (_, old), (when, new) in zip(copies, copies[1:], strict=False):
        day = when[:10]
        a, b = mailbox_forwarding(old), mailbox_forwarding(new)
        if a != b:
            events.append(Event(day, "Forwarding", f"Set to {b}" if b else "Removed", "warn" if b else None))
        if old.get("RecipientTypeDetails") != new.get("RecipientTypeDetails") and new.get("RecipientTypeDetails"):
            events.append(
                Event(day, "Type", f"Converted to a {mailbox_type(new['RecipientTypeDetails']).lower()} mailbox")
            )
        for key, label in (("LitigationHoldEnabled", "Litigation hold"), ("HiddenFromAddressListsEnabled", "Hidden")):
            if bool(old.get(key)) != bool(new.get(key)):
                events.append(Event(day, "Settings", f"{label} turned {'on' if new.get(key) else 'off'}"))
        was, now_ = (
            old.get("ArchiveStatus") not in (None, "", "None"),
            new.get("ArchiveStatus") not in (None, "", "None"),
        )
        if was != now_:
            events.append(Event(day, "Settings", f"Archive turned {'on' if now_ else 'off'}"))
        if old.get("DisplayName") and new.get("DisplayName") and old["DisplayName"] != new["DisplayName"]:
            events.append(Event(day, "Name", f"Renamed from {old['DisplayName']} to {new['DisplayName']}"))
        before = {str(x).lower() for x in old.get("GrantSendOnBehalfTo") or []}
        after = {str(x).lower() for x in new.get("GrantSendOnBehalfTo") or []}
        events += [Event(day, "Access", f"Send on behalf given to {w}", "warn") for w in sorted(after - before)]
        events += [Event(day, "Access", f"Send on behalf removed from {w}") for w in sorted(before - after)]

    def seen_by_run(collector: str, kind: str, field: str, area: str, label) -> None:
        """Events for rows naming the mailbox appearing in or vanishing from one collection to the next. A run
        that read nothing counts (the tenant's last inbox rule removed), one whose snapshot retention removed
        doesn't."""
        runs = conn.execute(
            "SELECT id, finished_at FROM runs WHERE tenant_id = ? AND collector = ? AND status IN ('succeeded',"
            " 'partial') AND (object_count = 0 OR EXISTS (SELECT 1 FROM objects o WHERE o.run_id = runs.id))"
            " ORDER BY finished_at, id",
            (tid, collector),
        ).fetchall()
        if len(runs) < 2:
            return
        held: dict[int, set[str]] = {r["id"]: set() for r in runs}
        for _, _, run_id, d in store.matching_objects(conn, tid, collector, kind, field, m.id):
            if run_id in held:
                held[run_id].add(label(d))
        pairs = [(r["finished_at"], held[r["id"]]) for r in runs]
        for (_, old), (when, new) in zip(pairs, pairs[1:], strict=False):
            events.extend(Event(when[:10], area, f"{x} added", "warn") for x in sorted(new - old))
            events.extend(Event(when[:10], area, f"{x} removed") for x in sorted(old - new))

    seen_by_run("mailboxes", "mailbox_permission", "mailboxId", "Access", lambda d: f"Full access for {d.get('User')}")
    seen_by_run("inbox_rules", "inbox_rule", "mailboxId", "Inbox rules", lambda d: f'Rule "{d.get("displayName")}"')
    events.sort(key=lambda e: e.day, reverse=True)
    return events[:HISTORY_LIMIT], since


def _about(m: Mailbox) -> list[tuple[str, object]]:
    facts: list[tuple[str, object]] = [("Address", m.address), ("Type", TYPE_WORDS.get(m.type, m.type or "Mailbox"))]
    if m.box.get("DisplayName") and m.box["DisplayName"] != m.name:
        facts.append(("Name", m.box["DisplayName"]))
    if m.box.get("Alias"):
        facts.append(("Alias", m.box["Alias"]))
    u = m.user
    if u is not None:
        label = u.get("displayName") or u.get("userPrincipalName") or u["_id"]
        facts.append(("Account", UserLink(label, user_path(m.tenant_id, u["_id"]))))
        facts.append(("Sign-in", "Allowed" if u.get("accountEnabled") else "Blocked"))
        if m.shared:
            last = last_sign_in(u)
            if u.get("signInActivityAvailable"):
                facts.append(("Last sign-in to the account", _when(last, m.now) if last else "Never"))
        facts.append(("Licensed", "Yes" if u.get("licensed") else "No"))
    elif m.box.get("UserPrincipalName"):
        facts.append(("Sign-in name", m.box["UserPrincipalName"]))
        facts.append(("Account", "Not found in the directory"))
    if m.box.get("WhenMailboxCreated"):
        facts.append(("Created", _day(m.box["WhenMailboxCreated"])))
    return facts


def _account_attention(m: Mailbox) -> None:
    u = m.user
    if u is None:
        return
    if m.type == "SharedMailbox" and u.get("accountEnabled"):
        m.flag(
            "medium",
            "The shared mailbox's account allows sign-in. Block it: shared mailboxes are opened through people's "
            "own accounts, so the account itself needs no password.",
        )
    if m.type == "UserMailbox" and not u.get("accountEnabled") and not m.box.get("LitigationHoldEnabled"):
        m.flag(
            "low",
            "The person's sign-in is blocked and the mailbox isn't on hold: once the licence goes, the mail goes "
            "with it. Put it on litigation hold or convert it to a shared mailbox if the client needs to keep it.",
        )


def _reports(m: Mailbox) -> list[tuple[str, str]]:
    links = [
        ("Mail forwarding", m.report("mailboxes")),
        ("Inbox rules", m.report("inbox-rules", "All inbox rules", "Mailbox", m.address)),
        ("Mailbox permissions", m.report("mailbox-permissions", "Every permission", "Mailbox", m.address)),
        ("Mailbox size", m.report("mailbox-size", "Mailboxes by size", "Mailbox", m.address)),
        ("Mailbox size trend", m.report("mailbox-trend")),
        ("Mailbox holds and archiving", m.report("mailbox-holds", "Every mailbox", "Mailbox", m.address)),
    ]
    if m.shared:
        links.insert(
            2, ("Shared mailboxes", m.report("shared-mailboxes", "Shared and resource mailboxes", "Mailbox", m.address))
        )
    if m.snap.has("mobile_devices"):
        links.append(("Mobile devices", m.report("mobile-devices", "Every device", "Mailbox", m.address)))
    if m.snap.has("auto_replies"):
        links.append(("Automatic replies", m.report("auto-replies")))
    if m.snap.has("legacy_signins"):
        links.append(("Legacy sign-ins", m.report("legacy-auth")))
    return links


def build(conn, tenant, mailbox_id: str, now: datetime | None = None) -> ObjectPage | None:
    """The page for one mailbox, or None when the latest Mailboxes snapshot doesn't have it."""
    now = now or datetime.now(UTC)
    snap = TenantSnapshot(conn, tenant)
    box = find_mailbox(snap, mailbox_id)
    if box is None:
        return None
    m = Mailbox(snap, box, now)
    tid = tenant["id"]
    page = ObjectPage(
        KIND,
        dict(tenant),
        mailbox_id,
        now,
        m.name,
        m.path,
        "Mailboxes",
        f"/tenants/{tid}/{URL}",
        subtitle=m.address if m.address != m.name else "",
        description=f"Everything collected about the mailbox {m.address or m.name}.",
        about_title="Mailbox",
        nothing="Nothing stands out for this mailbox in the data collected.",
    )
    _account_attention(m)
    page.parts = build_parts(m)
    page.about = _about(m)
    page.attention = sort_attention(m.attention)
    page.findings = findings_for(snap, {m.id}, m.keys)
    page.history, page.history_since = _history(m)
    page.reports = _reports(m)
    page.sources = sources_for(snap, SOURCES)

    parts = {p.key: p for p in page.parts}
    page.badges = [(TYPE_WORDS.get(m.type, m.type or "Mailbox"), None)]
    if m.user is not None:
        page.badges.append(("Sign-in allowed", "ok") if m.user.get("accountEnabled") else ("Sign-in blocked", "bad"))
    if m.box.get("LitigationHoldEnabled"):
        page.badges.append(("On hold", None))
    if m.box.get("ArchiveStatus") not in (None, "", "None"):
        page.badges.append(("Archive on", None))
    if m.box.get("HiddenFromAddressListsEnabled"):
        page.badges.append(("Hidden from the address book", None))
    if mailbox_forwarding(m.box) and is_external(mailbox_forwarding(m.box), m.domains):
        page.badges.append(("Forwards outside", "bad"))

    size = parts.get("size")
    if m.total is not None and m.quota:
        foot = f"{100 * m.total / m.quota:.0f}% of {_size(m.quota)}"
    elif m.total is not None:
        foot = "No sending limit read"
    else:
        foot = "Not read" if not m.sizes_collected else "Size not returned"
    page.tiles = [
        attention_tile(page.attention),
        Highlight("Size", _size(m.total) if m.total is not None else "Not checked", foot, size.tone if size else None),
    ]
    access = parts.get("access")
    if access is not None:
        page.tiles.append(Highlight("Others with access", access.summary, "Can open or send from it", access.tone))
    fwd = parts.get("forwarding")
    if fwd is not None:
        page.tiles.append(Highlight("Forwarding and rules", fwd.summary, "", fwd.tone))
    settings = parts.get("settings")
    if settings is not None and settings.summary:
        page.tiles.append(Highlight("Protocols", settings.summary, "", settings.tone))
    devices = parts.get("devices")
    if devices is not None:
        page.tiles.append(Highlight("Phones and tablets", devices.summary, "Sync this mailbox", devices.tone))
    return page


# -- The Mailboxes page -------------------------------------------------------------------


@dataclass
class MailboxRow:
    id: str
    name: str
    address: str
    type: str
    account: str  # Active, Sign-in blocked, No account
    size: str
    size_sort: int
    share: int | None  # percent of quota
    forwarding: str  # "", "Inside", "Outside"
    access: int  # people who can open or send from it
    rules: int
    flagged_rules: int
    findings: int


def directory(conn, tenant, now: datetime | None = None) -> tuple[list[MailboxRow], list[Highlight]] | None:
    """Every mailbox with the few facts that tell you where to look. None before Mailboxes is collected."""
    now = now or datetime.now(UTC)
    snap = TenantSnapshot(conn, tenant)
    boxes = mailboxes(snap)
    if boxes is None:
        return None
    users = {u["_id"]: u for u in snap.objects("users", "user") or []}
    domains = tenant_domains(snap) or set()
    sizes = {s.get("mailboxId"): s for s in snap.objects("mailbox_usage", "mailbox_size") or []}
    d = _Directory(boxes, list(users.values()))
    access: dict[str, set[str]] = {}
    for box, _, person, _, _ in _grants(snap, d):
        if box is not None:
            access.setdefault(box["_id"], set()).add(person.lower())
    rules: dict[str, list[dict]] = {}
    for r in snap.objects("inbox_rules", "inbox_rule") or []:
        key = r.get("mailboxId") or _text(r.get("mailbox")).lower()
        rules.setdefault(key, []).append(r)
    addresses = {_text(m.get("PrimarySmtpAddress")).lower(): m["_id"] for m in boxes}
    counts: dict[str, int] = {}
    for r in conn.execute(
        "SELECT object_id, subject FROM findings WHERE tenant_id = ? AND resolved_at IS NULL", (tenant["id"],)
    ):
        key = r["object_id"] if r["object_id"] in addresses.values() else addresses.get((r["subject"] or "").lower())
        if key:
            counts[key] = counts.get(key, 0) + 1

    rows = []
    outside = near = shared_open = 0
    for m in boxes:
        address = _text(m.get("PrimarySmtpAddress"))
        user = users.get(m.get("ExternalDirectoryObjectId"))
        s = sizes.get(m["_id"]) or {}
        total, quota = s.get("totalBytes"), s.get("sendQuotaBytes")
        share = round(100 * total / quota) if isinstance(total, int) and quota else None
        if share is not None and total >= NEAR_QUOTA * quota:
            near += 1
        target = mailbox_forwarding(m)
        fwd = ("Outside" if is_external(target, domains) else "Inside") if target else ""
        outside += fwd == "Outside"
        mine = rules.get(m["_id"]) or rules.get(address.lower()) or []
        flagged = sum(inbox_rule_risk(r, domains).severity in ("high", "medium") for r in mine)
        if m.get("RecipientTypeDetails") == "SharedMailbox" and user is not None and user.get("accountEnabled"):
            shared_open += 1
        rows.append(
            MailboxRow(
                m["_id"],
                _text(m.get("DisplayName")) or address or m["_id"],
                address,
                mailbox_type(m.get("RecipientTypeDetails")),
                ("Active" if user.get("accountEnabled") else "Sign-in blocked") if user else "No account",
                _size(total) if isinstance(total, int) else "",
                total if isinstance(total, int) else -1,
                share,
                fwd,
                len(access.get(m["_id"], ())),
                len(mine),
                flagged,
                counts.get(m["_id"], 0),
            )
        )
    rows.sort(key=lambda r: (-r.findings, -r.flagged_rules, r.forwarding != "Outside", fold(r.name)))
    by_type = {}
    for r in rows:
        by_type[r.type] = by_type.get(r.type, 0) + 1
    tiles = [
        Highlight(
            "Mailboxes",
            len(rows),
            ", ".join(f"{n} {t.lower()}" for t, n in sorted(by_type.items(), key=lambda kv: -kv[1])),
        ),
        Highlight("Forwarding outside", outside, "Mail leaves the organisation", "bad" if outside else "ok"),
        Highlight(
            f"Over {NEAR_QUOTA:.0%} full",
            near if sizes else "Not checked",
            "Close to their sending limit",
            "warn" if near else None,
        ),
        Highlight("Shared with others", sum(1 for r in rows if r.access), "Someone else can open or send from them"),
        Highlight(
            "Shared mailboxes with sign-in",
            shared_open,
            "The account should be blocked",
            "warn" if shared_open else None,
        ),
    ]
    return rows, tiles
