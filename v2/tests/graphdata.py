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

# -- Phase 4: Exchange, inbox rules, apps, sharing ------------------------------

MICROSOFT = "f8cdef31-a31e-4b4a-93e4-5f571e91255a"

SERVICE_PRINCIPALS = [
    {"id": "sp-graph", "appId": "00000003-0000-0000-c000-000000000000", "displayName": "Microsoft Graph",
     "appOwnerOrganizationId": MICROSOFT, "servicePrincipalType": "Application"},
    {"id": "sp-evil", "appId": "app-evil", "displayName": "Mail Sync Pro", "appOwnerOrganizationId": "other",
     "publisherName": "Unknown Ltd", "servicePrincipalType": "Application"},
    {"id": "sp-zoom", "appId": "app-zoom", "displayName": "Zoom", "appOwnerOrganizationId": "zoom-tenant",
     "verifiedPublisher": {"displayName": "Zoom Video Communications"}, "servicePrincipalType": "Application"},
]
GRAPH_APP_ROLES = {"appRoles": [{"id": "r-mailread", "value": "Mail.Read"},
                                {"id": "r-userread", "value": "User.Read.All"}]}
APP_ROLE_ASSIGNMENTS = {
    "sp-evil": [{"id": "ara1", "resourceId": "sp-graph", "resourceDisplayName": "Microsoft Graph",
                 "appRoleId": "r-mailread", "createdDateTime": "2026-09-20T00:00:00Z"}],
    "sp-zoom": [{"id": "ara2", "resourceId": "sp-graph", "resourceDisplayName": "Microsoft Graph",
                 "appRoleId": "r-userread", "createdDateTime": "2025-01-01T00:00:00Z"}],
}
DELEGATED_GRANTS = [
    {"id": "g1", "clientId": "sp-zoom", "consentType": "AllPrincipals", "resourceId": "sp-graph",
     "scope": "openid User.Read"},
    {"id": "g2", "clientId": "sp-evil", "consentType": "Principal", "principalId": "u1", "resourceId": "sp-graph",
     "scope": "Mail.Read offline_access"},
]
APPLICATIONS = [
    {"id": "app1", "appId": "a1", "displayName": "Old connector", "createdDateTime": "2023-01-01T00:00:00Z",
     "passwordCredentials": [{"displayName": "s1", "endDateTime": "2025-01-01T00:00:00Z"}], "keyCredentials": []},
    {"id": "app2", "appId": "a2", "displayName": "Current connector", "createdDateTime": "2024-01-01T00:00:00Z",
     "passwordCredentials": [{"displayName": "s1", "endDateTime": "2099-01-01T00:00:00Z"}], "keyCredentials": []},
]
AUTHORIZATION_POLICY = {
    "allowInvitesFrom": "everyone", "guestUserRoleId": "10dae51f-b6af-4016-8d66-8c2a99b929b3",
    "defaultUserRolePermissions": {"allowedToCreateApps": True,
                                   "permissionGrantPoliciesAssigned": [
                                       "ManagePermissionGrantsForSelf.microsoft-user-default-legacy"]},
}
SHAREPOINT_SETTINGS = {"sharingCapability": "externalUserAndGuestSharing", "sharingDomainRestrictionMode": "none"}

INBOX_RULES = {
    "u1": [
        {"id": "r1", "displayName": ".", "isEnabled": True, "sequence": 1,
         "conditions": {"bodyContains": ["invoice", "payment"]},
         "actions": {"moveToFolder": "fid-rss", "markAsRead": True, "stopProcessingRules": True}},
        {"id": "r2", "displayName": "Copy to home", "isEnabled": True, "sequence": 2, "conditions": {},
         "actions": {"forwardTo": [{"emailAddress": {"address": "ann.home@gmail.com"}}]}},
    ],
    "u7": [
        {"id": "r3", "displayName": "To manager", "isEnabled": True, "sequence": 1, "conditions": {},
         "actions": {"redirectTo": [{"emailAddress": {"address": "boss@x.com"}}]}},
    ],
}
FOLDERS = {"fid-rss": {"displayName": "RSS Feeds"}}

EXCHANGE = {
    "Get-AcceptedDomain": [{"DomainName": "x.com", "DomainType": "Authoritative", "Default": True}],
    "Get-Mailbox": [
        {"ExternalDirectoryObjectId": "u1", "Guid": "g-u1", "UserPrincipalName": "ann@x.com",
         "PrimarySmtpAddress": "ann@x.com", "DisplayName": "Ann", "RecipientTypeDetails": "UserMailbox",
         "ForwardingSmtpAddress": "smtp:ann@gmail.com", "DeliverToMailboxAndForward": True,
         "GrantSendOnBehalfTo": []},
        {"ExternalDirectoryObjectId": "u7", "Guid": "g-u7", "UserPrincipalName": "info@x.com",
         "PrimarySmtpAddress": "info@x.com", "DisplayName": "Info", "RecipientTypeDetails": "SharedMailbox",
         "GrantSendOnBehalfTo": ["Ann"]},
    ],
    "Get-MailboxPermission": {
        "g-u1": [{"User": "NT AUTHORITY\\SELF", "AccessRights": ["FullAccess"], "IsInherited": False}],
        "g-u7": [{"User": "ann@x.com", "AccessRights": ["FullAccess"], "IsInherited": False},
                 {"User": "NT AUTHORITY\\SELF", "AccessRights": ["FullAccess"], "IsInherited": False}],
    },
    "Get-RecipientPermission": [{"Identity": "Info", "Trustee": "ann@x.com", "AccessRights": ["SendAs"]},
                                {"Identity": "Ann", "Trustee": "NT AUTHORITY\\SELF", "AccessRights": ["SendAs"]}],
    "Get-OrganizationConfig": [{"AuditDisabled": False}],
    "Get-TransportConfig": [{"SmtpClientAuthenticationDisabled": False}],
    "Get-HostedOutboundSpamFilterPolicy": [{"Name": "Default", "AutoForwardingMode": "Automatic", "IsDefault": True}],
    "Get-RemoteDomain": [{"Name": "Default", "DomainName": "*", "AutoForwardEnabled": True}],
    "Get-Recipient": [],
}
INFO_USER = {"id": "u7", "displayName": "Info", "userPrincipalName": "info@x.com", "userType": "Member",
             "accountEnabled": True, "assignedLicenses": [], "signInActivity": {}}
