"""One way out for every alert (alerts.deliver): the webhook and any destination registered beside it, each with
its own severity for new findings, and the severity and stable key a ticketing system needs."""

from __future__ import annotations

import pytest
import responses

from officesentry import activity_alerts, alerts, cert_lifecycle, store, system_alerts
from tests.test_alerts import HOOK, add_finding, add_run, audit_rows, configure, posted, public_dns  # noqa: F401


class Desk:
    """A ticketing system that takes what it's sent, or refuses it."""

    def __init__(self, min_severity=None, fail=None, skip=lambda m: False):
        self.got: list[alerts.Message] = []
        self.min_severity, self.fail, self.skip = min_severity, fail, skip
        self.on = True

    def send(self, conn, alerting, message, s):
        if self.fail:
            raise alerts.AlertError(self.fail)
        if self.skip(message):
            return False
        self.got.append(message)


@pytest.fixture
def desk():
    """A "desk" destination registered beside the webhook, taken out again after the test."""
    before = list(alerts.DESTINATIONS)
    d = Desk()
    alerts.register(
        alerts.Destination("desk", lambda conn, s: d.on, d.send, lambda conn, s: d.min_severity),
    )
    yield d
    alerts.DESTINATIONS[:] = before


@pytest.fixture
def alerting(cfg, vault):
    return alerts.Alerting(cfg.base_url, vault)


def test_alerts_reach_a_registered_destination_without_a_webhook(db, vault, alerting, two_tenants, desk):
    alpha, _ = two_tenants
    configure(db, vault, url=None, enabled=False)
    ids = [add_finding(db, alpha, severity=s, object_id=s) for s in ("low", "critical", "medium")]
    tenant = store.get_tenant(db, alpha)
    assert alerts.after_sync(db, alerting, tenant, {"opened": ids}) is True
    (m,) = desk.got
    # Every new finding, worst first, whatever the webhook's own severity is.
    assert [i.severity for i in m.items] == ["critical", "medium", "low"]
    assert m.event == "findings.new" and m.severity == "critical" and m.tenant["id"] == alpha
    assert m.title == "Alpha Ltd: 3 new findings" and m.text == "Office Sentry found 3 new or returning issues."
    assert [i.key for i in m.items] == [f"finding:{ids[1]}", f"finding:{ids[2]}", f"finding:{ids[0]}"]
    assert m.key.startswith(f"findings.new:{alpha}:") and not m.resolves
    # The same findings make the same key.
    again = alerts.findings_message(m.link, tenant, list(reversed(m.items)))
    assert again.key == m.key and alerts.findings_message(m.link, tenant, m.items[:1]).key != m.key
    (row,) = audit_rows(db, "alert_sent")
    assert row["detail"] == "findings.new via desk, 3 findings" and row["tenant_id"] == alpha

    desk.on = False
    assert not alerts.wanted(db) and alerts.after_sync(db, alerting, tenant, {"opened": ids}) is None


@responses.activate
def test_each_destination_gets_the_findings_at_its_own_severity(db, vault, alerting, two_tenants, desk):
    alpha, _ = two_tenants
    configure(db, vault, fmt="generic", severity="high")
    desk.min_severity = "medium"
    responses.post(HOOK, status=202)
    ids = [add_finding(db, alpha, severity=s, object_id=s) for s in ("low", "high", "medium")]
    assert alerts.after_sync(db, alerting, store.get_tenant(db, alpha), {"opened": ids}) is True
    body = posted(responses)
    assert [f["severity"] for f in body["findings"]] == ["high"]
    assert body["title"] == "Alpha Ltd: 1 new finding" and body["text"].endswith("rated high or worse.")
    assert body["severity"] == "high" and body["key"].startswith(f"findings.new:{alpha}:") and body["resolves"] is False
    (m,) = desk.got
    assert [i.severity for i in m.items] == ["high", "medium"] and m.text.endswith("rated medium or worse.")
    assert sorted(r["detail"] for r in audit_rows(db, "alert_sent")) == [
        "findings.new via desk, 2 findings",
        "findings.new via generic, 1 finding",
    ]

    # Nothing at the webhook's severity: only the desk is sent anything, and the webhook isn't called.
    low = add_finding(db, alpha, severity="medium", object_id="m2")
    assert alerts.after_sync(db, alerting, store.get_tenant(db, alpha), {"opened": [low]}) is True
    assert len(responses.calls) == 1 and len(desk.got) == 2


@responses.activate
def test_one_destination_failing_doesnt_stop_the_other(db, vault, alerting, two_tenants, desk):
    alpha, _ = two_tenants
    configure(db, vault, severity="low")
    responses.post(HOOK, status=500)
    finding = add_finding(db, alpha)
    tenant = store.get_tenant(db, alpha)
    assert alerts.after_sync(db, alerting, tenant, {"opened": [finding]}) is True  # the desk took it
    assert [r["detail"] for r in audit_rows(db, "alert_failed")] == [
        "findings.new via teams: The webhook answered HTTP 500."
    ]
    desk.fail = "Autotask said no."
    assert alerts.after_sync(db, alerting, tenant, {"opened": [finding]}) is False  # nobody took it
    assert audit_rows(db, "alert_failed")[-1]["detail"] == "findings.new via desk: Autotask said no."

    # A destination that leaves a message out isn't recorded, and nothing took it.
    desk.fail, desk.skip = None, lambda m: True
    configure(db, vault, url=None, enabled=False)
    before = len(audit_rows(db, "alert_sent", "alert_failed"))
    assert alerts.after_sync(db, alerting, tenant, {"opened": [finding]}) is None
    assert len(audit_rows(db, "alert_sent", "alert_failed")) == before


def test_a_collection_failing_and_working_again_share_a_key(db, vault, alerting, two_tenants, desk):
    alpha, _ = two_tenants
    configure(db, vault, url=None, enabled=False, recovery=True)
    add_run(db, alpha, "succeeded")
    failed = add_run(db, alpha, "failed", error="Graph 500")
    assert alerts.after_run(db, alerting, failed, "failed") is True
    worked = add_run(db, alpha, "succeeded")
    assert alerts.after_run(db, alerting, worked, "succeeded") is True
    down, up = desk.got
    assert (down.event, down.severity, down.resolves) == ("collection.failed", "medium", False)
    assert (up.event, up.severity, up.resolves) == ("collection.recovered", "info", True)
    assert down.key == up.key == f"collection:{alpha}:users"
    events = [r[0] for r in db.execute("SELECT message FROM run_events WHERE run_id = ?", (failed,))]
    assert "Alert sent via desk" in events


def test_msp_wide_alerts_carry_a_severity_and_key(cfg):
    e = cert_lifecycle.Expiry("mail", None, "Mail app", "AB12", "2026-11-01T00:00:00Z", 5, "7")
    m = cert_lifecycle._message(cfg.base_url, e)
    assert (m.severity, m.key, m.tenant) == ("high", "certificate:mail:AB12", None)
    expired = cert_lifecycle._message(
        cfg.base_url, cert_lifecycle.Expiry("mail", None, "Mail", "AB12", "", -1, "expired")
    )
    assert expired.severity == "critical" and expired.key == m.key  # each warning about one certificate shares it

    problem = system_alerts.Problem("backup", "Backups are failing", "The nightly copy failed.", "Check the disk.")
    start = system_alerts._message(cfg, "backup", "start", problem, None, None)
    over = system_alerts._message(cfg, "backup", "resolved", None, {"title": "Backups are failing"}, None)
    assert (start.severity, start.key, start.resolves) == ("high", "system:backup", False)
    assert (over.severity, over.key, over.resolves) == ("info", "system:backup", True)

    assert alerts.test_message(cfg.base_url).severity == "info"
    sample = activity_alerts.sample_message(cfg.base_url)
    assert sample.severity == "high" and sample.items[0].severity == "high"


def test_registering_again_replaces_and_the_webhook_is_always_there(desk):
    names = [d.name for d in alerts.DESTINATIONS]
    assert names.count("webhook") == 1 and names.count("desk") == 1 and alerts.DESTINATIONS[0] is alerts.WEBHOOK
    alerts.register(alerts.Destination("desk", lambda conn, s: False, lambda *a: None))
    assert [d.name for d in alerts.DESTINATIONS].count("desk") == 1
