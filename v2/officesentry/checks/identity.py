"""Checks on MFA, admins, accounts and their licences, and Conditional Access coverage."""

from __future__ import annotations

from datetime import datetime

from .. import coverage
from ..licences import is_stale, last_sign_in, licence_summary, load_prices
from ..snapshot import TenantSnapshot
from ..text import day, every, plural, verb
from .base import Finding, _all_are, _label, _names, _unavailable, rule


def _enabled(policy: dict) -> bool:
    return policy.get("state") == "enabled"


def _all_users_all_apps(policy: dict) -> bool:
    cond = policy.get("conditions") or {}
    users = cond.get("users") or {}
    apps = cond.get("applications") or {}
    return "All" in (users.get("includeUsers") or []) and "All" in (apps.get("includeApplications") or [])


_grants = coverage.grants


def mfa_for_all_policies(policies: list[dict]) -> list[dict]:
    """Enabled policies requiring MFA for all users on every sign-in (not only risky or located ones)."""
    return [p for p in policies if _enabled(p) and _all_users_all_apps(p) and coverage.requires_mfa_everywhere(p)]


def _no_p1(snap: TenantSnapshot) -> bool:
    return snap.capabilities.get("entra_p1") is False


# Advice for tenants without Entra ID P1, which can't use Conditional Access.
SECURITY_DEFAULTS_ADVICE = (
    "This tenant has no Entra ID P1, so Conditional Access isn't available. Turn on Security Defaults instead "
    "(Entra admin centre > Overview > Properties > Manage security defaults): they require MFA for everyone "
    "and block legacy authentication. Or add Entra ID P1 (included in Microsoft 365 Business Premium) to use "
    "Conditional Access."
)


def _ca_data(snap: TenantSnapshot):
    defaults = snap.objects("conditional_access", "security_defaults")
    policies = snap.objects("conditional_access", "ca_policy")
    if defaults is None:
        return None, None
    enabled = bool(defaults and defaults[0].get("isEnabled"))
    return enabled, policies or []


# -- MFA, admins, accounts and licences -------------------------------------------


@rule(
    "mfa_enforced",
    "MFA is enforced for all users",
    "Identity",
    "Create a Conditional Access policy requiring MFA for all users and all cloud apps, or turn on "
    "Security Defaults if the tenant has no Entra ID P1. Keep exclusions to break-glass accounts.",
)
def mfa_enforced(snap: TenantSnapshot, now: datetime):
    defaults, policies = _ca_data(snap)
    if defaults is None:
        return _unavailable("Conditional Access data hasn't been collected yet")
    if defaults:
        return ("pass", "Security Defaults are on, which require MFA registration and enforce it", [])
    if _no_p1(snap):
        return (
            "fail",
            "MFA is not enforced",
            [
                Finding(
                    "high", "Tenant", "Security Defaults are off, and without Entra ID P1 there's no Conditional Access"
                )
            ],
            SECURITY_DEFAULTS_ADVICE,
        )
    covering = mfa_for_all_policies(policies)
    if not covering:
        report_only = [
            p for p in policies if p.get("state") == "enabledForReportingButNotEnforced" and "mfa" in _grants(p)
        ]
        detail = "No enabled policy requires MFA for all users and apps, and Security Defaults are off."
        if left_out := coverage.apps_left_out(policies, "mfa", _names(snap)):
            detail += f" {left_out}"
        if optional := coverage.mfa_optional(policies):
            detail += f" {optional}"
        if report_only:
            n = len(report_only)
            detail += f" {plural(n, 'MFA policy')} {verb(n, 'is', 'are')} in report-only mode."
        return ("fail", "MFA is not enforced", [Finding("high", "Tenant", detail)])
    names = _names(snap)
    roles = {
        r.get("templateId") or r["_id"]: r.get("displayName")
        for r in snap.objects("admin_roles", "role_definition") or []
    }
    # One finding per excluded account, group, role or guests, naming every policy it's excluded from.
    excluded: dict[str, tuple[str, str, list[str]]] = {}
    for p in covering:
        spec = (p.get("conditions") or {}).get("users") or {}
        name = p.get("displayName", "?")
        for uid in spec.get("excludeUsers") or []:
            if uid == "GuestsOrExternalUsers":
                excluded.setdefault("guests", ("Guests and external users", "Excluded from", []))[2].append(name)
            else:
                excluded.setdefault(uid, (names.get(uid, uid), "Excluded from", []))[2].append(name)
        if spec.get("excludeGuestsOrExternalUsers"):
            excluded.setdefault("guests", ("Guests and external users", "Excluded from", []))[2].append(name)
        for gid in spec.get("excludeGroups") or []:
            excluded.setdefault(gid, (names.get(gid, f"Group {gid}"), "Group excluded from", []))[2].append(name)
    findings = [
        Finding("medium", label, f"{what} " + ", ".join(f'"{n}"' for n in pols), oid)
        for oid, (label, what, pols) in excluded.items()
    ]
    findings += _role_exclusions(snap, covering, policies, roles, names)
    policy_names = ", ".join(coverage.policy_name(p) for p in covering)
    if findings:
        return ("fail", f"Enforced by {policy_names}, with {plural(len(findings), 'exclusion')} to review", findings)
    return ("pass", f"Enforced by {policy_names}", [])


def _role_exclusions(
    snap: TenantSnapshot, covering: list[dict], policies: list[dict], roles: dict, names: dict[str, str]
) -> list[Finding]:
    """A finding per MFA policy that leaves out roles whose holders no other policy requires MFA of.

    Excluding admin roles from the all-users policy is common when a stricter admin policy covers them,
    so a role exclusion is only a gap for the people it actually leaves without MFA.
    """
    d = coverage.directory(snap)
    if d is None:
        return []
    users = {u["_id"]: u for u in snap.objects("users", "user") or []}
    out = []
    for p in covering:
        spec = (p.get("conditions") or {}).get("users") or {}
        excluded = set(spec.get("excludeRoles") or [])
        if not excluded:
            continue
        by_name = set(spec.get("excludeUsers") or [])  # listed on their own already
        uncovered = sorted(
            names.get(uid, uid)
            for uid, held in d.user_roles.items()
            if held & excluded
            and uid not in by_name
            and (users.get(uid) or {}).get("accountEnabled") is not False
            and not coverage.coverage_for(uid, names.get(uid, uid), "Member", policies, d).mfa
        )
        if uncovered:
            left_out = ", ".join(roles.get(r) or f"role {r[:8]}" for r in sorted(excluded))
            out.append(
                Finding(
                    "medium",
                    coverage.policy_name(p),
                    f"Leaves out {left_out}, so {', '.join(uncovered)} "
                    + verb(len(uncovered), "isn't", "aren't")
                    + " required to use MFA by any policy",
                    f"roles:{p.get('id')}",
                )
            )
    return out


@rule(
    "legacy_auth_blocked",
    "Legacy authentication is blocked",
    "Identity",
    "Block legacy authentication (Exchange ActiveSync and other clients) with a Conditional Access policy "
    "for all users. Legacy protocols can't do MFA and are the most common password-spray target.",
)
def legacy_auth_blocked(snap: TenantSnapshot, now: datetime):
    defaults, policies = _ca_data(snap)
    if defaults is None:
        return _unavailable("Conditional Access data hasn't been collected yet")
    if defaults:
        return ("pass", "Security Defaults block legacy authentication", [])
    if _no_p1(snap):
        return (
            "fail",
            "Legacy authentication is allowed",
            [
                Finding(
                    "high", "Tenant", "Security Defaults are off, and without Entra ID P1 there's no Conditional Access"
                )
            ],
            SECURITY_DEFAULTS_ADVICE,
        )
    for p in policies:
        if _enabled(p) and _all_users_all_apps(p) and coverage.blocks_legacy(p):
            return ("pass", f'Blocked by "{p.get("displayName")}"', [])
    detail = "No enabled policy blocks legacy authentication for all users"
    if left_out := coverage.apps_left_out(policies, "block", _names(snap)):
        detail += f". {left_out}"
    return ("fail", "Legacy authentication is allowed", [Finding("high", "Tenant", detail)])


def _privileged_users(snap: TenantSnapshot, eligible: bool = False) -> list[dict] | None:
    """Privileged role assignments with a principal: active ones, and PIM-eligible ones too when asked.

    Members of a group that holds a role have rows of their own, marked with the group (``viaGroup``).
    """
    rows = snap.objects("admin_roles", "role_assignment")
    if rows is None:
        return None
    return [
        dict(r, roleName=r.get("roleName") or "Unknown role")
        for r in rows
        if r.get("privileged") and (eligible or r.get("assignment") == "active") and r.get("principalId")
    ]


def _role(a: dict) -> str:
    """ "Global Administrator", "Global Administrator through Tenant admins", and "(eligible)" under PIM."""
    text = a.get("roleName") or "Unknown role"
    if a.get("viaGroup"):
        text += f" through {a['viaGroup']}"
    return f"{text} (eligible)" if a.get("assignment") == "eligible" else text


def _by_principal(admins: list[dict]) -> dict[str, list[dict]]:
    """Assignments grouped by who holds them, so one account with several roles is one finding naming all."""
    out: dict[str, list[dict]] = {}
    for a in admins:
        out.setdefault(a["principalId"], []).append(a)
    return out


def _roles_text(assignments: list[dict]) -> str:
    texts = []
    for a in assignments:
        if (t := _role(a)) not in texts:
            texts.append(t)
    return ", ".join(texts)


@rule(
    "admin_mfa",
    "Every admin has MFA registered",
    "Admins",
    "Have each listed admin register a strong method (Authenticator or a FIDO2 key), or remove the role. "
    "Break-glass accounts should use FIDO2 keys kept offline.",
)
def admin_mfa(snap: TenantSnapshot, now: datetime):
    admins = _privileged_users(snap)
    reg = snap.objects("mfa_registration", "mfa_registration")
    if admins is None or reg is None:
        return _unavailable("Needs admin role and MFA registration data")
    if not reg:
        return _unavailable("Microsoft returned no MFA registration data yet")
    registered = {r["_id"]: r.get("isMfaRegistered") for r in reg}
    findings, seen = [], set()
    for a in admins:
        pid = a["principalId"]
        if a.get("principalType") != "user" or pid in seen or a.get("accountEnabled") is False:
            continue
        seen.add(pid)
        if registered.get(pid) is False:
            findings.append(Finding("critical", _label(a), f"{_role(a)} with no MFA method registered", pid))
        elif pid not in registered:
            findings.append(Finding("low", _label(a), f"{_role(a)}: MFA registration unknown", pid))
    missing = sum(f.severity == "critical" for f in findings)
    unknown = len(findings) - missing
    if missing:
        extra = f", and {unknown} whose registration isn't known" if unknown else ""
        return ("fail", f"{plural(missing, 'admin')} without MFA{extra}", findings)
    if unknown:  # can't call it a pass, and "0 admins without MFA" as a failure reads as nonsense
        return _unavailable(f"MFA registration isn't known for {plural(unknown, 'admin')}")
    return (
        "pass",
        f"{every(len(seen), 'privileged user', start=True)} {verb(len(seen), 'has', 'have')} MFA registered",
        [],
    )


@rule(
    "global_admin_count",
    "Between 2 and 4 Global Administrators",
    "Admins",
    "Keep two to four Global Administrators: enough for a break-glass account, few enough to protect. "
    "Move day-to-day work to narrower roles such as User or Exchange Administrator.",
)
def global_admin_count(snap: TenantSnapshot, now: datetime):
    admins = _privileged_users(snap, eligible=True)
    if admins is None:
        return _unavailable("Admin role data hasn't been collected yet")
    # People who can sign in as Global Administrator now: enabled user accounts, directly or through a
    # group. Apps, groups themselves, disabled accounts and PIM-eligible holders don't count.
    held = [a for a in admins if a.get("roleName") == "Global Administrator" and a.get("principalType") == "user"]
    gas = {
        a["principalId"]: a for a in held if a.get("assignment") == "active" and a.get("accountEnabled") is not False
    }
    n = len(gas)
    if n > 4:
        return (
            "fail",
            f"{n} Global Administrators",
            [Finding("medium", _label(a), _role(a), pid) for pid, a in gas.items()],
        )
    if n == 0:
        eligible = {a["principalId"] for a in held if a.get("assignment") == "eligible"}
        detail = "No enabled account holds Global Administrator permanently"
        if eligible:
            detail += f" ({plural(len(eligible), 'account')} can activate it through PIM)"
        detail += ", so there is no break-glass account to recover access"
        return ("fail", "No active Global Administrator", [Finding("medium", "Tenant", detail)])
    if n < 2:
        return (
            "fail",
            "1 Global Administrator",
            [
                Finding(
                    "low", "Tenant", "Only one Global Administrator; there is no break-glass account to recover access"
                )
            ],
        )
    return ("pass", f"{n} Global Administrators", [])


@rule(
    "guest_admins",
    "No guests hold admin roles",
    "Admins",
    "Remove admin roles from guest accounts. If an external partner needs access, use GDAP or a "
    "member account in this tenant protected by MFA.",
)
def guest_admins(snap: TenantSnapshot, now: datetime):
    admins = _privileged_users(snap, eligible=True)
    if admins is None:
        return _unavailable("Admin role data hasn't been collected yet")
    guests = _by_principal([a for a in admins if a.get("userType") == "Guest"])
    findings = [
        Finding("high", _label(held[0]), f"Guest holding {_roles_text(held)}", pid) for pid, held in guests.items()
    ]
    return (
        ("fail", f"{plural(len(findings), 'guest')} with admin roles", findings)
        if findings
        else ("pass", "No guest holds a privileged role", [])
    )


@rule(
    "service_principal_admins",
    "Apps with admin roles are reviewed",
    "Admins",
    "Check each app that holds a privileged directory role still needs it. A compromised app secret "
    "with an admin role bypasses MFA entirely.",
)
def service_principal_admins(snap: TenantSnapshot, now: datetime):
    admins = _privileged_users(snap, eligible=True)
    if admins is None:
        return _unavailable("Admin role data hasn't been collected yet")
    own = snap.own_app_ids
    apps = _by_principal(
        [
            a
            for a in admins
            if a.get("principalType") == "servicePrincipal"
            and not ((a.get("appId") or "").lower() in own and a.get("roleName") == "Global Reader")
        ]
    )
    findings = [
        Finding("medium", held[0].get("displayName") or pid, f"App holding {_roles_text(held)}", pid)
        for pid, held in apps.items()
    ]
    return (
        ("fail", f"{plural(len(findings), 'app')} with privileged roles", findings)
        if findings
        else ("pass", "No apps hold privileged roles", [])
    )


@rule(
    "users_mfa_registered",
    "Every active user has MFA registered",
    "Identity",
    "Ask these users to register the Microsoft Authenticator app. Until they do, anyone with their "
    "password can register MFA in their place.",
)
def users_mfa_registered(snap: TenantSnapshot, now: datetime):
    reg = snap.objects("mfa_registration", "mfa_registration")
    if reg is None:
        return _unavailable("MFA registration data hasn't been collected yet")
    users = {u["_id"]: u for u in (snap.objects("users", "user") or [])}
    findings, checked = [], 0
    for r in reg:
        user = users.get(r["_id"])
        if user is not None and not user.get("accountEnabled"):
            continue
        if str(r.get("userType") or "member").lower() != "member":
            continue
        checked += 1
        if r.get("isMfaRegistered"):
            continue
        findings.append(
            Finding("high" if r.get("isAdmin") else "medium", _label(r), "No MFA method registered", r["_id"])
        )
    if not checked:  # an empty registration report proves nothing
        return _unavailable("Microsoft returned no MFA registration data for any enabled member yet")
    return (
        ("fail", f"{plural(len(findings), 'user')} without MFA", findings)
        if findings
        else ("pass", f"{every(checked, 'checked user', start=True)} {verb(checked, 'has', 'have')} MFA registered", [])
    )


@rule(
    "stale_users",
    "No inactive accounts",
    "Accounts",
    "Disable accounts that haven't signed in for 90 days, or were created over 30 days ago and never used "
    "(after checking with the client), then remove their licences. Inactive accounts are a common foothold "
    "for attackers.",
)
def stale_users(snap: TenantSnapshot, now: datetime):
    users = snap.objects("users", "user")
    if users is None:
        return _unavailable("User data hasn't been collected yet")
    if users and not any(u.get("signInActivityAvailable") for u in users):
        return _unavailable("Sign-in activity needs Entra ID P1")
    findings = []
    for u in users:
        if not u.get("accountEnabled") or not is_stale(u, now):
            continue
        last = last_sign_in(u)
        when = (
            f"last signed in {day(last.date().isoformat())}"
            if last
            else f"never signed in, created {day(u.get('createdDateTime'))}"
        )
        kind = "Guest" if u.get("userType") == "Guest" else ("Licensed member" if u.get("licensed") else "Member")
        severity = "medium" if u.get("licensed") else "low"
        findings.append(Finding(severity, _label(u), f"{kind}, {when}", u["_id"]))
    return (
        ("fail", f"{plural(len(findings), 'account')} inactive for 90+ days or never used", findings)
        if findings
        else ("pass", "No enabled account has been inactive for 90 days", [])
    )


@rule(
    "licensed_disabled",
    "No licences on disabled accounts",
    "Licences",
    "Remove licences from disabled accounts, or convert leavers' mailboxes to shared mailboxes "
    "(free up to 50 GB) before removing the licence.",
)
def licensed_disabled(snap: TenantSnapshot, now: datetime):
    users = snap.objects("users", "user")
    if users is None:
        return _unavailable("User data hasn't been collected yet")
    needs = _shared_needing_licence(snap)
    findings = [
        Finding("medium", _label(u), f"Disabled, holding {plural(len(u.get('skuIds') or []), 'licence')}", u["_id"])
        for u in users
        if not u.get("accountEnabled") and u.get("licensed") and u["_id"] not in needs
    ]
    n = len(needs)
    kept = (
        f" (not counting {plural(n, 'shared mailbox', 'shared mailboxes')} that {verb(n, 'needs', 'need')} one)"
        if n
        else ""
    )
    return (
        ("fail", f"{plural(len(findings), 'disabled account')} still licensed{kept}", findings)
        if findings
        else ("pass", f"No disabled account holds a licence{kept}", [])
    )


SHARED_FREE_BYTES = 50 * 1024**3


def _shared_needing_licence(snap: TenantSnapshot) -> set[str]:
    """Shared mailboxes that must keep a licence although their account is blocked from sign-in (as it
    should be): over 50 GB, with an archive or on litigation hold. Their directory object ids."""
    sizes = {r.get("mailboxId") or r["_id"]: r for r in snap.objects("mailbox_usage", "mailbox_size") or []}
    out = set()
    for m in snap.objects("mailboxes", "mailbox") or []:
        if m.get("RecipientTypeDetails") != "SharedMailbox" or not m.get("ExternalDirectoryObjectId"):
            continue
        size = sizes.get(m["_id"]) or {}
        archive = m.get("ArchiveStatus") not in (None, "", "None")
        big = isinstance(size.get("totalBytes"), int) and size["totalBytes"] > SHARED_FREE_BYTES
        if archive or m.get("LitigationHoldEnabled") or big:
            out.add(m["ExternalDirectoryObjectId"])
    return out


@rule(
    "unassigned_licences",
    "No paid licences sit unassigned",
    "Licences",
    "Reduce the subscription quantity at the next renewal, or assign the spare licences.",
)
def unassigned_licences(snap: TenantSnapshot, now: datetime):
    skus = snap.objects("tenant_profile", "subscribed_sku")
    if skus is None:
        return _unavailable("Licence data hasn't been collected yet")
    subscriptions = snap.objects("tenant_profile", "company_subscription")
    summary = licence_summary(skus, None, load_prices(snap.conn), now, subscriptions)
    row = snap.conn.execute("SELECT value FROM settings WHERE key = 'currency'").fetchone()
    currency = row["value"] if row else "GBP"
    findings = []
    for line in summary.lines:
        if line.free or not line.unassigned:
            continue
        cost = (
            f", {line.unassigned * line.monthly_price:,.2f} {currency}/month" if line.monthly_price is not None else ""
        )
        findings.append(
            Finding("low", line.name, f"{line.unassigned} of {line.purchased} unassigned{cost}", line.sku_id)
        )
    return (
        ("fail", f"{plural(len(findings), 'subscription')} with spare licences", findings)
        if findings
        else ("pass", "Every paid licence is assigned", [])
    )


# -- Conditional Access coverage ---------------------------------------------------


@rule(
    "admins_ca_mfa",
    "Every admin is covered by an MFA policy",
    "Admins",
    "Make sure each admin is included in an enabled Conditional Access policy that requires MFA for all "
    "apps, and isn't excluded from it directly or through a group. Use a separate policy for break-glass accounts.",
)
def admins_ca_mfa(snap: TenantSnapshot, now: datetime):
    defaults, _ = _ca_data(snap)
    admins = _privileged_users(snap)
    if defaults is None or admins is None:
        return _unavailable("Needs Conditional Access and admin role data")
    if defaults:
        return ("pass", "Security Defaults require MFA for admins", [])
    no_p1 = _no_p1(snap)
    d = coverage.directory(snap)
    policies = snap.objects("conditional_access", "ca_policy") or []
    findings, seen = [], set()
    for a in admins:
        pid = a["principalId"]
        if a.get("principalType") != "user" or pid in seen or a.get("accountEnabled") is False:
            continue
        seen.add(pid)
        c = coverage.coverage_for(pid, _label(a), a.get("userType") or "Member", policies, d)
        if not c.mfa:
            names = ", ".join(c.excluded_from[:2])
            if len(c.excluded_from) > 3:  # one line on the Findings tab; Conditional Access coverage has them all
                names += f" and {plural(len(c.excluded_from) - 2, 'more policy')}"
            elif len(c.excluded_from) == 3:
                names += f", {c.excluded_from[2]}"
            why = f"excluded from {names}" if names else "no MFA policy applies"
            findings.append(Finding("high", c.label, f"{_role(a)}: {why}", pid))
    if findings and no_p1:
        return ("fail", f"{plural(len(findings), 'admin')} not covered by MFA", findings, SECURITY_DEFAULTS_ADVICE)
    return (
        ("fail", f"{plural(len(findings), 'admin')} not covered by MFA", findings)
        if findings
        else ("pass", _all_are(len(seen), "active admin") + " covered", [])
    )
