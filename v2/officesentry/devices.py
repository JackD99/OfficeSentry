"""Device helpers shared by the device checks and the Devices report.

Windows support dates come from Microsoft's lifecycle pages. Graph doesn't
say which edition a device runs, so both dates are kept: Home and Pro (what
most small businesses run) and Enterprise and Education, which get a year more.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

STALE_DAYS = 90  # an Entra device not seen for this long is probably gone
NO_SYNC_DAYS = 30  # an Intune device that hasn't checked in for this long isn't getting policy
BITLOCKER = "BitLockerKey.ReadBasic.All"  # lists which devices have a recovery key saved (added in 2.2)
LAPS = "DeviceLocalCredential.ReadBasic.All"  # lists which devices back up a local admin password (added in 2.4)


@dataclass(frozen=True)
class WindowsRelease:
    name: str
    min_build: int
    pro_end: date  # Home, Pro, Pro for Workstations, Business
    enterprise_end: date  # Enterprise, Education, IoT Enterprise (non-LTSC)


# Newest first; a build belongs to the first release whose min_build it reaches. Builds between the last
# Windows 10 and the first Windows 11 (20348 is Windows Server 2022) are no PC release.
LAST_WINDOWS_10 = 19045
WINDOWS_RELEASES = (
    WindowsRelease("Windows 11 25H2", 26200, date(2027, 10, 12), date(2028, 10, 10)),
    WindowsRelease("Windows 11 24H2", 26100, date(2026, 10, 13), date(2027, 10, 12)),
    WindowsRelease("Windows 11 23H2", 22631, date(2025, 11, 11), date(2026, 11, 10)),
    WindowsRelease("Windows 11 22H2", 22621, date(2024, 10, 8), date(2025, 10, 14)),
    WindowsRelease("Windows 11 21H2", 22000, date(2023, 10, 10), date(2024, 10, 8)),
    WindowsRelease("Windows 10", 10240, date(2025, 10, 14), date(2025, 10, 14)),
)

SOON_DAYS = 90


@dataclass(frozen=True)
class OsSupport:
    release: str
    status: str  # supported | ending | pro_ended | ended | unknown
    end: date | None
    detail: str


def windows_build(version: str | None) -> int | None:
    """Build number from an OS version like ``10.0.22631.4317``."""
    parts = (version or "").split(".")
    if len(parts) < 3 or parts[0] != "10":
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None


def windows_support(version: str | None, today: date) -> OsSupport:
    release = _release(version)
    if release is None:
        return OsSupport("Windows", "unknown", None, f"Unrecognised Windows version {version or '(none)'}")
    if release.enterprise_end < today:
        extra = (
            " Extended Security Updates or LTSC can cover it; otherwise upgrade."
            if release.name == "Windows 10"
            else ""
        )
        return OsSupport(
            release.name,
            "ended",
            release.enterprise_end,
            f"{release.name} stopped getting security updates on {release.enterprise_end:%d %b %Y}." + extra,
        )
    if release.pro_end < today:
        return OsSupport(
            release.name,
            "pro_ended",
            release.pro_end,
            f"{release.name}: updates for Home and Pro ended {release.pro_end:%d %b %Y} "
            f"(Enterprise and Education: {release.enterprise_end:%d %b %Y}).",
        )
    if (release.pro_end - today).days <= SOON_DAYS:
        return OsSupport(
            release.name,
            "ending",
            release.pro_end,
            f"{release.name}: updates for Home and Pro end {release.pro_end:%d %b %Y}.",
        )
    return OsSupport(release.name, "supported", release.pro_end, f"{release.name}, supported")


BOOL_FIELDS = frozenset({"accountEnabled", "isCompliant", "isManaged", "isEncrypted", "available"})


def clean(record: dict) -> dict:
    """A stored device record with wrongly typed values treated as missing.

    Every device field is text apart from the flags in ``BOOL_FIELDS``, so a value of the
    wrong type (a list where Graph should have sent a name, say) becomes ``None`` instead of
    breaking a check, the Devices report or "what changed".
    """
    return {k: v if isinstance(v, bool if k in BOOL_FIELDS else str) else None for k, v in record.items()}


def objects(snap, kind: str) -> list[dict] | None:
    """``snap.objects("devices", kind)``, cleaned."""
    rows = snap.objects("devices", kind)
    return None if rows is None else [clean(r) for r in rows if isinstance(r, dict)]


def is_server(device: dict) -> bool:
    return "server" in (device.get("operatingSystem") or "").lower()


def is_windows(device: dict) -> bool:
    """Windows on a PC. Windows Server shares Windows 10 and 11 build numbers but not their support dates."""
    return (device.get("operatingSystem") or "").lower().startswith("windows") and not is_server(device)


def is_computer(device: dict) -> bool:
    return (device.get("operatingSystem") or "").lower().startswith(("windows", "macos", "mac os"))


def parse_ts(value) -> datetime | None:
    """An ISO timestamp from Graph as an aware datetime; None for missing, "0001-01-01" or unreadable values."""
    if not isinstance(value, str) or not value or value.startswith("0001-"):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_day(value) -> date | None:
    """The date of an ISO date or timestamp; None for missing, "0001-01-01" or unreadable values."""
    if not isinstance(value, str) or value.startswith("0001-"):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def days_since(value: str | None, now: datetime) -> int | None:
    dt = parse_ts(value)
    return None if dt is None else max((now - dt).days, 0)


def os_label(device: dict) -> str:
    os_name = device.get("operatingSystem") or "Unknown"
    version = device.get("osVersion") or device.get("operatingSystemVersion") or ""
    release = _release(version) if is_windows(device) else None
    if release:
        return f"{release.name} ({version})"
    return f"{os_name} {version}".strip()


def bitlocker_state(snap) -> str | None:
    """How the latest Devices run read the BitLocker recovery key list: "read", "waiting" (the client hasn't
    approved BitLockerKey.ReadBasic.All yet) or "failed"; None from a run before Office Sentry read it."""
    rows = snap.objects("devices", "bitlocker_status")
    found = rows[0].get("state") if rows and isinstance(rows[0], dict) else None
    return found if isinstance(found, str) else None


def laps_state(snap) -> str | None:
    """How the latest Devices run read Windows LAPS backups: "read", "waiting" (the client hasn't approved
    DeviceLocalCredential.ReadBasic.All yet) or "failed"; None from a run before Office Sentry read them."""
    rows = snap.objects("devices", "laps_status")
    found = rows[0].get("state") if rows and isinstance(rows[0], dict) else None
    return found if isinstance(found, str) else None


def laps_backups(snap) -> dict[str, dict]:
    """Local admin passwords backed up in Entra ID by Windows LAPS, by lower-cased Entra device ID: when the
    password was last backed up (``lastBackupDateTime``) and when it's next changed (``refreshDateTime``).
    Never the password: the list doesn't carry it."""
    out = {}
    for r in snap.objects("devices", "laps_backup") or []:
        if isinstance(r, dict) and isinstance(r.get("_id"), str):
            out[r["_id"].lower()] = r
    return out


def laps_backup(device: dict, backups: dict[str, dict]) -> dict | None:
    """An Entra device's Windows LAPS backup, by its Entra device ID, else by name (Microsoft lists both)."""
    found = backups.get((device.get("deviceId") or "").lower())
    if found is None and device.get("displayName"):
        name = device["displayName"].lower()
        found = next((b for b in backups.values() if (b.get("deviceName") or "").lower() == name), None)
    return found


def recovery_keys(snap) -> dict[str, dict]:
    """Saved BitLocker recovery keys by lower-cased Entra device ID (``deviceId``): how many (``keys``),
    whether one is for the system drive (``osVolume``) and when the newest was saved (``latest``)."""
    out = {}
    for r in snap.objects("devices", "recovery_key") or []:
        if isinstance(r, dict) and isinstance(r.get("_id"), str):
            out[r["_id"].lower()] = r
    return out


def has_recovery_key(device_id, keys: dict[str, dict]) -> bool:
    """Whether a device (by Entra ``deviceId``, or a managed device's ``azureADDeviceId``) has a system drive
    recovery key saved."""
    found = keys.get(device_id.lower()) if isinstance(device_id, str) else None
    return bool(found and found.get("osVolume"))


def escrows_keys(device: dict, now: datetime) -> bool:
    """A Windows computer joined to Entra ID (or hybrid joined), enabled and seen recently: the devices that
    save their BitLocker recovery key in Entra ID. Registered personal devices don't."""
    return (
        (device.get("operatingSystem") or "").lower().startswith("windows")
        and device.get("trustType") in ("AzureAd", "ServerAd")
        and bool(device.get("accountEnabled"))
        and (days_since(device.get("approximateLastSignInDateTime"), now) or 0) <= STALE_DAYS
    )


def _release(version: str | None) -> WindowsRelease | None:
    build = windows_build(version)
    if build is None or LAST_WINDOWS_10 < build < WINDOWS_RELEASES[-2].min_build:
        return None
    return next((r for r in WINDOWS_RELEASES if build >= r.min_build), None)
