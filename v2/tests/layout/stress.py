"""Data that stresses page layouts, added to the demo site from scripts/screenshots.py.

* A client with a very long name, people with long names and long sign-in addresses, every display name
  longer (``seed_tenant(mark=...)``), and Conditional Access policies with long names that include and
  exclude sixteen admin roles, several groups and people: the long lists in a cell that once pushed the
  Conditional Access table off the screen.
* A tenant group, a report plan, a month-end recommendation, a staff account and a client contact, all with
  long names, so the pages that list them get long text too.
* A client user who sees only the stress client, so the client's own pages get the same data.

Empty states come from the demo's client still being onboarded (no data yet) and from ``empty_site()``: a new
install with an admin and nothing else.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path

from werkzeug.security import generate_password_hash

NAME = "Harrington, Okonkwo-Fitzgerald & Vanderbilt-Lindqvist Chartered Accountants (Northern Region) LLP"
CLIENT_USER = "stress.client"
STAFF_USER = "alexandra.konstantinopoulou-whitfield@harbourit-managedservices.example"
# Added to every person's, group's, device's, mailbox's and site's display name (tests/seed.mark_names).
MARK = "(Audit & Tax, Manchester)"

# Long, but names a real client could have: a sign-in address runs to about 65 characters.
TENANT = dict(
    name=NAME,
    base="harrington-fitzgerald-accountants",
    tld="example",
    staff=64,
    mfa=(0.35, 0.38),
    inactive=9,
    score=(18.0, 21.5),
    storage=(0.93, 0.98),
    sites="dental",
    forwards=3,
    people={
        "ann": "Anastasia Wolfe-Bergerdorff",
        "bob": "Bartholomew Fitzwilliam-Okonkwo",
        "carl": "Carlos de la Fuente-Vanderbilt",
        "carol": "Carolina Konstantinopoulou",
        "erin": "Erin Ní Fhaoláin-Ó Súilleabháin",
        "eve": "Evangeline Abernathy-Ravenscroft",
        "dan": "Daniel Lindqvist-Featherstonehaugh",
        "mallory": "Mallory Oyelaran-Thistlethwaite",
    },
)

# The admin roles a careful tenant's policies name: the fixture's own role ids first, so its role
# assignments still resolve.
ROLES = [
    ("role-ga", "Global Administrator"),
    ("role-exch", "Exchange Administrator"),
    ("role-security", "Security Administrator"),
    ("role-sharepoint", "SharePoint Administrator"),
    ("role-ca", "Conditional Access Administrator"),
    ("role-apps", "Application Administrator"),
    ("role-cloudapps", "Cloud Application Administrator"),
    ("role-password", "Password Administrator"),
    ("role-privauth", "Privileged Authentication Administrator"),
    ("role-privrole", "Privileged Role Administrator"),
    ("role-helpdesk", "Helpdesk Administrator"),
    ("role-users", "User Administrator"),
    ("role-billing", "Billing Administrator"),
    ("role-teams", "Teams Administrator"),
    ("role-intune", "Intune Administrator"),
    ("role-authadmin", "Authentication Administrator"),
]
ROLE_IDS = [rid for rid, _ in ROLES]
GROUPS = ["grp-breakglass", "grp-exclude-device-compliance", "grp-service-accounts", "grp-travellers"]
PEOPLE = ["bg", "u1", "u2", "u3", "u4", "u5", "u6"]


def _policy(n: int, name: str, users: dict, *, state="enabled", grant=("mfa",), operator="OR", **conditions) -> dict:
    return {
        "id": f"stress-ca{n}",
        "displayName": f"CA{n:03d} — {name}",
        "state": state,
        "conditions": {
            "users": users,
            "applications": conditions.pop("applications", {"includeApplications": ["All"]}),
            "clientAppTypes": conditions.pop("clientAppTypes", ["all"]),
            **conditions,
        },
        "grantControls": {"operator": operator, "builtInControls": list(grant)},
    }


def policies() -> list[dict]:
    """Eleven policies like a real tenant's, several naming every admin role."""
    everyone_but_admins = {
        "includeUsers": ["All"],
        "excludeUsers": ["bg"],
        "excludeGroups": GROUPS[:2],
        "excludeRoles": ROLE_IDS,
    }
    admins = {"includeRoles": ROLE_IDS, "excludeUsers": ["bg"]}
    return [
        _policy(1, "Admins: Require Phishing-Resistant MFA", admins),
        _policy(2, "All Users: Require MFA", everyone_but_admins),
        _policy(
            3,
            "Block Legacy Authentication",
            {"includeUsers": ["All"], "excludeUsers": ["bg"]},
            grant=("block",),
            clientAppTypes=["exchangeActiveSync", "other"],
        ),
        _policy(
            4,
            "All Users: Require Compliant or Hybrid-Joined Device",
            {"includeUsers": ["All"], "excludeUsers": PEOPLE, "excludeGroups": GROUPS},
            grant=("compliantDevice", "domainJoinedDevice"),
            platforms={"includePlatforms": ["windows", "macOS", "iOS", "android"]},
        ),
        _policy(
            5,
            "Block Sign-ins from Countries Outside the United Kingdom and the European Economic Area",
            {"includeUsers": ["All"], "excludeUsers": ["bg"], "excludeGroups": GROUPS[3:]},
            grant=("block",),
            locations={"includeLocations": ["All"], "excludeLocations": ["loc-uk", "loc-eea", "loc-office"]},
        ),
        _policy(
            6,
            "Require MFA for Azure Management",
            {"includeUsers": ["All"], "excludeUsers": ["bg"]},
            applications={"includeApplications": ["797f4846-ba00-4fd7-ba43-dac1f8f63013"]},
        ),
        _policy(7, "Admins: No Persistent Browser, 8h Sign-In Frequency", admins),
        _policy(8, "All Users: 8h Sign-In Frequency", everyone_but_admins),
        _policy(
            9,
            "Require MFA for Device Registration",
            {"includeUsers": ["All"], "excludeUsers": ["bg"]},
            applications={"includeUserActions": ["urn:user:registerdevice"]},
        ),
        _policy(
            10,
            "Insider Risk: Step-Up MFA (P2)",
            {"includeUsers": ["All"], "excludeUsers": ["bg"], "excludeRoles": ROLE_IDS},
            state="enabledForReportingButNotEnforced",
            userRiskLevels=["high", "medium"],
        ),
        _policy(
            11,
            "High Sign-In Risk: Block (P2)",
            {"includeUsers": ["All"], "excludeUsers": ["bg"], "excludeRoles": ROLE_IDS},
            state="disabled",
            grant=("block",),
            signInRiskLevels=["high"],
        ),
    ]


def add(db, ids: list[int]) -> dict:
    """Add the stress data to the demo site's database. ``ids`` are the demo's tenant ids (showcase first,
    the one being onboarded last). Returns {"tenant": id, "client_password": ...}."""
    import demo_data
    import screenshots

    from officesentry import delivery, month_end, roles, tenant_groups
    from officesentry.db import utcnow
    from tests import graphdata

    now = datetime.now(UTC)
    connection = db.execute("SELECT id FROM app_connections ORDER BY id LIMIT 1").fetchone()[0]
    definitions = [{"id": rid, "displayName": n, "isBuiltIn": True, "templateId": rid} for rid, n in ROLES]
    with demo_data.Swapped({graphdata: {"ROLE_DEFINITIONS": definitions}}):
        tenant = demo_data.add_tenant(db, connection, TENANT, now, mark=MARK, ca=policies())
    screenshots.add_email(db, tenant)
    delivery.add_contact(
        db, tenant, "Practice and Finance Director (Audit, Tax & Advisory)", "finance.director.northern@" + _domain()
    )

    group = tenant_groups.create(
        db,
        {
            "name": "Professional services: accountancy, legal and financial advisers (North West and Yorkshire)",
            "category": "Sector and region",
            "description": "Partnerships that hold client money, so insurers ask about MFA, devices and email.",
        },
        "demo",
    )
    tenant_groups.set_members(db, group, [tenant, ids[0]])

    plan = month_end.save_plan(
        db,
        {
            "name": "Monthly security and licensing review for Managed+ clients, sent once approved after the call",
            "reports": ["review", "evidence", "data:conditional-access", "data:licence-rightsizing"],
            "include_xlsx": 1,
            "day_of_month": 2,
            "hour": 8,
            "timezone": "Europe/London",
            "subject": "{client}: Monthly Security & Licensing Review, {month}",
            "message": "Your monthly review is attached. Give us a call if anything needs a hand.",
            "mode": "approval",
            "delivery": "email",
            "bcc_team": 1,
        },
        "demo",
    )
    month_end.apply_plan(db, month_end.get_plan(db, plan), [tenant, ids[0]], "demo", "Europe/London", now)
    month_end.add_action(
        db,
        tenant,
        "Move the remaining fourteen partners and their assistants to phishing-resistant sign-in "
        "(FIDO2 security keys or Windows Hello for Business) before the insurance renewal in January",
        "The cyber insurer's proposal form now asks whether every account with access to client money signs "
        "in with phishing-resistant MFA; today only the IT administrators do.",
        "demo",
    )

    role = roles.by_key(db, "second_line")
    db.execute(
        "INSERT INTO users (username, password_hash, role, role_id, all_tenants, created_at) VALUES (?, ?, ?, ?, 1, ?)",
        (STAFF_USER, generate_password_hash(secrets.token_urlsafe(32)), role.column, role.id, utcnow()),
    )
    password = secrets.token_urlsafe(12)
    uid = db.execute(
        "INSERT INTO users (username, password_hash, role, all_tenants, created_at) VALUES (?, ?, 'client', 0, ?)",
        (CLIENT_USER, generate_password_hash(password), utcnow()),
    ).lastrowid
    db.execute("INSERT INTO user_tenants (user_id, tenant_id) VALUES (?, ?)", (uid, tenant))
    return {"tenant": tenant, "client_password": password}


def _domain() -> str:
    return f"{TENANT['base']}.{TENANT['tld']}"


def empty_site(data_dir: Path):
    """A new install: migrated, one admin, nothing else. Returns (config, admin password)."""
    from officesentry.config import load_config
    from officesentry.db import connect, migrate, utcnow

    cfg = load_config(data_dir=data_dir, secure_cookies=False, require_mfa="off")
    db = connect(cfg.db_path)
    try:
        migrate(db)
        password = secrets.token_urlsafe(12)
        db.execute(
            "INSERT INTO users (username, password_hash, role, all_tenants, created_at) VALUES (?, ?, 'admin', 1, ?)",
            ("demo", generate_password_hash(password), utcnow()),
        )
    finally:
        db.close()
    return cfg, password
