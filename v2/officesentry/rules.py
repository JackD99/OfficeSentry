"""Rules turn a tenant snapshot into findings.

Each rule returns a ``RuleResult``:
  pass         checked, nothing wrong
  fail         checked, with findings
  unavailable  the data isn't there (collector never ran, or the tenant's
               licence doesn't provide it); the reason says which

Rules never call Graph; they only read stored snapshots.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import coverage, devices, dnscheck
from .licences import is_stale, last_sign_in, licence_summary, load_prices
from .mailflow import inbox_rule_risk, is_external, mailbox_forwarding, tenant_domains
from .snapshot import TenantSnapshot

SEVERITIES = ("critical", "high", "medium", "low", "info")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}


@dataclass
class Finding:
    severity: str
    subject: str
    detail: str
    object_id: str = ""
    new: bool = False  # set by reports: first seen (or reopened) in the period


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
    for rows in (snap.objects("conditional_access", "ca_group"), snap.objects("admin_roles", "role_assignment"),
                 snap.objects("mfa_registration", "mfa_registration"), snap.objects("users", "user")):
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
            findings.append(Finding("medium", names.get(gid, f"Group {gid}"),
                                    f"Group excluded from \"{p.get('displayName')}\"", gid))
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
    own = snap.own_app_id
    findings = [Finding("medium", a.get("displayName") or a["principalId"], f"App holding {a['roleName']}",
                        a["principalId"]) for a in admins if a.get("principalType") == "servicePrincipal"
                and not (own and a.get("appId") == own and a.get("roleName") == "Global Reader")]
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


# -- Conditional Access coverage ---------------------------------------------------


@rule("admins_ca_mfa", "Every admin is covered by an MFA policy", "Admins",
      "Make sure each admin is included in an enabled Conditional Access policy that requires MFA for all "
      "apps, and isn't excluded from it directly or through a group. Use a separate policy for break-glass accounts.")
def admins_ca_mfa(snap: TenantSnapshot, now: datetime):
    defaults, _ = _ca_data(snap)
    admins = _privileged_users(snap)
    if defaults is None or admins is None:
        return _unavailable("Needs Conditional Access and admin role data")
    if defaults:
        return ("pass", "Security Defaults require MFA for admins", [])
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
            why = f"excluded from {', '.join(c.excluded_from)}" if c.excluded_from else "no MFA policy applies"
            findings.append(Finding("high", c.label, f"{a['roleName']}: {why}", pid))
    return ("fail", f"{len(findings)} admin(s) not covered by MFA", findings) if findings else \
        ("pass", f"All {len(seen)} active admin(s) are covered", [])


# -- Email -----------------------------------------------------------------------


def _exchange_missing(snap: TenantSnapshot):
    status = snap.objects("mailboxes", "exchange_status")
    if status and not status[0].get("available"):
        return _unavailable("The tenant has no Exchange Online licence")
    return None


@rule("external_forwarding", "No mail is forwarded outside the organisation", "Email",
      "Remove forwarding to outside addresses unless the client has a documented business reason. "
      "Forwarding is the most common way attackers keep reading a mailbox after a password reset.")
def external_forwarding(snap: TenantSnapshot, now: datetime):
    if missing := _exchange_missing(snap):
        return missing
    mailboxes = snap.objects("mailboxes", "mailbox")
    rules = snap.objects("inbox_rules", "inbox_rule")
    domains = tenant_domains(snap)
    if (mailboxes is None and rules is None) or domains is None:
        return _unavailable("Mailbox and inbox rule data hasn't been collected yet")
    findings = []
    for m in mailboxes or []:
        target = mailbox_forwarding(m)
        if target and is_external(target, domains):
            keep = "keeps a copy" if m.get("DeliverToMailboxAndForward") else "doesn't keep a copy"
            findings.append(Finding("high", m.get("PrimarySmtpAddress") or m["_id"],
                                    f"Mailbox forwarding to {target} ({keep})", f"mailbox:{m['_id']}"))
    for r in rules or []:
        risk = inbox_rule_risk(r, domains)
        if risk.external:
            state = "" if r.get("isEnabled", True) else " (disabled)"
            findings.append(Finding("high" if r.get("isEnabled", True) else "low", r.get("mailbox") or "?",
                                    f"Inbox rule \"{r.get('displayName')}\" forwards to {', '.join(risk.external)}"
                                    f"{state}", f"rule:{r['_id']}"))
    scope = "" if mailboxes is not None else " (inbox rules only; Exchange data not collected)"
    return ("fail", f"{len(findings)} external forward(s){scope}", findings) if findings else \
        ("pass", f"Nothing forwards outside the organisation{scope}", [])


@rule("suspicious_inbox_rules", "No inbox rules hide mail", "Email",
      "Check each rule with the mailbox owner. Rules that delete or bury messages about payments or "
      "security alerts are a sign of a compromised account: reset the password, revoke sessions and review sign-ins.")
def suspicious_inbox_rules(snap: TenantSnapshot, now: datetime):
    if missing := _exchange_missing(snap):
        return missing
    rules = snap.objects("inbox_rules", "inbox_rule")
    domains = tenant_domains(snap)
    if rules is None or domains is None:
        return _unavailable("Inbox rules haven't been collected yet")
    findings = []
    for r in rules:
        risk = inbox_rule_risk(r, domains)
        reasons = [x for x in risk.reasons if not x.startswith("Forwards to")]
        if risk.severity and reasons:
            findings.append(Finding(risk.severity, r.get("mailbox") or "?",
                                    f"\"{r.get('displayName')}\": {'; '.join(reasons)}", f"rule:{r['_id']}"))
    return ("fail", f"{len(findings)} suspicious inbox rule(s)", findings) if findings else \
        ("pass", f"None of {len(rules)} inbox rule(s) hide mail", [])


@rule("auto_forward_policy", "Automatic forwarding to outside addresses is off", "Email",
      "Set the outbound spam filter policy's automatic forwarding to Off (or Automatic, which Microsoft treats "
      "as Off), and allow forwarding only for named mailboxes with a separate policy.")
def auto_forward_policy(snap: TenantSnapshot, now: datetime):
    if missing := _exchange_missing(snap):
        return missing
    policies = snap.objects("mailboxes", "outbound_spam_policy")
    if policies is None:
        return _unavailable("Exchange data hasn't been collected yet")
    findings = [Finding("medium", p.get("Name") or p["_id"], "Allows automatic forwarding to any outside address",
                        p["_id"]) for p in policies if p.get("AutoForwardingMode") == "On"]
    return ("fail", "Automatic external forwarding is allowed", findings) if findings else \
        ("pass", "Outbound policies block automatic external forwarding", [])


@rule("shared_mailbox_signin", "Shared mailboxes can't be signed in to", "Email",
      "Block sign-in for each shared mailbox's account (it doesn't need a password). An enabled shared "
      "mailbox account usually has no MFA and nobody watching it.")
def shared_mailbox_signin(snap: TenantSnapshot, now: datetime):
    if missing := _exchange_missing(snap):
        return missing
    mailboxes = snap.objects("mailboxes", "mailbox")
    users = snap.objects("users", "user")
    if mailboxes is None or users is None:
        return _unavailable("Needs mailbox and user data")
    enabled = {u["_id"] for u in users if u.get("accountEnabled")}
    shared = [m for m in mailboxes if m.get("RecipientTypeDetails") == "SharedMailbox"]
    findings = [Finding("medium", m.get("PrimarySmtpAddress") or m["_id"], "Shared mailbox account allows sign-in",
                        m["_id"]) for m in shared if m.get("ExternalDirectoryObjectId") in enabled]
    return ("fail", f"{len(findings)} of {len(shared)} shared mailbox(es) allow sign-in", findings) if findings else \
        ("pass", f"All {len(shared)} shared mailbox(es) block sign-in", [])


def _mail_setting(snap: TenantSnapshot, key: str):
    rows = snap.objects("mailboxes", "mail_settings")
    if not rows:
        return None
    return rows[0].get(key)


@rule("mailbox_auditing", "Mailbox auditing is on", "Email",
      "Turn mailbox auditing back on for the organisation (Set-OrganizationConfig -AuditDisabled $false). "
      "Without it there's no record of who read or deleted mail during an incident.")
def mailbox_auditing(snap: TenantSnapshot, now: datetime):
    if missing := _exchange_missing(snap):
        return missing
    value = _mail_setting(snap, "AuditDisabled")
    if value is None:
        return _unavailable("Exchange data hasn't been collected yet")
    return ("fail", "Mailbox auditing is turned off", [Finding("medium", "Organisation", "AuditDisabled is set")]) \
        if value else ("pass", "Mailbox auditing is on for the organisation", [])


@rule("smtp_auth", "SMTP AUTH is turned off", "Email",
      "Turn off SMTP AUTH for the organisation and enable it only on the mailboxes of devices or apps that "
      "still need it, such as scanners. It accepts passwords without MFA.")
def smtp_auth(snap: TenantSnapshot, now: datetime):
    if missing := _exchange_missing(snap):
        return missing
    value = _mail_setting(snap, "SmtpClientAuthenticationDisabled")
    if value is None:
        return _unavailable("Exchange data hasn't been collected yet")
    return ("pass", "SMTP AUTH is off for the organisation", []) if value else \
        ("fail", "SMTP AUTH is on for the organisation", [Finding("low", "Organisation", "SMTP AUTH is allowed")])


# -- Apps ------------------------------------------------------------------------

HIGH_RISK_PERMISSIONS = {
    "Mail.Read", "Mail.ReadWrite", "Mail.Send", "MailboxSettings.ReadWrite", "full_access_as_app",
    "Exchange.ManageAsApp", "Files.Read.All", "Files.ReadWrite.All", "Sites.Read.All", "Sites.ReadWrite.All",
    "Sites.FullControl.All", "Directory.ReadWrite.All", "User.ReadWrite.All", "Group.ReadWrite.All",
    "RoleManagement.ReadWrite.Directory", "Application.ReadWrite.All", "AppRoleAssignment.ReadWrite.All",
    "Policy.ReadWrite.ConditionalAccess", "UserAuthenticationMethod.ReadWrite.All", "Domain.ReadWrite.All",
}
SENSITIVE_DELEGATED = {
    "Mail.Read", "Mail.ReadWrite", "Mail.Send", "Mail.Read.Shared", "Mail.ReadWrite.Shared", "full_access_as_user",
    "EWS.AccessAsUser.All", "Files.Read.All", "Files.ReadWrite.All", "Sites.Read.All", "Sites.ReadWrite.All",
    "Directory.ReadWrite.All", "Directory.AccessAsUser.All", "User.ReadWrite.All", "MailboxSettings.ReadWrite",
}


def app_risks(snap: TenantSnapshot) -> list[dict] | None:
    """Third-party apps with what they hold and how risky it is (shared with the Apps report)."""
    sps = snap.objects("app_consents", "service_principal")
    if sps is None:
        return None
    own = snap.own_app_id
    perms = snap.objects("app_consents", "app_permission") or []
    grants = snap.objects("app_consents", "delegated_grant") or []
    out = []
    for sp in sps:
        if sp.get("microsoft") or sp.get("type") != "Application" or (own and sp.get("appId") == own):
            continue
        app_perms = sorted({p["permission"] for p in perms if p.get("clientId") == sp["_id"] and p.get("permission")})
        tenant_scopes, user_scopes, users = set(), set(), set()
        for g in grants:
            if g.get("clientId") != sp["_id"]:
                continue
            if g.get("consentType") == "AllPrincipals":
                tenant_scopes.update(g.get("scopes") or [])
            else:
                user_scopes.update(g.get("scopes") or [])
                users.add(g.get("principalId"))
        if not (app_perms or tenant_scopes or user_scopes):
            continue
        risky_app = sorted(set(app_perms) & HIGH_RISK_PERMISSIONS)
        risky_tenant = sorted(tenant_scopes & SENSITIVE_DELEGATED)
        risky_user = sorted(user_scopes & SENSITIVE_DELEGATED)
        verified = bool(sp.get("verifiedPublisher"))
        if risky_app:
            severity = "medium" if verified else "high"
        elif risky_tenant:
            severity = "medium"
        elif risky_user and not verified:
            severity = "low"
        else:
            severity = None
        out.append({"id": sp["_id"], "name": sp.get("displayName") or sp["_id"],
                    "publisher": sp.get("verifiedPublisher") or sp.get("publisherName") or "",
                    "verified": verified, "app_permissions": app_perms, "tenant_scopes": sorted(tenant_scopes),
                    "user_scopes": sorted(user_scopes), "users": len(users), "severity": severity,
                    "risky": risky_app + [s for s in risky_tenant + risky_user if s not in risky_app]})
    out.sort(key=lambda a: (SEVERITY_RANK.get(a["severity"] or "info", 9), a["name"].lower()))
    return out


@rule("risky_app_permissions", "Third-party apps hold only the access they need", "Apps",
      "Review each app with the client. Remove apps nobody recognises (Enterprise applications > Delete), "
      "and for the rest confirm the vendor and that it still needs mailbox or file access.")
def risky_app_permissions(snap: TenantSnapshot, now: datetime):
    apps = app_risks(snap)
    if apps is None:
        return _unavailable("App data hasn't been collected yet")
    findings = []
    for a in apps:
        if not a["severity"]:
            continue
        who = "unverified publisher" if not a["verified"] else f"verified publisher {a['publisher']}"
        if set(a["risky"]) & set(a["app_permissions"]):
            how = "application permission"
        elif set(a["risky"]) & set(a["tenant_scopes"]):
            how = "consented for all users"
        else:
            how = f"consented by {a['users']} user(s)"
        findings.append(Finding(a["severity"], a["name"], f"{', '.join(a['risky'])} ({how}; {who})", a["id"]))
    return ("fail", f"{len(findings)} app(s) with access to mail, files or the directory", findings) if findings \
        else ("pass", f"No third-party app holds high-risk access ({len(apps)} app(s) reviewed)", [])


def _authorization_policy(snap: TenantSnapshot):
    rows = snap.objects("tenant_policies", "authorization_policy")
    return rows[0] if rows else None


@rule("user_consent", "Users can't consent to apps on their own", "Apps",
      "Set user consent to \"Allow user consent for apps from verified publishers, for selected permissions\" "
      "or turn it off and use the admin consent workflow. Consent phishing relies on this setting.")
def user_consent(snap: TenantSnapshot, now: datetime):
    policy = _authorization_policy(snap)
    if policy is None:
        return _unavailable("Consent settings haven't been collected yet")
    assigned = [p for p in policy.get("permissionGrantPolicies") or [] if "ManagePermissionGrantsForSelf" in p]
    if any(p.endswith("microsoft-user-default-legacy") for p in assigned):
        return ("fail", "Users can consent to any app", [Finding(
            "medium", "Tenant", "Any user can grant any app access to their mail and files")])
    if assigned:
        return ("pass", "Users can consent only to verified publishers' low-risk permissions", [])
    return ("pass", "User consent is turned off", [])


@rule("app_credentials", "App secrets and certificates are current", "Apps",
      "Renew credentials before they expire to avoid an outage, and delete app registrations whose "
      "credentials have all expired if nothing uses them any more.")
def app_credentials(snap: TenantSnapshot, now: datetime):
    apps = snap.objects("app_consents", "application")
    if apps is None:
        return _unavailable("App data hasn't been collected yet")
    soon = now.timestamp() + 30 * 86400
    findings = []
    for a in apps:
        ends = [c["endDateTime"] for c in a.get("credentials") or [] if c.get("endDateTime")]
        if not ends:
            continue
        latest = max(ends)
        latest_ts = datetime.fromisoformat(latest.replace("Z", "+00:00")).timestamp()
        if latest_ts < now.timestamp():
            findings.append(Finding("low", a.get("displayName") or a["_id"],
                                    f"All {len(ends)} credential(s) expired, last on {latest[:10]}", a["_id"]))
        elif latest_ts < soon:
            findings.append(Finding("medium", a.get("displayName") or a["_id"],
                                    f"Last valid credential expires {latest[:10]}", a["_id"]))
    return ("fail", f"{len(findings)} app registration(s) with expiring or expired credentials", findings) \
        if findings else ("pass", "No app registration credentials expire in the next 30 days", [])


# -- Sharing and guests -------------------------------------------------------------

SHARING_LEVELS = {
    "disabled": "Only people in the organisation",
    "existingExternalUserSharingOnly": "Existing guests only",
    "externalUserSharingOnly": "New and existing guests",
    "externalUserAndGuestSharing": "Anyone, including anonymous links",
}


@rule("sharepoint_sharing", "Files can't be shared with anonymous links", "Sharing",
      "Change SharePoint external sharing to \"New and existing guests\" so every external person signs in. "
      "If the client relies on Anyone links, set them to expire and to view-only.")
def sharepoint_sharing(snap: TenantSnapshot, now: datetime):
    rows = snap.objects("tenant_policies", "sharepoint_settings")
    if snap.objects("tenant_policies", "authorization_policy") is None:
        return _unavailable("Sharing settings haven't been collected yet")
    if not rows:
        return _unavailable("SharePoint sharing settings weren't available")
    level = rows[0].get("sharingCapability")
    label = SHARING_LEVELS.get(level, level or "unknown")
    if level == "externalUserAndGuestSharing":
        return ("fail", label, [Finding("medium", "SharePoint and OneDrive", "Anyone links are allowed")])
    return ("pass", label, [])


@rule("guest_invites", "Only admins and members can invite guests", "Guests",
      "Restrict guest invitations to admins and the Guest Inviter role, or at least to members, under "
      "External collaboration settings.")
def guest_invites(snap: TenantSnapshot, now: datetime):
    policy = _authorization_policy(snap)
    if policy is None:
        return _unavailable("Guest settings haven't been collected yet")
    if policy.get("allowInvitesFrom") == "everyone":
        return ("fail", "Anyone, including guests, can invite guests", [Finding(
            "low", "Tenant", "Guests can invite other guests")])
    return ("pass", {"none": "Nobody can invite guests", "adminsAndGuestInviters": "Only admins can invite guests",
                     }.get(policy.get("allowInvitesFrom"), "Members and admins can invite guests"), [])


@rule("pending_guests", "No old, unaccepted guest invitations", "Guests",
      "Delete guest accounts whose invitation has gone unaccepted for 30 days; re-invite if still needed.")
def pending_guests(snap: TenantSnapshot, now: datetime):
    users = snap.objects("users", "user")
    if users is None:
        return _unavailable("User data hasn't been collected yet")
    cutoff = now.timestamp() - 30 * 86400
    findings = []
    for u in users:
        created = u.get("createdDateTime")
        if u.get("userType") != "Guest" or u.get("externalUserState") != "PendingAcceptance" or not created:
            continue
        if datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() < cutoff:
            findings.append(Finding("low", _label(u), f"Invited {created[:10]}, never accepted", u["_id"]))
    return ("fail", f"{len(findings)} guest invitation(s) unaccepted for 30+ days", findings) if findings else \
        ("pass", "No stale guest invitations", [])



# -- Email domains ---------------------------------------------------------------------


def _domain_rule(snap: TenantSnapshot, area: str):
    rows = snap.objects("domain_health", "domain")
    if not rows:
        return None
    findings = []
    for row in rows:
        health = dnscheck.DomainHealth.from_dict(row)
        for issue in dnscheck.issues(health):
            if issue.area == area:
                findings.append(Finding(issue.severity, health.domain, issue.detail, f"{health.domain}:{area}"))
    return findings, rows


@rule("spf_record", "Every domain has a working SPF record", "Email",
      "Publish one SPF record per domain ending in -all (or ~all while testing), e.g. "
      "\"v=spf1 include:spf.protection.outlook.com -all\". Domains that never send mail should publish "
      "\"v=spf1 -all\".")
def spf_record(snap: TenantSnapshot, now: datetime):
    result = _domain_rule(snap, "SPF")
    if result is None:
        return _unavailable("Domain checks haven't run yet, or the tenant has no custom domains")
    findings, rows = result
    return ("fail", f"{len({f.subject for f in findings})} domain(s) with SPF problems", findings) if findings \
        else ("pass", f"SPF is correct on all {len(rows)} domain(s)", [])


@rule("dmarc_policy", "Every domain enforces DMARC", "Email",
      "Publish a DMARC record with a rua= reporting address, start at p=none to see who sends as the domain, "
      "then move to p=quarantine and p=reject. Without enforcement, spoofed mail from the domain is delivered.")
def dmarc_policy(snap: TenantSnapshot, now: datetime):
    result = _domain_rule(snap, "DMARC")
    if result is None:
        return _unavailable("Domain checks haven't run yet, or the tenant has no custom domains")
    findings, rows = result
    return ("fail", f"{len({f.subject for f in findings})} domain(s) without DMARC enforcement", findings) \
        if findings else ("pass", f"DMARC is enforced on all {len(rows)} domain(s)", [])


@rule("dkim_enabled", "DKIM signing is on for every mail domain", "Email",
      "Turn on DKIM for each domain in the Defender portal (Email authentication settings > DKIM), "
      "publishing the two selector CNAME records it shows.")
def dkim_enabled(snap: TenantSnapshot, now: datetime):
    result = _domain_rule(snap, "DKIM")
    if result is None:
        return _unavailable("Domain checks haven't run yet, or the tenant has no custom domains")
    findings, rows = result
    checked = [r for r in rows if dnscheck.DomainHealth.from_dict(r).microsoft_365]
    if not checked:
        return _unavailable("No domain receives mail through Microsoft 365")
    return ("fail", f"{len(findings)} mail domain(s) without DKIM", findings) if findings else \
        ("pass", f"DKIM is on for all {len(checked)} Microsoft 365 mail domain(s)", [])


# -- Devices -------------------------------------------------------------------------


def _managed_devices(snap: TenantSnapshot):
    """Intune devices, or the reason they can't be checked."""
    status = devices.objects(snap, "intune_status")
    if status is None:
        return None, "Device data hasn't been collected yet"
    if not status or not status[0].get("available"):
        return None, "The tenant has no Intune licence, or Intune isn't readable"
    managed = devices.objects(snap, "managed_device") or []
    if not managed:
        return None, "No devices are enrolled in Intune"
    return managed, ""


def _device_label(d: dict) -> str:
    name = d.get("deviceName") or d.get("displayName") or d.get("_id", "?")
    user = d.get("userPrincipalName")
    return f"{name} ({user})" if user else name


@rule("device_compliance", "Managed devices meet compliance policies", "Devices",
      "Open each non-compliant device in Intune (Devices > Monitor > Noncompliant devices) to see which "
      "setting fails, fix it or retire the device, and block non-compliant devices with a Conditional "
      "Access policy that requires a compliant device.")
def device_compliance(snap: TenantSnapshot, now: datetime):
    managed, reason = _managed_devices(snap)
    if managed is None:
        return _unavailable(reason)
    findings = []
    for d in managed:
        if str(d.get("jailBroken")).lower() == "true":
            findings.append(Finding("high", _device_label(d), "Jailbroken or rooted", d["_id"]))
        elif d.get("complianceState") == "noncompliant":
            findings.append(Finding("medium", _device_label(d), "Not compliant", d["_id"]))
        elif d.get("complianceState") == "inGracePeriod":
            findings.append(Finding("low", _device_label(d), "Not compliant, in grace period", d["_id"]))
    return ("fail", f"{len(findings)} of {len(managed)} managed device(s) not compliant", findings) if findings \
        else ("pass", f"All {len(managed)} managed device(s) are compliant", [])


@rule("device_encryption", "Company computers are encrypted", "Devices",
      "Turn on BitLocker (Windows) and FileVault (macOS) with an Intune disk encryption policy, with recovery "
      "keys escrowed to Entra ID, so a lost or stolen laptop doesn't expose the client's data.")
def device_encryption(snap: TenantSnapshot, now: datetime):
    managed, reason = _managed_devices(snap)
    if managed is None:
        return _unavailable(reason)
    computers = [d for d in managed if devices.is_computer(d)]
    if not computers:
        return _unavailable("No Windows or Mac computers are enrolled in Intune")
    findings = [Finding("high", _device_label(d), f"{d.get('operatingSystem')} disk isn't encrypted", d["_id"])
                for d in computers if d.get("isEncrypted") is False]
    return ("fail", f"{len(findings)} of {len(computers)} computer(s) not encrypted", findings) if findings \
        else ("pass", f"All {len(computers)} managed computer(s) are encrypted", [])


@rule("supported_windows", "Windows computers still get security updates", "Devices",
      "Upgrade computers on Windows 10 or an old Windows 11 release to the current Windows 11 release "
      "(Intune: Windows feature updates policy). Replace hardware that can't run Windows 11, or buy "
      "Extended Security Updates as a stopgap.")
def supported_windows(snap: TenantSnapshot, now: datetime):
    managed, _ = _managed_devices(snap)
    if managed is not None:
        windows, source = [d for d in managed if devices.is_windows(d)], "osVersion"
    else:
        entra = devices.objects(snap, "entra_device")
        if entra is None:
            return _unavailable("Device data hasn't been collected yet")
        # Without Intune, use Entra's record of devices that signed in recently.
        windows = [d for d in entra if devices.is_windows(d) and d.get("accountEnabled")
                   and (devices.days_since(d.get("approximateLastSignInDateTime"), now) or 0) <= devices.STALE_DAYS]
        source = "operatingSystemVersion"
    if not windows:
        return _unavailable("No active Windows computers found")
    severity = {"ended": "high", "pro_ended": "medium", "ending": "low"}
    findings = []
    for d in windows:
        support = devices.windows_support(d.get(source), now.date())
        if support.status in severity:
            findings.append(Finding(severity[support.status], _device_label(d), support.detail, d["_id"]))
    unsupported = sum(f.severity != "low" for f in findings)
    if not findings:
        return ("pass", f"All {len(windows)} Windows computer(s) are on a supported release", [])
    summary = f"{unsupported} of {len(windows)} Windows computer(s) past end of support" if unsupported \
        else f"{len(findings)} Windows computer(s) reach end of support within {devices.SOON_DAYS} days"
    return ("fail", summary, findings)


@rule("intune_enrolled", "Joined computers are managed by Intune", "Devices",
      "Enrol Entra-joined and hybrid-joined computers in Intune (automatic enrolment under Devices > "
      "Enrollment > Windows) so compliance, encryption and updates can be enforced on them.")
def intune_enrolled(snap: TenantSnapshot, now: datetime):
    status = devices.objects(snap, "intune_status")
    entra = devices.objects(snap, "entra_device")
    if status is None or entra is None:
        return _unavailable("Device data hasn't been collected yet")
    if not status or not status[0].get("available"):
        return _unavailable("The tenant has no Intune licence, or Intune isn't readable")
    joined = [d for d in entra if d.get("trustType") in ("AzureAd", "ServerAd") and devices.is_computer(d)
              and d.get("accountEnabled")
              and (devices.days_since(d.get("approximateLastSignInDateTime"), now) or 0) <= devices.STALE_DAYS]
    if not joined:
        return _unavailable("No active Entra-joined or hybrid-joined computers")
    findings = [Finding("low", d.get("displayName") or d["_id"],
                        ("Hybrid joined" if d.get("trustType") == "ServerAd" else "Entra joined")
                        + ", not managed by Intune", d["_id"])
                for d in joined if not d.get("isManaged")]
    return ("fail", f"{len(findings)} of {len(joined)} joined computer(s) not in Intune", findings) if findings \
        else ("pass", f"All {len(joined)} joined computer(s) are managed", [])


@rule("device_checkin", "Managed devices check in with Intune", "Devices",
      "Find out whether each device is lost, replaced or broken. Retire devices that are gone; for "
      "devices still in use, restart the Intune Management Extension or re-enrol.")
def device_checkin(snap: TenantSnapshot, now: datetime):
    managed, reason = _managed_devices(snap)
    if managed is None:
        return _unavailable(reason)
    findings = []
    for d in managed:
        days = devices.days_since(d.get("lastSyncDateTime"), now)
        if days is not None and days > devices.NO_SYNC_DAYS:
            findings.append(Finding("low", _device_label(d), f"Last check-in {days} days ago", d["_id"]))
    return ("fail", f"{len(findings)} device(s) haven't checked in for {devices.NO_SYNC_DAYS}+ days", findings) \
        if findings else ("pass", f"All {len(managed)} managed device(s) checked in recently", [])


@rule("stale_devices", "No old devices left in Entra ID", "Devices",
      "Disable, then after 30 days delete, device records that haven't signed in for 90 days (Entra admin "
      "center > Devices > All devices, filter by activity). Old records can still hold BitLocker keys and "
      "count toward device limits.")
def stale_devices(snap: TenantSnapshot, now: datetime):
    entra = devices.objects(snap, "entra_device")
    if entra is None:
        return _unavailable("Device data hasn't been collected yet")
    findings = []
    for d in entra:
        days = devices.days_since(d.get("approximateLastSignInDateTime"), now)
        if d.get("accountEnabled") and days is not None and days > devices.STALE_DAYS:
            findings.append(Finding("low", d.get("displayName") or d["_id"],
                                    f"{d.get('operatingSystem') or 'Device'}, last seen {days} days ago", d["_id"]))
    return ("fail", f"{len(findings)} of {len(entra)} device record(s) unused for {devices.STALE_DAYS}+ days",
            findings) if findings else ("pass", f"No stale devices among {len(entra)} record(s)", [])


def evaluate(snap: TenantSnapshot, now: datetime | None = None) -> list[RuleResult]:
    now = now or datetime.now(UTC)
    results = []
    for r in RULES:
        try:
            status, summary, findings = r.check(snap, now)
            findings.sort(key=lambda f: (SEVERITY_RANK[f.severity], f.subject.lower()))
        except Exception as exc:  # one broken check mustn't blank the whole report
            logging.getLogger(__name__).exception("Check %s failed", r.key)
            status, summary, findings = "unavailable", f"This check couldn't run: {type(exc).__name__}", []
        results.append(RuleResult(r.key, r.title, r.category, status, summary, r.recommendation, findings))
    return results
