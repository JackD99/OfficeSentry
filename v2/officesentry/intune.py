"""Intune policies as stored by ``collectors/intune_policies.py``, for the Intune policies report and the device
checks: which platform each policy is for, what it requires in plain words, and who it's assigned to.

Reads stored snapshots only. Platforms are named the same way for policies and for enrolled devices
(``PLATFORMS``), so a report can say which platforms have devices but no compliance policy.
"""

from __future__ import annotations

from .collectors.intune_policies import PARTS, UPDATE_RING, is_assigned
from .snapshot import TenantSnapshot

COLLECTOR = "intune_policies"
WINDOWS, MACOS, IOS, ANDROID, LINUX = "Windows", "macOS", "iOS/iPadOS", "Android", "Linux"
PLATFORMS = (WINDOWS, MACOS, IOS, ANDROID, LINUX)
MOBILE = (IOS, ANDROID)

# Settings catalog ``platforms`` values (a comma-separated flags enum).
_CATALOG_PLATFORMS = {
    "windows10": WINDOWS,
    "windows10x": WINDOWS,
    "macos": MACOS,
    "ios": IOS,
    "android": ANDROID,
    "androidenterprise": ANDROID,
    "aosp": ANDROID,
    "linux": LINUX,
}
SEVERITY_LEVELS = {"secured": "Secured", "low": "Low", "medium": "Medium", "high": "High"}
GATEKEEPER = {
    "macAppStore": "App Store apps only",
    "macAppStoreAndIdentifiedDevelopers": "App Store and identified developers",
    "anywhere": "Apps from anywhere",
}


def type_platform(odata_type: str | None) -> str:
    """The platform of a compliance policy or configuration profile, from its Intune type
    (``windows10CompliancePolicy`` -> Windows)."""
    t = (odata_type or "").lower()
    if t.startswith(("windows", "edition", "sharedpc")):
        return WINDOWS
    if t.startswith("macos"):
        return MACOS
    if t.startswith("ios"):
        return IOS
    if t.startswith(("android", "aosp")):
        return ANDROID
    return "Other"


def catalog_platforms(value) -> list[str]:
    """The platforms of a settings catalog or endpoint security policy."""
    names = [_CATALOG_PLATFORMS.get(p.strip().lower()) for p in str(value or "").split(",")]
    return list(dict.fromkeys(n for n in names if n)) or ["Other"]


def device_platform(device: dict) -> str | None:
    """The platform of an Intune managed device, named like a policy's."""
    name = (device.get("operatingSystem") or "").lower()
    if name.startswith("windows"):
        return WINDOWS
    if name.startswith(("macos", "mac os")):
        return MACOS
    if name.startswith(("ios", "ipados")):
        return IOS
    if name.startswith("android"):
        return ANDROID
    if name.startswith("linux"):
        return LINUX
    return None


def status(snap: TenantSnapshot) -> dict | None:
    """The collection's status (parts read, waiting or unlicensed), or None when it hasn't run."""
    rows = snap.objects(COLLECTOR, "intune_policy_status")
    if rows is None:
        return None
    return rows[0] if rows and isinstance(rows[0], dict) else {}


def parts(snap: TenantSnapshot) -> dict[str, str]:
    found = (status(snap) or {}).get("parts")
    return {p: found.get(p) for p in PARTS} if isinstance(found, dict) else {}


def policies(snap: TenantSnapshot, kind: str) -> list[dict]:
    return [p for p in snap.objects(COLLECTOR, kind) or [] if isinstance(p, dict)]


def settings(policy: dict) -> dict:
    found = policy.get("settings")
    return found if isinstance(found, dict) else {}


def assigned(policy: dict) -> bool:
    return is_assigned(policy)


def is_update_ring(profile: dict) -> bool:
    return profile.get("type") == UPDATE_RING


def _number(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def compliance_requires(policy: dict) -> list[str]:
    """What a compliance policy requires, in plain words, most important first."""
    s = settings(policy)
    out = []
    if s.get("passwordRequired") or s.get("passcodeRequired"):
        length = _number(s.get("passwordMinimumLength")) or _number(s.get("passcodeMinimumLength"))
        out.append(f"Password of {length}+ characters" if length else "Password")
        lock = _number(s.get("passwordMinutesOfInactivityBeforeLock")) or _number(
            s.get("passcodeMinutesOfInactivityBeforeLock")
        )
        if lock:
            out.append(f"Locks after {lock} min")
    if s.get("bitLockerEnabled"):
        out.append("BitLocker")
    if s.get("storageRequireEncryption"):
        out.append("Encrypted storage")
    if s.get("secureBootEnabled"):
        out.append("Secure Boot")
    if s.get("codeIntegrityEnabled"):
        out.append("Code integrity")
    if s.get("tpmRequired"):
        out.append("TPM chip")
    if s.get("activeFirewallRequired") or s.get("firewallEnabled"):
        out.append("Firewall on")
    if s.get("defenderEnabled"):
        out.append("Microsoft Defender on")
    if s.get("antivirusRequired"):
        out.append("Antivirus")
    if s.get("antiSpywareRequired"):
        out.append("Antispyware")
    if s.get("rtpEnabled"):
        out.append("Real-time protection")
    if s.get("signatureOutOfDate"):
        out.append("Up-to-date antivirus signatures")
    if s.get("deviceThreatProtectionEnabled"):
        level = SEVERITY_LEVELS.get(str(s.get("deviceThreatProtectionRequiredSecurityLevel")))
        out.append(f"Threat level {level} or lower" if level else "Threat protection")
    if s.get("systemIntegrityProtectionEnabled"):
        out.append("System Integrity Protection")
    if s.get("gatekeeperAllowedAppSource") in GATEKEEPER and s["gatekeeperAllowedAppSource"] != "anywhere":
        out.append(f"Gatekeeper: {GATEKEEPER[s['gatekeeperAllowedAppSource']]}")
    if s.get("securityBlockJailbrokenDevices"):
        out.append("Not jailbroken or rooted")
    if s.get("securityRequireSafetyNetAttestationBasicIntegrity"):
        out.append("Play Integrity check")
    if s.get("securityPreventInstallAppsFromUnknownSources"):
        out.append("No apps from unknown sources")
    if isinstance(s.get("osMinimumVersion"), str) and s["osMinimumVersion"]:
        out.append(f"Version {s['osMinimumVersion']} or later")
    if isinstance(s.get("osMaximumVersion"), str) and s["osMaximumVersion"]:
        out.append(f"Version {s['osMaximumVersion']} or earlier")
    if isinstance(s.get("minAndroidSecurityPatchLevel"), str) and s["minAndroidSecurityPatchLevel"]:
        out.append(f"Security patch {s['minAndroidSecurityPatchLevel']} or later")
    if s.get("managedEmailProfileRequired"):
        out.append("Managed email profile")
    if s.get("configurationManagerComplianceRequired"):
        out.append("Configuration Manager compliance")
    return out


def compliance_platforms(snap: TenantSnapshot) -> dict[str, int]:
    """How many assigned compliance policies each platform has."""
    out: dict[str, int] = {}
    for p in policies(snap, "compliance_policy"):
        if assigned(p):
            platform = type_platform(p.get("type"))
            out[platform] = out.get(platform, 0) + 1
    return out
