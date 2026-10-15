"""The audit log add-on leaves nothing on at Microsoft, and nothing showing, once it's off: a feed that half
started, or that Microsoft says is off for one content type, is stopped too; a delete asked for while a stop
is queued isn't lost; and the add-on switched off for the install hides its records from people's timelines and
its report from scheduled emails.

Every Microsoft call is simulated with ``responses``.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
import responses

from officesentry import delivery, events, lifecycle, store, tasks
from officesentry.auditlog import addon, feed
from officesentry.reports.file_activity import KEY
from officesentry.reports.profile import timeline as profile_timeline
from officesentry.reports.profile.base import Person
from officesentry.snapshot import TenantSnapshot
from tests.auditdata import TENANT, addon_on, collect, iso, sample
from tests.conftest import login, make_user
from tests.test_bulk_onboarding import _sign_in_works

ROOT = f"https://manage.office.com/api/v1.0/{TENANT}/activity/feed"


def _posts(rsps, path: str) -> list[str]:
    return [
        re.search(r"contentType=([^&]+)", c.request.url).group(1)
        for c in rsps.calls
        if c.request.method == "POST" and c.request.url.startswith(ROOT + path)
    ]


def _stops(app, db, vault, cfg) -> list[str]:
    """Run the queued tasks with Microsoft taking every stop; the content types stopped."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        _sign_in_works(rsps, guid=TENANT)
        rsps.post(re.compile(re.escape(ROOT) + "/subscriptions/stop"), body="")
        rsps.get(f"{ROOT}/subscriptions/list", json=[])
        tasks.drain(app, db, vault, cfg)
        return _posts(rsps, "/subscriptions/stop")


def _stop_task(db):
    return db.execute("SELECT * FROM tasks WHERE kind = 'audit_stop' ORDER BY id DESC").fetchone()


def _half_started(app, client, db, vault, cfg, alpha) -> None:
    """The client approved and the feed's start worked for SharePoint, then Microsoft refused Exchange's (its
    15-minute rule, a throttle, an outage): SharePoint is on at Microsoft, and the client isn't On here."""
    addon_on(db, vault)
    assert client.post(f"/tenants/{alpha}/audit-addon/sent", data={"sent": "1"}).status_code == 302
    addon.client_approved(db, store.get_tenant(db, alpha))
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        _sign_in_works(rsps, guid=TENANT)
        rsps.get(f"{ROOT}/subscriptions/list", json=[])

        def start(request):
            if "Audit.SharePoint" in request.url:
                return 200, {}, json.dumps({"contentType": "Audit.SharePoint", "status": "enabled"})
            return 503, {}, json.dumps({"error": {"code": "AF429", "message": "Too many requests"}})

        rsps.add_callback("POST", re.compile(re.escape(ROOT) + "/subscriptions/start"), callback=start)
        tasks.drain(app, db, vault, cfg)
        assert "Audit.SharePoint" in _posts(rsps, "/subscriptions/start")
    row = addon.row_for(db, alpha)
    assert json.loads(row["content_types"]) == {"Audit.SharePoint": "enabled"} and row["started_at"] is None
    assert addon.feed_left(row) == list(feed.CONTENT_TYPES)  # Exchange's start may have landed too


# -- A feed that half started, or that Microsoft says is half off ---------------------------------------------


def test_a_half_started_feed_is_stopped_when_switched_off(app, client, db, vault, cfg, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    _half_started(app, client, db, vault, cfg, alpha)

    assert client.post(f"/tenants/{alpha}/audit-addon/off").status_code == 302
    assert json.loads(_stop_task(db)["payload"])["stop"] == list(feed.CONTENT_TYPES)
    assert sorted(_stops(app, db, vault, cfg)) == sorted(feed.CONTENT_TYPES)
    row = addon.row_for(db, alpha)
    assert addon.state(row).key == "off" and addon.feed_left(row) == []
    assert json.loads(row["content_types"]) == dict.fromkeys(feed.CONTENT_TYPES, "disabled")


def test_deleting_a_client_whose_feed_half_started_stops_it(app, client, db, vault, cfg, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    _half_started(app, client, db, vault, cfg, alpha)

    with db:
        lifecycle.delete_tenant(db, store.get_tenant(db, alpha), "admin")
    assert json.loads(_stop_task(db)["payload"])["stop"] == list(feed.CONTENT_TYPES)
    assert sorted(_stops(app, db, vault, cfg)) == sorted(feed.CONTENT_TYPES)


def test_a_feed_microsoft_says_is_half_off_has_the_rest_stopped(app, client, db, vault, cfg, two_tenants):
    """The client's admin turned Exchange's feed off: the client is marked Off, and SharePoint's is stopped too
    rather than left on with nobody reading it. Switching on waits for that stop, and deleting the client
    doesn't send it twice."""
    alpha, _ = two_tenants
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    addon_on(db, vault, [alpha], started_at=iso(now - timedelta(hours=3)))
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.get(
            f"{ROOT}/subscriptions/list",
            json=[
                {"contentType": "Audit.SharePoint", "status": "enabled"},
                {"contentType": "Audit.Exchange", "status": "disabled"},
            ],
        )
        status, calls = collect(db, alpha, {}, now, rsps=rsps)
    assert status == "partial" and {c.request.method for c in calls} == {"GET"}  # the collection only reads
    row = addon.row_for(db, alpha)
    assert addon.state(row).key == "off" and "stops the rest of it too" in row["stop_reason"]
    assert json.loads(_stop_task(db)["payload"])["stop"] == ["Audit.SharePoint"]

    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/audit-addon").get_data(as_text=True)
    assert "Office Sentry is stopping the feed at Microsoft" in page
    assert "Switch on again" not in page and ">Switch off</button>" not in page
    resp = client.post(f"/tenants/{alpha}/audit-addon/on", follow_redirects=True)
    assert "still stopping this client" in resp.get_data(as_text=True)
    assert addon.state(addon.row_for(db, alpha)).key == "off"

    with db:
        lifecycle.delete_tenant(db, store.get_tenant(db, alpha), "admin")
    assert db.execute("SELECT COUNT(*) FROM tasks WHERE kind = 'audit_stop'").fetchone()[0] == 1
    assert _stops(app, db, vault, cfg) == ["Audit.SharePoint"]


def test_a_feed_microsoft_says_is_all_off_sends_nothing(db, vault, two_tenants):
    alpha, _ = two_tenants
    addon_on(db, vault, [alpha])
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    assert collect(db, alpha, sample(now), now, status="disabled")[0] == "partial"
    row = addon.row_for(db, alpha)
    assert addon.state(row).key == "off" and "stops the rest" not in row["stop_reason"]
    assert _stop_task(db) is None


# -- "Also delete the events collected" when a stop is already queued ------------------------------------------


def test_delete_what_was_collected_is_kept_when_a_stop_is_already_queued(app, client, db, vault, cfg, two_tenants):
    alpha, _ = two_tenants
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    addon_on(db, vault, [alpha], started_at=iso(now - timedelta(hours=3)))
    assert collect(db, alpha, sample(now), now)[0] == "succeeded"
    assert events.counts(db, alpha)["activity"]
    make_user(db, "admin")
    login(client, "admin")
    assert client.post(f"/tenants/{alpha}/audit-addon/off").status_code == 302
    resp = client.post(f"/tenants/{alpha}/audit-addon/on", follow_redirects=True)
    assert "still stopping this client" in resp.get_data(as_text=True)  # would be stopped by the queued task
    resp = client.post(f"/tenants/{alpha}/audit-addon/off", data={"delete": "1"}, follow_redirects=True)
    assert "deleting what was collected" in resp.get_data(as_text=True)
    assert db.execute("SELECT COUNT(*) FROM tasks WHERE kind = 'audit_stop'").fetchone()[0] == 1
    assert json.loads(_stop_task(db)["payload"])["delete"] is True

    assert sorted(_stops(app, db, vault, cfg)) == sorted(feed.CONTENT_TYPES)  # once each
    assert events.counts(db, alpha)["activity"] == 0
    logged = db.execute(
        "SELECT detail FROM audit_log WHERE action = 'audit_addon_switched_off_client' ORDER BY id DESC"
    ).fetchone()[0]
    assert logged == "and deleted what was collected"


def test_a_delete_asked_for_while_the_stop_is_running_is_refused_not_lost(db, vault, two_tenants):
    alpha, _ = two_tenants
    addon_on(db, vault, [alpha])
    tenant = store.get_tenant(db, alpha)
    addon.switch_off(db, tenant, "admin")
    db.execute("UPDATE tasks SET status = 'running' WHERE kind = 'audit_stop'")
    switched_off = db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'audit_addon_switched_off_client'")
    before = switched_off.fetchone()[0]
    with pytest.raises(addon.AddonError, match="right now"):
        addon.switch_off(db, tenant, "admin", delete=True)
    assert json.loads(_stop_task(db)["payload"])["delete"] is False
    after = db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'audit_addon_switched_off_client'")
    assert after.fetchone()[0] == before  # nothing said it was deleted
    addon.switch_off(db, tenant, "admin")  # without a delete, the running stop already does it


# -- The add-on switched off for the install ------------------------------------------------------------------


def test_switching_the_addon_off_hides_its_records_from_peoples_timelines(db, vault, two_tenants):
    alpha, _ = two_tenants
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    addon_on(db, vault, [alpha], started_at=iso(now - timedelta(hours=3)))
    assert collect(db, alpha, sample(now), now)[0] == "succeeded"

    def person():
        tenant = store.get_tenant(db, alpha)
        return Person(TenantSnapshot(db, tenant), {"_id": "ceo", "userPrincipalName": "ceo@contoso.com"}, now)

    texts = [e.text for e in profile_timeline.timeline(person())]
    assert any(t.startswith("Mail read by admin@contoso.com") for t in texts)
    addon.switch_off(db, store.get_tenant(db, alpha), "admin")  # keeps what was collected
    addon.switch(db, False, "admin")

    texts = [e.text for e in profile_timeline.timeline(person())]
    assert not any(t.startswith("Mail read by") for t in texts)
    assert "audit log records" not in (profile_timeline.timeline_part(person()).note or "")


def test_scheduled_emails_leave_out_the_addons_report_once_its_off(app, db, vault, two_tenants):
    """A schedule made while the add-on was on still names its report and a saved view of it: neither is sent
    once the add-on is switched off for the install, and the rest of the schedule still is."""
    alpha, _ = two_tenants
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    addon_on(db, vault, [alpha], started_at=iso(now - timedelta(hours=3)))
    assert collect(db, alpha, sample(now), now)[0] == "succeeded"
    sid = db.execute(
        "INSERT INTO email_schedules (kind, tenant_id, reports, include_xlsx, frequency, hour, created_at)"
        " VALUES ('report', ?, ?, 1, 'monthly', 8, '2026-01-01T00:00:00Z')",
        (alpha, json.dumps([f"data:{KEY}", "data:mailboxes"])),
    ).lastrowid
    view = delivery.ScheduledView(KEY, "Deleted", "", "xlsx")
    delivery.set_schedule_views(db, sid, [view])

    def sent() -> tuple[list[str], list[str], list[str]]:
        schedule, tenant = db.execute("SELECT * FROM email_schedules").fetchone(), store.get_tenant(db, alpha)
        names = delivery.build_report_email(app, db, schedule, tenant, [], now=now, attach=False).attachment_names
        keys = json.loads(schedule["reports"])
        files = delivery._attachments(
            app, db, tenant, keys, True, now, chosen_views=delivery.tenant_views(db, sid, tenant)
        )
        across = [r.view.report_key for r in delivery.views_data(db, [view], None, now)["views"]]
        return names, [name for name, _, _ in files], across

    names, files, across = sent()
    assert (
        names
        == files
        == [
            "files-and-mailboxes-alpha-ltd.xlsx",
            "mailboxes-alpha-ltd.xlsx",
            "files-and-mailboxes-deleted-alpha-ltd.xlsx",
        ]
    )
    assert across == [KEY]

    addon.switch_off(db, store.get_tenant(db, alpha), "admin")  # keeps what was collected
    addon.switch(db, False, "admin")
    assert sent() == (["mailboxes-alpha-ltd.xlsx"], ["mailboxes-alpha-ltd.xlsx"], [])
