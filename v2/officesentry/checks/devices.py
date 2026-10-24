"""Checks on devices: Intune compliance and its policies, encryption and BitLocker recovery keys, Windows support
and stale records."""

from __future__ import annotations

from datetime import datetime

from .. import devices, intune
from ..snapshot import TenantSnapshot
from ..text import every, plural, verb
from .base import Finding, _all_are, _unavailable, rule

# Intune compliance states that aren't a verdict either way: Intune couldn't evaluate the device.
UNCONFIRMED_STATES = ("error", "conflict", "unknown")


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


@rule(
    "device_compliance",
    "Managed devices meet compliance policies",
    "Devices",
    "Open each non-compliant device in Intune (Devices > Monitor > Noncompliant devices) to see which "
    "setting fails, fix it or retire the device, and block non-compliant devices with a Conditional "
    "Access policy that requires a compliant device.",
    needs=("intune",),
)
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
        elif d.get("complianceState") in UNCONFIRMED_STATES:
            # Conditional Access only accepts "compliant", so these devices are blocked like non-compliant ones.
            findings.append(Finding("low", _device_label(d), f"Compliance state: {d['complianceState']}", d["_id"]))
    if findings:
        return ("fail", f"{len(findings)} of {plural(len(managed), 'managed device')} not compliant", findings)
    compliant = sum(d.get("complianceState") == "compliant" for d in managed)
    if compliant == len(managed):
        return ("pass", _all_are(len(managed), "managed device") + " compliant", [])
    return (
        "pass",
        f"{compliant} of {plural(len(managed), 'managed device')} compliant; none reported as not compliant",
        [],
    )


@rule(
    "device_encryption",
    "Company computers are encrypted",
    "Devices",
    "Turn on BitLocker (Windows) and FileVault (macOS) with an Intune disk encryption policy, with recovery "
    "keys escrowed to Entra ID, so a lost or stolen laptop doesn't expose the organisation's data.",
    needs=("intune",),
)
def device_encryption(snap: TenantSnapshot, now: datetime):
    managed, reason = _managed_devices(snap)
    if managed is None:
        return _unavailable(reason)
    computers = [d for d in managed if devices.is_computer(d)]
    if not computers:
        return _unavailable("No Windows or Mac computers are enrolled in Intune")
    findings = [
        Finding("high", _device_label(d), f"{d.get('operatingSystem')} disk isn't encrypted", d["_id"])
        for d in computers
        if d.get("isEncrypted") is False
    ]
    return (
        ("fail", f"{len(findings)} of {plural(len(computers), 'computer')} not encrypted", findings)
        if findings
        else ("pass", _all_are(len(computers), "managed computer") + " encrypted", [])
    )


@rule(
    "supported_windows",
    "Windows computers still get security updates",
    "Devices",
    "Upgrade computers on Windows 10 or an old Windows 11 release to the current Windows 11 release "
    "(Intune: Windows feature updates policy). Replace hardware that can't run Windows 11, or buy "
    "Extended Security Updates as a stopgap.",
    needs=("intune",),
)
def supported_windows(snap: TenantSnapshot, now: datetime):
    managed, _ = _managed_devices(snap)
    if managed is not None:
        windows, source = [d for d in managed if devices.is_windows(d)], "osVersion"
    else:
        entra = devices.objects(snap, "entra_device")
        if entra is None:
            return _unavailable("Device data hasn't been collected yet")
        # Without Intune, use Entra's record of devices that signed in recently.
        windows = [
            d
            for d in entra
            if devices.is_windows(d)
            and d.get("accountEnabled")
            and (devices.days_since(d.get("approximateLastSignInDateTime"), now) or 0) <= devices.STALE_DAYS
        ]
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
    soon = len(findings) - unsupported
    if not findings:
        return ("pass", _all_are(len(windows), "Windows computer") + " on a supported release", [])
    if not unsupported:
        summary = (
            f"{plural(soon, 'Windows computer')} {verb(soon, 'reaches', 'reach')} end of "
            f"support within {devices.SOON_DAYS} days"
        )
        return ("fail", summary, findings)
    # Computers reaching it soon are findings too: name them, so the summary agrees with the count shown beside it.
    summary = f"{unsupported} of {plural(len(windows), 'Windows computer')} past end of support"
    if soon:
        summary += f", {soon} more reaching it within {devices.SOON_DAYS} days"
    return ("fail", summary, findings)


@rule(
    "intune_enrolled",
    "Joined computers are managed by Intune",
    "Devices",
    "Enrol Entra-joined and hybrid-joined computers in Intune (automatic enrolment under Devices > "
    "Enrollment > Windows) so compliance, encryption and updates can be enforced on them.",
    needs=("intune",),
)
def intune_enrolled(snap: TenantSnapshot, now: datetime):
    status = devices.objects(snap, "intune_status")
    entra = devices.objects(snap, "entra_device")
    if status is None or entra is None:
        return _unavailable("Device data hasn't been collected yet")
    if not status or not status[0].get("available"):
        return _unavailable("The tenant has no Intune licence, or Intune isn't readable")
    joined = [
        d
        for d in entra
        if d.get("trustType") in ("AzureAd", "ServerAd")
        and devices.is_computer(d)
        and d.get("accountEnabled")
        and (devices.days_since(d.get("approximateLastSignInDateTime"), now) or 0) <= devices.STALE_DAYS
    ]
    if not joined:
        return _unavailable("No active Entra-joined or hybrid-joined computers")
    findings = [
        Finding(
            "low",
            d.get("displayName") or d["_id"],
            ("Hybrid joined" if d.get("trustType") == "ServerAd" else "Entra joined") + ", not managed by Intune",
            d["_id"],
        )
        for d in joined
        if not d.get("isManaged")
    ]
    return (
        ("fail", f"{len(findings)} of {plural(len(joined), 'joined computer')} not in Intune", findings)
        if findings
        else ("pass", _all_are(len(joined), "joined computer") + " managed", [])
    )


@rule(
    "device_checkin",
    "Managed devices check in with Intune",
    "Devices",
    "Find out whether each device is lost, replaced or broken. Retire devices that are gone; for "
    "devices still in use, restart the Intune Management Extension or re-enrol.",
    needs=("intune",),
)
def device_checkin(snap: TenantSnapshot, now: datetime):
    managed, reason = _managed_devices(snap)
    if managed is None:
        return _unavailable(reason)
    findings = []
    for d in managed:
        days = devices.days_since(d.get("lastSyncDateTime"), now)
        if days is not None and days > devices.NO_SYNC_DAYS:
            findings.append(Finding("low", _device_label(d), f"Last check-in {days} days ago", d["_id"]))
    return (
        (
            "fail",
            f"{plural(len(findings), 'device')} " + verb(len(findings), "hasn't", "haven't") + " checked in "
            f"for {devices.NO_SYNC_DAYS}+ days",
            findings,
        )
        if findings
        else ("pass", f"{every(len(managed), 'managed device', start=True)} checked in recently", [])
    )


@rule(
    "stale_devices",
    "No old devices left in Entra ID",
    "Devices",
    "Disable, then after 30 days delete, device records that haven't signed in for 90 days (Entra admin "
    "center > Devices > All devices, filter by activity). Old records can still hold BitLocker keys and "
    "count toward device limits.",
    needs=("intune",),
)
def stale_devices(snap: TenantSnapshot, now: datetime):
    entra = devices.objects(snap, "entra_device")
    if entra is None:
        return _unavailable("Device data hasn't been collected yet")
    findings = []
    for d in entra:
        days = devices.days_since(d.get("approximateLastSignInDateTime"), now)
        if d.get("accountEnabled") and days is not None and days > devices.STALE_DAYS:
            findings.append(
                Finding(
                    "low",
                    d.get("displayName") or d["_id"],
                    f"{d.get('operatingSystem') or 'Device'}, last seen {days} days ago",
                    d["_id"],
                )
            )
    return (
        (
            "fail",
            f"{len(findings)} of {plural(len(entra), 'device record')} unused for {devices.STALE_DAYS}+ days",
            findings,
        )
        if findings
        else ("pass", f"No stale devices among {plural(len(entra), 'record')}", [])
    )


HYBRID_NO_KEY = "Encrypted, no recovery key in Entra ID: check it's saved in on-premises Active Directory"


@rule(
    "bitlocker_keys",
    "Windows computers have their BitLocker recovery key saved",
    "Devices",
    "Make Intune's disk encryption policy (Endpoint security > Disk encryption) require the recovery key to be "
    "saved in Entra ID before BitLocker turns on, and on computers already encrypted run "
    "BackupToAAD-BitLockerKeyProtector (or re-run the policy). Without the key, a computer that asks for it after a "
    "firmware or hardware change can't be unlocked, and its data is lost.",
    needs=("intune",),
)
def bitlocker_keys(snap: TenantSnapshot, now: datetime):
    entra = devices.objects(snap, "entra_device")
    if entra is None:
        return _unavailable("Device data hasn't been collected yet")
    state = devices.bitlocker_state(snap)
    if state == "waiting":
        return _unavailable(f"Waiting for {devices.BITLOCKER} to be approved")
    if state != "read":
        return _unavailable(
            "BitLocker recovery keys couldn't be read" if state else "BitLocker recovery keys haven't been read yet"
        )
    computers = [d for d in entra if devices.escrows_keys(d, now)]
    if not computers:
        return _unavailable("No Windows computers are joined to Entra ID")
    keys = devices.recovery_keys(snap)
    status = devices.objects(snap, "intune_status") or []
    managed = (devices.objects(snap, "managed_device") or []) if status and status[0].get("available") else []
    encrypted = {(d.get("azureADDeviceId") or "").lower(): d.get("isEncrypted") for d in managed}
    findings, unencrypted, hybrid = [], 0, 0
    for d in computers:
        device_id = (d.get("deviceId") or "").lower()
        if devices.has_recovery_key(device_id, keys):
            continue
        is_encrypted = encrypted.get(device_id)
        if is_encrypted is False:
            unencrypted += 1  # not encrypted at all: device_encryption says so
            continue
        name = d.get("displayName") or d["_id"]
        if d.get("trustType") == devices.HYBRID:
            # Usually saved in on-premises Active Directory, which Office Sentry can't see: worth a look only
            # when Intune says the disk is encrypted.
            if is_encrypted:
                findings.append(Finding("low", name, HYBRID_NO_KEY, d["_id"]))
            else:
                hybrid += 1
        elif is_encrypted:
            findings.append(Finding("medium", name, "Encrypted, but no recovery key saved in Entra ID", d["_id"]))
        else:
            findings.append(Finding("low", name, "No recovery key saved in Entra ID (encryption not known)", d["_id"]))
    if findings:
        return (
            "fail",
            f"{len(findings)} of {plural(len(computers), 'joined Windows computer')} without a saved recovery key",
            findings,
        )
    if not unencrypted and not hybrid:
        return ("pass", _all_are(len(computers), "joined Windows computer") + " covered by a saved recovery key", [])
    saved = len(computers) - unencrypted - hybrid
    others = [f"{unencrypted} not encrypted"] if unencrypted else []
    if hybrid:
        others.append(f"{hybrid} hybrid joined, whose {verb(hybrid, 'key', 'keys')} may be in on-premises AD")
    return (
        "pass",
        f"{saved} of {plural(len(computers), 'joined Windows computer')} {verb(saved, 'has', 'have')} a saved "
        f"recovery key ({', '.join(others)})",
        [],
    )


@rule(
    "laps_backups",
    "Windows computers back up their local admin password with LAPS",
    "Devices",
    "In Entra ID (Devices > Device settings), turn on Microsoft Entra Local Administrator Password Solution (LAPS), "
    "then in Intune (Endpoint security > Account protection) create a Local admin password solution policy that "
    "backs the password up to Entra ID and assign it to every Windows computer. Each computer then gets its own "
    "local admin password, changed regularly, so one leaked password doesn't open every computer.",
    needs=("intune",),
)
def laps_backups(snap: TenantSnapshot, now: datetime):
    entra = devices.objects(snap, "entra_device")
    if entra is None:
        return _unavailable("Device data hasn't been collected yet")
    state = devices.laps_state(snap)
    if state == "waiting":
        return _unavailable(f"Waiting for the client to approve {devices.LAPS}")
    if state != "read":
        return _unavailable(
            "Windows LAPS backups couldn't be read" if state else "Windows LAPS backups haven't been read yet"
        )
    # Hybrid joined computers may back theirs up to on-premises Active Directory, which Office Sentry can't see.
    computers = [d for d in entra if devices.escrows_keys(d, now) and d.get("trustType") == "AzureAd"]
    if not computers:
        return _unavailable("No Windows computers are joined to Entra ID")
    backups = devices.laps_backups(snap)
    findings = [
        Finding("low", d.get("displayName") or d["_id"], "No local admin password backed up in Entra ID", d["_id"])
        for d in computers
        if not devices.laps_backup(d, backups)
    ]
    if findings:
        return (
            "fail",
            f"{len(findings)} of {plural(len(computers), 'Entra joined Windows computer')} without a LAPS backup",
            findings,
        )
    return ("pass", _all_are(len(computers), "Entra joined Windows computer") + " backed up by LAPS", [])


@rule(
    "intune_compliance_coverage",
    "Every platform in use has a compliance policy",
    "Devices",
    "In Intune (Devices > Compliance), create and assign a compliance policy for each platform with enrolled "
    "devices, requiring at least encryption, a screen lock and a minimum OS version, and set devices with no "
    "policy to show as not compliant (Compliance settings). Until then those devices count as compliant, so "
    "Conditional Access that requires a compliant device lets them through.",
    needs=("intune",),
)
def intune_compliance_coverage(snap: TenantSnapshot, now: datetime):
    status = intune.status(snap)
    if status is None:
        return _unavailable("Intune policies haven't been collected yet")
    if status.get("available") is False:
        return _unavailable("The tenant has no Intune licence")
    part = intune.parts(snap).get("compliance")
    if part == "waiting":
        return _unavailable("Waiting for DeviceManagementConfiguration.Read.All to be approved")
    if part != "read":
        return _unavailable("Intune compliance policies couldn't be read")
    managed, reason = _managed_devices(snap)
    if managed is None:
        return _unavailable(reason)
    enrolled: dict[str, int] = {}
    for d in managed:
        platform = intune.device_platform(d)
        if platform:
            enrolled[platform] = enrolled.get(platform, 0) + 1
    if not enrolled:
        return _unavailable("No enrolled devices run Windows, macOS, iOS, Android or Linux")
    covered = intune.compliance_platforms(snap)
    findings = [
        Finding(
            "medium",
            platform,
            f"{plural(n, 'enrolled device')}, no compliance policy assigned",
            f"platform:{platform}",
        )
        for platform, n in sorted(enrolled.items())
        if not covered.get(platform)
    ]
    if findings:
        names = ", ".join(f.subject for f in findings)
        return ("fail", f"No compliance policy for {names}", findings)
    return (
        "pass",
        f"{every(len(enrolled), 'platform', start=True)} in use {verb(len(enrolled), 'has', 'have')} a "
        "compliance policy",
        [],
    )
