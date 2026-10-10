"""Cyber-insurance evidence pack: the controls insurers ask about, answered from collected data.

Each control maps to existing checks. The pack states what the data shows and
when it was collected; anything Office Sentry can't see (backups, training)
is listed as "Not assessed" rather than guessed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from .. import coverage
from ..checks import RuleResult
from ..checks.auth_methods import admin_phishing_resistant
from ..snapshot import TenantSnapshot
from ..text import WHOLE_ORGANISATION
from . import evidence_answers, questionnaires
from .evidence_answers import Answer
from .monthly_review import build as build_review
from .workbook import Book, Cover

IN_PLACE, PARTLY, NOT_IN_PLACE, NOT_ASSESSED = "In place", "Partly in place", "Not in place", "Not assessed"


@dataclass(frozen=True)
class Control:
    key: str
    question: str
    rules: tuple[str, ...]
    outside_scope: str = ""  # set when the data can't answer it at all


CONTROLS = (
    Control(
        "mfa_all",
        "Is multi-factor authentication required for all users accessing email and cloud apps?",
        ("mfa_enforced", "users_mfa_registered"),
    ),
    Control(
        "mfa_admins", "Is MFA required for all privileged and administrator accounts?", ("admin_mfa", "admins_ca_mfa")
    ),
    Control(
        "legacy_auth",
        "Are legacy authentication protocols (which can't do MFA) blocked?",
        ("legacy_auth_blocked", "smtp_auth"),
    ),
    Control(
        "privileged",
        "Is administrative access limited to named, internal accounts?",
        ("global_admin_count", "guest_admins", "service_principal_admins", "cloud_only_admins"),
    ),
    Control(
        "email_auth",
        "Are SPF, DKIM and DMARC configured for your email domains?",
        ("spf_record", "dkim_enabled", "dmarc_policy"),
    ),
    Control(
        "forwarding",
        "Is automatic forwarding of email to external addresses blocked or monitored?",
        ("auto_forward_policy", "external_forwarding", "suspicious_inbox_rules"),
    ),
    Control("logging", "Is audit logging enabled for email and user activity?", ("mailbox_auditing",)),
    Control(
        "accounts",
        "Are inactive and leavers' accounts disabled promptly?",
        ("stale_users", "licensed_disabled", "pending_guests"),
    ),
    Control("apps", "Is access by third-party applications controlled?", ("user_consent", "risky_app_permissions")),
    Control("sharing", "Is external file sharing restricted?", ("sharepoint_sharing", "anyone_links", "guest_invites")),
    Control(
        "devices",
        "Are company devices managed, encrypted and kept on supported operating systems?",
        ("device_encryption", "device_compliance", "supported_windows"),
    ),
    Control(
        "backups",
        "Are backups of Microsoft 365 data taken and kept separately?",
        (),
        "Backups are outside what Microsoft 365 reports. Provide evidence from the backup product.",
    ),
    Control(
        "training",
        "Do staff receive security awareness and phishing training?",
        (),
        "Training isn't visible in Microsoft 365 data. Provide records from the training provider.",
    ),
)


@dataclass
class ControlResult:
    control: Control
    status: str
    evidence: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


@dataclass
class EvidencePack:
    tenant: dict
    generated_at: datetime
    controls: list[ControlResult]
    secure_score: float | None
    oldest_data: str | None
    answers: list[Answer] = field(default_factory=list)  # suggested questionnaire answers, with overrides

    @property
    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in (IN_PLACE, PARTLY, NOT_IN_PLACE, NOT_ASSESSED)}
        for c in self.controls:
            out[c.status] += 1
        return out


def _status(results: list[RuleResult]) -> str:
    ran = [r for r in results if r.status != "unavailable"]
    if not ran:
        return NOT_ASSESSED
    if all(r.status == "pass" for r in ran):
        return IN_PLACE if len(ran) == len(results) else PARTLY
    if any(r.status == "pass" for r in ran):
        return PARTLY
    # Nothing that ran passed. The control is missing when a finding is serious or is about the whole
    # organisation (mailbox auditing turned off, at medium); findings about a few accounts are gaps in it.
    failing = [f for r in ran if r.status == "fail" for f in r.findings]
    if any(f.severity in ("critical", "high") or f.subject in WHOLE_ORGANISATION for f in failing):
        return NOT_IN_PLACE
    return PARTLY


def _extra_evidence(key: str, snap: TenantSnapshot, now: datetime | None = None) -> list[str]:
    if key == "mfa_all":
        out = []
        pct = snap.metric("mfa.registered_pct")
        if pct is not None:
            out.append(f"{pct:g}% of checked users have registered an MFA method.")
        try:
            cov = coverage.user_coverage(snap)
        except Exception:  # damaged policy or user data: leave this extra line out rather than the whole pack
            logging.getLogger(__name__).exception("Conditional Access coverage failed")
            cov = None
        defaults = snap.objects("conditional_access", "security_defaults")
        if cov and not (defaults and defaults[0].get("isEnabled")):
            out.append(
                f"{sum(c.mfa for c in cov)} of {len(cov)} enabled accounts are covered by an enforced "
                "Conditional Access policy requiring MFA."
            )
        return out
    if key == "mfa_admins":
        # Phishing-resistant MFA goes beyond the question, so it's evidence, not part of the answer.
        try:
            status, summary, *_ = admin_phishing_resistant(snap, now)
        except Exception:  # damaged data: leave the line out rather than the whole pack
            logging.getLogger(__name__).exception("Phishing-resistant admin check failed")
            return []
        if status == "pass":
            return [_sentence(summary)]
        if status == "fail":
            return [f"Phishing-resistant MFA (passkeys, security keys, Windows Hello): {summary}."]
    return []


def build(conn, tenant, now: datetime | None = None) -> EvidencePack:
    review = build_review(conn, tenant, now)
    snap = TenantSnapshot(conn, tenant)
    by_key = {r.key: r for r in review.results}
    accepted_keys = {r.key for r, _f, _l in review.accepted}
    controls = []
    for c in CONTROLS:
        if c.outside_scope:
            controls.append(ControlResult(c, NOT_ASSESSED, [c.outside_scope]))
            continue
        results = [by_key[k] for k in c.rules if k in by_key]
        status = _status([r if r.status != "accepted" else _as_pass(r) for r in results])
        evidence, gaps = [], []
        for r in results:
            if r.status == "pass":
                evidence.append(_sentence(r.summary))
            elif r.status == "unavailable":
                evidence.append(f"Not checked: {r.title[0].lower() + r.title[1:]} ({r.summary.rstrip('.')}).")
            elif r.status == "accepted":
                evidence.append(f"{r.title}: exceptions accepted as a documented risk.")
            else:
                gaps.append(f"{r.title}: {r.summary}.")
                gaps += [
                    f"  {f.detail}" if f.subject in WHOLE_ORGANISATION else f"  {f.subject}: {f.detail}"
                    for f in r.findings[:5]
                ]
                if len(r.findings) > 5:
                    gaps.append(f"  …and {len(r.findings) - 5} more")
            if r.key in accepted_keys and r.status != "accepted":
                evidence.append(f"{r.title}: some exceptions accepted as documented risks.")
        controls.append(ControlResult(c, status, evidence + _extra_evidence(c.key, snap, review.generated_at), gaps))

    finished = [r["finished_at"] for r in snap.runs.values() if r["finished_at"]]
    answers = evidence_answers.apply_overrides(
        evidence_answers.suggest(snap, by_key), evidence_answers.overrides(conn, tenant["id"])
    )
    return EvidencePack(
        dict(tenant),
        review.generated_at,
        controls,
        snap.metric("secure_score.pct"),
        min(finished) if finished else None,
        answers,
    )


def _sentence(text: str) -> str:
    text = text.strip()
    return text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")


def _as_pass(r: RuleResult) -> RuleResult:
    return RuleResult(r.key, r.title, r.category, "pass", r.summary, r.recommendation, [])


ANSWER_COLUMNS = ["Question", "Answer", "Why", "Answered by", "Evidence"]


def answer_rows(pack: EvidencePack) -> list[list[str]]:
    """The questionnaire answers as exports show them: an override shows its note and who set it, never the
    suggestion it replaced."""
    rows = []
    for a in pack.answers:
        if a.override:
            when = datetime.fromisoformat(a.override.set_at.replace("Z", "+00:00"))
            why, by = a.override.note, f"{a.override.set_by}, {when.day} {when:%b %Y}"
        else:
            why, by = a.why, "Suggested from Microsoft 365 data"
        source = (a.source.label + (" report" if a.source.report else "")) if a.source else ""
        rows.append([a.question, a.answer, why, by, source])
    return rows


def to_xlsx(pack: EvidencePack, brand=None, only: str | None = None) -> bytes:
    """Every question set as its own sheet, then the controls; or just question set ``only``."""
    book = Book(brand.accent if brand else "")
    sheets = [questionnaires.sheet(pack, k) for k in ([only] if only else questionnaires.SETS)]
    for name, rows, description in questionnaires.to_xlsx_tables(sheets):
        book.table(name, questionnaires.COLUMNS, rows, description=description)
    if only:
        n = sheets[0].counts
        return book.save(
            _cover(
                pack,
                brand,
                sheets[0].qset.title,
                [
                    ("Answered from Microsoft 365", n["data"], ""),
                    ("Answered by your team", n["team"], ""),
                    ("Need attention", n["attention"], "Partly or No"),
                    ("Your input needed", n["input"], ""),
                ],
            )
        )
    book.table(
        "Controls",
        ["Question", "Status", "What we found", "Gaps"],
        [
            [c.control.question, c.status, "\n".join(c.evidence), "\n".join(g.strip() for g in c.gaps)]
            for c in pack.controls
        ],
        description="Each control insurers ask about, with the evidence",
    )
    return book.save(_cover(pack, brand, "Security controls evidence"))


def _cover(pack: EvidencePack, brand, title: str, facts=None) -> Cover:
    facts = facts or [(status, n, "") for status, n in pack.counts.items()]
    if pack.secure_score is not None:
        facts.append(("Microsoft Secure Score", f"{pack.secure_score:g}%", "Microsoft's own measure"))
    notes = []
    if pack.oldest_data:
        oldest = datetime.fromisoformat(pack.oldest_data.replace("Z", "+00:00"))
        notes.append(("Oldest data used", f"{oldest.day} {oldest:%b %Y, %H:%M} UTC"))
    notes.append(
        (
            "How to read it",
            "Partly in place means the control exists but has the gaps listed. Not assessed "
            "means Microsoft 365 can't show it, so evidence has to come from elsewhere.",
        )
    )
    notes.append(
        (
            "Your input",
            "Questions Microsoft 365 can't answer have a text to complete: fill in the "
            "[parts in brackets] before you paste it.",
        )
    )
    return Cover(
        title,
        pack.tenant["name"],
        pack.generated_at,
        brand.display_name if brand else "",
        pack.tenant["tenant_ref"],
        brand.accent if brand else "",
        "Answers to security questionnaires, from Microsoft 365 data read with read-only access.",
        facts,
        notes,
    )
