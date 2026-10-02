"""Rules turn a tenant snapshot into findings.

Each rule returns a ``RuleResult``:
  pass         checked, nothing wrong
  fail         checked, with findings
  unavailable  the data isn't there (collector never ran, or the tenant's
               licence doesn't provide it); the reason says which

Rules never call Graph; they only read stored snapshots.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .licences import is_stale, last_sign_in, licence_summary, load_prices
from .snapshot import TenantSnapshot

SEVERITIES = ("critical", "high", "medium", "low", "info")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}


@dataclass
class Finding:
    severity: str
    subject: str
    detail: str
    object_id: str = ""


@dataclass
class RuleResult:
    key: str
    title: str
    category: str
    status: str  # pass | fail | unavailable
    summary: str
    recommendation: str = ""
    findings: list[Finding] = field(default_factory=list)

    @property
    def severity(self) -> str | None:
        if not self.findings:
            return None
        return min((f.severity for f in self.findings), key=SEVERITY_RANK.__getitem__)


@dataclass
class Rule:
    key: str
    title: str
    category: str
    recommendation: str
    check: Callable[[TenantSnapshot, datetime], RuleResult | tuple]


RULES: list[Rule] = []


def rule(key: str, title: str, category: str, recommendation: str):
    def decorator(fn):
        RULES.append(Rule(key, title, category, recommendation, fn))
        return fn
    return decorator


def _unavailable(reason: str) -> tuple:
    return ("unavailable", reason, [])


def _label(row: dict) -> str:
    return row.get("userPrincipalName") or row.get("displayName") or row.get("_id", "?")


def _names(snap: TenantSnapshot) -> dict[str, str]:
    """Best available label for any object ID seen in the tenant's snapshots."""
    names: dict[str, str] = {}
    for rows in (snap.objects("admin_roles", "role_assignment"), snap.objects("mfa_registration", "mfa_registration"),
                 snap.objects("users", "user")):
        for r in rows or []:
            key = r.get("principalId") or r["_id"]
            label = r.get("userPrincipalName") or r.get("displayName")
            if label:
                names[key] = label
    return names


# -- Helpers over Conditional Access ------------------------------------------


def _enabled(policy: dict) -> bool:
    return policy.get("state") == "enabled"


def _all_users_all_apps(policy: dict) -> bool:
    cond = policy.get("conditions") or {}
    users = cond.get("users") or {}
    apps = cond.get("applications") or {}
    return "All" in (users.get("includeUsers") or []) and "All" in (apps.get("includeApplications") or [])


def _grants(policy: dict) -> set[str]:
    grant = policy.get("grantControls") or {}
    controls = set(grant.get("builtInControls") or [])
    if grant.get("authenticationStrength"):
        controls.add("mfa")
    return controls


def mfa_for_all_policies(policies: list[dict]) -> list[dict]:
    return [p for p in policies if _enabled(p) and _all_users_all_apps(p) and "mfa" in _grants(p)
            and set((p.get("conditions") or {}).get("clientAppTypes") or ["all"]) & {"all", "browser",
                                                                                    "mobileAppsAndDesktopClients"}]


def _ca_data(snap: TenantSnapshot):
    defaults = snap.objects("conditional_access", "security_defaults")
    policies = snap.objects("conditional_access", "ca_policy")
    if defaults is None:
        return None, None
    enabled = bool(defaults and defaults[0].get("isEnabled"))
    return enabled, policies or []


# -- Rules ----------------------------------------------------------------------


@rule("mfa_enforced", "MFA is enforced for all users", "Identity",
      "Create a Conditional Access policy requiring MFA for all users and all cloud apps, or turn on "
      "Security Defaults if the tenant has no Entra ID P1. Keep exclusions to break-glass accounts.")
def mfa_enforced(snap: TenantSnapshot, now: datetime):
    defaults, policies = _ca_data(snap)
    if defaults is None:
        return _unavailable("Conditional Access data hasn't been collected yet")
    if defaults:
        return ("pass", "Security Defaults are on, which require MFA registration and enforce it", [])
    covering = mfa_for_all_policies(policies)
    if not covering:
        report_only = [p for p in policies if p.get("state") == "enabledForReportingButNotEnforced"
                       and "mfa" in _grants(p)]
        detail = "No enabled policy requires MFA for all users and apps, and Security Defaults are off."
        if report_only:
            detail += f" {len(report_only)} MFA policy(s) are in report-only mode."
        return ("fail", "MFA is not enforced", [Finding("high", "Tenant", detail)])
    names = _names(snap)
    findings = []
    for p in covering:
        excluded = (p.get("conditions") or {}).get("users") or {}
        for uid in excluded.get("excludeUsers") or []:
            findings.append(Finding("medium", names.get(uid, uid), f"Excluded from \"{p.get('displayName')}\"", uid))
        for gid in excluded.get("excludeGroups") or []:
            findings.append(Finding("medium", f"Group {gid}", f"Group excluded from \"{p.get('displayName')}\"", gid))
    policy_names = ", ".join(p.get("displayName", "?") for p in covering)
    if findings:
        return ("fail", f"Enforced by {policy_names}, with {len(findings)} exclusion(s) to review", findings)
    return ("pass", f"Enforced by {policy_names}", [])


@rule("legacy_auth_blocked", "Legacy authentication is blocked", "Identity",
      "Block legacy authentication (Exchange ActiveSync and other clients) with a Conditional Access policy "
      "for all users. Legacy protocols can't do MFA and are the most common password-spray target.")
def legacy_auth_blocked(snap: TenantSnapshot, now: datetime):
    defaults, policies = _ca_data(snap)
    if defaults is None:
        return _unavailable("Conditional Access data hasn't been collected yet")
    if defaults:
        return ("pass", "Security Defaults block legacy authentication", [])
    for p in policies:
        apps = set((p.get("conditions") or {}).get("clientAppTypes") or [])
        if _enabled(p) and _all_users_all_apps(p) and "block" in _grants(p) and {"exchangeActiveSync", "other"} <= apps:
            return ("pass", f"Blocked by \"{p.get('displayName')}\"", [])
    return ("fail", "Legacy authentication is allowed",
            [Finding("high", "Tenant", "No enabled policy blocks legacy authentication for all users")])


def _privileged_users(snap: TenantSnapshot) -> list[dict] | None:
    rows = snap.objects("admin_roles", "role_assignment")
    if rows is None:
        return None
    return [r for r in rows if r.get("privileged") and r.get("assignment") == "active"]


@rule("admin_mfa", "Every admin has MFA registered", "Admins",
      "Have each listed admin register a strong method (Authenticator or a FIDO2 key), or remove the role. "
      "Break-glass accounts should use FIDO2 keys kept offline.")
def admin_mfa(snap: TenantSnapshot, now: datetime):
    admins = _privileged_users(snap)
    reg = snap.objects("mfa_registration", "mfa_registration")
    if admins is None or reg is None:
        return _unavailable("Needs admin role and MFA registration data")
    registered = {r["_id"]: r["isMfaRegistered"] for r in reg}
    findings, seen = [], set()
    for a in admins:
        pid = a["principalId"]
        if a.get("principalType") != "user" or pid in seen or a.get("accountEnabled") is False:
            continue
        seen.add(pid)
        if registered.get(pid) is False:
            findings.append(Finding("critical", _label(a), f"{a['roleName']} with no MFA method registered", pid))
        elif pid not in registered:
            findings.append(Finding("low", _label(a), f"{a['roleName']}: MFA registration unknown", pid))
    if findings:
        return ("fail", f"{sum(f.severity == 'critical' for f in findings)} admin(s) without MFA", findings)
    return ("pass", f"All {len(seen)} privileged user(s) have MFA registered", [])


@rule("global_admin_count", "Between 2 and 4 Global Administrators", "Admins",
      "Keep two to four Global Administrators: enough for a break-glass account, few enough to protect. "
      "Move day-to-day work to narrower roles such as User or Exchange Administrator.")
def global_admin_count(snap: TenantSnapshot, now: datetime):
    admins = _privileged_users(snap)
    if admins is None:
        return _unavailable("Admin role data hasn't been collected yet")
    gas = {a["principalId"]: a for a in admins if a.get("roleName") == "Global Administrator"}
    n = len(gas)
    if n > 4:
        return ("fail", f"{n} Global Administrators", [Finding("medium", _label(a), "Global Administrator", pid)
                                                        for pid, a in gas.items()])
    if n < 2:
        return ("fail", f"{n} Global Administrator(s)", [Finding(
            "low", "Tenant", "Only one Global Administrator; there is no break-glass account to recover access")])
    return ("pass", f"{n} Global Administrators", [])


@rule("guest_admins", "No guests hold admin roles", "Admins",
      "Remove admin roles from guest accounts. If an external partner needs access, use GDAP or a "
      "member account in this tenant protected by MFA.")
def guest_admins(snap: TenantSnapshot, now: datetime):
    admins = _privileged_users(snap)
    if admins is None:
        return _unavailable("Admin role data hasn't been collected yet")
    findings = [Finding("high", _label(a), f"Guest holding {a['roleName']}", a["principalId"])
                for a in admins if a.get("userType") == "Guest"]
    return ("fail", f"{len(findings)} guest admin assignment(s)", findings) if findings else \
        ("pass", "No guest holds a privileged role", [])


@rule("service_principal_admins", "Apps with admin roles are reviewed", "Admins",
      "Check each app that holds a privileged directory role still needs it. A compromised app secret "
      "with an admin role bypasses MFA entirely.")
def service_principal_admins(snap: TenantSnapshot, now: datetime):
    admins = _privileged_users(snap)
    if admins is None:
        return _unavailable("Admin role data hasn't been collected yet")
    findings = [Finding("medium", a.get("displayName") or a["principalId"], f"App holding {a['roleName']}",
                        a["principalId"]) for a in admins if a.get("principalType") == "servicePrincipal"]
    return ("fail", f"{len(findings)} app(s) with privileged roles", findings) if findings else \
        ("pass", "No apps hold privileged roles", [])


@rule("users_mfa_registered", "Every active user has MFA registered", "Identity",
      "Ask these users to register the Microsoft Authenticator app. Until they do, anyone with their "
      "password can register MFA in their place.")
def users_mfa_registered(snap: TenantSnapshot, now: datetime):
    reg = snap.objects("mfa_registration", "mfa_registration")
    if reg is None:
        return _unavailable("MFA registration data hasn't been collected yet")
    users = {u["_id"]: u for u in (snap.objects("users", "user") or [])}
    findings = []
    for r in reg:
        user = users.get(r["_id"])
        if user is not None and not user.get("accountEnabled"):
            continue
        if (r.get("userType") or "member").lower() != "member" or r["isMfaRegistered"]:
            continue
        findings.append(Finding("high" if r.get("isAdmin") else "medium", _label(r), "No MFA method registered",
                                r["_id"]))
    return ("fail", f"{len(findings)} user(s) without MFA", findings) if findings else \
        ("pass", f"All {len(reg)} checked user(s) have MFA registered", [])


@rule("stale_users", "No inactive accounts", "Accounts",
      "Disable accounts that haven't signed in for 90 days (after checking with the client), then remove "
      "their licences. Inactive accounts are a common foothold for attackers.")
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
        when = f"last signed in {last.date().isoformat()}" if last else "never signed in"
        kind = "Guest" if u.get("userType") == "Guest" else ("Licensed member" if u.get("licensed") else "Member")
        severity = "medium" if u.get("licensed") else "low"
        findings.append(Finding(severity, _label(u), f"{kind}, {when}", u["_id"]))
    return ("fail", f"{len(findings)} account(s) inactive for 90+ days", findings) if findings else \
        ("pass", "No enabled account has been inactive for 90 days", [])


@rule("licensed_disabled", "No licences on disabled accounts", "Licences",
      "Remove licences from disabled accounts, or convert leavers' mailboxes to shared mailboxes "
      "(free up to 50 GB) before removing the licence.")
def licensed_disabled(snap: TenantSnapshot, now: datetime):
    users = snap.objects("users", "user")
    if users is None:
        return _unavailable("User data hasn't been collected yet")
    findings = [Finding("medium", _label(u), f"Disabled, holding {len(u.get('skuIds') or [])} licence(s)", u["_id"])
                for u in users if not u.get("accountEnabled") and u.get("licensed")]
    return ("fail", f"{len(findings)} disabled account(s) still licensed", findings) if findings else \
        ("pass", "No disabled account holds a licence", [])


@rule("unassigned_licences", "No paid licences sit unassigned", "Licences",
      "Reduce the subscription quantity at the next renewal, or assign the spare licences.")
def unassigned_licences(snap: TenantSnapshot, now: datetime):
    skus = snap.objects("tenant_profile", "subscribed_sku")
    if skus is None:
        return _unavailable("Licence data hasn't been collected yet")
    summary = licence_summary(skus, None, load_prices(snap.conn), now)
    row = snap.conn.execute("SELECT value FROM settings WHERE key = 'currency'").fetchone()
    currency = row["value"] if row else "GBP"
    findings = []
    for line in summary.lines:
        if line.free or not line.unassigned:
            continue
        cost = (f", {line.unassigned * line.monthly_price:,.2f} {currency}/month"
                if line.monthly_price is not None else "")
        findings.append(Finding("low", line.name, f"{line.unassigned} of {line.purchased} unassigned{cost}",
                                line.sku_id))
    return ("fail", f"{sum(1 for _ in findings)} subscription(s) with spare licences", findings) if findings else \
        ("pass", "Every paid licence is assigned", [])


def evaluate(snap: TenantSnapshot, now: datetime | None = None) -> list[RuleResult]:
    now = now or datetime.now(UTC)
    results = []
    for r in RULES:
        status, summary, findings = r.check(snap, now)
        findings.sort(key=lambda f: (SEVERITY_RANK[f.severity], f.subject.lower()))
        results.append(RuleResult(r.key, r.title, r.category, status, summary, r.recommendation, findings))
    return results
