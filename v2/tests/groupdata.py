"""Groups and Teams fixtures, shaped like the real Graph responses."""

UNIFIED_TEMPLATE = "62375ab9-6b52-47ed-826b-58e47e0e304b"


def _group(
    gid,
    name,
    *,
    unified=True,
    team=False,
    visibility="Private",
    mail=True,
    security=False,
    created="2025-01-10T09:00:00Z",
    **extra,
):
    return {
        "id": gid,
        "displayName": name,
        "description": None,
        "groupTypes": ["Unified"] if unified else [],
        "mailEnabled": mail,
        "securityEnabled": security,
        "visibility": visibility if unified else None,
        "resourceProvisioningOptions": ["Team"] if team else [],
        "createdDateTime": created,
        "mail": f"{gid}@x.com" if mail else None,
        "membershipRule": None,
        "onPremisesSyncEnabled": None,
        "expirationDateTime": None,
        **extra,
    }


def _user(uid, name, upn, enabled=True, user_type="Member"):
    return {
        "@odata.type": "#microsoft.graph.user",
        "id": uid,
        "displayName": name,
        "userPrincipalName": upn,
        "accountEnabled": enabled,
        "userType": user_type,
    }


def _member(uid):
    return {
        "@odata.type": "#microsoft.graph.user",
        "id": uid,
        "userType": "Member",
        "accountEnabled": True,
        "userPrincipalName": f"{uid}@x.com",
        "mail": f"{uid}@x.com",
    }


def _guest(uid, address, mail=True):
    return {
        "@odata.type": "#microsoft.graph.user",
        "id": uid,
        "userType": "Guest",
        "accountEnabled": True,
        "userPrincipalName": address.replace("@", "_") + "#EXT#@x.onmicrosoft.com",
        "mail": address if mail else None,
    }


GROUPS = [
    _group("g1", "Sales", team=True),
    _group("g2", "Project Phoenix", team=True, visibility="Public"),
    _group("g3", "Marketing", visibility="Public", created="2026-09-01T09:00:00Z"),
    _group("g4", "Board", team=True),
    _group("g5", "Provisioned", visibility="Private"),
    _group(
        "s1",
        "All Staff",
        unified=False,
        mail=False,
        security=True,
        groupTypes=["DynamicMembership"],
        membershipRule='(user.userType -eq "Member")',
        membershipRuleProcessingState="On",
        assignedLicenses=[{"skuId": "sku-bp", "disabledPlans": []}],
    ),
    _group("s2", "VPN Users", unified=False, mail=False, security=True, onPremisesSyncEnabled=True),
    _group("s3", "Group creators", unified=False, mail=False, security=True),
    _group("m1", "Finance alerts", unified=False, security=True),
    _group("d1", "Info", unified=False),
]

ANN = _user("u1", "Ann", "ann@x.com")
OWNERS = {
    "g1": [ANN, _user("u5", "Eve", "eve@x.com")],
    "g2": [_user("u2", "Bob", "bob@x.com", enabled=False)],  # a leaver: nobody looks after it
    "g3": [],
    "g4": [ANN],
    "g5": [
        {
            "@odata.type": "#microsoft.graph.servicePrincipal",
            "id": "sp9",
            "displayName": "Provisioning app",
            "accountEnabled": True,
        }
    ],
}
MEMBERS = {
    "g1": [_member("u1"), _member("u5"), _guest("gx1", "pat@supplier.com")],
    "g2": [_member("u2"), _guest("gx1", "pat@supplier.com"), _guest("gx2", "lee@partner.org", mail=False)],
    "g3": [_member("u1")],
    "g4": [_member("u1"), _member("u5")],
    "g5": [_member("u1")],
    # Security groups and distribution lists (read by Group nesting): VPN Users holds Group creators, which is
    # empty; Info holds Finance alerts, which holds VPN Users: three levels.
    "s1": [_member("u1"), _member("u5")],
    "s2": [_member("u1"), {"@odata.type": "#microsoft.graph.group", "id": "s3"}],
    "m1": [_member("u5"), {"@odata.type": "#microsoft.graph.group", "id": "s2"}],
    "d1": [{"@odata.type": "#microsoft.graph.group", "id": "m1"}, _member("u1")],
}

GROUP_SETTINGS = [
    {
        "id": "set1",
        "displayName": "Group.Unified",
        "templateId": UNIFIED_TEMPLATE,
        "values": [
            {"name": "EnableGroupCreation", "value": "false"},
            {"name": "GroupCreationAllowedGroupId", "value": "s3"},
            {"name": "AllowGuestsToAccessGroups", "value": "true"},
            {"name": "AllowToAddGuests", "value": "False"},
            {"name": "UsageGuidelinesUrl", "value": "https://intranet.x.com/teams"},
            {"name": "ClassificationList", "value": ""},
        ],
    },
]

LIFECYCLE_POLICIES = [
    {"id": "lp1", "groupLifetimeInDays": 365, "managedGroupTypes": "All", "alternateNotificationEmails": "it@x.com"},
]
