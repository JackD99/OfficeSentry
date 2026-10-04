"""One page per account: everything Office Sentry has collected about one person.

Licences, MFA, admin roles, groups, mailbox and who can get into it, devices,
OneDrive sharing, apps, sign-ins and admin changes, open findings and what
changed for the account across stored snapshots. Like the detail reports it
reads stored data only and returns plain data; ``user.html`` and the exports
format it.

Each area of the page is a "part": a function decorated with ``@part(order)``
that returns a ``Part`` (or None to leave the area out) and can raise items
for "Needs attention" with ``person.flag()``. Orders are spaced by 10 so new
areas (usage, say) slot in between without touching the others.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .. import authmethods, devices, signins
from ..licences import FREE_SKUS, is_stale, last_sign_in, load_prices, sku_name
from ..mailflow import inbox_rule_risk, is_external, mailbox_forwarding, tenant_domains
from ..rules import SENSITIVE_DELEGATED
from ..snapshot import TenantSnapshot
from ..text import plural as _plural
from .data_reports import (
    COMPLIANCE,
    DataReport,
    Highlight,
    Link,
    Section,
    _describe_actions,
    _describe_conditions,
    _device_issues,
    _type,
    link_people,
    people_index,
)
from .identity_reports import (
    GAP_TEXT,
    METHOD_NAMES,
    P1,
    Enforcement,
    Money,
    Registration,
    _methods,
    _phishing_resistant,
    _roles,
)
from .mail_reports import _account, _Directory, _grants, _size
from .monthly_review import setting

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
INACTIVE_DAYS = 90
HISTORY_LIMIT = 60


@dataclass
class Attention:
    severity: str  # critical | high | medium | low
    text: str
    part: str = ""  # key of the part with the detail, for the page's anchor link


@dataclass
class Part:
    key: str
    title: str
    summary: str = ""  # the answer in a few words, shown next to the title
    tone: str | None = None  # ok | warn | bad, for the summary
    facts: list[tuple[str, object]] = field(default_factory=list)
    tables: list[Section] = field(default_factory=list)
    empty: str = ""  # shown when there are no facts or table rows
    unavailable: str = ""  # why this couldn't be checked; replaces everything else
    note: str = ""  # what's partial or left out, in small print


@dataclass
class Event:
    day: str  # YYYY-MM-DD
    area: str
    text: str
    tone: str | None = None  # bad | warn | ok


@dataclass
class Profile:
    tenant: dict
    user_id: str
    user: dict
    generated_at: datetime
    name: str
    upn: str
    badges: list[tuple[str, str | None]] = field(default_factory=list)
    tiles: list[Highlight] = field(default_factory=list)
    attention: list[Attention] = field(default_factory=list)
    account: list[tuple[str, object]] = field(default_factory=list)
    parts: list[Part] = field(default_factory=list)
    history: list[Event] = field(default_factory=list)
    history_since: str | None = None
    findings: list[dict] = field(default_factory=list)
    sources: list[tuple[str, str | None]] = field(default_factory=list)

    def as_report(self) -> DataReport:
        """The same content as a detail report, for the PDF and XLSX downloads."""
        report = DataReport("user", self.name, f"Everything collected about {self.upn or self.name}.",
                            self.tenant, self.generated_at, sources=self.sources, highlights=self.tiles)
        todo = Section("Needs attention", ["What", "Area"], empty="Nothing needs attention.")
        titles = {p.key: p.title for p in self.parts}
        for a in self.attention:
            todo.add([a.text, titles.get(a.part, "Account")], a.severity)
        account = Section("Account", ["Detail", "Value"])
        for label, value in self.account:
            account.add([label, value])
        sections = [todo, account]
        for p in self.parts:
            if p.unavailable:
                sections.append(Section(p.title, ["Note"], rows=[[p.unavailable]], flags=[None]))
                continue
            if p.facts:
                facts = Section(p.title, ["Detail", "Value"])
                for label, value in p.facts:
                    facts.add([label, value])
                sections.append(facts)
            for t in p.tables:
                sections.append(Section(t.title, t.columns, list(t.rows), list(t.flags), t.intro, t.empty,
                                        compare=False))
            if not p.facts and not p.tables:
                sections.append(Section(p.title, ["Note"], rows=[[p.empty or p.summary]], flags=[None]))
        if self.findings:
            found = Section("Open findings", ["Check", "Detail", "First seen"])
            for f in self.findings:
                found.add([f["title"], f["detail"], (f["first_seen"] or "")[:10]], f["severity"])
            sections.append(found)
        history = Section("History", ["Seen", "Area", "Change"], compare=False,
                          empty="No changes recorded yet.")
        for e in self.history:
            history.add([e.day, e.area, e.text])
        sections.append(history)
        report.sections = sections
        return report


class Person:
    """What every part needs: the snapshot, the account and the ways other data names it."""

    def __init__(self, snap: TenantSnapshot, user: dict, now: datetime):
        self.snap = snap
        self.user = user
        self.id = user["_id"]
        self.now = now
        self.upn = user.get("userPrincipalName") or ""
        self.label = self.upn or user.get("displayName") or self.id
        self.guest = (user.get("userType") or "Member") == "Guest"
        self.enabled = bool(user.get("accountEnabled"))
        # Exchange, SharePoint and Intune name people by sign-in name or address.
        self.keys = {k.lower() for k in (self.upn, user.get("mail")) if isinstance(k, str) and k}
        self.attention: list[Attention] = []
        self.roles = (_roles(snap) or {}).get(self.id)
        self._index: dict[str, str] | None = None

    @property
    def admin(self) -> bool:
        return bool(self.roles and (self.roles["active"] or self.roles["eligible"]))

    def flag(self, severity: str, text: str, part: str = "") -> None:
        self.attention.append(Attention(severity, text, part))

    def link(self, sections: list[Section]) -> None:
        if self._index is None:
            self._index = people_index(self.snap)
        link_people(sections, self.snap.tenant["id"], self._index)

    def named(self, value) -> bool:
        return isinstance(value, str) and value.lower() in self.keys


Builder = Callable[[Person], Part | None]
PARTS: list[tuple[int, Builder]] = []


def part(order: int) -> Callable[[Builder], Builder]:
    def register(fn: Builder) -> Builder:
        PARTS.append((order, fn))
        PARTS.sort(key=lambda p: p[0])
        return fn
    return register


# -- Small formatting helpers ------------------------------------------------------------------


def _day(value) -> str:
    value = devices.parse_ts(value) if isinstance(value, str) else value
    return f"{value.day} {value:%b %Y}" if value else ""


def _when(value, now: datetime) -> str:
    """'3 Sep 2026 (12 days ago)'."""
    when = devices.parse_ts(value) if isinstance(value, str) else value
    if not when:
        return ""
    days = (now - when).days
    ago = "today" if days <= 0 else ("yesterday" if days == 1 else f"{days} days ago")
    return f"{_day(when)} ({ago})"


def _yes(value) -> str:
    return "Unknown" if value is None else ("Yes" if value else "No")


def _currency(snap: TenantSnapshot) -> str:
    return setting(snap.conn, "currency", "GBP")


# -- Licences ----------------------------------------------------------------------------------


def _licences(p: Person) -> tuple[list[tuple[str, str, float | None]], dict]:
    """(sku ID, name, monthly price) for each licence held, and the tenant's SKUs by ID."""
    skus = {s["skuId"]: s for s in p.snap.objects("tenant_profile", "subscribed_sku") or [] if s.get("skuId")}
    prices = load_prices(p.snap.conn)
    out = []
    for sid in dict.fromkeys(p.user.get("skuIds") or []):
        part_number = (skus.get(sid) or {}).get("skuPartNumber")
        out.append((sid, sku_name(part_number) if part_number else "Unknown licence",
                    prices.get(part_number) if part_number not in FREE_SKUS else 0.0))
    return out, skus


@part(10)
def licences_part(p: Person) -> Part:
    held, skus = _licences(p)
    currency = _currency(p.snap)
    result = Part("licences", "Licences")
    if not held:
        result.summary, result.empty = "None", "Holds no licences."
        return result
    table = Section("Licences", ["Licence", "Cost / month"])
    for _, name, price in held:
        table.add([name, "Not priced" if price is None else Money(price, currency)])
    priced = [price for _, _, price in held if price is not None]
    total = sum(priced)
    result.tables = [table]
    result.summary = _plural(len(held), "licence") + (f", {Money(total, currency)} a month" if total else "")
    if len(priced) < len(held):
        result.note = "Some licences have no price yet. An admin can add them on the Licence prices page."

    paid = [(sid, name) for sid, name, price in held if price != 0.0]
    cost = f" ({Money(total, currency)} a month)" if total else ""
    if paid and not p.enabled:
        p.flag("medium", f"Sign-in is blocked but the account still holds {_plural(len(paid), 'paid licence')}"
                         f"{cost}. Remove them or convert the mailbox to shared.", "licences")
    elif paid and is_stale(p.user, p.now, INACTIVE_DAYS):
        p.flag("medium", f"No sign-in for {INACTIVE_DAYS} days or more but holds "
                         f"{_plural(len(paid), 'paid licence')}{cost}.", "licences")
    plans = {sid: {sp.get("servicePlanId") for sp in s.get("servicePlans") or [] if sp.get("servicePlanId")}
             for sid, s in skus.items()}
    names = {sid: name for sid, name, _ in held}
    for a in names:
        for b in names:
            if a == b or not plans.get(a) or not plans[a] <= plans.get(b, set()) or (plans[a] == plans[b] and a > b):
                continue
            p.flag("low", f"{names[a]} is already included in {names[b]}, so one of them is probably wasted.",
                   "licences")
    return result


# -- Microsoft 365 usage ------------------------------------------------------------------------


@part(15)
def usage_part(p: Person) -> Part | None:
    from .. import usage

    status = usage.status(p.snap)
    if status is None:
        return None  # usage isn't collected for this tenant yet; leave the area out
    result = Part("usage", "Microsoft 365 use")
    if not status.get("available"):
        result.unavailable = "Usage reports weren't readable. Check Reports.Read.All consent on the tenant page."
        return result
    if status.get("concealed"):
        result.unavailable = usage.CONCEALED_NOTICE
        return result
    row = usage.person(p.snap, p.upn)
    days = status.get("days") or usage.ACTIVE_DAYS
    if row is None:
        result.summary, result.empty = "Nothing recorded", f"Not in Microsoft's usage reports for the last {days} days."
        return result
    used = usage.used_services(row)
    for key, name in usage.SERVICES:
        last = (row.get("lastActive") or {}).get(key)
        if (row.get("active") or {}).get(key):
            text = f"Used (last {_day(last)})" if last else "Used"
        else:
            text = f"Not in the last {days} days" + (f" (last {_day(last)})" if last else "")
        result.facts.append((name, text))
    platforms = row.get("platforms")
    if platforms is not None:
        on = [name for key, name in usage.PLATFORMS if platforms.get(key)]
        result.facts.append(("Office apps used on", ", ".join(on) or "None"))
    if row.get("email"):
        e = row["email"]
        result.facts.append(("Email", f"{e.get('sent') or 0:,} sent, {e.get('received') or 0:,} received"))
    if row.get("teamsCounts"):
        t = row["teamsCounts"]
        result.facts.append(("Teams", f"{_plural(t.get('messages') or 0, 'message')}, "
                                      f"{_plural(t.get('meetings') or 0, 'meeting')}, "
                                      f"{_plural(t.get('calls') or 0, 'call')}"))
    if row.get("activations"):
        table = Section("Office installs", ["Product", "Windows", "Mac", "Mobile", "Last activated"])
        for a in row["activations"]:
            table.add([a.get("product"), a.get("windows") or 0, a.get("mac") or 0, a.get("mobile") or 0,
                       _day(a.get("lastActivated"))])
        result.tables.append(table)

    _, skus = _licences(p)
    sku_parts = {sid: s.get("skuPartNumber") or "" for sid, s in skus.items()}
    held = usage.paid_parts(row, p.user, sku_parts).parts
    box = {s.get("mailboxId"): s for s in p.snap.objects("mailbox_usage", "mailbox_size") or []}.get(p.id)
    prices = load_prices(p.snap.conn)
    currency = _currency(p.snap)
    suggestions = usage.rightsize(row, held, box) if p.enabled else []
    if suggestions:
        table = Section("Cheaper licence options", ["Licence", "Option", "Why", "Saving / month"])
        for sg in suggestions:
            saving = sg.saving(prices)
            table.add([sku_name(sg.current), sg.action, sg.why + (f". {sg.caution}" if sg.caution else ""),
                       Money(saving, currency) if saving else ""], "low")
            p.flag("low", f"{sku_name(sg.current)}: {sg.action.lower() if sg.target == '' else sg.action}. {sg.why}."
                   + (f" Saves {Money(saving, currency)} a month." if saving else ""), "usage")
        result.tables.append(table)
    if used:
        result.summary = ", ".join(used)
    else:
        result.summary, result.tone = f"No activity in {days} days", "warn" if held else None
    result.note = ("From Microsoft's usage reports, which run a couple of days behind"
                   + (f" (as of {_day(status.get('reportDate'))})." if status.get("reportDate") else "."))
    return result


# -- MFA and sign-in protection -----------------------------------------------------------------


@part(20)
def mfa_part(p: Person) -> Part:
    result = Part("mfa", "MFA and sign-in protection")
    reg, enf = Registration(p.snap), Enforcement(p.snap)
    row = reg.get(p.id)
    registered = reg.registered(p.id)
    user_type = "Guest" if p.guest else "Member"
    sources, excluded = enf.for_user(p.id, p.label, user_type) if enf.known else ([], [])
    required = bool(sources) if enf.known else None
    cov = enf.coverage(p.id, p.label, user_type)

    if p.guest:
        result.facts.append(("MFA set up", "Set up in their own organisation, so not visible here"))
    else:
        result.facts.append(("MFA set up", _yes(registered)))
        if row is not None:
            result.facts.append(("Methods", ", ".join(_methods(row)) or "None"))
            default = row.get("defaultMfaMethod")
            if default and default.lower() != "none":
                result.facts.append(("Default method", METHOD_NAMES.get(default.lower(), default)))
            result.facts.append(("Phishing-resistant method", _yes(_phishing_resistant(row))))
            _sign_in_strength(p, row, result)
    result.facts.append(("MFA required by", ", ".join(sources) or ("Nothing" if enf.known else "Not checked")))
    if excluded:
        result.facts.append(("Excluded from", ", ".join(excluded)))
    if cov is not None:
        result.facts.append(("Conditional Access policies that apply", ", ".join(cov.policies) or "None"))
        result.facts.append(("Legacy sign-in blocked", "Yes" if cov.legacy_blocked or enf.security_defaults else "No"))
    elif enf.security_defaults:
        result.facts.append(("Legacy sign-in blocked", "Yes, by Security Defaults"))
    if enf.per_user_checked and p.id in enf.per_user:
        result.facts.append(("Per-user MFA", (enf.per_user[p.id] or "disabled").capitalize()))
    result.note = " ".join(n for n in (reg.note, enf.note) if n)

    if p.guest:
        result.summary, result.tone = ("Required", "ok") if required else (
            ("Not required", "warn") if required is False else ("Not checked", None))
        if required is False and p.enabled:
            p.flag("low", "This guest isn't asked for MFA when signing in to this tenant.", "mfa")
        return result
    if registered and required:
        result.summary, result.tone = "Protected", "ok"
        return result
    if required is False:
        kind = "none" if registered is False else "not_required"
    elif required and registered is False:
        kind = "not_registered"
    else:
        kind = "unknown"
    title, meaning = GAP_TEXT[kind]
    if kind == "not_required" and registered is None:
        title, meaning = "Not required", "Isn't asked for MFA, so a stolen password may be enough."
    result.summary = title
    result.tone = {"none": "bad", "not_required": "warn", "not_registered": "warn"}.get(kind)
    if p.enabled and kind != "unknown":
        severity = {"none": "critical" if p.admin else "high",
                    "not_required": "high" if p.admin else "medium",
                    "not_registered": "high" if p.admin else "medium"}[kind]
        p.flag(severity, f"{title}: {meaning}" + (" This account holds an admin role." if p.admin else ""), "mfa")
    if excluded and p.enabled:
        p.flag("medium" if p.admin else "low", f"Excluded from {', '.join(excluded)}.", "mfa")
    return result


def _sign_in_strength(p: Person, row: dict, result: Part) -> None:
    """How strong the account's methods are (Passwords and sign-in methods report), as facts and flags."""
    s = authmethods.strength(row)
    result.facts.append(("Strongest method", s.strongest))
    first = authmethods.preferred(row)
    if first:
        result.facts.append(("Asked for first", first))
    for key, label in (("isPasswordlessCapable", "Can sign in without a password"),
                       ("isSsprRegistered", "Can reset their own password")):
        if row.get(key) is not None:
            result.facts.append((label, _yes(row.get(key))))
    if not p.enabled:
        return
    privileged = bool(p.roles and p.roles["privileged"])
    if privileged and s.methods and not s.phishing_resistant:
        p.flag("medium", "Admin without a phishing-resistant method: register a passkey or security key. A fake "
                         "sign-in page can relay codes and push approvals.", "mfa")
    if s.phone_only:
        p.flag("medium" if privileged else "low", "A text or call is the only second factor: set up the "
                                                  "Authenticator app.", "mfa")


# -- Admin roles --------------------------------------------------------------------------------


@part(30)
def roles_part(p: Person) -> Part:
    result = Part("roles", "Admin roles")
    rows = p.snap.objects("admin_roles", "role_assignment")
    if rows is None:
        result.unavailable = "Admin roles haven't been collected yet. Refresh Admin roles on the tenant page."
        return result
    mine = [r for r in rows if r.get("principalId") == p.id]
    if not mine:
        result.summary, result.empty = "None", "Holds no admin roles."
        return result
    table = Section("Admin roles", ["Role", "How assigned", "Scope", "Privileged"])
    for r in sorted(mine, key=lambda r: (not r.get("privileged"), r.get("assignment") != "active",
                                         r.get("roleName") or "")):
        table.add([r.get("roleName") or "Unknown role",
                   "Active" if r.get("assignment") == "active" else "Eligible (PIM)",
                   "Whole directory" if r.get("directoryScopeId") in (None, "/") else "Limited scope",
                   bool(r.get("privileged"))])
    result.tables = [table]
    names = list(dict.fromkeys(r.get("roleName") or "Unknown role" for r in mine))
    result.summary = names[0] if len(names) == 1 else f"{names[0]} and {len(names) - 1} more"
    result.tone = "warn" if any(r.get("roleName") == "Global Administrator" for r in mine) else None
    privileged = sorted({r.get("roleName") for r in mine if r.get("privileged") and r.get("roleName")})
    if not p.enabled:
        p.flag("high", f"Sign-in is blocked but the account still holds {', '.join(names)}. Remove the roles.",
               "roles")
    elif p.guest and privileged:
        p.flag("high", f"A guest from another organisation holds {', '.join(privileged)}.", "roles")
    return result


# -- Groups and teams ---------------------------------------------------------------------------

GROUP_KINDS = {"microsoft365": "Microsoft 365 group", "security": "Security group",
               "mail_security": "Mail-enabled security group", "distribution": "Distribution list"}


@part(40)
def groups_part(p: Person) -> Part:
    result = Part("groups", "Teams and groups")
    groups = p.snap.objects("groups", "group")
    if groups is None:
        result.unavailable = "Groups haven't been collected yet. Refresh Groups and Teams on the tenant page."
        return result
    ca = {g["_id"]: g for g in p.snap.objects("conditional_access", "ca_group") or []}
    rows, owned, orphaned = [], 0, []
    old_snapshot = any(g.get("examined") and "memberIds" not in g for g in groups)
    for g in groups:
        role = None
        if any(p.named(o.get("upn")) for o in g.get("owners") or []):
            role = "Owner"
            owned += 1
        elif p.id in (g.get("memberIds") or []) or p.id in ((ca.get(g["_id"]) or {}).get("members") or []):
            role = "Member"
        elif any(p.named(x) for x in g.get("guests") or []):
            role = "Guest member"
        if role is None:
            continue
        kind = "Team" if g.get("isTeam") else GROUP_KINDS.get(g.get("kind"), "Group")
        notes = []
        if role == "Owner" and g.get("examined"):
            enabled_owners = g.get("enabledOwnerCount") or 0
            if not p.enabled and enabled_owners == 0:
                notes.append("No active owner left")
                orphaned.append(g.get("displayName") or "a group")
            elif p.enabled and enabled_owners == 1:
                notes.append("Only owner")
        if g.get("dynamic"):
            notes.append("Dynamic membership")
        if g.get("synced"):
            notes.append("Synced from AD")
        rows.append(((role != "Owner", not g.get("isTeam"), (g.get("displayName") or "").lower()),
                     [g.get("displayName") or "Unnamed group", kind, role, "; ".join(notes)],
                     "medium" if "No active owner left" in notes else None))
    if orphaned:
        p.flag("medium", f"Owned {', '.join(orphaned[:3])}{' and more' if len(orphaned) > 3 else ''} and sign-in is "
                         "now blocked, so nobody active looks after "
                         f"{'it' if len(orphaned) == 1 else 'them'}. Add a new owner.", "groups")
    result.note = ("Members are read for teams, Microsoft 365 groups and groups used by Conditional Access. "
                   "For other security groups and distribution lists only owners show.")
    if old_snapshot:
        result.note += " Team and group members show after the next Groups and Teams collection."
    if not rows:
        result.summary, result.empty = "None", "Not an owner or member of any team or group Office Sentry reads."
        return result
    table = Section("Teams and groups", ["Name", "Type", "Role", "Note"])
    for _, row, flag in sorted(rows, key=lambda r: r[0]):
        table.add(row, flag)
    result.tables = [table]
    result.summary = f"Owner of {owned}, member of {len(rows) - owned}" if owned else _plural(len(rows), "group")
    return result


# -- Mailbox ------------------------------------------------------------------------------------

PROTOCOLS = (("PopEnabled", "POP"), ("ImapEnabled", "IMAP"), ("ActiveSyncEnabled", "ActiveSync"),
             ("EwsEnabled", "EWS"))


@part(50)
def mailbox_part(p: Person) -> Part:
    result = Part("mailbox", "Mailbox")
    boxes = p.snap.objects("mailboxes", "mailbox")
    status = p.snap.objects("mailboxes", "exchange_status")
    if status and not status[0].get("available"):
        result.unavailable = "This tenant has no Exchange Online licence."
        return result
    if boxes is None:
        result.unavailable = "Mailboxes haven't been collected yet. Refresh Mailboxes on the tenant page."
        return result
    users = p.snap.objects("users", "user") or []
    d = _Directory(boxes, users)
    box = d.mailbox(p.id) or next((d.mailbox(k) for k in p.keys if d.mailbox(k)), None)
    domains = tenant_domains(p.snap) or set()
    if box is not None:
        result.facts.append(("Address", box.get("PrimarySmtpAddress")))
        result.facts.append(("Type", _type(box.get("RecipientTypeDetails"))))
        sizes = {s.get("mailboxId"): s for s in p.snap.objects("mailbox_usage", "mailbox_size") or []}
        s = sizes.get(box["_id"])
        if s and s.get("totalBytes") is not None:
            quota = s.get("sendQuotaBytes")
            used = f"{_size(s['totalBytes'])}" + (f" of {_size(quota)} ({100 * s['totalBytes'] / quota:.0f}%)"
                                                  if quota else "")
            result.facts.append(("Size", used))
            if quota and s["totalBytes"] >= 0.9 * quota:
                p.flag("medium", f"The mailbox is {100 * s['totalBytes'] / quota:.0f}% full and stops sending at "
                                 f"{_size(quota)}.", "mailbox")
            result.summary = _size(s["totalBytes"])
        result.facts.append(("Archive", "On" if box.get("ArchiveStatus") not in (None, "", "None") else "Off"))
        result.facts.append(("Litigation hold", "On" if box.get("LitigationHoldEnabled") else "Off"))
        target = mailbox_forwarding(box)
        if target:
            outside = is_external(target, domains)
            result.facts.append(("Forwarding", f"{target}{' (outside the organisation)' if outside else ''}"
                                 + ("" if box.get("DeliverToMailboxAndForward") else ", no copy kept")))
            if outside:
                p.flag("high", f"The mailbox forwards all mail to {target}, outside the organisation.", "mailbox")
                result.summary, result.tone = "Forwards outside", "bad"
        else:
            result.facts.append(("Forwarding", "None"))
        if box.get("HiddenFromAddressListsEnabled"):
            result.facts.append(("Hidden from the address book", "Yes"))
        cas = {c["_id"]: c for c in p.snap.objects("mailboxes", "cas_mailbox") or []}.get(box["_id"])
        if cas is not None:
            allowed = [name for key, name in PROTOCOLS if cas.get(key)]
            if cas.get("SmtpClientAuthenticationDisabled") is False:
                allowed.append("SMTP AUTH")
            result.facts.append(("Older protocols allowed", ", ".join(allowed) or "None"))
        result.summary = result.summary or _type(box.get("RecipientTypeDetails"))

    rules = [r for r in p.snap.objects("inbox_rules", "inbox_rule") or []
             if r.get("mailboxId") == p.id or p.named(r.get("mailbox"))]
    if rules:
        table = Section("Inbox rules", ["Rule", "Enabled", "What it does", "Why it stands out"])
        for r in sorted(rules, key=lambda r: r.get("sequence") or 0):
            risk = inbox_rule_risk(r, domains)
            table.add([r.get("displayName"), r.get("isEnabled", True),
                       f"{_describe_conditions(r.get('conditions') or {})}: {_describe_actions(r)}",
                       "; ".join(risk.reasons)], risk.severity)
            if risk.severity in ("high", "medium"):
                p.flag(risk.severity, f"Inbox rule \"{r.get('displayName')}\": {'; '.join(risk.reasons)}.",
                       "mailbox")
        result.tables.append(table)

    grants = _grants(p.snap, d)
    into = [g for g in grants if box is not None and g[0] is not None and g[0]["_id"] == box["_id"]]
    if into:
        table = Section("Who else can open or send from it", ["Person", "Access", "Account"])
        for _, _, person, user, right in sorted(into, key=lambda g: (g[2].lower(), g[4])):
            words, flag = _account(user, person, domains)
            table.add([person, right, words], flag)
            if flag == "high":
                p.flag("high", f"{person} ({words.lower()}) has {right.lower()} to this mailbox.", "mailbox")
        result.tables.append(table)
    out = [g for g in grants if (g[3] or {}).get("_id") == p.id or (g[3] is None and p.named(g[2]))]
    out = [g for g in out if box is None or g[0] is None or g[0]["_id"] != box["_id"]]
    if out:
        table = Section("Mailboxes this person can open or send from", ["Mailbox", "Type", "Access"])
        rights: dict[str, tuple[dict | None, list[str]]] = {}
        for b, label, _, _, right in out:
            entry = rights.setdefault(label, (b, []))
            if right not in entry[1]:
                entry[1].append(right)
        for label, (b, held) in sorted(rights.items(), key=lambda kv: kv[0].lower()):
            table.add([label, _type((b or {}).get("RecipientTypeDetails")), held])
        result.tables.append(table)
        if not p.enabled:
            p.flag("low", f"Sign-in is blocked but the account can still open or send from "
                          f"{_plural(len({g[1] for g in out}), 'other mailbox', 'other mailboxes')}.", "mailbox")
    if box is None and not result.tables:
        result.summary, result.empty = "None", "No mailbox, and no access to anyone else's."
    elif box is None:
        result.summary = "No mailbox of its own"
    return result


# -- Devices ------------------------------------------------------------------------------------


@part(60)
def devices_part(p: Person) -> Part:
    result = Part("devices", "Devices")
    entra = devices.objects(p.snap, "entra_device")
    if entra is None:
        result.unavailable = "Devices haven't been collected yet. Refresh Devices on the tenant page."
        return result
    status = devices.objects(p.snap, "intune_status") or []
    if not (status and status[0].get("available")):
        result.unavailable = ("This tenant has no Intune, and Entra ID doesn't record who uses each device, "
                              "so devices can't be listed per person. The Devices report lists them all.")
        return result
    mine = [d for d in devices.objects(p.snap, "managed_device") or [] if p.named(d.get("userPrincipalName"))]
    if not mine:
        result.summary, result.empty = "None", "No devices enrolled in Intune for this person."
        return result
    table = Section("Devices", ["Device", "Operating system", "Compliance", "Encrypted", "Last check-in",
                                "Problems"])
    problems = 0
    for d in sorted(mine, key=lambda d: (d.get("deviceName") or "").lower()):
        issues, flag = _device_issues(d, p.now)
        days = devices.days_since(d.get("lastSyncDateTime"), p.now)
        table.add([d.get("deviceName"), devices.os_label(d),
                   COMPLIANCE.get(d.get("complianceState"), d.get("complianceState")),
                   d.get("isEncrypted") if devices.is_computer(d) else "",
                   "Never" if days is None else ("Today" if days == 0 else ("Yesterday" if days == 1 else
                                                                             f"{days} days ago")),
                   "; ".join(issues)], flag)
        if issues:
            problems += 1
            if flag in ("high", "medium"):
                p.flag(flag, f"{d.get('deviceName')}: {'; '.join(issues)}.", "devices")
    result.tables = [table]
    result.summary = _plural(len(mine), "device") + (f", {problems} with problems" if problems else "")
    result.tone = "warn" if problems else "ok"
    return result


# -- OneDrive -----------------------------------------------------------------------------------


def _size_text(n) -> str:
    from .sharepoint import size
    return size(n)


@part(70)
def onedrive_part(p: Person) -> Part:
    result = Part("onedrive", "OneDrive")
    status = (p.snap.objects("sharepoint_usage", "usage_status") or [None])[0]
    drives = p.snap.objects("sharepoint_usage", "onedrive")
    scan = (p.snap.objects("shared_files", "scan_status") or [None])[0]
    shared = (p.snap.objects("shared_files", "shared_item") or []) if scan and scan.get("available") else None
    if drives is None and shared is None:
        result.unavailable = "OneDrive data hasn't been collected yet. Refresh SharePoint and OneDrive usage."
        return result
    if status and status.get("concealed"):
        result.note = ("Microsoft hides people's names in this tenant's usage reports, so OneDrive storage can't "
                       "be matched to accounts. An admin can turn this off in the Microsoft 365 admin centre "
                       "(Settings, Org settings, Reports).")
    drive = next((r for r in drives or [] if not r.get("deleted") and p.named(r.get("ownerPrincipalName"))), None)
    if drive:
        used, quota = drive.get("storageUsed") or 0, drive.get("storageAllocated") or 0
        result.facts.append(("Storage used", _size_text(used) + (f" of {_size_text(quota)}" if quota else "")))
        result.facts.append(("Files", f"{drive.get('files') or 0:,}"))
        result.facts.append(("Files active in the last 30 days", f"{drive.get('activeFiles') or 0:,}"))
        result.facts.append(("Last activity", _day(drive.get("lastActivityDate")) or "None recorded"))
        result.summary = _size_text(used)
        if not p.enabled and used:
            p.flag("low", f"Sign-in is blocked and the OneDrive still holds {_size_text(used)}. Move what's needed "
                          "before the account is deleted.", "onedrive")

    names = p.keys | ({p.user["displayName"].lower()} if p.user.get("displayName") else set())
    mine = [s for s in shared or [] if s.get("personal") and isinstance(s.get("owner"), str)
            and s["owner"].lower() in names]
    if shared is None:
        result.note = (result.note + " " if result.note else "") + \
            "Shared files haven't been scanned, so sharing isn't shown."
    elif mine:
        order = {"anyone": 0, "external": 1, "organization": 2}
        table = Section("Shared from this OneDrive", ["File or folder", "Shared with", "Can edit", "Link expires"])
        for s in sorted(mine, key=lambda s: (order.get(s.get("exposure"), 3), (s.get("path") or "").lower())):
            text = (s.get("path") or s.get("name") or "?") + (" (folder)" if s.get("folder") else "")
            who = {"anyone": "Anyone with the link", "organization": "Everyone in the organisation"}.get(
                s.get("exposure")) or ", ".join(s.get("externalPeople") or []) or "People outside"
            expires = [lk.get("expires") for lk in s.get("links") or [] if lk.get("scope") == "anonymous"]
            table.add([Link(text, s["webUrl"]) if s.get("webUrl") else text, who, bool(s.get("editable")),
                       ("Never" if not all(expires) else _day(min(expires))) if expires else ""],
                      {"anyone": "high", "external": "medium"}.get(s.get("exposure")))
        result.tables.append(table)
        anyone = sum(s.get("exposure") == "anyone" for s in mine)
        outside = sorted({e for s in mine for e in s.get("externalPeople") or []})
        if anyone:
            p.flag("high", f"Shares {_plural(anyone, 'item')} from OneDrive with anyone who has the link.", "onedrive")
        if outside:
            p.flag("medium", f"Shares OneDrive files with {_plural(len(outside), 'person', 'people')} outside the "
                             f"organisation ({', '.join(outside[:3])}{' and more' if len(outside) > 3 else ''}).",
                   "onedrive")
        if anyone:
            result.summary, result.tone = "Shared with anyone", "bad"
        elif outside:
            result.summary, result.tone = "Shared outside", "warn"
    if not drive and not result.tables:
        result.summary, result.empty = "None", "No OneDrive found for this account."
    return result


# -- Apps ---------------------------------------------------------------------------------------


@part(80)
def apps_part(p: Person) -> Part:
    result = Part("apps", "Apps this person allowed")
    grants = p.snap.objects("app_consents", "delegated_grant")
    if grants is None:
        result.unavailable = "Apps haven't been collected yet. Refresh Apps and consents on the tenant page."
        return result
    sps = {s["_id"]: s for s in p.snap.objects("app_consents", "service_principal") or []}
    own = p.snap.own_app_ids
    mine = [g for g in grants if g.get("consentType") == "Principal" and g.get("principalId") == p.id]
    if not mine:
        result.summary, result.empty = "None", "Hasn't consented to any app on their own behalf."
        return result
    by_app: dict[str, dict] = {}
    for g in mine:
        sp = sps.get(g.get("clientId")) or {}
        a = by_app.setdefault(g.get("clientId") or "?", {"sp": sp, "scopes": set()})
        a["scopes"].update(s for s in g.get("scopes") or [] if s)
    table = Section("Apps this person allowed", ["App", "Publisher", "Permissions", "From"])
    for a in sorted(by_app.values(), key=lambda a: (a["sp"].get("displayName") or "").lower()):
        sp = a["sp"]
        name = sp.get("displayName") or "Unknown app"
        third = not sp.get("microsoft") and (sp.get("appId") or "").lower() not in own and not sp.get("ownTenant")
        risky = sorted(a["scopes"] & SENSITIVE_DELEGATED)
        publisher = sp.get("verifiedPublisher") or sp.get("publisherName") or ""
        if third:
            publisher += " (verified)" if sp.get("verifiedPublisher") else (" (unverified)" if publisher
                                                                            else "Unverified")
        table.add([name, publisher or "Microsoft", sorted(a["scopes"]),
                   "Third party" if third else ("Microsoft" if sp.get("microsoft") else "This organisation")],
                  ("medium" if risky else "low") if third else None)
        if third and risky:
            p.flag("medium", f"Gave {name} access to {', '.join(risky)}. Check it's an app the business uses.",
                   "apps")
    result.tables = [table]
    result.summary = _plural(len(by_app), "app")
    return result


# -- Sign-ins and changes -----------------------------------------------------------------------


@part(90)
def activity_part(p: Person) -> Part:
    result = Part("activity", "Sign-ins and admin changes")
    u = p.user
    if u.get("signInActivityAvailable"):
        for key, label in (("lastSignInDateTime", "Last interactive sign-in"),
                           ("lastNonInteractiveSignInDateTime", "Last background sign-in"),
                           ("lastSuccessfulSignInDateTime", "Last successful sign-in")):
            result.facts.append((label, _when(u.get(key), p.now) or (
                "None recorded" if last_sign_in(u) else "Never")))
        last = last_sign_in(u)
        result.summary = _when(last, p.now).split(" (")[-1].rstrip(")").capitalize() if last else "Never"
    else:
        result.facts.append(("Last sign-in", f"Not checked: sign-in dates need {P1}"))

    status = (p.snap.objects("legacy_signins", "legacy_signin_status") or [None])[0]
    legacy = [s for s in p.snap.objects("legacy_signins", "legacy_signin") or []
              if s.get("userId") == p.id or p.named(s.get("user"))]
    if legacy:
        table = Section("Legacy sign-ins in the last 30 days", ["Client", "Succeeded", "Failed", "Last attempt",
                                                                "Countries"])
        for s in sorted(legacy, key=lambda s: s.get("lastAttempt") or "", reverse=True):
            table.add([s.get("client"), s.get("succeeded") or 0, s.get("failed") or 0,
                       _when(s.get("lastAttempt"), p.now), s.get("countries") or []],
                      "medium" if s.get("succeeded") else "low")
        result.tables.append(table)
        ok = sum(s.get("succeeded") or 0 for s in legacy)
        if ok:
            clients = ", ".join(sorted({s.get("client") or "?" for s in legacy if s.get("succeeded")}))
            p.flag("medium", f"Signed in {_plural(ok, 'time')} with legacy authentication ({clients}) in the last "
                             "30 days. These sign-ins skip MFA.", "activity")
    elif status and status.get("available"):
        result.facts.append(("Legacy sign-ins (30 days)", "None"))

    window = (p.snap.objects("admin_activity", "audit_window") or [None])[0]
    events = []
    for e in p.snap.objects("admin_activity", "admin_event") or []:
        by = p.named(e.get("actor"))
        to = any(p.named(t) for t in e.get("targets") or [])
        if by or to:
            events.append((e, by))
    if events:
        table = Section("Admin changes in the last 30 days", ["When (UTC)", "Change", "By", "Target", "Detail"],
                        compare=False)
        for e, by in sorted(events, key=lambda x: x[0].get("time") or "", reverse=True):
            table.add([(e.get("time") or "")[:16].replace("T", " "), e.get("activity"), e.get("actor"),
                       ", ".join(e.get("targets") or []), e.get("detail")],
                      "medium" if e.get("kind") in ("role_grant",) and not by else None)
        result.tables.append(table)
    elif window and window.get("available"):
        result.facts.append(("Admin changes (30 days)", "None by or to this account"))
    result.note = ("Office Sentry doesn't keep the full sign-in log; the last week is summarised under Recent "
                   "sign-ins, and legacy sign-ins and the last sign-in dates show here.")
    return result


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _utc(value) -> str:
    return value[:16].replace("T", " ") if isinstance(value, str) else ""


@part(85)
def sign_ins_part(p: Person) -> Part | None:
    """The last week of the sign-in log for this account, from the summaries the sign_ins collector keeps."""
    status = signins.status(p.snap)
    if status is None:
        return None
    days = status.get("days") if isinstance(status.get("days"), int) else signins.WINDOW_DAYS
    result = Part("signins", "Recent sign-ins")
    if not status.get("available"):
        result.unavailable = ("Not checked: Office Sentry can't read this tenant's sign-in log"
                              if status.get("reason") == "permission" else f"Not checked: the sign-in log needs {P1}")
        return result
    mine = lambda r: r.get("userId") == p.id or p.named(r.get("user"))  # noqa: E731
    row = signins.for_account(p.snap, p.id, p.keys) or {}
    risky = [r for r in p.snap.objects("sign_ins", "risky_sign_in") or [] if mine(r)]
    if not row and not risky:
        result.summary, result.empty = "None", f"No interactive sign-ins in the last {days} days."
        return result

    def n(key: str) -> int:
        return _int(row.get(key))

    unusual = signins.unusual_countries(p.snap)
    locations = [r for r in p.snap.objects("sign_ins", "sign_in_location") or [] if mine(r)]
    countries = [c for c in row.get("countries") or [] if isinstance(c, str)]
    ok, failed, single, mfa = n("succeeded"), n("failed"), n("singleFactor"), n("mfa")
    split = ", ".join(x for x in (f"{mfa} with MFA" if mfa else "",
                                  f"{single} with only a password" if single else "") if x)
    result.facts.append(("Successful", f"{ok} ({split})" if split else str(ok)))
    how = [f"{n('credentialFailures')} wrong password" if n("credentialFailures") else "",
           _plural(n("lockouts"), "lockout") if n("lockouts") else "",
           f"from {_plural(n('failureIpCount'), 'address', 'addresses')}" if n("failureIpCount") else ""]
    how = [h for h in how if h]
    result.facts.append(("Failed", (f"{failed} ({', '.join(how)})" if how else str(failed)) if failed else "None"))
    if n("interrupted"):
        result.facts.append(("MFA and other prompts", n("interrupted")))
    if row.get("lastFailure"):
        result.facts.append(("Last failure", f"{_when(row.get('lastFailure'), p.now)}: "
                             f"{signins.meaning(row.get('lastFailureCode'), row.get('lastFailureReason'))}"))
    if countries:
        result.facts.append(("Countries", ", ".join(c + (" (unusual for this tenant)" if c in unusual else "")
                                                    for c in countries)))
    for key, label in (("apps", "Apps"), ("operatingSystems", "Devices"), ("failureIps", "Addresses that failed")):
        values = [v for v in row.get(key) or [] if isinstance(v, str)]
        if values:
            result.facts.append((label, ", ".join(values)))

    odd = []
    places = Section("Where this account signed in", ["Country", "Successful", "Failed", "Cities", "IP addresses",
                                                      "Last success (UTC)"])
    for loc in sorted(locations, key=lambda r: -_int(r.get("succeeded"))):
        unusual_here = loc.get("country") in unusual and _int(loc.get("succeeded")) > 0
        if unusual_here:
            odd.append(loc["country"])
        places.add([signins.country(loc.get("country")), _int(loc.get("succeeded")), _int(loc.get("failed")),
                    loc.get("cities") or [], loc.get("ips") or [], _utc(loc.get("lastSuccess"))],
                   "medium" if unusual_here else None)
    if places.rows:
        result.tables.append(places)
    codes = row.get("failureCodes") if isinstance(row.get("failureCodes"), dict) else {}
    if codes:
        reasons = Section("Why sign-ins failed", ["Error code", "What it means", "Sign-ins"])
        for code, count in sorted(codes.items(), key=lambda c: -_int(c[1])):
            reasons.add([str(code), signins.meaning(code), _int(count)])
        result.tables.append(reasons)
    worst = None
    if risky:
        table = Section("Risky sign-ins", ["When (UTC)", "Risk level", "Risk state", "Result", "Country",
                                           "IP address"])
        for r in sorted(risky, key=lambda r: r.get("time") or "", reverse=True):
            severity = signins.risk_severity(r.get("level"), r.get("state")) if r.get("succeeded") else None
            if severity and (worst is None or (severity == "high" and worst[0] != "high")):
                worst = (severity, r)
            table.add([_utc(r.get("time")), (r.get("level") or "Unknown").capitalize(),
                       signins.RISK_STATES.get(r.get("state"), r.get("state")),
                       "Signed in" if r.get("succeeded") else signins.meaning(r.get("errorCode")),
                       signins.country(r.get("country")), r.get("ip")], severity or "low")
        result.tables.append(table)

    if worst:
        severity, r = worst
        p.flag(severity, f"Signed in on {_day(r.get('time'))} from {signins.country(r.get('country'))} although "
                         f"Entra ID rated the sign-in {r.get('level') or 'risky'} risk. Check with the person; if "
                         "they don't recognise it, reset the password and sign them out everywhere.", "signins")
    if failed >= signins.REPEATED_FAILURES:
        p.flag("high" if n("lockouts") else "medium",
               f"{_plural(failed, 'failed sign-in')} in the last {days} days"
               + (f", {n('credentialFailures')} with a wrong password" if n("credentialFailures") else "")
               + (", and locked out" if n("lockouts") else "")
               + ". If the person didn't make them, someone is guessing the password: make sure MFA is enforced.",
               "signins")
    if odd:
        p.flag("medium", f"Signed in from {', '.join(odd)}, which this tenant rarely signs in from. Check the "
                         "person was there.", "signins")
    if single and (p.admin or not mfa):
        p.flag("medium" if p.admin else "low",
               f"Signed in {_plural(single, 'time')} with only a password in the last {days} days"
               + ("" if mfa else ", never with MFA") + ".", "signins")
    result.summary = f"{_plural(n('signIns'), 'sign-in')}, {failed} failed"
    result.tone = "bad" if worst else ("warn" if failed >= signins.REPEATED_FAILURES or odd else None)
    result.note = (f"Interactive sign-ins in the last {days} days from the Entra ID sign-in log. MFA and other "
                   "prompts aren't counted as failures.")
    return result


# -- History across snapshots -------------------------------------------------------------------


def _object_history(conn, tenant_id: int, collector: str, kind: str, object_id: str) -> list[tuple[str, dict]]:
    rows = conn.execute(
        "SELECT r.finished_at, o.data FROM runs r JOIN objects o ON o.run_id = r.id AND o.kind = ?"
        " AND o.object_id = ? WHERE r.tenant_id = ? AND r.collector = ? AND r.status IN ('succeeded', 'partial')"
        " ORDER BY r.finished_at, r.id", (kind, object_id, tenant_id, collector)).fetchall()
    return [(r["finished_at"], json.loads(r["data"])) for r in rows]


def _role_history(conn, tenant_id: int, uid: str) -> list[tuple[str, set[str]]]:
    runs = conn.execute(
        "SELECT id, finished_at FROM runs WHERE tenant_id = ? AND collector = 'admin_roles'"
        " AND status IN ('succeeded', 'partial')"
        " AND EXISTS (SELECT 1 FROM objects o WHERE o.run_id = runs.id) ORDER BY finished_at, id",
        (tenant_id,)).fetchall()
    held: dict[int, set[str]] = {r["id"]: set() for r in runs}
    if runs:
        for r in conn.execute(
                "SELECT o.run_id, o.data FROM objects o JOIN runs r ON r.id = o.run_id WHERE r.tenant_id = ?"
                " AND r.collector = 'admin_roles' AND o.kind = 'role_assignment'"
                " AND json_extract(o.data, '$.principalId') = ?", (tenant_id, uid)):
            if r["run_id"] in held:
                d = json.loads(r["data"])
                held[r["run_id"]].add((d.get("roleName") or "a role")
                                      + ("" if d.get("assignment") == "active" else " (eligible)"))
    return [(r["finished_at"], held[r["id"]]) for r in runs]


def history(snap: TenantSnapshot, uid: str, skus: dict) -> tuple[list[Event], str | None]:
    """What changed for the account between stored snapshots, newest first, and the oldest snapshot it's in."""
    conn, tid = snap.conn, snap.tenant["id"]
    events: list[Event] = []

    def sku(sid: str) -> str:
        part_number = (skus.get(sid) or {}).get("skuPartNumber")
        return sku_name(part_number) if part_number else "a licence"

    users = _object_history(conn, tid, "users", "user", uid)
    since = users[0][0] if users else None
    for (_, old), (when, new) in zip(users, users[1:], strict=False):
        day = when[:10]
        if old.get("accountEnabled") and not new.get("accountEnabled"):
            events.append(Event(day, "Account", "Sign-in blocked", "warn"))
        elif not old.get("accountEnabled") and new.get("accountEnabled"):
            events.append(Event(day, "Account", "Sign-in allowed again"))
        for key, label in (("userPrincipalName", "Sign-in name"), ("displayName", "Name"),
                           ("userType", "Account type")):
            if old.get(key) and new.get(key) and old[key] != new[key]:
                events.append(Event(day, "Account", f"{label} changed from {old[key]} to {new[key]}"))
        before, after = set(old.get("skuIds") or []), set(new.get("skuIds") or [])
        events += [Event(day, "Licences", f"{sku(s)} added") for s in sorted(after - before)]
        events += [Event(day, "Licences", f"{sku(s)} removed") for s in sorted(before - after)]
        if old.get("lastPasswordChangeDateTime") != new.get("lastPasswordChangeDateTime") and \
                new.get("lastPasswordChangeDateTime"):
            events.append(Event(new["lastPasswordChangeDateTime"][:10], "Account", "Password changed"))
        if old.get("onPremisesSyncEnabled") != new.get("onPremisesSyncEnabled"):
            events.append(Event(day, "Account", "Now synced from on-premises AD" if new.get("onPremisesSyncEnabled")
                                else "No longer synced from on-premises AD"))

    roles = _role_history(conn, tid, uid)
    for (_, old), (when, new) in zip(roles, roles[1:], strict=False):
        events += [Event(when[:10], "Admin roles", f"Given {r}", "warn") for r in sorted(new - old)]
        events += [Event(when[:10], "Admin roles", f"{r} removed") for r in sorted(old - new)]

    mfa = _object_history(conn, tid, "mfa_registration", "mfa_registration", uid)
    for (_, old), (when, new) in zip(mfa, mfa[1:], strict=False):
        before, after = set(_methods(old)), set(_methods(new))
        events += [Event(when[:10], "MFA", f"Registered {m}", "ok") for m in sorted(after - before)]
        events += [Event(when[:10], "MFA", f"Removed {m}", "warn") for m in sorted(before - after)]
        if before == after and bool(old.get("isMfaRegistered")) != bool(new.get("isMfaRegistered")):
            events.append(Event(when[:10], "MFA", "MFA set up" if new.get("isMfaRegistered") else "MFA removed",
                                "ok" if new.get("isMfaRegistered") else "warn"))

    boxes = _object_history(conn, tid, "mailboxes", "mailbox", uid)
    for (_, old), (when, new) in zip(boxes, boxes[1:], strict=False):
        a, b = mailbox_forwarding(old), mailbox_forwarding(new)
        if a != b:
            events.append(Event(when[:10], "Mailbox", f"Forwarding set to {b}" if b else "Forwarding removed",
                                "warn" if b else None))
        if old.get("RecipientTypeDetails") != new.get("RecipientTypeDetails") and new.get("RecipientTypeDetails"):
            events.append(Event(when[:10], "Mailbox", f"Converted to a {_type(new['RecipientTypeDetails']).lower()} "
                                                      "mailbox"))
    events.sort(key=lambda e: e.day, reverse=True)
    return events[:HISTORY_LIMIT], since


# -- The page -----------------------------------------------------------------------------------


def find_user(snap: TenantSnapshot, user_id: str) -> dict | None:
    return next((u for u in snap.objects("users", "user") or [] if u["_id"] == user_id), None)


def _findings(snap: TenantSnapshot, person: Person) -> list[dict]:
    from ..rules import RULES
    titles = {r.key: r.title for r in RULES}
    rows = snap.conn.execute("SELECT * FROM findings WHERE tenant_id = ? AND resolved_at IS NULL",
                             (snap.tenant["id"],)).fetchall()
    out = [dict(r, title=titles.get(r["rule_key"], r["rule_key"])) for r in rows
           if r["object_id"] == person.id or person.named(r["subject"])]
    out.sort(key=lambda f: (SEVERITY_RANK.get(f["severity"], 4), f["title"]))
    return out


def _account_facts(p: Person) -> list[tuple[str, object]]:
    u = p.user
    facts: list[tuple[str, object]] = [("Name", u.get("displayName") or ""), ("Sign-in name", p.upn)]
    if u.get("mail") and u["mail"].lower() != p.upn.lower():
        facts.append(("Email", u["mail"]))
    kind = "Guest" if p.guest else "Member"
    if p.guest and u.get("externalUserState"):
        kind += {"PendingAcceptance": ", invitation not accepted", "Accepted": ", invitation accepted"}.get(
            u["externalUserState"], "")
    facts.append(("Account type", kind))
    facts.append(("Sign-in", "Allowed" if p.enabled else "Blocked"))
    facts.append(("Source", "Synced from on-premises AD" if u.get("onPremisesSyncEnabled") else "Cloud-only"))
    if u.get("createdDateTime"):
        facts.append(("Created", _when(u["createdDateTime"], p.now)))
    if u.get("lastPasswordChangeDateTime"):
        facts.append(("Password last changed", _when(u["lastPasswordChangeDateTime"], p.now)))
    facts.append(("Password expires", "Never (set to never expire)" if u.get("passwordNeverExpires")
                  else "Follows the organisation's policy"))
    return facts


def _account_attention(p: Person) -> None:
    u = p.user
    if not p.enabled:
        return
    stale = is_stale(u, p.now, INACTIVE_DAYS)
    last = last_sign_in(u)
    if stale and last:
        p.flag("medium" if u.get("licensed") else "low",
               f"No sign-in for {(p.now - last).days} days. If they've left, block sign-in and reclaim the "
               "licences.", "activity")
    elif stale:
        p.flag("low", f"Has never signed in (created {_day(u.get('createdDateTime')) or 'a while ago'}).",
               "activity")
    if p.guest and u.get("externalUserState") == "PendingAcceptance":
        created = devices.parse_ts(u.get("createdDateTime"))
        if created and (p.now - created).days > 30:
            p.flag("low", f"Invited {_day(created)} and never accepted. Remove the invitation if it's not needed.")


def build(conn, tenant, user_id: str, now: datetime | None = None) -> Profile | None:
    """The page for one account, or None when the latest Users snapshot doesn't have it."""
    now = now or datetime.now(UTC)
    snap = TenantSnapshot(conn, tenant)
    user = find_user(snap, user_id)
    if user is None:
        return None
    p = Person(snap, user, now)
    profile = Profile(dict(tenant), user_id, user, now, user.get("displayName") or p.label, p.upn)
    _account_attention(p)
    for _, builder in PARTS:
        result = builder(p)
        if result is not None:
            p.link(result.tables)
            profile.parts.append(result)
    profile.account = _account_facts(p)
    profile.attention = sorted(p.attention, key=lambda a: SEVERITY_RANK.get(a.severity, 4))
    profile.findings = _findings(snap, p)
    _, skus = _licences(p)
    profile.history, profile.history_since = history(snap, user_id, skus)

    parts = {x.key: x for x in profile.parts}
    profile.badges = [("Guest" if p.guest else "Member", None),
                      ("Sign-in allowed", "ok") if p.enabled else ("Sign-in blocked", "bad"),
                      ("Synced from AD" if user.get("onPremisesSyncEnabled") else "Cloud-only", None)]
    if p.admin:
        profile.badges.append(("Admin", "warn"))
    last = last_sign_in(user)
    sign_in_foot = (f"Last sign-in {_when(last, now).split(' (')[-1].rstrip(')')}" if last else "Never signed in") \
        if user.get("signInActivityAvailable") else f"Sign-in dates need {P1}"
    stale = p.enabled and is_stale(user, now, INACTIVE_DAYS)
    worst = profile.attention[0].severity if profile.attention else None
    profile.tiles = [
        Highlight("Needs attention", len(profile.attention), "Nothing to do" if not profile.attention else
                  f"Worst: {worst}", "bad" if worst in ("critical", "high") else ("warn" if worst else "ok")),
        Highlight("Sign-in", "Allowed" if p.enabled else "Blocked", sign_in_foot, "warn" if stale else None),
    ]
    for key, label in (("mfa", "MFA"), ("roles", "Admin roles"), ("licences", "Licences"),
                       ("devices", "Devices")):
        x = parts.get(key)
        if x is None:
            continue
        value = "Not checked" if x.unavailable else (x.summary or "None")
        foot = ""
        if key == "licences" and "," in value:
            value, foot = value.split(", ", 1)
        elif key == "devices" and "," in value:
            value, foot = value.split(", ", 1)
        elif key == "roles" and not x.unavailable and x.summary != "None":
            n = len(x.tables[0].rows) if x.tables else 0
            value, foot = str(n), x.summary
        profile.tiles.append(Highlight(label, value, foot, x.tone))
    sources = ("users", "mfa_registration", "conditional_access", "admin_roles", "groups", "mailboxes",
               "inbox_rules", "mailbox_usage", "devices", "sharepoint_usage", "shared_files", "app_consents",
               "legacy_signins", "sign_ins", "admin_activity")
    from ..collectors import REGISTRY
    profile.sources = [(REGISTRY[c].title, (snap.runs.get(c) or {"finished_at": None})["finished_at"])
                       for c in sources if c in REGISTRY]
    return profile


# -- Everyone, for the Users tab -----------------------------------------------------------------


@dataclass
class DirectoryRow:
    id: str
    name: str
    upn: str
    guest: bool
    enabled: bool
    last_sign_in: str  # "3 Sep 2026", "Never" or "Not checked"
    last_sign_in_sort: str
    mfa: str
    mfa_tone: str | None
    roles: list[str]
    licences: int
    findings: int


def directory(conn, tenant, now: datetime | None = None) -> tuple[list[DirectoryRow], list[Highlight]] | None:
    """Every account with the few facts that tell you where to look. None before Users is collected."""
    now = now or datetime.now(UTC)
    snap = TenantSnapshot(conn, tenant)
    users = snap.objects("users", "user")
    if users is None:
        return None
    reg, enf = Registration(snap), Enforcement(snap)
    roles = _roles(snap) or {}
    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    for u in users:
        for k in (u.get("userPrincipalName"), u.get("mail")):
            if isinstance(k, str) and k:
                labels.setdefault(k.lower(), u["_id"])
    ids = {u["_id"] for u in users}
    for r in conn.execute("SELECT object_id, subject FROM findings WHERE tenant_id = ? AND resolved_at IS NULL",
                          (snap.tenant["id"],)):
        uid = r["object_id"] if r["object_id"] in ids else labels.get((r["subject"] or "").lower())
        if uid:
            counts[uid] = counts.get(uid, 0) + 1

    rows = []
    no_mfa = inactive = 0
    for u in users:
        uid = u["_id"]
        guest = (u.get("userType") or "Member") == "Guest"
        enabled = bool(u.get("accountEnabled"))
        if not u.get("signInActivityAvailable"):
            seen, seen_sort = "Not checked", ""
        else:
            last = last_sign_in(u)
            seen, seen_sort = (_day(last), last.isoformat()) if last else ("Never", "0")
            inactive += bool(enabled and is_stale(u, now, INACTIVE_DAYS))
        label = u.get("userPrincipalName") or u.get("displayName") or uid
        registered = reg.registered(uid)
        sources, _ = enf.for_user(uid, label, "Guest" if guest else "Member") if enf.known else ([], [])
        required = bool(sources) if enf.known else None
        if guest:
            mfa, tone = ("Required", "ok") if required else (("Not required", "warn") if required is False
                                                             else ("Unknown", None))
        elif registered and required:
            mfa, tone = "Protected", "ok"
        elif required is False and registered is False:
            mfa, tone = "No MFA", "bad"
        elif required is False:
            mfa, tone = "Not required", "warn"
        elif required and registered is False:
            mfa, tone = "Not set up", "warn"
        else:
            mfa, tone = "Unknown", None
        if enabled and not guest and mfa == "No MFA":
            no_mfa += 1
        r = roles.get(uid)
        rows.append(DirectoryRow(uid, u.get("displayName") or label, u.get("userPrincipalName") or "", guest,
                                 enabled, seen, seen_sort, mfa if enabled else "–", tone if enabled else None,
                                 (r["active"] + [f"{n} (eligible)" for n in r["eligible"]]) if r else [],
                                 len(u.get("skuIds") or []), counts.get(uid, 0)))
    rows.sort(key=lambda r: (-r.findings, not r.roles, r.name.lower()))
    members = sum(not r.guest for r in rows)
    tiles = [
        Highlight("Accounts", len(rows), f"{members} members, {len(rows) - members} guests"),
        Highlight("Sign-in blocked", sum(not r.enabled for r in rows), "Can't sign in"),
        Highlight("No MFA at all", no_mfa if enf.known and not reg.note else "Not checked",
                  "Enabled members", "bad" if no_mfa else None),
        Highlight("Admins", sum(bool(r.roles) for r in rows), "Hold an admin role"),
        Highlight(f"Inactive {INACTIVE_DAYS}+ days", inactive if any(u.get("signInActivityAvailable") for u in users)
                  else "Not checked", "Enabled, no recent sign-in", "warn" if inactive else None),
    ]
    return rows, tiles
