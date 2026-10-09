"""Recorded-style Graph payloads shared by collector, rule and report tests."""

from datetime import UTC, datetime, timedelta

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
     "methodsRegistered": ["microsoftAuthenticatorPush"], "isSsprRegistered": True, "isSsprCapable": True,
     "isPasswordlessCapable": False, "isSystemPreferredAuthenticationMethodEnabled": True,
     "systemPreferredAuthenticationMethods": ["Push"], "userPreferredMethodForSecondaryAuthentication": "push"},
    {"id": "u4", "userPrincipalName": "bg@x.com", "userDisplayName": "Breakglass", "userType": "member",
     "isAdmin": True, "isMfaRegistered": False, "isMfaCapable": False, "methodsRegistered": [],
     "isSsprRegistered": False, "isPasswordlessCapable": False},
    {"id": "u6", "userPrincipalName": "carl@x.com", "userDisplayName": "Carl", "userType": "member",
     "isAdmin": False, "isMfaRegistered": False, "isMfaCapable": False, "methodsRegistered": [],
     "isSsprRegistered": False, "isPasswordlessCapable": False},
]

# /policies/authenticationMethodsPolicy: passkeys for one group, SMS on for everyone else, voice off.
AUTH_METHODS_POLICY = {
    "id": "authenticationMethodsPolicy", "displayName": "Authentication Methods Policy",
    "policyVersion": "1.5", "policyMigrationState": "migrationComplete",
    "lastModifiedDateTime": "2026-06-01T09:00:00Z",
    "registrationEnforcement": {"authenticationMethodsRegistrationCampaign": {
        "snoozeDurationInDays": 1, "enforceRegistrationAfterAllowedSnoozes": True, "state": "default",
        "excludeTargets": [], "includeTargets": [{"id": "all_users", "targetType": "group",
                                                  "targetedAuthenticationMethod": "microsoftAuthenticator"}]}},
    "systemCredentialPreferences": {"state": "disabled", "excludeTargets": [],
                                    "includeTargets": [{"id": "all_users", "targetType": "group"}]},
    "reportSuspiciousActivitySettings": {"state": "default", "voiceReportingCode": 0,
                                         "includeTarget": {"targetType": "group", "id": "all_users"}},
    "authenticationMethodConfigurations": [
        {"@odata.type": "#microsoft.graph.fido2AuthenticationMethodConfiguration", "id": "Fido2", "state": "enabled",
         "isSelfServiceRegistrationAllowed": True, "isAttestationEnforced": False,
         "includeTargets": [{"targetType": "group", "id": "grp-admins", "isRegistrationRequired": False}],
         "excludeTargets": []},
        {"@odata.type": "#microsoft.graph.microsoftAuthenticatorAuthenticationMethodConfiguration",
         "id": "MicrosoftAuthenticator", "state": "enabled", "isSoftwareOathEnabled": False,
         "featureSettings": {"displayAppInformationRequiredState": {"state": "default"},
                             "displayLocationInformationRequiredState": {"state": "disabled"}},
         "includeTargets": [{"targetType": "group", "id": "all_users", "isRegistrationRequired": False,
                             "authenticationMode": "any"}], "excludeTargets": []},
        {"@odata.type": "#microsoft.graph.smsAuthenticationMethodConfiguration", "id": "Sms", "state": "enabled",
         "includeTargets": [{"targetType": "group", "id": "all_users", "isRegistrationRequired": False,
                             "isUsableForSignIn": False}],
         "excludeTargets": [{"targetType": "group", "id": "grp-admins"}]},
        {"@odata.type": "#microsoft.graph.voiceAuthenticationMethodConfiguration", "id": "Voice", "state": "disabled",
         "isOfficePhoneAllowed": False, "includeTargets": [], "excludeTargets": []},
        {"@odata.type": "#microsoft.graph.emailAuthenticationMethodConfiguration", "id": "Email", "state": "enabled",
         "allowExternalIdToUseEmailOtp": "default",
         "includeTargets": [{"targetType": "group", "id": "all_users"}], "excludeTargets": []},
        {"@odata.type": "#microsoft.graph.temporaryAccessPassAuthenticationMethodConfiguration",
         "id": "TemporaryAccessPass", "state": "disabled", "defaultLifetimeInMinutes": 60, "isUsableOnce": False,
         "includeTargets": [], "excludeTargets": []},
    ],
}

SECURE_SCORES = [
    {"createdDateTime": "2026-10-01T00:00:00Z", "currentScore": 42.0, "maxScore": 100.0,
     "controlScores": [{"controlName": "AdminMFAV2", "controlCategory": "Identity", "score": 10,
                        "scoreInPercentage": 100, "implementationStatus": "Done"}]},
    {"createdDateTime": "2026-09-30T00:00:00Z", "currentScore": 40.0, "maxScore": 100.0, "controlScores": []},
]

# Secure Score with a month of history, for the trend report's control changes.
SECURE_SCORES_HISTORY = [
    {"createdDateTime": "2026-10-01T00:00:00Z", "currentScore": 42.0, "maxScore": 100.0,
     "controlScores": [{"controlName": "AdminMFAV2", "controlCategory": "Identity", "score": 10},
                       {"controlName": "BlockLegacyAuthentication", "controlCategory": "Identity", "score": 4}]},
    SECURE_SCORES[1],
    {"createdDateTime": "2026-08-31T00:00:00Z", "currentScore": 35.0, "maxScore": 100.0,
     "controlScores": [{"controlName": "AdminMFAV2", "controlCategory": "Identity", "score": 5},
                       {"controlName": "BlockLegacyAuthentication", "controlCategory": "Identity", "score": 8}]},
]
SECURE_SCORE_PROFILES = [
    {"id": "AdminMFAV2", "title": "Ensure multifactor authentication is enabled for all users in administrative roles",
     "maxScore": 10, "controlCategory": "Identity", "userImpact": "Low", "implementationCost": "Low"},
    {"id": "BlockLegacyAuthentication", "title": "Enable Conditional Access policies to block legacy authentication",
     "maxScore": 8, "controlCategory": "Identity", "userImpact": "Moderate", "implementationCost": "Moderate"},
]

# Per-user MFA states by user ID (beta /users/{id}/authentication/requirements).
PER_USER_MFA = {"u1": "enforced"}

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
# -- Office Sentry's own app in the client tenant (the connection collector) -----------------
# conftest's app connection signs in as OUR_APP; seeded tenants grant it every permission and Global Reader.

OUR_APP = "11111111-2222-3333-4444-555555555555"
OUR_SP = {"id": "sp-officesentry", "appId": OUR_APP, "displayName": "Office Sentry (read-only)",
          "accountEnabled": True}
GLOBAL_READER = "f2ef992c-3afb-46b9-b7cf-a126ee74c451"  # Microsoft's built-in role template ID


def _role_id(permission: str) -> str:
    return {"User.Read.All": "r-userread"}.get(permission, "r-" + permission.lower())


def _resource_roles():
    from officesentry.entra import EXCHANGE_APP_ID, GRAPH_APP_ID, GRAPH_PERMISSIONS

    graph = [{"id": "r-mailread", "value": "Mail.Read"}] + [{"id": _role_id(p), "value": p} for p in GRAPH_PERMISSIONS]
    return {"sp-graph": {"id": "sp-graph", "appId": GRAPH_APP_ID, "appRoles": graph},
            "sp-exchange": {"id": "sp-exchange", "appId": EXCHANGE_APP_ID,
                            "appRoles": [{"id": "r-exo", "value": "Exchange.ManageAsApp"},
                                         {"id": "r-exo-full", "value": "full_access_as_app"}]}}


RESOURCE_APP_ROLES = _resource_roles()


def our_assignments(missing=()) -> list[dict]:
    """The app's granted application permissions, less ``missing``."""
    from officesentry.entra import GRAPH_PERMISSIONS

    rows = [{"id": f"our-{p}", "resourceId": "sp-graph", "resourceDisplayName": "Microsoft Graph",
             "appRoleId": _role_id(p), "principalId": OUR_SP["id"]} for p in GRAPH_PERMISSIONS if p not in missing]
    if "Exchange.ManageAsApp" not in missing:
        rows.append({"id": "our-exo", "resourceId": "sp-exchange", "resourceDisplayName": "Office 365 Exchange Online",
                     "appRoleId": "r-exo", "principalId": OUR_SP["id"]})
    return rows


OUR_ROLE_ASSIGNMENTS = [{"id": "a-gr", "roleDefinitionId": GLOBAL_READER, "principalId": OUR_SP["id"],
                         "directoryScopeId": "/"}]

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

EXCHANGE = {
    # Graph's rule IDs here aren't real ones, so the rule's name finds its folder.
    "Get-InboxRule": {"u1": [{"Name": ".", "RuleIdentity": 1, "MoveToFolder": "RSS Feeds"}]},
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
# Sign-ins happen in the last few days, whenever the tests run: the activity history keeps the
# sign-in log by date, and the summaries cover the last week of it.
SIGNIN_BASE = (datetime.now(UTC) - timedelta(days=1)).date()


def _on(days_back: int, hhmm: str) -> str:
    """A sign-in time ``days_back`` days before yesterday."""
    return f"{SIGNIN_BASE - timedelta(days=days_back)}T{hhmm}:00Z"


LEGACY_SIGNINS = [
    {"id": "legacy-1", "createdDateTime": _on(2, "10:00"), "userPrincipalName": "info@x.com", "userId": "u7",
     "clientAppUsed": "Authenticated SMTP", "appDisplayName": "Office 365 Exchange Online", "ipAddress": "203.0.113.5",
     "location": {"countryOrRegion": "GB"}, "status": {"errorCode": 0}},
    {"id": "legacy-2", "createdDateTime": _on(3, "03:00"), "userPrincipalName": "ann@x.com", "userId": "u1",
     "clientAppUsed": "IMAP4", "ipAddress": "198.51.100.9", "location": {"countryOrRegion": "RU"},
     "status": {"errorCode": 50126, "failureReason": "Invalid username or password."}},
]
INFO_USER = {"id": "u7", "displayName": "Info", "userPrincipalName": "info@x.com", "userType": "Member",
             "accountEnabled": True, "assignedLicenses": [], "signInActivity": {}}


def _signin(n, when, upn, uid, code=0, *, app="Office 365 Exchange Online", ip="192.0.2.10", country="GB",
            city="London", mfa=True, reason=None, risk="hidden", state="none"):
    """One interactive sign-in from /beta/auditLogs/signIns."""
    return {"id": f"si-{n}", "createdDateTime": when, "userPrincipalName": upn, "userId": uid,
            "userDisplayName": upn.split("@")[0].title(), "appDisplayName": app, "clientAppUsed": "Browser",
            "ipAddress": ip, "isInteractive": True,
            "authenticationRequirement": "multiFactorAuthentication" if mfa else "singleFactorAuthentication",
            "location": {"city": city, "countryOrRegion": country},
            "deviceDetail": {"operatingSystem": "Windows10", "browser": "Edge 128.0.0"},
            "status": {"errorCode": code, "failureReason": reason or ("Other." if code == 0 else f"Error {code}"),
                       "additionalDetails": "MFA requirement satisfied by claim in the token" if mfa else None},
            "riskLevelDuringSignIn": risk, "riskState": state, "riskDetail": "none", "riskEventTypes_v2": []}


# A week of interactive sign-ins: Ann signs in from the office with MFA (once from France, flagged
# medium risk where the tenant has P2), Bob's disabled account is being guessed from one address,
# another address sprays five accounts, and the shared info@ mailbox signs in with only a password.
SIGNINS = (
    [_signin(i, _on(1 - i % 2, f"{8 + i % 9:02d}:15"), "ann@x.com", "u1",
             app="Microsoft Teams" if i % 3 else "Office 365 Exchange Online") for i in range(25)]
    + [_signin(30, _on(1, "07:00"), "ann@x.com", "u1", mfa=False, app="Azure Portal"),
       _signin(31, _on(1, "07:01"), "ann@x.com", "u1", 50074),
       _signin(32, _on(1, "07:02"), "ann@x.com", "u1", 50140),
       _signin(33, _on(2, "21:40"), "ann@x.com", "u1", ip="198.51.100.40", country="FR", city="Paris",
               app="Office 365 SharePoint Online", risk="medium", state="atRisk")]
    + [_signin(40 + i, _on(3, f"02:{10 + i:02d}"), "bob@x.com", "u2", 50126, ip="198.51.100.7",
               country="RU", city="Moscow", reason="Invalid username or password or Invalid on-premise "
               "username or password.") for i in range(6)]
    + [_signin(47, _on(3, "02:20"), "bob@x.com", "u2", 50053, ip="198.51.100.7", country="RU", city="Moscow"),
       _signin(48, _on(2, "09:00"), "bob@x.com", "u2", 50057, reason="User account is disabled.")]
    + [_signin(50 + i, _on(4, f"23:{i:02d}"), upn, uid, code, ip="203.0.113.66", country="BR",
               city="Sao Paulo")
       for i, (upn, uid, code) in enumerate([("ann@x.com", "u1", 50126), ("bob@x.com", "u2", 50126),
                                             ("info@x.com", "u7", 50126), ("g#EXT#@x.com", "u3", 50126),
                                             ("nobody@x.com", None, 50034)])]
    + [_signin(60, _on(0, "11:00"), "info@x.com", "u7", mfa=False),
       _signin(61, _on(0, "12:00"), "info@x.com", "u7", mfa=False),
       _signin(62, _on(1, "16:00"), "g#EXT#@x.com", "u3", 53003, ip="192.0.2.99", country="US",
               city="Seattle", app="Microsoft Teams", reason="Access has been blocked by Conditional Access policies.")]
)

def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.1234567Z")


def _risky_user(uid, upn, level, state, detail="none", days=1, deleted=False):
    """One entry of /v1.0/identityProtection/riskyUsers."""
    return {"id": uid, "isDeleted": deleted, "isProcessing": False, "riskLastUpdatedDateTime": _days_ago(days),
            "riskLevel": level, "riskState": state, "riskDetail": detail, "userDisplayName": upn.split("@")[0].title(),
            "userPrincipalName": upn}


# Entra ID Protection (P2): Ann is at medium risk after the Paris sign-in, Bob's disabled account was
# confirmed compromised, info@ was remediated by a password reset last month, and older or never-risky
# entries (and a deleted account's) are what the collector leaves out or doesn't count.
RISKY_USERS = [
    _risky_user("u1", "ann@x.com", "medium", "atRisk", days=5),
    _risky_user("u2", "bob@x.com", "high", "confirmedCompromised", "adminConfirmedUserCompromised", days=6),
    _risky_user("u7", "info@x.com", "none", "remediated", "userPerformedSecuredPasswordReset", days=30),
    _risky_user("u8", "old@x.com", "none", "dismissed", "adminDismissedAllRiskForUser", days=200),
    _risky_user("u9", "fine@x.com", "none", "none", days=10),
    _risky_user("u10", "gone@x.com", "high", "atRisk", days=20, deleted=True),
]

ORGANIZATION = {"id": "org", "displayName": "Org", "verifiedDomains": [
    {"name": "x.com", "isDefault": True}, {"name": "parked.com"}, {"name": "x.onmicrosoft.com"}]}

DNS = {
    ("x.com", "MX"): ["x-com.mail.protection.outlook.com"],
    ("x.com", "TXT"): ["v=spf1 include:spf.protection.outlook.com -all", "MS=ms123"],
    ("spf.protection.outlook.com", "TXT"): ["v=spf1 ip4:40.92.0.0/15 -all"],
    ("_dmarc.x.com", "TXT"): ["v=DMARC1; p=none; rua=mailto:dmarc@x.com"],
    ("selector1._domainkey.x.com", "CNAME"): ["selector1-x-com._domainkey.x.onmicrosoft.com"],
}


def fake_dns(name, rdtype):
    return list(DNS.get((name, rdtype), []))


def _days_ago(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


# The Entra ID recycle bin (/directory/deletedItems), relative to today since Microsoft keeps items for 30
# days: a licensed leaver with 5 days left to restore, a guest with 4, a recent leaver, a team about to go
# and a group deleted yesterday.
DELETED_USERS = [
    {"id": "du1", "displayName": "Dan Leaver", "userPrincipalName": "du1dan@x.com", "mail": "dan@x.com",
     "userType": "Member", "deletedDateTime": _days_ago(25), "accountEnabled": False, "jobTitle": "Sales",
     "department": "Sales", "assignedLicenses": [{"skuId": "sku-bp"}]},
    {"id": "du2", "displayName": "Old Guest", "userPrincipalName": "du2old_guest#EXT#@x.com", "mail": None,
     "userType": "Guest", "deletedDateTime": _days_ago(26), "accountEnabled": True, "assignedLicenses": []},
    {"id": "du3", "displayName": "Eve Recent", "userPrincipalName": "du3eve@x.com", "mail": "eve@x.com",
     "userType": "Member", "deletedDateTime": _days_ago(3), "accountEnabled": True, "department": "Finance",
     "assignedLicenses": [{"skuId": "sku-bp"}]},
]
DELETED_GROUPS = [
    {"id": "dg1", "displayName": "Project Falcon", "mail": "falcon@x.com", "groupTypes": ["Unified"],
     "deletedDateTime": _days_ago(28), "mailEnabled": True, "securityEnabled": False,
     "resourceProvisioningOptions": ["Team"]},
    {"id": "dg2", "displayName": "Marketing", "mail": "marketing@x.com", "groupTypes": ["Unified"],
     "deletedDateTime": _days_ago(1), "mailEnabled": True, "securityEnabled": False,
     "resourceProvisioningOptions": []},
]
