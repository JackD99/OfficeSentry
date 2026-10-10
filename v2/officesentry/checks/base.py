"""The check framework: ``@rule`` registers a check in ``RULES``, ``evaluate()`` runs them all.

Each check returns ``(status, summary, findings)``, where status is
  pass         checked, nothing wrong
  fail         checked, with findings
  unavailable  the data isn't there (collector never ran, or the tenant's
               licence doesn't provide it); the reason says which

and may add advice that replaces the default recommendation. Checks never
call Graph; they only read stored snapshots.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..snapshot import TenantSnapshot
from ..text import every, verb

SEVERITIES = ("critical", "high", "medium", "low", "info")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}


@dataclass
class Finding:
    severity: str
    subject: str
    detail: str
    object_id: str = ""
    new: bool = False  # set by reports: first seen (or reopened) in the period

    def __post_init__(self):
        # A name read from a damaged stored object may not be text; fall back to the ID so the
        # finding still sorts, shows and syncs.
        if not isinstance(self.object_id, str):
            self.object_id = ""
        if not isinstance(self.subject, str):
            self.subject = self.object_id or "Unknown"


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

    @property
    def action(self) -> str:
        """The recommendation cut to one short instruction ("Turn on DKIM for each domain in the Defender
        portal"), to sit under the problem in lists. The full recommendation goes in the report."""
        first = re.split(r"(?<=[.!?])\s+(?=[A-Z])", self.recommendation.strip(), maxsplit=1)[0].rstrip(".")
        return re.split(r" \(|, so | so that | so |: | such as ", first, maxsplit=1)[0].rstrip(",;")


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


def _subjects(findings: list[Finding]) -> int:
    """How many different things (domains, say) the findings are about."""
    return len({f.subject for f in findings})


def _all_are(n: int, noun: str) -> str:
    """ "All 4 admins are", or "The 1 admin is"."""
    return f"{every(n, noun, start=True)} {verb(n, 'is', 'are')}"


def _label(row: dict) -> str:
    return row.get("userPrincipalName") or row.get("displayName") or row.get("_id", "?")


def _names(snap: TenantSnapshot) -> dict[str, str]:
    """Best available label for any object ID seen in the tenant's snapshots."""
    names: dict[str, str] = {}
    for rows in (
        snap.objects("conditional_access", "ca_group"),
        snap.objects("admin_roles", "role_assignment"),
        snap.objects("mfa_registration", "mfa_registration"),
        snap.objects("users", "user"),
    ):
        for r in rows or []:
            key = r.get("principalId") or r["_id"]
            label = r.get("userPrincipalName") or r.get("displayName")
            if label:
                names[key] = label
    return names


def evaluate(snap: TenantSnapshot, now: datetime | None = None) -> list[RuleResult]:
    now = now or datetime.now(UTC)
    results = []
    for r in RULES:
        # A check returns (status, summary, findings) and may add advice that replaces the default.
        try:
            status, summary, findings, *advice = r.check(snap, now)
            findings.sort(key=lambda f: (SEVERITY_RANK[f.severity], f.subject.lower()))
        except Exception as exc:  # one broken check mustn't blank the whole report
            logging.getLogger(__name__).exception("Check %s failed", r.key)
            status, summary, findings, advice = "unavailable", f"This check couldn't run: {type(exc).__name__}", [], []
        results.append(
            RuleResult(r.key, r.title, r.category, status, summary, advice[0] if advice else r.recommendation, findings)
        )
    return results
