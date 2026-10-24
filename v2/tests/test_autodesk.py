"""Autodesk licences (autodesk/, collectors/autodesk.py, reports/autodesk.py, web/autodesk.py): setting it up,
giving each client its own Autodesk team, reading only that team, the report and the person page, and that the
app's secret and the sign-ins' tokens never reach a page."""

import json
from datetime import UTC, datetime

import pytest
import requests
import responses
from cryptography.fernet import Fernet

from officesentry import permissions, roles, store, support
from officesentry.__main__ import reencrypt_all
from officesentry.autodesk import api, exports, setup
from officesentry.collectors import REGISTRY
from officesentry.crypto import Vault
from officesentry.reports.autodesk import BLOCKED, GONE, KEY, USED
from officesentry.reports.data_reports import build
from officesentry.reports.profile.autodesk import autodesk_part
from officesentry.reports.profile.base import Person
from officesentry.snapshot import TenantSnapshot
from tests import autodeskdata as ad
from tests.conftest import login, make_user
from tests.seed import seed_tenant


def _text(cell) -> str:
    return str(getattr(cell, "text", cell))


def _rows(section) -> list[dict]:
    return [
        dict(zip(section.columns, [_text(c) if c is not None else None for c in r], strict=True)) for r in section.rows
    ]


def _tiles(report) -> dict:
    return {t.label: t.value for t in report.tiles}


def _read(db, vault, tenant_id, **kwargs):
    return ad.collect(db, vault, tenant_id, ad.Autodesk({ad.TEAM_A: ad.alpha_files()}, **kwargs))


# -- Setting it up -----------------------------------------------------------------------------------------


def test_nothing_about_autodesk_shows_until_its_app_is_saved(client, db, vault, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    assert client.get(f"/tenants/{alpha}/autodesk").status_code == 404
    page = client.get(f"/tenants/{alpha}/manage").get_data(as_text=True)
    assert "/autodesk" not in page
    assert KEY not in client.get(f"/tenants/{alpha}/data").get_data(as_text=True)
    assert not REGISTRY["autodesk"].applies(db, store.get_tenant(db, alpha))
    assert "autodesk" not in {r["collector"] for r in db.execute("SELECT collector FROM runs")}

    settings = client.get("/admin/autodesk").get_data(as_text=True)
    assert "Off" in settings and "Add a sign-in" not in settings  # sign-ins come after the app

    ad.set_up(db, vault)
    page = client.get(f"/tenants/{alpha}/manage").get_data(as_text=True)
    assert f"/tenants/{alpha}/autodesk" in page  # on the client's Settings tab now
    assert client.get(f"/tenants/{alpha}/autodesk").status_code == 200
    assert KEY not in client.get(f"/tenants/{alpha}/data").get_data(as_text=True)  # not until it has a team


def test_the_app_and_a_sign_in_are_saved_encrypted_and_never_shown_again(client, db, vault):
    make_user(db, "admin")
    login(client, "admin")
    resp = client.post("/admin/autodesk/app", data={"client_id": ad.CLIENT_ID, "secret": ad.SECRET})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/admin/autodesk#sign-ins")
    assert setup.configured(db)

    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        ad.Autodesk({}).mock(rsps)
        page = client.post(
            "/admin/autodesk/sign-ins", data={"label": " Sam  at the MSP ", "token": ad.PAT}, follow_redirects=True
        ).get_data(as_text=True)
        [contexts] = [c for c in rsps.calls if c.request.url.endswith("/contexts")]
    assert "Autodesk lists 3 teams for this sign-in, 2 with API reporting" in page
    assert "Alpha Design" in page and "Zebedee Architects" in page and "Charlie Standard" in page
    assert "No API reporting" in page and "Not given to a client" in page
    assert contexts.request.headers["ADSK-PAT"] == ad.PAT

    page = client.get("/admin/autodesk").get_data(as_text=True)
    assert ad.SECRET not in page and ad.PAT not in page
    assert "Saved, leave blank to keep" in page
    stored = json.dumps([dict(r) for r in db.execute("SELECT * FROM settings")])
    stored += json.dumps([dict(r) for r in db.execute("SELECT * FROM autodesk_sign_ins")])
    assert ad.SECRET not in stored and ad.PAT not in stored  # encrypted
    [row] = db.execute("SELECT label, pat_enc FROM autodesk_sign_ins").fetchall()
    assert row["label"] == "Sam at the MSP" and vault.decrypt(row["pat_enc"]) == ad.PAT
    actions = [r[0] for r in db.execute("SELECT action FROM audit_log WHERE action LIKE 'autodesk%'")]
    assert actions == ["autodesk_app_saved", "autodesk_sign_in_added"]
    assert ad.PAT not in json.dumps([dict(r) for r in db.execute("SELECT * FROM audit_log")])

    # Blank keeps the secret; a new app needs its own.
    client.post("/admin/autodesk/app", data={"client_id": ad.CLIENT_ID, "secret": ""})
    assert setup.client_for(db, vault, setup.get_sign_in(db, 1))._client_secret == ad.SECRET
    page = client.post(
        "/admin/autodesk/app", data={"client_id": "OtherApp0123456789", "secret": ""}, follow_redirects=True
    ).get_data(as_text=True)
    assert "Enter the app&#39;s client secret too." in page
    assert setup.app(db)["client_id"] == ad.CLIENT_ID
    page = client.post("/admin/autodesk/app", data={"client_id": "not an id!", "secret": "x"}, follow_redirects=True)
    assert "Enter the app&#39;s client ID" in page.get_data(as_text=True)


def test_a_refused_token_is_explained_and_kept_on_the_sign_in(client, db, vault):
    make_user(db, "admin")
    login(client, "admin")
    ad.set_up(db, vault)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        ad.Autodesk({}, status=401).mock(rsps)
        page = client.post("/admin/autodesk/sign-ins/1/check", follow_redirects=True).get_data(as_text=True)
    assert "Autodesk refused the personal access token" in page
    assert "Autodesk refused the personal access token" in setup.get_sign_in(db, 1)["check_error"]
    assert setup.seen_teams(db)  # what it saw last time is kept

    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, api.HOST + api.TOKEN, status=401, json={"error": "invalid_client"})
        page = client.post("/admin/autodesk/sign-ins/1/check", follow_redirects=True).get_data(as_text=True)
    assert "Autodesk refused the app&#39;s client ID or secret" in page


def test_removing_autodesk_deletes_the_sign_ins_and_keeps_which_team_is_whose(client, db, vault, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    ad.set_up(db, vault)
    ad.give(db, alpha, ad.TEAM_A)
    assert client.post("/admin/autodesk/app/remove").status_code == 302
    assert not setup.configured(db)
    assert db.execute("SELECT COUNT(*) FROM autodesk_sign_ins").fetchone()[0] == 0
    assert db.execute("SELECT tenant_id FROM autodesk_teams").fetchone()[0] == alpha
    assert not setup.applies(db, store.get_tenant(db, alpha))
    assert client.get(f"/tenants/{alpha}/autodesk").status_code == 404
    assert "/autodesk" not in client.get(f"/tenants/{alpha}/manage").get_data(as_text=True)


# -- Each client's team ------------------------------------------------------------------------------------


def test_a_team_is_one_clients_and_only_someone_who_sees_every_client_gives_it(client, db, vault, two_tenants):
    alpha, bravo = two_tenants
    ad.set_up(db, vault)
    make_user(db, "admin")
    # Every capability, but only Alpha.
    manager = roles.create(db, "test", "Manager of Alpha", "", permissions.STAFF, permissions.GRANTED, permissions.KEYS)
    roles.assign(db, make_user(db, "limited", role="analyst", tenants=[alpha]), roles.get(db, manager))

    login(client, "limited")
    page = client.get(f"/tenants/{alpha}/autodesk").get_data(as_text=True)
    assert "Zebedee Architects" not in page and "Alpha Design" not in page  # other clients' accounts' names
    assert "Someone who manages every client gives it one here" in page
    client.post(f"/tenants/{alpha}/autodesk/teams", data={"team": ad.TEAM_A})
    assert setup.teams_for(db, alpha) == []
    assert client.get(f"/tenants/{bravo}/autodesk").status_code == 404
    assert client.get("/admin/autodesk").status_code in (403, 404)

    client.post("/logout")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/autodesk").get_data(as_text=True)
    assert "Alpha Design" in page and "Charlie Standard (no API reporting)" in page
    page = client.post(f"/tenants/{alpha}/autodesk/teams", data={"team": ad.TEAM_A}, follow_redirects=True)
    assert "Alpha Design is now Alpha Ltd&#39;s Autodesk team." in page.get_data(as_text=True)
    assert [r["collector"] for r in db.execute("SELECT collector FROM runs WHERE status = 'queued'")] == ["autodesk"]
    assert setup.applies(db, store.get_tenant(db, alpha))

    page = client.get(f"/tenants/{bravo}/autodesk").get_data(as_text=True)
    assert f'value="{ad.TEAM_A}"' not in page  # taken
    page = client.post(f"/tenants/{bravo}/autodesk/teams", data={"team": ad.TEAM_A}, follow_redirects=True)
    assert "already belongs to a client" in page.get_data(as_text=True)
    client.post(f"/tenants/{bravo}/autodesk/teams/remove", data={"team": ad.TEAM_A})  # not Bravo's to remove
    assert [r["team_id"] for r in setup.teams_for(db, alpha)] == [ad.TEAM_A]
    client.post(f"/tenants/{bravo}/autodesk/teams", data={"team": "999999"})  # no sign-in sees it
    assert setup.teams_for(db, bravo) == []

    settings = client.get("/admin/autodesk").get_data(as_text=True)
    assert f'href="/tenants/{alpha}/autodesk">Alpha Ltd</a>' in settings
    client.post(f"/tenants/{alpha}/autodesk/teams/remove", data={"team": ad.TEAM_A})
    assert setup.teams_for(db, alpha) == []
    actions = [r[0] for r in db.execute("SELECT action FROM audit_log WHERE action LIKE 'autodesk_team%'")]
    assert actions == ["autodesk_team_added", "autodesk_team_removed"]


# -- Collecting --------------------------------------------------------------------------------------------


def test_a_collection_asks_only_for_the_clients_own_team_and_changes_nothing(db, vault, two_tenants):
    alpha, bravo = two_tenants
    ad.set_up(db, vault)
    ad.give(db, alpha, ad.TEAM_A)
    ad.give(db, bravo, ad.TEAM_B)  # the same sign-in sees both
    status, calls = ad.collect(
        db, vault, alpha, ad.Autodesk({ad.TEAM_A: ad.alpha_files(), ad.TEAM_B: ad.bravo_files()})
    )
    assert status == "succeeded"

    sent = [(c.request.method, c.request.url.split("?")[0]) for c in calls]
    assert {m for m, u in sent if m != "GET"} == {"POST"}
    posts = [c.request for c in calls if c.request.method == "POST"]
    assert [p.url for p in posts].count(api.HOST + api.TOKEN) == 1  # the app's token is kept for the run
    started = [json.loads(p.body) for p in posts if p.url == api.HOST + api.EXPORTS]
    assert [s["teamId"] for s in started] == [ad.TEAM_A, ad.TEAM_A]  # never Bravo's, never every team
    assert started[0]["reports"] == ["SUBSCRIPTIONS", "USERS"]
    assert started[1]["usageReports"] == ["USAGE_REPORT_BY_PRODUCT"]
    downloads = [c.request for c in calls if c.request.url.startswith(ad.FILES)]
    assert len(downloads) == 3
    assert all("ADSK-PAT" not in d.headers and "Authorization" not in d.headers for d in downloads)

    run = store.latest_run(db, alpha, "autodesk")
    objects = {(o["_kind"], o["_id"]): o for o in store.run_objects(db, run["id"])}
    text = json.dumps(list(objects.values()))
    assert "Zebedee" not in text and "990000001" not in text and "lee@x.com" not in text  # Bravo's rows left out
    assert "carl@x.com" not in text  # tried Inventor once, holds no seat
    seat = objects[("autodesk_seat", f"{ad.TEAM_A}:ann@x.com:autocad")]
    assert seat["daysUsed"] == 40 and seat["name"] == "Ann Archer" and seat["subscription"] == "110000001"
    assert seat["lastUsed"] == ad._days_ago(4).isoformat() and seat["assignedOn"] == ad._days_ago(260).isoformat()
    team = objects[("autodesk_team", ad.TEAM_A)]
    assert (team["usage"], team["hashed"], team["seats"], team["subscriptions"]) == ("read", False, 4, 2)
    metrics = dict(db.execute("SELECT key, value FROM metrics WHERE tenant_id = ?", (alpha,)).fetchall())
    assert metrics["autodesk.seats"] == 7 and metrics["autodesk.assigned"] == 4
    assert metrics["autodesk.unassigned"] == 3 and metrics["autodesk.unused"] == 2
    log = " ".join(r[0] for r in db.execute("SELECT message FROM run_events WHERE run_id = ?", (run["id"],)))
    assert "left out 3 rows that named another team" in log
    assert "Country" in log  # a column it doesn't read is named


def test_a_failed_usage_report_keeps_the_seats_and_says_so(db, vault, two_tenants):
    alpha, _ = two_tenants
    ad.set_up(db, vault)
    ad.give(db, alpha, ad.TEAM_A)
    status, _ = _read(db, vault, alpha, usage="ERROR")
    assert status == "partial"
    report = build(db, store.get_tenant(db, alpha), KEY)
    assert _tiles(report)["Not used in 90 days"] == "Not read"
    assert any("couldn't be read for Alpha Design" in n for n in report.notes)
    assert _tiles(report)["Assigned"] == 4


def test_a_refused_sign_in_fails_the_run_in_words_and_keeps_the_last_good_one(db, vault, two_tenants):
    alpha, _ = two_tenants
    ad.set_up(db, vault)
    ad.give(db, alpha, ad.TEAM_A)
    assert _read(db, vault, alpha)[0] == "succeeded"
    status, _ = _read(db, vault, alpha, status=401)
    assert status == "failed"
    run = db.execute("SELECT * FROM runs WHERE collector = 'autodesk' ORDER BY id DESC").fetchone()
    assert "Autodesk refused the personal access token" in run["error"]
    assert _tiles(build(db, store.get_tenant(db, alpha), KEY))["Assigned"] == 4  # the good run's

    # A Standard plan team: Autodesk refuses its export.
    ad.give(db, alpha, ad.TEAM_C)
    status, _ = _read(db, vault, alpha)
    assert status == "partial"
    run = db.execute("SELECT id FROM runs WHERE collector = 'autodesk' ORDER BY id DESC").fetchone()
    log = " ".join(r[0] for r in db.execute("SELECT message FROM run_events WHERE run_id = ?", (run["id"],)))
    assert "Charlie Standard" in log and "Business Success Plan" in log


def test_a_team_whose_sign_in_was_removed_is_a_warning(db, vault, two_tenants):
    alpha, _ = two_tenants
    sign_in = ad.set_up(db, vault)
    ad.give(db, alpha, ad.TEAM_A)
    setup.remove_sign_in(db, sign_in, "admin")
    assert setup.teams_for(db, alpha)[0]["sign_in_id"] is None
    status, calls = _read(db, vault, alpha)
    assert status == "failed" and calls == []
    assert "no Autodesk sign-in sees it any more" in store.latest_run(db, alpha, "autodesk", successful=False)["error"]
    # A new sign-in that sees it picks it up when checked.
    new = setup.add_sign_in(db, vault, "Sam again", ad.PAT, "admin")
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        ad.Autodesk({}).mock(rsps)
        setup.check(db, vault, new)
    assert setup.teams_for(db, alpha)[0]["sign_in_id"] == new
    assert _read(db, vault, alpha)[0] == "succeeded"


# -- The report and the person page ------------------------------------------------------------------------


@pytest.fixture
def read_alpha(db, vault, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)  # Microsoft 365: ann@x.com can sign in, bob@x.com is blocked
    ad.set_up(db, vault)
    ad.give(db, alpha, ad.TEAM_A)
    assert _read(db, vault, alpha)[0] == "succeeded"
    return alpha, bravo


def test_the_report_leads_with_spare_seats_unused_seats_and_leavers(db, read_alpha):
    alpha, bravo = read_alpha
    report = build(db, store.get_tenant(db, alpha), KEY)
    assert report.unavailable is None
    assert _tiles(report) == {
        "Seats": 7,
        "Assigned": 4,
        "Not assigned": 3,
        "Not used in 90 days": 2,
        "Held by leavers": 2,
    }
    subs, seats = report.sections
    assert [(r["Product"], r["Seats"], r["Assigned"], r["Not assigned"]) for r in _rows(subs)] == [
        ("AutoCAD", "5", "2", "3"),
        ("Revit", "2", "2", "0"),
    ]
    by = {(r["Person"], r["Product"]): r for r in _rows(seats)}
    assert by[("ann@x.com", "AutoCAD")]["Microsoft 365 account"] == "Enabled"
    assert by[("bob@x.com", "AutoCAD")]["Microsoft 365 account"] == BLOCKED
    assert by[("gone@x.com", "Revit")]["Microsoft 365 account"] == GONE
    assert by[("bob@x.com", "AutoCAD")][USED] == "No" and by[("ann@x.com", "Revit")][USED] == "Yes"
    assert seats.flags[list(by).index(("bob@x.com", "AutoCAD"))] == "high"

    # Only for the clients given a team.
    assert build(db, store.get_tenant(db, bravo), KEY).unavailable == "Autodesk licences aren't read for this client."


def test_the_report_and_its_downloads_open_for_the_client(client, db, read_alpha):
    alpha, bravo = read_alpha
    make_user(db, "admin")
    login(client, "admin")
    assert f"/tenants/{alpha}/data/{KEY}" in client.get(f"/tenants/{alpha}/data").get_data(as_text=True)
    assert f"/tenants/{bravo}/data/{KEY}" not in client.get(f"/tenants/{bravo}/data").get_data(as_text=True)
    page = client.get(f"/tenants/{alpha}/data/{KEY}").get_data(as_text=True)
    assert "Held by leavers" in page and "gone@x.com" in page
    for ext in ("xlsx", "csv", "pdf"):
        assert client.get(f"/tenants/{alpha}/data/{KEY}.{ext}").status_code == 200, ext
    page = client.get(f"/tenants/{alpha}/autodesk").get_data(as_text=True)
    assert "Alpha Design" in page and "Sam at the MSP" in page and f"/tenants/{alpha}/data/{KEY}" in page


def test_a_persons_page_lists_their_autodesk_seats(db, read_alpha):
    alpha, _ = read_alpha
    snap = TenantSnapshot(db, store.get_tenant(db, alpha))
    now = datetime.now(UTC)
    bob = Person(snap, {"_id": "u2", "userPrincipalName": "bob@x.com", "accountEnabled": False}, now)
    part = autodesk_part(bob)
    assert part.summary == "1 product, 1 not used lately" and part.tone == "warn"
    assert [a.text for a in bob.attention if a.part == "autodesk"] == [
        "Sign-in is blocked but the account still holds Autodesk seats (AutoCAD). Take them back in Autodesk Account."
    ]
    ann = Person(snap, {"_id": "u1", "userPrincipalName": "ANN@x.com", "accountEnabled": True}, now)
    assert autodesk_part(ann).summary == "2 products"
    carl = Person(snap, {"_id": "u6", "userPrincipalName": "carl@x.com", "accountEnabled": True}, now)
    assert autodesk_part(carl).empty == "Holds no Autodesk seat."


def test_hashed_people_are_named_by_their_code_and_the_report_says_why(db, vault, two_tenants):
    alpha, _ = two_tenants
    ad.set_up(db, vault)
    ad.give(db, alpha, ad.TEAM_A)
    code = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    files = {
        "SUBSCRIPTIONS": ad.csv_file("Subscription ID,Product,Seats", "1,AutoCAD,1"),
        "USERS": ad.csv_file("Email,Full name,Product,Subscription ID", f"{code},{code},AutoCAD,1"),
        "USAGE_REPORT": ad.csv_file("Email,Product,Last accessed"),
    }
    assert ad.collect(db, vault, alpha, ad.Autodesk({ad.TEAM_A: files}))[0] == "succeeded"
    report = build(db, store.get_tenant(db, alpha), KEY)
    assert any("Autodesk hides people's names and addresses for Alpha Design" in n for n in report.notes)
    [seat] = _rows(report.sections[1])
    assert seat["Person"] == code and seat["Name"] == "" and seat["Microsoft 365 account"] == "Can't tell"
    assert _tiles(report)["Held by leavers"] == 0


# -- The API client and the files --------------------------------------------------------------------------


def _client(**kwargs) -> api.AutodeskClient:
    return api.AutodeskClient(ad.CLIENT_ID, ad.SECRET, ad.PAT, sleep=lambda s: None, **kwargs)


def test_the_client_only_sends_its_tokens_to_the_reporting_api():
    c = _client()
    for method, path in (
        ("DELETE", "/insights/v1/exports/abc"),
        ("POST", "/insights/v1/contexts"),
        ("GET", "/insights/v1/exports/../../users"),
        ("GET", "/userprofile/v1/users/@me"),
        ("PUT", api.EXPORTS),
    ):
        with pytest.raises(ValueError, match="refusing"):
            c._send(method, path)
    with pytest.raises(ValueError, match="must name its team"):
        c.start_export("", ("USERS",))
    with pytest.raises(ValueError):
        c.start_export(ad.TEAM_A, ("USAGE_REPORT",))  # needs its dates
    for link in ("http://files.example/x.csv", "https://10.0.0.5/x.csv", "https://user:pw@files.example/x.csv"):
        with pytest.raises(api.AutodeskError, match="bad_link|download link"):
            c.download(link)


@responses.activate
def test_the_client_waits_for_an_export_and_refuses_another_teams():
    sim = ad.Autodesk({ad.TEAM_A: ad.alpha_files(), ad.TEAM_B: ad.bravo_files()})
    sim.mock(responses)
    c = _client()
    export_id = c.start_export(ad.TEAM_A, ("SUBSCRIPTIONS",))
    links = c.wait(export_id, ad.TEAM_A)
    assert list(links) == ["SUBSCRIPTIONS"]
    assert exports.subscriptions(exports.read(c.download(links["SUBSCRIPTIONS"])))[0].product == "AutoCAD"
    other = c.start_export(ad.TEAM_B, ("USERS",))
    with pytest.raises(api.ExportFailed, match="another team"):
        c.wait(other, ad.TEAM_A)
    stuck = c.start_export(ad.TEAM_A, ("USERS",))
    sim.exports[stuck]["polls"] = -100  # never ready
    ticks = iter(range(0, 10_000, 60))
    with pytest.raises(api.ExportFailed, match="after 15 minutes"):
        api.AutodeskClient(ad.CLIENT_ID, ad.SECRET, ad.PAT, sleep=lambda s: None, clock=lambda: next(ticks)).wait(
            stuck, ad.TEAM_A
        )


@responses.activate
def test_the_client_retries_when_autodesk_is_busy():
    responses.add(responses.POST, api.HOST + api.TOKEN, json={"access_token": ad.APP_TOKEN, "expires_in": 3599})
    busy = responses.add(
        responses.GET, api.HOST + "/insights/v1/contexts", status=429, headers={"Retry-After": "7"}, json={}
    )
    waits = []
    c = api.AutodeskClient(ad.CLIENT_ID, ad.SECRET, ad.PAT, sleep=waits.append, max_retries=2)
    with pytest.raises(api.AutodeskError) as exc:
        c.contexts()
    assert exc.value.status == 429 and waits == [7.0, 7.0] and busy.call_count == 3
    assert c.telemetry  # counted for the run page
    responses.replace(responses.GET, api.HOST + "/insights/v1/contexts", body=requests.ConnectionError("down"))
    with pytest.raises(api.AutodeskError, match="network_error"):
        c.contexts()


def test_export_files_are_read_by_old_and_new_column_names():
    old = exports.read(
        ad.csv_file(
            "subscription_id,offering_name,seat_quantity,seats_assigned,subscription_status,end_date",
            "S1,AutoCAD LT,3,1,ACTIVE,2027-01-31",
        )
    )
    new = exports.read(
        ad.csv_file(
            '"Subscription ID","Product","Seats","Status","End date"', 'S1,"AutoCAD LT","1,200",Active,01/31/2027'
        )
    )
    [a], [b] = exports.subscriptions(old), exports.subscriptions(new)
    assert (a.id, a.product, a.seats, a.assigned, a.status, a.end) == ("S1", "AutoCAD LT", 3, 1, "Active", "2027-01-31")
    assert (b.product, b.seats, b.assigned, b.end) == ("AutoCAD LT", 1200, None, "2027-01-31")
    assert exports.subscriptions(exports.read(b"\x00\x01 not a csv")) == []
    assert exports.read(b"").rows == []
    assert exports.day("13/31/2026") == "" and exports.day("31-Jan-2027") == "2027-01-31"


# -- Keys and the support package --------------------------------------------------------------------------


def test_rotating_the_encryption_key_keeps_autodesk_signed_in(db, cfg, vault):
    ad.set_up(db, vault)
    new = Fernet.generate_key().decode()
    reencrypt_all(db, Vault([new, *cfg.encryption_keys]))
    c = setup.client_for(db, Vault([new]), setup.get_sign_in(db, 1))
    assert (c._client_secret, c._pat) == (ad.SECRET, ad.PAT)


def test_the_support_package_names_no_autodesk_team_person_or_secret(cfg, db, vault, two_tenants):
    alpha, _ = two_tenants
    ad.set_up(db, vault)
    ad.give(db, alpha, ad.TEAM_A)
    assert _read(db, vault, alpha, status=401)[0] == "failed"
    pkg = support.build(db, cfg)
    everything = b"".join(pkg.files.values()).decode()
    for secret in (ad.SECRET, ad.PAT, "Alpha Design", ad.LABEL):
        assert secret not in everything, secret
    [run] = json.loads(pkg.files["failures.json"])["runs"]
    assert run["error"].startswith(f"Autodesk team [client {alpha}]: Autodesk refused the personal access token")
