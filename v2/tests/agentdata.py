"""AI agents with their own identity in Entra ID (Entra Agent ID, the 2.6 permission batch), with dates relative
to now: one that reads every mailbox, one switched off by Microsoft, one turned off and a new one with nothing
given."""

from __future__ import annotations

from tests.devicedata import days_ago

GRAPH = "https://graph.microsoft.com/v1.0"
AGENTS_URL = f"{GRAPH}/servicePrincipals/microsoft.graph.agentIdentity"
STUDIO = "app-copilot-studio"  # the app that made most of them (in graphdata.SERVICE_PRINCIPALS)
BLUEPRINT = "app-blueprint-helpdesk"


def _agent(id_, name, created, enabled=True, microsoft=None, made_by=STUDIO, blueprint=BLUEPRINT) -> dict:
    return {
        "@odata.type": "#microsoft.graph.agentIdentity",
        "id": id_,
        "appId": "app-" + id_,
        "displayName": name,
        "accountEnabled": enabled,
        "agentIdentityBlueprintId": blueprint,
        "createdByAppId": made_by,
        "createdDateTime": days_ago(created),
        "disabledByMicrosoftStatus": microsoft,
        "servicePrincipalType": "ServiceIdentity",
        "tags": ["helpdesk"],
    }


AGENTS = [
    _agent("ag-mail", "Inbox triage agent", 120),
    _agent("ag-flagged", "Invoice chaser", 60, microsoft="DisabledDueToViolationOfServicesAgreement"),
    _agent("ag-off", "Old FAQ agent", 300, enabled=False),
    _agent("ag-new", "Onboarding buddy", 5, made_by="app-unknown", blueprint=None),
]

# Each agent's app role assignments, by agent ID: the triage agent reads every mailbox (Mail.Read is in
# graphdata.GRAPH_APP_ROLES) and the FAQ agent reads users.
ASSIGNMENTS = {
    "ag-mail": [
        {
            "id": "agr1",
            "resourceId": "sp-graph",
            "resourceDisplayName": "Microsoft Graph",
            "appRoleId": "r-mailread",
            "createdDateTime": days_ago(100),
        }
    ],
    "ag-off": [
        {
            "id": "agr2",
            "resourceId": "sp-graph",
            "resourceDisplayName": "Microsoft Graph",
            "appRoleId": "r-userread",
            "createdDateTime": days_ago(290),
        }
    ],
}

# The apps they were made with, as Apps and consents lists them.
SERVICE_PRINCIPALS = [
    {
        "id": "sp-copilot-studio",
        "appId": STUDIO,
        "displayName": "Copilot Studio",
        "appOwnerOrganizationId": "f8cdef31-a31e-4b4a-93e4-5f571e91255a",
        "servicePrincipalType": "Application",
    },
    {
        "id": "sp-blueprint-helpdesk",
        "appId": BLUEPRINT,
        "displayName": "Helpdesk agent blueprint",
        "appOwnerOrganizationId": "own",
        "servicePrincipalType": "Application",
    },
]
