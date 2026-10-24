"""Autotask PSA: the connection, each client's company, and a ticket for the alerts the MSP chooses."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import responses

from officesentry import alerts, autotask, store
from officesentry.db import utcnow
from tests.conftest import login, make_user
from tests.test_alerts import add_finding, add_run, audit_rows, public_dns  # noqa: F401

API = "https://webservices5.autotask.net/ATServicesRest/"
WEB = "https://ww5.autotask.net/"
TICKETS = f"{API}v1.0/Tickets"
NOTES = f"{API}v1.0/TicketNotes"
SECRET = "AtSecret*4kq9"
CODE = "TRACKINGID12345"
CHOICES = {
    "status": [[1, "New", True], [5, "Complete", False], [8, "In Progress", False]],
    "priority": [[4, "Critical", False], [1, "High", False], [2, "Medium", True], [3, "Low", False]],
    "queueID": [[29682833, "Monitoring Alert", False], [29683354, "Level I Support", False]],
    "ticketType": [[1, "Service Request", True], [5, "Alert", False]],
    "source": [[8, "Monitoring Alert", False], [2, "Phone", True]],
    "note.noteType": [[1, "Task Summary", True], [13, "Workflow Rule Note", False]],
    "note.publish": [[1, "All Autotask Users", True], [2, "Internal Project Team", False]],
}


@pytest.fixture
def alerting(cfg, vault):
    return alerts.Alerting(cfg.base_url, vault)


def connect(db, vault, **values):
    """Autotask connected, checked and turned on, with tickets for every alert it offers."""
    autotask.save_settings(
        db,
        {
            "autotask.enabled": "1",
            "autotask.username": "officesentry@msp.example",
            "autotask.secret_enc": vault.encrypt(SECRET),
            "autotask.code_enc": vault.encrypt(CODE),
            "autotask.zone_api": API,
            "autotask.zone_web": WEB,
            "autotask.checked_at": utcnow(),
            "autotask.choices": json.dumps(CHOICES),
            "autotask.status": "1",
            "autotask.priority_critical": "4",
            "autotask.priority_high": "1",
            "autotask.priority_medium": "2",
            "autotask.priority_low": "3",
            "autotask.queue": "29682833",
            **values,
        },
    )


def companies(db, *rows):
    db.executemany("INSERT INTO autotask_company_list (company_id, name, company_type) VALUES (?, ?, 1)", rows)


def created(rsps, i=-1) -> dict:
    return json.loads(rsps.calls[i].request.body)


def tickets(db) -> list:
    return db.execute("SELECT * FROM autotask_tickets ORDER BY id").fetchall()


def accepts_tickets(ticket_id=9001, number="T20261024.0001"):
    responses.post(TICKETS, json={"itemId": ticket_id})
    responses.get(f"{TICKETS}/{ticket_id}", json={"item": {"id": ticket_id, "ticketNumber": number}})


# -- Connecting ------------------------------------------------------------------


def _fields(*fields):
    return {
        "fields": [
            {
                "name": name,
                "isPickList": True,
                "picklistValues": [
                    {"value": str(v), "label": label, "isDefaultValue": default, "sortOrder": i, "isActive": True}
                    for i, (v, label, default) in enumerate(values)
                ]
                + [{"value": "99", "label": "Retired", "isDefaultValue": False, "sortOrder": 0, "isActive": False}],
            }
            for name, values in fields
        ]
        + [{"name": "title", "isPickList": False}]
    }


@responses.activate
def test_checking_finds_the_zone_and_reads_choices_and_companies(db, vault):
    autotask.save_settings(
        db,
        {
            "autotask.username": "officesentry@msp.example",
            "autotask.secret_enc": vault.encrypt(SECRET),
            "autotask.code_enc": vault.encrypt(CODE),
        },
    )
    zone = responses.get(
        autotask.ZONE_LOOKUP,
        json={"zoneName": "America East 3", "url": API, "webUrl": WEB, "ci": 5},
        match=[responses.matchers.query_param_matcher({"user": "officesentry@msp.example"})],
    )
    ticket_fields = [(f, CHOICES[f]) for f in ("status", "priority", "queueID", "ticketType", "source")]
    responses.get(f"{TICKETS}/entityInformation/fields", json=_fields(*ticket_fields))
    responses.get(
        f"{NOTES}/entityInformation/fields",
        json=_fields(("noteType", CHOICES["note.noteType"]), ("publish", CHOICES["note.publish"])),
    )
    second = f"{API}v1.0/Companies/query/next?paging=abc"
    responses.get(
        f"{API}v1.0/Companies/query",
        json={
            "items": [{"id": 0, "companyName": "Harbour IT", "companyType": 5}, {"id": 174, "companyName": "Alpha"}],
            "pageDetails": {"count": 2, "nextPageUrl": second},
        },
    )
    responses.get(second, json={"items": [{"id": 175, "companyName": "Bravo Inc"}], "pageDetails": {"count": 1}})

    checked = autotask.check(db, vault)

    assert (checked.companies, checked.queues) == (3, 2)
    assert zone.call_count == 1 and "Secret" not in responses.calls[0].request.headers  # the lookup needs none
    sent = responses.calls[1].request.headers
    assert (sent["Username"], sent["Secret"], sent["ApiIntegrationCode"]) == ("officesentry@msp.example", SECRET, CODE)
    query = json.loads(responses.calls[3].request.params["search"])
    assert query == {
        "filter": [{"op": "eq", "field": "isActive", "value": True}],
        "IncludeFields": ["id", "companyName", "companyType"],
    }
    s = autotask.settings(db)
    assert (s["autotask.zone_api"], s["autotask.zone_web"]) == (API, WEB)
    assert autotask.choices(s) == {k: v for k, v in CHOICES.items()}  # the inactive choice is left out
    # What nobody chose yet: Autotask's default status and the priorities named like each severity.
    assert s["autotask.status"] == "1"
    assert [s[f"autotask.priority_{p}"] for p in autotask.PRIORITY_SEVERITIES] == ["4", "1", "2", "3"]
    assert [tuple(r) for r in autotask.company_list(db)] == [
        (174, "Alpha", None),
        (175, "Bravo Inc", None),
        (0, "Harbour IT", 5),
    ]


@responses.activate
def test_a_zone_outside_autotask_is_refused(db, vault):
    autotask.save_settings(
        db,
        {
            "autotask.username": "u@x",
            "autotask.secret_enc": vault.encrypt("s"),
            "autotask.code_enc": vault.encrypt("c"),
        },
    )
    responses.get(autotask.ZONE_LOOKUP, json={"url": "https://evil.example/ATServicesRest/", "webUrl": WEB})
    with pytest.raises(autotask.AutotaskError, match="outside autotask.net"):
        autotask.check(db, vault)
    responses.get(autotask.ZONE_LOOKUP, json={"url": "http://webservices5.autotask.net/x/", "webUrl": WEB})
    with pytest.raises(autotask.AutotaskError, match="outside autotask.net"):
        autotask.check(db, vault)
    assert len(responses.calls) == 2  # nothing was sent anywhere else
    assert autotask.settings(db)["autotask.zone_api"] == ""


@responses.activate
def test_a_refused_api_user_says_why_without_its_secret(db, vault):
    connect(db, vault)
    responses.get(autotask.ZONE_LOOKUP, json={"url": API, "webUrl": WEB})
    responses.get(f"{TICKETS}/entityInformation/fields", status=401)
    with pytest.raises(autotask.AutotaskError, match="HTTP 401") as caught:
        autotask.check(db, vault)
    assert SECRET not in str(caught.value) and CODE not in str(caught.value)


# -- Tickets ---------------------------------------------------------------------


@responses.activate
def test_new_findings_make_a_ticket_in_the_clients_company(db, vault, alerting, two_tenants):
    alpha, _ = two_tenants
    connect(db, vault, **{"autotask.min_severity": "medium", "autotask.due_hours": "24"})
    companies(db, (174, "Alpha Ltd"))
    autotask.set_company(db, alpha, {"company_id": 174, "name": "Alpha Ltd"}, "admin")
    accepts_tickets()
    ids = [add_finding(db, alpha, severity=s, object_id=s) for s in ("low", "high", "medium")]
    before = datetime.now(UTC)

    assert alerts.after_sync(db, alerting, store.get_tenant(db, alpha), {"opened": ids}) is True

    ticket = created(responses, 0)
    due = datetime.strptime(ticket.pop("dueDateTime"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert before + timedelta(hours=23, minutes=59) < due < before + timedelta(hours=24, minutes=1)
    description = ticket.pop("description")
    assert ticket == {
        "companyID": 174,
        "title": "Alpha Ltd: 2 new findings",  # the low one is below Autotask's own severity
        "status": 1,
        "priority": 1,  # high, the worst of them
        "queueID": 29682833,
    }
    assert description.splitlines() == [
        "Office Sentry found 2 new or returning issues rated medium or worse.",
        "",
        f"Open in Office Sentry: {alerting.base_url.rstrip('/')}/tenants/{alpha}",
        "",
        "- High: Every admin has MFA registered (ann@x.com)",
        "  No MFA method registered",
        "- Medium: Every admin has MFA registered (ann@x.com)",
        "  No MFA method registered",
        "",
        "Created by Office Sentry.",
    ]
    (row,) = tickets(db)
    assert (row["ticket_id"], row["ticket_number"], row["company_id"], row["tenant_id"]) == (
        9001,
        "T20261024.0001",
        174,
        alpha,
    )
    assert row["error"] is None and row["retry_at"] is None and row["attempts"] == 1
    (sent,) = audit_rows(db, "alert_sent")
    assert sent["detail"] == "findings.new via autotask, 2 findings"
    assert autotask.ticket_link(autotask.settings(db), 9001) == (
        f"{WEB}Autotask/AutotaskExtend/ExecuteCommand.aspx?Code=OpenTicketDetail&TicketID=9001"
    )


@responses.activate
def test_a_client_without_a_company_uses_your_own_or_none(db, vault, alerting, two_tenants):
    alpha, _ = two_tenants
    connect(db, vault)
    accepts_tickets()
    fid = add_finding(db, alpha, severity="critical")
    tenant = store.get_tenant(db, alpha)
    assert alerts.after_sync(db, alerting, tenant, {"opened": [fid]}) is True
    assert created(responses, 0)["companyID"] == autotask.OWN_COMPANY == 0
    assert created(responses, 0)["priority"] == 4

    autotask.save_settings(db, {"autotask.unmapped": "skip"})
    calls = len(responses.calls)
    fid = add_finding(db, alpha, severity="critical", object_id="u2")
    assert alerts.after_sync(db, alerting, tenant, {"opened": [fid]}) is None
    assert len(responses.calls) == calls and len(tickets(db)) == 1


@responses.activate
def test_only_the_chosen_alerts_make_tickets(db, vault, alerting, two_tenants):
    alpha, _ = two_tenants
    connect(db, vault, **{"autotask.events": "collection.failed"})
    fid = add_finding(db, alpha, severity="critical")
    assert alerts.after_sync(db, alerting, store.get_tenant(db, alpha), {"opened": [fid]}) is None
    autotask.save_settings(db, {"autotask.enabled": "0", "autotask.events": ",".join(autotask.EVENTS)})
    assert not alerts.wanted(db)
    assert alerts.after_sync(db, alerting, store.get_tenant(db, alpha), {"opened": [fid]}) is None
    assert not responses.calls and not tickets(db)


@responses.activate
def test_a_failing_collection_makes_one_ticket_and_its_recovery_a_note(db, vault, alerting, two_tenants):
    alpha, _ = two_tenants
    connect(db, vault)
    alerts.save_settings(db, {"alerts.on_failures": "1", "alerts.on_recovery": "1"})
    accepts_tickets()
    responses.post(NOTES, json={"itemId": 77})
    add_run(db, alpha, "succeeded")
    first = add_run(db, alpha, "failed", error="Graph said no")
    assert alerts.after_run(db, alerting, first, "failed") is True
    ticket = created(responses, 0)
    assert ticket["title"] == "Alpha Ltd: Users collection failed" and ticket["priority"] == 2  # medium

    # Failing again the next night isn't a new failure; a ticket for the same problem stays one either way.
    message = alerts.Message(
        "collection.failed",
        "Alpha Ltd: Users collection failed",
        "Again",
        "x",
        {"id": alpha, "name": "Alpha Ltd"},
        severity="medium",
        key=f"collection:{alpha}:users",
    )
    assert alerts.deliver(db, alerting, message) is None
    assert len([c for c in responses.calls if c.request.method == "POST"]) == 1

    fixed = add_run(db, alpha, "succeeded")
    assert alerts.after_run(db, alerting, fixed, "succeeded") is True
    note = created(responses)
    assert note["ticketID"] == 9001 and (note["noteType"], note["publish"]) == (1, 1)
    assert note["title"] == "Alpha Ltd: Users collection working again"
    assert "Added by Office Sentry." in note["description"]
    (row,) = tickets(db)
    assert row["resolved_at"] is not None and row["alert_key"] == f"collection:{alpha}:users"

    # Failing again after that is a new problem: a new ticket.
    again = add_run(db, alpha, "failed", error="Graph said no")
    assert alerts.after_run(db, alerting, again, "failed") is True
    assert len(tickets(db)) == 2


@responses.activate
def test_problems_with_office_sentry_go_to_your_own_company_once(db, vault, alerting):
    connect(db, vault)
    accepts_tickets()
    problem = alerts.Message(
        "system.backups",
        "Last night's backup failed",
        "The disk is full.",
        "https://os.example/admin/status",
        severity="high",
        key="system:backups",
    )
    assert alerts.deliver(db, alerting, problem) is True
    assert (created(responses, 0)["companyID"], created(responses, 0)["priority"]) == (0, 1)
    assert alerts.deliver(db, alerting, problem) is None  # the daily reminder: still the one ticket
    assert len(tickets(db)) == 1


@responses.activate
def test_a_ticket_autotask_didnt_take_is_tried_again(db, vault, alerting, two_tenants):
    alpha, _ = two_tenants
    connect(db, vault)
    responses.post(TICKETS, status=503)
    fid = add_finding(db, alpha, severity="high")
    assert alerts.after_sync(db, alerting, store.get_tenant(db, alpha), {"opened": [fid]}) is False
    (failed,) = audit_rows(db, "alert_failed")
    assert failed["detail"] == "findings.new via autotask: Autotask answered HTTP 503. Trying again in 5 minutes."
    (row,) = tickets(db)
    assert row["ticket_id"] is None and row["attempts"] == 1 and row["retry_at"] is not None

    now = datetime.now(UTC)
    assert autotask.retry_due(db, vault, now) == 0  # not due yet
    responses.replace(responses.POST, TICKETS, json={"itemId": 9002})
    responses.get(f"{TICKETS}/9002", json={"item": {"ticketNumber": "T20261024.0002"}})
    assert autotask.retry_due(db, vault, now + timedelta(minutes=6)) == 1
    (row,) = tickets(db)
    assert (row["ticket_id"], row["ticket_number"], row["retry_at"], row["attempts"]) == (
        9002,
        "T20261024.0002",
        None,
        2,
    )
    assert audit_rows(db, "alert_sent")[-1]["detail"] == "findings.new via autotask, tried again: T20261024.0002"
    # The payload sent again is the one recorded: no secret in it.
    assert SECRET not in row["payload"] and CODE not in row["payload"]


@responses.activate
def test_a_ticket_autotask_refused_isnt_tried_again(db, vault, alerting, two_tenants):
    alpha, _ = two_tenants
    connect(db, vault)
    responses.post(TICKETS, status=500, json={"errors": ["Missing Required Field: dueDateTime."]})
    fid = add_finding(db, alpha, severity="high")
    assert alerts.after_sync(db, alerting, store.get_tenant(db, alpha), {"opened": [fid]}) is False
    (failed,) = audit_rows(db, "alert_failed")
    assert (
        failed["detail"]
        == "findings.new via autotask: Autotask answered HTTP 500: Missing Required Field: dueDateTime."
    )
    (row,) = tickets(db)
    assert row["retry_at"] is None and row["error"].endswith("dueDateTime.")


def test_retries_stop_when_autotask_is_turned_off(db, vault):
    connect(db, vault)
    db.execute(
        "INSERT INTO autotask_tickets (event, title, company_id, payload, attempts, retry_at, created_at)"
        " VALUES ('system.backups', 'x', 0, '{}', 1, '2000-01-01T00:00:00Z', ?)",
        (utcnow(),),
    )
    autotask.save_settings(db, {"autotask.enabled": "0"})
    assert autotask.retry_due(db, vault) == 0
    (row,) = tickets(db)
    assert row["retry_at"] is None and row["error"] == "Not sent: Autotask was turned off."


def test_old_ticket_records_are_pruned(db):
    old = (datetime.now(UTC) - timedelta(days=autotask.KEEP_DAYS + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for created_at in (old, utcnow()):
        db.execute(
            "INSERT INTO autotask_tickets (event, title, company_id, payload, created_at)"
            " VALUES ('test', 'x', 0, '{}', ?)",
            (created_at,),
        )
    assert autotask.prune(db) == 1 and len(tickets(db)) == 1


def test_names_match_without_ltd_and_punctuation(db, two_tenants):
    alpha, bravo = two_tenants
    companies(db, (174, "ALPHA LIMITED"), (175, "Bravo, Inc."), (176, "Bravo Inc"))
    found = autotask.suggestions(db, db.execute("SELECT * FROM tenants").fetchall())
    assert found[alpha]["company_id"] == 174
    assert bravo not in found  # two companies match: nobody guesses


def test_deleting_a_client_deletes_its_company_and_tickets(db, two_tenants):
    from officesentry import lifecycle

    alpha, _ = two_tenants
    autotask.set_company(db, alpha, {"company_id": 174, "name": "Alpha Ltd"}, "admin")
    db.execute(
        "INSERT INTO autotask_tickets (tenant_id, event, title, company_id, payload, created_at)"
        " VALUES (?, 'findings.new', 'x', 174, '{}', ?)",
        (alpha, utcnow()),
    )
    lifecycle.delete_tenant(db, store.get_tenant(db, alpha), "admin")
    assert db.execute("SELECT COUNT(*) FROM autotask_companies").fetchone()[0] == 0
    assert not tickets(db)


# -- The settings page ---------------------------------------------------------------


@responses.activate
def test_saving_new_credentials_checks_them_and_keeps_them_encrypted(client, db, vault):
    make_user(db, "admin")
    login(client, "admin")
    responses.get(autotask.ZONE_LOOKUP, json={"url": API, "webUrl": WEB})
    responses.get(
        f"{TICKETS}/entityInformation/fields",
        json=_fields(("status", CHOICES["status"]), ("priority", CHOICES["priority"])),
    )
    responses.get(f"{NOTES}/entityInformation/fields", json=_fields())
    responses.get(
        f"{API}v1.0/Companies/query", json={"items": [{"id": 0, "companyName": "Harbour IT"}], "pageDetails": {}}
    )
    form = {
        "username": "officesentry@msp.example",
        "secret": SECRET,
        "code": CODE,
        "events": list(autotask.EVENTS),
        "min_severity": "high",
        "unmapped": "own",
        "enabled": "1",
    }
    html = client.post("/admin/autotask", data=form, follow_redirects=True).get_data(as_text=True)
    assert "Connected to Autotask: 1 active company and 0 queues read." in html
    assert SECRET not in html and CODE not in html
    s = autotask.settings(db)
    assert vault.decrypt(s["autotask.secret_enc"]) == SECRET and vault.decrypt(s["autotask.code_enc"]) == CODE
    assert s["autotask.enabled"] == "1" and s["autotask.zone_api"] == API
    detail = db.execute("SELECT detail FROM audit_log WHERE action = 'autotask_settings_updated'").fetchone()[0]
    assert "changed=user,secret,code" in detail and SECRET not in detail

    # Saving again without them keeps them, and keeps the connection.
    client.post(
        "/admin/autotask",
        data={
            **form,
            "secret": "",
            "code": "",
            "status": "1",
            "priority_critical": "4",
            "priority_high": "1",
            "priority_medium": "2",
            "priority_low": "3",
        },
    )
    assert autotask.settings(db)["autotask.secret_enc"] == s["autotask.secret_enc"]
    assert len([c for c in responses.calls if "zoneInformation" in c.request.url]) == 1

    # A choice Autotask didn't offer is refused.
    assert client.post("/admin/autotask", data={**form, "secret": "", "code": "", "status": "42"}).status_code == 400


def test_tickets_stay_off_until_the_choices_are_made(client, db, vault):
    make_user(db, "admin")
    login(client, "admin")
    form = {"username": "", "events": [], "min_severity": "high", "unmapped": "own", "enabled": "1"}
    html = client.post("/admin/autotask", data=form, follow_redirects=True).get_data(as_text=True)
    assert "Tickets stay off until you choose the connection" in html
    assert autotask.settings(db)["autotask.enabled"] == "0"


def test_clients_are_matched_to_companies(client, db, vault, two_tenants):
    alpha, bravo = two_tenants
    connect(db, vault)
    companies(db, (174, "Alpha Ltd"), (175, "Contoso"), (176, "Contoso"))
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/admin/autotask").get_data(as_text=True)
    assert 'value="Alpha Ltd (#174)"' in page and "Suggested" in page  # matched by name, not saved yet
    assert SECRET not in page and CODE not in page

    html = client.post(
        "/admin/autotask/companies",
        data={f"company-{alpha}": "Alpha Ltd (#174)", f"company-{bravo}": "Contoso", "company-99999": "Contoso (#175)"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "No single Autotask company matches what was typed for Bravo Inc" in html
    assert autotask.mappings(db).keys() == {alpha}

    client.post("/admin/autotask/companies", data={f"company-{bravo}": "Contoso (#176)"})
    assert autotask.mappings(db)[bravo]["company_id"] == 176
    client.post("/admin/autotask/companies", data={f"company-{bravo}": ""})
    assert autotask.mappings(db).keys() == {alpha}
    details = [r["detail"] for r in audit_rows(db, "autotask_companies_mapped")]
    assert details == ["Alpha Ltd: Alpha Ltd", "Bravo Inc: Contoso", "Bravo Inc: none"]


@responses.activate
def test_a_test_ticket_and_trying_a_failed_one_again(client, db, vault, two_tenants):
    alpha, _ = two_tenants
    connect(db, vault)
    make_user(db, "admin")
    login(client, "admin")
    accepts_tickets()
    html = client.post("/admin/autotask/test", follow_redirects=True).get_data(as_text=True)
    assert "Test ticket T20261024.0001 created in your own company." in html
    assert created(responses, 0)["companyID"] == 0 and created(responses, 0)["title"] == "Office Sentry test ticket"

    db.execute(
        "INSERT INTO autotask_tickets (tenant_id, event, title, company_id, payload, attempts, error, created_at)"
        " VALUES (?, 'findings.new', 'Alpha Ltd: 1 new finding', 0, ?, 4, 'Autotask answered HTTP 503.', ?)",
        (alpha, json.dumps({"companyID": 0, "title": "Alpha Ltd: 1 new finding"}), utcnow()),
    )
    failed = db.execute("SELECT id FROM autotask_tickets WHERE ticket_id IS NULL").fetchone()[0]
    page = client.get("/admin/autotask").get_data(as_text=True)
    assert "Try again" in page and "Autotask answered HTTP 503." in page
    html = client.post(f"/admin/autotask/tickets/{failed}/retry", follow_redirects=True).get_data(as_text=True)
    assert "Ticket T20261024.0001 created." in html
    assert db.execute("SELECT ticket_id FROM autotask_tickets WHERE id = ?", (failed,)).fetchone()[0] == 9001
    assert client.post("/admin/autotask/tickets/99999/retry").status_code == 404
