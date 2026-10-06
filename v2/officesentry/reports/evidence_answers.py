"""Suggested answers to the questions cyber-insurance questionnaires ask, worded as the insurer asks them.

Each answer is Yes, Partly, No or Not known, with a one-line justification
citing the evidence and a link to the report rows behind it. They're built
from the stored snapshot and the checks' results only, never Graph.

Enforcement comes first: MFA required by Conditional Access for everyone but a
named break-glass account is a "Yes" that lists the exclusion, not a gap.

A technical lead (admin or analyst) can override any answer with a note
(``evidence_answer_overrides``, one row per tenant and question). The pack,
its PDF and exports show the override, who set it and when; the suggestion
stays visible to staff on the page, and when the suggestion has changed since
the override was set the page says the data has changed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from .. import coverage
from ..db import audit, transaction, utcnow
from ..rules import RuleResult, _ca_data, _names, _privileged_users, mfa_for_all_policies
from ..snapshot import TenantSnapshot
from ..text import plural
from .identity_reports import BREAK_GLASS_NAME
from .views import anchor, slug

YES, PARTLY, NO, NOT_KNOWN = "Yes", "Partly", "No", "Not known"
ANSWERS = (YES, PARTLY, NO, NOT_KNOWN)
MAX_NOTE = 1000
MAX_NAMED = 3  # more named exclusions than this (that aren't break-glass) and MFA is only partly enforced

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Source:
    """Where the evidence for an answer is: a detail report's table, or a control further down the pack."""
    label: str
    report: str = ""   # data_reports key
    table: str = ""    # section title in that report
    control: str = ""  # evidence.CONTROLS key

    @property
    def anchor(self) -> str:
        return anchor(slug(self.table)) if self.table else ""


@dataclass(frozen=True)
class Override:
    answer: str
    note: str
    set_by: str
    set_at: str
    suggested: str  # what Office Sentry suggested when it was set


@dataclass
class Answer:
    key: str
    question: str
    suggested: str
    why: str
    source: Source | None = None
    override: Override | None = None

    @property
    def answer(self) -> str:
        return self.override.answer if self.override else self.suggested

    @property
    def data_changed(self) -> bool:
        """The suggestion now differs from the one the override was set against."""
        return bool(self.override) and self.override.suggested != self.suggested


@dataclass(frozen=True)
class Question:
    key: str
    question: str
    answer: Callable  # (snap, results by rule key) -> (answer, justification)
    source: Source | None = None


# -- Helpers -------------------------------------------------------------------------------


def _sentence(text: str) -> str:
    text = (text or "").strip()
    return (text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")) if text else ""


def _quoted(names) -> str:
    return ", ".join(f'"{n}"' for n in names)


def _from_checks(results: dict[str, RuleResult], keys: tuple[str, ...]) -> tuple[str, str]:
    """All checks pass: Yes; none could run: Not known; all fail: No; anything in between: Partly."""
    found = [results[k] for k in keys if k in results]
    ran = [r for r in found if r.status != "unavailable"]
    if not ran:
        reason = found[0].summary if found else "The data hasn't been collected yet"
        return NOT_KNOWN, _sentence(reason)
    good = [r for r in ran if r.status in ("pass", "accepted")]
    bad = [r for r in ran if r.status not in ("pass", "accepted")]
    parts = [_sentence(r.summary) if r.status == "pass" else f"{r.title}: exceptions accepted as a documented risk."
             for r in good] + [_sentence(r.summary) for r in bad]
    answer = YES if not bad else NO if not good else PARTLY
    return answer, " ".join(parts)


def _global_admins(snap: TenantSnapshot) -> set[str]:
    return {a.get("principalId") for a in snap.objects("admin_roles", "role_assignment") or []
            if a.get("assignment") == "active" and a.get("roleName") == "Global Administrator"}


def _break_glass(uid: str, label: str, snap: TenantSnapshot, users: dict[str, dict]) -> bool:
    """Named like an emergency account, or a Global Administrator kept out of the MFA policy on purpose."""
    name = f"{label} {(users.get(uid) or {}).get('displayName') or ''}"
    return bool(BREAK_GLASS_NAME.search(name)) or uid in _global_admins(snap)


def _users(snap: TenantSnapshot) -> dict[str, dict]:
    return {u["_id"]: u for u in snap.objects("users", "user") or [] if u.get("_id")}


def _excluded(label: str, glass: bool) -> str:
    return f"{label} (break-glass)" if glass else label


# -- The questions -------------------------------------------------------------------------


def _mfa_everyone(snap: TenantSnapshot, results: dict[str, RuleResult]):
    r = results.get("mfa_enforced")
    defaults, policies = _ca_data(snap)
    if r is None or r.status == "unavailable" or defaults is None:
        return NOT_KNOWN, _sentence(r.summary if r else "Conditional Access data hasn't been collected yet")
    if defaults:
        return YES, "Security Defaults are on, which require MFA for every user signing in to email and cloud apps."
    covering = mfa_for_all_policies(policies)
    pct = snap.metric("mfa.registered_pct")
    registered = f" {pct:g}% of checked users have registered an MFA method." if pct is not None else ""
    if not covering:
        report_only = [p for p in policies if p.get("state") == "enabledForReportingButNotEnforced"]
        tail = f" {plural(len(report_only), 'policy')} only in report-only mode." if report_only else ""
        return NO, ("No enabled Conditional Access policy requires MFA for all users and apps, and Security "
                    "Defaults are off." + tail + registered)
    names, users = _names(snap), _users(snap)
    people, groups = {}, {}
    for p in covering:
        spec = (p.get("conditions") or {}).get("users") or {}
        for uid in spec.get("excludeUsers") or []:
            people.setdefault(uid, names.get(uid, uid))
        for gid in spec.get("excludeGroups") or []:
            groups.setdefault(gid, names.get(gid, f"group {gid}"))
    by = f"Enforced by Conditional Access ({_quoted(coverage.policy_name(p) for p in covering)})"
    if not people and not groups:
        return YES, f"{by} for all users and apps, with no exclusions."
    glass = {uid: _break_glass(uid, label, snap, users) for uid, label in people.items()}
    listed = [_excluded(label, glass[uid]) for uid, label in sorted(people.items(), key=lambda kv: kv[1].lower())]
    listed += [f"{label} (group)" for label in sorted(groups.values(), key=str.lower)]
    others = sum(1 for g in glass.values() if not g)
    exclusions = plural(len(people) + len(groups), "exclusion")
    if groups or others > MAX_NAMED:
        return PARTLY, f"{by}, but with {exclusions} to review: {', '.join(listed)}."
    return YES, f"{by} for everyone except {plural(len(listed), 'named exclusion')}: {', '.join(listed)}."


def _mfa_admins(snap: TenantSnapshot, results: dict[str, RuleResult]):
    r = results.get("admins_ca_mfa")
    defaults, policies = _ca_data(snap)
    admins = _privileged_users(snap)
    if r is None or r.status == "unavailable" or defaults is None or admins is None:
        return NOT_KNOWN, _sentence(r.summary if r else "Needs Conditional Access and admin role data")
    if defaults:
        return YES, "Security Defaults are on, which require MFA for every admin role."
    d = coverage.directory(snap)
    users = _users(snap)
    seen, missing = {}, []
    for a in admins:
        pid = a["principalId"]
        if a.get("principalType") != "user" or pid in seen or a.get("accountEnabled") is False:
            continue
        label = a.get("userPrincipalName") or a.get("displayName") or pid
        c = coverage.coverage_for(pid, label, a.get("userType") or "Member", policies, d)
        seen[pid] = c
        if not c.mfa:
            missing.append((pid, c))
    if not seen:
        return NOT_KNOWN, "No active admin accounts were found to check."
    policy_names = sorted({n for c in seen.values() for n in c.mfa_policies})
    by = f" by {_quoted(policy_names)}" if policy_names else ""
    covered = len(seen) - len(missing)
    if not missing:
        return YES, f"All {plural(len(seen), 'active admin account')} are required to use MFA{by}."
    glass = {pid: _break_glass(pid, c.label, snap, users) for pid, c in missing}

    def why(pid, c, mark: bool) -> str:
        where = f"excluded from {_quoted(c.excluded_from)}" if c.excluded_from else "no MFA policy applies"
        return f"{c.label} ({'break-glass, ' if mark and glass[pid] else ''}{where})"

    if all(glass.values()) and covered:
        return YES, (f"{covered} of {plural(len(seen), 'active admin account')} are required to use MFA{by}; "
                     f"the exception is {plural(len(missing), 'break-glass account')}: "
                     f"{', '.join(why(pid, c, False) for pid, c in missing)}.")
    named = [why(pid, c, True) for pid, c in missing]
    answer = PARTLY if covered else NO
    return answer, (f"{covered} of {plural(len(seen), 'active admin account')} are required to use MFA{by}. "
                    f"Not covered: {', '.join(named)}.")


def _legacy(snap: TenantSnapshot, results: dict[str, RuleResult]):
    r, smtp = results.get("legacy_auth_blocked"), results.get("smtp_auth")
    if r is None or r.status == "unavailable":
        return NOT_KNOWN, _sentence(r.summary if r else "Conditional Access data hasn't been collected yet")
    if r.status == "fail":
        return NO, _sentence(r.summary) + " No enabled policy blocks legacy sign-in for all users."
    why = _sentence(r.summary)
    if smtp and smtp.status == "fail":
        return PARTLY, f"{why} SMTP AUTH (password-only mail sending) is still allowed: {smtp.summary.rstrip('.')}."
    if smtp and smtp.status == "accepted":
        return YES, f"{why} SMTP AUTH exceptions are accepted as a documented risk."
    if smtp and smtp.status == "pass":
        return YES, f"{why} {_sentence(smtp.summary)}"
    return YES, why


def _separate_admins(snap: TenantSnapshot, results: dict[str, RuleResult]):
    """An admin account that holds a licence or a user mailbox looks like someone's everyday account."""
    admins = _privileged_users(snap)
    users = snap.objects("users", "user")
    if admins is None or users is None:
        return NOT_KNOWN, "Admin role and user data haven't been collected yet."
    by_id = {u["_id"]: u for u in users}
    mailboxes = {m.get("ExternalDirectoryObjectId") for m in snap.objects("mailboxes", "mailbox") or []
                 if m.get("RecipientTypeDetails") == "UserMailbox"}
    people = {a["principalId"]: a.get("userPrincipalName") or a.get("displayName") or a["principalId"]
              for a in admins if a.get("principalType") == "user" and a.get("accountEnabled") is not False
              and a.get("userType") != "Guest"}
    if not people:
        return NOT_KNOWN, "No active admin accounts were found to check."
    daily = sorted(label for pid, label in people.items()
                   if _licensed(by_id.get(pid) or {}) or pid in mailboxes)
    if not daily:
        return YES, (f"None of the {plural(len(people), 'admin account')} holds a licence or a mailbox, so they "
                     "aren't used for everyday work.")
    answer = NO if len(daily) == len(people) else PARTLY
    one = len(daily) == 1
    looks = "it looks like an everyday account" if one else "they look like everyday accounts"
    return answer, (f"{len(daily)} of {plural(len(people), 'admin account')} also {'has' if one else 'have'} a "
                    f"licence or mailbox, so {looks}: {', '.join(daily)}.")


def _licensed(user: dict) -> bool:
    return bool(user.get("skuIds") or user.get("licensed"))


def _forwarding(snap: TenantSnapshot, results: dict[str, RuleResult]):
    policy, forwards = results.get("auto_forward_policy"), results.get("external_forwarding")
    if policy is None or policy.status == "unavailable":
        return _from_checks(results, ("auto_forward_policy", "external_forwarding"))
    if policy.status == "fail":
        return NO, _sentence(policy.summary) + (f" {_sentence(forwards.summary)}" if forwards and
                                                forwards.status == "fail" else "")
    if forwards and forwards.status == "fail":
        return PARTLY, f"{_sentence(policy.summary)} But {forwards.summary[:1].lower() + forwards.summary[1:]} found."
    return YES, " ".join(_sentence(r.summary) for r in (policy, forwards)
                         if r and r.status in ("pass",)) or _sentence(policy.summary)


def _checks(*keys: str):
    return lambda snap, results: _from_checks(results, keys)


def _guests(snap: TenantSnapshot, results: dict[str, RuleResult]):
    """Evidence that guest access is reviewed: no long-inactive guests and no forgotten invitations."""
    users = snap.objects("users", "user")
    pending = results.get("pending_guests")
    stale = results.get("stale_users")
    if users is None:
        return NOT_KNOWN, "User data hasn't been collected yet."
    guests = [u for u in users if u.get("userType") == "Guest"]
    stale_guests = [f.subject for f in (stale.findings if stale else []) if f.detail.startswith("Guest")]
    stale_known = stale is not None and stale.status != "unavailable"
    if not stale_known:
        return NOT_KNOWN, f"{plural(len(guests), 'guest account')}; sign-in activity isn't available to show review."
    problems = []
    if stale_guests:
        problems.append(f"{plural(len(stale_guests), 'guest')} inactive for 90+ days or never signed in "
                        f"({', '.join(stale_guests[:3])}{', …' if len(stale_guests) > 3 else ''})")
    if pending and pending.status == "fail":
        problems.append(pending.summary[:1].lower() + pending.summary[1:])
    if not guests:
        return YES, "There are no guest accounts."
    if not problems:
        return YES, f"{plural(len(guests), 'guest account')}, none inactive for 90+ days and no stale invitations."
    answer = PARTLY if len(stale_guests) < len(guests) else NO
    return answer, f"{plural(len(guests), 'guest account')}: {'; '.join(problems)}."


def _outside(text: str):
    return lambda snap, results: (NOT_KNOWN, text)


QUESTIONS = (
    Question("mfa_remote", "Is MFA enforced for all remote access and email access?", _mfa_everyone,
             Source("MFA coverage: accounts with a gap", "mfa", "Accounts with a gap")),
    Question("mfa_privileged", "Is MFA enforced for all privileged and administrator accounts?", _mfa_admins,
             Source("Admins and roles: privileged role holders", "admins", "Privileged role holders")),
    Question("legacy_auth", "Is legacy authentication blocked?", _legacy,
             Source("Legacy sign-ins: what each mailbox allows", "legacy-auth", "What each mailbox allows")),
    Question("separate_admins", "Are admin accounts separate from day-to-day user accounts?", _separate_admins,
             Source("Admins and roles: privileged role holders", "admins", "Privileged role holders")),
    Question("forwarding", "Is automatic forwarding of email to external addresses blocked?", _forwarding,
             Source("Mail forwarding", "mailboxes", "Forwarding")),
    Question("email_auth", "Are SPF, DKIM and DMARC in place for your email domains?",
             _checks("spf_record", "dkim_enabled", "dmarc_policy"), Source("Email domains", "domains")),
    Question("audit_logs", "Are mailbox audit logs enabled?", _checks("mailbox_auditing"),
             Source("Audit logging control", control="logging")),
    Question("devices_encrypted", "Are company laptops and desktops encrypted?", _checks("device_encryption"),
             Source("Devices: managed devices", "devices", "Managed devices")),
    Question("devices_managed", "Are company devices centrally managed and required to meet security policies?",
             _checks("intune_enrolled", "device_compliance"),
             Source("Devices: managed devices", "devices", "Managed devices")),
    Question("devices_patched", "Are all devices on operating systems that still receive security updates?",
             _checks("supported_windows"), Source("Devices: operating systems", "devices", "Operating systems")),
    Question("leavers", "Are leavers' and inactive accounts disabled promptly?",
             _checks("stale_users", "licensed_disabled"), Source("Inactive accounts", "inactive", "Inactive members")),
    Question("guest_review", "Do you review guest and external user access?", _guests,
             Source("Inactive accounts: inactive guests", "inactive", "Inactive guests")),
    Question("external_sharing", "Is sharing of files with anonymous or external users restricted?",
             _checks("sharepoint_sharing", "anyone_links", "guest_invites"),
             Source("Guests and sharing: external access settings", "guests", "External access settings")),
    Question("third_party_apps", "Is access to your data by third-party applications controlled?",
             _checks("user_consent", "risky_app_permissions"), Source("Apps and consents", "apps")),
    Question("backups", "Do you back up Microsoft 365 data (email, OneDrive, SharePoint) separately?",
             _outside("Backups aren't visible in Microsoft 365 data. Answer from the backup product's records.")),
    Question("training", "Do staff receive regular security awareness and phishing training?",
             _outside("Training isn't visible in Microsoft 365 data. Answer from the training provider's records.")),
)
KEYS = {q.key for q in QUESTIONS}


# -- Building and overriding ---------------------------------------------------------------


def suggest(snap: TenantSnapshot, results: dict[str, RuleResult]) -> list[Answer]:
    out = []
    for q in QUESTIONS:
        try:
            answer, why = q.answer(snap, results)
        except Exception:  # damaged stored data: this answer is unknown, the rest of the pack still renders
            log.exception("Evidence answer %s failed", q.key)
            answer, why = NOT_KNOWN, "The stored data for this couldn't be read."
        out.append(Answer(q.key, q.question, answer, why, q.source))
    return out


def overrides(conn, tenant_id: int) -> dict[str, Override]:
    rows = conn.execute("SELECT * FROM evidence_answer_overrides WHERE tenant_id = ?", (tenant_id,)).fetchall()
    return {r["question_key"]: Override(r["answer"], r["note"], r["set_by"], r["set_at"], r["suggested"])
            for r in rows if r["question_key"] in KEYS}


def apply_overrides(answers: list[Answer], found: dict[str, Override]) -> list[Answer]:
    for a in answers:
        a.override = found.get(a.key)
    return answers


def set_override(conn, tenant_id: int, key: str, answer: str, note: str, suggested: str, username: str) -> None:
    if key not in KEYS or answer not in ANSWERS or suggested not in ANSWERS:
        raise ValueError("unknown question or answer")
    with transaction(conn):
        conn.execute(
            "INSERT INTO evidence_answer_overrides (tenant_id, question_key, answer, note, suggested, set_by, set_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (tenant_id, question_key) DO UPDATE SET"
            " answer = excluded.answer, note = excluded.note, suggested = excluded.suggested,"
            " set_by = excluded.set_by, set_at = excluded.set_at",
            (tenant_id, key, answer, note.strip()[:MAX_NOTE], suggested, username, utcnow()))
        # The note can name the client's people, so the log records only the question and the answer.
        audit(conn, username, "evidence_answer_set", tenant_id, f"{key}: {answer} (suggested {suggested})")


def clear_override(conn, tenant_id: int, key: str, username: str) -> bool:
    with transaction(conn):
        gone = conn.execute("DELETE FROM evidence_answer_overrides WHERE tenant_id = ? AND question_key = ?",
                            (tenant_id, key)).rowcount
        if gone:
            audit(conn, username, "evidence_answer_cleared", tenant_id, key)
    return bool(gone)
