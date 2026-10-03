"""SharePoint, OneDrive and shared files: collectors, checks and reports."""

import io
from datetime import UTC, datetime, timedelta

from openpyxl import load_workbook

from officesentry import store
from officesentry.collectors.sharepoint import concealed, describe_permissions
from officesentry.reports import data_reports
from officesentry.reports.data_reports import Link
from officesentry.reports.sharepoint import nice_max, size
from officesentry.rules import evaluate
from officesentry.snapshot import TenantSnapshot
from tests import sharepoint_data as sd
from tests.conftest import login, make_user
from tests.seed import seed_tenant


def _snap(db, tenant_id):
    return TenantSnapshot(db, store.get_tenant(db, tenant_id))


def _report(db, tenant_id, key):
    return data_reports.build(db, store.get_tenant(db, tenant_id), key)


def _section(report, title):
    return next(s for s in report.sections if s.title == title)


def _results(db, tenant_id):
    return {r.key: r for r in evaluate(_snap(db, tenant_id))}


# -- Collectors ----------------------------------------------------------------------


def test_usage_collector(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    snap = _snap(db, alpha)
    sites = {s["_id"]: s for s in snap.objects("sharepoint_usage", "site")}
    assert sites[sd.FINANCE]["anyoneLinks"] == 2 and sites[sd.FINANCE]["guestLinks"] == 1
    assert not sites[sd.FINANCE]["concealed"]
    assert len(snap.objects("sharepoint_usage", "onedrive")) == 3
    history = {h["_id"]: h["points"] for h in snap.objects("sharepoint_usage", "storage_history")}
    assert history["sharepoint"][0][0] < history["sharepoint"][-1][0]  # oldest first
    assert len(history["onedrive"]) == 18  # only the "All" series, not the OneDrive duplicate
    assert snap.metric("sharepoint.sites") == 3  # the deleted site isn't counted
    assert snap.metric("sharepoint.links.anyone") == 2
    assert snap.metric("onedrive.storage_bytes") == 6 * 1024 ** 3
    assert not snap.objects("sharepoint_usage", "usage_status")[0]["concealed"]


def test_concealed_names_are_detected(db, two_tenants):
    alpha, _ = two_tenants
    assert concealed(sd.CONCEALED_SITE) and not concealed(sd.SITE_USAGE[0])
    seed_tenant(db, alpha, sharepoint_kw={"sites": [sd.CONCEALED_SITE]})
    report = _report(db, alpha, "sharepoint")
    assert "admin.microsoft.com" in report.notice
    row = _section(report, "Sites shared outside the organisation").rows[0]
    assert str(row[0]) == "Name hidden by Microsoft (group or team site)"


def test_shared_files_collector(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    snap = _snap(db, alpha)
    items = {i["name"]: i for i in snap.objects("shared_files", "shared_item")}
    # The file inside the shared folder inherits its sharing; only the folder is listed.
    # Internal-only shares and unshared or deleted items aren't stored.
    assert set(items) == {"Clients", "Budget.xlsx", "Policy.docx", "holiday.jpg"}
    clients = items["Clients"]
    assert clients["exposure"] == "anyone" and clients["editable"] and clients["neverExpires"]
    assert clients["folder"] and clients["library"] == "Documents" and not clients["personal"]
    assert items["Budget.xlsx"]["exposure"] == "external"
    assert items["Budget.xlsx"]["externalPeople"] == ["partner@other.com"]
    assert items["Policy.docx"]["exposure"] == "organization"
    holiday = items["holiday.jpg"]
    assert holiday["personal"] and holiday["owner"] == "ann@x.com" and not holiday["neverExpires"]
    status = snap.objects("shared_files", "scan_status")[0]
    assert status["available"] and status["drives"] == 2  # Ann's OneDrive found twice, scanned once
    assert snap.metric("sharing.anyone_items") == 2


def test_shared_files_without_permission(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, sharepoint_kw={"files": False})
    run = store.latest_run(db, alpha, "shared_files")
    assert run["status"] == "partial" and "Sites.Read.All" in run["error"]
    report = _report(db, alpha, "shared-files")
    assert "consent link" in report.unavailable
    # The check falls back to Microsoft's per-site counts instead of passing.
    result = _results(db, alpha)["anyone_links"]
    assert result.status == "fail" and result.findings[0].subject == sd.FINANCE


def test_describe_permissions_treats_guest_logins_as_external():
    info = describe_permissions([{"roles": ["read"], "grantedToV2": {"siteUser": {
        "loginName": "i:0#.f|membership|partner_other.com#ext#@x.onmicrosoft.com", "displayName": "Partner"}}}],
        {"x.com"})
    assert info["exposure"] == "external"
    assert describe_permissions([{"roles": ["read"], "link": {"scope": "users"},
                                  "grantedToIdentitiesV2": [{"user": {"email": "bob@x.com"}}]}],
                                {"x.com"})["exposure"] is None


# -- Checks ---------------------------------------------------------------------------


def test_checks(db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    results = _results(db, alpha)
    anyone = results["anyone_links"]
    assert anyone.status == "fail" and len(anyone.findings) == 2
    assert anyone.findings[0].severity == "high"  # the editable folder sorts first
    assert anyone.findings[0].subject == "Clients (Finance / Documents)"
    empty = _results(db, bravo)
    assert empty["anyone_links"].status == "unavailable"


# -- Reports --------------------------------------------------------------------------


def test_sites_report(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = _report(db, alpha, "sharepoint")
    tiles = {t.label: t for t in report.tiles}
    assert tiles["Sites"].value == 2 and tiles["Anyone links"].value == 2 and tiles["Anyone links"].tone == "bad"
    assert tiles["Storage used"].foot.startswith("+")  # Microsoft's 180-day history gives growth on day one
    assert "<svg" in report.chart and not report.notice
    shared = _section(report, "Sites shared outside the organisation")
    assert str(shared.rows[0][0]) == sd.FINANCE and shared.flags == ["high"]
    idle = _section(report, "Inactive sites")
    assert [str(r[0]) for r in idle.rows] == [sd.OLD]  # system sites are never "inactive"
    every = _section(report, "All sites")
    assert str(every.rows[0][0]) == sd.FINANCE and every.rows[0][3] == "5.0 GB"


def test_site_growth_against_a_month_ago(db, two_tenants):
    alpha, _ = two_tenants
    old = (datetime.now(UTC) - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = db.execute("INSERT INTO runs (tenant_id, collector, status, trigger, created_at, finished_at)"
                     " VALUES (?, 'sharepoint_usage', 'succeeded', 'manual', ?, ?)", (alpha, old, old))
    store.add_objects(db, cur.lastrowid, "site", [(sd.FINANCE, {"storageUsed": 4 * 1024 ** 3})])
    seed_tenant(db, alpha)
    row = next(r for r in _section(_report(db, alpha, "sharepoint"), "All sites").rows if str(r[0]) == sd.FINANCE)
    assert row[4] == "+1.0 GB"


def test_onedrive_report(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = _report(db, alpha, "onedrive")
    left = _section(report, "OneDrives of people who've left")
    assert [(r[0], r[1]) for r in left.rows] == [("bob@x.com", "Disabled"), ("gone@x.com", "Deleted")]
    sharing = _section(report, "Sharing by person")
    assert sharing.rows == [["ann@x.com", 1, "None", 0, 1]] and sharing.flags == ["high"]
    assert {t.label: t.value for t in report.tiles}["Files shared outside"] == 1


def test_shared_files_report(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = _report(db, alpha, "shared-files")
    anyone = _section(report, "Shared with anyone")
    first = anyone.rows[0]
    assert str(first[0]) == "Clients (folder)" and first[0].safe_href == "https://x/Clients"
    assert first[2] == "Edit" and first[3] == "Never" and first[5] == "2026-09-01"
    assert anyone.flags == ["high", "low"]
    assert anyone.rows[1][1] == "OneDrive of ann@x.com" and anyone.rows[1][3] == "2099-01-01"
    assert _section(report, "Shared with people outside the organisation").rows[0][2] == "partner@other.com"
    assert _section(report, "Shared with the whole organisation").rows[0][1] == "Finance / Documents"
    assert "Scanned 7 files and folders in 2 libraries" in report.footnote


def test_reports_before_collection(db, two_tenants):
    _, bravo = two_tenants
    for key in ("sharepoint", "onedrive", "shared-files"):
        assert "yet" in _report(db, bravo, key).unavailable


def test_pages_and_exports(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    for key in ("sharepoint", "onedrive", "shared-files"):
        page = client.get(f"/tenants/{alpha}/data/{key}")
        assert page.status_code == 200, key
        assert 'class="tiles"' in page.get_data(as_text=True)
        wb = load_workbook(io.BytesIO(client.get(f"/tenants/{alpha}/data/{key}.xlsx").data))
        assert wb.sheetnames[0] == "Summary"
        assert client.get(f"/tenants/{bravo}/data/{key}").status_code == 200  # empty state renders
    page = client.get(f"/tenants/{alpha}/data/sharepoint").get_data(as_text=True)
    assert "SharePoint storage over time" in page and "<svg" in page
    assert "Shared files" in client.get(f"/tenants/{alpha}").get_data(as_text=True)


def test_client_only_sees_own_tenant(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, bravo)
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    for key in ("sharepoint", "shared-files"):
        assert client.get(f"/tenants/{bravo}/data/{key}").status_code == 404
        assert client.get(f"/tenants/{bravo}/data/{key}.xlsx").status_code == 404


def test_size_and_axis():
    assert size(0) == "0 bytes" and size(1536) == "1.5 KB" and size(5 * 1024 ** 3) == "5.0 GB"
    assert nice_max(6) == 8 and nice_max(0.4) == 2 and nice_max(512) == 600


def test_links_only_for_https():
    assert Link("x", "javascript:alert(1)").safe_href is None
    assert Link("x", "https://x.sharepoint.com/a").safe_href == "https://x.sharepoint.com/a"


def test_xlsx_keeps_links(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    wb = load_workbook(io.BytesIO(data_reports.to_xlsx(_report(db, alpha, "shared-files"))))
    cell = wb["Shared with anyone"]["A2"]
    assert cell.value == "Clients (folder)" and cell.hyperlink.target == "https://x/Clients"
