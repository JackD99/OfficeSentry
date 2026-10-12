"""The live-fixture scrubber: nothing that identifies the tenant or its people may survive."""

import json
import re

import pytest

from officesentry import recording
from officesentry.recording import find_leaks, phone_leaks, scrub

TENANT = "a1b2c3d4-1111-4222-8333-444455556666"
CLIENT = "c0ffee00-1111-4222-8333-444455556666"
JANE = "b0b0b0b0-1111-4222-8333-444455556666"
GROUP = "d00d0000-1111-4222-8333-444455556666"
GLOBAL_ADMIN = "62e90394-69f5-4237-9190-012177145e10"  # a role template the code looks for

GRAPH = "https://graph.microsoft.com"


def _graph(path, body):
    return {"api": "graph", "url": f"{GRAPH}{path}", "status": 200, "body": body}


def raw_log():
    jane = {
        "id": JANE,
        "displayName": "Jane Smithson",
        "givenName": "Jane",
        "surname": "Smithson",
        "userPrincipalName": "jane.smithson@contoso.com",
        "mail": "jane.smithson@contoso.com",
        "mailNickname": "jsmithson",
        "businessPhones": ["07700 900123"],
        "mobilePhone": "+44 7700 900456",
        "faxNumber": "(212) 555-7788",
        "otherPhones": [],
        "jobTitle": None,
        "onPremisesSecurityIdentifier": "S-1-5-21-1514539094-1088041144-2428767999-1129",
        "onPremisesImmutableId": "OKQ4SPFIsESXgoBAjSK8kg==",
    }
    return [
        _graph(
            "/v1.0/organization",
            {
                "value": [
                    {
                        "id": TENANT,
                        "displayName": "Contoso Widgets",
                        "verifiedDomains": [{"name": "contoso.com"}, {"name": "contosowidgets.onmicrosoft.com"}],
                    }
                ]
            },
        ),
        _graph("/v1.0/users?$select=id,displayName", {"value": [jane]}),
        _graph(f"/v1.0/groups/{GROUP}", {"id": GROUP, "displayName": "Finance Team"}),
        _graph(
            "/v1.0/auditLogs/signIns",
            {
                "value": [
                    {
                        "userId": JANE,
                        "userDisplayName": "Jane Smithson",
                        "userPrincipalName": "jane.smithson@contoso.com",
                        "ipAddress": "81.2.69.160",
                        "clientAppUsed": "Browser",
                        "location": {
                            "city": "Kilmarnock",
                            "state": "East Ayrshire",
                            "countryOrRegion": "GB",
                            "geoCoordinates": {"altitude": None, "latitude": 55.611, "longitude": -4.495},
                        },
                    },
                    {
                        "userId": JANE,
                        "userDisplayName": "Jane Smithson",
                        "userPrincipalName": "jane.smithson@contoso.com",
                        "ipAddress": "2a00:23c5:1234::1",
                    },
                ]
            },
        ),
        _graph("/v1.0/directoryRoles", {"value": [{"roleTemplateId": GLOBAL_ADMIN, "members": [{"id": JANE}]}]}),
        _graph(
            "/v1.0/roleManagement/directory/roleAssignments?$expand=principal",
            {
                "value": [
                    {
                        "principal": {
                            **jane,
                            "businessPhones": ["07700900123"],
                        },  # the same number, written differently
                    }
                ]
            },
        ),
        _graph(
            "/v1.0/sites",
            {
                "value": [
                    {
                        "webUrl": "https://contoso-my.sharepoint.com/personal/jane_smithson_contoso_com/Documents",
                        "description": "Call Jane on 020 7946 0123 or +44 20 7946 0999",
                    }
                ]
            },
        ),
        _graph(
            f"/v1.0/users/{JANE}/authentication/methods",
            {
                "value": [
                    {
                        "phoneNumber": "+44 7700 900456",
                        "displayName": "Jane's iPhone",
                        "phoneType": "mobile",
                    }
                ]
            },
        ),
        _graph("/v1.0/reports/usage", {"value": [{"mobile": False, "userPrincipalName": "jane.smithson@contoso.com"}]}),
        {
            "api": "exchange",
            "url": "https://outlook.office365.com/adminapi/beta/x/InvokeCommand",
            "status": 200,
            "cmdlet": {"CmdletName": "Get-Mailbox"},
            "body": {
                "value": [
                    {
                        "DisplayName": "Jane Smithson",
                        "Alias": "jsmithson",
                        "Name": "Jane Smithson",
                        "Phone": "07700900789",
                        "PrimarySmtpAddress": "jane.smithson@contoso.com",
                    }
                ]
            },
        },
        {
            "api": "dns",
            "name": "contoso.com",
            "rdtype": "TXT",
            "answer": ["v=spf1 include:spf.protection.outlook.com include:spf.sendgrid.net -all", "MS=ms12345678"],
        },
    ]


SENSITIVE = [
    "Jane",
    "Smithson",
    "jsmithson",
    "contoso",
    "Contoso Widgets",
    "Finance Team",
    TENANT,
    CLIENT,
    JANE,
    GROUP,
    "81.2.69.160",
    "2a00:23c5",
    "7700",
    "7946",
    "555-7788",
    "MS=ms12345678",
    "Kilmarnock",
    "Ayrshire",
    "55.611",
    "4.495",
    "sendgrid",
    "1514539094",
    "OKQ4SPFIsESXgoBAjSK8kg",
]


@pytest.fixture(scope="module")
def fixture():
    return scrub(raw_log(), TENANT, CLIENT)


def test_nothing_identifying_survives(fixture):
    text = json.dumps(fixture)
    for value in SENSITIVE:
        assert value.lower() not in text.lower(), value


def test_names_emails_and_domains_get_consistent_placeholders(fixture):
    users = fixture["entries"][1]["body"]["value"][0]
    sign_in = fixture["entries"][3]["body"]["value"][0]
    assert users["displayName"] == sign_in["userDisplayName"] == "Test User 1"
    assert users["userPrincipalName"] == users["mail"] == sign_in["userPrincipalName"]
    assert re.fullmatch(r"user\d+@sentrytest\d*\.example", users["userPrincipalName"])
    assert fixture["entries"][0]["body"]["value"][0]["displayName"] == "Sentry Test Ltd"
    assert fixture["entries"][2]["body"]["displayName"] == "Test Group 1"
    assert fixture["entries"][-1]["name"] == users["userPrincipalName"].split("@")[1]


def test_names_microsoft_hides_stay_hidden():
    """A tenant that hides names in usage reports sends 32-character codes; they stay codes in the fixture, so
    the replay still shows hidden names."""
    upn, name = "E6F3956464556EB0F7E310CCFAEBE1D1", "8815384BBDE8447FBF5C03DF8E19EBA2"
    log = raw_log() + [
        _graph(
            "/beta/reports/getOffice365ActiveUserDetail(period='D30')",
            {"value": [{"userPrincipalName": upn, "displayName": name}]},
        ),
        _graph(
            "/beta/reports/getOneDriveUsageAccountDetail(period='D30')",
            {"value": [{"ownerPrincipalName": upn.lower(), "ownerDisplayName": name}]},
        ),
    ]
    entries = scrub(log, TENANT, CLIENT)["entries"]
    assert entries[-2]["body"]["value"] == [{"userPrincipalName": upn, "displayName": name}]
    assert entries[-1]["body"]["value"] == [{"ownerPrincipalName": upn.lower(), "ownerDisplayName": name}]
    assert entries[1]["body"]["value"][0]["displayName"] == "Test User 1"  # real names are still replaced


def test_an_id_where_a_name_would_be_isnt_taken_for_a_name():
    # An audit event's target can be named with the empty GUID, which the hidden-name usage reports also use as
    # every user's and team's ID. Taken for a person's name, it became "Test User N" all through the recording.
    empty = "00000000-0000-0000-0000-000000000000"
    log = raw_log() + [
        _graph(
            "/v1.0/auditLogs/directoryAudits",
            {"value": [{"targetResources": [{"id": empty, "displayName": empty, "userPrincipalName": None}]}]},
        ),
        _graph("/beta/reports/getTeamsTeamActivityDetail(period='D90')", {"value": [{"teamId": empty}]}),
    ]
    entries = scrub(log, TENANT, CLIENT)["entries"]
    assert entries[-2]["body"]["value"][0]["targetResources"][0]["displayName"] == empty
    assert entries[-1]["body"]["value"] == [{"teamId": empty}]


def test_guids_are_consistent_and_known_ids_are_kept(fixture):
    user_id = fixture["entries"][1]["body"]["value"][0]["id"]
    assert re.fullmatch(r"5e[0-9a-f]{6}-0000-4000-8000-[0-9a-f]{12}", user_id)
    assert fixture["entries"][3]["body"]["value"][0]["userId"] == user_id
    role = fixture["entries"][4]["body"]["value"][0]
    assert role["members"][0]["id"] == user_id
    assert role["roleTemplateId"] == GLOBAL_ADMIN
    assert fixture["tenant_id"] == fixture["entries"][0]["body"]["value"][0]["id"] != TENANT
    assert GROUP not in fixture["entries"][2]["url"]


def test_phones_are_replaced_by_key_and_by_format(fixture):
    user = fixture["entries"][1]["body"]["value"][0]
    fake = re.compile(r"\+1 555 01\d\d")
    assert all(fake.fullmatch(v) for v in [*user["businessPhones"], user["mobilePhone"], user["faxNumber"]])
    assert user["otherPhones"] == []
    # The same number keeps the same placeholder, however it was written.
    principal = fixture["entries"][5]["body"]["value"][0]["principal"]
    assert principal["businessPhones"] == user["businessPhones"]
    description = fixture["entries"][6]["body"]["value"][0]["description"]
    assert len(fake.findall(description)) == 2
    assert fixture["entries"][9]["body"]["value"][0]["Phone"].startswith("+1 555 01")
    # Flags that only share a phone-ish name are left alone.
    assert fixture["entries"][8]["body"]["value"][0]["mobile"] is False
    method = fixture["entries"][7]["body"]["value"][0]
    assert method["phoneNumber"] == method["displayName"] == "redacted"
    assert method["phoneType"] == "mobile"


def test_ips_become_documentation_addresses(fixture):
    first, second = fixture["entries"][3]["body"]["value"]
    assert first["ipAddress"].startswith("203.0.113.")
    assert second["ipAddress"].startswith("2001:db8::")


def test_sign_in_places_become_numbered_placeholders(fixture):
    first, second = fixture["entries"][3]["body"]["value"]
    assert first["location"] == {
        "city": "Town 1",
        "state": "County 1",
        "countryOrRegion": "GB",
        "geoCoordinates": {"altitude": None, "latitude": None, "longitude": None},
    }
    assert "location" not in second


def test_committed_fixture_has_no_sign_in_places():
    from pathlib import Path

    from officesentry.recording import _objects

    path = Path(__file__).parent / "fixtures" / "live_tenant.json"
    for place in _objects(json.loads(path.read_text(encoding="utf-8"))):
        if "geoCoordinates" in place:
            assert not any((place["geoCoordinates"] or {}).values()), place
            assert re.fullmatch(r"(Town \d+)?", place.get("city") or ""), place["city"]
            assert re.fullmatch(r"(County \d+)?", place.get("state") or ""), place["state"]


def test_personal_onedrive_urls_and_dns_tokens(fixture):
    site = fixture["entries"][6]["body"]["value"][0]["webUrl"]
    assert "/personal/user1_sentrytest_example/" in site
    assert fixture["entries"][-1]["answer"] == [
        "v=spf1 include:spf.protection.outlook.com include:spf1.mail-provider.example -all"
    ]


def test_on_premises_ids_are_replaced_consistently(fixture):
    users = fixture["entries"][1]["body"]["value"][0]
    principal = fixture["entries"][5]["body"]["value"][0]["principal"]
    assert users["onPremisesSecurityIdentifier"] == "S-1-5-21-1000000000-2000000000-3000000001-1129"
    assert principal["onPremisesSecurityIdentifier"] == users["onPremisesSecurityIdentifier"]
    assert users["onPremisesImmutableId"] == principal["onPremisesImmutableId"] != "OKQ4SPFIsESXgoBAjSK8kg=="


def test_sharepoint_drive_ids_have_their_guids_replaced():
    # A drive ID is "b!" and the site, web and list GUIDs in base64url: the plain GUID pattern can't see them.
    import base64
    import uuid

    site, web, lst = (
        "a5e1e000-1111-4222-8333-444455556666",
        "3eb00000-1111-4222-8333-444455556666",
        "11570000-1111-4222-8333-444455556666",
    )
    drive = "b!" + base64.urlsafe_b64encode(b"".join(uuid.UUID(g).bytes_le for g in (site, web, lst))).decode()
    log = [
        _graph("/v1.0/organization", {"value": [{"id": TENANT, "verifiedDomains": [{"name": "contoso.com"}]}]}),
        _graph(f"/v1.0/sites/contoso.sharepoint.com,{site},{web}/drives", {"value": [{"id": drive}]}),
        _graph(
            f"/v1.0/drives/{drive}/root/delta",
            {"@odata.deltaLink": f"{GRAPH}/v1.0/drives/{drive}/root/delta?token=x", "value": []},
        ),
    ]
    fixture = scrub(log, TENANT, CLIENT)
    text = json.dumps(fixture)
    assert drive[2:] not in text and site not in text and web not in text
    fake = fixture["entries"][1]["body"]["value"][0]["id"]
    assert fake.startswith("b!") and len(fake) == len(drive)
    fake_site, fake_web, _ = (
        str(uuid.UUID(bytes_le=base64.urlsafe_b64decode(fake[2:])[i : i + 16])) for i in (0, 16, 32)
    )
    # The same placeholders as the plain GUIDs in the site's address, so replayed requests still match.
    assert f",{fake_site},{fake_web}/drives" in fixture["entries"][1]["url"]
    assert fixture["entries"][2]["url"] == f"{GRAPH}/v1.0/drives/{fake}/root/delta"
    assert fixture["entries"][2]["body"]["@odata.deltaLink"].startswith(f"{GRAPH}/v1.0/drives/{fake}/")


def test_outlook_ids_have_their_mailbox_guid_replaced():
    # A folder ID is base64url of the mailbox GUID as text and binary store and folder IDs.
    import base64

    mailbox = "67794afe-1111-4222-8333-444455556666"
    raw = b"\x00\x03$\x00" + mailbox.encode() + b"\x00.\x00\x00\x00\x00\x00" + bytes(range(40, 82))
    folder = base64.urlsafe_b64encode(raw).decode()
    assert folder.startswith("AAMk") and folder.endswith("=")
    user = "b0b00000-1111-4222-8333-444455556666"
    rules = f"/v1.0/users/{user}/mailFolders/inbox/messageRules"
    log = [
        _graph("/v1.0/organization", {"value": [{"id": TENANT, "verifiedDomains": [{"name": "contoso.com"}]}]}),
        _graph(rules, {"value": [{"actions": {"moveToFolder": folder}}]}),
        _graph(f"/v1.0/users/{user}/mailFolders/{folder}?%24select=displayName", {"displayName": "Archive"}),
    ]
    fixture = scrub(log, TENANT, CLIENT)
    text = json.dumps(fixture)
    assert folder.rstrip("=") not in text and mailbox not in text
    fake = fixture["entries"][1]["body"]["value"][0]["actions"]["moveToFolder"]
    assert fake.startswith("AAMk") and len(fake) == len(folder)
    assert base64.urlsafe_b64decode(fake)[4:40].decode().startswith("5e")
    # The rule and the folder lookup still name the same folder, so the replay matches.
    assert f"/mailFolders/{fake}?" in fixture["entries"][2]["url"]


def test_scrub_refuses_to_write_when_something_survives(monkeypatch):
    monkeypatch.setattr(recording.Scrubber, "text_only", lambda self, value: value)
    with pytest.raises(ValueError, match="identifying value") as exc:
        scrub(raw_log(), TENANT, CLIENT)
    assert "Smithson" not in str(exc.value)  # the hint never prints the values


def test_phone_leak_check():
    assert phone_leaks({"businessPhones": ["07470699178"]}) == ["07470699178"]
    assert phone_leaks({"mobilePhone": "+44 7700 900456"}) == ["+44 7700 900456"]
    assert phone_leaks({"description": "ring 020 7946 0123"}) == ["020 7946 0123"]
    assert phone_leaks({"businessPhones": ["+1 555 0100"], "mobile": False, "phoneType": "mobile"}) == []
    # Dates, GUIDs, versions and request URLs aren't phone numbers.
    clean = {
        "createdDateTime": "2026-04-01",
        "id": "00000000-0000-0000-0000-000000000002",
        "version": "16.0.17126.20132",
        "url": "https://graph.microsoft.com/v1.0/users/07470699178",
    }
    assert phone_leaks(clean) == []


def test_find_leaks_matches_whole_words_only():
    assert find_leaks("Administrator signed in", ["admin"]) == []
    assert find_leaks("signed in as Jane Smithson", ["Smithson", "abc"]) == ["Smithson"]


def test_committed_fixture_has_no_real_phone_numbers():
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / "live_tenant.json"
    assert phone_leaks(json.loads(path.read_text(encoding="utf-8"))) == []


def test_device_numbers_and_free_text_are_redacted():
    log = raw_log() + [
        {
            "api": "exchange",
            "url": "https://outlook.office365.com/adminapi/beta/x/InvokeCommand",
            "status": 200,
            "cmdlet": {"CmdletName": "Get-MobileDeviceStatistics", "Parameters": {"Mailbox": "jsmithson"}},
            "body": {
                "value": [
                    {
                        "DeviceImei": "356938035643809",
                        "DevicePhoneNumber": "+44 7700 900111",
                        "RecoveryPassword": "1234-5678",
                        "DeviceModel": "iPhone 15",
                    }
                ]
            },
        },
        {
            "api": "exchange",
            "url": "https://outlook.office365.com/adminapi/beta/x/InvokeCommand",
            "status": 200,
            "cmdlet": {"CmdletName": "Get-MessageTraceV2", "Parameters": {}},
            "body": {
                "value": [
                    {
                        "Subject": "Salary review",
                        "RecipientAddress": "all@contoso.com",
                        "FromIP": "209.85.220.41",
                        "ToIP": "2a01:111:f403:c201::1",
                    }
                ]
            },
        },
        _graph(
            f"/v1.0/users/{JANE}/mailboxSettings/automaticRepliesSetting",
            {
                "status": "alwaysEnabled",
                "internalReplyMessage": "<p>Off sick until Friday</p>",
                "externalReplyMessage": "",
            },
        ),
    ]
    entries = scrub(log, TENANT, CLIENT)["entries"]
    device = entries[-3]["body"]["value"][0]
    assert device["DeviceImei"] == device["RecoveryPassword"] == "redacted"
    assert device["DevicePhoneNumber"].startswith("+1 555 01") and device["DeviceModel"] == "iPhone 15"
    trace = entries[-2]["body"]["value"][0]
    assert trace["Subject"] == "redacted"
    # The mail servers a message passed through say which providers the tenant uses.
    assert trace["FromIP"].startswith("203.0.113.") and trace["ToIP"].startswith("2001:db8::")
    reply = entries[-1]["body"]
    assert reply["internalReplyMessage"] == "redacted" and reply["externalReplyMessage"] == ""
