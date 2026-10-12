"""Exchange Online answers for mail flow rules, connectors and the email protection policies.

Shapes follow what Get-TransportRule, Get-InboundConnector, Get-OutboundConnector, the Get-*Policy and
Get-*Rule cmdlets of anti-spam, anti-phishing, anti-malware, Safe Links and Safe Attachments, and
Get-EOPProtectionPolicyRule return through the admin API's InvokeCommand, including fields Office Sentry
doesn't keep (identities, object IDs, the rule's own condition objects) and free text longer than it keeps.

The tenant (x.com) has a rule that lets a partner's mail skip spam filtering by domain, one that copies
mail to an outside archive, a gateway's servers allowed past the filter by address, a disabled and a
testing rule that would be flagged if they acted, a partner domain allowed past the anti-spam policy, a
gateway connector without enhanced filtering, and the Standard preset for the Finance group.
"""

from tests.exchangedata import ago

LONG_DISCLAIMER = "<p>This message came from outside the organisation.</p>" + "Take care. " * 200

TRANSPORT_RULES = [
    {
        "Name": "External sender warning",
        "Guid": "6f1c-0001",
        "Identity": "External sender warning",
        "ExchangeObjectId": "6f1c-0001",
        "State": "Enabled",
        "Mode": "Enforce",
        "Priority": 0,
        "FromScope": "NotInOrganization",
        "SentToScope": "InOrganization",
        "PrependSubject": "[EXTERNAL] ",
        "ApplyHtmlDisclaimerText": LONG_DISCLAIMER,
        "Conditions": ["Microsoft.Exchange.MessagingPolicies.Rules.Tasks.FromScopePredicate"],
        "WhenChanged": ago(90),
    },
    {
        "Name": "Partner never spam",
        "Guid": "6f1c-0002",
        "State": "Enabled",
        "Mode": "Enforce",
        "Priority": 1,
        "SenderDomainIs": ["partner.example"],
        "SetSCL": -1,
        "Description": "If the message:\r\n\tIs received from 'partner.example'\r\nTake the following actions:\r\n"
        "\tSet the spam confidence level (SCL) to '-1'",
        "WhenChanged": ago(400),
    },
    {
        "Name": "Copy finance mail",
        "Guid": "6f1c-0003",
        "State": "Enabled",
        "Mode": "Enforce",
        "Priority": 2,
        "SentTo": ["ann@x.com"],
        "BlindCopyTo": ["archive@outside.example"],
        "WhenChanged": ago(20),
    },
    {
        "Name": "Gateway servers",
        "Guid": "6f1c-0004",
        "State": "Enabled",
        "Mode": "Enforce",
        "Priority": 3,
        "SenderIpRanges": ["203.0.113.0/24"],
        "SetSCL": "-1",
        "WhenChanged": ago(700),
    },
    {
        "Name": "Old allow list",
        "Guid": "6f1c-0005",
        "State": "Disabled",
        "Mode": "Enforce",
        "Priority": 4,
        "SenderDomainIs": ["gmail.com"],
        "SetSCL": -1,
        "WhenChanged": ago(900),
    },
    {
        "Name": "Trial redirect",
        "Guid": "6f1c-0006",
        "State": "Enabled",
        "Mode": "Audit",
        "Priority": 5,
        "RedirectMessageTo": ["someone@else.example"],
        "WhenChanged": ago(3),
    },
]

INBOUND_CONNECTORS = [
    {
        "Name": "From mail gateway",
        "Identity": "From mail gateway",
        "Enabled": True,
        "ConnectorType": "Partner",
        "ConnectorSource": "AdminUI",
        "SenderDomains": ["smtp:*;1"],
        "SenderIPAddresses": ["203.0.113.10", "203.0.113.11"],
        "RequireTls": True,
        "RestrictDomainsToIPAddresses": True,
        "RestrictDomainsToCertificate": False,
        "EFSkipIPs": [],
        "EFSkipLastIP": False,
        "WhenChanged": ago(700),
    }
]
OUTBOUND_CONNECTORS = [
    {
        "Name": "To mail gateway",
        "Identity": "To mail gateway",
        "Enabled": True,
        "ConnectorType": "Partner",
        "ConnectorSource": "AdminUI",
        "RecipientDomains": ["*"],
        "SmartHosts": ["relay.gateway.example"],
        "UseMXRecord": False,
        "TlsSettings": "CertificateValidation",
        "IsTransportRuleScoped": False,
        "WhenChanged": ago(700),
    }
]

ANTI_SPAM = [
    {
        "Name": "Default",
        "Identity": "Default",
        "IsDefault": True,
        "BulkThreshold": 7,
        "SpamAction": "MoveToJmf",
        "HighConfidenceSpamAction": "MoveToJmf",
        "PhishSpamAction": "MoveToJmf",
        "HighConfidencePhishAction": "Quarantine",
        "BulkSpamAction": "MoveToJmf",
        "QuarantineRetentionPeriod": 15,
        "AllowedSenderDomains": ["partner.example"],
        "AllowedSenders": [],
        "BlockedSenderDomains": [],
        "BlockedSenders": [],
        "SpamZapEnabled": True,
        "PhishZapEnabled": True,
        "WhenChanged": ago(400),
    },
    {
        "Name": "Standard Preset Security Policy1702000000000",
        "IsDefault": False,
        "RecommendedPolicyType": "Standard",
        "BulkThreshold": 6,
        "SpamAction": "MoveToJmf",
        "HighConfidenceSpamAction": "Quarantine",
        "PhishSpamAction": "Quarantine",
        "HighConfidencePhishAction": "Quarantine",
        "BulkSpamAction": "MoveToJmf",
        "AllowedSenderDomains": [],
        "WhenChanged": ago(100),
    },
]
ANTI_PHISH = [
    {
        "Name": "Office365 AntiPhish Default",
        "IsDefault": True,
        "Enabled": True,
        "EnableSpoofIntelligence": True,
        "HonorDmarcPolicy": True,
        "EnableFirstContactSafetyTips": False,
        "EnableUnauthenticatedSender": True,
        "EnableViaTag": True,
        "AuthenticationFailAction": "MoveToJmf",
        "PhishThresholdLevel": 1,
        "TargetedUsersToProtect": ["Ann;ann@x.com"],
        "WhenChanged": ago(400),
    }
]
ANTI_MALWARE = [
    {
        "Name": "Default",
        "IsDefault": True,
        "EnableFileFilter": True,
        "FileTypeAction": "Reject",
        "FileTypes": [f"ext{n}" for n in range(60)],
        "ZapEnabled": True,
        "WhenChanged": ago(400),
    },
    {
        "Name": "Finance malware",
        "IsDefault": False,
        "EnableFileFilter": True,
        "FileTypes": ["exe", "iso"],
        "ZapEnabled": True,
        "WhenChanged": ago(30),
    },
]
MALWARE_RULES = [
    {
        "Name": "Finance malware",
        "MalwareFilterPolicy": "Finance malware",
        "State": "Enabled",
        "Priority": 0,
        "SentToMemberOf": ["Finance"],
        "Conditions": ["Microsoft.Exchange.MessagingPolicies.Rules.Tasks.SentToMemberOfPredicate"],
    }
]
PRESET_RULES = [
    {
        "Name": "Standard Preset Security Policy",
        "State": "Enabled",
        "Priority": 0,
        "SentToMemberOf": ["Finance"],
    },
    {"Name": "Strict Preset Security Policy", "State": "Disabled", "Priority": 1},
]

# With Defender for Office 365: the built-in protection policies, which cover everyone no other policy does.
SAFE_LINKS = [
    {
        "Name": "Built-In Protection Policy",
        "IsBuiltInProtection": True,
        "EnableSafeLinksForEmail": True,
        "EnableSafeLinksForTeams": True,
        "EnableSafeLinksForOffice": True,
        "ScanUrls": True,
        "AllowClickThrough": False,
        "TrackClicks": True,
    }
]
SAFE_ATTACHMENTS = [{"Name": "Built-In Protection Policy", "IsBuiltInProtection": True, "Enable": True}]

EXCHANGE = {
    "Get-TransportRule": TRANSPORT_RULES,
    "Get-InboundConnector": INBOUND_CONNECTORS,
    "Get-OutboundConnector": OUTBOUND_CONNECTORS,
    "Get-HostedContentFilterPolicy": ANTI_SPAM,
    "Get-HostedContentFilterRule": [],
    "Get-AntiPhishPolicy": ANTI_PHISH,
    "Get-AntiPhishRule": [],
    "Get-MalwareFilterPolicy": ANTI_MALWARE,
    "Get-MalwareFilterRule": MALWARE_RULES,
    "Get-EOPProtectionPolicyRule": PRESET_RULES,
}
DEFENDER = {
    "Get-SafeLinksPolicy": SAFE_LINKS,
    "Get-SafeLinksRule": [],
    "Get-SafeAttachmentPolicy": SAFE_ATTACHMENTS,
    "Get-SafeAttachmentRule": [],
    "Get-ATPProtectionPolicyRule": [
        {"Name": "Standard Preset Security Policy", "State": "Enabled", "Priority": 0, "SentToMemberOf": ["Finance"]}
    ],
}
