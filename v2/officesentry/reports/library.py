"""The report library: every report in one searchable list, by area and by job to be done.

Detail reports come from ``data_reports.grouped()`` at call time, so a report
registered anywhere shows up here without changes. ``USES`` groups reports by
the job an MSP is doing ("Offboarding check"); ``KEYWORDS`` adds words people
search for that aren't in a report's title or description. Keys that aren't
registered are skipped, so either can name a report another change adds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import data_reports


@dataclass(frozen=True)
class Use:
    key: str
    title: str
    description: str
    reports: tuple[str, ...]


USES: tuple[Use, ...] = (
    Use("offboarding", "Offboarding check",
        "Someone has left: check their access is gone, their mail isn't forwarded and their licences come back.",
        ("inactive", "licences", "mailboxes", "inbox-rules", "mailbox-permissions", "onedrive", "shared-files",
         "admins", "devices", "groups")),
    Use("quarterly", "Quarterly review",
        "What a client review meeting covers: the summary, what changed, and the trends behind it.",
        ("review", "changes", "secure-score", "mfa", "admins", "conditional-access", "licences", "guests")),
    Use("insurance", "Insurance renewal",
        "The controls cyber insurers ask about, and the reports that prove each one.",
        ("evidence", "mfa", "auth-methods", "admins", "conditional-access", "legacy-auth", "domains", "mailboxes",
         "devices", "admin-activity")),
    Use("phishing-resistant", "Phishing-resistant MFA rollout",
        "Moving admins, then everyone, to passkeys: who signs in with what, what the methods policy allows, and "
        "which Conditional Access policies require it.",
        ("auth-methods", "mfa", "admins", "conditional-access", "legacy-auth")),
    Use("sharing", "External sharing",
        "Who outside the organisation can reach what: guests, shared files and sites, and mailbox access.",
        ("guests", "shared-files", "sharepoint", "onedrive", "groups", "tenant-settings", "mailbox-permissions")),
    Use("storage", "Storage cleanup",
        "What's filling SharePoint, OneDrive and mailboxes, and what can go.",
        ("sharepoint-storage", "sharepoint", "onedrive", "mailbox-size", "shared-mailboxes")),
    Use("licences", "Licence savings",
        "Paid licences nobody uses, people who could have a cheaper one, accounts that could be shared mailboxes, "
        "and people who've stopped signing in.",
        ("licences", "licence-rightsizing", "usage", "inactive", "shared-mailboxes")),
)

# Words people search for that a report's own title and description don't contain.
KEYWORDS: dict[str, str] = {
    "mfa": "multi-factor two-factor 2fa authenticator authentication methods registration",
    "auth-methods": "passkeys fido2 security keys windows hello phishing-resistant passwordless sms text message "
                    "voice call authenticator sspr self-service password reset expiry expiration never expires "
                    "password age authentication strengths methods policy registration campaign nudge",
    "admins": "administrators global admin privileged roles pim break-glass",
    "conditional-access": "ca policies policy require mfa block",
    "inactive": "stale dormant leavers unused accounts sign-in last login",
    "guests": "external users b2b invitations",
    "legacy-auth": "basic auth smtp imap pop old protocols",
    "hybrid": "ad connect entra connect sync on-premises password hash",
    "mailboxes": "forwarding redirect external smtp",
    "inbox-rules": "forward delete move rules mailbox compromise",
    "mailbox-permissions": "full access send as delegates",
    "shared-mailboxes": "rooms equipment sign-in",
    "mailbox-size": "quota storage full large",
    "domains": "spf dkim dmarc dns mta-sts email authentication spoofing",
    "sharepoint": "sites storage sharing anyone links",
    "sharepoint-storage": "quota versions recycle bin large old files cleanup",
    "onedrive": "personal storage leavers",
    "shared-files": "anyone links external sharing links",
    "groups": "teams microsoft 365 groups owners ownerless",
    "devices": "intune compliance bitlocker encryption windows",
    "apps": "enterprise applications oauth consent secrets certificates expiry",
    "licences": "licenses waste cost money unused subscriptions",
    "licence-rightsizing": "licenses downgrade cheaper plan savings cost copilot business basic standard e1 e3",
    "usage": "adoption activity active users teams onedrive outlook office apps copilot",
    "secure-score": "score trend controls",
    "tenant-settings": "defaults user consent authorization policy",
    "admin-activity": "audit log directory changes who changed",
}

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
    ("evidence", "Insurance evidence", "Answers to the controls cyber insurers ask about, with the evidence.",
     "evidence", "cyber insurance questionnaire controls"),
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
