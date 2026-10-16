"""Mail rules and protection: mail flow (transport) rules, inbound and outbound connectors, and the policies that
filter mail: anti-spam, anti-phishing and anti-malware, Safe Links and Safe Attachments where Defender for
Office 365 is licensed, with the rules that say whom each policy applies to and the preset security policies.

Read-only ``Get-`` cmdlets only, which Global Reader can run. Each part is read on its own: one that fails is
stored as "failed" with a warning, and the others still show. Only settings the report and checks describe
are kept (``RULE_FIELDS``, ``POLICY_FIELDS``); free text (a rule's description, a disclaimer) is cut short.
"""

from __future__ import annotations

from ..exchange import ExchangeAccessDenied, ExchangeError
from .base import Collector, RunContext, register

TEXT_LIMIT = 500  # characters kept of free text: a rule's description or comments, a disclaimer
LIST_LIMIT = 50  # items kept of a list setting (sender domains, file types)
# Allowed sender lists are kept whole (Microsoft allows 1,024 entries), so every entry is checked for free mail or
# the organisation's own domains, and counted.
WHOLE_LISTS = {"AllowedSenderDomains", "AllowedSenders"}
WHOLE_LIST_LIMIT = 1024

RULE_FIELDS = (
    "Name",
    "Guid",
    "State",
    "Mode",
    "Priority",
    "Description",
    "Comments",
    "WhenChanged",
    # Conditions.
    "FromScope",
    "SentToScope",
    "From",
    "SentTo",
    "FromMemberOf",
    "SentToMemberOf",
    "SenderDomainIs",
    "RecipientDomainIs",
    "SenderIpRanges",
    "FromAddressContainsWords",
    "HeaderContainsMessageHeader",
    "HeaderContainsWords",
    "HeaderMatchesMessageHeader",
    "SubjectContainsWords",
    "SubjectOrBodyContainsWords",
    "AttachmentExtensionMatchesWords",
    "AttachmentHasExecutableContent",
    "MessageTypeMatches",
    # Exceptions.
    "ExceptIfFrom",
    "ExceptIfFromScope",
    "ExceptIfSentToScope",
    "ExceptIfSenderDomainIs",
    "ExceptIfSenderIpRanges",
    "ExceptIfFromMemberOf",
    "ExceptIfSentToMemberOf",
    # Actions.
    "SetSCL",
    "RedirectMessageTo",
    "BlindCopyTo",
    "AddToRecipients",
    "CopyTo",
    "DeleteMessage",
    "Quarantine",
    "RejectMessageReasonText",
    "ModerateMessageByUser",
    "ModerateMessageByManager",
    "PrependSubject",
    "ApplyHtmlDisclaimerText",
    "SetHeaderName",
    "SetHeaderValue",
    "RemoveHeader",
    "RouteMessageOutboundConnector",
    "ApplyRightsProtectionTemplate",
    "ApplyOME",
    "StopRuleProcessing",
)
INBOUND_FIELDS = (
    "Name",
    "Enabled",
    "ConnectorType",
    "ConnectorSource",
    "SenderDomains",
    "SenderIPAddresses",
    "RequireTls",
    "RestrictDomainsToCertificate",
    "RestrictDomainsToIPAddresses",
    "TlsSenderCertificateName",
    "EFSkipIPs",
    "EFSkipLastIP",
    "Comment",
    "WhenChanged",
)
OUTBOUND_FIELDS = (
    "Name",
    "Enabled",
    "ConnectorType",
    "ConnectorSource",
    "RecipientDomains",
    "SmartHosts",
    "UseMXRecord",
    "TlsSettings",
    "TlsDomain",
    "IsTransportRuleScoped",
    "RouteAllMessagesViaOnPremises",
    "AllAcceptedDomains",
    "Comment",
    "WhenChanged",
)
COMMON_POLICY_FIELDS = ("Name", "IsDefault", "RecommendedPolicyType", "IsBuiltInProtection", "WhenChanged")
POLICY_FIELDS = {
    "anti_spam": (
        "BulkThreshold",
        "SpamAction",
        "HighConfidenceSpamAction",
        "PhishSpamAction",
        "HighConfidencePhishAction",
        "BulkSpamAction",
        "QuarantineRetentionPeriod",
        "AllowedSenderDomains",
        "AllowedSenders",
        "BlockedSenderDomains",
        "BlockedSenders",
        "SpamZapEnabled",
        "PhishZapEnabled",
        "InlineSafetyTipsEnabled",
    ),
    "anti_phish": (
        "Enabled",
        "EnableSpoofIntelligence",
        "HonorDmarcPolicy",
        "EnableFirstContactSafetyTips",
        "EnableUnauthenticatedSender",
        "EnableViaTag",
        "AuthenticationFailAction",
        "PhishThresholdLevel",
        "EnableMailboxIntelligence",
        "EnableMailboxIntelligenceProtection",
        "EnableOrganizationDomainsProtection",
        "EnableTargetedDomainsProtection",
        "EnableTargetedUserProtection",
    ),
    "anti_malware": (
        "EnableFileFilter",
        "FileTypeAction",
        "FileTypes",
        "ZapEnabled",
        "EnableInternalSenderAdminNotifications",
    ),
    "safe_links": (
        "EnableSafeLinksForEmail",
        "EnableSafeLinksForTeams",
        "EnableSafeLinksForOffice",
        "ScanUrls",
        "DeliverMessageAfterScan",
        "EnableForInternalSenders",
        "TrackClicks",
        "AllowClickThrough",
        "DisableUrlRewrite",
    ),
    "safe_attachments": ("Enable", "Action", "Redirect"),
}
# Counted, not kept: who an anti-phishing policy protects from impersonation is a list of names.
COUNTED = {"anti_phish": ("TargetedUsersToProtect", "TargetedDomainsToProtect")}
# Each kind of policy: its cmdlet, the cmdlet of the rules that scope it and the rule field naming the policy.
POLICIES = {
    "anti_spam": ("Get-HostedContentFilterPolicy", "Get-HostedContentFilterRule", "HostedContentFilterPolicy"),
    "anti_phish": ("Get-AntiPhishPolicy", "Get-AntiPhishRule", "AntiPhishPolicy"),
    "anti_malware": ("Get-MalwareFilterPolicy", "Get-MalwareFilterRule", "MalwareFilterPolicy"),
    "safe_links": ("Get-SafeLinksPolicy", "Get-SafeLinksRule", "SafeLinksPolicy"),
    "safe_attachments": ("Get-SafeAttachmentPolicy", "Get-SafeAttachmentRule", "SafeAttachmentPolicy"),
}
DEFENDER = ("safe_links", "safe_attachments")  # need Defender for Office 365
SCOPE_FIELDS = (
    "SentTo",
    "SentToMemberOf",
    "RecipientDomainIs",
    "ExceptIfSentTo",
    "ExceptIfSentToMemberOf",
    "ExceptIfRecipientDomainIs",
)
# Preset security policies (Standard and Strict): Exchange Online Protection's and Defender's.
PRESETS = {"eop": "Get-EOPProtectionPolicyRule", "defender": "Get-ATPProtectionPolicyRule"}

# The parts the status reports on, as "read", "failed" or "unlicensed".
PARTS = ("rules", "connectors", "anti_spam", "anti_phish", "anti_malware", "safe_links", "safe_attachments", "presets")


def _value(value, limit: int = LIST_LIMIT):
    """A setting as stored: lists cut to ``limit`` items, text to ``TEXT_LIMIT`` characters."""
    if isinstance(value, list):
        return [_value(v) for v in value[:limit]]
    if isinstance(value, str):
        return value[:TEXT_LIMIT]
    return value


def _kept(item: dict, fields: tuple[str, ...]) -> dict:
    return {
        f: _value(item[f], WHOLE_LIST_LIMIT if f in WHOLE_LISTS else LIST_LIMIT)
        for f in fields
        if f in item and item[f] is not None
    }


def scope(rule: dict) -> dict:
    """Whom a policy's rule applies to: its state and priority, the recipients, groups and domains it names
    and leaves out."""
    return {"State": rule.get("State"), "Priority": rule.get("Priority"), **_kept(rule, SCOPE_FIELDS)}


@register
class MailProtection(Collector):
    key = "mail_protection"
    title = "Mail rules and protection"
    description = (
        "Mail flow rules, connectors, and the anti-spam, anti-phishing and anti-malware policies (with Safe Links "
        "and Safe Attachments where Defender for Office 365 is licensed) and whom each applies to (needs Global "
        "Reader)."
    )
    permissions = ("Exchange.ManageAsApp",)

    def collect(self, ctx: RunContext) -> None:
        if ctx.capabilities.get("exchange") is False:
            ctx.log("Tenant has no Exchange Online licence; nothing to collect")
            ctx.objects("mail_protection_status", [("status", {"available": False, "parts": {}})])
            return
        parts = dict.fromkeys(PARTS, "read")
        defender = ctx.capabilities.get("defender_o365_p1") is not False
        if not defender:
            for kind in DEFENDER:
                parts[kind] = "unlicensed"

        rules = self._read(ctx, parts, "rules", "Get-TransportRule")
        if rules is not None:
            ctx.objects(
                "transport_rule",
                [(str(r.get("Guid") or r.get("Name")), _kept(r, RULE_FIELDS)) for r in rules if r.get("Name")],
            )

        inbound = self._read(ctx, parts, "connectors", "Get-InboundConnector")
        outbound = self._read(ctx, parts, "connectors", "Get-OutboundConnector") if inbound is not None else None
        if inbound is not None and outbound is not None:
            ctx.objects(
                "connector",
                [(f"in:{c['Name']}", {"direction": "in", **_kept(c, INBOUND_FIELDS)}) for c in inbound if c.get("Name")]
                + [
                    (f"out:{c['Name']}", {"direction": "out", **_kept(c, OUTBOUND_FIELDS)})
                    for c in outbound
                    if c.get("Name")
                ],
            )
            ctx.metric("mail.connectors", len(inbound) + len(outbound))

        policies = []
        for kind, (cmdlet, rule_cmdlet, field) in POLICIES.items():
            if parts[kind] != "read":
                continue
            found = self._read(ctx, parts, kind, cmdlet)
            scoped = self._read(ctx, parts, kind, rule_cmdlet) if found is not None else None
            if found is None or scoped is None:
                continue
            by_policy = {str(r.get(field)): scope(r) for r in scoped if r.get(field)}
            for p in found:
                if not p.get("Name"):
                    continue
                row = {"kind": kind, **_kept(p, COMMON_POLICY_FIELDS), **_kept(p, POLICY_FIELDS[kind])}
                for counted in COUNTED.get(kind, ()):
                    if isinstance(p.get(counted), list):
                        row[counted] = len(p[counted])
                if p["Name"] in by_policy:
                    row["rule"] = by_policy[p["Name"]]
                policies.append((f"{kind}:{p['Name']}", row))
        ctx.objects("protection_policy", policies)

        presets = []
        for source, cmdlet in PRESETS.items():
            if source == "defender" and not defender:
                continue
            found = self._read(ctx, parts, "presets", cmdlet)
            if found is None:
                break
            presets += [(f"{source}:{r['Name']}", {"source": source, "Name": r["Name"], **scope(r)}) for r in found]
        if parts["presets"] == "read":
            ctx.objects("preset_policy", presets)

        ctx.objects("mail_protection_status", [("status", {"available": True, "parts": parts})])
        if rules is not None:
            live = [r for r in rules if r.get("State") == "Enabled" and r.get("Mode") in (None, "Enforce")]
            ctx.metric("mail.transport_rules", len(rules))
            ctx.metric("mail.rules_skipping_spam_filter", sum(str(r.get("SetSCL")) == "-1" for r in live))
        if parts["anti_spam"] == "read":
            ctx.metric(
                "mail.allowed_sender_domains",
                sum(len(p.get("AllowedSenderDomains") or []) for _, p in policies if p["kind"] == "anti_spam"),
            )

    @staticmethod
    def _read(ctx: RunContext, parts: dict, part: str, cmdlet: str) -> list[dict] | None:
        try:
            return ctx.exchange.invoke(cmdlet)
        except ExchangeAccessDenied:
            raise
        except ExchangeError as exc:
            parts[part] = "failed"
            ctx.warn(f"Couldn't read {cmdlet}: {exc.message}")
            return None
