"""What a role can do: the capabilities, and the built-in roles made of them.

Access has two axes. **Capabilities** say what someone may do (see each report area, export, refresh a
client's data, accept risks, approve month-end reports, manage clients, users or settings, ...).
**Scope** says on which clients: every client, or the clients and client groups an admin granted them
(``users.all_tenants``, ``user_tenants``, ``user_tenant_groups``). A role (``roles`` table, ``roles.py``)
holds a set of capabilities and a scope; a person has one role, and an admin can add or take away single
capabilities for that person, everywhere or on one client (``user_access``). Taken away always wins.

Three kinds of role: ``admin`` (the one Administrator role, every capability on every client, never
narrowed, so there's always someone who can fix things), ``staff`` (the MSP's people) and ``client`` (a
client's own people, who only ever get report capabilities on their own clients). The kind is also kept
in ``users.role`` (``admin``, ``analyst`` for staff, ``client``) for the code that only needs to know that.

Nothing here reads the database: ``roles.py`` does. Reports and checks never import this.
"""

from __future__ import annotations

from dataclasses import dataclass

ADMIN, STAFF, CLIENT = "admin", "staff", "client"
KINDS = (ADMIN, STAFF, CLIENT)
# What ``users.role`` says for each kind of role (the column keeps its original values).
ROLE_COLUMN = {ADMIN: "admin", STAFF: "analyst", CLIENT: "client"}
KIND_OF_COLUMN = {v: k for k, v in ROLE_COLUMN.items()}
ALL_CLIENTS, GRANTED = "all", "granted"
SCOPES = (ALL_CLIENTS, GRANTED)


@dataclass(frozen=True)
class Capability:
    key: str
    title: str
    help: str
    group: str
    client_ok: bool = False  # may a client role hold it?


GROUPS: tuple[tuple[str, str, str], ...] = (
    ("see", "See", "Which parts of Office Sentry open for them, on the clients in their scope."),
    ("work", "Work on clients", "What they can change about a client's reporting."),
    ("run", "Run Office Sentry", "Settings that affect every client and every user."),
)

CAPABILITIES: tuple[Capability, ...] = (
    # See
    Capability(
        "reports.security",
        "Security findings and reviews",
        "Findings, the monthly and quarterly reviews, questionnaires and evidence, custom reports and the "
        "check matrix.",
        "see",
        client_ok=True,
    ),
    Capability("reports.identity", "Identity and access reports", "Users, MFA, admins, sign-ins, guests.", "see", True),
    Capability("reports.email", "Email reports", "Mailboxes, forwarding, inbox rules, domains.", "see", True),
    Capability("reports.files", "Files and Teams reports", "SharePoint, OneDrive, sharing, groups.", "see", True),
    Capability("reports.devices", "Devices and apps reports", "Intune devices, apps and consents.", "see", True),
    Capability(
        "reports.tenant",
        "Tenant and licence reports",
        "Licences, subscriptions, usage, tenant settings, Secure Score and admin activity.",
        "see",
        True,
    ),
    Capability("reports.people", "People", "The Users tab and each person's page.", "see", True),
    Capability("reports.changes", "What changed", "Month-over-month changes for a client.", "see", True),
    Capability(
        "reports.activity", "Activity history", "Kept sign-in and audit history beyond Microsoft's 30 days.", "see"
    ),
    Capability(
        "emails.view",
        "Emailed reports",
        "A client's report schedules and report history; previews also need security findings.",
        "see",
        True,
    ),
    Capability("projects.view", "Recommended projects", "Projects and quick fixes worked out from the checks.", "see"),
    Capability("export", "Download files", "PDF, Excel and CSV downloads of what they can see.", "see", True),
    Capability(
        "clients.compare",
        "Across clients",
        "All clients, What changed across clients, Coming up, search and the Today and Accounts home pages.",
        "see",
    ),
    # Work on clients
    Capability("collect", "Refresh data", "Collect now, see collection runs and check the connection.", "work"),
    Capability("findings.manage", "Accept risks and annotate", "Accept risks, override evidence answers.", "work"),
    Capability(
        "emails.manage", "Emailed reports", "Set up a client's schedules, contacts and alerts; send now.", "work"
    ),
    Capability("month_end.review", "Review month-end reports", "The queue, commentary, notes and assignments.", "work"),
    Capability("month_end.approve", "Approve and send month-end reports", "Approve, send and re-issue.", "work"),
    Capability("projects.manage", "Update projects", "Mark projects proposed, won or declined.", "work"),
    Capability("designs.manage", "Design custom reports", "Create and edit report designs.", "work"),
    # Run Office Sentry
    Capability(
        "clients.manage",
        "Manage clients",
        "Add, edit, pause, archive and offboard clients; onboarding; client groups.",
        "run",
    ),
    Capability("users.manage", "Users and roles", "Accounts, roles and what each person can see.", "run"),
    Capability(
        "settings.manage",
        "Settings",
        "App connection, email delivery, alerts, branding, licence prices, data retention, report plans and "
        "team-wide emails. Only for someone who sees every client.",
        "run",
    ),
    Capability("activity_log.read", "Activity log", "Who did what in Office Sentry.", "run"),
    Capability(
        "system.manage",
        "System status",
        "Health checks, the monitoring token and support. Only for someone who sees every client.",
        "run",
    ),
)
BY_KEY: dict[str, Capability] = {c.key: c for c in CAPABILITIES}
# Capabilities about every client at once (team-wide emails and alerts, report plans, the app connection,
# system status and the support package): they only take effect for someone who sees every client, since
# with them a person limited to some clients would see or change the others (``roles.access_for``).
EVERY_CLIENT: frozenset[str] = frozenset({"settings.manage", "system.manage"})
KEYS: tuple[str, ...] = tuple(BY_KEY)
CLIENT_KEYS: frozenset[str] = frozenset(c.key for c in CAPABILITIES if c.client_ok)
# What a report area's reports need: the menu group (registry.GROUP_TITLES) -> capability.
AREA_CAPABILITY = {
    "identity": "reports.identity",
    "email": "reports.email",
    "files": "reports.files",
    "devices": "reports.devices",
    "tenant": "reports.tenant",
}
# ...and the summaries (library.SUMMARIES kinds).
SUMMARY_CAPABILITY = {
    "review": "reports.security",
    "evidence": "reports.security",
    "findings": "reports.security",
    "changes": "reports.changes",
}


def area_capability(group: str | None) -> str:
    """The capability a report in this menu group needs (an unlisted group counts as tenant reports)."""
    return AREA_CAPABILITY.get(group or "", "reports.tenant")


@dataclass(frozen=True)
class Template:
    key: str
    name: str
    description: str
    kind: str
    scope: str
    capabilities: frozenset[str]


def _caps(*keys: str) -> frozenset[str]:
    unknown = set(keys) - set(KEYS)
    if unknown:
        raise ValueError(f"unknown capabilities {sorted(unknown)}")
    return frozenset(keys)


SEE = tuple(c.key for c in CAPABILITIES if c.group == "see")
WORK = tuple(c.key for c in CAPABILITIES if c.group == "work")
RUN = tuple(c.key for c in CAPABILITIES if c.group == "run")

# The built-in roles. Today's admins become Administrators and today's analysts Second line, which holds
# exactly what an analyst could do; a client account becomes a Client viewer.
TEMPLATES: tuple[Template, ...] = (
    Template(
        "administrator",
        "Administrator",
        "Everything, on every client: settings, users and roles, the app connection.",
        ADMIN,
        ALL_CLIENTS,
        _caps(*KEYS),
    ),
    Template(
        "team_leader",
        "Team leader",
        "Every client and every report; manages clients, approves month-end reports. Not users or settings.",
        STAFF,
        ALL_CLIENTS,
        _caps(*SEE, *WORK, "clients.manage", "activity_log.read"),
    ),
    Template(
        "second_line",
        "Second line",
        "Every report on their clients; refreshes data, accepts risks, sets up emails and reviews month-end.",
        STAFF,
        GRANTED,
        _caps(
            *SEE,
            "collect",
            "findings.manage",
            "emails.manage",
            "month_end.review",
            "projects.manage",
            "designs.manage",
        ),
    ),
    Template(
        "first_line",
        "First line",
        "Looks things up on their clients: people, identity, email and device reports. Nothing to change.",
        STAFF,
        GRANTED,
        _caps(
            "reports.identity",
            "reports.email",
            "reports.devices",
            "reports.people",
            "reports.changes",
            "emails.view",
            "export",
            "clients.compare",
        ),
    ),
    Template(
        "account_manager",
        "Account manager",
        "Licences, renewals, month-end reports and projects for their clients; approves and sends reports.",
        STAFF,
        GRANTED,
        _caps(
            "reports.security",
            "reports.tenant",
            "reports.changes",
            "emails.view",
            "projects.view",
            "export",
            "clients.compare",
            "emails.manage",
            "month_end.review",
            "month_end.approve",
            "projects.manage",
        ),
    ),
    Template(
        "auditor",
        "Auditor",
        "Read-only: every report and download on their clients, nothing to change.",
        STAFF,
        GRANTED,
        _caps(*SEE),
    ),
    Template(
        "client_viewer",
        "Client viewer",
        "A client's own people: their reports and downloads, their client only.",
        CLIENT,
        GRANTED,
        _caps(*(k for k in SEE if k in CLIENT_KEYS)),
    ),
)
TEMPLATE_BY_KEY: dict[str, Template] = {t.key: t for t in TEMPLATES}
# The built-in role today's users land on, by what users.role said before roles existed.
UPGRADE_ROLE = {"admin": "administrator", "analyst": "second_line", "client": "client_viewer"}


def allowed_for(kind: str, keys) -> set[str]:
    """The capabilities a role of this kind may hold: a client role only report ones, an admin all of them."""
    if kind == ADMIN:
        return set(KEYS)
    chosen = {k for k in keys if k in BY_KEY}
    return chosen & CLIENT_KEYS if kind == CLIENT else chosen
