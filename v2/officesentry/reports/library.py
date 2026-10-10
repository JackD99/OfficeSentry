"""The report library: every report in one searchable list, by area and by job to be done.

Detail reports come from ``data_reports.grouped()`` at call time, so a report
declared anywhere (``registry.report``) shows up here without changes. ``USES``
groups reports by the job an MSP is doing ("Offboarding check"): each job lists
its reports, and a report can join one with ``uses=`` in its declaration.
``KEYWORDS`` (from the declarations' ``keywords=``) adds words people search
for that aren't in a report's title or description. Keys that aren't
registered are skipped, so a job can name a report another change adds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import data_reports
from .registry import KEYWORDS, declared_uses


@dataclass(frozen=True)
class Use:
    key: str
    title: str
    description: str
    listed: tuple[str, ...]  # the job's reports, most useful first

    @property
    def reports(self) -> tuple[str, ...]:
        """The listed reports, then any whose declaration names this job in ``uses``."""
        return self.listed + tuple(k for k in declared_uses().get(self.key, ()) if k not in self.listed)


USES: tuple[Use, ...] = (
    Use("offboarding", "Offboarding check",
        "Someone has left: check their access is gone, their mail isn't forwarded and their licences come back.",
        ("inactive", "licences", "mailboxes", "inbox-rules", "mailbox-permissions", "mailbox-holds", "onedrive",
         "shared-files", "admins", "devices", "groups", "group-membership", "deleted-items")),
    Use("quarterly", "Quarterly review",
        "What a client review meeting covers: the summary, what changed, and the trends behind it.",
        ("review", "changes", "secure-score", "mfa", "admins", "conditional-access", "licences", "guests")),
    Use("insurance", "Insurance renewal",
        "The controls cyber insurers ask about, and the reports that prove each one.",
        ("evidence", "mfa", "auth-methods", "admins", "conditional-access", "legacy-auth", "domains", "mailboxes",
         "devices", "admin-activity", "sign-ins", "risky-users")),
    Use("phishing-resistant", "Phishing-resistant MFA rollout",
        "Moving admins, then everyone, to passkeys: who signs in with what, what the methods policy allows, and "
        "which Conditional Access policies require it.",
        ("auth-methods", "mfa", "admins", "conditional-access", "legacy-auth")),
    Use("sharing", "External sharing",
        "Who outside the organisation can reach what: guests, shared files and sites, and mailbox access.",
        ("guests", "shared-files", "sharepoint", "onedrive", "groups", "group-membership", "tenant-settings",
         "mailbox-permissions")),
    Use("storage", "Storage cleanup",
        "What's filling SharePoint, OneDrive and mailboxes, and what can go.",
        ("sharepoint-storage", "sharepoint", "onedrive", "mailbox-size", "shared-mailboxes",
         "mailbox-holds")),
    Use("licences", "Licence savings",
        "Paid licences nobody uses, people who could have a cheaper one, accounts that could be shared mailboxes, "
        "and people who've stopped signing in.",
        ("licences", "licence-rightsizing", "usage", "inactive", "shared-mailboxes")),
    Use("compromise", "Investigate a compromised account",
        "Someone may have someone else's password: where and how they signed in, failed and risky sign-ins, "
        "mail rules and forwarding, new app consents and what admins changed.",
        ("sign-ins", "risky-users", "inbox-rules", "mailboxes", "mailbox-permissions", "legacy-auth", "apps",
         "admin-activity", "mfa")),
    Use("renewal", "Licence renewal",
        "Before a renewal or true-up: what's bought, used and spare, what it costs, and which subscriptions and "
        "trials end soon.",
        ("subscriptions", "licences", "licence-rightsizing", "usage")),
    Use("billing", "Billing check",
        "Check the client's bill against the tenant: products, quantities, prices, trials and suspended "
        "subscriptions, and licence assignment errors.",
        ("subscriptions", "licences")),
)


SUMMARY_AREA = "Summaries"


@dataclass
class Entry:
    key: str
    title: str
    description: str
    area: str
    kind: str = "data"  # data | review | evidence | changes | findings
    uses: tuple[str, ...] = ()
    search: str = ""  # lower-case words to match
    href: str = ""  # set by the page: the report for the chosen tenant, or across all of them
    across: str = ""  # set by the page: the same report across every tenant


@dataclass
class Area:
    key: str
    title: str
    entries: list[Entry] = field(default_factory=list)


SUMMARIES = (
    ("review", "Monthly review", "Security and licensing summary with recommendations, ready to send to the client.",
     "review", "summary pdf client report recommendations"),
    ("evidence", "Questionnaires and evidence", "Answers to paste into cyber insurance, supplier security and "
     "Cyber Essentials questions, with the evidence behind each.",
     "evidence", "cyber insurance questionnaire controls supplier vendor due diligence cyber essentials iasme "
     "danzell proposal renewal form"),
    ("changes", "What changed", "Configuration changes, new and fixed findings, and figures that moved.",
     "changes", "month over month history drift differences"),
    ("findings", "Findings", "Every open finding from the checks, worst first, with accepted risks.",
     "findings", "issues checks risks problems"),
)


def catalogue() -> list[Area]:
    """Every report once, under its area, with the summaries first."""
    uses = {}
    for use in USES:
        for key in use.reports:
            uses.setdefault(key, []).append(use.key)
    areas = [Area("summaries", SUMMARY_AREA, [
        Entry(key, title, desc, SUMMARY_AREA, kind, tuple(uses.get(key, ())),
              f"{title} {desc} {words}".lower())
        for key, title, desc, kind, words in SUMMARIES])]
    for group in data_reports.grouped():
        areas.append(Area(group.key, group.title, [
            Entry(key, title, desc, group.title, "data", tuple(uses.get(key, ())),
                  f"{title} {desc} {group.title} {KEYWORDS.get(key, '')}".lower())
            for key, title, desc in group.reports]))
    return areas


def uses() -> list[tuple[Use, int]]:
    """Each use with how many of its reports exist."""
    known = {e.key for a in catalogue() for e in a.entries}
    return [(u, sum(1 for k in u.reports if k in known)) for u in USES]


def count() -> int:
    return sum(len(a.entries) for a in catalogue())
