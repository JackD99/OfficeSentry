"""SharePoint, OneDrive and shared files: collectors, checks and reports."""

import io
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import responses
from openpyxl import load_workbook

from officesentry import store
from officesentry.collectors import sharepoint
from officesentry.collectors.sharepoint import concealed, describe_permissions
from officesentry.graph import GraphClient
from officesentry.reports import data_reports
from officesentry.reports.data_reports import Link
from officesentry.reports.sharepoint import nice_max, size
from officesentry.rules import evaluate
from officesentry.snapshot import TenantSnapshot
from officesentry.worker import execute_run
from tests import sharepoint_data as sd
from tests.conftest import login, make_user
from tests.seed import mock_tenant, seed_tenant
from tests.stored import unpack


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
    assert snap.metric("onedrive.storage_bytes") == 6 * 1024**3
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
    seed_tenant(db, alpha, sharepoint_kw={"files": False}, our_app={"missing": ("Sites.Read.All",)})
    run = store.latest_run(db, alpha, "shared_files")
    assert run["status"] == "partial" and "needs the Sites.Read.All permission" in run["error"]
    report = _report(db, alpha, "shared-files")
    assert "send the consent link to the client's Global Admin again" in report.unavailable
    # The check falls back to Microsoft's per-site counts instead of passing.
    result = _results(db, alpha)["anyone_links"]
    assert result.status == "fail" and result.findings[0].subject == sd.FINANCE


def test_sharepoint_refusing_a_granted_app_is_not_called_missing_consent(db, two_tenants):
    """A 403 from SharePoint while Sites.Read.All is granted is SharePoint's own restriction: a consent
    link won't fix it, so neither the run nor the reports send the admin to re-consent."""
    alpha, _ = two_tenants
    seed_tenant(db, alpha, sharepoint_kw={"files": False})
    run = store.latest_run(db, alpha, "shared_files")
    assert run["status"] == "partial" and "isn't a consent problem" in run["error"]
    assert _snap(db, alpha).objects("shared_files", "scan_status")[0]["reason"] == "denied"
    report = _report(db, alpha, "shared-files")
    assert "won't help" in report.unavailable and "Admin again" not in report.unavailable


def test_describe_permissions_treats_guest_logins_as_external():
    info = describe_permissions(
        [
            {
                "roles": ["read"],
                "grantedToV2": {
                    "siteUser": {
                        "loginName": "i:0#.f|membership|partner_other.com#ext#@x.onmicrosoft.com",
                        "displayName": "Partner",
                    }
                },
            }
        ],
        {"x.com"},
    )
    assert info["exposure"] == "external"
    assert (
        describe_permissions(
            [
                {
                    "roles": ["read"],
                    "link": {"scope": "users"},
                    "grantedToIdentitiesV2": [{"user": {"email": "bob@x.com"}}],
                }
            ],
            {"x.com"},
        )["exposure"]
        is None
    )


def _calls(db, alpha, collector: str = "shared_files") -> list[str]:
    factory = lambda tenant, connection: GraphClient(lambda: "t", sleep=lambda s: None)  # noqa: E731
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_tenant(rsps)
        store.enqueue_run(db, alpha, collector)
        assert execute_run(db, store.claim_next_run(db), factory) in ("succeeded", "partial")
        return [urlsplit(c.request.url).path.removeprefix("/v1.0") for c in rsps.calls]


def _scan(db, alpha):
    """One more shared-files run: its Graph paths, its scan status and its items by name."""
    paths = _calls(db, alpha)
    snap = _snap(db, alpha)
    status = snap.objects("shared_files", "scan_status")[0]
    return paths, status, {i["name"]: i for i in snap.objects("shared_files", "shared_item")}


def test_shared_files_scan_stops_at_its_page_budget_and_carries_on_next_time(db, two_tenants, monkeypatch):
    """A night's share of pages: the libraries left over keep their last results and go first next time."""
    alpha, _ = two_tenants
    seed_tenant(db, alpha)  # a full scan: four shared items in two libraries
    monkeypatch.setattr(sharepoint, "MAX_PAGES", 1)  # one library's delta a night
    paths, status, items = _scan(db, alpha)
    assert [p for p in paths if p.endswith("/root/delta")] == ["/drives/drv-ann/root/delta"]  # in id order
    assert set(items) == {"Clients", "Budget.xlsx", "Policy.docx", "holiday.jpg"}  # the other library's kept
    assert status["scanned_drives"] == 1 and status["carried"] == 3 and status["next_drive"] == "drv-fin"
    run = store.latest_run(db, alpha, "shared_files")
    assert run["status"] == "partial" and run["truncated"]
    assert "1 library of 2 before reaching the limit of 1 page a night" in run["error"]
    assert "scanned first next time" in run["error"]

    paths, status, items = _scan(db, alpha)  # the next night starts with the library left over
    assert [p for p in paths if p.endswith("/root/delta")] == ["/drives/drv-fin/root/delta"]
    assert set(items) == {"Clients", "Budget.xlsx", "Policy.docx", "holiday.jpg"}
    assert status["scanned_drives"] == 1 and status["carried"] == 1 and status["next_drive"] == "drv-ann"
    paths, _, _ = _scan(db, alpha)
    assert [p for p in paths if p.endswith("/root/delta")] == ["/drives/drv-ann/root/delta"]


def test_shared_files_scan_stops_at_its_time_budget(db, two_tenants, monkeypatch):
    """A library the clock runs out in is scanned again, whole, the next night; nothing of it is half stored."""
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    now = [0.0]
    monkeypatch.setattr(sharepoint, "clock", lambda: now[0])
    describe = sharepoint.describe_permissions
    calls = []

    def slow_describe(perms, domains):  # the clock jumps past the budget during the second library's first item
        calls.append(perms)
        if len(calls) == 2:
            now[0] += sharepoint.TIME_BUDGET.total_seconds()
        return describe(perms, domains)

    monkeypatch.setattr(sharepoint, "describe_permissions", slow_describe)
    paths, status, items = _scan(db, alpha)
    assert [p for p in paths if "/permissions" in p] == [
        "/drives/drv-ann/items/it-holiday/permissions",
        "/drives/drv-fin/items/it-clients/permissions",
    ]
    assert set(items) == {"Clients", "Budget.xlsx", "Policy.docx", "holiday.jpg"}
    assert status["scanned_drives"] == 1 and status["carried"] == 3 and status["next_drive"] == "drv-fin"
    assert "the 2-hour limit" in store.latest_run(db, alpha, "shared_files")["error"]

    # Carried rows are the last scan's, so a library that fell out of reach keeps its rows across nights.
    now[0] = 0.0
    monkeypatch.setattr(sharepoint, "describe_permissions", describe)
    monkeypatch.setattr(sharepoint, "MAX_PAGES", 1)
    _, status, items = _scan(db, alpha)  # drv-fin fresh, drv-ann carried from the first scan
    assert set(items) == {"Clients", "Budget.xlsx", "Policy.docx", "holiday.jpg"} and status["carried"] == 1
    _, status, items = _scan(db, alpha)  # drv-ann fresh, drv-fin carried from the one before
    assert set(items) == {"Clients", "Budget.xlsx", "Policy.docx", "holiday.jpg"} and status["carried"] == 3


def _age_sharepoint_runs(db, alpha, days: float) -> None:
    when = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute(
        "UPDATE runs SET finished_at = ? WHERE tenant_id = ?"
        " AND collector IN ('shared_files', 'sharepoint_storage', 'site_access')",
        (when, alpha),
    )
    unpack(db)
    db.execute("UPDATE objects SET data = json_set(data, '$.checked', ?) WHERE kind = 'lookup_onedrive'", (when,))


def test_shared_files_skips_onedrives_the_site_listing_gave(db, two_tenants):
    """Ann's OneDrive comes back in the site listing, so it isn't looked up again by user; Bob's doesn't."""
    alpha, _ = two_tenants
    seed_tenant(db, alpha)  # the users snapshot the collector reads
    _age_sharepoint_runs(db, alpha, 30)  # nothing recent to reuse
    before = {i["_id"]: i for i in _snap(db, alpha).objects("shared_files", "shared_item")}
    paths = _calls(db, alpha)
    assert paths.count("/users/u1/drive") == 0
    assert paths.count("/users/u2/drive") == 1  # not in the listing, so still asked for (404: never opened)
    assert paths.count("/sites/site-ann/drives") == 1 and paths.count("/sites/getAllSites") == 1
    assert len(paths) == 1 + 3 + 1 + 2 + 5  # sites, their drives, Bob's OneDrive, two deltas, five shared items
    snap = _snap(db, alpha)
    assert {i["_id"]: i for i in snap.objects("shared_files", "shared_item")} == before
    assert snap.objects("shared_files", "scan_status")[0]["drives"] == 2


def test_sharepoint_collectors_share_their_lookups(db, two_tenants):
    """The second SharePoint collection of the night reuses the first one's lookups."""
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    _age_sharepoint_runs(db, alpha, 30)
    first = _calls(db, alpha)
    assert first.count("/sites/getAllSites") == 1 and first.count("/users/u2/drive") == 1  # 404: no OneDrive
    storage = _calls(db, alpha, "sharepoint_storage")
    assert storage.count("/sites/getAllSites") == 0  # shared files asked for every field storage needs
    assert storage.count("/users/u2/drive") == 0  # and found Bob has no OneDrive
    assert storage.count("/users/u1/drive") == 1  # shared files didn't need Ann's (the site listing had it)
    # Both ask for the same fields, so the other way round works too: shared files reuses storage's lookups.
    again = _calls(db, alpha)
    assert again.count("/sites/getAllSites") == 0 and again.count("/users/u2/drive") == 0
    assert _snap(db, alpha).objects("shared_files", "scan_status")[0]["drives"] == 2

    _age_sharepoint_runs(db, alpha, 1)  # a day later: "no OneDrive" is still remembered
    assert _calls(db, alpha).count("/users/u2/drive") == 0
    _age_sharepoint_runs(db, alpha, 8)  # after a week Bob is asked again
    assert _calls(db, alpha).count("/users/u2/drive") == 1


def test_one_onedrive_lookup_a_night(db, two_tenants):
    """Whichever SharePoint collector runs second asks again for no site list and no OneDrive the first asked for."""
    alpha, _ = two_tenants
    seed_tenant(db, alpha)

    def lookups(calls):
        return [c for c in calls if c == "/sites/getAllSites" or c.startswith("/users/") and c.endswith("/drive")]

    for first, second in (("shared_files", "sharepoint_storage"), ("sharepoint_storage", "shared_files")):
        _age_sharepoint_runs(db, alpha, 30)  # nothing fresh to reuse, and "no OneDrive" forgotten
        asked = lookups(_calls(db, alpha, first))
        assert asked.count("/sites/getAllSites") == 1 and asked.count("/users/u2/drive") == 1, first
        assert set(asked) & set(lookups(_calls(db, alpha, second))) == set(), second


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
    cur = db.execute(
        "INSERT INTO runs (tenant_id, collector, status, trigger, created_at, finished_at)"
        " VALUES (?, 'sharepoint_usage', 'succeeded', 'manual', ?, ?)",
        (alpha, old, old),
    )
    store.add_objects(db, cur.lastrowid, "site", [(sd.FINANCE, {"storageUsed": 4 * 1024**3})])
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
    assert anyone.flags == ["high", "medium"]  # view-only with an expiry: medium, as the check rates it
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
    assert size(0) == "0 bytes" and size(1536) == "1.5 KB" and size(5 * 1024**3) == "5.0 GB"
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
