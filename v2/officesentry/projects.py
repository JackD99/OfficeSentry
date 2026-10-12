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

A project is something the client buys: new protection, a migration, a clean-up that saves money. Repairs
(directory sync that stopped, say) belong to no project, so they're quick fixes done under the support
agreement rather than sold. What a project covers (``covers``) is listed for the parts its reasons call for,
so a proposal never offers work the client doesn't need.

Recommendations are worked out from stored data whenever they're shown, so a project drops off once the
tenant is fixed. What the account manager did with one (proposed, agreed, declined, with a note) is kept in
``project_status``. Estimates are a rough guide for staff and never reach the client's proposal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from . import devices, hybrid
from .checks import SEVERITY_RANK, Finding, RuleResult, evaluate
from .db import utcnow
from .findings import finding_key, lifecycle_map
from .reports.precomputed import Rows, stored_rows
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

    @property
    def headline(self) -> str:
        """What was found, in a few words: a check's summary ("3 sites with no owner"), a signal's title."""
        return self.summary if self.check else self.title

    @property
    def detail(self) -> str:
        """The line under it: the check's goal, labelled so it doesn't read as a second finding, or what a
        signal means."""
        return f"Goal: {self.title}" if self.check else self.summary


Signal = Callable[[TenantSnapshot], "Reason | None"]


@dataclass(frozen=True)
class Cover:
    """A part of what a project delivers. ``when`` names the checks (and ``signal:<key>``) that call for it:
    it's offered only when one of them is among the project's reasons. With no ``when`` it's always offered."""

    text: str
    when: tuple[str, ...] = ()


def _cover(text: str, *when: str) -> Cover:
    return Cover(text, when)


@dataclass
class Project:
    key: str
    title: str  # what the client buys, in their words: a name you'd put on a quote
    pitch: str  # why it matters, in words a client understands
    covers: list[Cover]  # what the project delivers
    checks: dict[str, Effort] = field(default_factory=dict)
    signals: list[Signal] = field(default_factory=list)
    size: tuple[str, str] | None = None  # (metric key, noun): how big the job is, "42 users"


def effort_label(hours: float) -> str:
    """ "Under an hour", "About 6 hours", "About 1½ days" (of ``DAY`` hours)."""
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
    return Reason(
        "No Conditional Access policies",
        "The tenant has Entra ID P1 but no Conditional Access policy is switched on, and Security Defaults are off",
        "high",
        hours=6,
        action="Set up Conditional Access: MFA for everyone and legacy sign-in blocked, report-only first",
        signal="no_ca_policies",
    )


def _missing(capability: str, title: str, summary: str, severity: str, hours: float, action: str) -> Signal:
    def signal(snap: TenantSnapshot) -> Reason | None:
        # False only when the tenant's subscriptions were read and it isn't there; unknown isn't missing.
        if snap.capabilities.get(capability) is not False:
            return None
        return Reason(title, summary, severity, hours=hours, action=action, signal=f"missing_{capability}")

    signal.__name__ = f"_missing_{capability}"  # like the other signals: its key with a leading underscore
    return signal


CLOUD_ONLY_MAX_USERS = 50  # above this, on-premises AD usually does more than sync accounts, so it stays


def _synced_users(snap: TenantSnapshot) -> list[dict] | None:
    """Enabled members synced from on-premises AD, for a small tenant with directory sync on."""
    status, users = hybrid.sync_status(snap), snap.objects("users", "user")
    if not status or not status.get("enabled") or users is None:
        return None
    synced = [
        u for u in users if u.get("onPremisesSyncEnabled") and u.get("accountEnabled") and u.get("userType") == "Member"
    ]
    return synced if 0 < len(synced) <= CLOUD_ONLY_MAX_USERS else None


def _synced_accounts(snap: TenantSnapshot) -> Reason | None:
    """A small business whose accounts still come from an office server: it can usually go cloud-only."""
    synced = _synced_users(snap)
    if synced is None:
        return None
    return Reason(
        f"{plural(len(synced), 'account')} synced from an office server",
        "They depend on on-premises Active Directory and the Entra Connect server staying up",
        "low",
        hours=3 + min(0.25 * len(synced), 8),  # plan, check what needs AD, convert, then turn sync off
        action="Move the accounts to cloud-only and turn off directory sync",
        signal="synced_accounts",
    )


def _domain_joined(snap: TenantSnapshot) -> Reason | None:
    """Computers joined to the office domain, which have to move to Entra ID before the server can go."""
    if _synced_users(snap) is None or (entra := devices.objects(snap, "entra_device")) is None:
        return None
    joined = [
        d for d in entra if d.get("trustType") == "ServerAd" and d.get("accountEnabled") and devices.is_computer(d)
    ]
    if not joined:
        return None
    return Reason(
        f"{plural(len(joined), 'computer')} joined to the office domain",
        "Each moves to Entra ID before the server can go",
        "low",
        hours=min(len(joined), 40),  # each one re-joined to Entra ID, keeping its files and settings
        action="Join each computer to Entra ID instead, keeping its user's files and settings",
        signal="domain_joined",
    )


# -- The catalog ----------------------------------------------------------------------
# Hours are rough, for a typical small business: enough to tell a half-day project from a quick fix.

CATALOG: list[Project] = [
    Project(
        "identity",
        "Identity and Conditional Access",
        "Most break-ins start with a stolen password, and admin accounts are the biggest prize. Requiring a "
        "second factor everywhere, blocking the old sign-in methods that can't ask for one and locking down "
        "admin accounts stops nearly all of them.",
        [
            _cover(
                "Conditional Access policies: MFA for everyone, legacy sign-in blocked, break-glass accounts",
                "signal:no_ca_policies",
                "mfa_enforced",
                "legacy_auth_blocked",
                "admins_ca_mfa",
            ),
            _cover(
                "MFA registration drive, moving people off text and call codes to the Authenticator app",
                "users_mfa_registered",
                "phone_only_mfa",
            ),
            _cover(
                "Admin accounts: separate, cloud-only and signing in with a passkey or security key",
                "admin_mfa",
                "admin_phishing_resistant",
                "cloud_only_admins",
            ),
            _cover(
                "Admin roles: the right number of Global Administrators, none held by guests, apps reviewed",
                "global_admin_count",
                "guest_admins",
                "service_principal_admins",
            ),
            _cover("Risky sign-in and risky user handling", "risky_sign_ins", "risky_users"),
            _cover(
                "Report-only rollout first, so nobody is locked out",
                "signal:no_ca_policies",
                "mfa_enforced",
                "legacy_auth_blocked",
                "admins_ca_mfa",
            ),
        ],
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
        signals=[_no_ca_policies],
        size=("users.enabled", "user"),
    ),
    Project(
        "licence_upgrade",
        "Security licence upgrade",
        "The current licences leave out security tools the other projects rely on: Conditional Access, device "
        "management or protection from malicious links and attachments. Microsoft 365 Business Premium "
        "includes all three; an add-on can cover a single gap.",
        [
            _cover("Licence options and cost comparison (Business Premium or add-ons)"),
            _cover("Entra ID P1, so sign-in rules can be set with Conditional Access", "signal:missing_entra_p1"),
            _cover("Intune, so laptops and phones can be managed, encrypted and wiped", "signal:missing_intune"),
            _cover(
                "Defender for Office 365, with Safe Links and Safe Attachments turned on",
                "signal:missing_defender_o365_p1",
            ),
            _cover("Switch-over with no interruption to email or files"),
        ],
        signals=[
            _missing(
                "entra_p1",
                "No Entra ID P1",
                "Without it there's no Conditional Access, so sign-in rules can't be set",
                "high",
                3,
                "Add Entra ID P1, on its own or with Business Premium",
            ),
            _missing(
                "intune",
                "No Intune",
                "Laptops and phones can't be managed, encrypted or wiped remotely",
                "medium",
                2,
                "Add Intune, on its own or with Business Premium",
            ),
            _missing(
                "defender_o365_p1",
                "No Defender for Office 365",
                "Links and attachments in email aren't checked when they're opened",
                "medium",
                1.5,
                "Add Defender for Office 365 Plan 1 and turn on Safe Links and Safe Attachments",
            ),
        ],
        size=("users.licensed", "licensed user"),
    ),
    Project(
        "device_management",
        "Device management with Intune",
        "Company data lives on laptops and phones. Managing them means they're encrypted, kept up to date and "
        "can be wiped if lost, and unmanaged devices can be kept away from company data.",
        [
            _cover("Intune enrolment for every company computer (Autopilot for new ones)", "intune_enrolled"),
            _cover(
                "Compliance policies: encryption, updates, antivirus, screen lock",
                "device_compliance",
                "intune_compliance_coverage",
            ),
            _cover("BitLocker with recovery keys stored in Entra ID", "device_encryption", "bitlocker_keys"),
            _cover("Clean-up of old devices and ones that stopped checking in", "device_checkin", "stale_devices"),
        ],
        checks={
            "intune_enrolled": Effort(each=0.5, most=20),
            "device_compliance": Effort(each=0.25, most=12),
            "device_encryption": Effort(fixed=0.5, each=0.25, most=12),
            # A low finding is a computer outside Intune: enrolling it (intune_enrolled) is the work.
            "bitlocker_keys": Effort(each=0.25, minor=0, most=8),
            "intune_compliance_coverage": Effort(each=0.5, most=3),
            "device_checkin": Effort(each=0.25, most=8),
            "stale_devices": Effort(fixed=0.5),  # one clean-up, however many
        },
        size=("devices.entra_total", "device"),
    ),
    Project(
        "windows_upgrade",
        "Windows upgrade and hardware refresh",
        "Computers on a Windows version that no longer gets security updates stay open to every flaw found from "
        "now on. Upgrading, or replacing machines that can't upgrade, closes that gap.",
        [
            _cover("Which computers can upgrade in place and which need replacing"),
            _cover("Upgrades scheduled around the working day"),
            _cover("Replacement hardware quoted and set up"),
        ],
        # Past end of support: upgrade or replace. Ending soon (low): a feature update before it does.
        checks={"supported_windows": Effort(fixed=1, each=1.5, minor=0.25)},
        size=("devices.unsupported_windows", "computer"),
    ),
    Project(
        "email_security",
        "Email security",
        "Email is how most attacks arrive and how fraud gets paid. Locking down the domain stops others "
        "pretending to be the business, and closing forwarding and old protocols stops mail leaking out.",
        [
            _cover(
                "SPF, DKIM and DMARC set up and enforced for every domain", "spf_record", "dkim_enabled", "dmarc_policy"
            ),
            _cover("Encrypted delivery (MTA-STS and TLS reporting)", "mta_sts", "tls_rpt"),
            _cover(
                "External forwarding and suspicious inbox rules removed and blocked",
                "external_forwarding",
                "auto_forward_policy",
                "suspicious_inbox_rules",
            ),
            _cover("SMTP AUTH turned off, once scanners and apps send mail another way", "smtp_auth"),
            _cover("Mailbox auditing on", "mailbox_auditing"),
            _cover("Sign-in blocked for shared mailboxes", "shared_mailbox_signin"),
            _cover(
                "Mail flow rules that let spam in or send copies out fixed",
                "mail_rule_spam_bypass",
                "mail_rule_external_copy",
            ),
            _cover(
                "Spam, phishing and malware policies tightened: allowed senders, spoof protection, attachment filter",
                "spam_allowed_domains",
                "spoof_protection",
                "malware_file_filter",
            ),
        ],
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
            "mail_rule_spam_bypass": Effort(each=0.5),  # agree a safer route with whoever the rule was for
            "mail_rule_external_copy": Effort(each=0.5),
            "spam_allowed_domains": Effort(each=0.5),
            "spoof_protection": Effort(each=0.25),
            "malware_file_filter": Effort(each=0.25),
        },
        size=("mailboxes.total", "mailbox"),
    ),
    Project(
        "app_governance",
        "Third-party app review",
        "Third-party apps can be granted access to mail and files without IT knowing. Reviewing what's there "
        "and putting an approval step in place stops data quietly leaving through an app.",
        [
            _cover("Review of every app with access, removing what isn't needed", "risky_app_permissions"),
            _cover(
                "Admin approval for new apps, instead of people agreeing to them on their own",
                "user_consent",
                "admin_consent_workflow",
            ),
            _cover("People stopped from registering apps or creating tenants", "app_registration", "tenant_creation"),
            _cover("Expiring app secrets and certificates renewed", "app_credentials"),
        ],
        checks={
            "risky_app_permissions": Effort(each=0.5),
            "user_consent": Effort(fixed=0.5),
            "app_credentials": Effort(each=0.5),
            "admin_consent_workflow": Effort(fixed=0.25),
            "app_registration": Effort(fixed=0.25),
            "tenant_creation": Effort(fixed=0.25),
        },
        size=("apps.third_party", "third-party app"),
    ),
    Project(
        "external_sharing",
        "File sharing and guest access",
        "Files shared with anyone who has the link, guests nobody remembers inviting and sites and teams nobody "
        "owns are an easy way for data to end up in the wrong hands.",
        [
            _cover("Sharing settings: no anonymous links, sensible defaults", "sharepoint_sharing"),
            _cover(
                "Existing sharing reviewed: anyone links removed, outside editors cut to view-only",
                "anyone_links",
                "outside_edit_access",
            ),
            _cover(
                "Guest invitation rules and a review of current guests",
                "guest_invites",
                "pending_guests",
                "guest_access_level",
                "email_verified_join",
            ),
            _cover(
                "An owner from the business for every site, team and group",
                "site_owners",
                "ownerless_groups",
                "guest_owners",
                "site_guest_full_control",
            ),
            _cover("Who can create teams and groups, and expiry for unused ones", "group_creation", "group_expiration"),
        ],
        checks={
            "sharepoint_sharing": Effort(fixed=0.5),
            "anyone_links": Effort(fixed=0.25, each=0.1, most=8),
            "outside_edit_access": Effort(fixed=0.25, each=0.1, most=6),
            "guest_invites": Effort(fixed=0.25),
            "pending_guests": Effort(fixed=0.25, each=0.05, most=2),
            "guest_access_level": Effort(fixed=0.25),
            "email_verified_join": Effort(fixed=0.25),
            "site_owners": Effort(each=0.25, most=8),
            "site_guest_full_control": Effort(each=0.25, most=4),
            "ownerless_groups": Effort(each=0.25, most=8),
            "guest_owners": Effort(each=0.25, most=8),
            "group_creation": Effort(fixed=1),  # agree who, set up the group and the policy
            "group_expiration": Effort(fixed=0.5),
        },
        size=("sharepoint.sites", "site"),
    ),
    Project(
        "storage",
        "Storage and archiving",
        "When storage runs out, people can't save files or receive email. Clearing old versions and archiving "
        "is usually cheaper than buying more space.",
        [
            _cover("Where the SharePoint space has gone, site by site", "sharepoint_storage_space"),
            _cover("Version history limits and clean-up", "sharepoint_storage_space"),
            _cover("Online archive and retention for mailboxes that are nearly full", "mailbox_quota"),
            _cover("OneDrive clean-up for people near their limit", "onedrive_quota"),
            _cover("Extra storage only where it's still needed"),
        ],
        checks={
            "sharepoint_storage_space": Effort(fixed=4),
            "mailbox_quota": Effort(each=0.5, most=8),  # archive on and a retention policy, per mailbox
            "onedrive_quota": Effort(each=0.25, most=8),
        },
    ),
    Project(
        "licence_cleanup",
        "Licence savings and leaver clean-up",
        "Licences on accounts nobody uses, and paid licences sitting unassigned, cost money every month and "
        "leave old accounts open to misuse.",
        [
            _cover("Leavers' accounts closed and their licences reclaimed", "stale_users", "licensed_disabled"),
            _cover("Unused paid licences cut at renewal", "unassigned_licences"),
            _cover("Licence assignment errors fixed", "licence_assignment_errors"),
            _cover("Renewals checked, so nothing lapses by accident", "subscriptions_lapsing"),
            _cover("Managers updated where people still report to a leaver", "blocked_manager"),
        ],
        checks={
            "stale_users": Effort(each=0.25, most=12),  # confirm with the client, then close each one
            "blocked_manager": Effort(each=0.1, most=4),
            "licensed_disabled": Effort(each=0.1),
            "unassigned_licences": Effort(each=0.25),
            "licence_assignment_errors": Effort(each=0.25),
            "subscriptions_lapsing": Effort(each=0.5),
        },
        size=("users.licensed", "licensed user"),
    ),
    # Directory sync that has stopped is a repair (a quick fix), not something to sell; this is the project. It
    # replaced "Directory sync health" (hybrid_identity), whose statuses were about the repair and so don't carry.
    Project(
        "cloud_only",
        "Move to cloud-only accounts",
        "Accounts that sync from an office server depend on it: when sync stops, leavers keep working accounts "
        "and password changes don't reach Microsoft 365, and the server needs patching and backing up. For a "
        "business this size, moving accounts to the cloud usually lets the server go.",
        [
            _cover("What still needs the office server: file shares, printers and older apps"),
            _cover("Accounts moved to cloud-only, keeping each person's mailbox and files", "signal:synced_accounts"),
            _cover(
                "Computers moved from the office domain to Entra ID, keeping files and settings", "signal:domain_joined"
            ),
            _cover("Directory sync turned off and the sync server retired"),
        ],
        signals=[_synced_accounts, _domain_joined],  # their counts are the size of the job
    ),
]

PROJECTS = {p.key: p for p in CATALOG}
# Projects folded into bigger ones (migrations/0028 moved their statuses), so old links still find them.
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
    def covers(self) -> list[str]:
        """What the project delivers for this client: the parts its reasons call for."""
        found = {r.key for r in self.reasons}
        return [c.text for c in self.project.covers if not c.when or found.intersection(c.when)]

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
    return Reason(r.title, r.summary, severity, r.key, len(active), effort.hours(active), r.action, project=project)


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
    return {
        row["project_key"]: dict(row)
        for row in conn.execute("SELECT * FROM project_status WHERE tenant_id = ?", (tenant_id,))
    }


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
    out.quick_fixes += [
        reason for key, r in results.items() if key not in covered and (reason := _check_reason(r, life, OTHER_EFFORT))
    ]
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
        (tenant_id, project_key, status, note, username, utcnow()),
    )


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
        out = [
            (row.tenant, rec)
            for row in self.rows
            for rec in row.recommendations
            if (not project or rec.project.key == project) and (rec.status == "declined") == declined
        ]
        out.sort(key=lambda x: (*x[1].weight, x[0]["name"].lower()))
        return out

    def count(self, project: str = "", status: str | None = None) -> int:
        return sum(
            1
            for row in self.rows
            for rec in row.recommendations
            if (not project or rec.project.key == project)
            and (rec.status == status if status else rec.status != "declined")
        )

    @property
    def tenants_with(self) -> int:
        return sum(1 for row in self.rows if any(r.status != "declined" for r in row.recommendations))

    @property
    def without_data(self) -> int:
        return sum(1 for row in self.rows if row.note)

    @property
    def quick_fixes(self) -> int:
        return sum(len(row.quick_fixes) for row in self.rows)


def pipeline(
    conn, tenants, now: datetime | None = None, rebuild: bool = False, *, cached: bool | None = None
) -> Pipeline:
    """Every tenant's recommendations. ``cached`` (by default when ``now`` isn't given: the pages, which show
    the present) reads each tenant's row from ``precomputed`` while that's current, and builds and stores it
    when it isn't (always, with ``rebuild``)."""
    cached = now is None if cached is None else cached
    stored = stored_rows(conn, "projects", tenants, None if cached else now, rebuild)
    now = now or datetime.now(UTC)
    return Pipeline([_tenant_row(conn, t, now, stored) for t in tenants], now)


def _tenant_row(conn, tenant, now: datetime, stored: Rows | None) -> TenantRow:
    row = TenantRow(dict(tenant))
    fingerprint = stored.fingerprint(tenant) if stored else ""
    if stored and (data := stored.get(tenant, fingerprint)) is not None:
        row.note = data["note"]
        row.recommendations = [_recommendation(r) for r in data["projects"]]
        row.quick_fixes = [Reason(**r) for r in data["quick"]]
        return row
    try:
        snap = TenantSnapshot(conn, tenant)
        if not snap.runs:
            row.note = "No data collected yet."
        else:
            found = assess(conn, tenant, now, snap)
            row.recommendations, row.quick_fixes = found.projects, found.quick_fixes
    except Exception as exc:  # one broken tenant mustn't blank the page
        logging.getLogger(__name__).exception("Project recommendations failed for tenant %s", tenant["id"])
        row.note = f"Couldn't be worked out: {type(exc).__name__}"
        return row  # not stored: the next view tries again
    if stored:
        stored.save(
            tenant,
            fingerprint,
            {
                "note": row.note,
                "projects": [
                    {**asdict(rec), "project": rec.project.key, "reasons": [asdict(r) for r in rec.reasons]}
                    for rec in row.recommendations
                ],
                "quick": [asdict(r) for r in row.quick_fixes],
            },
        )
    return row


def _recommendation(data: dict) -> Recommendation:
    return Recommendation(
        PROJECTS[data["project"]],
        [Reason(**r) for r in data["reasons"]],
        data["size"],
        data["status"],
        data["note"],
        data["updated_by"],
        data["updated_at"],
    )


def to_table(items: list[tuple[dict, Recommendation]]):
    from .reports.tables import Table

    columns = [
        "Client",
        "Tenant ID",
        "Project",
        "Priority",
        "Why",
        "Size",
        "Estimated hours",
        "Status",
        "Note",
        "Updated by",
        "Updated (UTC)",
    ]
    rows = [
        [
            t["name"],
            t["tenant_ref"],
            rec.project.title,
            rec.severity.capitalize(),
            "; ".join(r.summary for r in rec.reasons),
            rec.size,
            round(rec.hours),
            rec.status_label,
            rec.note,
            rec.updated_by or "",
            rec.updated_at or "",
        ]
        for t, rec in items
    ]
    return Table("Recommended projects", columns, rows)
