"""Recorded-style Graph payloads shared by collector, rule and report tests."""

GA = "role-ga"
EXCH = "role-exch"
READER = "role-reader"

ROLE_DEFINITIONS = [
    {"id": GA, "displayName": "Global Administrator", "isBuiltIn": True, "templateId": GA},
    {"id": EXCH, "displayName": "Exchange Administrator", "isBuiltIn": True, "templateId": EXCH},
    {"id": READER, "displayName": "Directory Readers", "isBuiltIn": True, "templateId": READER},
]


def _user_principal(uid, name, upn, user_type="Member", enabled=True):
    return {"@odata.type": "#microsoft.graph.user", "id": uid, "displayName": name,
            "userPrincipalName": upn, "accountEnabled": enabled, "userType": user_type}


ROLE_ASSIGNMENTS = [
    {"id": "a1", "roleDefinitionId": GA, "principalId": "u1", "directoryScopeId": "/",
     "principal": _user_principal("u1", "Ann", "ann@x.com")},
    {"id": "a2", "roleDefinitionId": GA, "principalId": "u4", "directoryScopeId": "/",
     "principal": _user_principal("u4", "Breakglass", "bg@x.com")},
    {"id": "a3", "roleDefinitionId": EXCH, "principalId": "u3", "directoryScopeId": "/",
     "principal": _user_principal("u3", "Guest", "g#EXT#@x.com", user_type="Guest")},
    {"id": "a4", "roleDefinitionId": READER, "principalId": "u2", "directoryScopeId": "/",
     "principal": _user_principal("u2", "Bob", "bob@x.com", enabled=False)},
    {"id": "a5", "roleDefinitionId": EXCH, "principalId": "sp1", "directoryScopeId": "/",
     "principal": {"@odata.type": "#microsoft.graph.servicePrincipal", "id": "sp1", "displayName": "Some app"}},
]

ROLE_ELIGIBILITY = [
    {"id": "e1", "roleDefinitionId": GA, "principalId": "u5", "directoryScopeId": "/",
     "principal": _user_principal("u5", "Eve", "eve@x.com")},
]

CA_MFA_ALL = {
    "id": "ca1", "displayName": "Require MFA for all users", "state": "enabled",
    "conditions": {"users": {"includeUsers": ["All"], "excludeUsers": ["u4"], "includeGroups": [],
                             "excludeGroups": [], "includeRoles": [], "excludeRoles": []},
                   "applications": {"includeApplications": ["All"]}, "clientAppTypes": ["all"]},
    "grantControls": {"operator": "OR", "builtInControls": ["mfa"]},
}
CA_BLOCK_LEGACY = {
    "id": "ca2", "displayName": "Block legacy auth", "state": "enabled",
    "conditions": {"users": {"includeUsers": ["All"], "excludeUsers": []},
                   "applications": {"includeApplications": ["All"]},
                   "clientAppTypes": ["exchangeActiveSync", "other"]},
    "grantControls": {"operator": "OR", "builtInControls": ["block"]},
}
CA_REPORT_ONLY = {
    "id": "ca3", "displayName": "MFA for admins (report only)", "state": "enabledForReportingButNotEnforced",
    "conditions": {"users": {"includeRoles": [GA]}, "applications": {"includeApplications": ["All"]},
                   "clientAppTypes": ["all"]},
    "grantControls": {"operator": "OR", "builtInControls": ["mfa"]},
}

REGISTRATION_DETAILS = [
    {"id": "u1", "userPrincipalName": "ann@x.com", "userDisplayName": "Ann", "userType": "member",
     "isAdmin": True, "isMfaRegistered": True, "isMfaCapable": True,
     "methodsRegistered": ["microsoftAuthenticatorPush"]},
    {"id": "u4", "userPrincipalName": "bg@x.com", "userDisplayName": "Breakglass", "userType": "member",
     "isAdmin": True, "isMfaRegistered": False, "isMfaCapable": False, "methodsRegistered": []},
    {"id": "u6", "userPrincipalName": "carl@x.com", "userDisplayName": "Carl", "userType": "member",
     "isAdmin": False, "isMfaRegistered": False, "isMfaCapable": False, "methodsRegistered": []},
]

SECURE_SCORES = [
    {"createdDateTime": "2026-10-01T00:00:00Z", "currentScore": 42.0, "maxScore": 100.0,
     "controlScores": [{"controlName": "AdminMFAV2", "controlCategory": "Identity", "score": 10,
                        "scoreInPercentage": 100, "implementationStatus": "Done"}]},
    {"createdDateTime": "2026-09-30T00:00:00Z", "currentScore": 40.0, "maxScore": 100.0, "controlScores": []},
]
