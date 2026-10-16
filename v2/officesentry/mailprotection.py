"""Mail flow rules, connectors and email protection policies as stored by ``collectors/mail_protection.py``, in
plain words, for the Mail rules and protection report and the email checks: what a rule does, whom a policy
applies to, and what in either is worth a look.

Reads stored snapshots only.
"""

from __future__ import annotations

from .mailflow import is_external
from .snapshot import TenantSnapshot

COLLECTOR = "mail_protection"
KIND_TITLES = {
    "anti_spam": "Anti-spam",
    "anti_phish": "Anti-phishing",
    "anti_malware": "Anti-malware",
    "safe_links": "Safe Links",
    "safe_attachments": "Safe Attachments",
}
DEFENDER = ("safe_links", "safe_attachments")  # kinds that need Defender for Office 365
# Free mail domains: allowing one past the spam filter lets anyone with an account there skip it.
FREEMAIL = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "hotmail.co.uk",
        "live.com",
        "live.co.uk",
        "msn.com",
        "yahoo.com",
        "yahoo.co.uk",
        "icloud.com",
        "me.com",
        "aol.com",
        "gmx.com",
        "proton.me",
        "protonmail.com",
    }
)
ACTIONS = {
    "MoveToJmf": "Junk folder",
    "Quarantine": "Quarantine",
    "Delete": "Delete",
    "AddXHeader": "Add a header",
    "ModifySubject": "Mark the subject",
    "Redirect": "Redirect",
    "NoAction": "Nothing",
}
SCOPES = {"InOrganization": "inside the organisation", "NotInOrganization": "outside the organisation"}
TLS = {
    "EncryptionOnly": "TLS",
    "CertificateValidation": "TLS with a valid certificate",
    "DomainValidation": "TLS with a certificate for the domain",
}
STRONG_PHISH_ACTIONS = ("Quarantine", "Delete")
BULK_THRESHOLD = 7  # Microsoft's default; its Standard preset uses 6 and Strict 5


def status(snap: TenantSnapshot) -> dict | None:
    """The collection's status (parts read, failed or unlicensed), or None when it hasn't run."""
    rows = snap.objects(COLLECTOR, "mail_protection_status")
    if rows is None:
        return None
    return rows[0] if rows and isinstance(rows[0], dict) else {}


def parts(snap: TenantSnapshot) -> dict[str, str]:
    found = (status(snap) or {}).get("parts")
    return {k: v for k, v in found.items() if isinstance(v, str)} if isinstance(found, dict) else {}


def objects(snap: TenantSnapshot, kind: str) -> list[dict]:
    return [o for o in snap.objects(COLLECTOR, kind) or [] if isinstance(o, dict)]


def policies(snap: TenantSnapshot, kind: str) -> list[dict]:
    return [p for p in objects(snap, "protection_policy") if p.get("kind") == kind]


def text_list(value) -> list[str]:
    """A list setting as text items, whatever shape it came in."""
    if isinstance(value, str):
        return [value] if value else []
    return [str(v) for v in value if isinstance(v, (str, int))] if isinstance(value, list) else []


def domain_of(entry: str) -> str:
    """A domain from a connector's ``smtp:contoso.com;1`` form, or as written."""
    text = entry.split(":", 1)[1] if entry.lower().startswith("smtp:") else entry
    return text.split(";", 1)[0].strip().lower()


def _named(value) -> str:
    return ", ".join(text_list(value))


# -- Mail flow rules -----------------------------------------------------------------------------------------


def is_live(rule: dict) -> bool:
    """A rule that's turned on and enforced, not just testing (Mode Audit or AuditAndNotify)."""
    return rule.get("State") == "Enabled" and rule.get("Mode") in (None, "Enforce")


def skips_spam_filter(rule: dict) -> bool:
    return str(rule.get("SetSCL")) == "-1"


def spoofable(rule: dict) -> bool:
    """Whether a rule picks mail out only by what a sender writes about themselves (their address, domain or a
    header), which anyone can fake, rather than by the server it came from."""
    return not text_list(rule.get("SenderIpRanges"))


def copies_to(rule: dict) -> list[str]:
    """Everyone a rule sends the mail, or a copy of it, to."""
    out: list[str] = []
    for field in ("RedirectMessageTo", "BlindCopyTo", "AddToRecipients", "CopyTo"):
        out += [r for r in text_list(rule.get(field)) if r not in out]
    return out


def external_copies(rule: dict, domains: set[str] | None) -> list[str]:
    """The addresses outside the organisation a rule sends mail or copies to. Recipients named without an
    address are people in the directory."""
    return [r for r in copies_to(rule) if domains and is_external(r, domains)]


def conditions(rule: dict) -> list[str]:
    """What a rule picks mail out by, in plain words."""
    out = []
    if rule.get("FromScope") in SCOPES:
        out.append(f"From {SCOPES[rule['FromScope']]}")
    if rule.get("SentToScope") in SCOPES:
        out.append(f"To {SCOPES[rule['SentToScope']]}")
    for field, words in (
        ("From", "From"),
        ("FromMemberOf", "From members of"),
        ("SenderDomainIs", "From domains"),
        ("SenderIpRanges", "From servers"),
        ("FromAddressContainsWords", "Sender address contains"),
        ("SentTo", "To"),
        ("SentToMemberOf", "To members of"),
        ("RecipientDomainIs", "To domains"),
        ("SubjectContainsWords", "Subject contains"),
        ("SubjectOrBodyContainsWords", "Subject or body contains"),
        ("AttachmentExtensionMatchesWords", "Attachment types"),
    ):
        if text_list(rule.get(field)):
            out.append(f"{words} {_named(rule[field])}")
    if rule.get("HeaderContainsMessageHeader") or rule.get("HeaderMatchesMessageHeader"):
        out.append(f"Header {rule.get('HeaderContainsMessageHeader') or rule.get('HeaderMatchesMessageHeader')}")
    if rule.get("AttachmentHasExecutableContent"):
        out.append("Has a program attached")
    if isinstance(rule.get("MessageTypeMatches"), str):
        out.append(f"Message type {rule['MessageTypeMatches']}")
    return out or ["Every message"]


def exceptions(rule: dict) -> list[str]:
    out = []
    if rule.get("ExceptIfFromScope") in SCOPES:
        out.append(f"From {SCOPES[rule['ExceptIfFromScope']]}")
    if rule.get("ExceptIfSentToScope") in SCOPES:
        out.append(f"To {SCOPES[rule['ExceptIfSentToScope']]}")
    for field, words in (
        ("ExceptIfFrom", "From"),
        ("ExceptIfFromMemberOf", "From members of"),
        ("ExceptIfSenderDomainIs", "From domains"),
        ("ExceptIfSenderIpRanges", "From servers"),
        ("ExceptIfSentToMemberOf", "To members of"),
    ):
        if text_list(rule.get(field)):
            out.append(f"{words} {_named(rule[field])}")
    return out


def actions(rule: dict) -> list[str]:
    """What a rule does to the mail it picks out, in plain words."""
    out = []
    scl = str(rule.get("SetSCL"))
    if scl == "-1":
        out.append("Skips spam filtering")
    elif scl.isdigit() and int(scl) >= 5:
        out.append("Marks it as spam")
    if text_list(rule.get("RedirectMessageTo")):
        out.append(f"Redirects it to {_named(rule['RedirectMessageTo'])}")
    for field in ("BlindCopyTo", "AddToRecipients", "CopyTo"):
        if text_list(rule.get(field)):
            out.append(f"Sends a copy to {_named(rule[field])}")
    if rule.get("DeleteMessage"):
        out.append("Deletes it without telling anyone")
    if rule.get("Quarantine"):
        out.append("Quarantines it")
    if rule.get("RejectMessageReasonText"):
        out.append("Rejects it")
    if text_list(rule.get("ModerateMessageByUser")) or rule.get("ModerateMessageByManager"):
        out.append("Holds it for approval")
    if rule.get("PrependSubject"):
        out.append(f"Adds “{str(rule['PrependSubject']).strip()}” to the subject")
    if rule.get("ApplyHtmlDisclaimerText"):
        out.append("Adds a disclaimer or banner")
    if rule.get("SetHeaderName"):
        out.append(f"Sets the header {rule['SetHeaderName']}")
    if rule.get("RemoveHeader"):
        out.append(f"Removes the header {rule['RemoveHeader']}")
    if rule.get("RouteMessageOutboundConnector"):
        out.append(f"Sends it through the connector {rule['RouteMessageOutboundConnector']}")
    if rule.get("ApplyOME") or rule.get("ApplyRightsProtectionTemplate"):
        out.append("Encrypts it")
    if rule.get("StopRuleProcessing"):
        out.append("Stops later rules")
    return out or ["Nothing the report describes; see the rule's description"]


def rule_problems(rule: dict, domains: set[str] | None) -> list[tuple[str, str]]:
    """(severity, what to check) for a rule that's turned on and enforced."""
    if not is_live(rule):
        return []
    out = []
    if skips_spam_filter(rule):
        if spoofable(rule):
            out.append(("high", "Lets mail skip spam filtering by who it claims to be from, which anyone can fake"))
        else:
            out.append(("medium", "Lets mail from these servers skip spam filtering"))
    outside = external_copies(rule, domains)
    if outside:
        out.append(("high", f"Sends mail or copies outside the organisation, to {', '.join(outside)}"))
    if rule.get("DeleteMessage"):
        out.append(("low", "Deletes mail without telling the sender or the recipient"))
    return out


# -- Connectors ----------------------------------------------------------------------------------------------


def connector_problems(connector: dict) -> list[tuple[str, str]]:
    if connector.get("Enabled") is False:
        return []
    out = []
    if connector.get("direction") == "in":
        if connector.get("ConnectorType") == "Partner" and not (
            connector.get("RequireTls") or connector.get("RestrictDomainsToCertificate")
        ):
            out.append(("low", "Doesn't require mail from this partner to be encrypted (TLS)"))
        gateway = "*" in [domain_of(d) for d in text_list(connector.get("SenderDomains"))]
        if gateway and not (text_list(connector.get("EFSkipIPs")) or connector.get("EFSkipLastIP")):
            out.append(
                (
                    "low",
                    "Mail arrives through another filtering service, but enhanced filtering isn't on, so Microsoft "
                    "judges the service's address instead of the real sender's",
                )
            )
    elif text_list(connector.get("SmartHosts")) and not connector.get("TlsSettings"):
        out.append(("low", "Sends mail through a smart host without requiring TLS"))
    return out


# -- Policies ------------------------------------------------------------------------------------------------


def is_default(policy: dict) -> bool:
    return policy.get("IsDefault") is True or policy.get("IsBuiltInProtection") is True


def preset_level(policy: dict) -> str | None:
    level = policy.get("RecommendedPolicyType")
    return level if level in ("Standard", "Strict") else None


def in_effect(policy: dict) -> bool:
    """Whether a policy applies to anyone: the default (or built-in) one applies to everyone no other policy
    covers; any other one through its rule, while the rule is on."""
    if is_default(policy):
        return True
    rule = policy.get("rule")
    return isinstance(rule, dict) and rule.get("State") == "Enabled"


def applies_to(policy: dict) -> list[str] | str:
    """Whom a policy applies to, in words."""
    if policy.get("IsBuiltInProtection"):
        return "Everyone no other policy covers (built-in protection)"
    if is_default(policy):
        return "Everyone no other policy covers"
    rule = policy.get("rule")
    if preset_level(policy) and not isinstance(rule, dict):
        return f"Whoever the {preset_level(policy)} preset covers"
    if not isinstance(rule, dict):
        return "Nobody (it has no rule)"
    if rule.get("State") != "Enabled":
        return "Nobody (its rule is off)"
    out = []
    for field, words in (
        ("SentTo", ""),
        ("SentToMemberOf", "Members of "),
        ("RecipientDomainIs", "Domain "),
        ("ExceptIfSentTo", "Except "),
        ("ExceptIfSentToMemberOf", "Except members of "),
        ("ExceptIfRecipientDomainIs", "Except domain "),
    ):
        out += [words + v for v in text_list(rule.get(field))]
    return out or "Everyone"


def _some(names: list[str], shown: int = 10) -> str:
    """The first few names and how many more, so a long allow list stays one readable line."""
    more = len(names) - shown
    return ", ".join(names[:shown]) + (f" and {more} more" if more > 0 else "")


def risky_domains(allowed: list[str], domains: set[str] | None) -> list[str]:
    """The allowed sender domains anyone can send from: free mail, or the organisation's own (which attackers
    fake to look like a colleague)."""
    return [d for d in allowed if d in FREEMAIL or (domains and not is_external(f"x@{d}", domains))]


def _settings_off(policy: dict, fields: tuple[tuple[str, str, str], ...]) -> list[tuple[str, str]]:
    return [(severity, text) for field, severity, text in fields if policy.get(field) is False]


def policy_problems(policy: dict, domains: set[str] | None, defender: bool = False) -> list[tuple[str, str]]:
    """(severity, what to check) for a policy that applies to someone."""
    if not in_effect(policy):
        return []
    kind, out = policy.get("kind"), []
    if kind == "anti_spam":
        allowed = [d.lower() for d in text_list(policy.get("AllowedSenderDomains"))]
        risky = risky_domains(allowed, domains)
        if allowed:
            text = f"Lets mail claiming to be from {_some(allowed)} skip spam filtering"
            if risky:
                text += f"; {_some(risky)} {'is' if len(risky) == 1 else 'are'} free mail or the organisation's own"
            out.append(("high" if risky else "medium", text))
        if text_list(policy.get("AllowedSenders")):
            out.append(
                ("low", f"Lets mail from {len(text_list(policy['AllowedSenders']))} addresses skip spam filtering")
            )
        action = policy.get("HighConfidencePhishAction")
        if isinstance(action, str) and action not in STRONG_PHISH_ACTIONS:
            out.append(("high", f"High confidence phishing goes to {ACTIONS.get(action, action)}, not quarantine"))
        threshold = policy.get("BulkThreshold")
        if isinstance(threshold, int) and threshold > BULK_THRESHOLD:
            out.append(("low", f"Bulk mail threshold is {threshold}, so most marketing mail reaches the inbox"))
        out += _settings_off(
            policy,
            (
                ("PhishZapEnabled", "low", "Phishing isn't removed from inboxes once it's found (ZAP off)"),
                ("SpamZapEnabled", "low", "Spam isn't removed from inboxes once it's found (ZAP off)"),
            ),
        )
    elif kind == "anti_phish":
        if policy.get("Enabled") is False and not is_default(policy):
            return []
        out += _settings_off(
            policy,
            (
                (
                    "EnableSpoofIntelligence",
                    "high",
                    "Spoof intelligence is off, so mail pretending to be from other domains isn't caught",
                ),
                ("HonorDmarcPolicy", "medium", "Ignores what senders' DMARC policies ask for"),
                ("EnableUnauthenticatedSender", "low", "Doesn't mark senders that fail authentication with a “?”"),
            ),
        )
        if defender and not any(
            policy.get(f)
            for f in (
                "EnableMailboxIntelligenceProtection",
                "EnableOrganizationDomainsProtection",
                "EnableTargetedUserProtection",
                "EnableTargetedDomainsProtection",
            )
        ):
            out.append(("low", "Impersonation protection (Defender for Office 365) isn't turned on"))
    elif kind == "anti_malware":
        out += _settings_off(
            policy,
            (
                ("EnableFileFilter", "medium", "Doesn't block common risky attachment types (.exe, .iso and the like)"),
                ("ZapEnabled", "low", "Malware isn't removed from inboxes once it's found (ZAP off)"),
            ),
        )
    elif kind == "safe_links":
        out += _settings_off(
            policy,
            (
                ("EnableSafeLinksForEmail", "medium", "Doesn't check links in email"),
                ("ScanUrls", "low", "Doesn't check links before delivering the message"),
            ),
        )
        if policy.get("AllowClickThrough"):
            out.append(("low", "Lets people click through to a link found to be malicious"))
    elif kind == "safe_attachments":
        if policy.get("Enable") is False:
            out.append(("medium", "Doesn't open attachments in a sandbox before delivering them"))
        elif policy.get("Action") == "Allow":
            out.append(("medium", "Delivers attachments found to be malicious"))
    return out


def settings(policy: dict) -> list[str]:
    """A policy's settings worth seeing at a glance."""
    kind, out = policy.get("kind"), []
    if kind == "anti_spam":
        for field, words in (
            ("SpamAction", "Spam"),
            ("HighConfidenceSpamAction", "High confidence spam"),
            ("PhishSpamAction", "Phishing"),
            ("HighConfidencePhishAction", "High confidence phishing"),
            ("BulkSpamAction", "Bulk"),
        ):
            if isinstance(policy.get(field), str):
                out.append(f"{words}: {ACTIONS.get(policy[field], policy[field])}")
        if isinstance(policy.get("BulkThreshold"), int):
            out.append(f"Bulk threshold {policy['BulkThreshold']}")
    elif kind == "anti_phish":
        for field, words in (
            ("EnableSpoofIntelligence", "Spoof intelligence"),
            ("HonorDmarcPolicy", "Honours DMARC"),
            ("EnableUnauthenticatedSender", "Unauthenticated sender mark"),
            ("EnableFirstContactSafetyTips", "First contact tip"),
            ("EnableMailboxIntelligenceProtection", "Mailbox intelligence"),
            ("EnableTargetedUserProtection", "Protects named people"),
            ("EnableOrganizationDomainsProtection", "Protects own domains"),
        ):
            if isinstance(policy.get(field), bool):
                out.append(f"{words}: {'on' if policy[field] else 'off'}")
    elif kind == "anti_malware":
        if isinstance(policy.get("EnableFileFilter"), bool):
            types = len(text_list(policy.get("FileTypes")))
            out.append(
                f"Common attachments filter: on ({types} file types)"
                if policy["EnableFileFilter"]
                else "Common attachments filter: off"
            )
        if isinstance(policy.get("ZapEnabled"), bool):
            out.append(f"Removes malware after delivery: {'on' if policy['ZapEnabled'] else 'off'}")
    elif kind == "safe_links":
        for field, words in (
            ("EnableSafeLinksForEmail", "Email"),
            ("EnableSafeLinksForTeams", "Teams"),
            ("EnableSafeLinksForOffice", "Office apps"),
            ("ScanUrls", "Checks before delivery"),
            ("AllowClickThrough", "Click-through allowed"),
        ):
            if isinstance(policy.get(field), bool):
                out.append(f"{words}: {'on' if policy[field] else 'off'}")
    elif kind == "safe_attachments":
        if isinstance(policy.get("Enable"), bool):
            out.append("On" if policy["Enable"] else "Off")
        if isinstance(policy.get("Action"), str):
            out.append(f"Malicious attachments: {policy['Action']}")
    return out


def presets_on(snap: TenantSnapshot) -> dict[str, str]:
    """The preset security policies turned on ("Standard", "Strict"), each with whom it covers in words."""
    out: dict[str, str] = {}
    for p in objects(snap, "preset_policy"):
        name = str(p.get("Name") or "")
        level = "Strict" if "Strict" in name else "Standard" if "Standard" in name else None
        if level and p.get("State") == "Enabled":
            named = [v for f in ("SentTo", "SentToMemberOf", "RecipientDomainIs") for v in text_list(p.get(f))]
            out[level] = "some people" if named else "everyone"
    return out
