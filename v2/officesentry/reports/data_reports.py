"""Detail reports Microsoft doesn't give you in one place.

Each builder reads stored snapshots and returns titled tables; the page and
the XLSX export only format them. Nothing here calls Microsoft.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .. import coverage, devices, dnscheck
from ..collectors import REGISTRY
from ..licences import last_sign_in
from ..mailflow import inbox_rule_risk, is_external, mailbox_forwarding, tenant_domains
from ..rules import SHARING_LEVELS, app_risks
from ..snapshot import TenantSnapshot


@dataclass
class Section:
    title: str
    columns: list[str]
    rows: list[list] = field(default_factory=list)
    flags: list[str | None] = field(default_factory=list)  # severity per row, for highlighting
    intro: str = ""
    empty: str = "Nothing to show."

    def add(self, row: list, flag: str | None = None) -> None:
        self.rows.append([_cell(v) for v in row])
        self.flags.append(flag)

    @property
    def flagged(self) -> int:
        return sum(1 for f in self.flags if f in ("critical", "high", "medium"))


@dataclass
class DataReport:
    key: str
    title: str
    description: str
    tenant: dict
    generated_at: datetime
    sections: list[Section] = field(default_factory=list)
    unavailable: str | None = None
    sources: list[tuple[str, str | None]] = field(default_factory=list)  # (collector title, finished_at)


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v) for v in value)
    return value


def _date(value: str | None) -> str:
    return value[:10] if value else ""


# -- Mailboxes ------------------------------------------------------------------


def mailboxes(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    boxes = snap.objects("mailboxes", "mailbox")
    rules = snap.objects("inbox_rules", "inbox_rule")
    if boxes is None and rules is None:
        report.unavailable = ("Mailbox data hasn't been collected yet. "
                              "Refresh Mailboxes and Inbox rules on the tenant page.")
        return
    status = snap.objects("mailboxes", "exchange_status")
    if status and not status[0].get("available"):
        report.unavailable = "This tenant has no Exchange Online licence."
        return
    domains = tenant_domains(snap) or set()

    fwd = Section("Forwarding", ["Mailbox", "Forwards to", "Set by", "Outside the organisation", "Keeps a copy"],
                  intro="Every place mail leaves a mailbox automatically: admin-set forwarding and inbox rules.",
                  empty="No mailbox forwards mail.")
    for m in boxes or []:
        target = mailbox_forwarding(m)
        if target:
            external = is_external(target, domains)
            fwd.add([m.get("PrimarySmtpAddress"), target, "Admin (mailbox setting)", external,
                     m.get("DeliverToMailboxAndForward")], "high" if external else None)
    suspicious = Section("Inbox rules worth checking", ["Mailbox", "Rule", "Enabled", "Why it stands out"],
                         intro="Rules that forward outside, delete, or bury mail. These are the classic signs of a "
                               "compromised mailbox.", empty="No inbox rule looks suspicious.")
    every = Section("All inbox rules", ["Mailbox", "Rule", "Enabled", "Conditions", "Actions"],
                    empty="No inbox rules found.")
    for r in sorted(rules or [], key=lambda r: ((r.get("mailbox") or "").lower(), r.get("sequence") or 0)):
        risk = inbox_rule_risk(r, domains)
        for target in risk.forwards_to:
            external = target in risk.external
            fwd.add([r.get("mailbox"), target, f"Inbox rule \"{r.get('displayName')}\"", external, True],
                    ("high" if r.get("isEnabled", True) else "low") if external else None)
        if risk.severity:
            suspicious.add([r.get("mailbox"), r.get("displayName"), r.get("isEnabled", True),
                            "; ".join(risk.reasons)], risk.severity)
        every.add([r.get("mailbox"), r.get("displayName"), r.get("isEnabled", True),
                   _describe_conditions(r.get("conditions") or {}), _describe_actions(r)], risk.severity)

    sections = [fwd, suspicious]
    if boxes is not None:
        access = Section("Delegated access", ["Mailbox", "Mailbox type", "Who", "Access"],
                         intro="People who can open or send as someone else's mailbox.",
                         empty="Nobody has access to another mailbox.")
        types = {m.get("PrimarySmtpAddress"): _type(m.get("RecipientTypeDetails")) for m in boxes}
        for p in sorted(snap.objects("mailboxes", "mailbox_permission") or [], key=lambda p: p.get("mailbox") or ""):
            access.add([p.get("mailbox"), _type(p.get("mailboxType")), p.get("User"), "Full access"])
        for p in snap.objects("mailboxes", "send_as") or []:
            access.add([p.get("Identity"), "", p.get("Trustee"), "Send as"])
        for m in boxes:
            for who in m.get("GrantSendOnBehalfTo") or []:
                access.add([m.get("PrimarySmtpAddress"), types.get(m.get("PrimarySmtpAddress")), who, "Send on behalf"])

        users = {u["_id"]: u for u in snap.objects("users", "user") or []}
        perms = snap.objects("mailboxes", "mailbox_permission") or []
        shared = Section("Shared mailboxes", ["Mailbox", "Sign-in allowed", "Licensed", "People with full access"],
                         intro="A shared mailbox's account doesn't need to sign in. If it can, it usually has no MFA.",
                         empty="No shared mailboxes.")
        for m in sorted(boxes, key=lambda m: m.get("PrimarySmtpAddress") or ""):
            if m.get("RecipientTypeDetails") != "SharedMailbox":
                continue
            user = users.get(m.get("ExternalDirectoryObjectId")) or {}
            enabled = user.get("accountEnabled")
            shared.add([m.get("PrimarySmtpAddress"), enabled if user else "Unknown", user.get("licensed"),
                        sum(1 for p in perms if p.get("mailboxId") == m["_id"])], "medium" if enabled else None)
        sections += [access, shared]
    report.sections = sections + [every]


def _type(value: str | None) -> str:
    return {"UserMailbox": "User", "SharedMailbox": "Shared", "RoomMailbox": "Room",
            "EquipmentMailbox": "Equipment"}.get(value or "", value or "")


def _describe_conditions(c: dict) -> str:
    parts = []
    for key, label in (("fromAddresses", "from"), ("sentToAddresses", "sent to")):
        addrs = [((a or {}).get("emailAddress") or {}).get("address") for a in c.get(key) or []]
        if addrs:
            parts.append(f"{label} {', '.join(a for a in addrs if a)}")
    for key, label in (("subjectContains", "subject has"), ("bodyContains", "body has"),
                       ("bodyOrSubjectContains", "subject or body has"), ("senderContains", "sender has")):
        if c.get(key):
            parts.append(f"{label} {', '.join(c[key])}")
    return "; ".join(parts) or "All messages"


def _describe_actions(r: dict) -> str:
    a = r.get("actions") or {}
    parts = []
    for key, label in (("forwardTo", "forward to"), ("redirectTo", "redirect to"),
                       ("forwardAsAttachmentTo", "forward as attachment to")):
        addrs = [((x or {}).get("emailAddress") or {}).get("address") for x in a.get(key) or []]
        if addrs:
            parts.append(f"{label} {', '.join(x for x in addrs if x)}")
    if a.get("moveToFolder"):
        parts.append(f"move to {r.get('moveToFolderName') or 'a folder'}")
    if a.get("delete") or a.get("permanentDelete"):
        parts.append("delete")
    if a.get("markAsRead"):
        parts.append("mark as read")
    if a.get("stopProcessingRules"):
        parts.append("stop processing rules")
    return "; ".join(parts) or "Other"


# -- Apps ----------------------------------------------------------------------------


def apps(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    risks = app_risks(snap)
    if risks is None:
        report.unavailable = "App data hasn't been collected yet. Refresh Apps and consents on the tenant page."
        return
    third = Section("Third-party apps with access", ["App", "Publisher", "Application permissions",
                                                     "Consented for everyone", "Consented by users", "Risk"],
                    intro="Apps from outside Microsoft that hold permissions in this tenant. Application permissions "
                          "work without anyone signed in.", empty="No third-party app holds permissions.")
    for a in risks:
        publisher = a["publisher"] + (" (verified)" if a["verified"] else (" (unverified)" if a["publisher"]
                                                                           else "Unverified"))
        users = f"{a['users']} user(s): {', '.join(a['user_scopes'])}" if a["user_scopes"] else ""
        third.add([a["name"], publisher, a["app_permissions"], a["tenant_scopes"], users,
                   ", ".join(a["risky"]) or "Low"], a["severity"])

    regs = Section("App registrations and their credentials", ["App", "Created", "Credentials", "Next expiry",
                                                                "Status"],
                   intro="Apps registered in this tenant. Expired secrets usually mean an integration has stopped "
                         "working, or the app is no longer used.", empty="No app registrations.")
    soon = now.timestamp() + 30 * 86400
    registrations = snap.objects("app_consents", "application") or []
    for a in sorted(registrations, key=lambda a: (a.get("displayName") or "").lower()):
        creds = a.get("credentials") or []
        ends = sorted(c["endDateTime"] for c in creds if c.get("endDateTime"))
        future = [e for e in ends if _ts(e) >= now.timestamp()]
        if not creds:
            status, flag = "No credentials", None
        elif not future:
            status, flag = "All expired", "low"
        elif _ts(future[-1]) < soon:
            status, flag = "Expiring soon", "medium"
        else:
            status, flag = "OK", None
        kinds = ", ".join(f"{sum(c['type'] == k for c in creds)} {k}(s)" for k in ("secret", "certificate")
                          if any(c["type"] == k for c in creds))
        regs.add([a.get("displayName"), _date(a.get("createdDateTime")), kinds or "None",
                  _date(future[0]) if future else (_date(ends[-1]) + " (expired)" if ends else ""), status], flag)
    report.sections = [third, regs]


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


# -- Conditional Access coverage -------------------------------------------------------


def conditional_access(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    defaults = snap.objects("conditional_access", "security_defaults")
    d = coverage.directory(snap)
    cov = coverage.user_coverage(snap)
    if defaults is None or d is None or cov is None:
        report.unavailable = "Conditional Access data hasn't been collected yet."
        return
    policies = snap.objects("conditional_access", "ca_policy") or []
    if defaults and defaults[0].get("isEnabled"):
        report.sections.append(Section("Security Defaults", ["Setting", "Value"], rows=[[
            "Security Defaults", "On: MFA registration and sign-in prompts apply to everyone"]], flags=[None]))
    order = {"enabled": 0, "enabledForReportingButNotEnforced": 1, "disabled": 2}
    pol = Section("Policies", ["Policy", "State", "Applies to", "Except", "Apps", "Conditions", "Requires"],
                  intro="Every policy in plain words, enforced ones first.",
                  empty="No Conditional Access policies." + (" The tenant has no Entra ID P1."
                                                              if snap.capabilities.get("entra_p1") is False else ""))
    for p in sorted(policies, key=lambda p: (order.get(p.get("state"), 3), (p.get("displayName") or "").lower())):
        row = coverage.describe(p, d)
        pol.add([row.name, row.state, row.users, row.excluded, row.apps, row.conditions, row.grant])
    report.sections.append(pol)

    covered = sum(c.mfa for c in cov)
    sd = bool(defaults and defaults[0].get("isEnabled"))
    accounts = Section("Coverage by account", ["Account", "Type", "Admin roles", "MFA required by",
                                               "Legacy sign-in blocked", "Excluded from"],
                       intro=(f"{covered} of {len(cov)} enabled account(s) are covered by an enforced policy that "
                              "requires MFA for all apps." + (" Security Defaults also apply." if sd else "")),
                       empty="No enabled accounts.")
    for c in cov:
        if c.mfa or sd:
            flag = None
        else:
            flag = "high" if c.roles else ("low" if c.user_type == "Guest" else "medium")
        accounts.add([c.label, c.user_type, c.roles, c.mfa_policies or ("Security Defaults" if sd else "Nothing"),
                      c.legacy_blocked or sd, c.excluded_from], flag)
    report.sections.append(accounts)


# -- Guests and sharing -----------------------------------------------------------


INVITES = {"none": "Nobody", "adminsAndGuestInviters": "Admins and Guest Inviters",
           "adminsGuestInvitersAndAllMembers": "Admins and members", "everyone": "Everyone, including guests"}


def guests(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    users = snap.objects("users", "user")
    policy = (snap.objects("tenant_policies", "authorization_policy") or [None])[0]
    sharepoint = (snap.objects("tenant_policies", "sharepoint_settings") or [None])[0]
    if users is None and policy is None:
        report.unavailable = "User and sharing data hasn't been collected yet."
        return
    settings = Section("External access settings", ["Setting", "Value"])
    if policy:
        settings.add(["Who can invite guests", INVITES.get(policy.get("allowInvitesFrom"),
                                                             policy.get("allowInvitesFrom"))],
                     "low" if policy.get("allowInvitesFrom") == "everyone" else None)
        grants = policy.get("permissionGrantPolicies") or []
        legacy = any(p.endswith("microsoft-user-default-legacy") for p in grants)
        settings.add(["User consent to apps", "Any app" if legacy else (
            "Verified publishers, low-risk permissions" if grants else "Off")], "medium" if legacy else None)
        settings.add(["Users can register apps", policy.get("allowedToCreateApps")])
    if sharepoint:
        level = sharepoint.get("sharingCapability")
        settings.add(["SharePoint and OneDrive sharing", SHARING_LEVELS.get(level, level)],
                     "medium" if level == "externalUserAndGuestSharing" else None)
        mode = sharepoint.get("sharingDomainRestrictionMode")
        if mode == "allowList":
            settings.add(["Sharing limited to domains", sharepoint.get("sharingAllowedDomainList") or []])
        elif mode == "blockList":
            settings.add(["Sharing blocked for domains", sharepoint.get("sharingBlockedDomainList") or []])
        settings.add(["Guests can reshare", sharepoint.get("isResharingByExternalUsersEnabled")])
    sections = [settings]
    if users is not None:
        activity = any(u.get("signInActivityAvailable") for u in users)
        guest = Section("Guest accounts", ["Guest", "Invited", "Invitation", "Last sign-in", "Sign-in allowed"],
                        intro="Every external account, oldest activity first." + (
                            "" if activity else " Sign-in dates need Entra ID P1."), empty="No guest accounts.")
        rows = [u for u in users if u.get("userType") == "Guest"]
        rows.sort(key=lambda u: (last_sign_in(u) or datetime.min.replace(tzinfo=UTC), u.get("createdDateTime") or ""))
        cutoff = now.timestamp() - 90 * 86400
        for u in rows:
            last = last_sign_in(u)
            pending = u.get("externalUserState") == "PendingAcceptance"
            stale = activity and u.get("accountEnabled") and (last is None or last.timestamp() < cutoff)
            guest.add([u.get("mail") or u.get("userPrincipalName"), _date(u.get("createdDateTime")),
                       "Not accepted" if pending else "Accepted",
                       last.date().isoformat() if last else ("Never" if activity else "Unknown"),
                       u.get("accountEnabled")], "low" if stale or pending else None)
        sections.append(guest)
    report.sections = sections


# -- Email domains --------------------------------------------------------------------


def domains(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    rows = snap.objects("domain_health", "domain")
    if not rows:
        report.unavailable = "Domain checks haven't run yet, or the tenant has no custom domains."
        return
    overview = Section("Domains", ["Domain", "Receives mail via", "SPF", "DMARC", "DKIM", "What to fix"],
                       intro="SPF, DMARC and DKIM stop other people sending email as the client's domains.")
    records = Section("Published records", ["Domain", "SPF record", "DNS lookups", "DMARC record", "MX"])
    for row in sorted(rows, key=lambda r: (not r.get("mx"), r["domain"])):
        h = dnscheck.DomainHealth.from_dict(row)
        found = dnscheck.issues(h)
        words = dnscheck.summary(h)
        via = "Microsoft 365" if h.microsoft_365 else (h.mx[0] if h.mx else "Doesn't receive mail")
        overview.add([h.domain, via, words["SPF"], words["DMARC"], words["DKIM"],
                      "; ".join(i.detail for i in found) or "Nothing"], found[0].severity if found else None)
        records.add([h.domain, h.spf or "None", h.spf_lookups if h.spf else "", h.dmarc or "None", h.mx])
    report.sections = [overview, records]


# -- Devices -----------------------------------------------------------------------------

COMPLIANCE = {"compliant": "Compliant", "noncompliant": "Not compliant", "inGracePeriod": "Grace period",
              "conflict": "Conflict", "error": "Error", "unknown": "Unknown", "configManager": "Config Manager"}
JOIN_TYPES = {"AzureAd": "Entra joined", "ServerAd": "Hybrid joined", "Workplace": "Registered (personal)"}
SUPPORT = {"supported": "Supported", "ending": "Ending soon", "pro_ended": "Ended (Home/Pro)",
           "ended": "Ended", "unknown": "Unknown"}


def _device_issues(d: dict, now: datetime) -> tuple[list[str], str | None]:
    issues, flag = [], None
    rank = {"high": 0, "medium": 1, "low": 2}

    def add(text: str, severity: str) -> None:
        nonlocal flag
        issues.append(text)
        if flag is None or rank[severity] < rank[flag]:
            flag = severity

    if str(d.get("jailBroken")).lower() == "true":
        add("Jailbroken or rooted", "high")
    if d.get("complianceState") == "noncompliant":
        add("Not compliant", "medium")
    if devices.is_computer(d) and d.get("isEncrypted") is False:
        add("Not encrypted", "high")
    if devices.is_windows(d):
        support = devices.windows_support(d.get("osVersion"), now.date())
        if support.status in ("ended", "pro_ended", "ending"):
            add(support.detail.rstrip("."), {"ended": "high", "pro_ended": "medium", "ending": "low"}[support.status])
    days = devices.days_since(d.get("lastSyncDateTime"), now)
    if days is not None and days > devices.NO_SYNC_DAYS:
        add(f"No check-in for {days} days", "low")
    return issues, flag


def _seen(value: str | None, now: datetime) -> str:
    days = devices.days_since(value, now)
    if days is None:
        return "Never"
    return "Today" if days == 0 else f"{_date(value)} ({days}d)"


def device_report(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    entra = snap.objects("devices", "entra_device")
    if entra is None:
        report.unavailable = "Device data hasn't been collected yet. Refresh Devices on the tenant page."
        return
    status = snap.objects("devices", "intune_status") or []
    intune = bool(status and status[0].get("available"))
    managed = (snap.objects("devices", "managed_device") or []) if intune else []

    # Operating systems: Intune's view when there is one, otherwise devices active in Entra.
    if managed:
        population, version_key = managed, "osVersion"
    else:
        population = [d for d in entra if d.get("accountEnabled") and (devices.days_since(
            d.get("approximateLastSignInDateTime"), now) or 0) <= devices.STALE_DAYS]
        version_key = "operatingSystemVersion"
    counts: dict[tuple[str, str], int] = {}
    for d in population:
        if devices.is_windows(d):
            support = devices.windows_support(d.get(version_key), now.date())
            key = (support.release, support.status)
        else:
            key = (d.get("operatingSystem") or "Unknown", "")
        counts[key] = counts.get(key, 0) + 1
    systems = Section("Operating systems", ["Operating system", "Devices", "Security updates"],
                      intro=("Devices enrolled in Intune." if managed else
                             f"Devices that signed in to Entra ID in the last {devices.STALE_DAYS} days."),
                      empty="No devices.")
    flags = {"ended": "high", "pro_ended": "medium", "ending": "low"}
    for (name, state), n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][0])):
        systems.add([name, n, SUPPORT.get(state, "")], flags.get(state))
    sections = [systems]

    if intune:
        attention = Section("Needs attention", ["Device", "User", "Operating system", "Problems"],
                            intro="Managed devices that aren't compliant, aren't encrypted, run an unsupported "
                                  "Windows release or have stopped checking in.",
                            empty="Every managed device is compliant, encrypted, supported and checking in.")
        every = Section("Managed devices", ["Device", "User", "Owner", "Operating system", "Compliance",
                                            "Encrypted", "Last check-in", "Enrolled", "Model"],
                        empty="No devices are enrolled in Intune.")
        for d in sorted(managed, key=lambda d: (d.get("deviceName") or "").lower()):
            issues, flag = _device_issues(d, now)
            if issues:
                attention.add([d.get("deviceName"), d.get("userPrincipalName"), devices.os_label(d),
                               "; ".join(issues)], flag)
            every.add([d.get("deviceName"), d.get("userPrincipalName"),
                       (d.get("managedDeviceOwnerType") or "").capitalize(), devices.os_label(d),
                       COMPLIANCE.get(d.get("complianceState"), d.get("complianceState")),
                       d.get("isEncrypted") if devices.is_computer(d) else "",
                       _seen(d.get("lastSyncDateTime"), now), _date(d.get("enrolledDateTime")),
                       " ".join(p for p in (d.get("manufacturer"), d.get("model")) if p)], flag)
        attention.rows, attention.flags = _by_flag(attention)
        sections += [attention, every]

    records = Section("Entra ID device records", ["Device", "Operating system", "Join type", "In Intune",
                                                  "Last seen", "Registered", "Enabled"],
                      intro="Every device known to Entra ID, least recently seen first. "
                            + ("" if intune else "The tenant has no Intune, so these are the only device data."),
                      empty="No devices are registered in Entra ID.")
    for d in sorted(entra, key=lambda d: d.get("approximateLastSignInDateTime") or ""):
        days = devices.days_since(d.get("approximateLastSignInDateTime"), now)
        stale = d.get("accountEnabled") and days is not None and days > devices.STALE_DAYS
        records.add([d.get("displayName"), devices.os_label(d),
                     JOIN_TYPES.get(d.get("trustType"), d.get("trustType") or "Unknown"), d.get("isManaged"),
                     _seen(d.get("approximateLastSignInDateTime"), now), _date(d.get("registrationDateTime")),
                     d.get("accountEnabled")], "low" if stale else None)
    sections.append(records)
    report.sections = sections


def _by_flag(section: Section) -> tuple[list, list]:
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, None: 4}
    pairs = sorted(zip(section.rows, section.flags, strict=True), key=lambda p: order[p[1]])
    return [p[0] for p in pairs], [p[1] for p in pairs]


# -- Registry ----------------------------------------------------------------------------

Builder = Callable[[TenantSnapshot, datetime, DataReport], None]

REPORTS: dict[str, tuple[str, str, tuple[str, ...], Builder]] = {
    "mailboxes": ("Mailbox access and forwarding",
                  "Forwarding, suspicious inbox rules, delegated access and shared mailboxes.",
                  ("mailboxes", "inbox_rules", "users"), mailboxes),
    "apps": ("Apps and consents", "Third-party apps with access to the tenant, and app credential expiry.",
             ("app_consents",), apps),
    "conditional-access": ("Conditional Access coverage",
                           "Every policy in plain words, and which accounts each one actually covers.",
                           ("conditional_access", "admin_roles", "users"), conditional_access),
    "domains": ("Email domains", "SPF, DMARC and DKIM for each of the tenant's domains, from public DNS.",
                ("domain_health",), domains),
    "guests": ("Guests and sharing", "Who can invite and share externally, and every guest account.",
               ("tenant_policies", "users"), guests),
    "devices": ("Devices", "Intune compliance, encryption and Windows support, and every Entra ID device record.",
                ("devices",), device_report),
}


def build(conn, tenant, key: str, now: datetime | None = None) -> DataReport:
    title, description, collectors, builder = REPORTS[key]
    now = now or datetime.now(UTC)
    snap = TenantSnapshot(conn, tenant)
    report = DataReport(key, title, description, dict(tenant), now)
    report.sources = [(REGISTRY[c].title, (snap.runs.get(c) or {"finished_at": None})["finished_at"])
                      for c in collectors]
    builder(snap, now, report)
    return report


def to_xlsx(report: DataReport) -> bytes:
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    used = set()
    for section in report.sections or [Section("No data", ["Note"], rows=[[report.unavailable or ""]])]:
        name = section.title[:31]
        while name in used:
            name = name[:29] + str(len(used))
        used.add(name)
        ws = wb.create_sheet(name)
        ws.append(section.columns + (["Flag"] if any(section.flags) else []))
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for row, flag in zip(section.rows, section.flags or [None] * len(section.rows), strict=False):
            ws.append(list(row) + ([flag or ""] if any(section.flags) else []))
        ws.freeze_panes = "A2"
        for i in range(1, ws.max_column + 1):
            width = max((len(str(c.value or "")) for c in ws[get_column_letter(i)]), default=10)
            ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 10), 60)
    out = BytesIO()
    wb.save(out)
    return out.getvalue()
