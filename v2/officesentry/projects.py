"""Project recommendations: the pieces of work a client's tenant shows they need, for account managers.

A project is a substantial piece of work, half a day or more (``MIN_HOURS``), such as a full identity and
Conditional Access rollout: something an account manager proposes and quotes. Anything smaller, like
registering MFA for one admin, is a **quick fix** the team just does.

Each project in ``CATALOG`` names the checks that call for it (``checks``, each with a rough ``Effort``) and,
where a check can't say it, signals read from the snapshot (``signals``), such as "Entra ID P1 but no
Conditional Access policy switched on". Every check belongs to at most one project, so one finding never
sells two projects. For each tenant (``assess``):

- a project's reasons are its failing checks (leaving out findings accepted as a risk) and signals, each with
  an estimate in hours from how many things it has to fix;
- a critical reason that is small on its own is a quick fix, so it's done now rather than waiting on a sale;
- the rest is a project when it adds up to ``MIN_HOURS`` with ``OVERHEAD`` (planning, testing, handover),
  otherwise each reason is a quick fix;
- failing checks no project covers are quick fixes too, so everything failing has a home.

Recommendations are worked out from stored data whenever they're shown, so a project drops off once the
tenant is fixed. What the account manager did with one (proposed, agreed, declined, with a note) is kept in
``project_status``. Estimates are a rough guide for staff and never reach the client's proposal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .db import utcnow
from .findings import finding_key, lifecycle_map
from .rules import SEVERITY_RANK, Finding, RuleResult, evaluate
from .snapshot import TenantSnapshot
from .text import WHOLE_ORGANISATION, plural

# What an account manager has done with a recommendation. No row means "Suggested".
STATUSES = {"suggested": "Suggested", "proposed": "Proposed", "agreed": "Agreed", "declined": "Declined"}

MIN_HOURS = 4  # the smallest piece of work worth proposing as a project
OVERHEAD = 1  # hours a project adds on top of its fixes: planning, testing, telling people, handover
DAY = 8


@dataclass(frozen=True)
class Effort:
    """Rough hours to fix a failing check: ``fixed`` once, plus ``each`` per finding (``minor`` for low and
    info ones, when set; ``whole`` for a finding about the whole organisation, such as a tenant setting),
    capped at ``most``."""
    fixed: float = 0
    each: float = 0
    whole: float | None = None
    minor: float | None = None
    most: float | None = None

    def hours(self, findings: list[Finding]) -> float:
        total = self.fixed
        for f in findings:
            if f.subject in WHOLE_ORGANISATION and self.whole is not None:
                total += self.whole
            elif f.severity in ("low", "info") and self.minor is not None:
                total += self.minor
            else:
                total += self.each
        if not findings:  # failing with nothing listed: one of whatever it is
            total += self.whole if self.whole is not None else self.each
        return min(total, self.most) if self.most is not None else total


# A failing check that no project covers.
OTHER_EFFORT = Effort(fixed=0.25, each=0.25, most=3)


@dataclass
class Reason:
    """Why a project is recommended, or a quick fix: a failing check, or a signal from the tenant's data."""
    title: str
    summary: str
    severity: str
    check: str = ""  # the check's key, to link its findings
    count: int = 0  # open findings not accepted as a risk
    hours: float = 0  # rough effort to fix
    action: str = ""  # the first thing to do, for quick fixes
    signal: str = ""  # the signal's key, when it isn't a check
    project: str = ""  # the catalog project it belongs to, if any

    @property
    def key(self) -> str:
        """Names it in the month-end review's suggestions: the check's key, or ``signal:<key>``."""
        return self.check or f"signal:{self.signal}"

    @property
    def effort(self) -> str:
        return effort_label(self.hours)


Signal = Callable[[TenantSnapshot], "Reason | None"]


@dataclass
class Project:
    key: str
    title: str
    pitch: str  # why it matters, in words a client understands
    covers: list[str]  # what the project delivers
    checks: dict[str, Effort] = field(default_factory=dict)
    signals: list[Signal] = field(default_factory=list)
    size: tuple[str, str] | None = None  # (metric key, noun): how big the job is, "42 users"


def effort_label(hours: float) -> str:
    """"Under an hour", "About 6 hours", "About 1½ days" (of ``DAY`` hours)."""
    if hours < 1:
        return "Under an hour"
    if hours < DAY:
        return f"About {plural(round(hours), 'hour')}"
    days = round(hours / DAY * 2) / 2
    return f"About {int(days)}{'½' if days % 1 else ''} {'day' if days == 1 else 'days'}"


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
                  "is switched on, and Security Defaults are off", "high", hours=6,
                  action="Set up Conditional Access: MFA for everyone and legacy sign-in blocked, report-only first",
                  signal="no_ca_policies")


def _missing(capability: str, title: str, summary: str, severity: str, hours: float, action: str) -> Signal:
    def signal(snap: TenantSnapshot) -> Reason | None:
        # False only when the tenant's subscriptions were read and it isn't there; unknown isn't missing.
        if snap.capabilities.get(capability) is not False:
            return None
        return Reason(title, summary, severity, hours=hours, action=action, signal=f"missing_{capability}")
    return signal


# -- The catalog ----------------------------------------------------------------------
# Hours are rough, for a typical small business: enough to tell a half-day project from a quick fix.

CATALOG: list[Project] = [
    Project(
        "identity", "Identity and Conditional Access",
        "Most break-ins start with a stolen password, and admin accounts are the biggest prize. Requiring a "
        "second factor everywhere, blocking the old sign-in methods that can't ask for one and locking down "
        "admin accounts stops nearly all of them.",
        ["Conditional Access policies: MFA for everyone, legacy sign-in blocked, break-glass accounts",
         "MFA registration drive, moving people off text and call codes to the Authenticator app",
         "Admin accounts: the right number of Global Administrators, cloud-only, phishing-resistant sign-in",
         "Risky sign-in and risky user handling",
         "Report-only rollout first, so nobody is locked out"],
        checks={
            "mfa_enforced": Effort(whole=4, each=0.25),  # no policy: design and roll out; else an exclusion
            "legacy_auth_blocked": Effort(whole=2),  # find what still uses it, then block it
            "users_mfa_registered": Effort(each=0.2, most=16),
            "phone_only_mfa": Effort(each=0.2, most=8),
            "risky_sign_ins": Effort(each=0.5, most=8),
            "risky_users": Effort(each=0.5, most=8),
            "admin_mfa": Effort(each=0.25),
            "global_admin_count": Effort(fixed=1),
            "guest_admins": Effort(each=0.25),
            "service_principal_admins": Effort(each=0.5),
            "admins_ca_mfa": Effort(fixed=0.5),
            "admin_phishing_resistant": Effort(each=0.5),
            "cloud_only_admins": Effort(each=0.75),
        },
        signals=[_no_ca_policies], size=("users.enabled", "user")),
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
                     "can't be set", "high", 3, "Add Entra ID P1, on its own or with Business Premium"),
            _missing("intune", "No Intune", "Laptops and phones can't be managed, encrypted or wiped remotely",
                     "medium", 2, "Add Intune, on its own or with Business Premium"),
            _missing("defender_o365_p1", "No Defender for Office 365", "Links and attachments in email aren't "
                     "checked when they're opened", "medium", 1.5,
                     "Add Defender for Office 365 Plan 1 and turn on Safe Links and Safe Attachments")],
        size=("users.licensed", "licensed user")),
    Project(
        "device_management", "Device management with Intune",
        "Company data lives on laptops and phones. Managing them means they're encrypted, kept up to date and "
        "can be wiped if lost, and unmanaged devices can be kept away from company data.",
        ["Intune enrolment for every company computer (Autopilot for new ones)",
         "Compliance policies: encryption, updates, antivirus, screen lock",
         "BitLocker with recovery keys stored in Entra ID",
         "Clean-up of old devices"],
        checks={
            "intune_enrolled": Effort(each=0.5, most=20),
            "device_compliance": Effort(each=0.25, most=12),
            "device_encryption": Effort(fixed=0.5, each=0.25, most=12),
            "device_checkin": Effort(each=0.25, most=8),
            "stale_devices": Effort(fixed=0.5),  # one clean-up, however many
        },
        size=("devices.entra_total", "device")),
    Project(
        "windows_upgrade", "Windows upgrade and hardware refresh",
        "Computers on a Windows version that no longer gets security updates stay open to every flaw found from "
        "now on. Upgrading, or replacing machines that can't upgrade, closes that gap.",
        ["Which computers can upgrade in place and which need replacing",
         "Upgrades scheduled around the working day",
         "Replacement hardware quoted and set up"],
        # Past end of support: upgrade or replace. Ending soon (low): a feature update before it does.
        checks={"supported_windows": Effort(fixed=1, each=1.5, minor=0.25)},
        size=("devices.unsupported_windows", "computer")),
    Project(
        "email_security", "Email security",
        "Email is how most attacks arrive and how fraud gets paid. Locking down the domain stops others "
        "pretending to be the business, and closing forwarding and old protocols stops mail leaking out.",
        ["SPF, DKIM and DMARC set up and enforced for every domain",
         "Encrypted delivery (MTA-STS and TLS reporting)",
         "External forwarding and suspicious inbox rules removed and blocked",
         "SMTP AUTH turned off, mailbox auditing on, shared mailboxes locked"],
        checks={
            "spf_record": Effort(each=0.5),
            "dmarc_policy": Effort(each=1),  # monitor, read the reports, then enforce
            "dkim_enabled": Effort(each=0.5),
            "mta_sts": Effort(each=1.5),  # the policy file needs hosting
            "tls_rpt": Effort(each=0.25),
            "smtp_auth": Effort(fixed=1),  # find the scanners and apps still using it
            "mailbox_auditing": Effort(fixed=0.5),
            "shared_mailbox_signin": Effort(fixed=0.25, each=0.1),
            "external_forwarding": Effort(each=0.25),
            "auto_forward_policy": Effort(fixed=0.5),
            "suspicious_inbox_rules": Effort(each=0.5),  # check each one with the mailbox owner
        },
        size=("mailboxes.total", "mailbox")),
    Project(
        "app_governance", "App and consent governance",
        "Third-party apps can be granted access to mail and files without IT knowing. Reviewing what's there "
        "and putting an approval step in place stops data quietly leaving through an app.",
        ["Review of every app with access, removing what isn't needed",
         "Admin approval workflow for new apps",
         "Users stopped from registering apps or creating tenants",
         "Expiring app secrets and certificates renewed"],
        checks={
            "risky_app_permissions": Effort(each=0.5),
            "user_consent": Effort(fixed=0.5),
            "app_credentials": Effort(each=0.5),
            "admin_consent_workflow": Effort(fixed=0.25),
            "app_registration": Effort(fixed=0.25),
            "tenant_creation": Effort(fixed=0.25),
        },
        size=("apps.third_party", "third-party app")),
    Project(
        "external_sharing", "Sharing, guests and Teams governance",
        "Files shared with anyone who has the link, guests nobody remembers inviting and teams nobody owns are "
        "an easy way for data to end up in the wrong hands.",
        ["Sharing settings: no anonymous links, sensible defaults",
         "Existing anyone links reviewed and removed",
         "Guest invitation rules and a review of current guests",
         "An owner for every team and group, who can create them, and expiry for unused ones"],
        checks={
            "sharepoint_sharing": Effort(fixed=0.5),
            "anyone_links": Effort(fixed=0.25, each=0.1, most=8),
            "guest_invites": Effort(fixed=0.25),
            "pending_guests": Effort(fixed=0.25, each=0.05, most=2),
            "guest_access_level": Effort(fixed=0.25),
            "email_verified_join": Effort(fixed=0.25),
            "ownerless_groups": Effort(each=0.25, most=8),
            "group_creation": Effort(fixed=1),  # agree who, set up the group and the policy
            "group_expiration": Effort(fixed=0.5),
        },
        size=("users.guests", "guest")),
    Project(
        "storage", "SharePoint storage and archiving",
        "When SharePoint storage runs out, people can't save files. Clearing old versions and archiving is "
        "usually cheaper than buying more space.",
        ["Where the space has gone, site by site",
         "Version history limits and clean-up",
         "Archiving, or extra storage where it's needed"],
        checks={"sharepoint_storage_space": Effort(fixed=4)}),
    Project(
        "licence_cleanup", "Licence and account clean-up",
        "Licences on accounts nobody uses, and paid licences sitting unassigned, cost money every month and "
        "leave old accounts open to misuse.",
        ["Leavers' accounts closed and their licences reclaimed",
         "Unused paid licences cut at renewal",
         "Licence assignment errors fixed and renewals checked"],
        checks={
            "stale_users": Effort(each=0.25, most=12),  # confirm with the client, then close each one
            "licensed_disabled": Effort(each=0.1),
            "unassigned_licences": Effort(each=0.25),
            "licence_assignment_errors": Effort(each=0.25),
            "subscriptions_lapsing": Effort(each=0.5),
        },
        size=("users.licensed", "licensed user")),
    Project(
        "hybrid_identity", "Directory sync health",
        "When sync from the office server stops, leavers keep working accounts and password changes don't "
        "reach Microsoft 365.",
        ["Entra Connect repaired or upgraded",
         "Password hash sync on, as a fallback and for leaked-password detection",
         "Plan for moving to cloud-only accounts where the server is no longer needed"],
        checks={"directory_sync_running": Effort(fixed=3), "password_hash_sync": Effort(fixed=0.5)},
        size=("users.members", "user")),
]

PROJECTS = {p.key: p for p in CATALOG}
# Projects folded into bigger ones (migrations/0027 moved their statuses), so old links still find them.
MERGED = {"admin_hardening": "identity", "teams_governance": "external_sharing"}


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
    def hours(self) -> float:
        return sum(r.hours for r in self.reasons) + OVERHEAD

    @property
    def effort(self) -> str:
        return effort_label(self.hours)

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


@dataclass
class Assessment:
    """What a tenant's latest data calls for: projects to propose, and quick fixes to just do."""
    projects: list[Recommendation] = field(default_factory=list)
    quick_fixes: list[Reason] = field(default_factory=list)

    @property
    def quick_hours(self) -> float:
        return sum(r.hours for r in self.quick_fixes)


def _check_reason(r: RuleResult, life: dict, effort: Effort, project: str = "") -> Reason | None:
    if r.status != "fail":
        return None
    active = [f for f in r.findings if not ((lc := life.get((r.key, finding_key(f)))) and lc.accepted)]
    if r.findings and not active:
        return None  # every finding accepted as a risk
    severity = min((f.severity for f in active), key=SEVERITY_RANK.__getitem__, default="medium")
    return Reason(r.title, r.summary, severity, r.key, len(active), effort.hours(active), r.action,
                  project=project)


def _reasons(project: Project, results: dict[str, RuleResult], life: dict, snap: TenantSnapshot) -> list[Reason]:
    # Signals first: at the same severity, "no Conditional Access at all" says more than the checks it causes.
    out = [reason for signal in project.signals if (reason := signal(snap)) is not None]
    for reason in out:
        reason.project = project.key
    for key, effort in project.checks.items():
        if (r := results.get(key)) is not None and (reason := _check_reason(r, life, effort, project.key)):
            out.append(reason)
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


def assess(conn, tenant, now: datetime | None = None, snap: TenantSnapshot | None = None) -> Assessment:
    """Projects (worst first, with what the account manager did) and quick fixes (worst first) for a tenant."""
    now = now or datetime.now(UTC)
    snap = snap or TenantSnapshot(conn, tenant)
    if not snap.runs:
        return Assessment()
    results = {r.key: r for r in evaluate(snap, now)}
    life = lifecycle_map(conn, tenant["id"], now)
    saved = statuses(conn, tenant["id"])
    out = Assessment()
    for project in CATALOG:
        work, quick = [], []
        for reason in _reasons(project, results, life, snap):
            # Urgent and small: do it now, don't hold it for a project.
            (quick if reason.severity == "critical" and reason.hours < MIN_HOURS else work).append(reason)
        if work and sum(r.hours for r in work) + OVERHEAD >= MIN_HOURS:
            rec = Recommendation(project, work, _size(project, snap))
            if row := saved.get(project.key):
                rec.status, rec.note = row["status"], row["note"] or ""
                rec.updated_by, rec.updated_at = row["updated_by"], row["updated_at"]
            out.projects.append(rec)
        else:
            quick += work
        out.quick_fixes += quick
    covered = {key for p in CATALOG for key in p.checks}
    out.quick_fixes += [reason for key, r in results.items()
                        if key not in covered and (reason := _check_reason(r, life, OTHER_EFFORT))]
    out.projects.sort(key=lambda r: r.weight)
    out.quick_fixes.sort(key=lambda r: (SEVERITY_RANK[r.severity], -r.count))
    return out


def recommend(conn, tenant, now: datetime | None = None, snap: TenantSnapshot | None = None) -> list[Recommendation]:
    """The projects this tenant's latest data calls for, worst first, with what the account manager did."""
    return assess(conn, tenant, now, snap).projects


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
    quick_fixes: list[Reason] = field(default_factory=list)
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

    @property
    def quick_fixes(self) -> int:
        return sum(len(row.quick_fixes) for row in self.rows)


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
            found = assess(conn, t, now, snap)
            row.recommendations, row.quick_fixes = found.projects, found.quick_fixes
        except Exception as exc:  # one broken tenant mustn't blank the page
            logging.getLogger(__name__).exception("Project recommendations failed for tenant %s", t["id"])
            row.note = f"Couldn't be worked out: {type(exc).__name__}"
    return Pipeline(rows, now)


def to_table(items: list[tuple[dict, Recommendation]]):
    from .reports.tables import Table

    columns = ["Tenant", "Tenant ID", "Project", "Priority", "Why", "Size", "Estimated hours", "Status", "Note",
               "Updated by", "Updated (UTC)"]
    rows = [[t["name"], t["tenant_ref"], rec.project.title, rec.severity.capitalize(),
             "; ".join(r.summary for r in rec.reasons), rec.size, round(rec.hours), rec.status_label, rec.note,
             rec.updated_by or "", rec.updated_at or ""] for t, rec in items]
    return Table("Recommended projects", columns, rows)
