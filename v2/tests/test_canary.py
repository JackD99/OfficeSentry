"""The live canary (``live-test --canary``): GitHub OIDC sign-in, and shape drift against the recorded fixture.

The end-to-end tests replay ``tests/fixtures/live_tenant.json`` as if it were Microsoft: unchanged, the canary
passes; with a simulated change (a field gone, a type changed, a call refused, a new Sunset header) it fails
and its report says what changed, which is what the workflow puts in the GitHub issue.
"""

import copy
import json
import re
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.parse import parse_qs

import pytest
import responses

from officesentry import canary, livetest, msauth
from tests import replay
from tests.test_clouds import TENANT, _mock_sign_in
from tests.test_live_fixture import NEW_FIELDS, STAND_INS

OIDC_URL = "https://pipelines.actions.githubusercontent.example/token?api-version=2.0"
FIXTURE = replay.load()


def _graph(url: str, body, status: int = 200, headers: dict | None = None) -> dict:
    entry = {"api": "graph", "url": f"https://graph.microsoft.com{url}", "status": status, "body": body}
    if headers:
        entry["headers"] = headers
    return entry


def _users(*users) -> dict:
    return {"entries": [_graph("/v1.0/users?$select=id,displayName,accountEnabled", {"value": list(users)})]}


def test_shape_lists_every_field_with_its_types_and_keeps_kinds_of_object_apart():
    body = {
        "@odata.context": "x",
        "@odata.nextLink": "y",
        "value": [
            {"@odata.type": "#microsoft.graph.user", "id": "a", "tags": [], "size": 3},
            {"@odata.type": "#microsoft.graph.group", "id": "b", "mail": None},
            {"7c1e5f0a-3b9d-4e62-9a51-2f6c0d8e4b17": True},
        ],
    }
    found = canary.shape(body)
    assert found["value[]<user>.size"] == {"number"}
    assert found["value[]<group>.mail"] == {"null"}
    assert found["value[]<user>.tags"] == {"list"}
    assert found["value[].{id}"] == {"true/false"}  # an ID used as a key is any ID
    assert "@odata.context" not in found and "@odata.nextLink" not in found  # paging, not data
    assert found["value[]<user>.@odata.type"] == {"text"}


def test_the_same_shape_is_no_change_whatever_the_values():
    a = _users({"id": "1", "displayName": "Ann", "accountEnabled": True})
    b = _users(
        {"id": "2", "displayName": "Bob", "accountEnabled": False},
        {"id": "3", "displayName": None, "accountEnabled": True},
    )
    report = canary.compare(a, b)
    assert not report.failed and report.compared == 1
    assert "Nothing changed" in report.markdown()


def test_missing_fields_type_changes_and_refusals_are_changes():
    before = {
        "entries": [
            *_users({"id": "1", "displayName": "Ann", "accountEnabled": True})["entries"],
            _graph("/v1.0/domains", {"value": [{"id": "x.example", "isVerified": True}]}),
        ]
    }
    after = {
        "entries": [
            *_users({"id": "1", "accountEnabled": "yes", "jobTitle": "Nurse"})["entries"],
            _graph("/v1.0/domains", {"error": {"code": "Authorization_RequestDenied", "message": "no"}}, status=403),
        ]
    }
    report = canary.compare(before, after)
    users = "v1.0 /users ($select=accountEnabled,displayName,id)"
    assert report.failed
    assert report.changes[users] == [
        "`value[].accountEnabled` was true/false, now text",
        "`value[].displayName` no longer comes back",
    ]
    assert report.changes["v1.0 /domains"] == ["used to answer, now fails (403, Authorization_RequestDenied)"]
    assert report.info[users] == ["new fields: `value[].jobTitle`"]  # Microsoft adding a field breaks nothing


def test_an_empty_list_or_a_null_compares_with_anything():
    # No sign-ins tonight, or a field that happens to be null, isn't Microsoft changing anything.
    before = _users({"id": "1", "displayName": "Ann", "accountEnabled": True})
    assert not canary.compare(before, _users()).failed
    assert not canary.compare(before, _users({"id": "1", "displayName": None, "accountEnabled": None})).failed


def test_new_retirement_notices_fail_and_recorded_ones_are_listed():
    sunset = {"Deprecation": "Fri, 03 Sep 2021 23:59:59 GMT", "Sunset": "Sun, 01 Oct 2023 23:59:59 GMT"}
    path = "/v1.0/drives/b!abc/items/xyz/permissions"
    before = {"entries": [_graph(path, {"value": []})]}
    after = {"entries": [_graph(path, {"value": []}, headers=sunset)]}
    report = canary.compare(before, after)
    key = "v1.0 /drives/{drive}/items/{item}/permissions"
    assert report.failed and report.notices[key] == [
        f"Deprecation: {sunset['Deprecation']}",
        f"Sunset: {sunset['Sunset']}",
    ]
    # Re-recording keeps the headers, which acknowledges them.
    report = canary.compare(after, after)
    assert not report.failed
    assert report.info[key] == [
        f"known retirement notice, Deprecation: {sunset['Deprecation']}",
        f"known retirement notice, Sunset: {sunset['Sunset']}",
    ]


def test_calls_not_recorded_yet_and_calls_that_now_answer_are_for_information():
    refused = {"entries": [_graph("/v1.0/domains", {"error": {"code": "Forbidden"}}, status=403)]}
    answered = {
        "entries": [
            _graph("/v1.0/domains", {"value": []}),
            {
                "api": "exchange",
                "url": "https://outlook.office365.com/adminapi/beta/t/InvokeCommand",
                "status": 200,
                "body": {"value": []},
                "cmdlet": {"CmdletName": "Get-Mailbox"},
            },
            {"api": "dns", "name": "x.example", "rdtype": "MX", "answer": []},
        ]
    }
    report = canary.compare(refused, answered)
    assert not report.failed
    assert report.info == {
        "v1.0 /domains": ["used to fail, now answers"],
        "Exchange Get-Mailbox": ["not in the recording yet (re-record to cover it)"],
    }


def test_the_committed_fixture_matches_itself():
    report = canary.compare(FIXTURE, FIXTURE)
    assert not report.failed and report.compared > 50
    # Every call asks only for the fields it reads (``$select``), so Microsoft sends no retirement notices.
    assert not any(e.get("headers") for e in FIXTURE["entries"])
    assert not any("retirement notice" in line for lines in report.info.values() for line in lines)


def _simulated_change(fixture: dict) -> dict:
    """The fixture as Microsoft might answer one night: a field dropped, one retyped, a call refused,
    and a new retirement notice."""
    changed = copy.deepcopy(fixture)
    entries = changed["entries"]
    users = next(e for e in entries if e["api"] == "graph" and "/v1.0/users?" in e["url"] and e["body"]["value"])
    for user in users["body"]["value"]:
        user.pop("accountEnabled", None)
    skus = next(e for e in entries if e["api"] == "graph" and "/v1.0/subscribedSkus" in e["url"])
    for sku in skus["body"]["value"]:
        sku["consumedUnits"] = str(sku["consumedUnits"])
    domains = next(e for e in entries if e["api"] == "graph" and "/v1.0/domains?" in e["url"])
    domains.update(status=403, body={"error": {"code": "Authorization_RequestDenied", "message": "Insufficient"}})
    org = next(e for e in entries if e["api"] == "graph" and "/v1.0/organization?" in e["url"])
    org["headers"] = {"Sunset": "Tue, 30 Mar 2027 23:59:59 GMT"}
    return changed


def test_a_simulated_change_is_reported():
    report = canary.compare(FIXTURE, _simulated_change(FIXTURE))
    text = report.markdown()
    assert report.failed and "found something to look at" in text
    assert "`value[].accountEnabled` no longer comes back" in text
    assert "`value[].consumedUnits` was number, now text" in text
    assert "used to answer, now fails (403, Authorization_RequestDenied)" in text
    assert "Sunset: Tue, 30 Mar 2027 23:59:59 GMT" in text
    assert "### What to do" in text


def _run_canary(cfg, monkeypatch, served: dict, tmp_path) -> tuple[int, str]:
    """``live-test --canary`` against ``served`` replayed as Microsoft, signed in as GitHub Actions would."""
    for k in livetest.ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OFFICESENTRY_TEST_TENANT_ID", FIXTURE["tenant_id"])
    monkeypatch.setenv("OFFICESENTRY_TEST_CLIENT_ID", FIXTURE["client_id"])
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", OIDC_URL)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "runner-token")
    providers = []

    def federated(tenant_ref, client_id, assertion, resource, cloud):
        providers.append((client_id, assertion, resource))
        return lambda: "tok"

    monkeypatch.setattr(livetest, "federated_token_provider", federated)
    # The replayed responses are scrubbed already, and scrubbing placeholders again finds "leaks": the scrubber
    # has its own tests (test_recording.py), so here the recording is compared as it comes.
    monkeypatch.setattr(livetest.recording, "scrub", lambda log, tenant, client: {"entries": log})
    report = tmp_path / "canary.md"
    with (
        responses.RequestsMock(assert_all_requests_are_fired=False) as rsps,
        patch("officesentry.dnscheck.lookup", replay.fake_dns(served)),
        patch("officesentry.events.clock", lambda: datetime(2026, 10, 9, 22, 26, tzinfo=UTC)),
    ):
        # Calls added since the recording get the live replay's stand-ins; the canary lists them as not recorded.
        replay.mock(rsps, served, STAND_INS, NEW_FIELDS)
        code = livetest.run(cfg, canary=report)
    # No secret: both Graph and Exchange sign in with GitHub's OIDC token.
    assert {(c, a) for c, a, _ in providers} == {(FIXTURE["client_id"], livetest.github_oidc_token)}
    assert len(providers) == 2
    return code, report.read_text(encoding="utf-8")


def test_canary_run_passes_when_microsoft_answers_as_recorded(cfg, db, monkeypatch, tmp_path, capsys):
    code, report = _run_canary(cfg, monkeypatch, FIXTURE, tmp_path)
    assert code == 0, report
    assert "Nothing changed" in report
    out = capsys.readouterr().out
    assert "Checks:" not in out  # check results can name people, and CI logs may be public


def test_canary_run_fails_and_reports_a_simulated_change(cfg, db, monkeypatch, tmp_path):
    code, report = _run_canary(cfg, monkeypatch, _simulated_change(FIXTURE), tmp_path)
    assert code == 1
    assert "`value[].consumedUnits` was number, now text" in report
    assert "Sunset: Tue, 30 Mar 2027 23:59:59 GMT" in report
    assert FIXTURE["tenant_id"] not in report and "sentrytest" not in report  # shapes only, never values


def test_canary_stops_when_scrubbing_finds_something(cfg, db, monkeypatch, tmp_path):
    def refuse(log, tenant, client):
        raise ValueError("scrubbing left 1 identifying value in the fixture: A… (5 chars)")

    monkeypatch.setattr(livetest.recording, "scrub", refuse)
    for k in livetest.ENV:
        monkeypatch.setenv(k, "tid" if k.endswith("TENANT_ID") else "x")
    monkeypatch.setattr(livetest, "secret_token_provider", lambda *a: lambda: "tok")
    with (
        responses.RequestsMock(assert_all_requests_are_fired=False) as rsps,
        patch("officesentry.dnscheck.lookup", replay.fake_dns(FIXTURE)),
    ):
        replay.mock(rsps, FIXTURE)
        assert livetest.run(cfg, ["tenant_profile"], canary=tmp_path / "r.md") == 1
    assert "couldn't check the responses" in (tmp_path / "r.md").read_text(encoding="utf-8")


def test_canary_needs_the_tenant_and_app(cfg, db, monkeypatch, tmp_path):
    for k in (*livetest.ENV, *livetest.GITHUB_OIDC):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", OIDC_URL)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "runner-token")
    # With GitHub's OIDC token there's no secret to ask for, but the tenant and app IDs are still needed.
    assert livetest.run(cfg, canary=tmp_path / "r.md") == 2


@responses.activate
def test_github_oidc_token_is_asked_for_with_entras_audience(monkeypatch):
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", OIDC_URL)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "runner-token")
    issuer = re.compile(re.escape(OIDC_URL.split("?")[0]) + r"\?.*")
    responses.get(issuer, json={"value": "github-jwt"})
    assert livetest.github_oidc_token() == "github-jwt"
    request = responses.calls[0].request
    assert parse_qs(request.url.split("?", 1)[1])["audience"] == ["api://AzureADTokenExchange"]
    assert request.headers["Authorization"] == "bearer runner-token"
    responses.replace(responses.GET, issuer, status=403)
    with pytest.raises(msauth.TokenError, match="GitHub didn't issue an OIDC token"):
        livetest.github_oidc_token()


def test_federated_sign_in_sends_the_assertion_instead_of_a_secret():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        tokens = _mock_sign_in(rsps, "https://login.microsoftonline.com")
        provider = msauth.federated_token_provider(
            TENANT, "app-id", lambda: "github-jwt", "https://graph.microsoft.com"
        )
        assert provider() == "tok"
    body = parse_qs(tokens[0].body)
    assert body["client_assertion"] == ["github-jwt"]
    assert body["client_assertion_type"] == ["urn:ietf:params:oauth:client-assertion-type:jwt-bearer"]
    assert "client_secret" not in body


def test_federated_sign_in_failure_is_a_token_error():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        # Registered first, so it answers before _mock_sign_in's token endpoint: Entra refusing the assertion.
        rsps.post(
            f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
            status=400,
            json={"error": "invalid_client", "error_description": "AADSTS70021: no matching federated identity"},
        )
        _mock_sign_in(rsps, "https://login.microsoftonline.com")
        provider = msauth.federated_token_provider(
            TENANT, "app-id", lambda: "github-jwt", "https://graph.microsoft.com"
        )
        with pytest.raises(msauth.TokenError, match="invalid_client"):
            provider()


def test_recording_keeps_retirement_headers(monkeypatch):
    from officesentry import recording

    log = []
    with responses.RequestsMock() as rsps:
        rsps.get(
            "https://graph.microsoft.com/beta/auditLogs/signIns",
            json={"value": []},
            headers={"Deprecation": "Sat, 31 Aug 2024 23:59:59 GMT", "Sunset": "Mon, 31 Aug 2026 23:59:59 GMT"},
        )
        rsps.get("https://graph.microsoft.com/v1.0/users", json={"value": []})
        session = recording.RecordingSession(log)
        session.get("https://graph.microsoft.com/beta/auditLogs/signIns")
        session.get("https://graph.microsoft.com/v1.0/users")
    fixture = recording.scrub(log, TENANT, "11111111-2222-3333-4444-555555555555")
    assert fixture["entries"][0]["headers"] == {
        "Deprecation": "Sat, 31 Aug 2024 23:59:59 GMT",
        "Sunset": "Mon, 31 Aug 2026 23:59:59 GMT",
    }
    assert "headers" not in fixture["entries"][1]
    assert json.dumps(fixture)  # still plain JSON


def test_failed_collectors_fail_the_canary_and_say_what_to_check():
    report = canary.compare(FIXTURE, FIXTURE, ["Users (`users`)"])
    assert report.failed
    text = report.markdown()
    assert "### Collectors that failed" in text and "- Users (`users`)" in text
    assert "federated credential" in text
