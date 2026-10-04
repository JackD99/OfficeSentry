"""Subscriptions and licence assignment fixtures, shaped like Graph returns them, with dates relative to now.

The default seed tenant has SKUS from ``test_collectors`` (Business Premium, and an old suspended
product) and SUBSCRIPTIONS for them. Its users get licences directly and from the "All Staff" group
(``groupdata`` s1), one of them both ways. The other lists are for tests that need trials,
grace periods and group licensing errors.
"""

from datetime import UTC, datetime, timedelta


def days_from_now(n: float) -> str:
    return (datetime.now(UTC) + timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


def subscription(sid, sku_id, part, status="Enabled", *, trial=False, total=10, ends_in=200, created="2025-03-01"):
    return {"id": sid, "commerceSubscriptionId": f"c-{sid}", "skuId": sku_id, "skuPartNumber": part,
            "status": status, "isTrial": trial, "totalLicenses": total, "createdDateTime": f"{created}T09:00:00Z",
            "nextLifecycleDateTime": days_from_now(ends_in) if ends_in is not None else None,
            "ownerType": None, "serviceStatus": []}


def state(sku_id, group=None, status="Active", error="None"):
    """One entry of a user's licenseAssignmentStates."""
    return {"skuId": sku_id, "assignedByGroup": group, "state": status, "error": error, "disabledPlans": [],
            "lastUpdatedDateTime": "2026-09-01T10:00:00Z"}


SUBSCRIPTIONS = [
    subscription("sub-bp", "sku-bp", "SPB", total=10, ends_in=150),
    subscription("sub-old", "sku-old", "OLD", "Suspended", trial=True, total=5, ends_in=20),
]

# A tenant with a Business Premium subscription, a Copilot trial ending in 12 days that people use,
# Visio in its grace period and an Exchange Online plan nobody may use any more.
SKUS = [
    {"skuId": "sku-bp", "skuPartNumber": "SPB", "appliesTo": "User", "capabilityStatus": "Enabled",
     "prepaidUnits": {"enabled": 10, "warning": 0, "suspended": 0, "lockedOut": 0}, "consumedUnits": 10,
     "servicePlans": []},
    {"skuId": "sku-copilot", "skuPartNumber": "Microsoft_365_Copilot", "appliesTo": "User",
     "capabilityStatus": "Enabled", "prepaidUnits": {"enabled": 5, "warning": 0, "suspended": 0, "lockedOut": 0},
     "consumedUnits": 2, "servicePlans": []},
    {"skuId": "sku-visio", "skuPartNumber": "VISIOCLIENT", "appliesTo": "User", "capabilityStatus": "Warning",
     "prepaidUnits": {"enabled": 0, "warning": 3, "suspended": 0, "lockedOut": 0}, "consumedUnits": 3,
     "servicePlans": []},
    {"skuId": "sku-exo", "skuPartNumber": "EXCHANGESTANDARD", "appliesTo": "User", "capabilityStatus": "Suspended",
     "prepaidUnits": {"enabled": 0, "warning": 0, "suspended": 2, "lockedOut": 0}, "consumedUnits": 1,
     "servicePlans": []},
    {"skuId": "sku-flow", "skuPartNumber": "FLOW_FREE", "appliesTo": "User", "capabilityStatus": "Enabled",
     "prepaidUnits": {"enabled": 10000, "warning": 0, "suspended": 0, "lockedOut": 0}, "consumedUnits": 4,
     "servicePlans": []},
]
TENANT_SUBSCRIPTIONS = [
    subscription("s-bp", "sku-bp", "SPB", total=10, ends_in=300),
    subscription("s-copilot", "sku-copilot", "Microsoft_365_Copilot", trial=True, total=5, ends_in=12),
    subscription("s-visio", "sku-visio", "VISIOCLIENT", "Warning", total=3, ends_in=25),
    subscription("s-exo", "sku-exo", "EXCHANGESTANDARD", "Suspended", total=2, ends_in=70),
    subscription("s-flow", "sku-flow", "FLOW_FREE", total=10000, ends_in=None),
]


def user(uid, name, states, *, location="GB", enabled=True):
    assigned = [{"skuId": s["skuId"], "disabledPlans": []} for s in states if s["state"] != "Error"]
    return {"id": uid, "displayName": name, "userPrincipalName": f"{name.lower()}@x.com", "userType": "Member",
            "accountEnabled": enabled, "assignedLicenses": list({a["skuId"]: a for a in assigned}.values()),
            "licenseAssignmentStates": states, "usageLocation": location,
            "signInActivity": {"lastSignInDateTime": days_from_now(-2)}}


# Licensing group l1 gives Business Premium; Dan's assignment failed because none are left.
USERS = [
    user("u1", "Ann", [state("sku-bp"), state("sku-bp", "l1"), state("sku-copilot")]),
    user("u5", "Eve", [state("sku-bp", "l1"), state("sku-visio")], location=None),
    user("u8", "Dan", [state("sku-bp", "l1", "Error", "CountViolation"), state("sku-exo")]),
    user("u9", "Fay", [state("sku-copilot", "l2", "Error", "DependencyViolation"), state("sku-visio"),
                       state("sku-flow")]),
]
LICENSING_GROUPS = [
    {"id": "l1", "displayName": "Licence - Business Premium", "groupTypes": [], "mailEnabled": False,
     "securityEnabled": True, "assignedLicenses": [{"skuId": "sku-bp", "disabledPlans": []}]},
    {"id": "l2", "displayName": "Licence - Copilot", "groupTypes": ["DynamicMembership"], "mailEnabled": False,
     "securityEnabled": True, "membershipRule": "(user.department -eq \"Sales\")",
     "assignedLicenses": [{"skuId": "sku-copilot", "disabledPlans": []}]},
]
