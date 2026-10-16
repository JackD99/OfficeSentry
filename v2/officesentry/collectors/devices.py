"""Devices: Entra ID device records for every tenant, Intune managed devices where licensed, which devices
have a BitLocker recovery key saved in Entra ID (never the key itself), and which back up a local admin password
with Windows LAPS (never the password).

The recovery key list needs BitLockerKey.ReadBasic.All, added in Office Sentry 2.2, and the LAPS list
DeviceLocalCredential.ReadBasic.All, added in 2.4 (``permission_batch``): until the client approves one, its
part is skipped and stored as waiting, so the run doesn't fail."""

from __future__ import annotations

from datetime import UTC, datetime

from ..devices import BITLOCKER, LAPS, NO_SYNC_DAYS, STALE_DAYS, days_since, is_computer, windows_support
from ..graph import GraphError, LicenseRequired, PermissionMissing
from .base import Collector, RunContext, register
from .connection import awaiting

ENTRA_FIELDS = (
    "id,deviceId,displayName,operatingSystem,operatingSystemVersion,trustType,isCompliant,isManaged,"
    "accountEnabled,approximateLastSignInDateTime,registrationDateTime,profileType"
)
MANAGED_FIELDS = (
    "id,deviceName,operatingSystem,osVersion,complianceState,lastSyncDateTime,enrolledDateTime,isEncrypted,"
    "userPrincipalName,userDisplayName,managedDeviceOwnerType,model,manufacturer,jailBroken,azureADDeviceId,"
    "managementAgent"
)
# The list never carries the key itself: Microsoft returns a key only from one key's own GET with
# ``$select=key``, which Office Sentry never makes.
RECOVERY_KEYS = "/informationProtection/bitlocker/recoveryKeys"
# Windows LAPS backups. Only these fields: the password itself comes back only with $select=credentials, which
# needs DeviceLocalCredential.Read.All, a permission Office Sentry never asks for.
LAPS_BACKUPS = "/directory/deviceLocalCredentials"
LAPS_FIELDS = "id,deviceName,lastBackupDateTime,refreshDateTime"
# volumeType: Microsoft's documentation shows the number ("1"), the enum name is operatingSystemVolume.
OS_VOLUME = ("1", "operatingSystemVolume")


def recovery_keys(keys: list[dict]) -> dict[str, dict]:
    """Saved recovery keys by Entra device ID (``deviceId``, not the object ID): how many, whether one is for
    the system drive, and when the newest was saved."""
    out: dict[str, dict] = {}
    for k in keys:
        device = (k.get("deviceId") or "").lower()
        if not device:
            continue
        entry = out.setdefault(device, {"keys": 0, "osVolume": False, "latest": None})
        entry["keys"] += 1
        entry["osVolume"] |= str(k.get("volumeType")) in OS_VOLUME
        created = k.get("createdDateTime")
        if isinstance(created, str) and created > (entry["latest"] or ""):
            entry["latest"] = created
    return out


@register
class Devices(Collector):
    key = "devices"
    title = "Devices"
    description = (
        "Devices registered or joined to Entra ID, which have a BitLocker recovery key saved and which back up "
        "a local admin password with Windows LAPS, and Intune compliance, encryption and check-in for managed "
        "devices (needs Intune)."
    )
    permissions = ("Directory.Read.All", "DeviceManagementManagedDevices.Read.All", BITLOCKER, LAPS)

    def collect(self, ctx: RunContext) -> None:
        now = datetime.now(UTC)
        entra = ctx.paged(ctx.graph.get_all("/devices", params={"$select": ENTRA_FIELDS, "$top": "999"}))
        ctx.objects("entra_device", [(d["id"], {k: d.get(k) for k in ENTRA_FIELDS.split(",")}) for d in entra])
        self._recovery_keys(ctx)
        self._laps(ctx)
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
    def _recovery_keys(ctx: RunContext) -> None:
        """Which devices have a BitLocker recovery key saved, stored with the state of the read ("read",
        "waiting" for the client to approve the permission, or "failed")."""
        state, keys = "read", []
        if awaiting(ctx, BITLOCKER):
            state = "waiting"
            ctx.log(f"Waiting for the client to approve {BITLOCKER}; BitLocker recovery keys are skipped")
        else:
            try:
                keys = ctx.paged(ctx.graph.get_all(RECOVERY_KEYS))
            except PermissionMissing as exc:  # a new permission (BATCH_OF), so a refusal means not approved yet
                state = "waiting"
                ctx.log(f"BitLocker recovery keys skipped: {BITLOCKER} isn't approved yet ({exc.message})")
            except GraphError as exc:
                state = "failed"
                ctx.warn(f"BitLocker recovery keys weren't readable: {exc.message}")
        ctx.objects("bitlocker_status", [("status", {"state": state})])
        if state == "read":
            saved = recovery_keys(keys)
            ctx.objects("recovery_key", list(saved.items()))
            ctx.metric("devices.recovery_keys_saved", len(saved))

    @staticmethod
    def _laps(ctx: RunContext) -> None:
        """Which devices back up a local admin password with Windows LAPS, stored with the state of the read
        ("read", "waiting" for the client to approve the permission, or "failed"), as for recovery keys."""
        state, backups = "read", []
        if awaiting(ctx, LAPS):
            state = "waiting"
            ctx.log(f"Waiting for the client to approve {LAPS}; Windows LAPS backups are skipped")
        else:
            try:
                backups = ctx.paged(ctx.graph.get_all(LAPS_BACKUPS, params={"$select": LAPS_FIELDS}))
            except PermissionMissing as exc:  # a new permission (BATCH_OF), so a refusal means not approved yet
                state = "waiting"
                ctx.log(f"Windows LAPS backups skipped: {LAPS} isn't approved yet ({exc.message})")
            except GraphError as exc:
                state = "failed"
                ctx.warn(f"Windows LAPS backups weren't readable: {exc.message}")
        ctx.objects("laps_status", [("status", {"state": state})])
        if state == "read":
            # The id is the device's Entra device ID (deviceId), not its object ID.
            kept = {
                b["id"].lower(): {k: b.get(k) for k in ("deviceName", "lastBackupDateTime", "refreshDateTime")}
                for b in backups
                if isinstance(b.get("id"), str)
            }
            ctx.objects("laps_backup", list(kept.items()))
            ctx.metric("devices.laps_backed_up", len(kept))

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
