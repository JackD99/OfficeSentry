"""The tenant's SharePoint storage: how much it has and how much is used.

Microsoft doesn't publish a tenant's SharePoint quota through Graph, so it's
worked out the way Microsoft sizes it: 1 TB, plus 10 GB for each licence that
includes SharePoint, plus any Office 365 Extra File Storage (1 GB per unit).
OneDrives have their own quotas and don't count towards it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .licences import FREE_UNIT_THRESHOLD, working_units
from .snapshot import TenantSnapshot
from .text import nouns

GB = 1024**3
BASE = 1024 * GB
PER_LICENCE = 10 * GB
EXTRA_STORAGE_SKUS = {"SHAREPOINTSTORAGE", "SHAREPOINTSTORAGE_GOV"}
SHAREPOINT_PLANS = ("SHAREPOINTSTANDARD", "SHAREPOINTENTERPRISE")


@dataclass
class TenantQuota:
    total: int
    licences: int
    extra_gb: int

    @property
    def explained(self) -> str:
        extra = f" + {self.extra_gb:,} GB of extra storage" if self.extra_gb else ""
        return f"1 TB + 10 GB × {self.licences:,} {nouns(self.licences, 'licence')}{extra}"


def tenant_quota(skus: list[dict] | None) -> TenantQuota | None:
    """Estimated SharePoint storage from the subscriptions, or None without SharePoint licences."""
    licences = extra = 0
    for s in skus or []:
        units = working_units(s)
        if units <= 0 or units >= FREE_UNIT_THRESHOLD or s.get("capabilityStatus") not in (None, "Enabled", "Warning"):
            continue
        if s.get("skuPartNumber") in EXTRA_STORAGE_SKUS:
            extra += units
        elif s.get("appliesTo") == "User" and any(
            (p.get("servicePlanName") or "").startswith(SHAREPOINT_PLANS) for p in s.get("servicePlans") or []
        ):
            licences += units
    if not licences:
        return None
    return TenantQuota(BASE + licences * PER_LICENCE + extra * GB, licences, extra)


def tenant_used(snap: TenantSnapshot) -> int | None:
    """Bytes used across SharePoint sites (not OneDrive), from the storage scan, else the usage report."""
    sites = snap.objects("sharepoint_storage", "site_storage")
    if sites:
        return sum(s.get("used") or 0 for s in sites)
    value = snap.metric("sharepoint.storage_bytes")
    return None if value is None else int(value)


def tenant_storage(snap: TenantSnapshot) -> tuple[int | None, TenantQuota | None]:
    return tenant_used(snap), tenant_quota(snap.objects("tenant_profile", "subscribed_sku"))
