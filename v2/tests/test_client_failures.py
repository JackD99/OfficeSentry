"""The Exchange and SharePoint clients (``exchange.py``, ``sharepoint_api.py``) against Microsoft simulated with
``responses``: what each signs in for, that a failed sign-in never reaches the service, and what network
failures, Microsoft's waits and its throttling become."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
import responses

from officesentry import clouds, msauth
from officesentry.certs import generate_certificate
from officesentry.exchange import ExchangeClient, ExchangeError
from officesentry.graph import GraphClient
from officesentry.ratelimit import Limiter
from officesentry.sharepoint_api import SharePointAccessDenied, SharePointClient, SharePointError, beside
from tests.test_clouds import TENANT, _mock_sign_in

CLIENT_ID = "abababab-1111-2222-3333-555555555555"  # the read-only app
OTHER = "d0d0d0d0-1111-2222-3333-444444444444"  # another client's directory ID
LOGIN = "https://login.microsoftonline.com"
TOKEN = re.compile(re.escape(LOGIN) + r"/.*/token")
EXCHANGE = f"{clouds.get('commercial').exchange}/adminapi/beta/{TENANT}/InvokeCommand"
HOST = "https://contoso.sharepoint.com"
SITE = f"{HOST}/sites/a"
REFUSED = {"status": 400, "json": {"error": "invalid_client", "error_description": "AADSTS700016: not found"}}
UNREACHABLE = {"body": requests.ConnectionError("unreachable")}


@pytest.fixture(scope="module")
def cert():
    return generate_certificate("client failures test")


@pytest.fixture(autouse=True)
def _sign_in_afresh(monkeypatch):
    """Token providers are kept between runs; each test starts without one."""
    monkeypatch.setattr(msauth, "_PROVIDERS", type(msauth._PROVIDERS)())


def _hosts(calls) -> list[str]:
    return [urlsplit(c.request.url).netloc for c in calls]


def _exchange(tenant: str = TENANT, **kwargs) -> ExchangeClient:
    kwargs.setdefault("sleep", lambda s: None)
    return ExchangeClient(tenant, lambda: "tok", app=CLIENT_ID, **kwargs)


def _sharepoint(host: str = HOST, tenant: str = TENANT, **kwargs) -> SharePointClient:
    kwargs.setdefault("sleep", lambda s: None)
    return SharePointClient(host, lambda: "tok", app=CLIENT_ID, tenant=tenant, **kwargs)


def _held_clock():
    """A limiter on a clock that only moves when it holds a request, and the seconds it held each one."""
    clock, held = [0.0], []

    def sleep(seconds: float) -> None:
        held.append(seconds)
        clock[0] += seconds

    return Limiter(clock=lambda: clock[0], sleep=sleep), held


# -- Exchange ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["commercial", "gcc_high"])
def test_exchange_signs_in_once_for_its_clouds_exchange_host(cert, key):
    cloud = clouds.get(key)
    url = f"{cloud.exchange}/adminapi/beta/{TENANT}/InvokeCommand"
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        tokens = _mock_sign_in(rsps, cloud.login)
        rsps.post(url, json={"value": [{"Name": "a"}]})
        exo = ExchangeClient.from_certificate(TENANT, CLIENT_ID, cert.key_pem, cert.thumbprint, cloud=key)
        assert exo.invoke("Get-Mailbox") == exo.invoke("Get-Mailbox") == [{"Name": "a"}]
        sent = [c.request for c in rsps.calls if c.request.url == url]
        assert len(sent) == 2 and {r.headers["Authorization"] for r in sent} == {"Bearer tok"}
    assert len(tokens) == 1  # kept for the next call
    assert parse_qs(tokens[0].body)["scope"] == [f"{cloud.exchange}/.default"]


@pytest.mark.parametrize(
    ("answer", "status", "code"), [(REFUSED, 401, "invalid_client"), (UNREACHABLE, 0, "network_error")]
)
def test_a_failed_exchange_sign_in_never_reaches_exchange(cert, answer, status, code):
    """A refused sign-in fails the call; not reaching the sign-in service is a network failure like any other."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.post(TOKEN, **answer)  # answers before _mock_sign_in's
        _mock_sign_in(rsps, LOGIN)
        rsps.post(EXCHANGE, json={"value": []})
        exo = ExchangeClient.from_certificate(TENANT, CLIENT_ID, cert.key_pem, cert.thumbprint, sleep=lambda s: None)
        with pytest.raises(ExchangeError) as caught:
            exo.invoke("Get-Mailbox")
        assert "outlook.office365.com" not in _hosts(rsps.calls)
    assert (caught.value.status, caught.value.code) == (status, code)


def test_exchange_retries_a_network_failure_then_gives_up():
    slept = []
    exo = _exchange(sleep=slept.append, max_retries=2)
    with responses.RequestsMock() as rsps:
        rsps.post(EXCHANGE, body=requests.ConnectionError("reset"))
        rsps.post(EXCHANGE, body=requests.Timeout("slow"))
        rsps.post(EXCHANGE, json={"value": [{"Name": "a"}]})
        assert exo.invoke("Get-Mailbox") == [{"Name": "a"}]
    assert slept == [1.0, 2.0]
    stats = exo.telemetry.endpoints["Exchange Get-Mailbox"]
    assert (stats.calls, stats.errors, stats.retry_wait) == (3, 2, 3.0)
    slept.clear()
    with responses.RequestsMock() as rsps:
        rsps.post(EXCHANGE, body=requests.ConnectionError("down"))  # every time
        with pytest.raises(ExchangeError) as caught:
            exo.invoke("Get-Mailbox")
        assert len(rsps.calls) == 3
    assert (caught.value.status, caught.value.code) == (0, "network_error") and slept == [1.0, 2.0]


def test_exchange_keeps_microsofts_wait_up_to_five_minutes_then_gives_up():
    slept = []
    with responses.RequestsMock() as rsps:
        rsps.post(EXCHANGE, status=503, headers={"Retry-After": "900"})
        rsps.post(EXCHANGE, status=502, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        rsps.post(EXCHANGE, json={"value": []})
        assert _exchange(sleep=slept.append).invoke("Get-Mailbox") == []
    assert slept == [300.0, 2.0]  # capped; a date isn't seconds, so the usual backoff for a second retry
    with responses.RequestsMock() as rsps:
        rsps.post(EXCHANGE, status=503)
        with pytest.raises(ExchangeError) as caught:
            _exchange(max_retries=1).invoke("Get-Mailbox")
        assert len(rsps.calls) == 2
    assert caught.value.status == 503


def test_an_exchange_429_holds_that_client_in_every_thread_and_no_other_client():
    """Exchange's limit is per client tenant, so a 429 pauses that client's Exchange calls in every thread
    (through the shared limiter) and never another client's."""
    limiter, held = _held_clock()
    other, same = _exchange(OTHER, limiter=limiter), _exchange(limiter=limiter)
    seen = {}

    def meanwhile(seconds: float) -> None:  # while the throttled call waits, two other threads ask
        seen["other"] = (other.invoke("Get-Mailbox"), list(held))
        seen["same"] = (same.invoke("Get-Mailbox"), list(held))

    with responses.RequestsMock() as rsps:
        rsps.post(EXCHANGE, status=429, headers={"Retry-After": "7"})
        rsps.post(EXCHANGE, json={"value": []})
        rsps.post(EXCHANGE.replace(TENANT, OTHER), json={"value": []})
        _exchange(limiter=limiter, sleep=meanwhile).invoke("Get-Mailbox")
    assert seen["other"] == ([], [])  # not held
    assert seen["same"] == ([], [7.0])  # held until Microsoft's 7 seconds were up


# -- SharePoint --------------------------------------------------------------------------------------------


def test_sharepoint_signs_in_once_for_the_site_host_as_graphs_app(cert):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        tokens = _mock_sign_in(rsps, LOGIN)
        rsps.get(f"{SITE}/_api/web", json={"Title": "A"})
        graph = GraphClient.from_certificate(TENANT, CLIENT_ID, cert.key_pem, cert.thumbprint)
        client = beside(graph, HOST)
        assert client.get(SITE, "/web") == client.get(SITE, "/web") == {"Title": "A"}
        assert rsps.calls[-1].request.headers["Authorization"] == "Bearer tok"
    # One token, for this SharePoint host alone (Graph wasn't called, so it never signed in).
    assert [parse_qs(t.body)["scope"] for t in tokens] == [[f"{HOST}/.default"]]


def test_a_refused_sharepoint_sign_in_is_access_denied_and_never_reaches_sharepoint(cert):
    """The site permissions collector reads this as the client not having approved SharePoint yet."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.post(TOKEN, **REFUSED)  # answers before _mock_sign_in's
        _mock_sign_in(rsps, LOGIN)
        rsps.get(f"{SITE}/_api/web", json={})
        graph = GraphClient.from_certificate(TENANT, CLIENT_ID, cert.key_pem, cert.thumbprint, sleep=lambda s: None)
        with pytest.raises(SharePointAccessDenied) as caught:
            beside(graph, HOST).get(SITE, "/web")
        assert "contoso.sharepoint.com" not in _hosts(rsps.calls)
    assert (caught.value.status, caught.value.code) == (401, "invalid_client")


def test_sharepoint_retries_a_network_failure_then_gives_up():
    slept = []
    client = _sharepoint(sleep=slept.append, max_retries=2)
    with responses.RequestsMock() as rsps:
        rsps.get(f"{SITE}/_api/web", body=requests.ConnectionError("reset"))
        rsps.get(f"{SITE}/_api/web", body=requests.Timeout("slow"))
        rsps.get(f"{SITE}/_api/web", json={"Title": "A"})
        assert client.get(SITE, "/web") == {"Title": "A"}
    assert slept == [1.0, 2.0]
    stats = client.telemetry.endpoints["SharePoint /web"]
    assert (stats.calls, stats.errors, stats.retry_wait) == (3, 2, 3.0)
    slept.clear()
    with responses.RequestsMock() as rsps:
        rsps.get(f"{SITE}/_api/web", body=requests.ConnectionError("down"))  # every time
        with pytest.raises(SharePointError) as caught:
            client.get(SITE, "/web")
        assert len(rsps.calls) == 3
    assert (caught.value.status, caught.value.code) == (0, "network_error") and slept == [1.0, 2.0]


def test_sharepoint_keeps_microsofts_wait_up_to_five_minutes_then_gives_up():
    slept = []
    with responses.RequestsMock() as rsps:
        rsps.get(f"{SITE}/_api/web", status=503, headers={"Retry-After": " 900 "})
        rsps.get(f"{SITE}/_api/web", status=500, headers={"Retry-After": "soon"})
        rsps.get(f"{SITE}/_api/web", json={"Title": "A"})
        assert _sharepoint(sleep=slept.append).get(SITE, "/web") == {"Title": "A"}
    assert slept == [300.0, 2.0]  # capped; not a number of seconds, so the usual backoff for a second retry
    with responses.RequestsMock() as rsps:
        rsps.get(f"{SITE}/_api/web", status=503)
        with pytest.raises(SharePointError) as caught:
            _sharepoint(max_retries=1).get(SITE, "/web")
        assert len(rsps.calls) == 2
    assert caught.value.status == 503


@pytest.mark.parametrize("status", [429, 503])
def test_sharepoint_throttling_holds_that_client_in_every_thread_and_no_other_client(status):
    """SharePoint throttles with 429 or 503 per client tenant: either pauses that client's SharePoint calls in
    every thread (through the shared limiter) and never another client's."""
    limiter, held = _held_clock()
    fabrikam = "https://fabrikam.sharepoint.com"
    other, same = _sharepoint(fabrikam, OTHER, limiter=limiter), _sharepoint(limiter=limiter)
    seen = {}

    def meanwhile(seconds: float) -> None:  # while the throttled call waits, two other threads ask
        seen["other"] = (other.get(fabrikam, "/web"), list(held))
        seen["same"] = (same.get(SITE, "/web"), list(held))

    with responses.RequestsMock() as rsps:
        rsps.get(f"{SITE}/_api/web", status=status, headers={"Retry-After": "7"})
        rsps.get(f"{SITE}/_api/web", json={"Title": "A"})
        rsps.get(f"{fabrikam}/_api/web", json={"Title": "F"})
        _sharepoint(limiter=limiter, sleep=meanwhile).get(SITE, "/web")
    assert seen["other"] == ({"Title": "F"}, [])  # not held
    assert seen["same"] == ({"Title": "A"}, [7.0])  # held until Microsoft's 7 seconds were up


def test_sharepoint_paging_stops_at_its_cap_and_paths_start_with_a_slash():
    with responses.RequestsMock() as rsps:
        page = {"value": [{"Id": 1}], "odata.nextLink": f"{SITE}/_api/web/lists?$skiptoken=2"}
        rsps.get(f"{SITE}/_api/web/lists", json=page)
        result = _sharepoint().get_all(SITE, "/web/lists", max_pages=1)
        assert len(rsps.calls) == 1
    assert (result.items, result.pages, result.truncated) == ([{"Id": 1}], 1, True)
    with pytest.raises(ValueError, match="must start with"):
        _sharepoint().get(SITE, "web")
