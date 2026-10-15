"""The audit log add-on's feed client (``auditlog/feed.py``) and its sign-in check against Microsoft simulated
with ``responses``: where its token may go, the two calls that aren't reads, and what Microsoft's refusals,
throttling and outages become."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
import responses

from officesentry import connection_check
from officesentry.auditlog import feed
from officesentry.auditlog.feed import FeedAccessDenied, FeedClient, FeedError, FeedExpired, FeedNotStarted
from officesentry.certs import generate_certificate
from officesentry.ratelimit import Limiter
from tests.auditdata import CLIENT_ID, FEED, MSP, TENANT
from tests.test_clouds import _mock_sign_in

OTHER = "d0d0d0d0-1111-2222-3333-444444444444"  # another client's directory ID
ROOT = f"{FEED}/api/v1.0/{TENANT}/activity/feed"
LOGIN = "https://login.microsoftonline.com"
END = datetime(2026, 10, 15, 12, tzinfo=UTC)
REFUSED = {"status": 400, "json": {"error": "invalid_client", "error_description": "AADSTS700016: not found"}}
UNREACHABLE = {"body": requests.ConnectionError("unreachable")}


def _client(tenant: str = TENANT, **kwargs) -> FeedClient:
    kwargs.setdefault("sleep", lambda s: None)
    return FeedClient(tenant, lambda: "tok", publisher=MSP, app=CLIENT_ID, **kwargs)


def _hosts(calls) -> list[str]:
    return [urlsplit(c.request.url).netloc for c in calls]


@pytest.fixture(scope="module")
def cert():
    return generate_certificate("audit add-on test")


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example/api/v1.0/{tenant}/activity/feed/audit/1",
        "http://manage.office.com/api/v1.0/{tenant}/activity/feed/audit/1",
        "https://manage.office.com.evil.example/api/v1.0/{tenant}/activity/feed/audit/1",
        "https://manage.office.com@evil.example/api/v1.0/{tenant}/activity/feed/audit/1",
        "https://manage.office365.us/api/v1.0/{tenant}/activity/feed/audit/1",
        "https://manage.office.com/api/v1.0/{other}/activity/feed/audit/1",
        "https://manage.office.com/api/v1.0/{tenant}/activity/other/1",
    ],
)
def test_the_token_goes_only_to_this_clients_feed(uri):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.get(re.compile(r".*"), json=[])  # anything sent would be answered, and counted
        with pytest.raises(ValueError, match="refusing to send an audit feed token"):
            _client().blob(uri.format(tenant=TENANT, other=OTHER))
        assert not rsps.calls


def test_a_next_page_elsewhere_is_never_followed():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.get(f"{ROOT}/subscriptions/content", json=[], headers={"NextPageUri": "https://evil.example/next"})
        rsps.get("https://evil.example/next", json=[])
        with pytest.raises(ValueError, match="refusing"):
            _client().content("Audit.Exchange", END - timedelta(hours=1), END)
        assert _hosts(rsps.calls) == ["manage.office.com"]


def test_start_and_stop_only_send_the_addons_content_types():
    client = _client()
    with responses.RequestsMock() as rsps:
        rsps.post(f"{ROOT}/subscriptions/start", json={"contentType": "Audit.Exchange", "status": "enabled"})
        rsps.post(f"{ROOT}/subscriptions/stop", status=204)
        assert client.start("Audit.Exchange") == "enabled"
        client.stop("Audit.SharePoint")
        for content_type in ("Audit.General", "Audit.AzureActiveDirectory", "DLP.All", ""):
            with pytest.raises(ValueError, match="doesn't read"):
                client.start(content_type)
            with pytest.raises(ValueError, match="doesn't read"):
                client.stop(content_type)
        calls = list(rsps.calls)
    sent = [(c.request.method, urlsplit(c.request.url).path, parse_qs(urlsplit(c.request.url).query)) for c in calls]
    assert sent == [
        (
            "POST",
            f"/api/v1.0/{TENANT}/activity/feed/subscriptions/start",
            {"contentType": ["Audit.Exchange"], "PublisherIdentifier": [MSP]},
        ),
        (
            "POST",
            f"/api/v1.0/{TENANT}/activity/feed/subscriptions/stop",
            {"contentType": ["Audit.SharePoint"], "PublisherIdentifier": [MSP]},
        ),
    ]
    assert all(c.request.headers["Authorization"] == "Bearer tok" for c in calls)


def test_only_a_tenant_id_in_a_cloud_with_the_feed():
    with pytest.raises(ValueError, match="tenant ID, not a domain"):
        FeedClient("contoso.onmicrosoft.com", lambda: "tok")
    with pytest.raises(ValueError, match="no audit log feed"):
        FeedClient(TENANT, lambda: "tok", cloud="china")
    assert FeedClient(TENANT.upper(), lambda: "tok").tenant == TENANT
    assert FeedClient(TENANT, lambda: "tok", cloud="gcc_high").root.startswith("https://manage.office365.us/")
    assert FeedClient(TENANT, lambda: "tok", publisher="contoso.com").publisher is None  # only ever a tenant ID


@pytest.mark.parametrize(
    ("status", "body", "kind", "code"),
    [
        (400, {"error": {"code": "AF20022", "message": "No subscription found"}}, FeedNotStarted, "AF20022"),
        (400, {"error": {"code": "AF20023", "message": "The subscription was disabled"}}, FeedNotStarted, "AF20023"),
        (400, {"error": {"code": "AF20051", "message": "Content has already expired"}}, FeedExpired, "AF20051"),
        (
            400,
            {"error": {"code": "AF10001", "message": "The permission set doesn't include it"}},
            FeedAccessDenied,
            "AF10001",
        ),
        (401, {"error": {"code": "AF50000", "message": "Unauthorized"}}, FeedAccessDenied, "AF50000"),
        (403, "Forbidden", FeedAccessDenied, "403"),
        (404, "<html>Not Found</html>", FeedError, "404"),
    ],
)
def test_refusals_say_what_they_mean(status, body, kind, code):
    with responses.RequestsMock() as rsps:
        if isinstance(body, dict):
            rsps.get(f"{ROOT}/audit/1", status=status, json=body)
        else:
            rsps.get(f"{ROOT}/audit/1", status=status, body=body)
        with pytest.raises(FeedError) as caught:
            _client().blob(f"{ROOT}/audit/1")
        assert len(rsps.calls) == 1  # a refusal isn't retried
    assert type(caught.value) is kind and caught.value.status == status and caught.value.code == code


def test_an_answer_that_isnt_json_is_an_error_not_an_empty_list():
    with responses.RequestsMock() as rsps:
        rsps.get(f"{ROOT}/subscriptions/list", body="<html>Service unavailable</html>")
        with pytest.raises(FeedError) as caught:
            _client().subscriptions()
    assert caught.value.code == "bad_json"


def test_microsofts_wait_is_kept_and_capped():
    slept = []
    with responses.RequestsMock() as rsps:
        rsps.get(f"{ROOT}/subscriptions/list", status=429, headers={"Retry-After": "7"})
        rsps.get(f"{ROOT}/subscriptions/list", status=503, headers={"Retry-After": "900"})
        rsps.get(f"{ROOT}/subscriptions/list", status=500)
        rsps.get(f"{ROOT}/subscriptions/list", json=[{"contentType": "Audit.Exchange", "status": "Enabled"}])
        client = _client(sleep=slept.append)
        assert client.subscriptions() == {"Audit.Exchange": "enabled"}
    assert slept == [7.0, feed.MAX_RETRY_AFTER, 4.0]  # the third try waits 2**2 without a Retry-After
    assert client.telemetry.totals()["requests"] == 4 and client.telemetry.totals()["throttled"] == 1


def test_an_outage_is_retried_then_reported_as_a_network_error():
    slept = []
    with responses.RequestsMock() as rsps:
        rsps.get(f"{ROOT}/subscriptions/list", body=requests.ConnectionError("no route to host"))
        client = _client(sleep=slept.append, max_retries=2)
        with pytest.raises(FeedError) as caught:
            client.subscriptions()
        assert len(rsps.calls) == 3
    assert (caught.value.status, caught.value.code) == (0, "network_error")
    assert slept == [1.0, 2.0]


def test_a_429_holds_that_client_for_every_thread_and_no_other_client():
    """Microsoft's feed limit is per client tenant, so a 429 for one client pauses that client's requests in
    every thread (through the shared limiter) and never another client's."""
    clock, held = [0.0], []

    def limiter_sleep(seconds: float) -> None:
        held.append(seconds)
        clock[0] += seconds

    limiter = Limiter(clock=lambda: clock[0], sleep=limiter_sleep)
    other = _client(OTHER, limiter=limiter)
    same = _client(limiter=limiter)
    seen = {}

    def meanwhile(seconds: float) -> None:  # while the throttled request waits, two other threads ask
        seen["other"] = (other.subscriptions(), list(held))
        seen["same"] = (same.subscriptions(), list(held))

    with responses.RequestsMock() as rsps:
        rsps.get(f"{ROOT}/subscriptions/list", status=429, headers={"Retry-After": "7"})
        rsps.get(f"{ROOT}/subscriptions/list", json=[])
        rsps.get(f"{FEED}/api/v1.0/{OTHER}/activity/feed/subscriptions/list", json=[])
        _client(limiter=limiter, sleep=meanwhile).subscriptions()
    assert seen["other"] == ({}, [])  # not held
    assert seen["same"] == ({}, [7.0])  # held until Microsoft's 7 seconds were up


def _connection(cert) -> dict:
    return {"client_id": CLIENT_ID, "thumbprint": cert.thumbprint, "cloud": "commercial", "home_tenant": MSP}


def test_the_addon_signs_in_for_the_feed_only(cert):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        tokens = _mock_sign_in(rsps, LOGIN)
        rsps.get(f"{ROOT}/subscriptions/list", json=[])
        client = feed.client(TENANT, _connection(cert), cert.key_pem)
        assert client.subscriptions() == {} and client.publisher == MSP
        assert rsps.calls[-1].request.headers["Authorization"] == "Bearer tok"
    assert parse_qs(tokens[0].body)["scope"] == [f"{FEED}/.default"]


@pytest.mark.parametrize(
    ("answer", "kind", "status", "code"),
    [(REFUSED, FeedAccessDenied, 401, "invalid_client"), (UNREACHABLE, FeedError, 0, "network_error")],
)
def test_a_failed_sign_in_never_reaches_the_feed(cert, answer, kind, status, code):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.post(re.compile(re.escape(LOGIN) + r"/.*/token"), **answer)  # answers before _mock_sign_in's
        _mock_sign_in(rsps, LOGIN)
        rsps.get(f"{ROOT}/subscriptions/list", json=[])
        with pytest.raises(FeedError) as caught:
            feed.client(TENANT, _connection(cert), cert.key_pem).subscriptions()
        assert "manage.office.com" not in _hosts(rsps.calls)
    assert type(caught.value) is kind and (caught.value.status, caught.value.code) == (status, code)


@pytest.mark.parametrize(
    ("answer", "ok", "title"),
    [
        (None, True, "Signed in."),
        (REFUSED, False, "This tenant doesn't have the app yet."),
        (
            {"status": 400, "json": {"error": "invalid_request", "error_description": "something else"}},
            False,
            "The sign-in test failed.",
        ),
        (UNREACHABLE, False, "Office Sentry couldn't reach Microsoft."),
    ],
)
def test_the_addons_sign_in_check_only_signs_in(cert, answer, ok, title):
    """How ``addon.confirm`` learns a client approved the add-on: a token for the feed is the proof, and
    nothing is asked of the feed itself."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        if answer:
            rsps.post(re.compile(re.escape(LOGIN) + r"/.*/token"), **answer)
        tokens = _mock_sign_in(rsps, LOGIN)
        rsps.get(re.compile(re.escape(FEED) + r"/.*"), json=[])
        result = connection_check.sign_in_check(TENANT, CLIENT_ID, cert.key_pem, cert.thumbprint)
        assert set(_hosts(rsps.calls)) == {"login.microsoftonline.com"}
    assert (result.ok, result.title) == (ok, title)
    if ok:
        assert parse_qs(tokens[0].body)["scope"] == [f"{FEED}/.default"]


def test_no_sign_in_check_where_microsoft_has_no_feed(cert):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.POST, re.compile(r".*"), json={})
        rsps.add(responses.GET, re.compile(r".*"), json={})
        result = connection_check.sign_in_check(TENANT, CLIENT_ID, cert.key_pem, cert.thumbprint, cloud="china")
        assert not rsps.calls
    assert not result.ok and "isn't available" in result.title
