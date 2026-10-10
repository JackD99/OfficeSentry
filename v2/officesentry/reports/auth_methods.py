"""Passwords and sign-in methods: how people sign in, how strong that is, and password hygiene.

Goes deeper than MFA coverage (who has MFA and who must use it): which
methods each person has, by strength; admins without a phishing-resistant
method; people whose only second factor is a phone; passwordless and
self-service reset readiness; the tenant's authentication methods policy
against what's recommended; authentication strengths in Conditional Access;
and password age, expiry and never-used accounts. Reads stored snapshots only.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .. import coverage
from ..authmethods import (
    APP,
    MANAGED,
    NO_SECOND_FACTOR,
    PHISHING_RESISTANT,
    PHONE,
    POLICY_METHODS,
    POLICY_ORDER,
    RESET_ONLY,
    TIER_MEANING,
    TIERS,
    Strength,
    asks_for_phone,
    preferred,
    strength,
    strength_is_phishing_resistant,
    who,
)
from ..devices import parse_ts
from ..licences import last_sign_in
from ..snapshot import TenantSnapshot
from ..text import plural
from .data_reports import Chart, DataReport, Highlight, Rows, Section
from .identity_reports import P1, Registration, _label, _roles
from .registry import report

PEOPLE = "Sign-in methods by person"
PASSWORDS = "Password age"
NEVER_USED_DAYS = 30  # a password set this long ago on an account that has never signed in
OLD_DAYS = 365
NEVER_EXPIRES = 2147483647  # what Graph reports for "passwords never expire"
UNKNOWN = "Unknown"


def _iso(value) -> str:
    when = parse_ts(value) if isinstance(value, str) else value
    return when.strftime("%Y-%m-%d") if when else ""


def _known(value) -> object:
    return UNKNOWN if value is None else bool(value)


def _admins(snap: TenantSnapshot, reg: Registration) -> tuple[dict[str, list[str]], set[str], bool]:
    """(role names by account, accounts holding a privileged role, whether roles were collected)."""
    roles = _roles(snap)
    if roles is None:  # fall back to Microsoft's own admin flag in the registration report
        return {}, {uid for uid, r in reg.by_id.items() if r.get("isAdmin")}, False
    names = {pid: p["active"] + [f"{n} (eligible)" for n in p["eligible"]] for pid, p in roles.items()}
    return names, {pid for pid, p in roles.items() if p["privileged"] and p["type"] == "user"}, True


def _advice(s: Strength | None, admin: bool, reg_row: dict | None) -> tuple[str, str | None]:
    """What to do about one person's methods, and how urgent it is."""
    if s is None:
        return "", None
    if s.strongest == NO_SECOND_FACTOR:
        return "No second factor. Set up MFA first (see MFA coverage).", "high" if admin else "medium"
    if s.phone_only:
        return ("Only a phone: set up the Authenticator app (or a passkey), then keep the phone as a backup at most.",
                "high" if admin else "medium")
    if admin and not s.phishing_resistant:
        return "Admin without a phishing-resistant method: register a passkey or security key.", "medium"
    if asks_for_phone(reg_row):
        return ("Asked for a text or call first although something stronger is set up: turn on system-preferred "
                "MFA.", "low")
    return "Nothing", None


def _people(users: list[dict], reg: Registration, role_names: dict[str, list[str]],
            privileged: set[str]) -> dict:
    """The person table, the method counts and the figures for the tiles."""
    people = Section(PEOPLE, ["Account", "Admin", "Admin roles", "Strongest method", "Methods", "Asked for first",
                              "Passwordless", "Self-service reset", "What to do"],
                     intro="Enabled members and what they can sign in with, weakest first and admins at the top. "
                           "Who hasn't set up MFA at all, and whether it's required, is in MFA coverage.",
                     empty="No enabled members.")
    counts = {t: [0, 0] for t in (*TIERS, NO_SECOND_FACTOR, UNKNOWN)}  # [people, admins]
    methods: dict[str, list] = {}  # label -> [tier, people, admins]
    stats = {"members": 0, "known": 0, "admins_weak": 0, "passwordless": 0, "passwordless_known": 0,
             "sspr_missing": 0, "sspr_known": 0}
    rank = {t: i for i, t in enumerate((NO_SECOND_FACTOR, PHONE, APP, PHISHING_RESISTANT, UNKNOWN))}
    rows = []
    for u in users:
        if not u.get("accountEnabled") or (u.get("userType") or "Member") != "Member":
            continue
        stats["members"] += 1
        uid, label = u["_id"], _label(u)
        row = reg.get(uid)
        s = strength(row)
        admin = uid in privileged
        tier = s.strongest if s else UNKNOWN
        counts[tier][0] += 1
        counts[tier][1] += admin
        if s is not None:
            stats["known"] += 1
            stats["admins_weak"] += admin and not s.phishing_resistant
            for name, t in s.methods:
                entry = methods.setdefault(name, [t, 0, 0])
                entry[1] += 1
                entry[2] += admin
        passwordless = row.get("isPasswordlessCapable") if row else None
        sspr = row.get("isSsprRegistered") if row else None
        if passwordless is not None:
            stats["passwordless_known"] += 1
            stats["passwordless"] += bool(passwordless)
        if sspr is not None:
            stats["sspr_known"] += 1
            stats["sspr_missing"] += not sspr
        todo, flag = _advice(s, admin, row)
        rows.append(((not admin, rank[tier], label.lower()),
                     [label, admin, role_names.get(uid), tier, s.labels if s else UNKNOWN, preferred(row),
                      _known(passwordless), _known(sspr), todo], flag))
    for _, cells, flag in sorted(rows, key=lambda r: r[0]):
        people.add(cells, flag)

    spread = Section("Strongest method", ["Strength", "People", "Admins", "Share of members", "What it means"],
                     intro="Each enabled member counted once, by the strongest method they have.",
                     empty="No enabled members.")
    spread.chart = Chart("Strength", ("People",), ranked=False)
    for tier in (*TIERS, NO_SECOND_FACTOR, UNKNOWN):
        n, admins = counts[tier]
        if tier == UNKNOWN and not n:
            continue
        flag = ("medium" if tier in (PHONE, NO_SECOND_FACTOR) else None) if n else None
        spread.add([tier, n, admins, f"{round(100 * n / stats['members'])}%" if stats["members"] else "",
                    TIER_MEANING.get(tier, "No MFA registration data for these people yet.")], flag)

    registered = Section("Methods registered", ["Method", "Strength", "People", "Admins"],
                         intro="How many enabled members have each method. People usually have more than one.",
                         empty="Nobody has registered a sign-in method yet, or registration hasn't been read.")
    registered.chart = Chart("Method", ("People",))
    order = {t: i for i, t in enumerate((*TIERS, RESET_ONLY))}
    for name, (tier, n, admins) in sorted(methods.items(), key=lambda m: (order.get(m[1][0], 9), -m[1][1], m[0])):
        registered.add([name, tier, n, admins], "low" if tier == PHONE else None)
    return {"sections": [spread, registered, people], "counts": counts, **stats}


def _policy(snap: TenantSnapshot, report: DataReport) -> Section | None:
    settings = (snap.objects("auth_methods_policy", "auth_methods_policy") or [None])[0]
    configs = snap.objects("auth_methods_policy", "auth_method_config")
    if settings is None:
        report.notes.append("The sign-in methods policy hasn't been collected yet, so which methods the tenant "
                            "allows isn't shown. Refresh \"Sign-in methods policy\" on the tenant page.")
        return None
    groups = {g["_id"]: g.get("displayName") for g in snap.objects("auth_methods_policy", "auth_policy_group") or []
              if isinstance(g.get("displayName"), str)}
    section = Section("Sign-in methods policy", ["Setting", "Current", "Recommended", "Why"],
                      intro="Which methods people may register and use, from the tenant's authentication methods "
                            "policy, against what's recommended for a small business.",
                      empty="Microsoft returned no method settings.")
    by_id = {c["_id"]: c for c in configs or []}
    for mid in sorted(by_id, key=lambda m: (POLICY_ORDER.index(m) if m in POLICY_ORDER else 99, m)):
        c = by_id[mid]
        info = POLICY_METHODS.get(mid)
        on = c.get("state") == "enabled"
        current = f"On for {who(c, groups)}" if on else ("Off" if c.get("state") == "disabled" else
                                                         (c.get("state") or "Not reported"))
        flag = info.severity if info and info.want_on is not None and on != info.want_on else None
        section.add([info.label if info else mid, current, info.recommended if info else "Your choice",
                     info.why if info else ""], flag)
        features = c.get("features") if isinstance(c.get("features"), dict) else {}
        for key, text in (("displayAppInformationRequiredState", "Authenticator shows which app is signing in"),
                          ("displayLocationInformationRequiredState", "Authenticator shows where the sign-in is "
                                                                      "from")):
            if features.get(key):
                state = features[key]
                section.add([text, MANAGED.get(state, state), "On (Microsoft managed turns it on)",
                             "Helps people spot a sign-in that isn't theirs."], "low" if state == "disabled" else None)
    if settings.get("methodsReturned") is False:
        report.notes.append("Microsoft didn't return the per-method settings of the sign-in methods policy.")

    campaign = settings.get("registrationCampaign") if isinstance(settings.get("registrationCampaign"), dict) else {}
    if campaign:
        state = campaign.get("state")
        current = MANAGED.get(state, state or "Not reported")
        if state in ("enabled", "default") and campaign.get("includeTargets"):
            current += f", for {who(campaign, groups)}"
            snooze = campaign.get("snoozeDurationInDays")
            if isinstance(snooze, int):
                current += f", can be skipped for {plural(snooze, 'day')}"
        section.add(["Registration campaign (asks people to set up Authenticator at sign-in)", current,
                     "On, or Microsoft managed", "Moves people off text messages and calls."],
                    "low" if state == "disabled" else None)
    system = settings.get("systemCredentialPreferences")
    if isinstance(system, dict):
        state = system.get("state")
        section.add(["System-preferred MFA (asks for the strongest method a person has)",
                     MANAGED.get(state, state or "Not reported") + (" (on)" if state == "default" else ""),
                     "On, or Microsoft managed",
                     "Otherwise people are asked for their own default, often a text message."],
                    "low" if state == "disabled" else None)
    suspicious = settings.get("reportSuspiciousActivity")
    if isinstance(suspicious, dict):
        state = suspicious.get("state")
        section.add(["People can report an MFA prompt they didn't expect", MANAGED.get(state, state or "Not reported"),
                     "On", "A reported prompt blocks the account's risky sign-ins and alerts admins."],
                    "low" if state == "disabled" else None)
    migration = settings.get("policyMigrationState")
    if migration:
        section.add(["Methods managed in this policy (not the old MFA and SSPR settings)",
                     {"migrationComplete": "Yes", "migrationInProgress": "Migration in progress",
                      "preMigration": "No: the old settings still apply too"}.get(migration, migration),
                     "Yes", "Microsoft has retired the old per-user MFA and SSPR method settings."],
                    None if migration == "migrationComplete" else "low")
    return section


def _strengths(snap: TenantSnapshot, report: DataReport) -> Section | None:
    policies = snap.objects("conditional_access", "ca_policy")
    if snap.capabilities.get("entra_p1") is False:
        report.notes.append(f"Authentication strengths need Conditional Access, which needs {P1}.")
        return None
    if policies is None:
        return None
    d = coverage.directory(snap)
    section = Section("Authentication strengths in Conditional Access",
                      ["Policy", "State", "Authentication strength", "Phishing-resistant", "Applies to", "Except"],
                      intro="Conditional Access policies that require a particular strength of MFA rather than any "
                            "MFA. Requiring Phishing-resistant MFA for admin roles is the usual first step.",
                      empty="No Conditional Access policy requires an authentication strength. Start with one "
                            "requiring Phishing-resistant MFA for admin roles, once admins have a passkey.")
    states = {"enabled": "On", "enabledForReportingButNotEnforced": "Report-only", "disabled": "Off"}
    rows = []
    for p in policies:
        s = (p.get("grantControls") or {}).get("authenticationStrength") if isinstance(
            p.get("grantControls"), dict) else None
        if not isinstance(s, dict):
            continue
        described = coverage.describe(p, d, snap.capabilities) if d else None
        rows.append(((p.get("state") != "enabled", coverage.policy_name(p).lower()),
                     [coverage.policy_name(p), states.get(p.get("state"), p.get("state") or ""),
                      s.get("displayName") or "Custom strength", strength_is_phishing_resistant(s),
                      described.users if described else "", described.excluded if described else ""]))
    for _, cells in sorted(rows, key=lambda r: r[0]):
        section.add(cells)
    return section


def _expiry(snap: TenantSnapshot, report: DataReport) -> Section | None:
    domains = snap.objects("directory_sync", "domain")
    if domains is None:
        report.notes.append("Domains haven't been collected yet, so the password expiry policy isn't shown. "
                            "Refresh Directory sync on the tenant page.")
        return None
    section = Section("Password expiry policy", ["Domain", "Signs in with", "Passwords expire after",
                                                 "Reminder before expiry", "Recommended"],
                      intro="How often cloud-only accounts on each domain must change their password. Microsoft "
                            "and the NCSC now advise against regular expiry when everyone has MFA: forced changes "
                            "lead to weaker, predictable passwords. Accounts synced from on-premises AD follow AD's "
                            "policy instead.", empty="No domains.")
    for d in sorted(domains, key=lambda d: (not d.get("isDefault"), str(d["_id"]).lower())):
        if d.get("isVerified") is False:
            continue
        federated = d.get("authenticationType") == "Federated"
        days = d.get("passwordValidityPeriodInDays")
        notice = d.get("passwordNotificationWindowInDays")
        if federated:
            expires, flag = "Set by the federation service", None
        elif not isinstance(days, int):
            expires, flag = "Not reported", None
        elif days >= NEVER_EXPIRES or days <= 0:
            expires, flag = "Never", None
        else:
            expires, flag = f"{days} days", "low"
        reminder = f"{plural(notice, 'day')}" if isinstance(notice, int) and not federated and \
            expires not in ("Never", "Not reported") else ""
        section.add([str(d["_id"]), "Federated" if federated else "Microsoft Entra ID", expires, reminder,
                     "Never, with MFA required for everyone"], flag)
    if not any(isinstance(d.get("passwordValidityPeriodInDays"), int) for d in domains):
        report.notes.append("Microsoft didn't report the password expiry policy for this tenant's domains.")
    return section


def _passwords(users: list[dict], reg: Registration, now: datetime) -> tuple[Section, dict]:
    section = Section(PASSWORDS, ["Account", "Password last changed", "Never expires", "Synced from on-premises",
                                  "Last sign-in", "MFA set up", "What it means"],
                      intro="When each enabled member's password was last set. An old password matters most "
                            "without MFA; an account that has never signed in may still have the password it was "
                            "created with. Problems first, then oldest first.", empty="No enabled members.")
    stats = {"never_expires": 0, "old": 0, "never_used": 0, "activity": any(u.get("signInActivityAvailable")
                                                                             for u in users)}
    rows = []
    for u in users:
        if not u.get("accountEnabled") or (u.get("userType") or "Member") != "Member":
            continue
        uid, label = u["_id"], _label(u)
        changed = parse_ts(u.get("lastPasswordChangeDateTime"))
        set_at = changed or parse_ts(u.get("createdDateTime"))
        registered = reg.registered(uid)
        last = last_sign_in(u)
        never = bool(u.get("signInActivityAvailable")) and last is None
        old = bool(changed and changed < now - timedelta(days=OLD_DAYS))
        stats["never_expires"] += bool(u.get("passwordNeverExpires"))
        stats["old"] += old
        flag, meaning = None, ""
        if never and set_at and set_at < now - timedelta(days=NEVER_USED_DAYS):
            stats["never_used"] += 1
            flag = "medium"
            meaning = ("Never signed in, and the password was set over a month ago: it's probably still the one the "
                       "account was created with. Block sign-in if it isn't needed, or reset the password.")
        elif old and registered is False:
            flag = "medium"
            meaning = "Unchanged for over a year and no MFA: if it ever leaked, it still works on its own."
        elif old:
            meaning = "Unchanged for over a year; MFA protects it." if registered else "Unchanged for over a year."
        if not u.get("signInActivityAvailable"):
            seen = "Not checked"
        else:
            seen = _iso(last) if last else "Never"
        rows.append(((flag is None, changed or now), [
            label, _iso(changed) or UNKNOWN, bool(u.get("passwordNeverExpires")),
            bool(u.get("onPremisesSyncEnabled")), seen, _known(registered), meaning], flag))
    for _, cells, flag in sorted(rows, key=lambda r: r[0]):
        section.add(cells, flag)
    return section, stats


@report(
    "auth-methods",
    "Passwords and sign-in methods",
    "How people sign in and how strong that is: methods by strength, admins without phishing-"
    "resistant MFA, the methods policy, and password age and expiry.",
    ("mfa_registration", "auth_methods_policy", "users", "admin_roles", "conditional_access",
    "directory_sync"),
    group="identity",
    order=20,
    keywords="passkeys fido2 security keys windows hello phishing-resistant passwordless sms text message "
    "voice call authenticator sspr self-service password reset expiry expiration never expires "
    "password age authentication strengths methods policy registration campaign nudge",
)
def auth_methods(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    users = snap.objects("users", "user")
    if users is None:
        report.unavailable = "User data hasn't been collected yet. Refresh Users on the tenant page."
        return
    reg = Registration(snap)
    role_names, privileged, roles_known = _admins(snap, reg)
    if reg.note:
        report.notes.append(reg.note)
    if reg.by_id and all(r.get("source") == "per_user_methods" for r in reg.by_id.values()):
        report.notes.append("Methods were read one person at a time (Microsoft's registration report was empty, "
                            f"or needs {P1}), so passwordless sign-in, self-service password reset and the method "
                            "people are asked for first aren't known.")
    if not roles_known:
        report.notes.append("Admin roles haven't been collected yet, so admins are the ones Microsoft's registration "
                            "report marks as admins.")

    found = _people(users, reg, role_names, privileged)
    sections = list(found["sections"])
    for extra in (_policy(snap, report), _strengths(snap, report)):
        if extra is not None:
            sections.append(extra)
    passwords, pw = _passwords(users, reg, now)
    sections.append(passwords)
    expiry = _expiry(snap, report)
    if expiry is not None:
        sections.append(expiry)
    if not pw["activity"]:
        report.notes.append(f"Sign-in dates need {P1}, so accounts that have never signed in aren't picked out.")
    report.sections = sections

    members, known, counts = found["members"], found["known"], found["counts"]
    checked = known and not reg.note

    def share(n: int) -> str:
        return f"{n} of {members}" if checked else "Not checked"

    weak_admins = found["admins_weak"]
    report.highlights = [
        Highlight("Phishing-resistant", share(counts[PHISHING_RESISTANT][0]),
                  "Passkey, security key, Windows Hello or certificate",
                  ("ok" if counts[PHISHING_RESISTANT][0] == members else None) if checked else None,
                  Rows(PEOPLE, {"Strongest method": PHISHING_RESISTANT})),
        Highlight("Admins without phishing-resistant", weak_admins if checked else "Not checked",
                  "Admin roles should need a passkey or security key", "bad" if checked and weak_admins else None,
                  Rows(PEOPLE, {"Admin": "Yes", "Strongest method": [APP, PHONE, NO_SECOND_FACTOR]})),
        Highlight("Phone is the only second factor", counts[PHONE][0] if checked else "Not checked",
                  "Text message or voice call", "warn" if checked and counts[PHONE][0] else None,
                  Rows(PEOPLE, {"Strongest method": PHONE})),
        Highlight("Passwordless ready", share(found["passwordless"]) if found["passwordless_known"] else "Not checked",
                  "Can sign in without a password", rows=Rows(PEOPLE, {"Passwordless": "Yes"})),
        Highlight("No self-service reset", found["sspr_missing"] if found["sspr_known"] else "Not checked",
                  "Can't reset a forgotten password themselves", "warn" if found["sspr_missing"] else None,
                  Rows(PEOPLE, {"Self-service reset": "No"})),
        Highlight("Password never expires", pw["never_expires"], "Fine with MFA: expiry is no longer advised",
                  rows=Rows(PASSWORDS, {"Never expires": "Yes"})),
        Highlight("Password over a year old", pw["old"], "Matters most without MFA",
                  rows=Rows(PASSWORDS, age=("Password last changed", "gt365"))),
        Highlight("Never signed in, old password", pw["never_used"] if pw["activity"] else "Not checked",
                  "Probably still the first password", "warn" if pw["never_used"] else None,
                  Rows(PASSWORDS, {"Last sign-in": "Never"}, flagged=True)),
    ]
