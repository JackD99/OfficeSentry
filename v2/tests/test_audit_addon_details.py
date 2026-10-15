"""The audit log add-on's details: hours lost before a late first read, a stop refused at the MSP's end, a client
that removed the app being switched on again, approvals not confirmed yet, paused and archived clients, mail flow
rules that send mail elsewhere, what the report says once the records are deleted, and names ending in s.

Every Microsoft call is simulated with ``responses``.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta

import pytest
import responses

from officesentry import events, store
from officesentry.auditlog import addon, feed
from officesentry.reports.file_activity import KEY
from officesentry.snapshot import TenantSnapshot
from tests.auditdata import TENANT, _ex, _params, addon_on, collect, iso, sample
from tests.conftest import login, make_user
from tests.test_audit_addon_stopping import Microsoft

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
EXPIRED = (
    "AADSTS700027: The certificate with identifier used to sign the client assertion is expired on application."
    " Trace ID: x Correlation ID: y Timestamp: 2026-10-12 09:00:00Z"
)
GONE = "AADSTS700016: Application with identifier 'abababab' was not found in the directory 'Alpha'."
ALL_ON = dict.fromkeys(feed.CONTENT_TYPES, "enabled")
ALL_OFF = dict.fromkeys(feed.CONTENT_TYPES, "disabled")


def _admin(client, db) -> None:
    make_user(db, "admin")
    login(client, "admin")


# -- Collecting -----------------------------------------------------------------------------------------------


def test_a_first_read_long_after_the_start_says_which_hours_were_lost(db, vault, two_tenants):
    """Microsoft lists 7 days back: a feed first read 9 days after it started has lost its first 2 days, and
    the report says so, rather than every night saying they haven't been read yet."""
    alpha, _ = two_tenants
    started = NOW - timedelta(days=9)
    addon_on(db, vault, [alpha], started_at=iso(started))
    assert collect(db, alpha, sample(NOW), NOW)[0] == "succeeded"
    assert collect(db, alpha, sample(NOW), NOW + timedelta(days=1))[0] == "succeeded"
    lost = TenantSnapshot(db, store.get_tenant(db, alpha)).objects("audit_activity", "audit_feed")[0]["lost"]
    assert sorted(c for c, _, _ in lost) == sorted(feed.CONTENT_TYPES)
    assert all(lo == iso(started) and hi < iso(NOW - timedelta(days=6)) for _, lo, hi in lost)


@pytest.mark.parametrize("parameter", ["RedirectMessageTo", "BlindCopyTo", "CopyTo", "AddToRecipients"])
def test_a_mail_flow_rule_sending_mail_elsewhere_needs_attention(db, vault, two_tenants, parameter):
    alpha, _ = two_tenants
    addon_on(db, vault, [alpha], started_at=iso(NOW - timedelta(hours=3)))
    at = (NOW - timedelta(hours=1)).replace(tzinfo=None)
    rule = _ex(
        40,
        at,
        "New-TransportRule",
        "admin@contoso.com",
        ObjectId="Copy everything",
        Parameters=_params(Name="Copy everything", **{parameter: "drop@mailbox.example"}),
    )
    assert collect(db, alpha, {"Audit.Exchange": [(at + timedelta(minutes=20), [rule])]}, NOW)[0] == "succeeded"
    (kept,) = events.activity_between(db, alpha, NOW - timedelta(days=1), NOW)
    assert kept.attention == "A rule forwards mail."
    assert kept.summary == f"{parameter} drop@mailbox.example"


# -- Stopping -------------------------------------------------------------------------------------------------


def test_a_stop_refused_for_the_addon_apps_certificate_blames_nobody_and_is_sent_again(
    app, client, db, vault, cfg, two_tenants
):
    alpha, _ = two_tenants
    addon_on(db, vault, [alpha])
    _admin(client, db)
    assert client.post(f"/tenants/{alpha}/audit-addon/off").status_code == 302
    microsoft = Microsoft("enabled")
    microsoft.tasks(app, db, vault, cfg, sign_in=EXPIRED)
    assert microsoft.status == ALL_ON  # nothing was stopped
    error = addon.row_for(db, alpha)["check_error"]
    assert error and "removed" not in error
    assert "removed the add-on" not in client.get(f"/tenants/{alpha}/audit-addon").get_data(as_text=True)

    now = datetime.now(UTC)
    assert addon.stops_due(db, now) == []
    later = now + addon.RETRY_STOP + timedelta(minutes=1)
    assert addon.stops_due(db, later) == [alpha]
    addon.retry_stop(db, alpha)  # what the audit_checks job does
    microsoft.tasks(app, db, vault, cfg)  # the certificate was replaced since
    assert microsoft.status == ALL_OFF
    assert addon.row_for(db, alpha)["check_error"] is None and addon.stops_due(db, later) == []


def test_a_stop_refused_because_the_client_removed_the_app_isnt_sent_again(app, client, db, vault, cfg, two_tenants):
    alpha, _ = two_tenants
    addon_on(db, vault, [alpha])
    _admin(client, db)
    client.post(f"/tenants/{alpha}/audit-addon/off")
    Microsoft("enabled").tasks(app, db, vault, cfg, sign_in=GONE)
    assert addon.row_for(db, alpha)["check_error"] == addon.STOP_REMOVED
    assert addon.stops_due(db, datetime.now(UTC) + addon.RETRY_STOP * 2) == []


# -- Where a client has got -----------------------------------------------------------------------------------


@pytest.mark.parametrize("noticed", ["by the nightly collection", "when switched on again"])
def test_switching_on_a_client_that_removed_the_app_asks_for_the_link_again(
    app, client, db, vault, cfg, two_tenants, noticed
):
    alpha, _ = two_tenants
    addon_on(db, vault, [alpha])
    _admin(client, db)
    if noticed == "by the nightly collection":
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.get(
                f"https://manage.office.com/api/v1.0/{TENANT}/activity/feed/subscriptions/list",
                status=401,
                json={"error": {"code": "AF10001", "message": "The permission set () sent in the request"}},
            )
            assert collect(db, alpha, {}, NOW, rsps=rsps)[0] == "partial"
        row = addon.row_for(db, alpha)
        assert addon.state(row).key == "off" and not row["consented_at"] and not row["sent_at"]
    else:  # switched off (the stop went through), then the client removed the app
        client.post(f"/tenants/{alpha}/audit-addon/off")
        Microsoft("enabled").tasks(app, db, vault, cfg)
        assert addon.row_for(db, alpha)["consented_at"]

    assert client.post(f"/tenants/{alpha}/audit-addon/on").status_code == 302
    Microsoft("disabled").tasks(app, db, vault, cfg, sign_in=GONE)
    state = addon.state(addon.row_for(db, alpha))
    assert state.key == "not_sent" and "Approved" not in state.label
    assert "Mark as sent" in client.get(f"/tenants/{alpha}/audit-addon").get_data(as_text=True)


def test_an_approval_not_confirmed_yet_reads_differently_from_a_confirmed_one(app, client, db, vault, cfg, two_tenants):
    alpha, bravo = two_tenants
    addon_on(db, vault)
    _admin(client, db)
    for tenant_id in (alpha, bravo):
        client.post(f"/tenants/{tenant_id}/audit-addon/sent", data={"sent": "1"})
        addon.client_approved(db, store.get_tenant(db, tenant_id))
    db.execute("DELETE FROM tasks WHERE key = ?", (f"audit_check:{bravo}",))  # Bravo's check hasn't run yet
    db.execute("UPDATE audit_feeds SET wanted_at = NULL WHERE tenant_id = ?", (alpha,))  # approved, not switched on
    Microsoft(None).tasks(app, db, vault, cfg)
    confirmed, claimed = addon.state(addon.row_for(db, alpha)), addon.state(addon.row_for(db, bravo))
    assert (confirmed.key, confirmed.label) == ("approved", "Approved")
    assert (claimed.key, claimed.label) == ("approved", "Approved, confirming")
    tracker = client.get("/admin/onboarding?show=all").get_data(as_text=True)
    assert f'href="/tenants/{bravo}/audit-addon">Approved, confirming</a>' in tracker


def test_a_paused_client_isnt_signed_in_to_until_its_resumed(app, client, db, vault, cfg, two_tenants):
    alpha, _ = two_tenants
    addon_on(db, vault)
    _admin(client, db)
    client.post(f"/tenants/{alpha}/audit-addon/sent", data={"sent": "1"})
    client.post(f"/tenants/{alpha}/pause")
    addon.client_approved(db, store.get_tenant(db, alpha))  # came back from the link while paused
    microsoft = Microsoft(None)
    microsoft.tasks(app, db, vault, cfg)
    assert microsoft.status == {} and not addon.row_for(db, alpha)["consented_at"]
    assert addon.due(db, datetime.now(UTC) + timedelta(hours=1)) == []

    client.post(f"/tenants/{alpha}/resume")
    assert addon.due(db, datetime.now(UTC) + timedelta(minutes=5)) == [alpha]
    addon.queue_check(db, alpha)  # what the audit_checks job does
    microsoft.tasks(app, db, vault, cfg)
    assert microsoft.status == ALL_ON and addon.state(addon.row_for(db, alpha)).key == "on"


def test_an_archived_client_keeping_the_addon_on_is_named_and_listed(client, db, vault, two_tenants):
    alpha, _ = two_tenants
    addon_on(db, vault, [alpha])
    _admin(client, db)
    assert client.post(f"/tenants/{alpha}/archive").status_code == 302
    resp = client.post("/admin/audit-addon/switch", data={"on": "0"}, follow_redirects=True)
    page = html.unescape(resp.get_data(as_text=True))
    assert addon.enabled(db) and "The add-on is on for 1 client: Alpha Ltd (archived)." in page
    clients = page[page.find(">Clients</h2>") :]
    assert "Alpha Ltd" in clients and ">Archived</span>" in clients
    assert "Archived, so nothing is read, but its feed stays on at Microsoft" in clients
    assert client.get(f"/tenants/{alpha}/audit-addon").status_code == 200  # where it's switched off


# -- What the report and the pages say ------------------------------------------------------------------------


def test_the_report_doesnt_say_deleted_records_are_kept(app, client, db, vault, cfg, two_tenants):
    alpha, _ = two_tenants
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    addon_on(db, vault, [alpha], started_at=iso(now - timedelta(hours=3)))
    assert collect(db, alpha, sample(now), now)[0] == "succeeded"
    _admin(client, db)
    client.post(f"/tenants/{alpha}/audit-addon/off")
    assert "stays until retention removes it" in client.get(f"/tenants/{alpha}/data/{KEY}").get_data(as_text=True)
    client.post(f"/tenants/{alpha}/audit-addon/off", data={"delete": "1"})
    Microsoft("enabled").tasks(app, db, vault, cfg)
    assert events.counts(db, alpha)["activity"] == 0
    page = client.get(f"/tenants/{alpha}/data/{KEY}").get_data(as_text=True)
    assert "stays until retention removes it" not in page and "Nothing it collected is kept." in page


def test_a_name_ending_in_s_reads_properly(client, db, vault, connection_id):
    tenant_id = store.create_tenant(db, "Northwind Traders", "northwind.onmicrosoft.com", connection_id)
    db.execute("UPDATE tenants SET consented_at = '2026-01-01T00:00:00Z'")
    addon_on(db, vault, [tenant_id])
    _admin(client, db)
    collect(db, tenant_id, {}, NOW)  # so the page says when it was last read
    page = html.unescape(client.get(f"/tenants/{tenant_id}/audit-addon").get_data(as_text=True))
    assert "Traders's" not in page
    assert "Northwind Traders' Microsoft 365 reports" in page  # the email to the client
    assert "the rest of Northwind Traders' data" in page and "read Northwind Traders' feed settings" in page
