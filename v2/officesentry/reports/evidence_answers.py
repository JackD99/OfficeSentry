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

from ..checks import RuleResult
from ..db import audit, transaction, utcnow
from ..snapshot import TenantSnapshot
from .evidence_base import (
    ANSWERS,
    IDENTITY,
    M365_ONLY,
    MAX_NOTE,
    NOT_KNOWN,
    Answer,
    Override,
    Question,
    Source,
    _checks,
    _forwarding,
    _guests,
    _legacy,
    _mfa_admins,
    _mfa_everyone,
    _separate_admins,
    gap,
    lead,
)
from .evidence_topics import TOPICS

log = logging.getLogger(__name__)


CORE = (
    Question(
        "mfa_remote",
        "Is MFA enforced for all remote access and email access?",
        _mfa_everyone,
        Source("MFA coverage: accounts with a gap", "mfa", "Accounts with a gap"),
        lead(
            "Multi-factor authentication (MFA) is enforced for Microsoft 365 email (including Outlook on the "
            "web and mobile) and cloud applications, through Microsoft Entra ID.",
            "Multi-factor authentication (MFA) is enforced through Microsoft Entra ID for Microsoft 365 email "
            "and cloud applications, with exceptions.",
            "Multi-factor authentication (MFA) is not yet enforced for every user of Microsoft 365 email and "
            "cloud applications.",
        ),
        f"{M365_ONLY} Add VPN, remote desktop and any other system people reach from outside the office.",
        collectors=IDENTITY + ("mailboxes",),
    ),
    Question(
        "mfa_privileged",
        "Is MFA enforced for all privileged and administrator accounts?",
        _mfa_admins,
        Source("Admins and roles: privileged role holders", "admins", "Privileged role holders"),
        lead(
            "Multi-factor authentication is enforced for the administrator accounts of Microsoft 365 and "
            "Microsoft Entra ID.",
            "Multi-factor authentication is enforced for administrator accounts in Microsoft 365, with exceptions.",
            "Multi-factor authentication is not enforced for administrator accounts in Microsoft 365.",
        ),
        "Covers Microsoft 365 and Entra ID admin roles. Add admin access to servers, firewalls, backup "
        "consoles and other cloud services.",
        collectors=IDENTITY + ("mailboxes",),
    ),
    Question(
        "legacy_auth",
        "Is legacy authentication blocked?",
        _legacy,
        Source("Legacy sign-ins: what each mailbox allows", "legacy-auth", "What each mailbox allows"),
        lead(
            "Legacy authentication protocols, which can't use multi-factor authentication, are blocked for "
            "Microsoft 365.",
            "Legacy authentication is blocked for Microsoft 365 sign-ins, with one exception.",
        ),
        collectors=("conditional_access", "mailboxes"),
    ),
    Question(
        "separate_admins",
        "Are admin accounts separate from day-to-day user accounts?",
        _separate_admins,
        Source("Admins and roles: privileged role holders", "admins", "Privileged role holders"),
        lead(
            "Administrator accounts are separate from the accounts people use day to day: they have no "
            "mailbox or Microsoft 365 apps licence, so they can't be used for email or everyday work.",
            "Most administrator accounts are separate from day-to-day accounts. There are exceptions.",
            "Administrator roles are held by the same accounts people use day to day.",
        ),
        "Covers Microsoft 365 admin roles. Add local administrator, server and domain admin accounts.",
        collectors=("admin_roles", "users", "mailboxes"),
    ),
    Question(
        "forwarding",
        "Is automatic forwarding of email to external addresses blocked?",
        _forwarding,
        Source("Mail forwarding", "mailboxes", "Forwarding"),
        lead(
            "Automatic forwarding of email to external addresses is blocked in Exchange Online.",
            "Automatic forwarding to external addresses is restricted in Exchange Online, with exceptions.",
            "Automatic forwarding of email to external addresses is not blocked in Exchange Online.",
        ),
        collectors=("mailboxes", "inbox_rules"),
    ),
    Question(
        "email_auth",
        "Are SPF, DKIM and DMARC in place for your email domains?",
        _checks("spf_record", "dkim_enabled", "dmarc_policy"),
        Source("Email domains", "domains"),
        lead(
            "SPF, DKIM and DMARC are configured for all of the organisation's email domains, which protects "
            "them against spoofing.",
            "SPF, DKIM and DMARC are in place for the organisation's email domains, with gaps.",
            "SPF, DKIM and DMARC are not fully configured for the organisation's email domains.",
        ),
        collectors=("domain_health",),
    ),
    Question(
        "audit_logs",
        "Are mailbox audit logs enabled?",
        _checks("mailbox_auditing"),
        Source("Audit logging control", control="logging"),
        lead(
            "Mailbox audit logging is enabled in Exchange Online, recording access to and actions on mailboxes.",
            "Mailbox audit logging is enabled for some mailboxes only.",
            "Mailbox audit logging is not enabled in Exchange Online.",
        ),
        "How long audit logs are kept depends on the Microsoft 365 licence; confirm the retention period.",
        collectors=("mailboxes",),
    ),
    Question(
        "devices_encrypted",
        "Are company laptops and desktops encrypted?",
        _checks("device_encryption"),
        Source("Devices: managed devices", "devices", "Managed devices"),
        lead(
            "Company laptops and desktops managed in Microsoft Intune are encrypted (BitLocker on Windows, "
            "FileVault on macOS).",
            "Most company computers managed in Microsoft Intune are encrypted. There are exceptions.",
            "Company computers managed in Microsoft Intune are not all encrypted.",
        ),
        "Covers computers managed by Intune. Add any computers that aren't.",
        collectors=("devices",),
    ),
    Question(
        "devices_managed",
        "Are company devices centrally managed and required to meet security policies?",
        _checks("intune_enrolled", "device_compliance"),
        Source("Devices: managed devices", "devices", "Managed devices"),
        lead(
            "Company devices are enrolled in Microsoft Intune and must meet its compliance policies.",
            "Company devices are managed with Microsoft Intune, with gaps.",
            "Company devices are not all managed centrally.",
        ),
        "Covers devices joined to Entra ID. Add any company devices that aren't.",
        collectors=("devices",),
    ),
    Question(
        "devices_patched",
        "Are all devices on operating systems that still receive security updates?",
        _checks("supported_windows"),
        Source("Devices: operating systems", "devices", "Operating systems"),
        lead(
            "All managed Windows computers run versions of Windows that still receive security updates.",
            "Some managed Windows computers run versions of Windows that no longer receive security updates.",
            "Managed Windows computers run versions of Windows that no longer receive security updates.",
        ),
        "Covers Windows computers known to Intune or Entra ID. Add servers, macOS, phones, network devices "
        "and other software.",
        collectors=("devices",),
    ),
    Question(
        "leavers",
        "Are leavers' and inactive accounts disabled promptly?",
        _checks("stale_users", "licensed_disabled"),
        Source("Inactive accounts", "inactive", "Inactive members"),
        lead(
            "Leavers' and inactive accounts are disabled promptly in Microsoft Entra ID.",
            "Most leavers' and inactive accounts are disabled, with exceptions.",
            "Some inactive accounts in Microsoft Entra ID have not been disabled.",
        ),
        "Describe the leavers process: who tells IT, and how quickly accounts are disabled.",
        collectors=("users",),
    ),
    Question(
        "guest_review",
        "Do you review guest and external user access?",
        _guests,
        Source("Inactive accounts: inactive guests", "inactive", "Inactive guests"),
        lead(
            "Guest and external user accounts in Microsoft 365 are kept under review.",
            "Guest and external user accounts are reviewed, with exceptions.",
            "Guest and external user accounts are not reviewed regularly.",
        ),
        "Say how often guest access is reviewed, and by whom.",
        collectors=("users",),
    ),
    Question(
        "external_sharing",
        "Is sharing of files with anonymous or external users restricted?",
        _checks("sharepoint_sharing", "anyone_links", "guest_invites"),
        Source("Guests and sharing: external access settings", "guests", "External access settings"),
        lead(
            "Sharing of files with anonymous or external users is restricted in SharePoint and OneDrive.",
            "Sharing with external users is partly restricted in SharePoint and OneDrive, with gaps.",
            "Sharing with anonymous and external users is not restricted in SharePoint and OneDrive.",
        ),
        collectors=("tenant_policies", "shared_files"),
    ),
    Question(
        "third_party_apps",
        "Is access to your data by third-party applications controlled?",
        _checks("user_consent", "risky_app_permissions"),
        Source("Apps and consents", "apps"),
        lead(
            "Third-party application access to Microsoft 365 data is controlled: users can't grant apps "
            "access on their own, and apps hold only the permissions they need.",
            "Third-party application access to Microsoft 365 data is partly controlled, with gaps.",
            "Third-party application access to Microsoft 365 data is not controlled.",
        ),
        collectors=("app_consents", "tenant_policies"),
    ),
    gap(
        "backups",
        "Do you back up Microsoft 365 data (email, OneDrive, SharePoint) separately?",
        "Backups aren't visible in Microsoft 365 data. Answer from the backup product's records.",
        "Microsoft 365 data (Exchange Online mail, OneDrive, SharePoint and Teams) is backed up [daily] by "
        "[backup product or provider] to storage separate from Microsoft 365. Backups are kept for [retention "
        "period], are [immutable or offline], and use credentials separate from the Microsoft 365 administrator "
        "accounts. Restores were last tested on [date].",
        "Add servers and any data held outside Microsoft 365.",
    ),
    gap(
        "training",
        "Do staff receive regular security awareness and phishing training?",
        "Training isn't visible in Microsoft 365 data. Answer from the training provider's records.",
        "All staff complete security awareness training [when they join and every year], through [training "
        "provider or platform]. Simulated phishing exercises run [monthly or quarterly] and the results are "
        "reviewed by [role].",
    ),
)


QUESTIONS = CORE + TOPICS
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
        out.append(Answer(q.key, q.question, answer, why, q.source, topic=q, as_of=_as_of(snap, q)))
    return out


def _as_of(snap: TenantSnapshot, q: Question) -> str | None:
    """When the oldest data this answer uses was collected; None when none of it has been."""
    finished = [snap.runs[c]["finished_at"] for c in q.collectors if c in snap.runs and snap.runs[c]["finished_at"]]
    return min(finished) if finished else None


def overrides(conn, tenant_id: int) -> dict[str, Override]:
    rows = conn.execute("SELECT * FROM evidence_answer_overrides WHERE tenant_id = ?", (tenant_id,)).fetchall()
    return {
        r["question_key"]: Override(r["answer"], r["note"], r["set_by"], r["set_at"], r["suggested"])
        for r in rows
        if r["question_key"] in KEYS
    }


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
            (tenant_id, key, answer, note.strip()[:MAX_NOTE], suggested, username, utcnow()),
        )
        # The note can name the client's people, so the log records only the question and the answer.
        audit(conn, username, "evidence_answer_set", tenant_id, f"{key}: {answer} (suggested {suggested})")


def clear_override(conn, tenant_id: int, key: str, username: str) -> bool:
    with transaction(conn):
        gone = conn.execute(
            "DELETE FROM evidence_answer_overrides WHERE tenant_id = ? AND question_key = ?", (tenant_id, key)
        ).rowcount
        if gone:
            audit(conn, username, "evidence_answer_cleared", tenant_id, key)
    return bool(gone)
