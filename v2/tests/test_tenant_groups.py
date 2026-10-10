"""Tenant groups: managing them, the group picker on staff pages, digests by group, and access by group.

Isolation across every route (a group never shows anyone a tenant they weren't granted) is walked in
``test_isolation_walk.py``; this file covers what groups do.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

import pytest

from officesentry import delivery, lifecycle, month_end, store, tenant_groups
from officesentry.web import access
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_email import _configure, fake_smtp, sends  # noqa: F401 (autouse fixture)

NOW = datetime.now(UTC)
PERIOD = f"{NOW.year}-{NOW.month:02d}"


@pytest.fixture
def three(db, two_tenants, connection_id):
    """Alpha and Charlie in Gold (a Tier), Bravo in no group."""
    alpha, bravo = two_tenants
    charlie = store.create_tenant(db, "Charlie Co", "charlie.onmicrosoft.com", connection_id)
    db.execute("UPDATE tenants SET consented_at = '2026-01-01T00:00:00Z'")
    gold = tenant_groups.create(db, {"name": "Gold", "category": "Tier", "description": ""}, "test")
    tenant_groups.set_members(db, gold, [alpha, charlie])
    return alpha, bravo, charlie, gold


def _admin(client, db):
    make_user(db, "admin")
    login(client, "admin")


def _text(resp) -> str:
    return resp.get_data(as_text=True)


# -- Managing groups ----------------------------------------------------------------------------


def test_admin_creates_fills_renames_and_deletes_a_group(client, db, two_tenants):
    alpha, bravo = two_tenants
    _admin(client, db)
    assert "No groups yet" in _text(client.get("/admin/groups"))
    resp = client.post("/admin/groups", data={"name": "  North   region ", "category": "Region"})
    group = db.execute("SELECT * FROM tenant_groups").fetchone()
    assert group["name"] == "North region" and resp.headers["Location"].endswith(f"/admin/groups/{group['id']}")
    # The same name again, in any case, is refused.
    client.post("/admin/groups", data={"name": "north REGION"})
    assert db.execute("SELECT COUNT(*) FROM tenant_groups").fetchone()[0] == 1
    assert "Alpha Ltd" in _text(client.get(f"/admin/groups/{group['id']}"))

    client.post(
        f"/admin/groups/{group['id']}",
        data={"name": "North", "category": "Region", "description": "Leeds", "tenants": [str(alpha), "99999"]},
    )
    assert tenant_groups.members(db, group["id"]) == {alpha}
    page = _text(client.get("/admin/groups"))
    assert "North" in page and "Region" in page and "Leeds" in page
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'tenant_group_%'").fetchone()[0] == 2

    # A digest on the group covers every tenant once the group is gone; the tenants stay.
    db.execute(
        "INSERT INTO email_schedules (kind, reports, frequency, hour, group_id, created_at)"
        " VALUES ('digest', '[]', 'weekly', 8, ?, '2026-01-01T00:00:00Z')",
        (group["id"],),
    )
    resp = client.post(f"/admin/groups/{group['id']}/delete", follow_redirects=True)
    assert "1 digest now covers every tenant" in _text(resp)
    assert db.execute("SELECT group_id FROM email_schedules").fetchone()[0] is None
    assert db.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM tenant_group_members").fetchone()[0] == 0


def test_only_admins_manage_groups(client, db, three):
    alpha, _, _, gold = three
    make_user(db, "ann", role="analyst", all_tenants=True)
    login(client, "ann")
    assert client.get("/admin/groups").status_code == 403
    assert client.post("/admin/groups", data={"name": "X"}).status_code == 403
    assert client.post(f"/admin/groups/{gold}/delete").status_code == 403
    assert client.post(f"/tenants/{alpha}/groups", data={"groups": []}).status_code == 403
    # Analysts see a tenant's groups on its Manage page, without the form.
    page = _text(client.get(f"/tenants/{alpha}/manage"))
    assert "Gold" in page and "Save groups" not in page


def test_tenant_groups_from_manage_and_new_tenant(client, db, three, connection_id):
    alpha, bravo, _, gold = three
    silver = tenant_groups.create(db, {"name": "Silver", "category": "Tier", "description": ""}, "test")
    _admin(client, db)
    assert "Save groups" in _text(client.get(f"/tenants/{bravo}/manage"))
    client.post(f"/tenants/{bravo}/groups", data={"groups": [str(gold), str(silver), "424242"]})
    assert {g["name"] for g in tenant_groups.of_tenants(db, [bravo])[bravo]} == {"Gold", "Silver"}
    client.post(f"/tenants/{bravo}/groups", data={})
    assert tenant_groups.of_tenants(db, [bravo])[bravo] == []

    assert "Silver" in _text(client.get("/tenants/new"))
    client.post(
        "/tenants/new",
        data={
            "name": "Delta",
            "tenant_ref": "delta.onmicrosoft.com",
            "connection_id": str(connection_id),
            "groups": [str(silver)],
        },
    )
    delta = db.execute("SELECT id FROM tenants WHERE name = 'Delta'").fetchone()[0]
    assert tenant_groups.members(db, silver) == {delta}


def test_deleting_a_tenant_takes_it_out_of_its_groups(db, three):
    alpha, _, _, gold = three
    assert (
        lifecycle.delete_tenant(db, db.execute("SELECT * FROM tenants WHERE id = ?", (alpha,)).fetchone(), "admin")
        is not None
    )
    assert alpha not in tenant_groups.members(db, gold)


# -- The group picker -----------------------------------------------------------------------------


def test_picking_a_group_narrows_the_dashboard_until_changed(client, db, three):
    alpha, bravo, charlie, gold = three
    _admin(client, db)
    page = _text(client.get("/"))
    assert all(n in page for n in ("Alpha Ltd", "Bravo Inc", "Charlie Co"))
    assert 'id="group-pick"' in page and "Gold (2)" in page and "All tenants (3)" in page

    resp = client.get(f"/?group={gold}")
    assert resp.status_code == 302 and resp.headers["Location"] == "/"
    page = _text(client.get("/"))
    assert "Alpha Ltd" in page and "Charlie Co" in page and "Bravo Inc" not in page
    assert "the 2 tenants in Gold" in page and 'class="group-picker on"' in page

    # The choice follows to the other pages, and to their downloads.
    assert "Bravo Inc" not in _text(client.get("/across/review"))
    assert "Bravo Inc" not in _text(client.get("/changes"))
    assert "Bravo Inc" not in _text(client.get("/admin/tenants"))
    rows = list(csv.reader(io.StringIO(client.get("/across/review.csv").get_data(as_text=True).lstrip("﻿"))))
    assert {r[0] for r in rows[1:]} >= {"Alpha Ltd", "Charlie Co"} and "Bravo Inc" not in {r[0] for r in rows}

    # Other arguments survive picking; ?group=all (or anything not offered) shows every tenant again.
    assert client.get(f"/changes?days=7&group={gold}").headers["Location"] == "/changes?days=7"
    client.get("/?group=all")
    assert "Bravo Inc" in _text(client.get("/"))
    client.get(f"/?group={gold}")
    client.get("/?group=999")
    assert "Bravo Inc" in _text(client.get("/"))


def test_the_choice_lasts_for_the_sign_in_only(client, db, three):
    *_, gold = three
    _admin(client, db)
    client.get(f"/?group={gold}")
    client.post("/logout")
    login(client, "admin")
    assert "Bravo Inc" in _text(client.get("/"))


def test_no_picker_without_groups_or_for_clients(client, db, two_tenants):
    alpha, _ = two_tenants
    _admin(client, db)
    assert 'id="group-pick"' not in _text(client.get("/"))
    gold = tenant_groups.create(db, {"name": "Gold", "category": "", "description": ""}, "test")
    tenant_groups.set_members(db, gold, [alpha])
    make_user(db, "cli", role="client", tenants=[alpha])
    client.post("/logout")
    login(client, "cli")
    page = _text(client.get(f"/?group={gold}", follow_redirects=True))
    assert 'id="group-pick"' not in page and "Gold" not in page


def test_analysts_are_offered_only_groups_holding_their_tenants(client, db, three):
    alpha, bravo, _, gold = three
    other = tenant_groups.create(db, {"name": "Bravo only", "category": "", "description": ""}, "test")
    tenant_groups.set_members(db, other, [bravo])
    make_user(db, "ann", role="analyst", tenants=[alpha])
    login(client, "ann")
    page = _text(client.get("/"))
    assert "Gold (1)" in page and "Bravo only" not in page  # counts only what she sees
    client.get(f"/?group={other}")  # not hers to pick: shows her tenants, never Bravo
    page = _text(client.get("/"))
    assert "Alpha Ltd" in page and "Bravo Inc" not in page and 'class="group-picker on"' not in page


def test_check_matrix_and_every_row_follow_the_group(client, db, three):
    alpha, bravo, charlie, gold = three
    for t in (alpha, bravo, charlie):
        seed_tenant(db, t)
    _admin(client, db)
    client.get(f"/?group={gold}")
    assert "Bravo Inc" not in _text(client.get("/across/checks"))
    page = _text(client.get("/across/mailboxes/rows", follow_redirects=True))
    assert "Alpha Ltd" in page and "Bravo Inc" not in page
    xlsx = client.get("/across/review.xlsx")
    assert xlsx.status_code == 200


def test_month_end_queue_follows_the_group(client, db, three):
    alpha, bravo, charlie, gold = three
    _admin(client, db)
    client.post(
        "/reporting/plans/new",
        data={
            "name": "Monthly",
            "reports": ["review"],
            "day_of_month": "1",
            "hour": "8",
            "timezone": "UTC",
            "subject": "{client}",
            "mode": "approval",
            "delivery": "email",
            "tenants": [str(alpha), str(bravo)],
        },
    )
    assert month_end.plan_for(db, alpha)[0] is not None
    client.get(f"/?group={gold}")
    page = _text(client.get(f"/reporting/{PERIOD}"))
    assert "Alpha Ltd" in page and "Bravo Inc" not in page and "Clients in Gold" in page
    lonely = tenant_groups.create(db, {"name": "Lonely", "category": "", "description": ""}, "test")
    tenant_groups.set_members(db, lonely, [charlie])  # Charlie isn't on a plan
    client.get(f"/?group={lonely}")
    assert "No clients in Lonely are on a report plan" in _text(client.get(f"/reporting/{PERIOD}"))


# -- Digests ----------------------------------------------------------------------------------------


def test_a_digest_can_cover_one_group(client, db, three):
    alpha, bravo, charlie, gold = three
    for t in (alpha, bravo):
        seed_tenant(db, t)
    data = delivery.digest_data(db, "weekly", NOW, gold)
    assert {r["tenant"]["name"] for r in data["rows"]} == {"Alpha Ltd", "Charlie Co"} and data["group"] == "Gold"
    assert all(f["tenant_name"] != "Bravo Inc" for f in data["new_findings"])
    assert delivery.digest_data(db, "weekly", NOW)["tenants"] == 3

    _admin(client, db)
    _configure(client)
    team = delivery.add_contact(db, None, "Me", "me@msp.com")
    assert "Gold (2 tenants)" in _text(client.get("/emails/digests/new"))
    assert (
        client.post(
            "/emails/digests/new",
            data={
                "frequency": "weekly",
                "day_of_week": "0",
                "day_of_month": "1",
                "hour": "7",
                "include_xlsx": "1",
                "contacts": [str(team)],
                "group_id": "31337",
            },
        ).status_code
        == 400
    )
    client.post(
        "/emails/digests/new",
        data={
            "frequency": "weekly",
            "day_of_week": "0",
            "day_of_month": "1",
            "hour": "7",
            "include_xlsx": "1",
            "contacts": [str(team)],
            "group_id": str(gold),
        },
    )
    s = db.execute("SELECT * FROM email_schedules WHERE kind = 'digest'").fetchone()
    assert s["group_id"] == gold
    assert "Tenant digest · Gold" in _text(client.get("/emails"))
    client.post(f"/schedules/{s['id']}/send")
    ((_, _, msg, _),) = sends()
    assert msg["Subject"].startswith("Tenant digest · Gold · ")
    html = msg.get_body(("html",)).get_content()
    assert "Alpha Ltd" in html and "Bravo Inc" not in html and "Tenant digest: Gold" in html
    assert [p.get_filename() for p in msg.iter_attachments()][0].startswith("tenant-digest-gold-")
    # Editing keeps the group; choosing "Every tenant" clears it.
    assert "selected>Tier: Gold" in _text(client.get(f"/schedules/{s['id']}/edit"))
    client.post(
        f"/schedules/{s['id']}/edit",
        data={
            "frequency": "weekly",
            "day_of_week": "0",
            "day_of_month": "1",
            "hour": "7",
            "contacts": [str(team)],
            "group_id": "",
        },
    )
    assert db.execute("SELECT group_id FROM email_schedules WHERE id = ?", (s["id"],)).fetchone()[0] is None


# -- Access by group --------------------------------------------------------------------------------


def test_an_analyst_granted_a_group_sees_tenants_added_later(db, three, connection_id):
    alpha, bravo, charlie, gold = three
    ann = db.execute("SELECT * FROM users WHERE id = ?", (make_user(db, "ann", role="analyst"),)).fetchone()
    tenant_groups.set_user_groups(db, ann["id"], [gold])
    assert [t["name"] for t in access.visible_tenants(db, ann)] == ["Alpha Ltd", "Charlie Co"]
    assert access.can_see_tenant(db, ann, alpha) and not access.can_see_tenant(db, ann, bravo)
    delta = store.create_tenant(db, "Delta", "delta.onmicrosoft.com", connection_id)
    tenant_groups.set_tenant_groups(db, delta, [gold])
    assert access.can_see_tenant(db, ann, delta)
    # Direct grants still add up with group grants.
    db.execute("INSERT INTO user_tenants (user_id, tenant_id) VALUES (?, ?)", (ann["id"], bravo))
    assert len(access.visible_tenants(db, ann)) == 4
    # Archived tenants stay hidden, as for direct grants.
    db.execute("UPDATE tenants SET archived_at = '2026-01-01T00:00:00Z' WHERE id = ?", (charlie,))
    assert "Charlie Co" not in [t["name"] for t in access.visible_tenants(db, ann)]


def test_group_grants_never_reach_a_client(db, three):
    alpha, *_, gold = three
    cid = make_user(db, "cli", role="client")
    tenant_groups.set_user_groups(db, cid, [gold])
    cli = db.execute("SELECT * FROM users WHERE id = ?", (cid,)).fetchone()
    assert access.visible_tenants(db, cli) == [] and not access.can_see_tenant(db, cli, alpha)


def test_user_page_grants_groups_to_analysts_only(client, db, three):
    alpha, *_, gold = three
    _admin(client, db)
    ann = make_user(db, "ann", role="analyst")
    page = _text(client.get(f"/admin/users/{ann}"))
    assert "Tenant groups" in page and "Gold" in page
    client.post(f"/admin/users/{ann}", data={"role": "analyst", "groups": [str(gold), "777"]})
    assert tenant_groups.user_groups(db, ann) == {gold}
    assert "Granted and 1 group" in _text(client.get("/admin/users"))
    # Turning her into a client drops the group grant.
    client.post(f"/admin/users/{ann}", data={"role": "client", "groups": [str(gold)]})
    assert tenant_groups.user_groups(db, ann) == set()


def test_group_page_names_analysts_who_see_it(client, db, three):
    *_, gold = three
    _admin(client, db)
    ann = make_user(db, "ann", role="analyst")
    tenant_groups.set_user_groups(db, ann, [gold])
    page = _text(client.get(f"/admin/groups/{gold}"))
    assert "1 analyst can see every tenant in this group" in page and "ann" in page
