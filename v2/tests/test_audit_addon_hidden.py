"""The audit log add-on's report shows only where the add-on is: for a client it was asked for, while it's on
for the install. Report designs neither offer nor show it otherwise, month-end plans don't attach it for a
client never asked, and what a client's own sign-in reads of it names nobody on the team.

Every Microsoft call is simulated with ``responses``.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from officesentry import delivery, store
from officesentry.auditlog import addon
from officesentry.reports import designs
from officesentry.reports.file_activity import KEY
from tests.auditdata import addon_on, collect, iso, sample
from tests.conftest import login, make_user

RECORD = "An admin opened someone else"  # from the sample's Exchange records


def _collected(db, vault, alpha) -> datetime:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    addon_on(db, vault, [alpha], started_at=iso(now - timedelta(hours=3)))
    assert collect(db, alpha, sample(now), now)[0] == "succeeded"
    return now


def _link_state(client, tenant_id: int) -> str:
    page = client.get(f"/tenants/{tenant_id}/audit-addon").get_data(as_text=True)
    return parse_qs(urlsplit(html.unescape(re.search(r'data-copy-text="([^"]+)"', page).group(1))).query)["state"][0]


# -- Report designs -------------------------------------------------------------------------------------------


def test_the_design_editor_offers_the_addons_report_only_while_the_addon_is_on(client, db, vault):
    make_user(db, "admin")
    login(client, "admin")
    design_id = designs.create(db, "Monthly pack", "admin", {"settings": {}, "blocks": []})
    assert "Files and mailbox access" not in client.get(f"/reporting/designs/{design_id}").get_data(as_text=True)
    addon_on(db, vault)
    assert "Files and mailbox access" in client.get(f"/reporting/designs/{design_id}").get_data(as_text=True)


def test_a_design_leaves_out_the_addons_records_once_its_switched_off(client, db, vault, two_tenants):
    alpha, _ = two_tenants
    _collected(db, vault, alpha)
    make_user(db, "admin")
    login(client, "admin")
    block = {"kind": "data_table", "width": "full", "settings": {"report": KEY, "max_rows": 50, "max_tables": 2}}
    design_id = designs.create(db, "Monthly pack", "admin", {"settings": {}, "blocks": [block]})
    assert RECORD in html.unescape(client.get(f"/tenants/{alpha}/designs/{design_id}").get_data(as_text=True))

    addon.switch_off(db, store.get_tenant(db, alpha), "admin")  # keeps what was collected
    addon.switch(db, False, "admin")
    assert RECORD not in html.unescape(client.get(f"/tenants/{alpha}/designs/{design_id}").get_data(as_text=True))
    preview = client.get(f"/reporting/designs/{design_id}/preview", query_string={"tenant": alpha})
    page = html.unescape(preview.get_data(as_text=True))
    assert RECORD not in page and "The audit log add-on isn't on for this client." in page  # the editor says why


# -- Clients the add-on was never asked for --------------------------------------------------------------------


def test_a_month_end_plan_leaves_the_addons_report_out_for_a_client_never_asked(db, vault, two_tenants):
    alpha, bravo = two_tenants
    addon_on(db, vault, [alpha])
    keys = ["review", f"data:{KEY}"]
    assert f"data:{KEY}" in [k for k, _ in delivery.attachment_plan(store.get_tenant(db, alpha), keys, False, db)]
    assert [k for k, _ in delivery.attachment_plan(store.get_tenant(db, bravo), keys, False, db)] == ["review"]


def test_taking_the_request_back_takes_the_report_away(app, client, db, vault, two_tenants):
    alpha, bravo = two_tenants
    addon_on(db, vault, [alpha])
    make_user(db, "admin")
    make_user(db, "it@bravo", role="client", tenants=[bravo])
    login(client, "admin")
    client.post(f"/tenants/{bravo}/audit-addon/sent", data={"sent": "1"})
    assert client.get(f"/tenants/{bravo}/data/{KEY}").status_code == 200
    client.post(f"/tenants/{bravo}/audit-addon/sent", data={"sent": "0"})  # not asked after all
    assert addon.state(addon.row_for(db, bravo)).key == "not_sent"

    viewer = app.test_client()
    login(viewer, "it@bravo")
    assert viewer.get(f"/tenants/{bravo}/data/{KEY}").status_code == 404
    assert client.get(f"/tenants/{bravo}/data/{KEY}").status_code == 404


def test_coming_back_from_the_link_unasked_doesnt_give_a_client_the_report(app, client, db, vault, two_tenants):
    alpha, bravo = two_tenants
    addon_on(db, vault, [alpha])
    make_user(db, "admin")
    login(client, "admin")
    state = _link_state(client, bravo)  # the link is on the page whether or not it was sent
    app.test_client().get("/consent/callback", query_string={"admin_consent": "True", "state": state})
    assert addon.row_for(db, bravo)["approved_at"]
    assert client.get(f"/tenants/{bravo}/data/{KEY}").status_code == 404


# -- What a client's own sign-in reads -------------------------------------------------------------------------


def test_a_clients_sign_in_reads_nothing_meant_for_the_team(app, client, db, vault, two_tenants):
    alpha, bravo = two_tenants
    _collected(db, vault, alpha)
    make_user(db, "admin")
    make_user(db, "it@alpha", role="client", tenants=[alpha])
    make_user(db, "it@bravo", role="client", tenants=[bravo])
    login(client, "admin")
    client.post(f"/tenants/{bravo}/audit-addon/sent", data={"sent": "1"})
    client.post(f"/tenants/{alpha}/audit-addon/off")

    def read(username: str, tenant_id: int) -> str:
        viewer = app.test_client()
        login(viewer, username)
        resp = viewer.get(f"/tenants/{tenant_id}/data/{KEY}")
        assert resp.status_code == 200, username
        return html.unescape(resp.get_data(as_text=True))

    waiting = read("it@bravo", bravo)
    assert "The audit log add-on isn't on for this client yet." in waiting
    assert "approval link" not in waiting and "Send the client" not in waiting
    off = read("it@alpha", alpha)
    assert "The audit log add-on is off for this client." in off and "Switched off by" not in off
    assert "Switched off by admin." in client.get(f"/tenants/{alpha}/audit-addon").get_data(as_text=True)
