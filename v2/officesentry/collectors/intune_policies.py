"""Intune policies: compliance policies, configuration profiles (Windows update rings among them), settings
catalog and endpoint security policies, and app protection policies for iOS and Android, each with whom it's
assigned to.

Device policies need DeviceManagementConfiguration.Read.All and app protection DeviceManagementApps.Read.All,
both added in Office Sentry 2.2 (``permission_batch``): until the client approves them, that part is skipped
and stored as waiting, so the run doesn't fail and the report says what it's waiting for. A tenant without an
Intune licence isn't asked at all.

Only the settings the report describes are kept (``COMPLIANCE_KEYS``, ``CONFIGURATION_KEYS``,
``APP_PROTECTION_KEYS``); Intune's answer is polymorphic (each policy type has its own settings), so the
lists are read without ``$select``.
"""

from __future__ import annotations

from ..graph import GraphError, LicenseRequired, PermissionMissing
from ..permission_batch import BATCH_OF
from .base import Collector, RunContext, register
from .connection import awaiting

CONFIG = "DeviceManagementConfiguration.Read.All"
APPS = "DeviceManagementApps.Read.All"
PARTS = {"compliance": CONFIG, "configuration": CONFIG, "settings": CONFIG, "app_protection": APPS}

COMPLIANCE = "/deviceManagement/deviceCompliancePolicies"
CONFIGURATIONS = "/deviceManagement/deviceConfigurations"
# Settings catalog and endpoint security policies (firewall, antivirus, disk encryption ...) are only in beta.
SETTINGS = "/deviceManagement/configurationPolicies"
SETTINGS_FIELDS = (
    "id,name,description,platforms,technologies,createdDateTime,lastModifiedDateTime,settingCount,isAssigned,"
    "templateReference"
)
APP_PROTECTION = {
    "iOS": "/deviceAppManagement/iosManagedAppProtections",
    "Android": "/deviceAppManagement/androidManagedAppProtections",
}

# What each compliance policy requires, by Intune's setting names (windows10CompliancePolicy,
# iosCompliancePolicy, android*CompliancePolicy, macOSCompliancePolicy and so on share most of them).
COMPLIANCE_KEYS = (
    "passwordRequired",
    "passwordMinimumLength",
    "passwordRequiredType",
    "passwordMinutesOfInactivityBeforeLock",
    "passwordBlockSimple",
    "passcodeRequired",
    "passcodeMinimumLength",
    "passcodeMinutesOfInactivityBeforeLock",
    "passcodeBlockSimple",
    "bitLockerEnabled",
    "secureBootEnabled",
    "codeIntegrityEnabled",
    "storageRequireEncryption",
    "tpmRequired",
    "activeFirewallRequired",
    "firewallEnabled",
    "defenderEnabled",
    "antivirusRequired",
    "antiSpywareRequired",
    "rtpEnabled",
    "signatureOutOfDate",
    "deviceThreatProtectionEnabled",
    "deviceThreatProtectionRequiredSecurityLevel",
    "systemIntegrityProtectionEnabled",
    "gatekeeperAllowedAppSource",
    "osMinimumVersion",
    "osMaximumVersion",
    "minAndroidSecurityPatchLevel",
    "securityBlockJailbrokenDevices",
    "securityRequireSafetyNetAttestationBasicIntegrity",
    "securityPreventInstallAppsFromUnknownSources",
    "managedEmailProfileRequired",
    "configurationManagerComplianceRequired",
)

# The settings of configuration profiles the report describes: Windows update rings
# (windowsUpdateForBusinessConfiguration), endpoint protection on Windows and macOS, and the screen lock of
# device restriction profiles. The firewall profiles keep only whether the firewall is turned on.
CONFIGURATION_KEYS = (
    "qualityUpdatesDeferralPeriodInDays",
    "featureUpdatesDeferralPeriodInDays",
    "qualityUpdatesPaused",
    "featureUpdatesPaused",
    "qualityUpdatesPauseExpiryDateTime",
    "featureUpdatesPauseExpiryDateTime",
    "deadlineForQualityUpdatesInDays",
    "deadlineForFeatureUpdatesInDays",
    "deadlineGracePeriodInDays",
    "automaticUpdateMode",
    "driversExcluded",
    "microsoftUpdateServiceAllowed",
    "allowWindows11Upgrade",
    "bitLockerEncryptDevice",
    "firewallEnabled",
    "fileVaultEnabled",
    "gatekeeperAllowedAppSource",
    "smartScreenEnableInShell",
    "deviceGuardEnableVirtualizationBasedSecurity",
    "defenderRequireRealTimeMonitoring",
    "storageBlockRemovableStorage",
    "passwordRequired",
    "passwordMinimumLength",
    "passwordMinutesOfInactivityBeforeScreenTimeout",
    "passcodeRequired",
    "passcodeMinimumLength",
    "passcodeMinutesOfInactivityBeforeScreenTimeout",
    "desiredOsVersion",
    "enforcedSoftwareUpdateDelayInDays",
)
FIREWALL_PROFILES = ("firewallProfileDomain", "firewallProfilePrivate", "firewallProfilePublic")

APP_PROTECTION_KEYS = (
    "pinRequired",
    "minimumPinLength",
    "simplePinBlocked",
    "fingerprintBlocked",
    "dataBackupBlocked",
    "allowedOutboundDataTransferDestinations",
    "allowedInboundDataTransferSources",
    "allowedOutboundClipboardSharingLevel",
    "saveAsBlocked",
    "printBlocked",
    "organizationalCredentialsRequired",
    "managedBrowserToOpenLinksRequired",
    "periodOfflineBeforeAccessCheck",
    "periodOfflineBeforeWipeIsEnforced",
    "appDataEncryptionType",
    "encryptAppData",
    "screenCaptureBlocked",
    "deviceComplianceRequired",
    "minimumRequiredOsVersion",
)

TARGETS = {
    "allDevicesAssignmentTarget": "all_devices",
    "allLicensedUsersAssignmentTarget": "all_users",
    "groupAssignmentTarget": "group",
    "exclusionGroupAssignmentTarget": "exclude",
}


def odata_type(item: dict) -> str:
    """``"#microsoft.graph.windows10CompliancePolicy"`` -> ``"windows10CompliancePolicy"``."""
    return (item.get("@odata.type") or "").rsplit(".", 1)[-1]


def assignments(item: dict) -> list[dict] | None:
    """Who a policy is assigned to: all devices, all users, a group or a group left out, each with whether an
    assignment filter narrows it. None when Intune didn't return the assignments."""
    found = item.get("assignments")
    if not isinstance(found, list):
        return None
    out = []
    for a in found:
        target = (a or {}).get("target") or {}
        kind = TARGETS.get(odata_type(target))
        if not kind:
            continue
        mode = target.get("deviceAndAppManagementAssignmentFilterType")
        out.append(
            {
                "kind": kind,
                "groupId": target.get("groupId"),
                "filter": mode if mode in ("include", "exclude") else None,
            }
        )
    return out


UPDATE_RING = "windowsUpdateForBusinessConfiguration"


def is_assigned(policy: dict) -> bool:
    """Whether a stored policy applies to anyone: an assignment that isn't only groups left out, or Intune's
    own ``isAssigned`` where the assignments weren't read (settings catalog and endpoint security)."""
    found = policy.get("assignments")
    if isinstance(found, list):
        return any(a.get("kind") != "exclude" for a in found if isinstance(a, dict))
    return policy.get("isAssigned") is not False


def _kept(item: dict, keys: tuple[str, ...]) -> dict:
    return {k: item[k] for k in keys if k in item and item[k] is not None}


def _common(item: dict) -> dict:
    return {
        "name": item.get("displayName") or item.get("name"),
        "description": (item.get("description") or "")[:300] or None,
        "type": odata_type(item),
        "createdDateTime": item.get("createdDateTime"),
        "lastModifiedDateTime": item.get("lastModifiedDateTime"),
    }


def compliance_policy(item: dict) -> dict:
    return {**_common(item), "assignments": assignments(item), "settings": _kept(item, COMPLIANCE_KEYS)}


def configuration_profile(item: dict) -> dict:
    settings = _kept(item, CONFIGURATION_KEYS)
    for key in FIREWALL_PROFILES:
        profile = item.get(key)
        if isinstance(profile, dict) and profile.get("firewallEnabled") is not None:
            settings[key] = profile.get("firewallEnabled")
    return {**_common(item), "assignments": assignments(item), "settings": settings}


def settings_policy(item: dict) -> dict:
    template = item.get("templateReference") or {}
    return {
        **_common(item),
        "platforms": item.get("platforms"),
        "technologies": item.get("technologies"),
        "templateFamily": template.get("templateFamily"),
        "templateName": template.get("templateDisplayName"),
        "settingCount": item.get("settingCount"),
        "isAssigned": item.get("isAssigned"),
    }


def app_protection(item: dict, platform: str) -> dict:
    return {
        **_common(item),
        "platform": platform,
        "assignments": assignments(item),
        "isAssigned": item.get("isAssigned"),
        "deployedAppCount": item.get("deployedAppCount"),
        "settings": _kept(item, APP_PROTECTION_KEYS),
    }


@register
class IntunePolicies(Collector):
    key = "intune_policies"
    title = "Intune policies"
    description = (
        "Intune compliance policies, configuration profiles and Windows update rings, settings catalog and "
        "endpoint security policies, and app protection for iOS and Android, with whom each is assigned to "
        "(needs Intune)."
    )
    permissions = (CONFIG, APPS)
    needs = ("intune",)

    def collect(self, ctx: RunContext) -> None:
        waiting = awaiting(ctx, *dict.fromkeys(PARTS.values()))
        # Each part: "read", "waiting" (a new permission the client hasn't approved), "unlicensed" or "failed".
        parts = {part: "waiting" if perm in waiting else "read" for part, perm in PARTS.items()}
        if ctx.capabilities.get("intune") is False:
            ctx.log("The tenant has no Intune licence; Intune policies aren't read")
            self._status(ctx, dict.fromkeys(PARTS, "unlicensed"), available=False)
            return
        if waiting:
            ctx.log(f"Waiting for the client to approve {', '.join(waiting)}; those Intune policies are skipped")

        stored: dict[str, list[dict]] = {}
        if parts["compliance"] == "read":
            found = self._read(ctx, parts, "compliance", COMPLIANCE, {"$expand": "assignments"})
            if found is not None:
                stored["compliance"] = self._store(ctx, "compliance_policy", found, compliance_policy)
        if parts["configuration"] == "read":
            found = self._read(ctx, parts, "configuration", CONFIGURATIONS, {"$expand": "assignments"})
            if found is not None:
                stored["configuration"] = self._store(ctx, "configuration_profile", found, configuration_profile)
        if parts["settings"] == "read":
            found = self._read(ctx, parts, "settings", SETTINGS, {"$select": SETTINGS_FIELDS}, version="beta")
            if found is not None:
                stored["settings"] = self._store(ctx, "settings_policy", found, settings_policy)
        if parts["app_protection"] == "read":
            rows = []
            for platform, path in APP_PROTECTION.items():
                found = self._read(ctx, parts, "app_protection", path, {"$expand": "assignments"})
                if found is None:
                    break
                rows += [(p["id"], app_protection(p, platform)) for p in found if p.get("id")]
            if parts["app_protection"] == "read":
                ctx.objects("app_protection", rows)
                stored["app_protection"] = [r for _, r in rows]
        self._status(ctx, parts, available=not all(state == "unlicensed" for state in parts.values()))

        if "compliance" in stored:
            ctx.metric("intune.compliance_policies", len(stored["compliance"]))
        if len(stored) == len(PARTS):  # only a full count is worth a trend line
            ctx.metric("intune.unassigned_policies", sum(not is_assigned(p) for rows in stored.values() for p in rows))
        if "configuration" in stored:
            ctx.metric(
                "intune.paused_update_rings",
                sum(
                    bool(p["settings"].get("qualityUpdatesPaused") or p["settings"].get("featureUpdatesPaused"))
                    for p in stored["configuration"]
                    if p["type"] == UPDATE_RING
                ),
            )

    @staticmethod
    def _store(ctx: RunContext, kind: str, found: list[dict], summary) -> list[dict]:
        rows = [(p["id"], summary(p)) for p in found if p.get("id")]
        ctx.objects(kind, rows)
        return [r for _, r in rows]

    @staticmethod
    def _read(
        ctx: RunContext, parts: dict, part: str, path: str, params: dict, version: str = "v1.0"
    ) -> list[dict] | None:
        try:
            return ctx.paged(ctx.graph.get_all(path, params=params, version=version))
        except LicenseRequired as exc:
            parts[part] = "unlicensed"
            ctx.log(f"Intune {part} needs an Intune licence: {exc.message}")
        except PermissionMissing as exc:
            if PARTS[part] in BATCH_OF:
                parts[part] = "waiting"
                ctx.log(f"Intune {part} skipped: {PARTS[part]} isn't approved yet ({exc.message})")
            else:
                parts[part] = "failed"
                ctx.warn(f"Intune {part} weren't readable: {exc.message}")
        except GraphError as exc:
            parts[part] = "failed"
            ctx.warn(f"Intune {part} weren't readable: {exc.message}")
        return None

    @staticmethod
    def _status(ctx: RunContext, parts: dict, *, available: bool) -> None:
        ctx.objects(
            "intune_policy_status",
            [
                (
                    "status",
                    {
                        "available": available,
                        "parts": parts,
                        "waiting": list(dict.fromkeys(PARTS[p] for p, s in parts.items() if s == "waiting")),
                    },
                )
            ],
        )
