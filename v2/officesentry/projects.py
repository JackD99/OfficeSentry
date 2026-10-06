"""Project recommendations: the pieces of work a client's tenant shows they need, for account managers.

Each project in ``CATALOG`` names the checks that call for it (``checks``) and,
where a check can't say it, signals read from the snapshot (``signals``), such
as "Entra ID P1 but no Conditional Access policy switched on". A tenant is
recommended a project when any of its checks fail with findings nobody has
accepted as a risk, or a signal fires. Every check belongs to at most one
project, so one finding never sells two projects.

Recommendations are worked out from stored data whenever they're shown, so a
project drops off once the tenant is fixed. What the account manager did with
one (proposed, agreed, declined, with a note) is kept in ``project_status``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .db import utcnow
from .findings import finding_key, lifecycle_map
from .rules import SEVERITY_RANK, RuleResult, evaluate
from .snapshot import TenantSnapshot
from .text import plural

# What an account manager has done with a recommendation. No row means "Suggested".
STATUSES = {"suggested": "Suggested", "proposed": "Proposed", "agreed": "Agreed", "declined": "Declined"}


@dataclass
class Reason:
    """Why a project is recommended: a failing check, or a signal from the tenant's data."""
    title: str
    summary: str
    severity: str
    check: str = ""  # the check's key, to link its findings
    count: int = 0  # open findings not accepted as a risk


Signal = Callable[[TenantSnapshot], "Reason | None"]


@dataclass
class Project:
    key: str
    title: str
    pitch: str  # why it matters, in words a client understands
    covers: list[str]  # what the project delivers
    checks: list[str] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    size: tuple[str, str] | None = None  # (metric key, noun): how big the job is, "42 users"


# -- Signals --------------------------------------------------------------------------


def _no_ca_policies(snap: TenantSnapshot) -> Reason | None:
    """Licensed for Conditional Access, with nothing switched on (and no Security Defaults either)."""
    defaults = snap.objects("conditional_access", "security_defaults")
    if snap.capabilities.get("entra_p1") is not True or defaults is None:
        return None
    if defaults and defaults[0].get("isEnabled"):
        return None
    if snap.metric("ca.policies_enabled"):
        return None
    return Reason("No Conditional Access policies", "The tenant has Entra ID P1 but no Conditional Access policy "
                  "is switched on, and Security Defaults are off", "high")


def _missing(capability: str, title: str, summary: str, severity: str) -> Signal:
    def signal(snap: TenantSnapshot) -> Reason | None:
        # False only when the tenant's subscriptions were read and it isn't there; unknown isn't missing.
        return Reason(title, summary, severity) if snap.capabilities.get(capability) is False else None
    return signal


# -- The catalog ----------------------------------------------------------------------

CATALOG: list[Project] = [
    Project(
        "identity", "Identity and Conditional Access",
        "Most break-ins start with a stolen password. Requiring a second factor everywhere, and blocking the old "
        "sign-in methods that can't ask for one, stops nearly all of them.",
        ["Conditional Access policies: MFA for everyone, legacy sign-in blocked, break-glass accounts",
         "MFA registration drive, moving people off text and call codes to the Authenticator app",
         "Risky sign-in and risky user handling",
         "Report-only rollout first, so nobody is locked out"],
        checks=["mfa_enforced", "legacy_auth_blocked", "users_mfa_registered", "phone_only_mfa", "risky_sign_ins",
                "risky_users"],
        signals=[_no_ca_policies], size=("users.enabled", "user")),
    Project(
        "admin_hardening", "Admin account hardening",
        "Admin accounts can change everything in Microsoft 365. Fewer of them, protected by phishing-resistant "
        "sign-in, makes a takeover far harder and far less damaging.",
        ["The right number of Global Administrators, with least-privilege roles for everyone else",
         "Phishing-resistant sign-in (passkeys or security keys) for every admin",
         "Cloud-only admin accounts, separate from day-to-day accounts",
         "A review of apps and guests holding admin roles"],
        checks=["admin_mfa", "global_admin_count", "guest_admins", "service_principal_admins", "admins_ca_mfa",
                "admin_phishing_resistant", "cloud_only_admins"],
        size=("admins.privileged_principals", "admin")),
    Project(
        "licence_upgrade", "Security licence upgrade",
        "The current licences leave out security tools the other projects rely on: Conditional Access, device "
        "management or protection from malicious links and attachments. Microsoft 365 Business Premium "
        "includes all three; an add-on can cover a single gap.",
        ["Licence options and cost comparison (Business Premium or add-ons)",
         "Switch-over with no interruption to email or files",
         "Turning on the new protections once they're licensed"],
        signals=[
            _missing("entra_p1", "No Entra ID P1", "Without it there's no Conditional Access, so sign-in rules "
                     "can't be set", "high"),
            _missing("intune", "No Intune", "Laptops and phones can't be managed, encrypted or wiped remotely",
                     "medium"),
            _missing("defender_o365_p1", "No Defender for Office 365", "Links and attachments in email aren't "
                     "checked when they're opened", "medium")],
        size=("users.licensed", "licensed user")),
    Project(
        "device_management", "Device management with Intune",
        "Company data lives on laptops and phones. Managing them means they're encrypted, kept up to date and "
        "can be wiped if lost, and unmanaged devices can be kept away from company data.",
        ["Intune enrolment for every company computer (Autopilot for new ones)",
         "Compliance policies: encryption, updates, antivirus, screen lock",
         "BitLocker with recovery keys stored in Entra ID",
         "Clean-up of old devices"],
        checks=["intune_enrolled", "device_compliance", "device_encryption", "device_checkin", "stale_devices"],
        size=("devices.entra_total", "device")),
    Project(
        "windows_upgrade", "Windows upgrade and hardware refresh",
        "Computers on a Windows version that no longer gets security updates stay open to every flaw found from "
        "now on. Upgrading, or replacing machines that can't upgrade, closes that gap.",
        ["Which computers can upgrade in place and which need replacing",
         "Upgrades scheduled around the working day",
         "Replacement hardware quoted and set up"],
        checks=["supported_windows"], size=("devices.unsupported_windows", "computer")),
    Project(
        "email_security", "Email security",
        "Email is how most attacks arrive and how fraud gets paid. Locking down the domain stops others "
        "pretending to be the business, and closing forwarding and old protocols stops mail leaking out.",
        ["SPF, DKIM and DMARC set up and enforced for every domain",
         "Encrypted delivery (MTA-STS and TLS reporting)",
         "External forwarding and suspicious inbox rules removed and blocked",
         "SMTP AUTH turned off, mailbox auditing on, shared mailboxes locked"],
        checks=["spf_record", "dmarc_policy", "dkim_enabled", "mta_sts", "tls_rpt", "smtp_auth", "mailbox_auditing",
                "shared_mailbox_signin", "external_forwarding", "auto_forward_policy", "suspicious_inbox_rules"],
        size=("mailboxes.total", "mailbox")),
    Project(
        "app_governance", "App and consent governance",
        "Third-party apps can be granted access to mail and files without IT knowing. Reviewing what's there "
        "and putting an approval step in place stops data quietly leaving through an app.",
        ["Review of every app with access, removing what isn't needed",
         "Admin approval workflow for new apps",
         "Users stopped from registering apps or creating tenants",
         "Expiring app secrets and certificates renewed"],
        checks=["risky_app_permissions", "user_consent", "app_credentials", "admin_consent_workflow",
                "app_registration", "tenant_creation"],
        size=("apps.third_party", "third-party app")),
    Project(
        "external_sharing", "Guest access and external sharing",
        "Files shared with anyone who has the link, and guests nobody remembers inviting, are an easy way for "
        "data to end up in the wrong hands.",
        ["Sharing settings: no anonymous links, sensible defaults",
         "Existing anyone links reviewed and removed",
         "Guest invitation rules and a review of current guests",
         "Old, unaccepted invitations cleared out"],
        checks=["sharepoint_sharing", "anyone_links", "guest_invites", "pending_guests", "guest_access_level",
                "email_verified_join"],
        size=("users.guests", "guest")),
    Project(
        "teams_governance", "Teams and groups governance",
        "Without rules, Teams and groups multiply, lose their owners and keep data nobody is looking after.",
        ["Who can create teams and groups",
         "An owner for every team and group",
         "Expiry for groups nobody uses"],
        checks=["ownerless_groups", "group_creation", "group_expiration"], size=("groups.total", "team or group")),
    Project(
        "storage", "SharePoint storage and archiving",
        "When SharePoint storage runs out, people can't save files. Clearing old versions and archiving is "
        "usually cheaper than buying more space.",
        ["Where the space has gone, site by site",
         "Version history limits and clean-up",
         "Archiving, or extra storage where it's needed"],
        checks=["sharepoint_storage_space"]),
    Project(
        "licence_cleanup", "Licence and account clean-up",
        "Licences on accounts nobody uses, and paid licences sitting unassigned, cost money every month and "
        "leave old accounts open to misuse.",
        ["Leavers' accounts closed and their licences reclaimed",
         "Unused paid licences cut at renewal",
         "Licence assignment errors fixed and renewals checked"],
        checks=["stale_users", "licensed_disabled", "unassigned_licences", "licence_assignment_errors",
                "subscriptions_lapsing"],
        size=("users.licensed", "licensed user")),
    Project(
        "hybrid_identity", "Directory sync health",
        "When sync from the office server stops, leavers keep working accounts and password changes don't "
        "reach Microsoft 365.",
        ["Entra Connect repaired or upgraded",
         "Password hash sync on, as a fallback and for leaked-password detection",
         "Plan for moving to cloud-only accounts where the server is no longer needed"],
        checks=["directory_sync_running", "password_hash_sync"], size=("users.members", "user")),
]

PROJECTS = {p.key: p for p in CATALOG}


@dataclass
class Recommendation:
    project: Project
    reasons: list[Reason]
    size: str = ""  # "42 users"
    status: str = "suggested"
    note: str = ""
    updated_by: str | None = None
    updated_at: str | None = None

    @property
    def severity(self) -> str:
        return min((r.severity for r in self.reasons), key=SEVERITY_RANK.__getitem__)

    @property
    def weight(self) -> tuple:
        """Sort key: worst severity first, then the most reasons."""
        return SEVERITY_RANK[self.severity], -len(self.reasons)

    @property
    def status_label(self) -> str:
        return STATUSES.get(self.status, self.status)


def _reasons(project: Project, results: dict[str, RuleResult], life: dict, snap: TenantSnapshot) -> list[Reason]:
    # Signals first: at the same severity, "no Conditional Access at all" says more than the checks it causes.
    out = [reason for signal in project.signals if (reason := signal(snap)) is not None]
    for key in project.checks:
        r = results.get(key)
        if r is None or r.status != "fail":
            continue
        active = [f for f in r.findings if not ((lc := life.get((r.key, finding_key(f)))) and lc.accepted)]
        if r.findings and not active:
            continue  # every finding accepted as a risk
        severity = min((f.severity for f in active), key=SEVERITY_RANK.__getitem__, default="medium")
        out.append(Reason(r.title, r.summary, severity, r.key, len(active)))
    out.sort(key=lambda x: SEVERITY_RANK[x.severity])
    return out


def _size(project: Project, snap: TenantSnapshot) -> str:
    if project.size is None:
        return ""
    value = snap.metric(project.size[0])
    return plural(int(value), project.size[1]) if value else ""


def statuses(conn, tenant_id: int) -> dict[str, dict]:
    return {row["project_key"]: dict(row) for row in conn.execute(
        "SELECT * FROM project_status WHERE tenant_id = ?", (tenant_id,))}


def recommend(conn, tenant, now: datetime | None = None, snap: TenantSnapshot | None = None) -> list[Recommendation]:
    """The projects this tenant's latest data calls for, worst first, with what the account manager did."""
    now = now or datetime.now(UTC)
    snap = snap or TenantSnapshot(conn, tenant)
    if not snap.runs:
        return []
    results = {r.key: r for r in evaluate(snap, now)}
    life = lifecycle_map(conn, tenant["id"], now)
    saved = statuses(conn, tenant["id"])
    out = []
    for project in CATALOG:
        reasons = _reasons(project, results, life, snap)
        if not reasons:
            continue
        rec = Recommendation(project, reasons, _size(project, snap))
        if row := saved.get(project.key):
            rec.status, rec.note = row["status"], row["note"] or ""
            rec.updated_by, rec.updated_at = row["updated_by"], row["updated_at"]
        out.append(rec)
    out.sort(key=lambda r: r.weight)
    return out


def set_status(conn, tenant_id: int, project_key: str, status: str, note: str, username: str) -> None:
    if project_key not in PROJECTS or status not in STATUSES:
        raise ValueError(f"unknown project or status: {project_key} {status}")
    if status == "suggested" and not note:
        conn.execute("DELETE FROM project_status WHERE tenant_id = ? AND project_key = ?", (tenant_id, project_key))
        return
    conn.execute(
        "INSERT INTO project_status (tenant_id, project_key, status, note, updated_by, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(tenant_id, project_key) DO UPDATE SET status = excluded.status,"
        " note = excluded.note, updated_by = excluded.updated_by, updated_at = excluded.updated_at",
        (tenant_id, project_key, status, note, username, utcnow()))


# -- Every tenant --------------------------------------------------------------------


@dataclass
class TenantRow:
    tenant: dict
    recommendations: list[Recommendation] = field(default_factory=list)
    note: str = ""  # why there are none to show: no data yet, or it couldn't be worked out


@dataclass
class Pipeline:
    rows: list[TenantRow]
    generated_at: datetime

    def items(self, project: str = "", declined: bool = False) -> list[tuple[dict, Recommendation]]:
        """(tenant, recommendation) pairs, worst first; declined ones only when asked for."""
        out = [(row.tenant, rec) for row in self.rows for rec in row.recommendations
               if (not project or rec.project.key == project) and (rec.status == "declined") == declined]
        out.sort(key=lambda x: (*x[1].weight, x[0]["name"].lower()))
        return out

    def count(self, project: str = "", status: str | None = None) -> int:
        return sum(1 for row in self.rows for rec in row.recommendations
                   if (not project or rec.project.key == project)
                   and (rec.status == status if status else rec.status != "declined"))

    @property
    def tenants_with(self) -> int:
        return sum(1 for row in self.rows if any(r.status != "declined" for r in row.recommendations))

    @property
    def without_data(self) -> int:
        return sum(1 for row in self.rows if row.note)


def pipeline(conn, tenants, now: datetime | None = None) -> Pipeline:
    now = now or datetime.now(UTC)
    rows = []
    for t in tenants:
        row = TenantRow(dict(t))
        rows.append(row)
        try:
            snap = TenantSnapshot(conn, t)
            if not snap.runs:
                row.note = "No data collected yet."
                continue
            row.recommendations = recommend(conn, t, now, snap)
        except Exception as exc:  # one broken tenant mustn't blank the page
            logging.getLogger(__name__).exception("Project recommendations failed for tenant %s", t["id"])
            row.note = f"Couldn't be worked out: {type(exc).__name__}"
    return Pipeline(rows, now)


def to_table(items: list[tuple[dict, Recommendation]]):
    from .reports.tables import Table

    columns = ["Tenant", "Tenant ID", "Project", "Priority", "Why", "Size", "Status", "Note", "Updated by",
              "Updated (UTC)"]
    rows = [[t["name"], t["tenant_ref"], rec.project.title, rec.severity.capitalize(),
             "; ".join(r.summary for r in rec.reasons), rec.size, rec.status_label, rec.note,
             rec.updated_by or "", rec.updated_at or ""] for t, rec in items]
    return Table("Recommended projects", columns, rows)
