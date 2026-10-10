"""Exchange Online and Graph responses for mobile devices, automatic replies and distribution lists.

Shapes follow what Get-MobileDevice, Get-MobileDeviceStatistics, Get-DistributionGroup,
Get-DistributionGroupMember and Get-MessageTraceV2 return through the admin API's InvokeCommand, and Graph's
``/users/{id}/mailboxSettings/automaticRepliesSetting``, including fields Office Sentry must not keep (phone
numbers, IMEIs, recovery passwords, subjects and senders). Times are relative to now, so the trace falls in
the window each run reads.
"""

import json
import re
from datetime import UTC, datetime, timedelta

import responses

from officesentry.graph import GRAPH_ROOT

V1 = f"{GRAPH_ROOT}/v1.0"
NOW = datetime.now(UTC).replace(microsecond=0)


def ago(days: float = 0, hours: float = 0) -> str:
    """An Exchange timestamp (seven decimal places, as Exchange writes them) this long before now."""
    return (NOW - timedelta(days=days, hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def _device(owner: str, guid: str, device_id: str, model: str, os: str, client: str = "EAS", access="Allowed",
            first="2025-11-03T08:10:00Z", friendly="", reason="Global"):
    return {
        "FriendlyName": friendly, "DeviceId": device_id, "DeviceImei": "356938035643809",
        "DeviceMobileOperator": "Vodafone UK", "DeviceOS": os, "DeviceOSLanguage": "en-GB",
        "DeviceTelephoneNumber": "+44 7700 900123", "DeviceType": model.split()[0], "DeviceUserAgent": f"{model}/1.0",
        "DeviceModel": model, "FirstSyncTime": first, "UserDisplayName": f"EURPR01A001.prod.outlook.com/Microsoft "
        f"Exchange Hosted Organizations/x.onmicrosoft.com/{owner}", "DeviceAccessState": access,
        "DeviceAccessStateReason": reason, "DeviceAccessControlRule": None, "ClientVersion": "16.1",
        "ClientType": client, "IsManaged": False, "IsCompliant": False, "IsDisabled": False,
        "Identity": f"{owner}\\ExchangeActiveSyncDevices\\{model.split()[0]}§{device_id}", "Guid": guid,
        "DistinguishedName": f"CN={model.split()[0]}§{device_id},CN=ExchangeActiveSyncDevices,CN={owner},"
                             "OU=x.onmicrosoft.com,OU=Microsoft Exchange Hosted Organizations,DC=EURPR01A001,"
                             "DC=prod,DC=outlook,DC=com",
        "WhenChangedUTC": "2026-09-01T10:00:00Z", "WhenCreatedUTC": first, "ObjectState": "Unchanged",
    }


def _stats(device: dict, last_sync: str | None, status: str = "DeviceOk", wipe_requested: str | None = None):
    return {
        "FirstSyncTime": device["FirstSyncTime"], "LastPolicyUpdateTime": last_sync, "LastSyncAttemptTime": last_sync,
        "LastSuccessSync": last_sync, "DeviceType": device["DeviceType"], "DeviceID": device["DeviceId"],
        "DeviceUserAgent": device["DeviceUserAgent"], "DeviceWipeSentTime": None,
        "DeviceWipeRequestTime": wipe_requested, "DeviceWipeAckTime": None, "LastPingHeartbeat": 540,
        "RecoveryPassword": "********", "DeviceModel": device["DeviceModel"], "DeviceImei": device["DeviceImei"],
        "DeviceFriendlyName": device["FriendlyName"], "DeviceOS": device["DeviceOS"],
        "DevicePhoneNumber": device["DeviceTelephoneNumber"], "Status": status, "StatusNote": None,
        "DeviceAccessState": device["DeviceAccessState"], "ClientType": device["ClientType"],
        "Identity": device["Identity"], "Guid": device["Guid"], "IsRemoteWipeSupported": True,
        "NumberOfFoldersSynced": 12,
    }


# Ann (u1) has a current iPhone on the Outlook app and an old Android on the built-in mail app; Bob (u2, sign-in
# blocked) still has an iPhone syncing; the shared mailbox has a quarantined tablet. Mailboxes are named by their
# Entra object ID, as Exchange Online names new mailboxes.
ANN_IPHONE = _device("u1", "d-1", "ABC123", "iPhone 15", "iOS 17.5.1 21F90", client="Outlook", friendly="Ann's iPhone")
ANN_ANDROID = _device("u1", "d-2", "DEF456", "SM-G991B", "Android 12", first="2023-01-10T09:00:00Z")
BOB_IPHONE = _device("u2", "d-3", "GHI789", "iPhone 13", "iOS 16.7 20H19")
INFO_TABLET = _device("u7", "d-4", "JKL012", "iPad Air", "iOS 17.1 21B74", access="Quarantined",
                      reason="DeviceRule")
DEVICES = [ANN_IPHONE, ANN_ANDROID, BOB_IPHONE, INFO_TABLET]
DEVICE_STATS = {
    "g-u1": [_stats(ANN_IPHONE, ago(hours=3)), _stats(ANN_ANDROID, ago(days=200))],
    "u2": [_stats(BOB_IPHONE, ago(days=1))],
    "g-u7": [_stats(INFO_TABLET, None, status="DeviceDiscovery")],
}


def _group(name: str, address: str, guid: str, *, owners=(), outside=False, kind="MailUniversalDistributionGroup",
           synced=False):
    return {
        "Name": name, "DisplayName": name, "Alias": address.split("@")[0], "PrimarySmtpAddress": address,
        "EmailAddresses": [f"SMTP:{address}"], "ExternalDirectoryObjectId": f"eid-{guid}", "Guid": guid,
        "RecipientTypeDetails": kind, "GroupType": "Universal", "ManagedBy": list(owners),
        "MemberJoinRestriction": "Closed", "MemberDepartRestriction": "Closed",
        "RequireSenderAuthenticationEnabled": not outside, "HiddenFromAddressListsEnabled": False,
        "ModerationEnabled": False, "IsDirSynced": synced, "WhenCreatedUTC": "2024-02-01T10:00:00Z",
        "WhenChangedUTC": "2026-08-01T10:00:00Z", "AcceptMessagesOnlyFromSendersOrMembers": [],
    }


def _member(name: str, address: str, kind: str = "UserMailbox", eid: str | None = None):
    return {"Name": eid or name, "DisplayName": name, "PrimarySmtpAddress": address,
            "ExternalEmailAddress": f"SMTP:{address}" if kind == "MailContact" else None,
            "RecipientType": "MailContact" if kind == "MailContact" else "UserMailbox",
            "RecipientTypeDetails": kind, "ExternalDirectoryObjectId": eid, "Guid": f"g-{name}"}


# Everyone: owned by Ann, open to outside senders, busy. Sales: owned only by Bob (sign-in blocked), with an
# outside contact. Old project: no owner, no members, no mail. Rooms: a room list, never shown.
DISTRIBUTION_GROUPS = [
    _group("Everyone", "everyone@x.com", "dl-1", owners=["u1"], outside=True),
    _group("Sales", "sales@x.com", "dl-2", owners=["u2"], kind="MailUniversalSecurityGroup"),
    _group("Old project", "oldproject@x.com", "dl-3"),
    _group("Meeting rooms", "rooms@x.com", "dl-4", kind="RoomList"),
]
GROUP_MEMBERS = {
    "dl-1": [_member("Ann", "ann@x.com", eid="u1"), _member("Info", "info@x.com", "SharedMailbox", "u7"),
             _member("Sales", "sales@x.com", "MailUniversalSecurityGroup")],
    "dl-2": [_member("Ann", "ann@x.com", eid="u1"), _member("Partner", "pat@partner.example", "MailContact")],
    "dl-3": [],
}


def _trace(recipient: str, received: str, n: int):
    return {"Organization": "x.onmicrosoft.com", "MessageId": f"<{n}@mail.example>", "Received": received,
            "SenderAddress": f"sender{n}@outside.example", "RecipientAddress": recipient,
            "Subject": f"Private subject {n}", "Status": "Expanded", "ToIP": None, "FromIP": "198.51.100.7",
            "Size": 23456, "MessageTraceId": f"0000000{n}-aaaa-bbbb-cccc-000000000000", "StartDate": ago(days=10),
            "EndDate": ago(), "Index": n}


MESSAGE_TRACE = [
    _trace("everyone@x.com", ago(days=1), 1), _trace("everyone@x.com", ago(days=2), 2),
    _trace("everyone@x.com", ago(days=2, hours=4), 3), _trace("sales@x.com", ago(days=6), 4),
]

EXCHANGE = {
    "Get-MobileDevice": DEVICES,
    "Get-MobileDeviceStatistics": DEVICE_STATS,
    "Get-DistributionGroup": DISTRIBUTION_GROUPS,
    "Get-DistributionGroupMember": GROUP_MEMBERS,
    "Get-MessageTraceV2": MESSAGE_TRACE,
}


def auto_reply(status="disabled", audience="all", start=None, end=None, internal="", external=""):
    return {
        "@odata.context": f"{V1}/$metadata#users('x')/mailboxSettings/automaticRepliesSetting",
        "status": status, "externalAudience": audience,
        "scheduledStartDateTime": {"dateTime": start or "2026-10-01T08:00:00.0000000", "timeZone": "UTC"},
        "scheduledEndDateTime": {"dateTime": end or "2026-10-02T08:00:00.0000000", "timeZone": "UTC"},
        "internalReplyMessage": internal, "externalReplyMessage": external,
    }


AUTO_REPLIES = {
    "u1": auto_reply("alwaysEnabled", "all",
                     internal="<html><body><p>I'm out of the office until Monday.</p><p>For urgent things call "
                              "Bob.</p></body></html>",
                     external="<html><head><style>p{color:red}</style></head><body><p>Thanks for your "
                              "email &amp; sorry, I&#39;m away.</p></body></html>"),
    "u7": auto_reply(),
}


def mock_auto_replies(rsps, replies=None):
    replies = AUTO_REPLIES if replies is None else replies

    def callback(request):
        uid = request.path_url.split("/")[3]
        found = replies.get(uid)
        if found is None:
            return 404, {}, json.dumps({"error": {"code": "ErrorItemNotFound", "message": "No mailbox."}})
        return 200, {}, json.dumps(found)
    rsps.add_callback(responses.GET, re.compile(re.escape(V1) + r"/users/[^/]+/mailboxSettings/"
                                                r"automaticRepliesSetting"), callback=callback,
                      content_type="application/json")
