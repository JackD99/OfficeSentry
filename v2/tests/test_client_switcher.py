"""The client switcher beside a client's name: the same page for another client, only ever for clients the
person sees (and the group they chose), and a list in place of a page about one client's own account."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from officesentry import store, tenant_groups
from officesentry.reports.file_activity import KEY as ADDON_REPORT
from tests.auditdata import addon_on, collect, iso, sample
from tests.conftest import login, make_user
from tests.seed import seed_tenant


@pytest.fixture
def three(db, connection_id):
    ids = [
        store.create_tenant(db, name, f"{name.split()[0].lower()}.onmicrosoft.com", connection_id)
        for name in ("Alpha Ltd", "Bravo Inc", "Charlie plc")
    ]
    db.execute("UPDATE tenants SET consented_at = '2026-01-01T00:00:00Z'")
    return ids


def _switch(page: str) -> dict[str, str]:
    """The switcher's links by client name, the current client's name ending in "*"."""
    if 'id="client-switch"' not in page:
        return {}
    menu = page[page.index('id="client-switch"') :]
    menu = menu[: menu.index("</details>")]
    links = re.findall(r'<li data-name="[^"]*"><a href="([^"]+)"( aria-current="page")?>([^<]+)</a>', menu)
    return {name + ("*" if current else ""): href for href, current, name in links}


def test_it_opens_the_same_page_for_another_client(client, db, three):
    alpha, bravo, charlie = three
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")

    assert _switch(client.get(f"/tenants/{alpha}").get_data(as_text=True)) == {
        "Alpha Ltd*": f"/tenants/{alpha}",
        "Bravo Inc": f"/tenants/{bravo}",
        "Charlie plc": f"/tenants/{charlie}",
    }
    for here, there in (
        ("findings", "findings"),
        ("findings/resolved", "findings/resolved"),
        ("data/mfa?sort=x", "data/mfa"),  # the view's filters are this client's: they stay behind
        ("evidence/cyber-essentials", "evidence/cyber-essentials"),
        ("users", "users"),
        ("users/u1", "users"),  # one client's own account: that client's list instead
        ("mailboxes", "mailboxes"),
        ("schedules", "schedules"),
        ("manage", "manage"),
        ("offboard", "manage"),
    ):
        links = _switch(client.get(f"/tenants/{alpha}/{here}").get_data(as_text=True))
        assert links["Bravo Inc"] == f"/tenants/{bravo}/{there}", here
        assert links["Alpha Ltd*"] == f"/tenants/{alpha}/{here.split('?')[0]}", here
        assert client.get(links["Bravo Inc"]).status_code == 200, there


def test_a_report_another_client_hasnt_got_opens_its_reports(client, db, vault, three):
    alpha, bravo, _ = three
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    addon_on(db, vault, [alpha], started_at=iso(now - timedelta(hours=3)))
    collect(db, alpha, sample(now), now)
    make_user(db, "admin")
    login(client, "admin")
    links = _switch(client.get(f"/tenants/{alpha}/data/{ADDON_REPORT}").get_data(as_text=True))
    assert links["Bravo Inc"] == f"/tenants/{bravo}/data"


def test_it_lists_only_clients_the_person_sees(client, db, three):
    alpha, bravo, _ = three
    make_user(db, "analyst", role="analyst", tenants=[alpha, bravo])
    login(client, "analyst")
    page = client.get(f"/tenants/{alpha}/findings").get_data(as_text=True)
    assert set(_switch(page)) == {"Alpha Ltd*", "Bravo Inc"}
    assert "Charlie plc" not in page

    make_user(db, "solo", role="analyst", tenants=[alpha])
    client.post("/logout")
    login(client, "solo")
    assert _switch(client.get(f"/tenants/{alpha}/findings").get_data(as_text=True)) == {}  # nowhere else to go


def test_it_follows_the_chosen_group(client, db, three):
    alpha, _, charlie = three
    gold = tenant_groups.create(db, {"name": "Gold", "category": "", "description": ""}, "test")
    tenant_groups.set_members(db, gold, [alpha, charlie])
    make_user(db, "admin")
    login(client, "admin")
    client.get(f"/?group={gold}")
    assert set(_switch(client.get(f"/tenants/{alpha}").get_data(as_text=True))) == {"Alpha Ltd*", "Charlie plc"}


def test_client_accounts_and_sample_headers_have_none(client, db, three):
    alpha, bravo, _ = three
    make_user(db, "admin")
    login(client, "admin")
    assert 'id="client-switch"' not in client.get("/styleguide").get_data(as_text=True)
    client.post("/logout")
    make_user(db, "ceo@alpha.example", role="client", tenants=[alpha, bravo])
    login(client, "ceo@alpha.example")
    page = client.get(f"/tenants/{alpha}/findings")
    assert page.status_code == 200 and 'id="client-switch"' not in page.get_data(as_text=True)


def test_slash_puts_the_cursor_in_the_search_box(client, db, three):
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/").get_data(as_text=True)
    assert 'aria-keyshortcuts="/ Control+K Meta+K"' in page
