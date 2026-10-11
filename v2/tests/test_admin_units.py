"""Administrative units: the collector, the report, the person-page part and the pages."""

import json
import re
from datetime import UTC, datetime

import responses

from officesentry import store
from officesentry.graph import GRAPH_ROOT, GraphClient
from officesentry.reports import profile
from officesentry.reports.data_reports import GROUPS, REPORTS, build
from officesentry.worker import execute_run
from tests import graphdata as gd
from tests.conftest import login, make_user
from tests.seed import seed_tenant

V1 = f"{GRAPH_ROOT}/v1.0"
UNITS = f"{V1}/directory/administrativeUnits"


def _run(db, tenant_id, collector):
    store.enqueue_run(db, tenant_id, collector)
    run = store.claim_next_run(db)
    return run["id"], execute_run(db, run, lambda t, c: GraphClient(lambda: "t", sleep=lambda s: None))


def _sections(report):
    return {s.title: s for s in report.sections}


def _rows(section):
    return {str(r[0]): (r, f) for r, f in zip(section.rows, section.flags, strict=True)}


def _mock_members():
    def members(request):
        uid = request.path_url.split("?")[0].split("/")[4]
        return 200, {}, json.dumps({"value": gd.ADMIN_UNIT_MEMBERS.get(uid, [])})

    responses.add_callback(
        responses.GET, re.compile(rf"{UNITS}/[^/]+/members"), callback=members, content_type="application/json"
    )


# -- Collector ---------------------------------------------------------------------------------------


@responses.activate
def test_admin_units_reads_each_unit_and_counts_its_members_by_kind(db, two_tenants):
    alpha, _ = two_tenants
    responses.get(UNITS, json={"value": gd.ADMIN_UNITS[:2], "@odata.nextLink": UNITS + "?$skiptoken=2"})
    responses.get(UNITS + "?$skiptoken=2", json={"value": gd.ADMIN_UNITS[2:]})
    _mock_members()
    run_id, status = _run(db, alpha, "admin_units")
    assert status == "succeeded"
    assert all(c.request.method == "GET" for c in responses.calls)
    assert "isMemberManagementRestricted" in responses.calls[0].request.url
    units = {u["_id"]: u for u in store.run_objects(db, run_id, "admin_unit")}
    sales = units["au-sales"]
    assert sales["examined"] and (sales["memberCount"], sales["userCount"], sales["groupCount"]) == (3, 2, 1)
    assert sales["userIds"] == ["u1", "u3"] and sales["groupIds"] == ["g1"] and not sales["dynamic"]
    branches = units["au-branches"]
    assert branches["dynamic"] and branches["ruleState"] == "Paused" and branches["restricted"] and branches["hidden"]
    assert (branches["userCount"], branches["deviceCount"]) == (1, 1)
    assert units["au-desk"]["memberCount"] == 0
    assert store.run_objects(db, run_id, "admin_unit_status")[0] == {
        "_id": "status",
        "_kind": "admin_unit_status",
        "available": True,
        "units": 3,
    }
    metrics = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM metrics WHERE run_id = ?", (run_id,))}
    assert metrics == {
        "admin_units.total": 3,
        "admin_units.restricted": 1,
        "admin_units.dynamic": 1,
        "admin_units.empty": 1,
    }


@responses.activate
def test_admin_units_say_why_when_not_readable(db, two_tenants):
    alpha, _ = two_tenants
    responses.get(
        UNITS,
        status=403,
        json={"error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges"}},
    )
    run_id, status = _run(db, alpha, "admin_units")
    assert status == "partial"
    assert store.run_objects(db, run_id, "admin_unit") == []
    assert store.run_objects(db, run_id, "admin_unit_status")[0]["available"] is False
    report = build(db, store.get_tenant(db, alpha), "admin-units")
    assert (
        report.unavailable.startswith("Administrative units couldn't be read") and "Insufficient" in report.unavailable
    )


@responses.activate
def test_a_unit_whose_members_cant_be_read_is_kept_unexamined(db, two_tenants):
    alpha, _ = two_tenants
    responses.get(UNITS, json={"value": gd.ADMIN_UNITS})
    responses.get(rf"{UNITS}/au-sales/members", status=404, json={"error": {"code": "Request_ResourceNotFound"}})
    responses.get(re.compile(rf"{UNITS}/au-(branches|desk)/members.*"), json={"value": []})
    run_id, status = _run(db, alpha, "admin_units")
    assert status == "succeeded"
    units = {u["_id"]: u for u in store.run_objects(db, run_id, "admin_unit")}
    assert units["au-sales"]["examined"] is False and "memberCount" not in units["au-sales"]
    assert units["au-desk"]["examined"]


# -- Report ------------------------------------------------------------------------------------------


def test_admin_units_report_lists_units_scoped_admins_and_members(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = build(db, store.get_tenant(db, alpha), "admin-units", datetime.now(UTC))
    assert report.unavailable is None and not report.notes
    s = _sections(report)
    every = _rows(s["Every unit"])
    assert list(every) == ["Branch offices", "Sales", "Service desk"]
    sales, flag = every["Sales"]
    assert sales[1:7] == ["Assigned", "No", 2, 1, 0, 1] and sales[7].parts == ("Ann",) and flag is None
    branches, flag = every["Branch offices"]
    assert branches[1:6] == ["Dynamic (paused)", "Yes", 1, 0, 1] and branches[7].parts == ("Bob",)
    assert flag == "medium"
    desk, flag = every["Service desk"]
    assert desk[3:7] == [0, 0, 0, 0] and flag == "low"

    admins = _rows(s["Scoped admins"])
    bob, flag = admins["bob@x.com"]
    assert bob[1:7] == ["Bob", "Groups Administrator", "Branch offices", "Active", "Blocked", "Member"]
    assert flag == "medium" and list(admins)[0] == "bob@x.com"  # what to fix first leads
    ann, flag = admins["ann@x.com"]
    assert ann[2:6] == ["Groups Administrator", "Sales", "Active", "Allowed"] and flag is None

    members = s["Members"]
    assert [r[:2] for r in members.rows] == [
        ["Branch offices", "bob@x.com"],
        ["Sales", "ann@x.com"],
        ["Sales", "g#EXT#@x.com"],
        ["Sales", "g1@x.com"],
    ]
    assert members.rows[0][3:5] == ["User", "Blocked"] and members.rows[2][3] == "Guest"
    assert members.rows[3][2:4] == ["Sales", "Group"]

    tiles = {t.label: t for t in report.tiles}
    assert tiles["Units"].value == 3 and tiles["Units"].foot == "1 dynamic unit, 1 restricted, 1 rule paused"
    assert tiles["Scoped admins"].value == 2 and tiles["Scoped admins"].foot == "Across 2 units"
    assert tiles["Empty units"].value == 1 and tiles["Empty units"].tone == "warn"
    assert tiles["Empty units"].rows.where == {"Users": 0, "Groups": 0, "Devices": 0}
    assert tiles["No scoped admin"].value == 1 and tiles["No scoped admin"].rows.where == {"Scoped admins": 0}


def test_admin_units_report_before_roles_and_users_are_collected(db, two_tenants):
    alpha, _ = two_tenants
    tenant = store.get_tenant(db, alpha)
    assert "Refresh Administrative units" in build(db, tenant, "admin-units").unavailable
    seed_tenant(db, alpha)
    db.execute("DELETE FROM runs WHERE collector IN ('admin_roles', 'users')")
    report = build(db, tenant, "admin-units")
    assert report.unavailable is None
    assert any("Admin roles" in n for n in report.notes) and any("Users" in n for n in report.notes)
    s = _sections(report)
    assert _rows(s["Every unit"])["Sales"][0][6] == "Not checked" and not s["Scoped admins"].rows
    assert s["Members"].rows[1][1] == "u1"  # named by ID until Users is collected
    tiles = {t.label: t.value for t in report.tiles}
    assert tiles["Scoped admins"] == "Not checked" and tiles["No scoped admin"] == "Not checked"


def test_a_tenant_without_units_says_so(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, admin_units=[])
    report = build(db, store.get_tenant(db, alpha), "admin-units")
    assert report.unavailable is None and all(not s.rows for s in report.sections)
    assert "no administrative units" in report.sections[0].empty
    tiles = {t.label: t for t in report.tiles}
    assert tiles["Units"].value == 0 and tiles["Empty units"].value == 0 and tiles["Empty units"].tone is None


# -- Person page -------------------------------------------------------------------------------------


def test_the_person_page_shows_units_administered_and_joined(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    tenant = store.get_tenant(db, alpha)
    ann = profile.build(db, tenant, "u1")
    part = next(p for p in ann.parts if p.key == "admin_units")
    assert part.summary == "Administers 1, In 1"
    assert [r[:2] for r in part.tables[0].rows] == [
        ["Sales", "Administers as Groups Administrator"],
        ["Sales", "Member"],
    ]
    bob = profile.build(db, tenant, "u2")
    part = next(p for p in bob.parts if p.key == "admin_units")
    assert part.tables[0].rows[0][:3] == ["Branch offices", "Administers as Groups Administrator", "Dynamic (paused)"]
    assert any("still administers Branch offices" in a.text for a in bob.attention)
    guest = profile.build(db, tenant, "u3")
    part = next(p for p in guest.parts if p.key == "admin_units")
    assert part.summary == "In 1" and part.tables[0].rows == [["Sales", "Member", "Assigned", "No"]]


# -- Pages -------------------------------------------------------------------------------------------


def test_the_report_is_listed_and_renders_with_downloads(client, db, two_tenants):
    alpha, _ = two_tenants
    assert "admin-units" in REPORTS and "admin-units" in {k for _, _, group in GROUPS for k in group}
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/data/admin-units")
    assert page.status_code == 200 and b"Branch offices" in page.data and b"Groups Administrator" in page.data
    assert b"Branch offices" in client.get(f"/tenants/{alpha}/data/admin-units.csv").data
    assert client.get(f"/tenants/{alpha}/data/admin-units.xlsx").status_code == 200
    assert client.get(f"/tenants/{alpha}/data/admin-units.pdf").status_code == 200
    assert b"Administrative units" in client.get("/reports?q=scoped+admins").data
    assert b"Administrative units" in client.get(f"/tenants/{alpha}/users/u1").data
