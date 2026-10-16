"""Identity and licence detail reports.

MFA coverage against what actually requires MFA, admins and break-glass
accounts, licence waste in money, inactive accounts, and the Secure Score
trend with control-level changes. Like the other detail reports they read
stored snapshots only and return titled tables; data a tenant's licences
can't provide is listed in ``report.notes`` rather than shown as a pass.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from .. import clouds, coverage
from ..authmethods import mfa_set_up
from ..devices import parse_day, parse_ts
from ..formatting import money
from ..licences import (
    FREE_SKUS,
    FREE_UNIT_THRESHOLD,
    STALE_DAYS,
    is_stale,
    last_sign_in,
    licence_summary,
    load_prices,
    never_signed_in,
    shared_needing_licence,
    sku_name,
)
from ..permission_batch import waiting_text
from ..snapshot import TenantSnapshot
from ..text import plural, verb
from .charts import line_chart
from .data_reports import Chart, DataReport, Highlight, Rows, Section, metric_trend
from .monthly_review import _secure_score_points, setting
from .registry import report

NEVER_SIGNED_IN_GRACE = 30  # days after creation before "never signed in" counts as inactive (see licences.is_stale)
P1 = "Entra ID P1"


class Money(float):
    """A number that shows as money on the page and stays a number in the XLSX."""

    currency = "GBP"

    def __new__(cls, value: float, currency: str):
        obj = super().__new__(cls, round(value, 2))
        obj.currency = currency
        return obj

    def __str__(self) -> str:
        return money(float(self), self.currency, 2)


# -- Shared helpers ------------------------------------------------------------------


_parse = parse_ts


def _day(value) -> str:
    """'3 Sep 2026' (no platform-specific strftime flags, so it also works on Windows)."""
    if isinstance(value, str):
        value = _parse(value)
    return f"{value.day} {value:%b %Y}" if value else ""


def _label(row: dict) -> str:
    return row.get("userPrincipalName") or row.get("displayName") or row.get("principalId") or row.get("_id", "?")


def _some(names: list[str], limit: int = 3) -> str:
    """'A, B, C and 4 more': long policy lists stay readable. The CA coverage report has them in full."""
    names = list(names)
    if len(names) <= limit + 1:
        return ", ".join(names)
    return f"{', '.join(names[:limit])} and {len(names) - limit} more"


def _users(snap: TenantSnapshot) -> dict[str, dict] | None:
    rows = snap.objects("users", "user")
    return None if rows is None else {u["_id"]: u for u in rows}


def _sign_in(user: dict | None) -> str:
    if not user or not user.get("signInActivityAvailable"):
        return "Not checked"
    last = last_sign_in(user)
    return _day(last) if last else never_signed_in(user)


def _roles(snap: TenantSnapshot) -> dict[str, dict] | None:
    """Role holders by principal: label, kind, and active and PIM-eligible role names (``privileged_roles``:
    the active ones that are privileged)."""
    rows = snap.objects("admin_roles", "role_assignment")
    if rows is None:
        return None
    out: dict[str, dict] = {}
    for r in rows:
        if not r.get("principalId"):
            continue
        p = out.setdefault(
            r["principalId"],
            {
                "id": r["principalId"],
                "label": _label(r),
                "type": r.get("principalType") or "unknown",
                "userType": r.get("userType") or "Member",
                "enabled": r.get("accountEnabled"),
                "appId": r.get("appId"),
                "active": [],
                "eligible": [],
                "privileged": False,
                "privileged_active": False,
                "privileged_roles": [],
                "privileged_eligible_roles": [],  # as the role columns show them: "Global Administrator (eligible)"
                "global_admin": False,
                "groups": [],
            },
        )
        # Someone who can activate membership of the group (PIM for groups) holds its roles as eligible.
        via = f"{r['viaGroup']} (eligible member)" if r.get("viaGroup") and r.get("viaPim") else r.get("viaGroup")
        if via and via not in p["groups"]:
            p["groups"].append(via)
        name = (r.get("roleName") or "Unknown role") + (
            "" if r.get("directoryScopeId") in (None, "/") else " (limited scope)"
        )
        bucket = p["active"] if r.get("assignment") == "active" else p["eligible"]
        if name not in bucket:
            bucket.append(name)
        if r.get("privileged"):
            p["privileged"] = True
            p["privileged_active"] |= r.get("assignment") == "active"
            if r.get("assignment") == "active" and name not in p["privileged_roles"]:
                p["privileged_roles"].append(name)
            elif r.get("assignment") != "active" and f"{name} (eligible)" not in p["privileged_eligible_roles"]:
                p["privileged_eligible_roles"].append(f"{name} (eligible)")
        p["global_admin"] |= r.get("roleName") == "Global Administrator" and r.get("assignment") == "active"
    return out


def _through(p: dict) -> str:
    """ "Through the group Tenant admins": how a member of a role group holds its roles."""
    if not p["groups"]:
        return ""
    return f"Through the group{'s' if len(p['groups']) > 1 else ''} {', '.join(p['groups'])}"


def _admin_role_names(roles: dict[str, dict] | None) -> dict[str, list[str]]:
    return {pid: p["active"] + [f"{n} (eligible)" for n in p["eligible"]] for pid, p in (roles or {}).items()}


def _privileged_names(holders, eligible: bool = False) -> list[str]:
    """The active privileged roles these holders hold (and with ``eligible`` the PIM-eligible ones), named as
    the role columns show them, so a filter on them keeps exactly the rows of people with such a role. Never
    empty: with nobody holding one, the filter still shows no rows rather than every row."""
    names = {n for p in holders for n in p["privileged_roles"] + (p["privileged_eligible_roles"] if eligible else [])}
    return sorted(names) or ["Global Administrator"]


METHOD_NAMES = {
    "microsoftauthenticatorpush": "Authenticator app",
    "microsoftauthenticator": "Authenticator app",
    "microsoftauthenticatorpasswordless": "Authenticator passwordless",
    "softwareonetimepasscode": "Authenticator code",
    "softwareoath": "Authenticator code",
    "hardwareonetimepasscode": "Hardware token",
    "hardwareoath": "Hardware token",
    "mobilephone": "Phone",
    "alternatemobilephone": "Phone",
    "officephone": "Phone",
    "phone": "Phone",
    "fido2securitykey": "Security key",
    "fido2": "Security key",
    "passkeydevicebound": "Passkey",
    "passkeydeviceboundauthenticator": "Passkey",
    "passkey": "Passkey",
    "windowshelloforbusiness": "Windows Hello",
    "email": "Email",
    "temporaryaccesspass": "Temporary pass",
    "x509certificatemultifactor": "Certificate",
    "x509certificate": "Certificate",
    "platformcredential": "Platform credential",
}
PHISHING_RESISTANT = ("fido2", "passkey", "windowshello", "x509", "platformcredential", "macossecureenclave")


def _methods(reg: dict | None) -> list[str]:
    names = []
    for m in (reg or {}).get("methodsRegistered") or []:
        name = METHOD_NAMES.get(m.lower(), m)
        if m.lower() != "password" and name not in names:
            names.append(name)
    return names


def _phishing_resistant(reg: dict | None) -> bool:
    return any(k in m.lower() for m in (reg or {}).get("methodsRegistered") or [] for k in PHISHING_RESISTANT)


class Registration:
    """MFA registration by user ID, and why it might be unknown."""

    def __init__(self, snap: TenantSnapshot):
        rows = snap.objects("mfa_registration", "mfa_registration")
        self.by_id = {r["_id"]: r for r in rows or []}
        self.note = ""
        if rows is None:
            self.note = "MFA registration hasn't been collected yet, so whether people have set up MFA is unknown."
        elif not rows:
            self.note = (
                "Microsoft returned no MFA registration data. This is normal for a day or two after a tenant "
                f"gets {P1}; until it fills in, whether people have set up MFA shows as unknown."
            )

    def registered(self, uid: str) -> bool | None:
        return mfa_set_up(self.by_id.get(uid))

    def get(self, uid: str) -> dict | None:
        return self.by_id.get(uid)


class Enforcement:
    """What requires MFA for an account: Security Defaults, Conditional Access or per-user MFA."""

    def __init__(self, snap: TenantSnapshot):
        defaults = snap.objects("conditional_access", "security_defaults")
        self.policies = snap.objects("conditional_access", "ca_policy")
        self.security_defaults = bool(defaults and defaults[0].get("isEnabled"))
        self.directory = coverage.directory(snap)
        self.p1 = snap.capabilities.get("entra_p1")
        status = (snap.objects("per_user_mfa", "per_user_mfa_status") or [None])[0] or {}
        self.per_user_checked = bool(status.get("checked"))
        self.per_user = {r["_id"]: r.get("state") for r in snap.objects("per_user_mfa", "per_user_mfa") or []}
        self.note = ""
        if defaults is None:
            self.note = "Conditional Access hasn't been collected yet, so whether MFA is required is unknown."
            self.known = False
        elif self.p1 is False and not self.security_defaults and not self.per_user_checked:
            self.note = (
                f"This tenant has no {P1} and Security Defaults are off, so MFA can only be required "
                "per user. Per-user MFA hasn't been read yet; refresh Per-user MFA on the client page."
            )
            self.known = False
        else:
            self.known = True

    def coverage(self, uid: str, label: str, user_type: str) -> coverage.UserCoverage | None:
        if not self.known or self.directory is None or not self.policies:
            return None
        return coverage.coverage_for(uid, label, user_type, self.policies, self.directory)

    def for_user(self, uid: str, label: str, user_type: str) -> tuple[list[str], list[str]]:
        """(what requires MFA, enforced policies the account is excluded from)."""
        sources, excluded = [], []
        if self.security_defaults:
            sources.append("Security Defaults")
        if self.directory is not None and self.policies:
            cov = coverage.coverage_for(uid, label, user_type, self.policies, self.directory)
            sources += cov.mfa_policies
            excluded = cov.excluded_from
        if self.per_user.get(uid) in ("enforced", "enabled"):
            sources.append(f"Per-user MFA ({self.per_user[uid]})")
        return sources, excluded

    def unread(self, uid: str) -> bool:
        """Per-user MFA is the only way this tenant can require MFA, and this account's setting wasn't read
        (a failed read, a new account or over the limit), so whether MFA is required is unknown."""
        return self.per_user_checked and uid not in self.per_user and not self.security_defaults and self.p1 is False

    def describe_rows(self) -> list[tuple[list, str | None]]:
        rows: list[tuple[list, str | None]] = []
        rows.append((["Security Defaults", "On" if self.security_defaults else "Off"], None))
        if self.p1 is False:
            rows.append((["Conditional Access", f"Not available: needs {P1}"], None))
        elif self.policies is not None:
            enforced = [
                coverage.policy_name(p)
                for p in self.policies
                if p.get("state") == "enabled" and coverage.requires_mfa_everywhere(p)
            ]
            report_only = [
                coverage.policy_name(p)
                for p in self.policies
                if p.get("state") == "enabledForReportingButNotEnforced" and "mfa" in coverage.grants(p)
            ]
            rows.append(
                (
                    ["Conditional Access policies requiring MFA", ", ".join(enforced) or "None"],
                    None if enforced or self.security_defaults else "high",
                )
            )
            if report_only:
                rows.append((["Report-only MFA policies (not enforced)", ", ".join(report_only)], "low"))
            legacy = [
                coverage.policy_name(p)
                for p in self.policies
                if p.get("state") == "enabled" and coverage.blocks_legacy(p)
            ]
            rows.append(
                (
                    [
                        "Legacy sign-in blocked by",
                        ", ".join(legacy) or ("Security Defaults" if self.security_defaults else "Nothing"),
                    ],
                    None if legacy or self.security_defaults else "high",
                )
            )
        if self.per_user_checked:
            n = sum(s in ("enforced", "enabled") for s in self.per_user.values())
            rows.append((["Per-user MFA", f"{n} of {plural(len(self.per_user), 'member')}"], None))
        elif self.p1:
            rows.append(
                (["Per-user MFA", "Not read: Conditional Access is the way to require MFA on this tenant"], None)
            )
        return rows


# -- MFA coverage ------------------------------------------------------------------------

GAP_TEXT = {
    "none": ("No MFA", "Not set up and not required: the password alone gets in."),
    "not_required": ("Set up, not required", "Has MFA but isn't asked for it, so a stolen password is enough."),
    "not_registered": (
        "Required, not set up",
        "Will be asked to set up MFA at next sign-in. Until then, anyone "
        "with the password can set it up in their place.",
    ),
    "unknown": ("Unknown", ""),
}


@report(
    "mfa",
    "MFA coverage",
    "Who has set up MFA, who is required to use it, and where the two don't match.",
    ("users", "mfa_registration", "conditional_access", "per_user_mfa", "admin_roles"),
    group="identity",
    order=10,
    keywords="multi-factor two-factor 2fa authenticator authentication methods registration",
)
def mfa(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    users = snap.objects("users", "user")
    if users is None:
        report.unavailable = "User data hasn't been collected yet. Refresh Users on the client page."
        return
    reg = Registration(snap)
    enf = Enforcement(snap)
    held = _roles(snap)
    roles = _admin_role_names(held)
    # Severity follows the admin checks: a privileged role makes a gap an admin's gap, active or PIM-eligible
    # (whoever sets up MFA first can activate it), but not a read-only role such as Directory Readers.
    privileged = {pid for pid, p in (held or {}).items() if p["privileged"]}
    report.notes += [n for n in (reg.note, enf.note) if n]

    how = Section(
        "How MFA is required",
        ["Setting", "Value"],
        intro="Setting up MFA and being required to use it are separate. Both are needed.",
    )
    for row, flag in enf.describe_rows():
        how.add(row, flag)

    gaps = Section(
        "Accounts with a gap",
        ["Account", "Admin roles", "Gap", "MFA set up", "Required by", "Excluded from", "What it means"],
        intro="Enabled members who either haven't set up MFA or aren't required to use it. Admins first.",
        empty="Every enabled member has MFA set up and is required to use it.",
    )
    gaps.chart = Chart("Gap")
    protected = Section(
        "Protected accounts",
        ["Account", "Admin roles", "Methods", "Required by"],
        empty="No account is both set up and required to use MFA.",
    )
    protected.chart = Chart("Methods", title="By method")
    guests = Section(
        "Guests",
        ["Guest", "Required by", "Excluded from"],
        intro="Guests set up MFA in their own organisation, so only whether this tenant requires it is shown.",
        empty="No enabled guest accounts.",
    )
    counts = {"protected": 0, "none": 0, "not_required": 0, "not_registered": 0, "unknown": 0}
    admin_gaps = 0
    gap_rows = []
    members = 0
    for u in sorted(users, key=lambda u: _label(u).lower()):
        if not u.get("accountEnabled"):
            continue
        uid, label, user_type = u["_id"], _label(u), u.get("userType") or "Member"
        sources, excluded = enf.for_user(uid, label, user_type) if enf.known else ([], [])
        required = bool(sources) if enf.known else None
        required_text = _some(sources) or ("Nothing" if enf.known else "Not checked")
        if required is False and enf.unread(uid):
            required, required_text = None, "Not read"
        if user_type == "Guest":
            guests.add([label, required_text, _some(excluded)], "low" if required is False else None)
            continue
        members += 1
        registered = reg.registered(uid)
        admin = uid in privileged
        if registered and required:
            counts["protected"] += 1
            protected.add([label, roles.get(uid), _methods(reg.get(uid)), required_text])
            continue
        if required is False:
            kind = "none" if registered is False else "not_required"
        elif required and registered is False:
            kind = "not_registered"
        else:
            kind = "unknown"
        counts[kind] += 1
        admin_gaps += admin and kind != "unknown"
        severity = {
            "none": "critical" if admin else "high",
            "not_required": "high" if admin else "medium",
            "not_registered": "high" if admin else "medium",
        }.get(kind)
        title, meaning = GAP_TEXT[kind]
        if kind == "not_required" and registered is None:
            title, meaning = "Not required", "Isn't asked for MFA, so a stolen password may be enough."
        set_up = "Unknown" if registered is None else registered
        gap_rows.append(
            (
                (0 if admin else 1, ["critical", "high", "medium", None].index(severity), label.lower()),
                [
                    label,
                    roles.get(uid),
                    title,
                    set_up,
                    required_text,
                    _some(excluded) if kind in ("none", "not_required") else "",
                    meaning,
                ],
                severity,
            )
        )
    for _, row, severity in sorted(gap_rows, key=lambda r: r[0]):
        gaps.add(row, severity)
    report.sections = [how, gaps, protected] + ([guests] if guests.rows else [])

    both = enf.known and not reg.note
    report.highlights = [
        Highlight(
            "Fully protected",
            f"{counts['protected']} of {members}" if both else "Not checked",
            f"{round(100 * counts['protected'] / members)}% of enabled members" if both and members else "",
            ("ok" if counts["protected"] == members else "warn") if both and members else None,
            Rows("Protected accounts"),
        ),
        Highlight(
            "No MFA at all",
            counts["none"] if both else "Not checked",
            "Not set up and not required",
            "bad" if counts["none"] else None,
            Rows("Accounts with a gap", {"Gap": GAP_TEXT["none"][0]}),
        ),
        Highlight(
            "MFA not required",
            counts["none"] + counts["not_required"] if enf.known else "Not checked",
            "A password alone gets in, whether or not MFA is set up",
            "warn" if counts["none"] + counts["not_required"] else None,
            Rows("Accounts with a gap", {"Gap": [GAP_TEXT[k][0] for k in ("none", "not_required")] + ["Not required"]}),
        ),
        Highlight(
            "Required, not set up",
            counts["not_registered"] if both else "Not checked",
            "Anyone with the password can set it up",
            "warn" if counts["not_registered"] else None,
            Rows("Accounts with a gap", {"Gap": GAP_TEXT["not_registered"][0]}),
        ),
        Highlight(
            "Admins with a gap",
            admin_gaps if enf.known else "Not checked",
            "Fix these first",
            "bad" if admin_gaps else None,
            Rows(
                "Accounts with a gap",
                {
                    "Admin roles": _privileged_names((held[pid] for pid in privileged), eligible=True),
                    "Gap": [GAP_TEXT[k][0] for k in ("none", "not_required", "not_registered")] + ["Not required"],
                },
            ),
        ),
    ]
    if counts["unknown"]:
        report.highlights.append(Highlight("Unknown", counts["unknown"], 'See "Not checked" below'))


# -- Admins and privileged roles ------------------------------------------------------------

PIM_GROUPS = "PrivilegedEligibilitySchedule.Read.AzureADGroup"
BREAK_GLASS_NAME = re.compile(r"break.?glass|emergency|\bbg[-_.]?admin|\beaa\b", re.IGNORECASE)


@report(
    "admins",
    "Admins and roles",
    "Every admin role holder with MFA, PIM and sign-in status, and a break-glass check.",
    ("admin_roles", "users", "mfa_registration", "conditional_access"),
    group="identity",
    order=30,
    keywords="administrators global admin privileged roles pim break-glass",
)
def admins(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    roles = _roles(snap)
    if roles is None:
        report.unavailable = "Admin role data hasn't been collected yet. Refresh Admin roles on the client page."
        return
    users = _users(snap) or {}
    reg = Registration(snap)
    enf = Enforcement(snap)
    p2 = snap.capabilities.get("entra_p2")
    report.notes += [n for n in (reg.note, enf.note) if n]
    if p2 is False:
        report.notes.append(
            "PIM (admins activating a role only when they need it) needs Entra ID P2, which this "
            "tenant doesn't have, so admins hold their roles all the time."
        )
    if not any(u.get("signInActivityAvailable") for u in users.values()):
        report.notes.append(f"Last sign-in dates need {P1}, so inactive admins aren't checked.")
    pim_groups = snap.objects("admin_roles", "pim_groups_status") or []
    if pim_groups and isinstance(pim_groups[0], dict) and pim_groups[0].get("state") == "waiting":
        report.notes.append(
            "People who can make themselves a member of a group that holds an admin role (PIM for groups) aren't "
            "listed yet. " + waiting_text(snap.tenant["name"], [PIM_GROUPS])
        )

    privileged = Section(
        "Privileged role holders",
        [
            "Account",
            "Kind",
            "Roles",
            "How assigned",
            "MFA set up",
            "MFA required by",
            "Last sign-in",
            "Synced from on-premises",
            "What to check",
        ],
        intro="Everyone who holds a role that can change security settings, users or data. Problems first.",
        empty="Nobody holds a privileged role.",
        optional_across=("Synced from on-premises",),
    )
    privileged.chart = Chart("Roles", title="By role")
    others = Section(
        "Other roles",
        ["Account", "Kind", "Roles", "How assigned"],
        intro="Roles with narrower or read-only permissions.",
        empty="No other roles are assigned.",
    )
    rows = []
    no_mfa = 0
    for p in roles.values():
        user = users.get(p["id"])
        kind = {
            "user": "Guest" if p["userType"] == "Guest" else "User",
            "group": "Group",
            "servicePrincipal": "App",
        }.get(p["type"], p["type"].title())
        how = " and ".join(x for x, has in (("Permanent", p["active"]), ("Eligible (PIM)", p["eligible"])) if has)
        role_text = p["active"] + [f"{n} (eligible)" for n in p["eligible"]]
        if not p["privileged"]:
            others.add([p["label"], kind, role_text, how])
            continue
        problems: list[tuple[str, str]] = []
        registered = reg.registered(p["id"]) if p["type"] == "user" else None
        required_text = ""
        if p["type"] == "user":
            if p["enabled"] is False or (user and not user.get("accountEnabled")):
                problems.append(("low", "Disabled account still holds the role"))
            else:
                if registered is False:
                    no_mfa += 1
                    problems.append(("critical", "No MFA set up"))
                if enf.known:
                    sources, _ = enf.for_user(p["id"], p["label"], p["userType"])
                    required_text = _some(sources) or "Nothing"
                    if not sources:
                        problems.append(("high", "MFA isn't required"))
                else:
                    required_text = "Not checked"
                if p["userType"] == "Guest":
                    problems.append(("high", "Guest account with an admin role"))
                if user and user.get("onPremisesSyncEnabled") and p["privileged_active"]:
                    problems.append(("medium", "Synced from on-premises: admins should be cloud-only"))
                if user and is_stale(user, now):
                    problems.append(("medium", f"No sign-in for {STALE_DAYS}+ days"))
        elif p["type"] == "servicePrincipal":
            problems.append(("medium", "App with an admin role: a leaked secret bypasses MFA"))
        elif p["type"] == "group":
            problems.append(("low", "Role given to a group: check who can add members"))
        severity = min((s for s, _ in problems), key=["critical", "high", "medium", "low"].index, default=None)
        rows.append(
            (
                (
                    ["critical", "high", "medium", "low", None].index(severity),
                    not p["global_admin"],
                    p["label"].lower(),
                ),
                [
                    p["label"],
                    kind,
                    role_text,
                    how,
                    ("Unknown" if registered is None else registered) if p["type"] == "user" else "",
                    required_text,
                    _sign_in(user) if p["type"] == "user" else "",
                    (bool(user.get("onPremisesSyncEnabled")) if user else "Unknown") if p["type"] == "user" else "",
                    "; ".join([t for _, t in problems] + ([_through(p)] if p["groups"] else [])) or "Nothing",
                ],
                severity,
            )
        )
    for _, row, severity in sorted(rows, key=lambda r: r[0]):
        privileged.add(row, severity)

    glass = _break_glass(roles, users, reg, enf)
    report.sections = [privileged, glass, others]

    people = [p for p in roles.values() if p["privileged_active"] and p["type"] == "user"]
    # Matches the check: enabled people only, not apps, groups or disabled accounts.
    gas = sum(p["global_admin"] for p in roles.values() if p["type"] == "user" and p["enabled"] is not False)
    eligible = {p["id"] for p in roles.values() if p["eligible"]}
    person = ["User", "Guest"]
    report.highlights = [
        Highlight(
            "Privileged accounts",
            len(people),
            "People with a permanent admin role",
            rows=Rows(privileged.title, {"Kind": person, "Roles": _privileged_names(people)}),
        ),
        Highlight(
            "Global Administrators",
            gas,
            "Keep between 2 and 4",
            None if 2 <= gas <= 4 else "warn",
            # A disabled holder is the only person flagged low: leave them out, as the figure does.
            Rows(
                privileged.title,
                {
                    "Kind": person,
                    "Roles": "Global Administrator",
                    privileged.flag_name: ["critical", "high", "medium", "none"],
                },
            ),
            metric_trend(snap, "admins.global_admins", now),
        ),
        Highlight(
            "Admins without MFA",
            no_mfa if not reg.note else "Not checked",
            "Not set up at all",
            "bad" if no_mfa else None,
            Rows(privileged.title, {"MFA set up": "No"}, flagged=True),  # not disabled holders, flagged low
        ),
        Highlight(
            "Break-glass accounts",
            len(glass.rows) or "None found",
            "Emergency accounts kept out of Conditional Access",
            None if glass.rows else "warn",
        ),
        Highlight(
            "Using PIM",
            "Not checked" if p2 is False else (f"{len(eligible)} eligible" if eligible else "No"),
            "Needs Entra ID P2" if p2 is False else "Roles activated only when needed",
            None if p2 is False or eligible else "warn",
        ),
    ]


def _break_glass(roles: dict[str, dict], users: dict[str, dict], reg: Registration, enf: Enforcement) -> Section:
    section = Section(
        "Break-glass accounts",
        [
            "Account",
            "Why we think it's break-glass",
            "Cloud-only",
            "On the .onmicrosoft.com domain",
            "Excluded from",
            "Sign-in methods",
            "Last sign-in",
            "What to check",
        ],
        intro="Emergency Global Administrator accounts for when normal sign-in breaks. Microsoft "
        "recommends two, cloud-only, excluded from Conditional Access, signing in with a "
        "FIDO2 key or passkey kept somewhere safe.",
        empty="No break-glass account found: no Global Administrator is excluded from Conditional "
        "Access or named like one. Without one, a broken policy can lock every admin out.",
    )
    candidates = {
        pid: p for pid, p in roles.items() if p["type"] == "user" and p["global_admin"] and p["enabled"] is not False
    }
    for uid, u in users.items():  # named like one but without the role: worth saying
        if (
            uid not in candidates
            and u.get("accountEnabled")
            and u.get("userType") != "Guest"
            and BREAK_GLASS_NAME.search(f"{_label(u)} {u.get('displayName') or ''}")
        ):
            candidates[uid] = {"id": uid, "label": _label(u), "userType": "Member", "global_admin": False}
    for p in sorted(candidates.values(), key=lambda p: p["label"].lower()):
        cov = enf.coverage(p["id"], p["label"], p["userType"])
        excluded = cov.excluded_from if cov else []
        why = []
        if BREAK_GLASS_NAME.search(f"{p['label']} {(users.get(p['id']) or {}).get('displayName') or ''}"):
            why.append("Named like one")
        if excluded and not cov.policies:  # an admin excluded from one policy but covered by another isn't one
            why.append("Excluded from Conditional Access")
        if not why:
            continue
        user = users.get(p["id"]) or {}
        row = reg.get(p["id"])
        problems = []
        if not p["global_admin"]:
            problems.append(("high", "Doesn't hold Global Administrator, so it can't recover the tenant"))
        if user.get("onPremisesSyncEnabled"):
            problems.append(("high", "Synced from on-premises: make it cloud-only"))
        if reg.registered(p["id"]) is False:
            problems.append(("high", "No MFA method: Microsoft requires MFA for admin portals, so it may not get in"))
        elif row is not None and not _phishing_resistant(row):
            problems.append(("medium", "Uses app or phone MFA: a FIDO2 key or passkey is safer"))
        on_ms_domain = p["label"].lower().endswith(".onmicrosoft.com")
        if not on_ms_domain:
            problems.append(("low", "Use the .onmicrosoft.com domain so it works if a custom domain breaks"))
        if cov and cov.policies:
            problems.append(
                ("medium", f"Conditional Access applies ({', '.join(cov.policies)}): a broken policy could lock it out")
            )
        severity = min((s for s, _ in problems), key=["critical", "high", "medium", "low"].index, default=None)
        section.add(
            [
                p["label"],
                why,
                not user.get("onPremisesSyncEnabled") if user else "Unknown",
                on_ms_domain,
                _some(excluded),
                _methods(row) or ("None" if row is not None else "Unknown"),
                _sign_in(user or None),
                "; ".join(t for _, t in problems) or "Nothing",
            ],
            severity,
        )
    return section


# -- Licence waste ---------------------------------------------------------------------------


def _kept_note(n: int, left_out: str) -> str:
    """Shared mailboxes over 50 GB, with an archive or on hold need a licence, though nobody signs in to them."""
    what = "holds a licence it needs" if n == 1 else "hold licences they need"
    return (
        f"{plural(n, 'shared mailbox', 'shared mailboxes')} {what} (over 50 GB, with an archive or on hold), "
        f"so {verb(n, 'it is not', 'they are not')} {left_out}."
    )


@report(
    "licences",
    "Licence waste",
    "Paid licences nobody uses, in money, and the accounts holding them.",
    ("tenant_profile", "users", "mailboxes", "mailbox_usage"),
    group="tenant",
    order=30,
    keywords="licenses waste cost money unused subscriptions",
)
def licences(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    skus = snap.objects("tenant_profile", "subscribed_sku")
    if skus is None:
        report.unavailable = "Licence data hasn't been collected yet. Refresh Tenant profile and licences."
        return
    users = snap.objects("users", "user")
    currency = setting(snap.conn, "currency", "GBP")
    subscriptions = snap.objects("tenant_profile", "company_subscription")
    keep = shared_needing_licence(snap)
    summary = licence_summary(skus, users, load_prices(snap.conn), now, subscriptions, keep)
    paid = {line.sku_id: line for line in summary.lines if not line.free}

    def cash(value: float | None) -> object:
        return "Not priced" if value is None else Money(value, currency)

    if users is None:
        report.notes.append(
            "User data hasn't been collected yet, so licences on disabled or inactive accounts aren't counted."
        )
    elif not summary.stale_available:
        report.notes.append(
            f"Sign-in dates need {P1}, so licences on inactive accounts aren't counted. "
            "Licences on disabled accounts and unassigned licences are."
        )
    if summary.kept:
        report.notes.append(_kept_note(summary.kept, "counted as waste"))
    if any(line.monthly_price is None for line in paid.values()):
        report.notes.append(
            "Some licences have no price, so their waste is counted in licences rather than money. "
            "An admin can add prices on the Licence prices page."
        )

    subs = Section(
        "Waste by subscription",
        [
            "Licence",
            "Purchased",
            "Assigned",
            "Unassigned",
            "On disabled accounts",
            "On inactive accounts",
            "Price / month",
            "Waste / month",
            "Waste / year",
        ],
        intro="Paid licences nobody is using: unassigned, or assigned to disabled or inactive accounts. "
        "Free and trial licences are left out.",
        empty="No paid subscriptions.",
    )
    subs.chart = Chart("Licence", ("Unassigned", "On disabled accounts", "On inactive accounts"))
    for line in paid.values():
        waste = line.monthly_waste
        subs.add(
            [
                line.name,
                line.purchased,
                line.assigned,
                line.unassigned,
                line.on_disabled_users,
                line.on_stale_users if summary.stale_available else "Not checked",
                cash(line.monthly_price),
                cash(waste) if line.wasted_units else Money(0, currency) if line.monthly_price is not None else "",
                cash(waste * 12) if waste else "",
            ],
            ("medium" if waste else "low") if line.wasted_units else None,
        )

    accounts = Section(
        "Accounts to review",
        ["Account", "Status", "Last sign-in", "Licences", "Cost / month"],
        intro=f"Disabled accounts, and accounts with no sign-in for {STALE_DAYS} days, that still hold "
        "paid licences. Most expensive first.",
        empty="No disabled or inactive account holds a paid licence.",
    )
    overlaps = Section(
        "Overlapping licences",
        ["Account", "Has", "Already included in", "Cost / month"],
        intro="People holding a licence whose features are all included in another licence they already have.",
        empty="Nobody holds overlapping licences.",
    )
    plans = {
        s["skuId"]: {p.get("servicePlanId") for p in s.get("servicePlans") or [] if p.get("servicePlanId")}
        for s in skus
        if s.get("skuId")
    }
    account_rows = []
    for u in users or []:
        held = [sid for sid in dict.fromkeys(u.get("skuIds") or []) if sid in paid]
        if not held:
            continue
        names = [paid[sid].name for sid in held]
        prices = [paid[sid].monthly_price for sid in held]
        total = sum(p for p in prices if p is not None) if any(p is not None for p in prices) else None
        status = None
        if u["_id"] in keep:
            pass  # a shared mailbox that needs its licence
        elif not u.get("accountEnabled"):
            status = "Disabled"
        elif is_stale(u, now):
            status = "Never signed in" if last_sign_in(u) is None else "Inactive"
        if status:
            account_rows.append(
                (
                    (-(total or 0), _label(u).lower()),
                    [_label(u), status, _sign_in(u), names, cash(total)],
                    "medium" if status == "Disabled" or total else "low",
                )
            )
        for a in held:
            for b in held:
                if a == b or not plans.get(a) or not plans[a] <= plans.get(b, set()):
                    continue
                if plans[a] == plans[b] and a > b:
                    continue  # identical plans: report the pair once
                overlaps.add([_label(u), paid[a].name, paid[b].name, cash(paid[a].monthly_price)], "low")
    for _, row, flag in sorted(account_rows, key=lambda r: r[0]):
        accounts.add(row, flag)
    report.sections = [subs, accounts, overlaps]

    wasted = sum(line.wasted_units for line in paid.values())
    priced = any(line.monthly_price is not None for line in paid.values())
    unpriced = sum(line.wasted_units for line in paid.values() if line.monthly_price is None)
    report.highlights = [
        Highlight(
            "Waste per month",
            Money(summary.monthly_waste, currency) if priced else "Not priced",
            f"{plural(unpriced, 'unpriced licence')} not included" if unpriced and priced else "Unused paid licences",
            "warn" if summary.monthly_waste else None,
            WASTE_ROWS,
        ),
        Highlight(
            "Waste per year",
            Money(summary.monthly_waste * 12, currency) if priced else "Not priced",
            "If nothing changes",
            rows=WASTE_ROWS,
        ),
        Highlight(
            "Unused licences",
            wasted,
            f"{sum(x.unassigned for x in paid.values())} unassigned, "
            f"{sum(x.on_disabled_users for x in paid.values())} on disabled, "
            f"{sum(x.on_stale_users for x in paid.values()) if summary.stale_available else 'unknown'} "
            "on inactive accounts",
            "warn" if wasted else "ok",
            WASTE_ROWS,
        ),
        Highlight(
            "Overlapping licences",
            len(overlaps.rows),
            "Features already included elsewhere",
            "warn" if overlaps.rows else None,
        ),
    ]


WASTE_ROWS = Rows("Waste by subscription")

# -- Inactive accounts ------------------------------------------------------------------------


def _inactive_for(user: dict, now: datetime) -> tuple[str, int | None]:
    last = last_sign_in(user)
    if last is None:
        created = _parse(user.get("createdDateTime"))
        days = (now - created).days if created else None
        return "Never signed in", days
    days = (now - last).days
    if days >= 365:
        bucket = "Over a year"
    elif days >= 180:
        bucket = "6 to 12 months"
    else:
        bucket = f"{STALE_DAYS} days to 6 months"
    return bucket, days


@report(
    "inactive",
    "Inactive accounts",
    "Members and guests who haven't signed in for a long time, and old invitations.",
    ("users", "admin_roles", "tenant_profile", "mailboxes", "mailbox_usage"),
    group="identity",
    order=50,
    keywords="stale dormant leavers unused accounts sign-in last login",
)
def inactive(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    users = snap.objects("users", "user")
    if users is None:
        report.unavailable = "User data hasn't been collected yet. Refresh Users on the client page."
        return
    activity = any(u.get("signInActivityAvailable") for u in users)
    roles = _admin_role_names(_roles(snap))
    skus = {
        s["skuId"]: s.get("skuPartNumber")
        for s in snap.objects("tenant_profile", "subscribed_sku") or []
        if s.get("skuId")
        and s.get("skuPartNumber") not in FREE_SKUS
        and ((s.get("prepaidUnits") or {}).get("enabled") or 0) < FREE_UNIT_THRESHOLD
    }
    if not activity:
        report.notes.append(
            f"Sign-in dates need {P1}, so inactive members and guests aren't checked. "
            "Unaccepted invitations and disabled accounts still holding licences or roles are listed."
        )

    rule = (
        f"Enabled accounts with no sign-in for {STALE_DAYS} days, or that have never signed in and were created "
        f"more than {NEVER_SIGNED_IN_GRACE} days ago."
    )
    members = Section(
        "Inactive members",
        [
            "Account",
            "Inactive for",
            "Last sign-in",
            "Days",
            "Licences",
            "Admin roles",
            "Created",
            "Synced from on-premises",
        ],
        intro=rule + " Admins and licensed accounts first.",
        empty="No inactive members.",
    )
    members.chart = Chart("Inactive for", title="By time since last sign-in")
    guests = Section(
        "Inactive guests",
        ["Guest", "Inactive for", "Last sign-in", "Days", "Invitation", "Invited"],
        intro=rule,
        empty="No inactive guests.",
    )
    pending = Section(
        "Invitations never accepted",
        ["Guest", "Invited", "Days waiting"],
        intro="Guest invitations still waiting after 30 days. Old invitations can be redeemed by "
        "whoever finds the link.",
        empty="No old unaccepted invitations.",
    )
    disabled = Section(
        "Disabled accounts still holding licences or roles",
        ["Account", "Licences", "Admin roles", "Last sign-in"],
        intro="Disabled accounts can't sign in, but their licences still cost money and their roles "
        "come back if someone re-enables them.",
        empty="None.",
    )
    member_rows, guest_rows = [], []
    counts = {"members": 0, "licensed": 0, "guests": 0}
    keep, kept = shared_needing_licence(snap), 0
    for u in users:
        uid, label = u["_id"], _label(u)
        licences = [sku_name(skus[s]) for s in u.get("skuIds") or [] if s in skus]  # paid ones only
        guest = u.get("userType") == "Guest"
        if not u.get("accountEnabled"):
            if uid in keep and licences:  # a shared mailbox that needs its licence
                licences, kept = [], kept + 1
            if licences or uid in roles:
                disabled.add([label, licences, roles.get(uid), _sign_in(u)], "medium" if uid in roles else "low")
            continue
        created = _parse(u.get("createdDateTime"))
        if (
            guest
            and u.get("externalUserState") == "PendingAcceptance"
            and created
            and created < now - timedelta(days=30)
        ):
            pending.add([label, _day(created), (now - created).days], "low")
        if not activity or not is_stale(u, now):
            continue
        bucket, days = _inactive_for(u, now)
        last = last_sign_in(u)
        if guest:
            counts["guests"] += 1
            guest_rows.append(
                (
                    -(days or 0),
                    [
                        label,
                        bucket,
                        _day(last) or never_signed_in(u),
                        days,
                        "Not accepted" if u.get("externalUserState") == "PendingAcceptance" else "Accepted",
                        _day(created),
                    ],
                    "low",
                )
            )
            continue
        counts["members"] += 1
        counts["licensed"] += bool(licences)
        severity = "high" if uid in roles else ("medium" if licences else "low")
        member_rows.append(
            (
                (["high", "medium", "low"].index(severity), -(days or 0)),
                [
                    label,
                    bucket,
                    _day(last) or never_signed_in(u),
                    days,
                    licences,
                    roles.get(uid),
                    _day(created),
                    u.get("onPremisesSyncEnabled"),
                ],
                severity,
            )
        )
    for _, row, flag in sorted(member_rows, key=lambda r: r[0]):
        members.add(row, flag)
    for _, row, flag in sorted(guest_rows, key=lambda r: r[0]):
        guests.add(row, flag)
    if kept:
        disabled.intro += " " + _kept_note(kept, "listed")
    report.sections = ([members, guests] if activity else []) + [pending, disabled]

    def figure(n: int) -> object:
        return n if activity else "Not checked"

    report.highlights = [
        Highlight(
            "Inactive members",
            figure(counts["members"]),
            f"{counts['licensed']} with a paid licence" if activity else f"Needs {P1}",
            "warn" if counts["members"] else None,
        ),
        Highlight(
            "Inactive guests",
            figure(counts["guests"]),
            f"No sign-in for {STALE_DAYS}+ days, or never" if activity else f"Needs {P1}",
            "warn" if counts["guests"] else None,
        ),
        Highlight(
            "Invitations never accepted", len(pending.rows), "Older than 30 days", "warn" if pending.rows else None
        ),
        Highlight(
            "Disabled but still assigned",
            len(disabled.rows),
            "Licences or admin roles",
            "warn" if disabled.rows else None,
            Rows(disabled.title),
        ),
    ]


def _level(value: str | None) -> str:
    """Microsoft mixes 'low' and 'Low', and says 'Unknown' for most device controls."""
    return "" if not value or value.lower() == "unknown" else value.capitalize()


def _num(value: float) -> int | float:
    """9.0 -> 9, 2.5 -> 2.5: whole numbers without a trailing .0, still numbers in the XLSX."""
    value = round(float(value), 2)
    return int(value) if value.is_integer() else value


# -- Secure Score trend --------------------------------------------------------------------------


@report(
    "secure-score",
    "Secure Score trend",
    "Secure Score over time, which controls changed, and where the points are.",
    ("secure_score",),
    group="tenant",
    order=10,
    keywords="score trend controls",
)
def secure_score(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    if why := clouds.missing(clouds.of(snap.tenant), "secure_score"):
        report.unavailable = why
        return
    daily = snap.objects("secure_score", "secure_score_daily")
    if daily is None:
        report.unavailable = "Secure Score hasn't been collected yet. Refresh Secure Score on the client page."
        return
    daily = [d for d in daily if d.get("maxScore") and parse_day(d.get("date"))]
    if not daily:
        report.unavailable = "Microsoft hasn't produced a Secure Score for this tenant yet. It appears within a day."
        return
    latest = max(daily, key=lambda d: d["date"])
    current, maximum = latest.get("currentScore") or 0, latest["maxScore"]
    pct = round(100 * current / maximum, 1)
    points = _secure_score_points(snap)
    latest_day = parse_day(latest["date"])

    def change(days: int) -> tuple[float | None, date | None]:
        earlier = [(d, v) for d, v in points if d <= latest_day - timedelta(days=days)]
        return (round(pct - earlier[-1][1], 1), earlier[-1][0]) if earlier else (None, None)

    d30, since30 = change(30)
    d90, since90 = change(90)
    controls = snap.objects("secure_score", "secure_score_control") or []
    profiles = {p["_id"]: p for p in snap.objects("secure_score", "secure_score_control_profile") or []}
    past = snap.objects("secure_score", "secure_score_control_past") or []

    def title(name: str) -> str:
        return (profiles.get(name) or {}).get("title") or name

    report.chart = line_chart(points, label="Secure Score over time")
    if not profiles:
        report.notes.append(
            "Microsoft didn't return control names and maximum points, so controls show their "
            "internal names and the points still available aren't known."
        )

    changes = Section(
        "Changes in the last 30 days",
        ["Control", "Category", "Points then", "Points now", "Change"],
        intro=f"Compared with {_day(past[0]['date'])}. Drops first." if past else "",
        empty="No control changed."
        if past
        else "Microsoft has only one day of Secure Score history for this tenant, so there's nothing to "
        "compare with yet.",
    )
    past = [p for p in past if p.get("controlName")]
    controls = [c for c in controls if c.get("controlName")]
    then = {p["controlName"]: p.get("score") or 0 for p in past}
    now_scores = {c["controlName"]: c.get("score") or 0 for c in controls}
    category = {c["controlName"]: c.get("controlCategory") for c in controls}
    rows = []
    if past:
        for name in set(then) | set(now_scores):
            before, after = then.get(name), now_scores.get(name)
            if before is None or after is None:
                continue  # control added or retired by Microsoft, not a change in the tenant
            delta = round(after - before, 2)
            if delta:
                rows.append(
                    (
                        delta,
                        [title(name), category.get(name), _num(before), _num(after), f"{delta:+g}"],
                        "medium" if delta < 0 else None,
                    )
                )
    for _, row, flag in sorted(rows, key=lambda r: r[0]):
        changes.add(row, flag)

    available = Section(
        "Where the points are",
        ["Control", "Category", "Points now", "Most possible", "Points available", "User impact", "Effort"],
        intro="Controls that aren't fully in place, biggest gain first.",
        empty="Every control is fully in place.",
    )
    open_rows, remaining = [], 0.0
    for c in controls:
        profile = profiles.get(c["controlName"]) or {}
        if profile.get("deprecated"):
            continue
        most = profile.get("maxScore")
        score = c.get("score") or 0
        if most is not None:
            gap = round(most - score, 2)
            if gap <= 0:
                continue
            remaining += gap
        elif (c.get("scoreInPercentage") or 0) >= 100:
            continue
        else:
            gap = None
        open_rows.append(
            (
                -(gap or 0),
                [
                    title(c["controlName"]),
                    c.get("controlCategory"),
                    _num(score),
                    "" if most is None else _num(most),
                    "Unknown" if gap is None else _num(gap),
                    _level(profile.get("userImpact")),
                    _level(profile.get("implementationCost")),
                ],
            )
        )
    for _, row in sorted(open_rows, key=lambda r: r[0]):
        available.add(row)

    months = Section(
        "Month by month",
        ["Month", "Secure Score", "Change"],
        compare=False,
        intro="The last score of each month. Microsoft keeps 90 days; Office Sentry keeps every day it has collected.",
    )
    by_month: dict[tuple[int, int], float] = {}
    for d, v in points:
        by_month[(d.year, d.month)] = v
    ordered = sorted(by_month.items())
    for i, ((y, m), v) in reversed(list(enumerate(ordered))):
        prev = ordered[i - 1][1] if i else None
        label = f"{date(y, m, 1):%B %Y}" + (" (so far)" if (y, m) == (now.year, now.month) else "")
        change = "" if prev is None else (f"{round(v - prev, 1):+g}" if round(v - prev, 1) else "No change")
        months.add([label, f"{v:g}%", change])
    report.sections = [changes, available, months]

    def trend(label: str, delta: float | None, since: date | None) -> Highlight:
        if delta is None:
            return Highlight(label, "Not enough history", "Builds up as Office Sentry collects")
        return Highlight(
            label,
            f"{delta:+g} points",
            f"Since {_day(datetime.combine(since, datetime.min.time()))}",
            None if not delta else ("ok" if delta > 0 else "bad"),
            Rows(months.title),
        )

    report.highlights = [
        Highlight(
            "Secure Score",
            f"{pct:g}%",
            f"{current:g} of {maximum:g} points",
            rows=Rows(months.title),
            trend=metric_trend(snap, "secure_score.pct", now, lambda v: f"{v:g}%"),
        ),
        trend("Change in 30 days", d30, since30),
        trend("Change in 90 days", d90, since90),
        Highlight(
            "Points available",
            f"{remaining:g}" if profiles else "Unknown",
            f"From {plural(len(open_rows), 'control')} not fully in place",
            rows=Rows(available.title),
        ),
        Highlight(
            "History",
            plural(len(points), "day"),
            f"Since {_day(datetime.combine(points[0][0], datetime.min.time()))}" if points else "",
        ),
    ]


# -- Registry ----------------------------------------------------------------------------------
