"""Devices: Entra ID device records for every tenant, and Intune managed devices where licensed."""

from __future__ import annotations

from datetime import UTC, datetime

from ..devices import NO_SYNC_DAYS, STALE_DAYS, days_since, is_computer, windows_support
from ..graph import LicenseRequired, PermissionMissing
from .base import Collector, RunContext, register

ENTRA_FIELDS = (
    "id,deviceId,displayName,operatingSystem,operatingSystemVersion,trustType,isCompliant,isManaged,"
    "accountEnabled,approximateLastSignInDateTime,registrationDateTime,profileType"
)
MANAGED_FIELDS = (
    "id,deviceName,operatingSystem,osVersion,complianceState,lastSyncDateTime,enrolledDateTime,isEncrypted,"
    "userPrincipalName,userDisplayName,managedDeviceOwnerType,model,manufacturer,jailBroken,azureADDeviceId,"
    "managementAgent"
)


@register
class Devices(Collector):
    key = "devices"
    title = "Devices"
    description = (
        "Devices registered or joined to Entra ID, and Intune compliance, encryption and "
        "check-in for managed devices (needs Intune)."
    )
    permissions = ("Directory.Read.All", "DeviceManagementManagedDevices.Read.All")

    def collect(self, ctx: RunContext) -> None:
        now = datetime.now(UTC)
        entra = ctx.paged(ctx.graph.get_all("/devices", params={"$select": ENTRA_FIELDS, "$top": "999"}))
        ctx.objects("entra_device", [(d["id"], {k: d.get(k) for k in ENTRA_FIELDS.split(",")}) for d in entra])
        ctx.metric("devices.entra_total", len(entra))
        ctx.metric(
            "devices.entra_stale",
            sum(
                1
                for d in entra
                if d.get("accountEnabled")
                and (days_since(d.get("approximateLastSignInDateTime"), now) or 0) > STALE_DAYS
            ),
        )

        managed = self._managed(ctx)
        if managed is None:
            return
        ctx.objects("managed_device", [(d["id"], {k: d.get(k) for k in MANAGED_FIELDS.split(",")}) for d in managed])
        computers = [d for d in managed if is_computer(d)]
        ctx.metric("devices.managed", len(managed))
        ctx.metric("devices.compliant", sum(d.get("complianceState") == "compliant" for d in managed))
        ctx.metric("devices.noncompliant", sum(d.get("complianceState") == "noncompliant" for d in managed))
        ctx.metric("devices.unencrypted_computers", sum(d.get("isEncrypted") is False for d in computers))
        ctx.metric(
            "devices.not_synced", sum((days_since(d.get("lastSyncDateTime"), now) or 0) > NO_SYNC_DAYS for d in managed)
        )
        today = now.date()
        ctx.metric(
            "devices.unsupported_windows",
            sum(
                windows_support(d.get("osVersion"), today).status in ("ended", "pro_ended")
                for d in managed
                if (d.get("operatingSystem") or "").lower() == "windows"
            ),
        )

    @staticmethod
    def _managed(ctx: RunContext) -> list[dict] | None:
        if ctx.capabilities.get("intune") is False:
            ctx.log("Tenant has no Intune licence; Entra devices only")
            ctx.objects("intune_status", [("intune", {"available": False})])
            return None
        try:
            items = ctx.paged(ctx.graph.get_all("/deviceManagement/managedDevices", params={"$select": MANAGED_FIELDS}))
        except LicenseRequired:
            ctx.warn("Intune managed devices need an Intune licence")
            ctx.objects("intune_status", [("intune", {"available": False})])
            return None
        except PermissionMissing as exc:
            ctx.warn(
                "Intune managed devices weren't readable (DeviceManagementManagedDevices.Read.All "
                f"consent missing, or Intune not set up): {exc.message}"
            )
            ctx.objects("intune_status", [("intune", {"available": False})])
            return None
        ctx.objects("intune_status", [("intune", {"available": True})])
        return items
