"""Approving Office Sentry for several clients yourself, one after another: each consent link comes back to the
Approve page, which goes on to the next client (Microsoft's answers simulated by calling the callback)."""

import html
import re
from urllib.parse import parse_qs, urlparse

import pytest

from officesentry import permissions as p
from officesentry import store, tenant_health
from tests.conftest import login, make_user
from tests.test_limited_roles import _limited

ALPHA = "aaaaaaaa-0000-4000-8000-000000000001"
BRAVO = "aaaaaaaa-0000-4000-8000-000000000002"
CHARLIE = "aaaaaaaa-0000-4000-8000-000000000003"


@pytest.fixture
def admin(client, db):
    make_user(db, "admin")
    login(client, "admin")
    return client


@pytest.fixture
def three(db, connection_id):
    return [
        store.create_tenant(db, name, ref, connection_id)
        for name, ref in (("Alpha Ltd", ALPHA), ("Bravo Inc", BRAVO), ("Charlie plc", CHARLIE))
    ]


def _links(page: str) -> dict[str, str]:
    """Tenant ID -> the consent link on the page for it."""
    hrefs = re.findall(r'href="(https://login\.microsoftonline\.com/[^"]+)"', page)
    return {urlparse(h).path.split("/")[1]: html.unescape(h) for h in hrefs}


def _answer(client, link: str, tenant: str, approved: bool = True):
    """Microsoft sending the admin back from the consent page."""
    state = parse_qs(urlparse(link).query)["state"][0]
    answer = {"admin_consent": "True", "tenant": tenant} if approved else {"error": "access_denied"}
    return client.get("/consent/callback", query_string={**answer, "state": state})


def test_approving_one_client_after_another(admin, db, three):
    run = ",".join(map(str, three))
    page = admin.get(f"/admin/onboarding/approve?t={run}").get_data(as_text=True)
    assert "0 of 3 clients approved" in page and "Next: Alpha Ltd" in page and 'data-go-on="' not in page
    assert set(_links(page)) == {ALPHA, BRAVO, CHARLIE}

    # Alpha approves: back to this page, which opens Bravo after a pause.
    resp = _answer(admin, _links(page)[ALPHA], ALPHA)
    assert resp.status_code == 302 and "/admin/onboarding/approve?" in resp.headers["Location"]
    assert parse_qs(urlparse(resp.headers["Location"]).query) == {"t": [run], "after": [str(three[0])], "done": ["1"]}
    page = admin.get(resp.headers["Location"]).get_data(as_text=True)
    assert store.get_tenant(db, three[0])["consented_at"]
    assert "Approved for <strong>Alpha Ltd</strong>" in page and "Next: Bravo Inc" in page
    go_on = html.unescape(re.search(r'data-go-on="([^"]+)"', page)[1])
    assert urlparse(go_on).path == f"/{BRAVO}/adminconsent"
    assert set(_links(page)) == {BRAVO, CHARLIE}  # Alpha needs nothing more

    # Bravo's admin says no: the page says why, offers Charlie, and doesn't open it by itself.
    resp = _answer(admin, go_on, BRAVO, approved=False)
    page = admin.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Consent was not granted" in page and "Next: Charlie plc" in page and 'data-go-on="' not in page
    assert not store.get_tenant(db, three[1])["consented_at"]

    # Charlie approves: Bravo is offered again, but not opened by itself.
    page = admin.get(_answer(admin, _links(page)[CHARLIE], CHARLIE).headers["Location"]).get_data(as_text=True)
    assert "Approved for <strong>Charlie plc</strong>" in page and "Next: Bravo Inc" in page
    assert 'data-go-on="' not in page and "2 of 3 clients approved" in page
    assert "Skip this one" not in page  # nothing else to skip to


def test_skip_moves_on_and_a_link_from_the_clients_page_still_goes_back_there(admin, db, three):
    run = ",".join(map(str, three))
    page = admin.get(f"/admin/onboarding/approve?t={run}&after={three[0]}").get_data(as_text=True)
    assert "Next: Bravo Inc" in page  # after Alpha
    page = admin.get(f"/admin/onboarding/approve?t={run}&after={three[2]}").get_data(as_text=True)
    assert "Next: Alpha Ltd" in page  # after the last one, from the top

    page = admin.get(f"/tenants/{three[0]}/manage").get_data(as_text=True)
    resp = _answer(admin, _links(page)[ALPHA], ALPHA)
    assert resp.headers["Location"].endswith(f"/tenants/{three[0]}")


def test_the_tracker_sends_ticked_clients_to_the_approve_page(admin, db, three):
    resp = admin.post("/admin/onboarding/bulk", data={"action": "approve", "tenant": [str(three[2]), str(three[0])]})
    assert parse_qs(urlparse(resp.headers["Location"]).query)["t"] == [f"{three[0]},{three[2]}"]
    page = admin.get("/admin/onboarding").get_data(as_text=True)
    assert "Approve them myself" in page


def test_without_a_list_it_offers_every_client_with_something_to_approve(admin, db, three):
    db.execute("UPDATE tenants SET consented_at = '2026-10-01T00:00:00Z' WHERE id = ?", (three[1],))
    page = admin.get("/admin/onboarding/approve").get_data(as_text=True)
    assert "Alpha Ltd" in page and "Charlie plc" in page and "Bravo Inc" not in page
    db.execute("UPDATE tenants SET consented_at = '2026-10-01T00:00:00Z'")
    page = admin.get("/admin/onboarding/approve").get_data(as_text=True)
    assert "Every client has approved Office Sentry" in page


def test_check_again_signs_in_to_each_client(admin, db, three):
    db.execute("UPDATE tenants SET consented_at = '2026-10-01T00:00:00Z' WHERE id = ?", (three[0],))
    resp = admin.post("/admin/onboarding/approve/check", data={"t": ",".join(map(str, three))})
    assert resp.status_code == 302
    runs = {(r["tenant_id"], r["collector"]) for r in db.execute("SELECT tenant_id, collector FROM runs")}
    assert runs == {(three[0], "connection")}  # its permissions and Global Reader
    checks = {r["tenant_id"] for r in db.execute("SELECT tenant_id FROM tasks WHERE kind = 'consent_check'")}
    assert checks == {three[1], three[2]}  # the other two: whether they've approved


def test_global_reader_gets_a_link_to_the_clients_own_admin_centre(admin, db, three, monkeypatch):
    db.execute("UPDATE tenants SET consented_at = '2026-10-01T00:00:00Z' WHERE id = ?", (three[0],))
    monkeypatch.setattr(
        tenant_health,
        "connection_result",
        lambda conn, tid: (
            {"roles_checked": True, "global_reader": False, "app_name": "Contoso MSP reporting"}
            if tid == three[0]
            else None
        ),
    )
    page = admin.get(f"/admin/onboarding/approve?t={three[0]}").get_data(as_text=True)
    assert f'href="https://entra.microsoft.com/{ALPHA}"' in page
    assert "Missing" in page and "Add assignments</em> → Contoso MSP reporting" in page
    assert "Every client here has approved Office Sentry" in page


def test_only_clients_the_person_sees_are_on_the_page(client, db, three):
    _limited(db, "rm", [*p.SEE, "clients.manage"], tenants=[three[0]])
    login(client, "rm")
    page = client.get(f"/admin/onboarding/approve?t={three[1]},{three[0]}").get_data(as_text=True)
    assert "Alpha Ltd" in page and "Bravo Inc" not in page and BRAVO not in page
    resp = client.get(f"/admin/onboarding/approve?t={three[1]}")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/admin/onboarding")
    client.post("/admin/onboarding/approve/check", data={"t": f"{three[1]},{three[2]}"})
    assert not db.execute("SELECT 1 FROM tasks").fetchone()


def test_without_clients_manage_there_is_no_approve_page(client, db, three):
    make_user(db, "viewer", role="analyst")
    login(client, "viewer")
    assert client.get(f"/admin/onboarding/approve?t={three[0]}").status_code in (302, 403)
    assert client.post("/admin/onboarding/approve/check", data={"t": str(three[0])}).status_code in (302, 403)
    assert not db.execute("SELECT 1 FROM tasks").fetchone()
