"""Exchange Online detail reports: inbox rules, mailbox permissions, shared mailboxes,
mailbox size and legacy sign-ins.

Builders read stored snapshots only and fill a ``DataReport`` with ``Section``s,
like the other detail reports in ``data_reports.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..licences import last_sign_in
from ..mailflow import inbox_rule_risk, is_external, tenant_domains
from ..snapshot import TenantSnapshot
from .data_reports import DataReport, Highlight, Rows, Section, _describe_actions, _describe_conditions, _type
from .registry import report

NOT_COLLECTED = "Mailbox data hasn't been collected yet. Refresh Mailboxes on the tenant page."
RESOURCE_TYPES = {"SharedMailbox": "Shared", "RoomMailbox": "Room", "EquipmentMailbox": "Equipment"}


def _no_exchange(snap: TenantSnapshot) -> bool:
    status = snap.objects("mailboxes", "exchange_status")
    return bool(status and not status[0].get("available"))


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _size(value: int | None) -> str:
    if value is None:
        return ""
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


PLURALS = {"person": "people", "mailbox": "mailboxes"}


def _plural(n: int, word: str) -> str:
    return f"{n} {word if n == 1 else PLURALS.get(word, word + 's')}"


# -- Inbox rules -------------------------------------------------------------------


@report(
    "inbox-rules",
    "Inbox rules",
    "Rules that forward, delete or hide mail, and every rule in every mailbox.",
    ("inbox_rules", "mailboxes"),
    group="email",
    order=20,
    keywords="forward delete move rules mailbox compromise",
)
def inbox_rules(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    rules = snap.objects("inbox_rules", "inbox_rule")
    if rules is None:
        report.unavailable = "Inbox rules haven't been collected yet. Refresh Inbox rules on the tenant page."
        return
    if _no_exchange(snap):
        report.unavailable = "This tenant has no Exchange Online licence."
        return
    domains = tenant_domains(snap) or set()
    checked = snap.metric("mail.mailboxes_checked")
    ordered = sorted(rules, key=lambda r: (_text(r.get("mailbox")).lower(), r.get("sequence") or 0))
    risks = [(r, inbox_rule_risk(r, domains)) for r in ordered]
    flagged = [(r, k) for r, k in risks if k.severity]
    rank = {"high": 0, "medium": 1, "low": 2}

    check = Section("Rules to check", ["Mailbox", "Rule", "Enabled", "Why it stands out", "What it does"],
                    intro=(f"{_plural(len(flagged), 'rule')} out of {len(rules)}"
                           + (f" across {int(checked)} mailboxes" if checked else "")
                           + " forward outside the organisation, delete, or bury mail. These are the classic signs "
                             "of a compromised mailbox: check each one with the mailbox owner."),
                    empty="No inbox rule looks suspicious.")
    for r, k in sorted(flagged, key=lambda rk: rank.get(rk[1].severity, 3)):
        check.add([r.get("mailbox"), r.get("displayName"), r.get("isEnabled", True), "; ".join(k.reasons),
                   f"{_describe_conditions(r.get('conditions') or {})}: {_describe_actions(r)}"], k.severity)

    sends = Section("Rules that send mail elsewhere", ["Mailbox", "Rule", "Enabled", "Sends to",
                                                       "Outside the organisation"],
                    intro="Every rule that forwards or redirects mail, including to colleagues.",
                    empty="No inbox rule forwards or redirects mail.")
    for r, k in risks:
        if k.forwards_to:
            sends.add([r.get("mailbox"), r.get("displayName"), r.get("isEnabled", True), k.forwards_to,
                       bool(k.external)], ("high" if r.get("isEnabled", True) else "low") if k.external else None)

    every = Section("All inbox rules", ["Mailbox", "Rule", "Enabled", "Conditions", "Actions"],
                    empty="No inbox rules found.")
    for r, k in risks:
        every.add([r.get("mailbox"), r.get("displayName"), r.get("isEnabled", True),
                   _describe_conditions(r.get("conditions") or {}), _describe_actions(r)], k.severity)
    high = sum(1 for _, k in flagged if k.severity == "high")
    report.highlights = [
        Highlight("Rules to check", len(flagged), f"{high} high risk" if high else "None high risk",
                  "bad" if high else ("warn" if flagged else "ok")),
        Highlight("Forward outside", sum(1 for _, k in risks if k.external), "Rules sending mail out",
                  rows=Rows("Rules that send mail elsewhere", {"Outside the organisation": "Yes"})),
        Highlight("Inbox rules", len(rules), f"Across {int(checked)} mailboxes" if checked else "",
                  rows=Rows("All inbox rules")),
    ]
    report.sections = [check, sends, every]


# -- Mailbox permissions --------------------------------------------------------------

RIGHTS = ("Full access", "Send as", "Send on behalf")


class _Directory:
    """Resolves the names Exchange uses (UPN, display name, alias, object ID) to mailboxes and users."""

    def __init__(self, boxes: list[dict], users: list[dict]):
        self.boxes: dict[str, dict] = {}
        for m in boxes:
            for key in ("PrimarySmtpAddress", "UserPrincipalName", "Name", "Alias", "DisplayName",
                        "ExternalDirectoryObjectId", "Guid", "_id"):
                if isinstance(m.get(key), str) and m[key]:
                    self.boxes.setdefault(m[key].lower(), m)
        self.users: dict[str, dict] = {}
        for u in users:
            for key in ("userPrincipalName", "mail", "_id", "displayName"):
                if isinstance(u.get(key), str) and u[key]:
                    self.users.setdefault(u[key].lower(), u)

    def mailbox(self, name) -> dict | None:
        return self.boxes.get(name.lower()) if isinstance(name, str) else None

    def person(self, name) -> tuple[str, dict | None]:
        """(label, user) for a trustee: their address when known, else the name Exchange gave."""
        if not isinstance(name, str) or not name:
            return "Unknown", None
        user = self.users.get(name.lower())
        box = self.mailbox(name)
        if user is None and box is not None:
            user = self.users.get((box.get("ExternalDirectoryObjectId") or "").lower())
        label = (user or {}).get("userPrincipalName") or (box or {}).get("PrimarySmtpAddress") or name
        return label, user


def _grants(snap: TenantSnapshot, d: _Directory) -> list[tuple[dict | None, str, str, dict | None, str]]:
    """(mailbox, mailbox label, person label, user, right) for every delegation."""
    out = []
    for p in snap.objects("mailboxes", "mailbox_permission") or []:
        box = d.mailbox(p.get("mailboxId")) or d.mailbox(p.get("mailbox"))
        label, user = d.person(p.get("User"))
        out.append((box, _text(p.get("mailbox")), label, user, "Full access"))
    for p in snap.objects("mailboxes", "send_as") or []:
        box = d.mailbox(p.get("Identity"))
        label, user = d.person(p.get("Trustee"))
        if box is None and "@" not in label:
            continue  # system objects, not mailboxes people use
        out.append((box, (box or {}).get("PrimarySmtpAddress") or _text(p.get("Identity")), label, user, "Send as"))
    for m in snap.objects("mailboxes", "mailbox") or []:
        for who in m.get("GrantSendOnBehalfTo") or []:
            label, user = d.person(who)
            out.append((m, _text(m.get("PrimarySmtpAddress")), label, user, "Send on behalf"))
    return out


def _account(user: dict | None, label: str, domains: set[str]) -> tuple[str, str | None]:
    """(account status words, flag) for the person holding access."""
    if user is None:
        if is_external(label, domains):
            return "Outside the organisation", "high"
        return "Not found in the directory", "low"
    if user.get("userType") == "Guest":
        return "Guest", "high"
    if not user.get("accountEnabled"):
        return "Sign-in blocked", "medium"
    return "Active", None


@report(
    "mailbox-permissions",
    "Mailbox permissions",
    "Who can open, send as or send on behalf of each mailbox, by person and by mailbox.",
    ("mailboxes", "users"),
    group="email",
    order=30,
    keywords="full access send as delegates",
)
def mailbox_permissions(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    boxes = snap.objects("mailboxes", "mailbox")
    if boxes is None:
        report.unavailable = NOT_COLLECTED
        return
    if _no_exchange(snap):
        report.unavailable = "This tenant has no Exchange Online licence."
        return
    domains = tenant_domains(snap) or set()
    d = _Directory(boxes, snap.objects("users", "user") or [])
    grants = _grants(snap, d)

    by_box: dict[str, dict] = {}
    by_person: dict[str, dict] = {}
    for box, box_label, person, user, right in grants:
        b = by_box.setdefault(box_label.lower(), {"label": box_label, "type": _type((box or {}).get(
            "RecipientTypeDetails")), **{r: [] for r in RIGHTS}})
        if person not in b[right]:
            b[right].append(person)
        p = by_person.setdefault(person.lower(), {"label": person, "user": user, **{r: [] for r in RIGHTS}})
        if box_label not in p[right]:
            p[right].append(box_label)

    people = Section("By person", ["Person", "Account", "Mailboxes", *RIGHTS],
                     intro=(f"{_plural(len(by_person), 'person')} can open or send from "
                            f"{_plural(len(by_box), 'mailbox')} other than their own. People with blocked "
                            "sign-in, guests and outside addresses are flagged: remove access nobody needs."),
                     empty="Nobody has access to another mailbox.")
    for p in sorted(by_person.values(), key=lambda p: (-len({m for r in RIGHTS for m in p[r]}), p["label"].lower())):
        status, flag = _account(p["user"], p["label"], domains)
        people.add([p["label"], status, len({m for r in RIGHTS for m in p[r]}), *(p[r] for r in RIGHTS)], flag)

    mailboxes = Section("By mailbox", ["Mailbox", "Type", *RIGHTS],
                        intro="Who can get into each mailbox, and how.",
                        empty="No mailbox is shared with anyone.")
    for b in sorted(by_box.values(), key=lambda b: b["label"].lower()):
        mailboxes.add([b["label"], b["type"], *(b[r] or "" for r in RIGHTS)])

    every = Section("Every permission", ["Mailbox", "Mailbox type", "Person", "Access", "Account"],
                    intro="One row per permission, for filtering in Excel.", empty="No permissions.")
    for box, box_label, person, user, right in sorted(grants, key=lambda g: (g[1].lower(), g[2].lower())):
        status, flag = _account(user, person, domains)
        every.add([box_label, _type((box or {}).get("RecipientTypeDetails")), person, right, status], flag)
    risky = sum(1 for f in people.flags if f in ("high", "medium"))
    report.highlights = [
        Highlight("People with access", len(by_person), "To a mailbox other than their own", rows=Rows("By person")),
        Highlight("Mailboxes others use", len(by_box), "Opened or sent from by someone else", rows=Rows("By mailbox")),
        Highlight("Access to review", risky, "Blocked, guest or outside accounts", "bad" if risky else "ok",
                  Rows("By person", flagged=True)),
    ]
    report.sections = [people, mailboxes, every]


# -- Shared and resource mailboxes --------------------------------------------------------


@report(
    "shared-mailboxes",
    "Shared mailboxes",
    "Shared, room and equipment mailboxes that can be signed in to, "
    "and who uses them.",
    ("mailboxes", "users", "mailbox_usage"),
    group="email",
    order=40,
    keywords="rooms equipment sign-in",
)
def shared_mailboxes(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    boxes = snap.objects("mailboxes", "mailbox")
    if boxes is None:
        report.unavailable = NOT_COLLECTED
        return
    if _no_exchange(snap):
        report.unavailable = "This tenant has no Exchange Online licence."
        return
    users = {u["_id"]: u for u in snap.objects("users", "user") or []}
    d = _Directory(boxes, list(users.values()))
    access: dict[str, dict[str, list[str]]] = {}
    for _box, box_label, person, _, right in _grants(snap, d):
        access.setdefault(box_label.lower(), {}).setdefault(right, []).append(person)
    sizes = {s.get("mailboxId"): s for s in snap.objects("mailbox_usage", "mailbox_size") or []}
    recent = now - timedelta(days=30)

    rows = [m for m in boxes if m.get("RecipientTypeDetails") in RESOURCE_TYPES]
    open_ = 0
    section = Section("Shared and resource mailboxes", ["Mailbox", "Type", "Sign-in allowed", "Last sign-in",
                                                        "Licensed", "Full access", "Send as", "Size"],
                      empty="No shared, room or equipment mailboxes.")
    for m in sorted(rows, key=lambda m: (m.get("RecipientTypeDetails") != "SharedMailbox",
                                         _text(m.get("PrimarySmtpAddress")).lower())):
        user = users.get(m.get("ExternalDirectoryObjectId"))
        enabled = user.get("accountEnabled") if user else None
        last = last_sign_in(user) if user and user.get("signInActivityAvailable") else None
        if enabled:
            open_ += 1
        flag = ("high" if last and last >= recent else "medium") if enabled else None
        who = access.get(_text(m.get("PrimarySmtpAddress")).lower(), {})
        section.add([m.get("PrimarySmtpAddress"), RESOURCE_TYPES[m["RecipientTypeDetails"]],
                     "Unknown" if user is None else enabled,
                     last.date().isoformat() if last else ("Never" if user and user.get(
                         "signInActivityAvailable") else ""),
                     user.get("licensed") if user else "", who.get("Full access") or "", who.get("Send as") or "",
                     _size((sizes.get(m["_id"]) or {}).get("totalBytes"))], flag)
    section.intro = (f"{open_} of {len(rows)} can be signed in to directly. These accounts don't need a password; "
                     "an open one usually has no MFA and nobody watching it. Block sign-in in the Microsoft 365 "
                     "admin centre (Users > Active users > the mailbox > Block sign-in). People keep their access.")
    report.highlights = [
        Highlight("Can be signed in to", f"{open_} of {len(rows)}", "Should be none", "bad" if open_ else "ok",
                  Rows(section.title, {"Sign-in allowed": "Yes"})),
        Highlight("Shared mailboxes", sum(m["RecipientTypeDetails"] == "SharedMailbox" for m in rows),
                  rows=Rows(section.title, {"Type": "Shared"})),
        Highlight("Rooms and equipment", sum(m["RecipientTypeDetails"] != "SharedMailbox" for m in rows),
                  rows=Rows(section.title, {"Type": ["Room", "Equipment"]})),
    ]
    report.sections = [section]


# -- Mailbox size and quota ----------------------------------------------------------------


@report(
    "mailbox-size",
    "Mailbox size",
    "Every mailbox's size against its quota, largest share of quota first.",
    ("mailbox_usage",),
    group="email",
    order=50,
    keywords="quota storage full large",
)
def mailbox_size(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    sizes = snap.objects("mailbox_usage", "mailbox_size")
    if sizes is None:
        report.unavailable = "Mailbox sizes haven't been collected yet. Refresh Mailbox size on the tenant page."
        return
    if _no_exchange(snap):
        report.unavailable = "This tenant has no Exchange Online licence."
        return

    def used(s: dict) -> float | None:
        quota = s.get("sendQuotaBytes")
        return (s.get("totalBytes") or 0) / quota if quota and s.get("totalBytes") is not None else None

    ordered = sorted(sizes, key=lambda s: (-(used(s) or 0), -(s.get("totalBytes") or 0)))
    near = sum(1 for s in sizes if (used(s) or 0) >= 0.9)
    total = sum(s.get("totalBytes") or 0 for s in sizes)
    section = Section("Mailboxes by size", ["Mailbox", "Type", "Size", "Stops sending at", "Used", "Items",
                                            "Recoverable items", "Archive", "Litigation hold"],
                      intro=(f"{_size(total)} across {_plural(len(sizes), 'mailbox')}. "
                             + (f"{near} {'is' if near == 1 else 'are'} over 90% of quota: turn on the archive or "
                                "clear old mail before sending stops." if near else "None is close to its quota.")),
                      empty="No mailboxes.")
    for s in ordered:
        pct = used(s)
        flag = "high" if pct is not None and pct >= 0.98 else ("medium" if pct is not None and pct >= 0.9 else None)
        section.add([s.get("mailbox"), _type(s.get("mailboxType")), _size(s.get("totalBytes")),
                     _size(s.get("sendQuotaBytes")) or "No limit", _percent(pct),
                     s.get("itemCount"), _size(s.get("deletedBytes")), s.get("archive"), s.get("litigationHold")],
                    flag)
    report.highlights = [
        Highlight("Total size", _size(total) or "0 bytes", f"Across {_plural(len(sizes), 'mailbox')}",
                  rows=Rows(section.title)),
        Highlight("Over 90% of quota", near, "Sending stops at 100%", "bad" if near else "ok",
                  Rows(section.title, flagged=True)),
        Highlight("Largest", _size(max((s.get("totalBytes") or 0 for s in sizes), default=0)) or "0 bytes",
                  rows=Rows(section.title)),
    ]
    report.sections = [section]


def _percent(value: float | None) -> str:
    if value is None:
        return ""
    return "Under 1%" if 0 < value < 0.005 else f"{value:.0%}"


# -- Legacy sign-ins and SMTP AUTH ----------------------------------------------------------------


@report(
    "legacy-auth",
    "Legacy sign-ins",
    "Sign-ins with SMTP AUTH, IMAP, POP and other protocols that can't do MFA, "
    "and which mailboxes still allow them.",
    ("legacy_signins", "mailboxes"),
    group="identity",
    order=100,
    keywords="basic auth smtp imap pop old protocols",
)
def legacy_auth(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    status = (snap.objects("legacy_signins", "legacy_signin_status") or [None])[0]
    signins = snap.objects("legacy_signins", "legacy_signin")
    cas = snap.objects("mailboxes", "cas_mailbox")
    if status is None and cas is None:
        report.unavailable = ("Sign-in and mailbox data hasn't been collected yet. Refresh Legacy sign-ins and "
                              "Mailboxes on the tenant page.")
        return
    sections = []
    if status is not None and status.get("available"):
        ok = [s for s in signins or [] if s.get("succeeded")]
        log = Section("Legacy sign-ins in the last 30 days", ["Account", "Client", "Succeeded", "Failed",
                                                             "Last success", "Countries", "IP addresses"],
                      intro=(f"{_plural(len({s.get('user') for s in ok}), 'account')} signed in successfully with "
                             "an old protocol that can't do MFA. Failed attempts are usually password guessing "
                             "that was blocked. Move printers and apps that use SMTP AUTH to a connector or "
                             "Microsoft's high volume email, then turn these protocols off."),
                      empty="No legacy sign-ins in the last 30 days.")
        for s in sorted(signins or [], key=lambda s: (-(s.get("succeeded") or 0), -(s.get("failed") or 0))):
            if s.get("succeeded"):
                flag = "medium" if s.get("client") == "Authenticated SMTP" else "high"
            else:
                flag = "low"
            log.add([s.get("user"), s.get("client"), s.get("succeeded"), s.get("failed"),
                     (s.get("lastSuccess") or "")[:10] or "Never", s.get("countries") or "",
                     s.get("topIps") or ""], flag)
        sections.append(log)

    if cas is not None:
        org_off = (snap.objects("mailboxes", "mail_settings") or [{}])[0].get("SmtpClientAuthenticationDisabled")
        smtp_users = {_text(s.get("user")).lower() for s in signins or []
                      if s.get("client") == "Authenticated SMTP" and s.get("succeeded")}
        boxes = {m["_id"]: m for m in snap.objects("mailboxes", "mailbox") or []}
        allowed = 0
        protocols = Section("What each mailbox allows", ["Mailbox", "SMTP AUTH", "Used SMTP AUTH recently",
                                                         "POP", "IMAP"], empty="No mailboxes.")
        for c in sorted(cas, key=lambda c: _text(c.get("PrimarySmtpAddress")).lower()):
            setting = c.get("SmtpClientAuthenticationDisabled")
            if setting is None:
                smtp_on = org_off is False
                words = "On (organisation default)" if smtp_on else "Off (organisation default)"
            else:
                smtp_on = not setting
                words = "On" if smtp_on else "Off"
            allowed += smtp_on
            upn = _text((boxes.get(c["_id"]) or {}).get("UserPrincipalName") or c.get("PrimarySmtpAddress")).lower()
            protocols.add([c.get("PrimarySmtpAddress"), words, upn in smtp_users if status and status.get(
                "available") else "Unknown", c.get("PopEnabled"), c.get("ImapEnabled")],
                "low" if smtp_on and upn not in smtp_users else None)
        org = ("on" if org_off is False else "off") if org_off is not None else "unknown"
        protocols.intro = (f"SMTP AUTH is {org} for the organisation, and {allowed} of {_plural(len(cas), 'mailbox')} "
                           "allow it. Turn it off for mailboxes that haven't used it recently.")
        sections.append(protocols)
    report.sections = sections
    if status is not None and status.get("available"):
        succeeded = {s.get("user") for s in signins or [] if s.get("succeeded")}
        attempts = sum(s.get("failed") or 0 for s in signins or [])
        report.highlights.append(Highlight("Signed in with an old protocol", len(succeeded), "Accounts, last 30 days",
                                           "bad" if succeeded else "ok", LEGACY_ROWS))
        report.highlights.append(Highlight("Blocked attempts", attempts, "Usually password guessing",
                                           rows=LEGACY_ROWS))
    elif status is not None:
        report.notes.append("Legacy sign-ins need Entra ID P1, which this tenant doesn't have.")
    if cas is not None:
        report.highlights.append(Highlight("SMTP AUTH allowed", f"{allowed} of {len(cas)}", "Mailboxes",
                                           "warn" if allowed else "ok", Rows(
                                               "What each mailbox allows",
                                               {"SMTP AUTH": ["On", "On (organisation default)"]})))


LEGACY_ROWS = Rows("Legacy sign-ins in the last 30 days")
