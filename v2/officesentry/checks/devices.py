"""Checks on devices: Intune compliance, encryption, Windows support and stale records."""

from __future__ import annotations

from datetime import datetime

from .. import devices
from ..snapshot import TenantSnapshot
from ..text import every, plural, verb
from .base import Finding, _all_are, _unavailable, rule


def _managed_devices(snap: TenantSnapshot):
    """Intune devices, or the reason they can't be checked."""
    status = devices.objects(snap, "intune_status")
    if status is None:
        return None, "Device data hasn't been collected yet"
    if not status or not status[0].get("available"):
        return None, "The tenant has no Intune licence, or Intune isn't readable"
    managed = devices.objects(snap, "managed_device") or []
    if not managed:
        return None, "No devices are enrolled in Intune"
    return managed, ""


def _device_label(d: dict) -> str:
    name = d.get("deviceName") or d.get("displayName") or d.get("_id", "?")
    user = d.get("userPrincipalName")
    return f"{name} ({user})" if user else name


@rule("device_compliance", "Managed devices meet compliance policies", "Devices",
      "Open each non-compliant device in Intune (Devices > Monitor > Noncompliant devices) to see which "
      "setting fails, fix it or retire the device, and block non-compliant devices with a Conditional "
      "Access policy that requires a compliant device.")
def device_compliance(snap: TenantSnapshot, now: datetime):
    managed, reason = _managed_devices(snap)
    if managed is None:
        return _unavailable(reason)
    findings = []
    for d in managed:
        if str(d.get("jailBroken")).lower() == "true":
            findings.append(Finding("high", _device_label(d), "Jailbroken or rooted", d["_id"]))
        elif d.get("complianceState") == "noncompliant":
            findings.append(Finding("medium", _device_label(d), "Not compliant", d["_id"]))
        elif d.get("complianceState") == "inGracePeriod":
            findings.append(Finding("low", _device_label(d), "Not compliant, in grace period", d["_id"]))
    return ("fail", f"{len(findings)} of {plural(len(managed), 'managed device')} not compliant", findings) \
        if findings \
        else ("pass", _all_are(len(managed), "managed device") + " compliant", [])


@rule("device_encryption", "Company computers are encrypted", "Devices",
      "Turn on BitLocker (Windows) and FileVault (macOS) with an Intune disk encryption policy, with recovery "
      "keys escrowed to Entra ID, so a lost or stolen laptop doesn't expose the client's data.")
def device_encryption(snap: TenantSnapshot, now: datetime):
    managed, reason = _managed_devices(snap)
    if managed is None:
        return _unavailable(reason)
    computers = [d for d in managed if devices.is_computer(d)]
    if not computers:
        return _unavailable("No Windows or Mac computers are enrolled in Intune")
    findings = [Finding("high", _device_label(d), f"{d.get('operatingSystem')} disk isn't encrypted", d["_id"])
                for d in computers if d.get("isEncrypted") is False]
    return ("fail", f"{len(findings)} of {plural(len(computers), 'computer')} not encrypted", findings) if findings \
        else ("pass", _all_are(len(computers), "managed computer") + " encrypted", [])


@rule("supported_windows", "Windows computers still get security updates", "Devices",
      "Upgrade computers on Windows 10 or an old Windows 11 release to the current Windows 11 release "
      "(Intune: Windows feature updates policy). Replace hardware that can't run Windows 11, or buy "
      "Extended Security Updates as a stopgap.")
def supported_windows(snap: TenantSnapshot, now: datetime):
    managed, _ = _managed_devices(snap)
    if managed is not None:
        windows, source = [d for d in managed if devices.is_windows(d)], "osVersion"
    else:
        entra = devices.objects(snap, "entra_device")
        if entra is None:
            return _unavailable("Device data hasn't been collected yet")
        # Without Intune, use Entra's record of devices that signed in recently.
        windows = [d for d in entra if devices.is_windows(d) and d.get("accountEnabled")
                   and (devices.days_since(d.get("approximateLastSignInDateTime"), now) or 0) <= devices.STALE_DAYS]
        source = "operatingSystemVersion"
    if not windows:
        return _unavailable("No active Windows computers found")
    severity = {"ended": "high", "pro_ended": "medium", "ending": "low"}
    findings = []
    for d in windows:
        support = devices.windows_support(d.get(source), now.date())
        if support.status in severity:
            findings.append(Finding(severity[support.status], _device_label(d), support.detail, d["_id"]))
    unsupported = sum(f.severity != "low" for f in findings)
    if not findings:
        return ("pass", _all_are(len(windows), "Windows computer") + " on a supported release", [])
    summary = f"{unsupported} of {plural(len(windows), 'Windows computer')} past end of support" if unsupported \
        else (f"{plural(len(findings), 'Windows computer')} {verb(len(findings), 'reaches', 'reach')} end of "
              f"support within {devices.SOON_DAYS} days")
    return ("fail", summary, findings)


@rule("intune_enrolled", "Joined computers are managed by Intune", "Devices",
      "Enrol Entra-joined and hybrid-joined computers in Intune (automatic enrolment under Devices > "
      "Enrollment > Windows) so compliance, encryption and updates can be enforced on them.")
def intune_enrolled(snap: TenantSnapshot, now: datetime):
    status = devices.objects(snap, "intune_status")
    entra = devices.objects(snap, "entra_device")
    if status is None or entra is None:
        return _unavailable("Device data hasn't been collected yet")
    if not status or not status[0].get("available"):
        return _unavailable("The tenant has no Intune licence, or Intune isn't readable")
    joined = [d for d in entra if d.get("trustType") in ("AzureAd", "ServerAd") and devices.is_computer(d)
              and d.get("accountEnabled")
              and (devices.days_since(d.get("approximateLastSignInDateTime"), now) or 0) <= devices.STALE_DAYS]
    if not joined:
        return _unavailable("No active Entra-joined or hybrid-joined computers")
    findings = [Finding("low", d.get("displayName") or d["_id"],
                        ("Hybrid joined" if d.get("trustType") == "ServerAd" else "Entra joined")
                        + ", not managed by Intune", d["_id"])
                for d in joined if not d.get("isManaged")]
    return ("fail", f"{len(findings)} of {plural(len(joined), 'joined computer')} not in Intune", findings) \
        if findings \
        else ("pass", _all_are(len(joined), "joined computer") + " managed", [])


@rule("device_checkin", "Managed devices check in with Intune", "Devices",
      "Find out whether each device is lost, replaced or broken. Retire devices that are gone; for "
      "devices still in use, restart the Intune Management Extension or re-enrol.")
def device_checkin(snap: TenantSnapshot, now: datetime):
    managed, reason = _managed_devices(snap)
    if managed is None:
        return _unavailable(reason)
    findings = []
    for d in managed:
        days = devices.days_since(d.get("lastSyncDateTime"), now)
        if days is not None and days > devices.NO_SYNC_DAYS:
            findings.append(Finding("low", _device_label(d), f"Last check-in {days} days ago", d["_id"]))
    return ("fail", f"{plural(len(findings), 'device')} " + verb(len(findings), "hasn't", "haven't") + " checked in "
                    f"for {devices.NO_SYNC_DAYS}+ days", findings) \
        if findings else ("pass", f"{every(len(managed), 'managed device', start=True)} checked in recently", [])


@rule("stale_devices", "No old devices left in Entra ID", "Devices",
      "Disable, then after 30 days delete, device records that haven't signed in for 90 days (Entra admin "
      "center > Devices > All devices, filter by activity). Old records can still hold BitLocker keys and "
      "count toward device limits.")
def stale_devices(snap: TenantSnapshot, now: datetime):
    entra = devices.objects(snap, "entra_device")
    if entra is None:
        return _unavailable("Device data hasn't been collected yet")
    findings = []
    for d in entra:
        days = devices.days_since(d.get("approximateLastSignInDateTime"), now)
        if d.get("accountEnabled") and days is not None and days > devices.STALE_DAYS:
            findings.append(Finding("low", d.get("displayName") or d["_id"],
                                    f"{d.get('operatingSystem') or 'Device'}, last seen {days} days ago", d["_id"]))
    return ("fail", f"{len(findings)} of {plural(len(entra), 'device record')} unused for {devices.STALE_DAYS}+ days",
            findings) if findings else ("pass", f"No stale devices among {plural(len(entra), 'record')}", [])
