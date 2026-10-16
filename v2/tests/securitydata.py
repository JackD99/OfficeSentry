"""Microsoft Defender alerts and incidents, and Microsoft 365 service health and message centre posts (the 2.4
permission batch), with dates relative to now so what's kept stays stable."""

from __future__ import annotations

from tests.devicedata import days_ago

GRAPH = "https://graph.microsoft.com/v1.0"
ALERTS_URL = f"{GRAPH}/security/alerts_v2"
INCIDENTS_URL = f"{GRAPH}/security/incidents"
HEALTH_URL = f"{GRAPH}/admin/serviceAnnouncement/healthOverviews"
ISSUES_URL = f"{GRAPH}/admin/serviceAnnouncement/issues"
MESSAGES_URL = f"{GRAPH}/admin/serviceAnnouncement/messages"

# A command line an attacker ran: never stored (only accounts and devices are kept from an alert's evidence).
COMMAND_LINE = "powershell.exe -enc SQBFAFgAIAAoAE4AZQB3AC0ATwBiAGoAZQBjAHQA"


def _alert(id_, title, severity, status, source, created, **extra) -> dict:
    return {
        "@odata.type": "#microsoft.graph.security.alert",
        "id": id_,
        "title": title,
        "severity": severity,
        "status": status,
        "serviceSource": source,
        "classification": "unknown",
        "determination": "unknown",
        "assignedTo": None,
        "incidentId": None,
        "createdDateTime": created,
        "lastUpdateDateTime": created,
        "resolvedDateTime": None,
        "alertWebUrl": f"https://security.microsoft.com/alerts/{id_}?tid=t1",
        "description": "Microsoft's description, not kept.",
        "recommendedActions": "Microsoft's advice, not kept.",
        "evidence": [],
        **extra,
    }


def _user(upn=None, account=None, domain=None) -> dict:
    return {
        "@odata.type": "#microsoft.graph.security.userEvidence",
        "userAccount": {"userPrincipalName": upn, "accountName": account, "domainName": domain, "displayName": None},
    }


ALERTS = [
    # Open, high, nobody has picked it up: a phishing mail that reached Ann.
    _alert(
        "a1",
        "Email messages containing malicious URL removed after delivery",
        "high",
        "new",
        "microsoftDefenderForOffice365",
        days_ago(2),
        incidentId="101",
        category="InitialAccess",
        evidence=[
            _user("ann@x.com"),
            {
                "@odata.type": "#microsoft.graph.security.mailboxEvidence",
                "primaryAddress": "ann@x.com",
                "userAccount": {"userPrincipalName": "ann@x.com"},
            },
            {
                "@odata.type": "#microsoft.graph.security.analyzedMessageEvidence",
                "subject": "Your invoice is overdue",
                "recipientEmailAddress": "ann@x.com",
            },
        ],
    ),
    # Open, medium, someone is on it: PowerShell on Bob's laptop, run by a local account.
    _alert(
        "a2",
        "Suspicious PowerShell command line",
        "medium",
        "inProgress",
        "microsoftDefenderForEndpoint",
        days_ago(4),
        incidentId="102",
        assignedTo="helpdesk@msp.example",
        category="Execution",
        evidence=[
            {
                "@odata.type": "#microsoft.graph.security.deviceEvidence",
                "hostName": "LAPTOP-BOB",
                "deviceDnsName": "laptop-bob.x.local",
            },
            _user(account="bob", domain="LAPTOP-BOB"),
            {
                "@odata.type": "#microsoft.graph.security.processEvidence",
                "processCommandLine": COMMAND_LINE,
                "userAccount": {"accountName": "bob", "domainName": "LAPTOP-BOB"},
            },
        ],
    ),
    # Open, low.
    _alert(
        "a3",
        "Unfamiliar sign-in properties",
        "low",
        "new",
        "azureAdIdentityProtection",
        days_ago(1),
        evidence=[_user("carl@x.com")],
    ),
    # Open, informational: listed, never a finding.
    _alert("a4", "eDiscovery search started or exported", "informational", "new", "microsoft365Defender", days_ago(3)),
    # Resolved recently: a real phish that got through to Dan.
    _alert(
        "a5",
        "User clicked a malicious link",
        "high",
        "resolved",
        "microsoftDefenderForOffice365",
        days_ago(9),
        incidentId="103",
        classification="truePositive",
        determination="phishing",
        lastUpdateDateTime=days_ago(5),
        resolvedDateTime=days_ago(5),
        evidence=[_user("dan@x.com")],
    ),
    # Resolved long ago: not kept.
    _alert(
        "a6",
        "Mass download by a single user",
        "medium",
        "resolved",
        "microsoftDefenderForCloudApps",
        days_ago(80),
        lastUpdateDateTime=days_ago(60),
        resolvedDateTime=days_ago(60),
    ),
]


def _incident(id_, name, severity, status, created, **extra) -> dict:
    return {
        "id": id_,
        "displayName": name,
        "severity": severity,
        "status": status,
        "classification": "unknown",
        "determination": "unknown",
        "assignedTo": None,
        "createdDateTime": created,
        "lastUpdateDateTime": created,
        "incidentWebUrl": f"https://security.microsoft.com/incident2/{id_}/overview?tid=t1",
        "customTags": [],
        "description": "Not kept.",
        **extra,
    }


INCIDENTS = [
    _incident("101", "Phishing involving one user", "high", "active", days_ago(2)),
    _incident(
        "102",
        "Execution incident on one endpoint",
        "medium",
        "inProgress",
        days_ago(4),
        assignedTo="helpdesk@msp.example",
    ),
    _incident("103", "Phishing involving one user", "high", "resolved", days_ago(9), lastUpdateDateTime=days_ago(5)),
    _incident("104", "Old incident", "low", "resolved", days_ago(120), lastUpdateDateTime=days_ago(100)),
    _incident("105", "Merged into 101", "high", "redirected", days_ago(2)),
]

HEALTH = [
    {"id": "Exchange", "service": "Exchange Online", "status": "serviceDegradation"},
    {"id": "SharePoint", "service": "SharePoint Online", "status": "serviceOperational"},
    {"id": "microsoftteams", "service": "Microsoft Teams", "status": "ServiceOperational"},
]


def _issue(id_, title, service, classification, status, start, end=None, *, impact, **extra) -> dict:
    return {
        "id": id_,
        "title": title,
        "service": service,
        "feature": "Feature",
        "featureGroup": "Group",
        "classification": classification,
        "status": status,
        "impactDescription": impact,
        "isResolved": end is not None,
        "origin": "microsoft",
        "startDateTime": start,
        "endDateTime": end,
        "lastModifiedDateTime": end or start,
        "posts": [{"description": {"contentType": "html", "content": "<p>Not kept.</p>"}}],
        **extra,
    }


ISSUES = [
    _issue(
        "EX1001",
        "Delayed mail delivery",
        "Exchange Online",
        "incident",
        "serviceDegradation",
        days_ago(1),
        impact="Users may see delays when receiving email from outside the organisation.",
    ),
    _issue(
        "TM2002",
        "Some users can't join meetings",
        "Microsoft Teams",
        "Advisory",
        "investigating",
        days_ago(2),
        impact="Some users may be unable to join Teams meetings from the desktop app.",
    ),
    _issue(
        "SP3003",
        "Sites slow to load",
        "SharePoint Online",
        "Incident",
        "ServiceRestored",
        days_ago(7),
        days_ago(5),
        impact="Users may have found that SharePoint Online sites were slow to load.",
    ),
    _issue(
        "OD4004",
        "Old advisory",
        "OneDrive for Business",
        "advisory",
        "resolved",
        days_ago(70),
        days_ago(60),
        impact="Users may have been unable to sync some files.",
    ),
]


def _message(
    id_, title, start, *, act_by=None, end=None, major=False, category="planForChange", services=("Exchange Online",)
) -> dict:
    return {
        "id": id_,
        "title": title,
        "category": category,
        "severity": "normal",
        "services": list(services),
        "tags": ["Admin impact"],
        "isMajorChange": major,
        "actionRequiredByDateTime": act_by,
        "startDateTime": start,
        "endDateTime": end or days_ago(-90),
        "lastModifiedDateTime": start,
        "body": {"contentType": "html", "content": "<p>Not kept.</p>"},
    }


MESSAGES = [
    _message("MC1001", "Basic authentication retires for SMTP", days_ago(20), act_by=days_ago(-10), major=True),
    _message(
        "MC1002", "Update your Teams Rooms devices", days_ago(15), act_by=days_ago(-60), services=("Microsoft Teams",)
    ),
    _message("MC1003", "New Outlook becomes the default", days_ago(5), major=True),
    _message(
        "MC1004",
        "New reports in the admin centre",
        days_ago(3),
        category="stayInformed",
        services=("Microsoft 365 suite",),
    ),
    _message("MC1005", "An older post still current", days_ago(200), category="stayInformed"),
    _message("MC1006", "A post that has ended", days_ago(100), end=days_ago(10)),
    _message("MC1007", "Past its deadline", days_ago(40), act_by=days_ago(10)),
]
