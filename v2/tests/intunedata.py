"""Intune policy fixtures, shaped like Graph's answers with ``$expand=assignments``.

Windows has an assigned compliance policy; the iOS one is assigned to nobody, so the iPhone in ``devicedata``
has none. One update ring is paused, a Mac profile and a FileVault policy are assigned to nobody, and the
Android app protection policy asks for no PIN. ``seed.mock_intune`` serves them.
"""


def _target(kind: str, group: str | None = None, **extra) -> dict:
    target = {"@odata.type": f"#microsoft.graph.{kind}", **extra}
    if group:
        target["groupId"] = group
    return {"id": f"{kind}-{group or 'all'}", "target": target}


ALL_DEVICES = _target("allDevicesAssignmentTarget", deviceAndAppManagementAssignmentFilterType="none")
ALL_USERS = _target("allLicensedUsersAssignmentTarget")

COMPLIANCE_POLICIES = [
    {
        "@odata.type": "#microsoft.graph.windows10CompliancePolicy",
        "id": "cp-win",
        "displayName": "Windows baseline",
        "description": "Every company PC",
        "createdDateTime": "2025-03-01T10:00:00Z",
        "lastModifiedDateTime": "2026-08-14T09:30:00Z",
        "passwordRequired": True,
        "passwordMinimumLength": 8,
        "bitLockerEnabled": True,
        "secureBootEnabled": True,
        "defenderEnabled": True,
        "osMinimumVersion": "10.0.22631",
        "earlyLaunchAntiMalwareDriverEnabled": False,  # not one the report describes: not kept
        "assignments": [ALL_DEVICES],
    },
    {
        "@odata.type": "#microsoft.graph.iosCompliancePolicy",
        "id": "cp-ios",
        "displayName": "iPhone draft",
        "description": None,
        "createdDateTime": "2026-02-01T10:00:00Z",
        "lastModifiedDateTime": "2026-02-01T10:00:00Z",
        "passcodeRequired": True,
        "passcodeMinimumLength": 6,
        "securityBlockJailbrokenDevices": True,
        "assignments": [],
    },
]

CONFIGURATIONS = [
    {
        "@odata.type": "#microsoft.graph.windowsUpdateForBusinessConfiguration",
        "id": "ring-broad",
        "displayName": "Update ring Broad",
        "createdDateTime": "2025-03-01T10:00:00Z",
        "lastModifiedDateTime": "2026-05-01T10:00:00Z",
        "qualityUpdatesDeferralPeriodInDays": 7,
        "featureUpdatesDeferralPeriodInDays": 60,
        "qualityUpdatesPaused": False,
        "featureUpdatesPaused": False,
        "deadlineForQualityUpdatesInDays": 3,
        "driversExcluded": False,
        "assignments": [_target("groupAssignmentTarget", "g1"), _target("exclusionGroupAssignmentTarget", "g2")],
    },
    {
        "@odata.type": "#microsoft.graph.windowsUpdateForBusinessConfiguration",
        "id": "ring-pilot",
        "displayName": "Update ring Pilot",
        "createdDateTime": "2025-03-01T10:00:00Z",
        "lastModifiedDateTime": "2026-09-30T10:00:00Z",
        "qualityUpdatesDeferralPeriodInDays": 0,
        "featureUpdatesDeferralPeriodInDays": 0,
        "qualityUpdatesPaused": True,
        "featureUpdatesPaused": False,
        "deadlineForQualityUpdatesInDays": 2,
        "driversExcluded": True,
        "assignments": [_target("groupAssignmentTarget", "g4", deviceAndAppManagementAssignmentFilterType="include")],
    },
    {
        "@odata.type": "#microsoft.graph.windows10EndpointProtectionConfiguration",
        "id": "ep-win",
        "displayName": "Windows firewall",
        "createdDateTime": "2025-03-01T10:00:00Z",
        "lastModifiedDateTime": "2026-01-10T10:00:00Z",
        "firewallProfileDomain": {"firewallEnabled": "allowed", "inboundConnectionsBlocked": True},
        "firewallProfilePrivate": {"firewallEnabled": "allowed"},
        "firewallProfilePublic": {"firewallEnabled": "allowed"},
        "bitLockerEncryptDevice": True,
        "assignments": [ALL_DEVICES],
    },
    {
        "@odata.type": "#microsoft.graph.macOSGeneralDeviceConfiguration",
        "id": "mac-restrict",
        "displayName": "Mac restrictions",
        "createdDateTime": "2026-04-01T10:00:00Z",
        "lastModifiedDateTime": "2026-04-01T10:00:00Z",
        "passwordRequired": True,
        "passwordMinimumLength": 10,
        "assignments": [],
    },
]

SETTINGS_POLICIES = [
    {
        "id": "sc-av",
        "name": "Defender antivirus",
        "description": "",
        "platforms": "windows10",
        "technologies": "mdm,microsoftSense",
        "createdDateTime": "2025-06-01T10:00:00Z",
        "lastModifiedDateTime": "2026-07-01T10:00:00Z",
        "settingCount": 14,
        "isAssigned": True,
        "templateReference": {
            "templateId": "804339ad-1553-4478-a742-138fb5807418_1",
            "templateFamily": "endpointSecurityAntivirus",
            "templateDisplayName": "Microsoft Defender Antivirus",
            "templateDisplayVersion": "Version 1",
        },
    },
    {
        "id": "sc-filevault",
        "name": "Mac FileVault",
        "description": "",
        "platforms": "macOS",
        "technologies": "mdm",
        "createdDateTime": "2026-04-01T10:00:00Z",
        "lastModifiedDateTime": "2026-04-01T10:00:00Z",
        "settingCount": 3,
        "isAssigned": False,
        "templateReference": {
            "templateId": "a239407c-698d-4ef8-b314-e3ae409204b8_1",
            "templateFamily": "endpointSecurityDiskEncryption",
            "templateDisplayName": "FileVault",
            "templateDisplayVersion": "Version 1",
        },
    },
]

IOS_APP_PROTECTION = [
    {
        "@odata.type": "#microsoft.graph.iosManagedAppProtection",
        "id": "T_ios-office",
        "displayName": "iOS Outlook and Teams",
        "createdDateTime": "2025-03-01T10:00:00Z",
        "lastModifiedDateTime": "2026-03-01T10:00:00Z",
        "pinRequired": True,
        "minimumPinLength": 6,
        "allowedOutboundDataTransferDestinations": "managedApps",
        "allowedOutboundClipboardSharingLevel": "managedAppsWithPasteIn",
        "dataBackupBlocked": True,
        "appDataEncryptionType": "whenDeviceLocked",
        "deployedAppCount": 4,
        "isAssigned": True,
        "assignments": [ALL_USERS],
    },
]

ANDROID_APP_PROTECTION = [
    {
        "@odata.type": "#microsoft.graph.androidManagedAppProtection",
        "id": "T_android-outlook",
        "displayName": "Android Outlook",
        "createdDateTime": "2025-03-01T10:00:00Z",
        "lastModifiedDateTime": "2026-03-01T10:00:00Z",
        "pinRequired": False,
        "allowedOutboundDataTransferDestinations": "allApps",
        "allowedOutboundClipboardSharingLevel": "allApps",
        "dataBackupBlocked": False,
        "encryptAppData": True,
        "deployedAppCount": 2,
        "isAssigned": True,
        "assignments": [_target("groupAssignmentTarget", "g1")],
    },
]

DENIED = {"error": {"code": "Forbidden", "message": "Application is not authorized to perform this operation."}}
UNLICENSED = {"error": {"code": "BadRequest", "message": "Request not applicable to target tenant."}}
