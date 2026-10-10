"""Phones and tablets connected to mailboxes, through Exchange ActiveSync or the Outlook app.

Get-MobileDevice lists every device partnership in the tenant in one call. When each device last
synced is only in Get-MobileDeviceStatistics, one call per mailbox that has a device, so those reads
use the same rotating window as other per-mailbox reads (``mailbox_window``): on very big tenants a
mailbox not read this run keeps its devices' last sync from the run that read it.

A partnership can outlive its mailbox (a mailbox deleted or recreated under a new name keeps its old
devices listed). Exchange answers 404 for that mailbox's statistics, so those devices are marked
``mailboxGone`` instead of counting as a failed read.

Device phone numbers, IMEIs and wipe recovery passwords are never stored.
"""

from __future__ import annotations

from .. import store
from ..exchange import ExchangeAccessDenied, ExchangeError
from ..text import plural
from . import mailbox_window
from .base import Collector, RunContext, register

DEVICE_FIELDS = (
    "Guid",
    "DeviceId",
    "DeviceType",
    "DeviceModel",
    "DeviceOS",
    "DeviceUserAgent",
    "FriendlyName",
    "ClientType",
    "ClientVersion",
    "DeviceAccessState",
    "DeviceAccessStateReason",
    "IsManaged",
    "IsCompliant",
    "IsDisabled",
    "FirstSyncTime",
    "UserDisplayName",
)
SYNC_FIELDS = (
    "LastSuccessSync",
    "LastSyncAttemptTime",
    "Status",
    "LastPolicyUpdateTime",
    "DeviceWipeRequestTime",
    "DeviceWipeSentTime",
    "DeviceWipeAckTime",
)
FOLDER = "\\ExchangeActiveSyncDevices\\"


def owner_name(device: dict) -> str:
    """The name of the mailbox a device belongs to, from its identity ("<mailbox>\\ExchangeActiveSyncDevices\\
    <device>") or, failing that, its distinguished name ("CN=<device>,CN=ExchangeActiveSyncDevices,CN=<mailbox>,...").
    """
    identity = device.get("Identity")
    if isinstance(identity, str) and FOLDER in identity:
        return identity.split(FOLDER, 1)[0]
    dn = device.get("DistinguishedName")
    if isinstance(dn, str) and ",CN=ExchangeActiveSyncDevices,CN=" in dn:
        return dn.split(",CN=ExchangeActiveSyncDevices,CN=", 1)[1].split(",OU=", 1)[0].replace("\\,", ",")
    return ""


def mailbox_index(boxes: list[dict]) -> dict[str, dict]:
    """Mailboxes by every name a device's identity might use for them, lower-cased."""
    out: dict[str, dict] = {}
    for m in boxes:
        for key in (
            "Name",
            "Alias",
            "ExternalDirectoryObjectId",
            "Guid",
            "PrimarySmtpAddress",
            "UserPrincipalName",
            "DisplayName",
        ):
            if isinstance(m.get(key), str) and m[key]:
                out.setdefault(m[key].lower(), m)
    return out


def _device_id(d: dict) -> str:
    return str(d.get("Guid") or d.get("Identity") or d.get("DeviceId") or "?")


@register
class MobileDevices(Collector):
    key = "mobile_devices"
    title = "Mobile devices"
    description = "Phones and tablets that sync mail, and when each last synced (needs Global Reader)."
    permissions = ("Exchange.ManageAsApp",)

    def collect(self, ctx: RunContext) -> None:
        if ctx.capabilities.get("exchange") is False:
            ctx.log("Tenant has no Exchange Online licence; nothing to collect")
            ctx.objects("mobile_device", [])
            return
        devices = ctx.exchange.invoke("Get-MobileDevice", {"ResultSize": "Unlimited"})
        run = store.latest_run(ctx.conn, ctx.tenant["id"], "mailboxes")
        index = mailbox_index(store.run_objects(ctx.conn, run["id"], "mailbox") if run else [])

        owners: dict[str, dict] = {}  # mailbox id -> what the statistics call needs
        rows = []
        for d in devices:
            name = owner_name(d)
            box = index.get(name.lower()) if name else None
            mailbox_id = (box or {}).get("_id") or name or "?"
            row = {f: d.get(f) for f in DEVICE_FIELDS}
            row.update(
                {
                    "mailboxId": mailbox_id,
                    "ownerName": name,
                    "mailbox": (box or {}).get("PrimarySmtpAddress"),
                    "mailboxType": (box or {}).get("RecipientTypeDetails"),
                    "identity": d.get("Identity"),
                }
            )
            rows.append((_device_id(d), row))
            if name:
                owners.setdefault(mailbox_id, {"_id": mailbox_id, "ident": (box or {}).get("Guid") or name})

        synced, gone = self._statistics(ctx, list(owners.values()))
        for device_id, row in rows:
            row.update(synced.get(device_id) or synced.get(str(row.get("identity"))) or {})
            if row["mailboxId"] in gone:
                row["mailboxGone"] = True
        if gone:
            left = sum(row["mailboxId"] in gone for _, row in rows)
            ctx.log(
                f"{plural(left, 'device')} {'belongs' if left == 1 else 'belong'} to a mailbox that no longer "
                "exists, so Exchange has no sync times for them"
            )
        ctx.objects("mobile_device", rows)

        data = [r for _, r in rows]
        ctx.metric("mobile.devices", len(data))
        ctx.metric("mobile.blocked", sum(r.get("DeviceAccessState") in ("Blocked", "Quarantined") for r in data))

    @staticmethod
    def _statistics(ctx: RunContext, owners: list[dict]) -> tuple[dict[str, dict], set[str]]:
        """Each device's last sync, by device ID and identity: read this run, or kept from an earlier run. And
        the mailboxes Exchange no longer finds (404), whose devices are left over."""
        window = mailbox_window.plan(
            ctx, MobileDevices.key, "mobile_device_sync", owners, lambda o: o["_id"], "mobile device sync times"
        )
        found, failed, gone = [], 0, set()
        for owner in window.mailboxes:
            try:
                stats = ctx.exchange.invoke("Get-MobileDeviceStatistics", {"Mailbox": owner["ident"]})
            except ExchangeAccessDenied:
                raise
            except ExchangeError as exc:
                if exc.status == 404:
                    gone.add(owner["_id"])
                else:
                    failed += 1
                continue
            for s in stats:
                row = {f: s.get(f) for f in SYNC_FIELDS}
                row.update({"mailboxId": owner["_id"], "identity": s.get("Identity")})
                found.append((_device_id(s), row))
        if failed:
            ctx.warn(f"Could not read when devices last synced for {plural(failed, 'mailbox')}")
        found += window.carried
        ctx.objects("mobile_device_sync", found)
        window.save(ctx)
        out: dict[str, dict] = {}
        for device_id, row in found:
            sync = {f: row.get(f) for f in SYNC_FIELDS}
            out[device_id] = sync
            if row.get("identity"):
                out.setdefault(str(row["identity"]), sync)
        return out, gone
