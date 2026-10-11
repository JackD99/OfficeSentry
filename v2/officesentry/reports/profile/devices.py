"""Devices."""

from __future__ import annotations

from ... import devices
from ...text import plural as _plural
from ..data_reports import COMPLIANCE, Section, _device_issues
from .base import Part, Person, part


@part(60)
def devices_part(p: Person) -> Part:
    result = Part("devices", "Devices")
    entra = devices.objects(p.snap, "entra_device")
    if entra is None:
        result.unavailable = "Devices haven't been collected yet. Refresh Devices on the client page."
        return result
    status = devices.objects(p.snap, "intune_status") or []
    if not (status and status[0].get("available")):
        result.unavailable = (
            "This tenant has no Intune, and Entra ID doesn't record who uses each device, "
            "so devices can't be listed per person. The Devices report lists them all."
        )
        return result
    mine = [d for d in devices.objects(p.snap, "managed_device") or [] if p.named(d.get("userPrincipalName"))]
    if not mine:
        result.summary, result.empty = "None", "No devices enrolled in Intune for this person."
        return result
    table = Section("Devices", ["Device", "Operating system", "Compliance", "Encrypted", "Last check-in", "Problems"])
    problems = 0
    for d in sorted(mine, key=lambda d: (d.get("deviceName") or "").lower()):
        issues, flag = _device_issues(d, p.now)
        days = devices.days_since(d.get("lastSyncDateTime"), p.now)
        table.add(
            [
                d.get("deviceName"),
                devices.os_label(d),
                COMPLIANCE.get(d.get("complianceState"), d.get("complianceState")),
                d.get("isEncrypted") if devices.is_computer(d) else "",
                "Never"
                if days is None
                else ("Today" if days == 0 else ("Yesterday" if days == 1 else f"{days} days ago")),
                "; ".join(issues),
            ],
            flag,
        )
        if issues:
            problems += 1
            if flag in ("high", "medium"):
                p.flag(flag, f"{d.get('deviceName')}: {'; '.join(issues)}.", "devices")
    result.tables = [table]
    result.summary = _plural(len(mine), "device") + (f", {problems} with problems" if problems else "")
    result.tone = "warn" if problems else "ok"
    return result
