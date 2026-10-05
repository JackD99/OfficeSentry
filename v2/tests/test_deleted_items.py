"""Deleted users and groups: the Entra ID recycle bin report."""

from datetime import UTC, datetime

from officesentry import store
from officesentry.reports import library
from officesentry.reports.data_reports import GROUPS, REPORTS, build
from tests.conftest import login, make_user
from tests.seed import seed_tenant


def sections(report):
    return {s.title: s for s in report.sections}


def test_recycle_bin_report_lists_days_left_and_flags_what_goes_soon(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = build(db, store.get_tenant(db, alpha), "deleted-items", datetime.now(UTC))
    assert report.unavailable is None and not report.notes
    s = sections(report)
    users = {r[0]: (r, f) for r, f in zip(s["Deleted users"].rows, s["Deleted users"].flags, strict=True)}
    dan, flag = users["Dan Leaver"]
    assert dan[2] == "Member" and dan[4].parts == ("Microsoft 365 Business Premium",)
    assert dan[7] in (4, 5) and dan[8] == "7 days" and flag == "medium"  # licensed member about to go
    assert users["Old Guest"][0][2] == "Guest" and users["Old Guest"][1] == "low"
    assert users["Eve Recent"][0][8] == "30 days" and users["Eve Recent"][1] is None
    assert s["Deleted users"].rows[0][0] == "Old Guest"  # fewest days left first
    groups = {r[0]: (r, f) for r, f in zip(s["Deleted teams and groups"].rows, s["Deleted teams and groups"].flags,
                                           strict=True)}
    assert groups["Project Falcon"][0][1] == "Team" and groups["Project Falcon"][1] == "medium"
    assert groups["Marketing"][0][1] == "Microsoft 365 group" and groups["Marketing"][1] is None
    tiles = {t.label: t for t in report.tiles}
    assert tiles["Deleted users"].value == 3 and tiles["Deleted groups and teams"].value == 2
    soon = tiles["Permanently deleted within 7 days"]
    assert soon.value == 3 and soon.tone == "warn" and soon.foot == "2 users, 1 group"
    assert "never restores" in report.footnote


def test_recycle_bin_report_before_collection_and_when_unreadable(db, two_tenants):
    alpha, _ = two_tenants
    tenant = store.get_tenant(db, alpha)
    assert "haven't been collected" in build(db, tenant, "deleted-items").unavailable
    seed_tenant(db, alpha, deleted_users=[], deleted_groups=[])
    report = build(db, tenant, "deleted-items")
    assert [len(s.rows) for s in report.sections] == [0, 0]
    assert {t.label: t.tone for t in report.tiles}["Permanently deleted within 7 days"] == "ok"


def test_recycle_bin_report_is_listed_and_renders(client, db, two_tenants):
    alpha, _ = two_tenants
    assert "deleted-items" in REPORTS
    assert any("deleted-items" in keys for _, _, keys in GROUPS)
    assert any("deleted-items" in use.reports for use in library.USES)
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/data/deleted-items")
    assert page.status_code == 200 and b"Dan Leaver" in page.data and b"Project Falcon" in page.data
    assert client.get(f"/tenants/{alpha}/data/deleted-items.xlsx").status_code == 200
    assert b"Dan Leaver" in client.get(f"/tenants/{alpha}/data/deleted-items.csv").data
