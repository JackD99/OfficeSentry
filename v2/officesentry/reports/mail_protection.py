"""Mail rules and protection: mail flow rules and what each does, connectors, and the anti-spam, anti-phishing,
anti-malware, Safe Links and Safe Attachments policies with whom each applies to, plus what in them is worth a
look: rules that skip spam filtering or send mail outside, domains allowed past the spam filter, protection
turned off.

Reads ``collectors/mail_protection.py`` (through ``mailprotection.py``) and the accepted domains from
``mailboxes``, to tell addresses outside the organisation from the tenant's own.
"""

from __future__ import annotations

from datetime import datetime

from .. import mailprotection as mp
from ..mailflow import tenant_domains
from ..snapshot import TenantSnapshot
from .data_reports import DataReport, Rows, Section, Tile
from .mail_reports import _no_exchange
from .registry import report

NOT_COLLECTED = (
    "Mail rules and protection haven't been collected yet. Refresh Mail rules and protection on the client page."
)
NO_EXCHANGE = "This client has no Exchange Online, so there's no mail protection to report."

ATTENTION = "Needs attention"
RULES = "Mail flow rules"
CONNECTORS = "Connectors"
POLICIES = "Protection policies"

SKIPS = "Skips spam filtering"
SENDS_OUT = "Sends outside the organisation"
FLAG_ORDER = {"high": 0, "medium": 1, "low": 2, None: 3}
TLS_WORDS = mp.TLS


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _day(value) -> str:
    return _text(value)[:10]


def _worst(problems: list[tuple[str, str]]) -> str | None:
    return min((s for s, _ in problems), key=lambda s: FLAG_ORDER[s], default=None)


def _unread(part: str | None, what: str) -> str:
    if part == "unlicensed":
        return f"{what} need Defender for Office 365."
    return f"{what} couldn't be read; see the Mail rules and protection collection's log."


@report(
    "mail-protection",
    "Mail rules and protection",
    "Mail flow rules and what each does, connectors, and the anti-spam, anti-phishing and anti-malware policies "
    "(Safe Links and Safe Attachments too) with whom each applies to.",
    ("mail_protection", "mailboxes"),
    group="email",
    order=75,
    keywords="transport rules mail flow rules exchange rules connectors smart host gateway mimecast anti-spam "
    "antispam spam filter allow list allowed domains anti-phishing spoof impersonation dmarc anti-malware "
    "attachments safe links safe attachments defender preset standard strict scl bypass redirect bcc",
    uses=("insurance", "compromise"),
)
def mail_protection(snap: TenantSnapshot, now: datetime, out: DataReport) -> None:
    status = mp.status(snap)
    if status is None:
        out.unavailable = NOT_COLLECTED
        return
    if status.get("available") is False or _no_exchange(snap):
        out.unavailable = NO_EXCHANGE
        return
    parts = mp.parts(snap)
    domains = tenant_domains(snap)
    defender = parts.get("safe_links") != "unlicensed"
    needs: list[tuple[list, str]] = []

    attention = Section(
        ATTENTION,
        ["Item", "Kind", "What to check"],
        intro="Rules and settings that let unwanted mail in or company mail out, worst first.",
        empty="Nothing needs attention in the rules, connectors and policies read.",
    )

    rules = Section(
        RULES,
        ["Rule", "On", "Priority", "If", "Then", "Except if", "Sends outside", "Last changed"],
        intro="Exchange's mail flow (transport) rules, in the order they run. Rules testing without acting "
        "(test mode) show as Testing.",
        empty="No mail flow rules." if parts.get("rules") == "read" else _unread(parts.get("rules"), "Mail flow rules"),
    )
    skipping = sending = 0
    found_rules = mp.objects(snap, "transport_rule")
    for r in sorted(found_rules, key=lambda r: r.get("Priority") if isinstance(r.get("Priority"), int) else 999):
        problems = mp.rule_problems(r, domains)
        for severity, text in problems:
            needs.append(([r.get("Name"), "Mail flow rule", text], severity))
        live = mp.is_live(r)
        skipping += live and mp.skips_spam_filter(r)
        outside = live and bool(mp.external_copies(r, domains))
        sending += outside
        state = "Testing" if r.get("State") == "Enabled" and not live else ("Yes" if live else "No")
        rules.add(
            [
                r.get("Name"),
                state,
                r.get("Priority") if isinstance(r.get("Priority"), int) else "",
                mp.conditions(r),
                mp.actions(r),
                mp.exceptions(r),
                outside,
                _day(r.get("WhenChanged")),
            ],
            _worst(problems),
        )

    connectors = Section(
        CONNECTORS,
        ["Connector", "Direction", "Type", "On", "Domains", "Servers", "Encryption", "Last changed"],
        intro="How mail reaches Microsoft 365 and leaves it other than directly: through a filtering service, a "
        "partner, or servers on the premises.",
        empty="No connectors: mail comes and goes directly."
        if parts.get("connectors") == "read"
        else _unread(parts.get("connectors"), "Connectors"),
    )
    found_connectors = mp.objects(snap, "connector")
    for c in sorted(found_connectors, key=lambda c: (c.get("direction") != "in", _text(c.get("Name")).lower())):
        inbound = c.get("direction") == "in"
        problems = mp.connector_problems(c)
        for severity, text in problems:
            needs.append(([c.get("Name"), "Connector", text], severity))
        if inbound:
            servers = mp.text_list(c.get("SenderIPAddresses")) or mp.text_list(c.get("TlsSenderCertificateName"))
            tls = (
                "Required, certificate checked"
                if c.get("RestrictDomainsToCertificate")
                else ("Required" if c.get("RequireTls") else "Not required")
            )
        else:
            servers = mp.text_list(c.get("SmartHosts")) or (["MX records"] if c.get("UseMXRecord") else [])
            tls = TLS_WORDS.get(_text(c.get("TlsSettings")), "Not required")
        domains_field = "SenderDomains" if inbound else "RecipientDomains"
        named = [mp.domain_of(d) for d in mp.text_list(c.get(domains_field))]
        rule_scoped = not inbound and c.get("IsTransportRuleScoped")
        connectors.add(
            [
                c.get("Name"),
                "Incoming" if inbound else "Outgoing",
                _text(c.get("ConnectorType")),
                c.get("Enabled") is not False,
                ["Any domain" if d == "*" else d for d in named] or ("Mail a rule sends here" if rule_scoped else ""),
                servers,
                tls,
                _day(c.get("WhenChanged")),
            ],
            _worst(problems),
        )

    policies = Section(
        POLICIES,
        ["Policy", "Kind", "Applies to", "Preset", "Settings", "Last changed"],
        intro="The default policy of each kind applies to everyone no other policy covers. A preset (Standard or "
        "Strict) overrides the policies below for the people it covers.",
        empty="No protection policies were read.",
        filter_columns=("Kind",),
    )
    kinds = list(mp.KIND_TITLES)
    unread = [
        mp.KIND_TITLES[k]
        for k in kinds
        if parts.get(k) not in (None, "read") and not (k in mp.DEFENDER and not defender)
    ]
    if unread:
        out.notes.append(f"{', '.join(unread)} policies couldn't be read; see the collection's log.")
    if not defender:
        out.notes.append(
            "Safe Links and Safe Attachments need Defender for Office 365, which this client doesn't have."
        )
    found_policies = mp.objects(snap, "protection_policy")
    allowed_domains = 0
    for p in sorted(
        found_policies,
        key=lambda p: (
            kinds.index(p["kind"]) if p.get("kind") in kinds else 9,
            not mp.is_default(p),
            _text(p.get("Name")),
        ),
    ):
        problems = mp.policy_problems(p, domains, defender)
        title = mp.KIND_TITLES.get(_text(p.get("kind")), _text(p.get("kind")))
        for severity, text in problems:
            needs.append(([p.get("Name"), f"{title} policy", text], severity))
        if p.get("kind") == "anti_spam" and mp.in_effect(p):
            allowed_domains += len(mp.text_list(p.get("AllowedSenderDomains")))
        policies.add(
            [
                p.get("Name"),
                title,
                mp.applies_to(p),
                mp.preset_level(p) or "",
                mp.settings(p),
                _day(p.get("WhenChanged")),
            ],
            _worst(problems),
        )

    for row, flag in sorted(needs, key=lambda n: (FLAG_ORDER[n[1]], _text(n[0][1]), _text(n[0][0]).lower())):
        attention.add(row, flag)
    out.sections = [attention, rules, connectors, policies]

    presets = mp.presets_on(snap)
    rules_read = parts.get("rules") == "read"
    out.tiles = [
        Tile(
            "Rules that skip spam filtering",
            skipping if rules_read else "Not read",
            "Mail flow rules turned on" if rules_read else "See the collection's log",
            ("bad" if skipping else "ok") if rules_read else None,
            Rows(RULES, {"Then": SKIPS}),
        ),
        Tile(
            "Rules sending mail outside",
            sending if rules_read else "Not read",
            "Redirect or copy to outside addresses" if rules_read else "See the collection's log",
            ("bad" if sending else "ok") if rules_read else None,
            Rows(RULES, {"Sends outside": "Yes"}),
        ),
        Tile(
            "Allowed sender domains",
            allowed_domains if parts.get("anti_spam") == "read" else "Not read",
            "Skip spam filtering by name alone",
            ("warn" if allowed_domains else "ok") if parts.get("anti_spam") == "read" else None,
            Rows(POLICIES, {"Kind": "Anti-spam"}),
        ),
        Tile(
            "Preset protection",
            " and ".join(sorted(presets)) if presets else ("Off" if parts.get("presets") == "read" else "Not read"),
            "; ".join(f"{level} for {who}" for level, who in sorted(presets.items()))
            or "Microsoft's Standard or Strict presets",
            ("ok" if presets else None),
            Rows(POLICIES),
        ),
        Tile(
            CONNECTORS,
            len(found_connectors) if parts.get("connectors") == "read" else "Not read",
            "Incoming and outgoing",
        ),
    ]
