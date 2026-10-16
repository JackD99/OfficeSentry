"""Intune policies: which platforms have a compliance policy, what each compliance policy requires, the
configuration profiles and Windows update rings, settings catalog and endpoint security policies, and app
protection for phones, each with whom it's assigned to.

Reads ``collectors/intune_policies.py`` (through ``intune.py``), the enrolled devices from ``devices`` and group
names from ``groups`` and ``group_nesting``. The policies need permissions added in 2.2; while the client hasn't
approved them the report says so (``permission_batch.waiting_text``) and shows what it has.
"""

from __future__ import annotations

from datetime import datetime

from .. import devices, intune
from ..permission_batch import waiting_text
from ..snapshot import TenantSnapshot
from ..text import plural
from .data_reports import DataReport, Rows, Section, Tile
from .registry import report

NOT_COLLECTED = "Intune policies haven't been collected yet. Refresh Intune policies on the client page."
NO_INTUNE = "This client has no Intune licence, so there are no Intune policies to report."

COVERAGE = "Coverage by platform"
ATTENTION = "Needs attention"
COMPLIANCE = "Compliance policies"
PROFILES = "Configuration profiles"
CATALOG = "Endpoint security and settings catalog"
RINGS = "Windows update rings"
APP_PROTECTION = "App protection for phones"

UNASSIGNED = "Assigned to nobody, so it does nothing"
NO_COMPLIANCE = "Devices are enrolled but no compliance policy is assigned"
NOBODY = "Nobody"

TYPE_LABELS = {
    "windowsUpdateForBusinessConfiguration": "Update ring",
    "windows10GeneralConfiguration": "Device restrictions",
    "windows10EndpointProtectionConfiguration": "Endpoint protection",
    "windows10CustomConfiguration": "Custom (OMA-URI)",
    "windowsHealthMonitoringConfiguration": "Windows health monitoring",
    "windowsIdentityProtectionConfiguration": "Identity protection",
    "windowsDefenderAdvancedThreatProtectionConfiguration": "Microsoft Defender for Endpoint",
    "windowsDeliveryOptimizationConfiguration": "Delivery Optimization",
    "windowsKioskConfiguration": "Kiosk",
    "windowsDomainJoinConfiguration": "Domain join",
    "windows10VpnConfiguration": "VPN",
    "windowsWifiConfiguration": "Wi-Fi",
    "windowsWifiEnterpriseEAPConfiguration": "Wi-Fi",
    "editionUpgradeConfiguration": "Edition upgrade",
    "sharedPCConfiguration": "Shared PC",
    "windows10TeamGeneralConfiguration": "Surface Hub",
    "iosGeneralDeviceConfiguration": "Device restrictions",
    "iosDeviceFeaturesConfiguration": "Device features",
    "iosUpdateConfiguration": "Update policy",
    "iosCustomConfiguration": "Custom",
    "iosEasEmailProfileConfiguration": "Email",
    "iosWiFiConfiguration": "Wi-Fi",
    "iosVpnConfiguration": "VPN",
    "macOSGeneralDeviceConfiguration": "Device restrictions",
    "macOSEndpointProtectionConfiguration": "Endpoint protection",
    "macOSDeviceFeaturesConfiguration": "Device features",
    "macOSCustomConfiguration": "Custom",
    "macOSExtensionsConfiguration": "Extensions",
    "macOSSoftwareUpdateConfiguration": "Software updates",
    "androidGeneralDeviceConfiguration": "Device restrictions",
    "androidWorkProfileGeneralDeviceConfiguration": "Work profile restrictions",
    "androidDeviceOwnerGeneralDeviceConfiguration": "Device restrictions (fully managed)",
    "androidCustomConfiguration": "Custom",
    "androidWorkProfileCustomConfiguration": "Custom",
}
TEMPLATE_FAMILIES = {
    "endpointSecurityAntivirus": "Antivirus",
    "endpointSecurityDiskEncryption": "Disk encryption",
    "endpointSecurityFirewall": "Firewall",
    "endpointSecurityEndpointDetectionAndResponse": "Endpoint detection and response",
    "endpointSecurityAttackSurfaceReduction": "Attack surface reduction",
    "endpointSecurityAccountProtection": "Account protection",
    "endpointSecurityApplicationControl": "App control",
    "endpointSecurityEndpointPrivilegeManagement": "Privilege management",
    "baseline": "Security baseline",
    "deviceConfigurationScripts": "Scripts",
    "deviceConfigurationPolicies": "Settings catalog",
    "none": "Settings catalog",
}
SHARING = {"allApps": "With any app", "managedApps": "Managed apps only", "none": "Blocked"}
CLIPBOARD = {
    "allApps": "Any app",
    "managedAppsWithPasteIn": "Managed apps (paste in from any)",
    "managedApps": "Managed apps only",
    "blocked": "Blocked",
}
IOS_ENCRYPTION = {
    "useDeviceSettings": "Device settings",
    "afterDeviceRestart": "After restart",
    "whenDeviceLockedExceptOpenFiles": "When locked (open files excepted)",
    "whenDeviceLocked": "When locked",
}
FLAG_ORDER = {"high": 0, "medium": 1, "low": 2, None: 3}


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _day(value) -> str:
    return _text(value)[:10]


def _number(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _group_names(snap: TenantSnapshot) -> dict[str, str]:
    names: dict[str, str] = {}
    for collector in ("group_nesting", "groups"):
        for g in snap.objects(collector, "group") or []:
            if isinstance(g, dict) and g.get("_id") and g.get("displayName"):
                names[g["_id"]] = g["displayName"]
    return names


def assigned_to(policy: dict, groups: dict[str, str]) -> list[str] | str:
    """Who a policy applies to, in words: "All devices", "All users", group names and "Except <group>"."""
    found = policy.get("assignments")
    if not isinstance(found, list):
        if policy.get("isAssigned") is False:
            return NOBODY
        return "Assigned" if policy.get("isAssigned") else "Not read"
    out = []
    for a in found:
        if not isinstance(a, dict):
            continue
        kind = a.get("kind")
        name = {"all_devices": "All devices", "all_users": "All users"}.get(kind) or groups.get(
            a.get("groupId") or "", "A deleted group"
        )
        text = f"Except {name}" if kind == "exclude" else name
        out.append(text + (" (filtered)" if a.get("filter") else ""))
    return out or NOBODY


def type_label(odata_type: str | None) -> str:
    t = _text(odata_type)
    if t in TYPE_LABELS:
        return TYPE_LABELS[t]
    if "Certificate" in t:
        return "Certificate"
    return t or "Unknown"


def _firewall(s: dict) -> str | None:
    states = [s.get(k) for k in ("firewallProfileDomain", "firewallProfilePrivate", "firewallProfilePublic")]
    if all(state is None or state == "notConfigured" for state in states):
        return None
    if "blocked" in states:
        return "Turns the firewall off for some networks"
    return "Firewall on" if all(state == "allowed" for state in states) else "Firewall on for some networks"


def profile_settings(profile: dict) -> list[str]:
    """The settings of a configuration profile worth seeing at a glance, in plain words."""
    s = intune.settings(profile)
    out = []
    if intune.is_update_ring(profile):
        return [_ring_summary(s)]
    firewall = _firewall(s)
    if firewall:
        out.append(firewall)
    if s.get("firewallEnabled"):
        out.append("Firewall on")
    if s.get("bitLockerEncryptDevice"):
        out.append("Encrypts with BitLocker")
    if s.get("fileVaultEnabled"):
        out.append("Encrypts with FileVault")
    if s.get("gatekeeperAllowedAppSource") in intune.GATEKEEPER:
        out.append(f"Gatekeeper: {intune.GATEKEEPER[s['gatekeeperAllowedAppSource']]}")
    if s.get("smartScreenEnableInShell"):
        out.append("SmartScreen")
    if s.get("deviceGuardEnableVirtualizationBasedSecurity"):
        out.append("Virtualization-based security")
    if s.get("defenderRequireRealTimeMonitoring"):
        out.append("Defender real-time protection")
    if s.get("passwordRequired") or s.get("passcodeRequired"):
        length = _number(s.get("passwordMinimumLength")) or _number(s.get("passcodeMinimumLength"))
        out.append(f"Password of {length}+ characters" if length else "Password")
    lock = _number(s.get("passwordMinutesOfInactivityBeforeScreenTimeout")) or _number(
        s.get("passcodeMinutesOfInactivityBeforeScreenTimeout")
    )
    if lock:
        out.append(f"Screen locks after {lock} min")
    if s.get("storageBlockRemovableStorage"):
        out.append("Blocks USB storage")
    if _text(s.get("desiredOsVersion")):
        out.append(f"Installs version {s['desiredOsVersion']}")
    if _number(s.get("enforcedSoftwareUpdateDelayInDays")):
        out.append(f"Holds updates back {s['enforcedSoftwareUpdateDelayInDays']} days")
    return out


def _ring_summary(s: dict) -> str:
    quality, feature = (
        _number(s.get("qualityUpdatesDeferralPeriodInDays")),
        _number(s.get("featureUpdatesDeferralPeriodInDays")),
    )
    parts = [
        f"Security updates after {plural(quality or 0, 'day')}",
        f"feature updates after {plural(feature or 0, 'day')}",
    ]
    deadline = _number(s.get("deadlineForQualityUpdatesInDays"))
    if deadline is not None:
        parts.append(f"restart deadline {plural(deadline, 'day')}")
    return ", ".join(parts)


def _paused(s: dict) -> list[str]:
    return [
        what for what, key in (("security", "qualityUpdatesPaused"), ("feature", "featureUpdatesPaused")) if s.get(key)
    ]


def _pin(s: dict) -> str:
    if not s.get("pinRequired"):
        return "Not required"
    length = _number(s.get("minimumPinLength"))
    return f"Required ({length}+ digits)" if length else "Required"


def _encryption(policy: dict) -> str:
    s = intune.settings(policy)
    if policy.get("platform") == "iOS":
        return IOS_ENCRYPTION.get(_text(s.get("appDataEncryptionType")), _text(s.get("appDataEncryptionType")))
    if "encryptAppData" in s:
        return "Encrypted" if s.get("encryptAppData") else "Not encrypted"
    return ""


@report(
    "intune-policies",
    "Intune policies",
    "Which platforms have a compliance policy, what each compliance policy requires, configuration profiles and "
    "Windows update rings, endpoint security policies and app protection for phones, with whom each applies to.",
    ("intune_policies", "devices", "groups", "group_nesting"),
    group="devices",
    order=15,
    keywords="intune compliance policy configuration profile endpoint security firewall antivirus bitlocker "
    "disk encryption update ring windows update for business app protection mam pin settings catalog "
    "baseline mdm unassigned",
    uses=("insurance",),
)
def intune_policies(snap: TenantSnapshot, now: datetime, out: DataReport) -> None:
    status = intune.status(snap)
    if status is None:
        out.unavailable = NOT_COLLECTED
        return
    if status.get("available") is False:
        out.unavailable = NO_INTUNE
        return
    parts = intune.parts(snap)
    waiting = [p for p in status.get("waiting") or [] if isinstance(p, str)]
    if waiting:
        out.notice = waiting_text(snap.tenant["name"], waiting)
    groups = _group_names(snap)

    def missing(part: str, what: str) -> str:
        if parts.get(part) == "waiting":
            return f"{what} will show once the client approves the new permission."
        if parts.get(part) == "unlicensed":
            return f"{what} need an Intune licence."
        return f"{what} couldn't be read; see the Intune policies collection's log."

    attention = Section(
        ATTENTION,
        ["Policy", "Kind", "Platform", "What to check"],
        intro="Policies that do nothing or leave a gap, and platforms with devices but no compliance policy.",
        empty="Nothing needs attention: every policy read is assigned and every platform in use has a "
        "compliance policy.",
    )
    needs: list[tuple[list, str | None]] = []

    # Compliance policies.
    compliance = Section(
        COMPLIANCE,
        ["Policy", "Platform", "Requires", "Assigned to", "Last changed"],
        intro="What a device must meet to count as compliant. Conditional Access can then let only compliant "
        "devices reach company data.",
        empty="No compliance policies." if parts.get("compliance") == "read" else missing("compliance", COMPLIANCE),
    )
    by_platform: dict[str, dict[str, int]] = {}

    def count(platform: str, column: str) -> None:
        by_platform.setdefault(platform, {})
        by_platform[platform][column] = by_platform[platform].get(column, 0) + 1

    for p in sorted(intune.policies(snap, "compliance_policy"), key=lambda p: _text(p.get("name")).lower()):
        platform = intune.type_platform(p.get("type"))
        requires = intune.compliance_requires(p)
        is_on = intune.assigned(p)
        flag = None
        if not is_on:
            flag = "low"
            needs.append(([p.get("name"), "Compliance policy", platform, UNASSIGNED], "low"))
        elif not requires:
            flag = "medium"
            needs.append(
                (
                    [
                        p.get("name"),
                        "Compliance policy",
                        platform,
                        "Requires nothing, so every device it covers passes",
                    ],
                    "medium",
                )
            )
        if is_on:
            count(platform, "compliance")
        compliance.add(
            [
                p.get("name"),
                platform,
                requires or "Nothing",
                assigned_to(p, groups),
                _day(p.get("lastModifiedDateTime")),
            ],
            flag,
        )

    # Configuration profiles, with update rings in their own table too.
    profiles = Section(
        PROFILES,
        ["Profile", "Type", "Platform", "Settings", "Assigned to", "Last changed"],
        intro="Settings Intune pushes to devices. Settings shows the ones that matter most for security.",
        empty="No configuration profiles."
        if parts.get("configuration") == "read"
        else missing("configuration", PROFILES),
    )
    rings = Section(
        RINGS,
        [
            "Ring",
            "Security updates after (days)",
            "Feature updates after (days)",
            "Restart deadline (days)",
            "Paused",
            "Drivers",
            "Assigned to",
        ],
        intro="How long Windows waits before installing monthly security updates and yearly feature updates, and "
        "how long before it restarts to finish them. Microsoft suggests no more than 7 days for security updates "
        "on most devices.",
        empty="No Windows update rings: Windows decides when to update."
        if parts.get("configuration") == "read"
        else missing("configuration", RINGS),
    )
    paused_rings = 0
    for p in sorted(intune.policies(snap, "configuration_profile"), key=lambda p: _text(p.get("name")).lower()):
        platform = intune.type_platform(p.get("type"))
        is_on = intune.assigned(p)
        flag = None
        if not is_on:
            flag = "low"
            needs.append(([p.get("name"), type_label(p.get("type")), platform, UNASSIGNED], "low"))
        if is_on:
            count(platform, "profiles")
        if intune.is_update_ring(p):
            s = intune.settings(p)
            paused = _paused(s)
            quality = _number(s.get("qualityUpdatesDeferralPeriodInDays"))
            ring_flag = "medium" if paused and is_on else ("low" if (quality or 0) > 14 or not is_on else None)
            if paused and is_on:
                paused_rings += 1
                needs.append(
                    (
                        [p.get("name"), "Update ring", platform, f"{' and '.join(paused).capitalize()} updates paused"],
                        "medium",
                    )
                )
            rings.add(
                [
                    p.get("name"),
                    quality or 0,
                    _number(s.get("featureUpdatesDeferralPeriodInDays")) or 0,
                    _number(s.get("deadlineForQualityUpdatesInDays")) if "deadlineForQualityUpdatesInDays" in s else "",
                    ", ".join(w.capitalize() for w in paused) or "No",
                    "Left out" if s.get("driversExcluded") else "Included",
                    assigned_to(p, groups),
                ],
                ring_flag,
            )
            flag = flag or ring_flag
        profiles.add(
            [
                p.get("name"),
                type_label(p.get("type")),
                platform,
                profile_settings(p),
                assigned_to(p, groups),
                _day(p.get("lastModifiedDateTime")),
            ],
            flag,
        )

    # Settings catalog and endpoint security policies.
    catalog = Section(
        CATALOG,
        ["Policy", "Kind", "Platform", "Settings", "Assigned", "Last changed"],
        intro="Endpoint security policies (antivirus, firewall, disk encryption and the like), security baselines "
        "and settings catalog profiles.",
        empty="No endpoint security or settings catalog policies."
        if parts.get("settings") == "read"
        else missing("settings", "Endpoint security and settings catalog policies"),
    )
    for p in sorted(intune.policies(snap, "settings_policy"), key=lambda p: _text(p.get("name")).lower()):
        platforms = intune.catalog_platforms(p.get("platforms"))
        family = _text(p.get("templateFamily"))
        kind = TEMPLATE_FAMILIES.get(family) or _text(p.get("templateName")) or "Settings catalog"
        is_on = intune.assigned(p)
        if not is_on:
            needs.append(([p.get("name"), kind, ", ".join(platforms), UNASSIGNED], "low"))
        else:
            for platform in platforms:
                count(platform, "catalog")
        catalog.add(
            [
                p.get("name"),
                kind,
                platforms,
                _number(p.get("settingCount")) if _number(p.get("settingCount")) is not None else "",
                is_on,
                _day(p.get("lastModifiedDateTime")),
            ],
            None if is_on else "low",
        )

    # App protection.
    protection = Section(
        APP_PROTECTION,
        ["Policy", "Platform", "PIN", "Sharing data", "Copy and paste", "Backups", "Encryption", "Apps", "Assigned to"],
        intro="Rules for company data inside apps such as Outlook and Teams on phones, including personal phones "
        "Intune doesn't manage: a PIN to open them, and whether data can leave for other apps.",
        empty="No app protection policies: company data in apps on phones isn't protected."
        if parts.get("app_protection") == "read"
        else missing("app_protection", "App protection policies"),
    )
    protected: set[str] = set()
    for p in sorted(
        intune.policies(snap, "app_protection"), key=lambda p: (_text(p.get("platform")), _text(p.get("name")).lower())
    ):
        s = intune.settings(p)
        platform = intune.IOS if p.get("platform") == "iOS" else intune.ANDROID
        is_on = intune.assigned(p)
        problems = []
        if not is_on:
            problems.append(("low", UNASSIGNED))
        else:
            protected.add(platform)
            count(platform, "app_protection")
            if not s.get("pinRequired"):
                problems.append(("medium", "No PIN to open company apps"))
            if s.get("allowedOutboundDataTransferDestinations") == "allApps":
                problems.append(("low", "Company data can be shared with any app"))
        for severity, text in problems:
            needs.append(([p.get("name"), "App protection", platform, text], severity))
        flag = min((s for s, _ in problems), key=lambda s: FLAG_ORDER[s], default=None)
        protection.add(
            [
                p.get("name"),
                platform,
                _pin(s),
                SHARING.get(_text(s.get("allowedOutboundDataTransferDestinations")), ""),
                CLIPBOARD.get(_text(s.get("allowedOutboundClipboardSharingLevel")), ""),
                ("Blocked" if s.get("dataBackupBlocked") else "Allowed") if "dataBackupBlocked" in s else "",
                _encryption(p),
                _number(p.get("deployedAppCount")) if _number(p.get("deployedAppCount")) is not None else "",
                assigned_to(p, groups),
            ],
            flag,
        )

    # Coverage by platform, from the enrolled devices.
    managed_status = devices.objects(snap, "intune_status") or []
    managed = (
        (devices.objects(snap, "managed_device") or [])
        if managed_status[:1] and managed_status[0].get("available")
        else []
    )
    enrolled: dict[str, int] = {}
    for d in managed:
        platform = intune.device_platform(d)
        if platform:
            enrolled[platform] = enrolled.get(platform, 0) + 1
    coverage = Section(
        COVERAGE,
        [
            "Platform",
            "Enrolled devices",
            "Compliance policies",
            "Configuration profiles",
            "Endpoint security and settings catalog",
            "App protection",
        ],
        intro="For each platform, the devices enrolled in Intune and how many policies assigned to someone cover "
        "it. A device with no compliance policy counts as compliant unless Intune's compliance settings say "
        "otherwise, so Conditional Access can't tell it apart.",
        empty="No devices are enrolled and no policies are assigned.",
        drifts=("Enrolled devices",),
    )
    compliance_read = parts.get("compliance") == "read"
    gaps = 0
    for platform in intune.PLATFORMS:
        counts = by_platform.get(platform, {})
        n = enrolled.get(platform, 0)
        if not n and not counts:
            continue
        gap = compliance_read and n > 0 and not counts.get("compliance")
        gaps += gap
        if gap:
            needs.append(([platform, "Platform", platform, f"{NO_COMPLIANCE} ({plural(n, 'device')})"], "medium"))

        coverage.add(
            [
                platform,
                n,
                _shown(counts, "compliance", parts.get("compliance")),
                _shown(counts, "profiles", parts.get("configuration")),
                _shown(counts, "catalog", parts.get("settings")),
                _shown(counts, "app_protection", parts.get("app_protection")) if platform in intune.MOBILE else "",
            ],
            "medium" if gap else None,
        )

    for row, flag in sorted(needs, key=lambda n: (FLAG_ORDER[n[1]], _text(n[0][1]), _text(n[0][0]).lower())):
        attention.add(row, flag)
    out.sections = [coverage, attention, compliance, rings, profiles, catalog, protection]

    unassigned = sum(1 for row, _ in needs if row[3] == UNASSIGNED)
    n_compliance = len(compliance.rows)
    n_assigned = sum(1 for p in intune.policies(snap, "compliance_policy") if intune.assigned(p))
    mobile_devices = sum(enrolled.get(p, 0) for p in intune.MOBILE)
    out.tiles = [
        Tile(
            "Compliance policies",
            n_compliance if compliance_read else _unread(parts, "compliance")[0],
            f"{n_assigned} assigned to someone" if compliance_read else _unread(parts, "compliance")[1],
            rows=Rows(COMPLIANCE),
        ),
        Tile(
            "Platforms without compliance",
            gaps if compliance_read else _unread(parts, "compliance")[0],
            "Devices enrolled, no compliance policy" if compliance_read else _unread(parts, "compliance")[1],
            ("bad" if gaps else "ok") if compliance_read else None,
            Rows(COVERAGE, flagged=True),
        ),
        Tile(
            "Policies assigned to nobody",
            unassigned,
            "Policies that do nothing until assigned",
            "warn" if unassigned else "ok",
            Rows(ATTENTION, {"What to check": UNASSIGNED}),
        ),
        Tile(
            "Update rings paused",
            paused_rings if parts.get("configuration") == "read" else _unread(parts, "configuration")[0],
            f"Of {plural(len(rings.rows), 'Windows update ring')}"
            if parts.get("configuration") == "read"
            else _unread(parts, "configuration")[1],
            ("warn" if paused_rings else "ok") if parts.get("configuration") == "read" else None,
            # Assigned rings only, as counted: a paused ring nobody gets is flagged low, not medium.
            Rows(RINGS, {"Paused": ["Security", "Feature", "Security, Feature"], "Flag": "medium"}),
        ),
        Tile(
            "App protection",
            _protection_value(protected)
            if parts.get("app_protection") == "read"
            else _unread(parts, "app_protection")[0],
            (f"{plural(mobile_devices, 'enrolled phone')}; personal phones count too")
            if parts.get("app_protection") == "read"
            else _unread(parts, "app_protection")[1],
            ("ok" if len(protected) == 2 else "warn") if parts.get("app_protection") == "read" else None,
            Rows(APP_PROTECTION),
        ),
    ]


def _shown(counts: dict[str, int], column: str, part: str | None) -> int | str:
    """A coverage count, or blank when that kind of policy wasn't read."""
    return counts.get(column, 0) if part == "read" else ""


def _protection_value(protected: set[str]) -> str:
    if len(protected) == 2:
        return "iOS and Android"
    if protected:
        return f"{next(iter(protected)).split('/')[0]} only"
    return "None"


def _unread(parts: dict, part: str) -> tuple[str, str]:
    """A tile's value and foot for a part that wasn't read."""
    if parts.get(part) == "waiting":
        return "Waiting", "For the client's approval"
    if parts.get(part) == "unlicensed":
        return "Not licensed", "Needs Intune"
    return "Not read", "See the collection's log"
