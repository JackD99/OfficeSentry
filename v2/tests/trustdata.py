"""Cross-tenant access settings, Conditional Access named locations and the apps Intune found on devices, in
Microsoft Graph's shapes."""

from __future__ import annotations


def _b2b(users: str, apps: str, *, user_targets=("AllUsers",), app_targets=("AllApplications",)) -> dict:
    return {
        "usersAndGroups": {
            "accessType": users,
            "targets": [{"target": t, "targetType": "user" if t == "AllUsers" else "group"} for t in user_targets],
        },
        "applications": {
            "accessType": apps,
            "targets": [{"target": t, "targetType": "application"} for t in app_targets],
        },
    }


# Microsoft's defaults, except that MFA from every other organisation is trusted.
CROSS_TENANT_DEFAULT = {
    "isServiceDefault": False,
    "inboundTrust": {
        "isMfaAccepted": True,
        "isCompliantDeviceAccepted": False,
        "isHybridAzureADJoinedDeviceAccepted": False,
    },
    "automaticUserConsentSettings": {"inboundAllowed": False, "outboundAllowed": False},
    "b2bCollaborationInbound": _b2b("allowed", "allowed"),
    "b2bCollaborationOutbound": _b2b("allowed", "allowed"),
    "b2bDirectConnectInbound": _b2b("blocked", "blocked"),
    "b2bDirectConnectOutbound": _b2b("blocked", "blocked"),
    "tenantRestrictions": _b2b("blocked", "blocked"),
}

PARTNERS = [
    # The MSP: its settings are its own, and it's the service provider.
    {
        "tenantId": "11111111-1111-1111-1111-111111111111",
        "isServiceProvider": True,
        "isInMultiTenantOrganization": False,
        "inboundTrust": {
            "isMfaAccepted": True,
            "isCompliantDeviceAccepted": True,
            "isHybridAzureADJoinedDeviceAccepted": False,
        },
        "automaticUserConsentSettings": {"inboundAllowed": True, "outboundAllowed": None},
        "b2bCollaborationInbound": None,
        "b2bCollaborationOutbound": None,
        "b2bDirectConnectInbound": None,
        "b2bDirectConnectOutbound": None,
    },
    # A partner whose people can join two groups' Teams shared channels, in Office 365 only.
    {
        "tenantId": "22222222-2222-2222-2222-222222222222",
        "isServiceProvider": False,
        "isInMultiTenantOrganization": False,
        "inboundTrust": None,
        "automaticUserConsentSettings": {"inboundAllowed": None, "outboundAllowed": None},
        "b2bCollaborationInbound": None,
        "b2bCollaborationOutbound": _b2b("blocked", "blocked"),
        "b2bDirectConnectInbound": _b2b("allowed", "allowed", user_targets=("g-a", "g-b"), app_targets=("Office365",)),
        "b2bDirectConnectOutbound": None,
    },
]

NAMED_LOCATIONS = [
    {
        "@odata.type": "#microsoft.graph.ipNamedLocation",
        "id": "loc-office",
        "displayName": "Head office",
        "createdDateTime": "2025-03-01T09:00:00Z",
        "modifiedDateTime": "2026-02-10T09:00:00Z",
        "isTrusted": True,
        "ipRanges": [{"@odata.type": "#microsoft.graph.iPv4CidrRange", "cidrAddress": "203.0.113.0/24"}],
    },
    # Trusted, and far too wide: a /8 of public addresses.
    {
        "@odata.type": "#microsoft.graph.ipNamedLocation",
        "id": "loc-wide",
        "displayName": "Old VPN",
        "createdDateTime": "2021-06-01T09:00:00Z",
        "modifiedDateTime": "2021-06-01T09:00:00Z",
        "isTrusted": True,
        "ipRanges": [
            {"@odata.type": "#microsoft.graph.iPv4CidrRange", "cidrAddress": "51.0.0.0/8"},
            {"@odata.type": "#microsoft.graph.iPv6CidrRange", "cidrAddress": "2001:db8::/32"},
        ],
    },
    {
        "@odata.type": "#microsoft.graph.countryNamedLocation",
        "id": "loc-uk",
        "displayName": "United Kingdom",
        "createdDateTime": "2025-03-01T09:00:00Z",
        "modifiedDateTime": "2025-03-01T09:00:00Z",
        "countriesAndRegions": ["GB"],
        "includeUnknownCountriesAndRegions": False,
        "countryLookupMethod": "clientIpAddress",
    },
    # Untrusted and used by nothing.
    {
        "@odata.type": "#microsoft.graph.ipNamedLocation",
        "id": "loc-lab",
        "displayName": "Test lab",
        "createdDateTime": "2024-01-01T09:00:00Z",
        "modifiedDateTime": "2024-01-01T09:00:00Z",
        "isTrusted": False,
        "ipRanges": [{"@odata.type": "#microsoft.graph.iPv4CidrRange", "cidrAddress": "10.0.0.0/8"}],
    },
]

# Policies that use the locations: MFA everywhere but trusted locations, and sign-ins blocked outside the UK.
CA_MFA_NOT_TRUSTED = {
    "id": "ca-loc1",
    "displayName": "MFA outside the office",
    "state": "enabled",
    "conditions": {
        "users": {"includeUsers": ["All"]},
        "applications": {"includeApplications": ["All"]},
        "locations": {"includeLocations": ["All"], "excludeLocations": ["AllTrusted"]},
        "clientAppTypes": ["all"],
    },
    "grantControls": {"operator": "OR", "builtInControls": ["mfa"]},
}
CA_BLOCK_ABROAD = {
    "id": "ca-loc2",
    "displayName": "Block outside the UK",
    "state": "enabledForReportingButNotEnforced",
    "conditions": {
        "users": {"includeUsers": ["All"]},
        "applications": {"includeApplications": ["All"]},
        "locations": {"includeLocations": ["All"], "excludeLocations": ["loc-uk"]},
        "clientAppTypes": ["all"],
    },
    "grantControls": {"operator": "OR", "builtInControls": ["block"]},
}


def _app(id_, name, version, devices, publisher="", platform="windows") -> dict:
    return {
        "@odata.type": "#microsoft.graph.detectedApp",
        "id": id_,
        "displayName": name,
        "version": version,
        "sizeInByte": 1_000_000,
        "deviceCount": devices,
        "publisher": publisher,
        "platform": platform,
    }


DETECTED_APPS = [
    _app("app1", "Google Chrome", "141.0.7390.66", 9, "Google LLC"),
    _app("app2", "Google Chrome", "140.0.7339.208", 2, "Google LLC"),
    _app("app3", "Google Chrome", "9.0.597.98", 1, "Google LLC"),  # sorts below the newer ones
    _app("app4", "Microsoft 365 Apps for business - en-gb", "16.0.19127.20264", 12, "Microsoft Corporation"),
    _app("app5", "AnyDesk", "9.0.5", 1, "AnyDesk Software GmbH"),
    _app("app6", "ScreenConnect Client (a1b2c3d4e5f6)", "24.3.7.9067", 12, "ConnectWise, LLC."),
    _app("app7", "Zoom Workplace", "6.6.0", 3, "Zoom Communications, Inc.", "macOS"),
    _app("app8", "Microsoft Authenticator", "6.2510.6500", 4, "Microsoft Corporation", "ios"),
    _app("app9", "  ", "1.0", 1),  # no name: not kept
]
