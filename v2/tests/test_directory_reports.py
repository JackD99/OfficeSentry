"""Unused apps, All users, and Empty, nested and dynamic groups: their collectors and reports."""

import json
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import responses

from officesentry import store
from officesentry.graph import GRAPH_ROOT, GraphClient
from officesentry.reports import library
from officesentry.reports.data_reports import GROUPS, REPORTS, build
from officesentry.worker import execute_run
from tests import graphdata as gd
from tests import groupdata as grp
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_collectors import USERS

V1, BETA = f"{GRAPH_ROOT}/v1.0", f"{GRAPH_ROOT}/beta"
ACTIVITY = f"{BETA}/reports/servicePrincipalSignInActivities"


def _run(db, tenant_id, collector):
    store.enqueue_run(db, tenant_id, collector)
    run = store.claim_next_run(db)
    return run["id"], execute_run(db, run, lambda t, c: GraphClient(lambda: "t", sleep=lambda s: None))


def _sections(report):
    return {s.title: s for s in report.sections}


def _rows(section):
    return {str(r[0]): (r, f) for r, f in zip(section.rows, section.flags, strict=True)}


def _query(call) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(call.request.url).query).items()}


# -- Collectors --------------------------------------------------------------------------------------


@responses.activate
def test_app_sign_ins_keeps_each_apps_latest_sign_in_by_kind(db, two_tenants):
    alpha, _ = two_tenants
    responses.get(ACTIVITY, json={"value": gd.APP_SIGN_INS[:1], "@odata.nextLink": ACTIVITY + "?$skiptoken=2"})
    responses.get(ACTIVITY + "?$skiptoken=2", json={"value": gd.APP_SIGN_INS[1:]})
    run_id, status = _run(db, alpha, "app_sign_ins")
    assert status == "succeeded"
    apps = {a["_id"]: a for a in store.run_objects(db, run_id, "app_activity")}
    assert set(apps) == {"app-zoom", "app-evil", "00000003-0000-0000-c000-000000000000"}
    assert apps["app-zoom"]["delegatedClient"] and apps["app-zoom"]["appClient"] is None
    assert apps["app-evil"]["appClient"] == apps["app-evil"]["last"]
    status = store.run_objects(db, run_id, "sign_in_status")[0]
    assert status["available"] is True and status["apps"] == 3
    assert all(c.request.method == "GET" for c in responses.calls)


@responses.activate
def test_app_sign_ins_without_the_licence_or_permission_say_why(db, two_tenants):
    alpha, bravo = two_tenants
    responses.get(ACTIVITY, status=403, json={"error": {
        "code": "Authentication_RequestFromNonPremiumTenantOrB2CTenant",
        "message": "Neither tenant is B2C or tenant doesn't have premium license"}})
    run_id, status = _run(db, alpha, "app_sign_ins")
    assert status == "partial"
    assert store.run_objects(db, run_id, "sign_in_status")[0]["reason"] == "licence"
    report = build(db, store.get_tenant(db, alpha), "unused-apps")
    assert "Entra ID P1" in report.unavailable

    responses.replace(responses.GET, ACTIVITY, status=403, json={"error": {
        "code": "Authorization_RequestDenied", "message": "Insufficient privileges to complete the operation."}})
    run_id, status = _run(db, bravo, "app_sign_ins")
    assert store.run_objects(db, run_id, "sign_in_status")[0]["reason"] == "permission"
    assert "AuditLog.Read.All" in build(db, store.get_tenant(db, bravo), "unused-apps").unavailable


@responses.activate
def test_user_details_reads_department_and_manager(db, two_tenants):
    alpha, _ = two_tenants
    responses.get(f"{V1}/users", json={"value": USERS})
    run_id, status = _run(db, alpha, "user_details")
    assert status == "succeeded"
    query = _query(responses.calls[0])
    assert "department" in query["$select"] and query["$expand"].startswith("manager(")
    people = {p["_id"]: p for p in store.run_objects(db, run_id, "person")}
    assert people["u1"]["department"] == "Sales" and people["u1"]["jobTitle"] == "Sales lead"
    assert people["u1"]["manager"] == {"id": "u2", "name": "Bob", "upn": "bob@x.com", "type": "user",
                                       "enabled": False}
    assert people["u2"]["manager"] is None and people["u2"]["department"] is None


@responses.activate
def test_group_nesting_reads_members_of_security_groups_and_lists_only(db, two_tenants):
    alpha, _ = two_tenants
    responses.get(f"{V1}/groups", json={"value": grp.GROUPS})

    def members(request):
        gid = request.path_url.split("?")[0].split("/")[3]
        return 200, {}, json.dumps({"value": grp.MEMBERS.get(gid, [])})
    responses.add_callback(responses.GET, re.compile(rf"{V1}/groups/[^/]+/members"), callback=members,
                           content_type="application/json")
    run_id, status = _run(db, alpha, "group_nesting")
    assert status == "succeeded"
    read = {c.request.path_url.split("?")[0].split("/")[3] for c in responses.calls if "/members" in c.request.url}
    assert read == {"s1", "s2", "s3", "m1", "d1"}  # teams and Microsoft 365 groups can't hold groups
    groups = {g["_id"]: g for g in store.run_objects(db, run_id, "group")}
    assert groups["s2"]["groupIds"] == ["s3"] and groups["s2"]["memberCount"] == 2
    assert groups["s3"]["memberCount"] == 0 and groups["s3"]["examined"]
    assert groups["s1"]["dynamic"] and groups["s1"]["ruleState"] == "On"
    assert groups["g1"]["examined"] is False and groups["g1"]["isTeam"]


# -- Reports -------------------------------------------------------------------------------------------


def test_unused_apps_flags_apps_unused_for_90_days_that_still_hold_permissions(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = build(db, store.get_tenant(db, alpha), "unused-apps", datetime.now(UTC))
    assert report.unavailable is None
    apps = _rows(_sections(report)["Enterprise apps"])
    assert "Microsoft Graph" not in apps and "Office Sentry (read-only)" not in apps  # Microsoft's and ours
    row, flag = apps["Mail Sync Pro"]
    assert row[3] == "Over 90 days ago" and row[4].parts == ("On its own",) and flag == "medium"
    assert row[5].parts == ("Mail.Read",) and row[6] == "1 person"
    assert apps["Survey Tool"][0][3] == "No sign-in on record" and apps["Survey Tool"][1] == "low"
    assert apps["Zoom"][0][3] == "Last 30 days" and apps["Zoom"][0][6] == "Everyone" and apps["Zoom"][1] is None
    assert list(apps)[0] == "Mail Sync Pro"  # what to remove first leads
    tiles = {t.label: t for t in report.tiles}
    assert tiles["Unused for 90+ days"].value == 2 and tiles["Unused for 90+ days"].tone == "warn"
    assert tiles["Unused but still has access"].value == 1 and tiles["Unused but still has access"].tone == "bad"
    assert tiles["No sign-in on record"].value == 1 and tiles["Used in the last 30 days"].value == 1


def test_all_users_lists_every_account_with_manager_and_flags_blocked_managers(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = build(db, store.get_tenant(db, alpha), "all-users", datetime.now(UTC))
    assert report.unavailable is None and not report.notes
    s = _sections(report)
    every = _rows(s["Every account"])
    ann, flag = every["ann@x.com"]
    assert ann[1:7] == ["Ann", "Member", "Allowed", "Sales", "Sales lead", "Bob"] and flag == "medium"
    assert ann[7].parts == ("Microsoft 365 Business Premium",) and ann[10] == "2026-09-30"
    assert every["bob@x.com"][0][3] == "Blocked" and every["bob@x.com"][1] is None
    assert every["g#EXT#@x.com"][0][2] == "Guest"
    bob, flag = _rows(s["Managers"])["bob@x.com"]
    assert bob[2:5] == ["Blocked", "", 1] and bob[5].parts == ("Ann",) and flag == "medium"
    assert [r[0] for r in s["Departments"].rows] == ["Sales", "No department"]
    tiles = {t.label: t for t in report.tiles}
    assert tiles["Accounts"].value == 3 and tiles["Accounts"].foot == "2 members, 1 guest"
    assert tiles["Report to a blocked manager"].value == 1 and tiles["Sign-in blocked"].foot == "1 still licensed"
    assert tiles["Members with no manager"].value == 0


def test_all_users_before_departments_and_managers_are_collected(db, two_tenants):
    alpha, _ = two_tenants
    tenant = store.get_tenant(db, alpha)
    assert "Refresh Users" in build(db, tenant, "all-users").unavailable
    seed_tenant(db, alpha)
    db.execute("DELETE FROM runs WHERE collector = 'user_details'")
    report = build(db, tenant, "all-users")
    assert report.unavailable is None and "Departments and managers" in report.notes[0]
    assert len(report.sections[0].rows) == 3 and report.sections[0].rows[0][6] == ""
    assert {t.label: t.value for t in report.tiles}["Members with no manager"] == "Not checked"


def test_group_structure_shows_empty_nested_and_dynamic_groups(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = build(db, store.get_tenant(db, alpha), "group-structure", datetime.now(UTC))
    assert report.unavailable is None and not report.notes
    s = _sections(report)
    empty = _rows(s["Empty groups"])
    assert list(empty) == ["Group creators"] and empty["Group creators"][1] == "low"
    nested = _rows(s["Groups inside groups"])
    assert nested["Info"][0][2].parts == ("Finance alerts",) and nested["Info"][0][4] == 3
    assert nested["Info"][1] == "low"  # three levels down
    assert nested["VPN Users"][0][3].parts == ("Finance alerts",) and nested["VPN Users"][1] is None
    dynamic = _rows(s["Dynamic groups"])
    assert dynamic["All Staff"][0][2:5] == ['(user.userType -eq "Member")', "Running", 2]
    every = _rows(s["Every group"])
    assert every["Sales"][0][1:4] == ["Team", "Assigned", 3]  # members from Groups and Teams
    assert every["VPN Users"][0][2] == "Synced from AD"
    tiles = {t.label: t for t in report.tiles}
    assert tiles["Empty groups"].value == 1 and tiles["Groups inside groups"].foot == "Deepest: 3 levels"
    assert tiles["Paused dynamic rules"].value == 0 and tiles["Paused dynamic rules"].tone == "ok"


def test_a_paused_dynamic_rule_and_a_group_loop_are_reported(db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    groups = [dict(g, membershipRuleProcessingState="Paused") if g["id"] == "s1" else g for g in grp.GROUPS]
    # Group creators holds Info, which (three levels down) holds Group creators.
    monkeypatch.setitem(grp.MEMBERS, "s3", [{"@odata.type": "#microsoft.graph.group", "id": "d1"}])
    seed_tenant(db, alpha, groups=groups)
    report = build(db, store.get_tenant(db, alpha), "group-structure")
    s = _sections(report)
    assert _rows(s["Dynamic groups"])["All Staff"][1] == "medium"
    assert {t.label: t.tone for t in report.tiles}["Paused dynamic rules"] == "bad"
    assert _rows(s["Groups inside groups"])["Info"][0][4] == 4  # each group in the loop counted once
    assert not s["Empty groups"].rows


def test_reports_are_listed_and_render_with_downloads(client, db, two_tenants):
    alpha, _ = two_tenants
    keys = ("unused-apps", "all-users", "group-structure")
    menu = {k for _, _, group in GROUPS for k in group}
    assert all(k in REPORTS and k in menu for k in keys)
    cleanup = next(u for u in library.USES if u.key == "cleanup")
    assert all(k in cleanup.reports for k in keys)
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    for key, text in (("unused-apps", b"Mail Sync Pro"), ("all-users", b"Sales lead"),
                      ("group-structure", b"Group creators")):
        page = client.get(f"/tenants/{alpha}/data/{key}")
        assert page.status_code == 200 and text in page.data, key
        assert text in client.get(f"/tenants/{alpha}/data/{key}.csv").data
        assert client.get(f"/tenants/{alpha}/data/{key}.xlsx").status_code == 200
        assert client.get(f"/tenants/{alpha}/data/{key}.pdf").status_code == 200
    page = client.get("/reports?q=empty+groups").data
    assert b"Empty, nested and dynamic groups" in page
