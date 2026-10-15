"""Microsoft 365 service health and the message centre (2.4): the collector, the Service health report and the
waiting state."""

import json
from datetime import UTC, datetime

from officesentry import store
from officesentry.reports import data_reports
from officesentry.snapshot import TenantSnapshot
from tests import securitydata as sec
from tests.conftest import login, make_user
from tests.seed import seed_tenant

HEALTH = "ServiceHealth.Read.All"
MESSAGES = "ServiceMessage.Read.All"


def _report(db, tenant_id):
    return data_reports.build(db, store.get_tenant(db, tenant_id), "service-health", datetime.now(UTC))


def _table(report, title) -> dict[str, dict]:
    section = next(s for s in report.sections if s.title == title)
    return {str(row[0]): dict(zip(section.columns, row, strict=True)) for row in section.rows}


def _flags(report, title) -> dict[str, str | None]:
    section = next(s for s in report.sections if s.title == title)
    return {str(row[0]): flag for row, flag in zip(section.rows, section.flags, strict=True)}


def _run(db, tenant_id):
    return db.execute(
        "SELECT * FROM runs WHERE tenant_id = ? AND collector = 'service_health' ORDER BY id DESC LIMIT 1",
        (tenant_id,),
    ).fetchone()


def test_collector_keeps_open_and_recent_issues_and_current_posts(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    run = _run(db, alpha)
    assert run["status"] == "succeeded"
    snap = TenantSnapshot(db, store.get_tenant(db, alpha))
    assert {r["_id"] for r in snap.objects("service_health", "service_issue")} == {"EX1001", "TM2002", "SP3003"}
    messages = {r["_id"] for r in snap.objects("service_health", "service_message")}
    assert messages == {"MC1001", "MC1002", "MC1003", "MC1004", "MC1005", "MC1007"}  # not MC1006, which has ended
    assert snap.metric("service_health.incidents_open") == 1 and snap.metric("service_health.advisories_open") == 1
    assert snap.metric("service_messages.action_required") == 2
    # Neither an issue's posts nor a message's text is kept.
    stored = json.dumps([store.run_objects(db, run["id"], k) for k in ("service_issue", "service_message")])
    assert "Not kept" not in stored


def test_report_leads_with_whats_wrong_and_whats_due(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = _report(db, alpha)
    assert [s.title for s in report.sections] == [
        "Status now",
        "Open issues",
        "Recently resolved",
        "Changes needing action",
        "Major changes",
        "Other recent posts",
    ]
    status = _table(report, "Status now")
    assert status["Exchange Online"]["Status"] == "Service degradation"
    assert status["Microsoft Teams"]["Status"] == "Service operational"  # Microsoft writes it both ways
    assert _flags(report, "Status now") == {
        "Exchange Online": "medium",
        "Microsoft Teams": None,
        "SharePoint Online": None,
    }
    issues = _table(report, "Open issues")
    assert list(issues) == ["EX1001", "TM2002"]
    assert (issues["EX1001"]["Kind"], issues["TM2002"]["Kind"]) == ("Incident", "Advisory")
    assert _flags(report, "Open issues") == {"EX1001": "medium", "TM2002": "low"}
    assert list(_table(report, "Recently resolved")) == ["SP3003"]
    due = _table(report, "Changes needing action")
    assert list(due) == ["MC1001", "MC1002"]  # soonest first; not MC1007, whose date has passed
    assert _flags(report, "Changes needing action") == {"MC1001": "medium", "MC1002": "low"}
    assert due["MC1001"]["Major change"] == "Yes" and due["MC1001"]["Category"] == "Plan for change"
    assert list(_table(report, "Major changes")) == ["MC1003"]  # MC1001 is listed once, under action
    assert list(_table(report, "Other recent posts")) == ["MC1004"]

    tiles = {t.label: t.value for t in report.tiles}
    assert tiles == {"Open incidents": 1, "Open advisories": 1, "Action needed": 2, "Major changes": 1}
    assert next(t for t in report.tiles if t.label == "Action needed").foot == "1 due within 30 days"

    make_user(db, "admin")
    login(client, "admin")
    for ext in ("", ".csv", ".xlsx"):
        assert client.get(f"/tenants/{alpha}/data/service-health{ext}").status_code == 200


def test_each_permission_waits_on_its_own(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, our_app={"missing": (MESSAGES,)})
    run = _run(db, alpha)
    assert run["status"] == "succeeded"
    endpoints = {r["endpoint"] for r in db.execute("SELECT endpoint FROM run_requests WHERE run_id = ?", (run["id"],))}
    assert endpoints == {
        "v1.0 /admin/serviceAnnouncement/healthOverviews",
        "v1.0 /admin/serviceAnnouncement/issues",
    }
    report = _report(db, alpha)
    assert MESSAGES in report.notice and HEALTH not in report.notice
    assert _table(report, "Open issues") and not _table(report, "Changes needing action")
    labels = [t.label for t in report.tiles]
    assert "Open incidents" in labels and "Action needed" not in labels


def test_a_refused_list_waits_rather_than_fails(db, two_tenants):
    alpha, _ = two_tenants
    from tests import intunedata as idata

    seed_tenant(db, alpha, security_kw={"status": {sec.MESSAGES_URL: (403, idata.DENIED)}})
    assert _run(db, alpha)["status"] == "succeeded"
    status = TenantSnapshot(db, store.get_tenant(db, alpha)).objects("service_health", "service_health_status")[0]
    assert status["parts"] == {"health": "read", "issues": "read", "messages": "waiting"}
