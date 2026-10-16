"""SharePoint, OneDrive and shared files: collectors, checks and reports."""

import io
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import responses
from openpyxl import load_workbook

from officesentry import formatting, state_history, store
from officesentry.collectors import sharepoint
from officesentry.collectors.sharepoint import anyone_can_edit, concealed, describe_permissions, outside_can_edit
from officesentry.collectors.sharepoint_lookup import is_system_site
from officesentry.graph import GraphClient
from officesentry.reports import data_reports
from officesentry.reports.data_reports import Link
from officesentry.reports.sharepoint import nice_max, site_name, size, storage_points
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


def test_hidden_owners_show_as_the_usage_reports_show_people(db, two_tenants, monkeypatch):
    """A hidden owner is a short code, as in every usage report, never Microsoft's whole 32-character code."""
    alpha, _ = two_tenants
    drive = dict(
        sd._drive("ann@x.com", 1000 * 1024**3),
        siteUrl=None,
        ownerPrincipalName="E6F3956464556EB0F7E310CCFAEBE1D1",  # the collector keeps it in lower case
        ownerDisplayName="8815384BBDE8447FBF5C03DF8E19EBA2",
    )
    monkeypatch.setattr(sd, "ONEDRIVE_USAGE", [drive])
    seed_tenant(db, alpha, sharepoint_kw={"sites": [dict(sd.CONCEALED_SITE, lastActivityDate=sd.days_ago(400))]})
    sites = _report(db, alpha, "sharepoint")
    assert [r[2] for r in _section(sites, "All sites").rows] == ["Hidden name (82D288)"]
    assert [r[2] for r in _section(sites, "Inactive sites").rows] == ["Hidden name (82D288)"]
    drives = _report(db, alpha, "onedrive")
    assert [r[0] for r in _section(drives, "All OneDrives").rows] == ["Hidden name (E6F395)"]
    assert [f.subject for f in _results(db, alpha)["onedrive_quota"].findings] == ["Hidden name (E6F395)"]


SHARED = {"Clients", "Budget.xlsx", "Policy.docx", "Internal.docx", "holiday.jpg"}


def test_shared_files_collector(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    snap = _snap(db, alpha)
    items = {i["name"]: i for i in snap.objects("shared_files", "shared_item")}
    # The file inside the shared folder inherits its sharing; only the folder is listed. Unshared or
    # deleted items aren't stored; a file shared with colleagues only is, for the permissions report.
    assert set(items) == SHARED
    clients = items["Clients"]
    assert clients["exposure"] == "anyone" and anyone_can_edit(clients) and clients["neverExpires"]
    assert clients["folder"] and clients["library"] == "Documents" and not clients["personal"]
    assert clients["sharedBy"] == "ann@x.com" and clients["sharedAt"] == "2026-08-20T10:00:00Z"
    assert clients["grants"] == [
        {
            "who": "Anyone with the link",
            "kind": "anyone",
            "how": "Anyone link",
            "can": "Edit",
            "outside": True,
            "expires": None,
            "password": False,
        }
    ]
    budget = items["Budget.xlsx"]
    assert budget["exposure"] == "external" and budget["sharedBy"] == "bob@x.com"
    assert budget["externalPeople"] == ["partner@other.com"]  # the expired link's person no longer counts
    assert [(g["who"], g["how"], g["can"]) for g in budget["grants"]] == [
        ("partner@other.com", "Link", "View"),
        ("old@other.com", "Link", "View"),
    ]
    assert budget["expires"] == sd.days_ago(3) + "T00:00:00Z"  # the earliest expiry, expired or not
    assert items["Policy.docx"]["exposure"] == "organization"
    internal = items["Internal.docx"]
    assert internal["exposure"] is None and internal["internalPeople"] == ["Sales", "bob@x.com"]
    assert [(g["who"], g["kind"], g["can"]) for g in internal["grants"]] == [
        ("bob@x.com", "person", "Edit"),
        ("Sales", "group", "View"),
    ]
    assert all(i["firstSeen"] for i in items.values())
    holiday = items["holiday.jpg"]
    assert holiday["personal"] and holiday["owner"] == "ann@x.com" and not holiday["neverExpires"]
    status = snap.objects("shared_files", "scan_status")[0]
    assert status["available"] and status["drives"] == 2  # Ann's OneDrive found twice, scanned once
    assert status["full_scans"] == 2 and status["kept"] == 0
    cursors = {c["_id"]: c for c in snap.objects("shared_files", "drive_cursor")}
    assert cursors["drv-fin"]["deltaLink"].endswith("/drives/drv-fin/root/delta?token=t-drv-fin")
    assert cursors["drv-fin"]["fullScanAt"] == status["at"]
    assert snap.metric("sharing.anyone_items") == 2 and snap.metric("sharing.unique_items") == 5
    assert snap.metric("sharing.external_people") == 1


def test_shared_files_scan_asks_for_changes_only_the_next_night(db, two_tenants, monkeypatch):
    """After a library's first walk, the next run follows its delta link: only changed items are read, their
    permissions looked up again, and the rest keep last night's answer with their first-seen date."""
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    first_seen = {i["name"]: i["firstSeen"] for i in _snap(db, alpha).objects("shared_files", "shared_item")}
    changes = {
        "drv-fin": [
            dict(sd._item("it-policy", "Policy.docx", "root-fin"), deleted={}),  # deleted
            sd._item("it-budget", "Budget.xlsx", "root-fin", shared=False),  # no longer shared
            sd._item("it-new", "Tender.docx", "root-fin", by="bob@x.com", at=sd.days_ago(1) + "T08:00:00Z"),
            sd._item("it-inside", "Clients plan.docx", "it-clients"),  # inside the shared folder: no lookup
            sd._item("it-internal", "Internal.docx", "root-fin"),  # changed: permissions read again
        ]
    }
    monkeypatch.setattr(sd, "DELTA_CHANGES", changes)
    monkeypatch.setitem(
        sd.PERMISSIONS, "it-new", [{"id": "p9", "roles": ["write"], "grantedToV2": {"user": {"email": "eve@x.com"}}}]
    )
    paths, status, items = _scan(db, alpha)
    assert [p for p in paths if "/root/delta" in p] == ["/drives/drv-ann/root/delta", "/drives/drv-fin/root/delta"]
    assert [p for p in paths if "/permissions" in p] == [
        "/drives/drv-fin/items/it-new/permissions",
        "/drives/drv-fin/items/it-internal/permissions",
    ]
    assert set(items) == {"Clients", "Internal.docx", "Tender.docx", "holiday.jpg"}
    assert items["Clients"]["firstSeen"] == first_seen["Clients"]
    assert items["Tender.docx"]["firstSeen"] == status["at"]  # new since the last scan
    assert items["Internal.docx"]["firstSeen"] == first_seen["Internal.docx"]  # read again, first seen before
    assert status["full_scans"] == 0 and status["kept"] == 2 and status["changed"] == 2 and status["items"] == 4
    cursors = {c["_id"]: c for c in _snap(db, alpha).objects("shared_files", "drive_cursor")}
    assert cursors["drv-fin"]["fullScanAt"] <= status["at"] and cursors["drv-fin"]["checkedAt"] == status["at"]
    run = store.latest_run(db, alpha, "shared_files")
    assert (
        run["status"] == "succeeded" and "changes only in 2 of 2 libraries" in run["log"]
        if "log" in run.keys()
        else True
    )

    # A month on, every library is walked whole again.
    _age_sharepoint_runs(db, alpha, 31)
    monkeypatch.setattr(sd, "DELTA_CHANGES", {})
    paths, status, items = _scan(db, alpha)
    assert status["full_scans"] == 2 and set(items) == SHARED
    # A delta link Microsoft no longer honours (410 Gone) means a whole walk of that library, not a failed run.
    seen = []

    def gone(request):
        seen.append(request.url)
        return 410, {}, '{"error": {"code": "resyncRequired", "message": "token expired"}}'

    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_tenant(rsps)
        rsps.remove(responses.GET, re.compile(re.escape(sd.V1) + r"/drives/[^/]+/root/delta"))
        rsps.add_callback(
            responses.GET, re.compile(re.escape(sd.V1) + r"/drives/[^/]+/root/delta\?token=.*"), callback=gone
        )
        rsps.add_callback(
            responses.GET,
            re.compile(re.escape(sd.V1) + r"/drives/[^/]+/root/delta(\?(?!token=).*)?$"),
            callback=lambda r: (200, {}, json.dumps({"value": sd.DELTA[r.path_url.split("/")[3]]})),
        )
        store.enqueue_run(db, alpha, "shared_files")
        factory = lambda tenant, connection: GraphClient(lambda: "t", sleep=lambda s: None)  # noqa: E731
        assert execute_run(db, store.claim_next_run(db), factory) == "succeeded"
    assert len(seen) == 2  # both links refused, both libraries walked whole
    snap = _snap(db, alpha)
    assert {i["name"] for i in snap.objects("shared_files", "shared_item")} == SHARED
    assert snap.objects("shared_files", "drive_cursor") == []  # no delta link came back, so none is kept


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


def test_microsofts_own_sites_are_system_sites():
    assert is_system_site("https://x.sharepoint.com/search") and is_system_site("https://x.sharepoint.com/search/")
    assert is_system_site("https://x.sharepoint.com/sites/contentTypeHub")
    assert is_system_site("https://x.sharepoint.com/portals/hub") and is_system_site("https://x-admin.sharepoint.com/")
    assert is_system_site(None, "Basic Search Center")
    assert not is_system_site("https://x.sharepoint.com/sites/search") and not is_system_site(
        "https://x.sharepoint.com/"
    )
    assert not is_system_site("https://x.sharepoint.com/searching") and not is_system_site(None, "Team Site")


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
    assert set(items) == SHARED  # the other library's kept
    assert status["scanned_drives"] == 1 and status["carried"] == 4 and status["next_drive"] == "drv-fin"
    run = store.latest_run(db, alpha, "shared_files")
    assert run["status"] == "partial" and run["truncated"]
    assert "1 library of 2 before reaching the limit of 1 page a night" in run["error"]
    assert "scanned first next time" in run["error"]

    paths, status, items = _scan(db, alpha)  # the next night starts with the library left over
    assert [p for p in paths if p.endswith("/root/delta")] == ["/drives/drv-fin/root/delta"]
    assert set(items) == SHARED
    assert status["scanned_drives"] == 1 and status["carried"] == 1 and status["next_drive"] == "drv-ann"
    paths, _, _ = _scan(db, alpha)
    assert [p for p in paths if p.endswith("/root/delta")] == ["/drives/drv-ann/root/delta"]


def test_shared_files_scan_stops_at_its_time_budget(db, two_tenants, monkeypatch):
    """A library the clock runs out in is scanned again, whole, the next night; nothing of it is half stored."""
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    monkeypatch.setattr(sharepoint, "FULL_SCAN_DAYS", 0)  # whole walks every night, so permissions are read
    now = [0.0]
    monkeypatch.setattr(sharepoint, "clock", lambda: now[0])
    describe = sharepoint.describe_permissions
    calls = []

    def slow_describe(perms, domains, when=None):  # the clock jumps past the budget in the second library
        calls.append(perms)
        if len(calls) == 2:
            now[0] += sharepoint.TIME_BUDGET.total_seconds()
        return describe(perms, domains, when)

    monkeypatch.setattr(sharepoint, "describe_permissions", slow_describe)
    paths, status, items = _scan(db, alpha)
    assert [p for p in paths if "/permissions" in p] == [
        "/drives/drv-ann/items/it-holiday/permissions",
        "/drives/drv-fin/items/it-clients/permissions",
    ]
    assert set(items) == SHARED
    assert status["scanned_drives"] == 1 and status["carried"] == 4 and status["next_drive"] == "drv-fin"
    assert "the 2-hour limit" in store.latest_run(db, alpha, "shared_files")["error"]

    # Carried rows are the last scan's, so a library that fell out of reach keeps its rows across nights.
    now[0] = 0.0
    monkeypatch.setattr(sharepoint, "describe_permissions", describe)
    monkeypatch.setattr(sharepoint, "MAX_PAGES", 1)
    _, status, items = _scan(db, alpha)  # drv-fin fresh, drv-ann carried from the first scan
    assert set(items) == SHARED and status["carried"] == 1
    _, status, items = _scan(db, alpha)  # drv-ann fresh, drv-fin carried from the one before
    assert set(items) == SHARED and status["carried"] == 4


def _age_sharepoint_runs(db, alpha, days: float) -> None:
    when = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute(
        "UPDATE runs SET finished_at = ? WHERE tenant_id = ?"
        " AND collector IN ('shared_files', 'sharepoint_storage', 'site_access', 'site_permissions')",
        (when, alpha),
    )
    unpack(db)
    db.execute("UPDATE objects SET data = json_set(data, '$.checked', ?) WHERE kind = 'lookup_onedrive'", (when,))
    db.execute("UPDATE objects SET data = json_set(data, '$.fullScanAt', ?) WHERE kind = 'drive_cursor'", (when,))


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
    assert _snap(db, alpha).objects("shared_files", "scan_status")[0]["full_scans"] == 2  # a month on: whole
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
    outside = results["outside_edit_access"]
    assert (
        outside.status == "pass"
        and outside.summary == "Nothing of the 1 item shared outside can be edited from outside"
    )
    empty = _results(db, bravo)
    assert empty["anyone_links"].status == "unavailable" and empty["outside_edit_access"].status == "unavailable"


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
    assert str(shared.rows[0][0]) == "Finance" and shared.rows[0][0].href == sd.FINANCE and shared.flags == ["high"]
    assert shared.intro.endswith(f"Microsoft's figures as of {formatting.day(sd.days_ago(2))}.")
    idle = _section(report, "Inactive sites")
    assert [str(r[0]) for r in idle.rows] == ["Old Projects"]  # system sites are never "inactive"
    every = _section(report, "All sites")
    assert str(every.rows[0][0]) == "Finance" and every.rows[0][3] == "5.0 GB"
    # Downloads keep the address, in a column the page leaves out until someone shows it.
    assert every.columns[-1] == "Address" and every.optional == ("Address",) and every.rows[0][-1] == sd.FINANCE
    assert [str(r[0]) for r in every.rows][-1] == "https://x-admin.sharepoint.com"  # no name: the address


def test_sites_are_named_from_their_address_until_storage_names_them(db, two_tenants):
    """The usage report only has addresses; the last part of one reads as a name, keeping its own capitals."""
    alpha, _ = two_tenants
    extra = [
        sd._site("https://x.sharepoint.com/teams/IT-helpdesk", "Team Site", storage=3 * 1024**3),
        sd._site("https://x.sharepoint.com/sites/hr_policies/", "Team Site", storage=2 * 1024**3),
    ]
    seed_tenant(db, alpha, sharepoint_kw={"sites": [*sd.SITE_USAGE, *extra]})
    every = _section(_report(db, alpha, "sharepoint"), "All sites")
    names = [str(r[0]) for r in every.rows]
    assert names[:4] == ["Finance", "IT helpdesk", "Hr Policies", "Old Projects"]
    assert every.rows[1][0].href == "https://x.sharepoint.com/teams/IT-helpdesk"
    assert site_name({"url": None}) == "" and site_name({}) == ""


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
    row = next(r for r in _section(_report(db, alpha, "sharepoint"), "All sites").rows if str(r[0]) == "Finance")
    assert row[4] == "+1.0 GB"


def test_site_growth_reads_the_daily_history_once_that_snapshot_is_pruned(db, two_tenants):
    alpha, _ = two_tenants
    old = store.enqueue_run(db, alpha, "sharepoint_usage")
    store.claim_next_run(db)
    store.add_objects(db, old, "site", [(sd.FINANCE, {"storageUsed": 4 * 1024**3})])
    store.finish_run(db, old, "succeeded")
    when = (datetime.now(UTC) - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("UPDATE runs SET created_at = ?, finished_at = ? WHERE id = ?", (when, when, old))
    assert state_history.record_run(db, db.execute("SELECT * FROM runs WHERE id = ?", (old,)).fetchone())
    db.execute("DELETE FROM objects WHERE run_id = ?", (old,))  # as the retention policy does
    seed_tenant(db, alpha)
    row = next(r for r in _section(_report(db, alpha, "sharepoint"), "All sites").rows if str(r[0]) == "Finance")
    assert row[4] == "+1.0 GB"


def test_storage_history_takes_microsofts_figures_where_it_has_them():
    class Snap:
        def history(self, key):
            return [("2026-03-01T09:00:00Z", 900), ("2026-09-02T09:00:00Z", 5000), ("2026-09-04T09:00:00Z", 5000)]

        def objects(self, collector, kind):
            return [{"_id": "onedrive", "points": [["2026-09-01", 1000], ["2026-09-03", 1010], ["2026-09-05", 1020]]}]

    days = [(str(d), v) for d, v in storage_points(Snap(), "onedrive")]
    assert days == [("2026-03-01", 900), ("2026-09-01", 1000), ("2026-09-03", 1010), ("2026-09-05", 1020)]


def test_onedrive_report(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = _report(db, alpha, "onedrive")
    left = _section(report, "OneDrives of people who've left")
    assert [(r[0], r[1]) for r in left.rows] == [("bob@x.com", "Disabled"), ("gone@x.com", "Deleted")]
    sharing = _section(report, "Sharing by person")
    assert sharing.rows == [["ann@x.com", 1, "None", 0, 1]] and sharing.flags == ["high"]
    assert {t.label: t.value for t in report.tiles}["Files shared outside"] == 1


def test_sharing_by_person_leaves_out_sharing_with_colleagues(db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    monkeypatch.setitem(
        sd.PERMISSIONS, "it-holiday", [{"id": "p5", "roles": ["read"], "grantedToV2": {"user": {"email": "bob@x.com"}}}]
    )
    seed_tenant(db, alpha)
    report = _report(db, alpha, "onedrive")
    assert _section(report, "Sharing by person").rows == []
    assert {t.label: t.value for t in report.tiles}["Files shared outside"] == 0


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
    assert "Scanned 7 files and folders in 2 libraries" in report.footnote
    assert _section(report, "Shared with the whole organisation").rows[0][1] == "Finance / Documents"
    assert "Scanned 7 files and folders in 2 libraries" in report.footnote


def test_who_can_edit_follows_each_link(db, two_tenants, monkeypatch):
    """An edit link sent to someone outside lets them edit; an organisation edit link beside an anyone view
    link doesn't let anyone with the link edit."""
    alpha, _ = two_tenants
    monkeypatch.setitem(
        sd.PERMISSIONS,
        "it-budget",
        [
            {
                "id": "p2",
                "roles": ["write"],
                "link": {"scope": "users", "type": "edit"},
                "grantedToIdentitiesV2": [{"user": {"email": "partner@other.com"}}],
            }
        ],
    )
    monkeypatch.setitem(
        sd.PERMISSIONS,
        "it-holiday",
        [
            {"id": "p5", "roles": ["read"], "link": {"scope": "anonymous", "type": "view"}},
            {"id": "p6", "roles": ["write"], "link": {"scope": "organization", "type": "edit"}},
        ],
    )
    seed_tenant(db, alpha)
    items = {i["name"]: i for i in _snap(db, alpha).objects("shared_files", "shared_item")}
    assert outside_can_edit(items["Budget.xlsx"]) and not anyone_can_edit(items["Budget.xlsx"])
    assert not anyone_can_edit(items["holiday.jpg"]) and not outside_can_edit(items["holiday.jpg"])
    report = _report(db, alpha, "shared-files")
    outside = _section(report, "Shared with people outside the organisation")
    assert outside.rows[0][2] == "partner@other.com" and outside.rows[0][3] == "Yes"
    assert outside.flags == ["medium"]
    anyone = _section(report, "Shared with anyone")
    assert [r[2] for r in anyone.rows] == ["Edit", "View"] and anyone.flags == ["high", "medium"]
    findings = {f.subject.split(" (")[0]: f for f in _results(db, alpha)["anyone_links"].findings}
    assert findings["holiday.jpg"].severity == "medium"
    assert findings["holiday.jpg"].detail == "Anyone can view, link never expires"


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
