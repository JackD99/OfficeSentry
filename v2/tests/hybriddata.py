"""Hybrid identity fixtures: a tenant synced from on-premises AD, with times relative to now.

The default seed tenant (graphdata.ORGANIZATION) is cloud-only and gets DOMAINS;
tests swap in ORGANIZATION, HYBRID_DOMAINS and USERS for a synced tenant.
"""

from datetime import UTC, datetime, timedelta

from tests import graphdata as gd


def hours_ago(n: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


# Entra Connect with password hash sync, last run 20 minutes ago.
ORGANIZATION = {
    **gd.ORGANIZATION,
    "onPremisesSyncEnabled": True,
    "onPremisesLastSyncDateTime": hours_ago(0.3),
    "onPremisesLastPasswordSyncDateTime": hours_ago(0.3),
}


def _domain(name, auth="Managed", default=False, services=("Email",), expiry=2147483647):
    return {
        "id": name,
        "authenticationType": auth,
        "isVerified": True,
        "isDefault": default,
        "supportedServices": list(services),
        "passwordValidityPeriodInDays": expiry,
        "passwordNotificationWindowInDays": 14,
    }


DOMAINS = [
    _domain("x.com", default=True, services=("Email", "OfficeCommunicationsOnline", "Intune")),
    _domain("parked.com", services=(), expiry=90),
    _domain("x.onmicrosoft.com", services=("Email", "OfficeCommunicationsOnline")),
]

HYBRID_DOMAINS = [
    _domain("x.com", default=True, services=("Email", "OfficeCommunicationsOnline", "Intune")),
    _domain("x.co.uk", auth="Federated"),
    _domain("x.onmicrosoft.com", services=("Email", "OfficeCommunicationsOnline")),
]


def _user(uid, name, upn, synced, user_type="Member", enabled=True):
    return {
        "id": uid,
        "displayName": name,
        "userPrincipalName": upn,
        "userType": user_type,
        "accountEnabled": enabled,
        "assignedLicenses": [{"skuId": "sku-bp"}] if user_type == "Member" else [],
        "onPremisesSyncEnabled": True if synced else None,
        "signInActivity": {"lastSignInDateTime": hours_ago(24)},
    }


# Ann (Global Administrator) and Eve (eligible Global Administrator) come from AD;
# the break-glass account is cloud-only, as it should be.
USERS = [
    _user("u1", "Ann", "ann@x.com", synced=True),
    _user("u2", "Bob", "bob@x.com", synced=True, enabled=False),
    _user("u3", "Guest", "g#EXT#@x.com", synced=False, user_type="Guest"),
    _user("u4", "Breakglass", "bg@x.com", synced=False),
    _user("u5", "Eve", "eve@x.com", synced=True),
    _user("u6", "Carl", "carl@x.com", synced=True),
]
