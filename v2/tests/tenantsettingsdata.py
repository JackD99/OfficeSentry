"""Tenant settings fixtures: authorization policy and admin consent request policy, as Graph returns them."""

MEMBER_GUESTS = "a0b1b346-4d3e-4e8b-98f8-753987be4970"
LIMITED_GUESTS = "10dae51f-b6af-4016-8d66-8c2a99b929b3"
RESTRICTED_GUESTS = "2af84b1e-32c8-42b7-82bc-daa82404023b"

# Defaults left as Microsoft ships them, apart from guests seeing the directory like members.
OPEN_POLICY = {
    "id": "authorizationPolicy",
    "allowInvitesFrom": "everyone",
    "allowedToSignUpEmailBasedSubscriptions": True,
    "allowedToUseSSPR": True,
    "allowEmailVerifiedUsersToJoinOrganization": True,
    "allowUserConsentForRiskyApps": None,
    "blockMsolPowerShell": False,
    "guestUserRoleId": MEMBER_GUESTS,
    "defaultUserRolePermissions": {
        "allowedToCreateApps": True,
        "allowedToCreateSecurityGroups": True,
        "allowedToCreateTenants": True,
        "allowedToReadBitlockerKeysForOwnedDevice": True,
        "allowedToReadOtherUsers": True,
        "permissionGrantPoliciesAssigned": ["ManagePermissionGrantsForSelf.microsoft-user-default-low"],
    },
}

LOCKED_POLICY = {
    **OPEN_POLICY,
    "allowInvitesFrom": "adminsAndGuestInviters",
    "allowedToSignUpEmailBasedSubscriptions": False,
    "allowEmailVerifiedUsersToJoinOrganization": False,
    "blockMsolPowerShell": True,
    "guestUserRoleId": RESTRICTED_GUESTS,
    "defaultUserRolePermissions": {
        **OPEN_POLICY["defaultUserRolePermissions"],
        "allowedToCreateApps": False,
        "allowedToCreateSecurityGroups": False,
        "allowedToCreateTenants": False,
        "permissionGrantPoliciesAssigned": [],
    },
}

# What a tenant returns before anyone has turned the workflow on (recorded from a test tenant).
ADMIN_CONSENT_POLICY = {
    "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#policies/adminConsentRequestPolicy/$entity",
    "isEnabled": False, "notifyReviewers": False, "remindersEnabled": False, "requestDurationInDays": 0,
    "version": 0, "reviewers": [],
}

ADMIN_CONSENT_ON = {
    **ADMIN_CONSENT_POLICY,
    "isEnabled": True, "notifyReviewers": True, "remindersEnabled": True, "requestDurationInDays": 30, "version": 2,
    "reviewers": [
        {"query": "/users/u1", "queryType": "MicrosoftGraph", "queryRoot": None},
        {"query": "/v1.0/roleManagement/directory/roleAssignments?$filter=roleDefinitionId eq "
                  "'62e90394-69f5-4237-9190-012177145e10'", "queryType": "MicrosoftGraph", "queryRoot": None},
    ],
}
