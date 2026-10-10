"""Scheduled emails that send saved views (one tenant, or every tenant or a group for the team), and
report-ready posts to Teams, Slack or a webhook that link to the report history and never carry files."""

import csv
import io
import json
import socket

import pytest
import responses

from officesentry import alerts, delivery, mailer, month_end, tenant_groups
from officesentry.reports import data_reports, views
from officesentry.reports.export import pdf_available
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_email import SENT, FakeSMTP, _smtp_ready, sends
from tests.test_month_end import PERIOD, _plan, work

HOOK = "https://hooks.example.com/workflows/abc/run?sig=TOPSECRETSIG"
FORWARDING = views.View.parse("f=forwarding/outside-the-organisation:Yes&sort=forwarding/mailbox:desc").query()
FORM = {"frequency": "monthly", "day_of_week": "0", "day_of_month": "1", "hour": "8", "timezone": "UTC"}


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.delenv(alerts.ALLOW_PRIVATE_ENV, raising=False)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("52.1.2.3", port))],
    )


@pytest.fixture(autouse=True)
def _smtp(monkeypatch):
    SENT.clear()
    FakeSMTP.fail_with = None
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)


def _view(db, user_id, key="mailboxes", name="Forwarding outside", query=FORWARDING) -> int:
    return db.execute(
        "INSERT INTO saved_views (user_id, report_key, name, query, created_at)"
        " VALUES (?, ?, ?, ?, '2026-10-01T00:00:00Z')",
        (user_id, key, name, query),
    ).lastrowid


def _attachments(msg) -> dict[str, bytes]:
    return {p.get_filename(): p.get_payload(decode=True) for p in msg.iter_attachments()}


def _csv(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))


def _hook(db, vault, on_reports=True, fmt="teams"):
    alerts.save_settings(
        db,
        {
            "alerts.enabled": "1",
            "alerts.format": fmt,
            "alerts.url_enc": vault.encrypt(HOOK),
            "alerts.on_reports": "1" if on_reports else "0",
        },
    )


def _schedule_from_form(client, tenant_id, **data):
    return client.post(f"/tenants/{tenant_id}/schedules/new", data=FORM | data)


# -- One tenant -------------------------------------------------------------------------


def test_a_tenant_schedule_sends_a_saved_view_with_its_filters(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    _smtp_ready(db)
    me = make_user(db, "analyst", role="analyst", tenants=[alpha])
    login(client, "analyst")
    view_id = _view(db, me)
    contact = delivery.add_contact(db, alpha, "IT", "it@alpha.com")
    page = client.get(f"/tenants/{alpha}/schedules/new").get_data(as_text=True)
    assert "Forwarding outside" in page and f'value="s{view_id}"' in page

    resp = _schedule_from_form(
        client, alpha, views=[f"s{view_id}"], **{f"view_format_s{view_id}": "csv"}, contacts=[str(contact)]
    )
    assert resp.status_code == 302
    sid = db.execute("SELECT id FROM email_schedules").fetchone()[0]
    (saved,) = delivery.schedule_views(db, sid)
    assert (saved.report_key, saved.name, saved.query, saved.format, saved.saved_view_id) == (
        "mailboxes",
        "Forwarding outside",
        FORWARDING,
        "csv",
        view_id,
    )
    listing = client.get(f"/tenants/{alpha}/schedules").get_data(as_text=True)
    assert f"{delivery.view_report_title('mailboxes')}: Forwarding outside" in listing

    client.post(f"/schedules/{sid}/send")
    ((_, _, msg, _),) = sends()
    files = _attachments(msg)
    name = "mailboxes-forwarding-outside-alpha-ltd-forwarding.csv"
    assert list(files) == [name]  # only the view: no review was ticked
    tenant = db.execute("SELECT * FROM tenants WHERE id = ?", (alpha,)).fetchone()
    expected = data_reports.section_table(
        views.apply(data_reports.build(db, tenant, "mailboxes"), views.View.parse(FORWARDING)).sections[0]
    )
    rows = _csv(files[name])
    assert rows[0] == expected.columns and rows[1:] and len(rows) - 1 == len(expected.rows)
    assert all(r[expected.columns.index("Outside the organisation")] == "Yes" for r in rows[1:])
    assert "Forwarding outside" in msg.get_body(("html",)).get_content()
    # The client's report history keeps the file under the view's name.
    issued = db.execute("SELECT kind, title, filename FROM issued_reports").fetchall()
    assert [tuple(r) for r in issued] == [
        ("view:mailboxes", delivery.view_report_title("mailboxes") + ": Forwarding outside", name)
    ]


def test_pdf_and_xlsx_views_and_the_findings_page(app, db, two_tenants, vault, cfg):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    _smtp_ready(db)
    sid = db.execute(
        "INSERT INTO email_schedules (kind, tenant_id, reports, frequency, hour, created_at)"
        " VALUES ('report', ?, '[\"review\"]', 'monthly', 8, '2026-01-01T00:00:00Z')",
        (alpha,),
    ).lastrowid
    delivery.set_recipients(db, "schedule", sid, [delivery.add_contact(db, alpha, "", "it@alpha.com")])
    delivery.set_schedule_views(
        db,
        sid,
        [
            delivery.ScheduledView("mailboxes", "Forwarding outside", FORWARDING, "pdf"),
            delivery.ScheduledView("findings", "Critical", "f=open-findings/flag:critical", "xlsx"),
        ],
    )
    assert delivery.send_schedule(app, db, vault, db.execute("SELECT * FROM email_schedules").fetchone()) is None
    ((_, _, msg, _),) = sends()
    names = list(_attachments(msg))
    pdf = "mailboxes-forwarding-outside-alpha-ltd." + ("pdf" if pdf_available() else "xlsx")
    assert names[-2:] == [pdf, "findings-critical-alpha-ltd.xlsx"]
    assert names[0].startswith("security-review-alpha-ltd")  # the reports first, then the views


def test_only_your_own_views_can_be_picked(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    other = make_user(db, "other", role="analyst", all_tenants=True)
    theirs = _view(db, other)
    login(client, "admin")
    assert f'value="s{theirs}"' not in client.get(f"/tenants/{alpha}/schedules/new").get_data(as_text=True)
    resp = _schedule_from_form(client, alpha, views=[f"s{theirs}", "k1", "sx"], new_email="it@alpha.com")
    assert "Choose at least one report or saved view" in resp.get_data(as_text=True)
    assert db.execute("SELECT COUNT(*) FROM email_schedule_views").fetchone()[0] == 0
    bad = _schedule_from_form(
        client, alpha, reports="review", views=["s1"], view_format_s1="docx", new_email="it@alpha.com"
    )
    assert bad.status_code == 400


def test_saving_the_view_again_updates_the_schedule_and_deleting_it_keeps_a_copy(client, db, two_tenants):
    alpha, _ = two_tenants
    me = make_user(db, "admin")
    login(client, "admin")
    view_id = _view(db, me)
    contact = delivery.add_contact(db, alpha, "IT", "it@alpha.com")
    _schedule_from_form(client, alpha, views=[f"s{view_id}"], contacts=[str(contact)])
    sid = db.execute("SELECT id FROM email_schedules").fetchone()[0]
    client.post(
        "/reports/mailboxes/views",
        data={"tenant_id": alpha, "name": "Forwarding outside", "query": "sort=forwarding/mailbox:asc"},
    )
    assert delivery.schedule_views(db, sid)[0].query == views.View.parse("sort=forwarding/mailbox:asc").query()

    client.post(f"/views/{view_id}/delete")
    (kept,) = delivery.schedule_views(db, sid)
    assert kept.saved_view_id is None and kept.query == views.View.parse("sort=forwarding/mailbox:asc").query()
    page = client.get(f"/schedules/{sid}/edit").get_data(as_text=True)
    assert "its saved view was deleted" in page and f'value="k{kept.id}" checked' in page
    # Saving the form keeps the copy, in the format chosen now.
    client.post(
        f"/schedules/{sid}/edit",
        data=FORM | {"views": [f"k{kept.id}"], f"view_format_k{kept.id}": "xlsx", "contacts": [str(contact)]},
    )
    (again,) = delivery.schedule_views(db, sid)
    assert (again.name, again.format) == ("Forwarding outside", "xlsx")


def test_views_menu_offers_to_schedule_the_current_view(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    me = make_user(db, "admin")
    login(client, "admin")
    view_id = _view(db, me)
    page = client.get(f"/tenants/{alpha}/data/mailboxes?{FORWARDING}").get_data(as_text=True)
    assert f"/tenants/{alpha}/schedules/new?view={view_id}" in page
    form = client.get(f"/tenants/{alpha}/schedules/new?view={view_id}").get_data(as_text=True)
    assert f'value="s{view_id}" checked' in form
    assert 'value="review" checked' not in form


# -- Every tenant, or a group --------------------------------------------------------------


def test_views_email_stacks_a_groups_tenants_for_the_team_only(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    seed_tenant(db, bravo)
    _smtp_ready(db)
    me = make_user(db, "admin")
    login(client, "admin")
    view_id = _view(db, me)
    findings_view = _view(db, me, key="findings", name="Critical", query="f=open-findings/flag:critical")
    group = tenant_groups.create(db, {"name": "Tier 1", "category": "", "description": ""}, "admin")
    tenant_groups.set_members(db, group, [alpha])
    team = delivery.add_contact(db, None, "Me", "me@msp.com")
    client_contact = delivery.add_contact(db, alpha, "IT", "it@alpha.com")
    form = client.get("/emails/views/new").get_data(as_text=True)
    assert f'value="s{view_id}"' in form and f'value="s{findings_view}"' not in form  # no all-tenant findings

    resp = client.post(
        "/emails/views/new",
        data=FORM
        | {
            "group_id": str(group),
            "views": [f"s{view_id}"],
            f"view_format_s{view_id}": "csv",
            "contacts": [str(team), str(client_contact)],
        },
    )
    assert resp.status_code == 302
    s = db.execute("SELECT * FROM email_schedules").fetchone()
    assert s["kind"] == "digest" and json.loads(s["reports"]) == ["views"] and s["group_id"] == group
    assert [c["email"] for c in delivery.recipients(db, "schedule", s["id"])] == ["me@msp.com"]
    overview = client.get("/emails").get_data(as_text=True)
    assert "Forwarding outside · Tier 1" in overview

    client.post(f"/schedules/{s['id']}/send")
    ((_, _, msg, to_addrs),) = sends()
    assert to_addrs == ["me@msp.com"] and "Tier 1" in msg["Subject"] and "Forwarding outside" in msg["Subject"]
    files = _attachments(msg)
    rows = _csv(files["mailboxes-forwarding-outside-tier-1-forwarding.csv"])
    assert rows[0][0] == "Tenant" and rows[1:] and {r[0] for r in rows[1:]} == {"Alpha Ltd"}
    assert db.execute("SELECT COUNT(*) FROM issued_reports").fetchone()[0] == 0  # not a client's report
    preview = client.get(f"/schedules/{s['id']}/preview/body").get_data(as_text=True)
    assert "Saved views: Tier 1" in preview and "Bravo" not in preview


def test_views_email_needs_a_view_and_an_admin(client, db, two_tenants):
    make_user(db, "admin")
    make_user(db, "analyst", role="analyst", all_tenants=True)
    login(client, "admin")
    team = delivery.add_contact(db, None, "Me", "me@msp.com")
    resp = client.post("/emails/views/new", data=FORM | {"contacts": [str(team)]})
    assert "Choose at least one saved view" in resp.get_data(as_text=True)
    client.post("/logout")
    login(client, "analyst")
    assert client.get("/emails/views/new").status_code == 403


# -- Report ready, to Teams -------------------------------------------------------------------


@responses.activate
def test_report_ready_posts_a_link_never_the_files(client, db, two_tenants, vault):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    _smtp_ready(db)
    make_user(db, "admin")
    login(client, "admin")
    responses.post(HOOK, status=202)
    sid = db.execute(
        "INSERT INTO email_schedules (kind, tenant_id, reports, frequency, hour, created_at)"
        " VALUES ('report', ?, '[\"review\"]', 'monthly', 8, '2026-01-01T00:00:00Z')",
        (alpha,),
    ).lastrowid
    delivery.set_recipients(db, "schedule", sid, [delivery.add_contact(db, alpha, "IT", "it@alpha.com")])

    _hook(db, vault, on_reports=False)
    client.post(f"/schedules/{sid}/send")
    assert len(sends()) == 1 and not responses.calls  # off unless the admin turns it on

    _hook(db, vault)
    client.post(f"/schedules/{sid}/send")
    (call,) = responses.calls
    body = call.request.body.decode()
    card = json.loads(body)["attachments"][0]["content"]
    title = card["body"][0]["text"]
    assert title.startswith("The ") and title.endswith(" report for Alpha Ltd is ready")
    assert card["actions"] == [
        {
            "type": "Action.OpenUrl",
            "title": "Open the report history",
            "url": f"http://localhost:8000/tenants/{alpha}/report-history",
        }
    ]
    assert card["body"][1]["text"] == "Emailed to the client: Monthly Security & Licensing Review."
    assert "security-review-alpha-ltd" not in body  # named, never attached
    assert "it@alpha.com" not in body and "base64" not in body and len(body) < 4000
    sent = db.execute("SELECT detail FROM audit_log WHERE action = 'alert_sent'").fetchone()
    assert sent["detail"].startswith("report.ready")


def test_the_link_only_opens_for_people_granted_the_tenant(client, db, two_tenants):
    alpha, bravo = two_tenants
    make_user(db, "client", role="client", tenants=[bravo])
    login(client, "client")
    assert client.get(f"/tenants/{alpha}/report-history").status_code == 404
    client.post("/logout")
    assert client.get(f"/tenants/{alpha}/report-history").status_code == 302  # to sign-in


@responses.activate
def test_published_month_end_reports_post_too(client, db, two_tenants, vault):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    responses.post(HOOK, status=202)
    _hook(db, vault, fmt="generic")
    delivery.add_contact(db, alpha, "IT", "it@alpha.com")
    _plan(client, [alpha], mode="auto", delivery_="portal", include_xlsx="")
    client.post(f"/reporting/{PERIOD}/{alpha}/approve")
    work(client)
    assert month_end.state_of(db, alpha, PERIOD) == "sent" and sends() == []
    (call,) = responses.calls
    posted = json.loads(call.request.body)
    assert posted["event"] == "report.ready" and posted["emailed"] is False and posted["period"] == PERIOD
    assert posted["url"].endswith(f"/tenants/{alpha}/report-history")
    assert posted["text"] == "Published to the client portal: Monthly Security & Licensing Review."
    assert posted["reports"] == ["Monthly Security & Licensing Review"]


def test_ready_titles():
    assert alerts.ready_title("Contoso", "October 2026") == "The October 2026 report for Contoso is ready"
    assert alerts.ready_title("Contoso", "Q4 2026") == "The Q4 2026 report for Contoso is ready"
    assert alerts.ready_title("Contoso", "Week 41, 2026") == "The report for Contoso for week 41, 2026 is ready"


@responses.activate
def test_settings_page_turns_it_on_and_sends_a_sample(client, db, vault):
    make_user(db, "admin")
    login(client, "admin")
    responses.post(HOOK, status=202)
    client.post(
        "/admin/alerts",
        data={"enabled": "1", "format": "teams", "min_severity": "high", "url": HOOK, "on_reports": "1"},
    )
    assert alerts.settings(db)["alerts.on_reports"] == "1"
    assert (
        "reports=1"
        in db.execute("SELECT detail FROM audit_log WHERE action = 'alert_settings_updated'").fetchone()["detail"]
    )
    client.post("/admin/alerts/test", data={"sample": "report"})
    card = json.loads(responses.calls[0].request.body)["attachments"][0]["content"]
    assert "report for Contoso (example) is ready" in card["body"][0]["text"]
