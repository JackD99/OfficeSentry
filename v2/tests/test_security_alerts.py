"""Microsoft Defender alerts and incidents (2.4): the collector, the Security alerts report, its check, and the
waiting, refused and national cloud states."""

import json
from datetime import UTC, datetime

from officesentry import store
from officesentry.collectors import security_alerts
from officesentry.reports import data_reports
from officesentry.rules import evaluate
from officesentry.snapshot import TenantSnapshot
from tests import intunedata as idata
from tests import securitydata as sec
from tests.conftest import login, make_user
from tests.seed import seed_tenant

ALERTS = "SecurityAlert.Read.All"
INCIDENTS = "SecurityIncident.Read.All"


def _report(db, tenant_id):
    return data_reports.build(db, store.get_tenant(db, tenant_id), "security-alerts", datetime.now(UTC))


def _table(report, title) -> dict[str, dict]:
    section = next(s for s in report.sections if s.title == title)
    return {str(row[0]): dict(zip(section.columns, row, strict=True)) for row in section.rows}


def _flags(report, title) -> dict[str, str | None]:
    section = next(s for s in report.sections if s.title == title)
    return {str(row[0]): flag for row, flag in zip(section.rows, section.flags, strict=True)}


def _check(db, tenant_id):
    return {r.key: r for r in evaluate(TenantSnapshot(db, store.get_tenant(db, tenant_id)))}["defender_alerts"]


def _run(db, tenant_id):
    return db.execute(
        "SELECT * FROM runs WHERE tenant_id = ? AND collector = 'security_alerts' ORDER BY id DESC LIMIT 1",
        (tenant_id,),
    ).fetchone()


def test_collector_keeps_whats_open_and_recently_resolved(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    run = _run(db, alpha)
    assert run["status"] == "succeeded"
    snap = TenantSnapshot(db, store.get_tenant(db, alpha))
    alerts = {r["_id"]: r for r in snap.objects("security_alerts", "security_alert")}
    assert sorted(alerts) == ["a1", "a2", "a3", "a4", "a5"]  # not a6, resolved two months ago
    assert alerts["a1"]["users"] == ["ann@x.com"]  # the account and its mailbox, once
    assert alerts["a2"]["devices"] == ["LAPTOP-BOB"] and alerts["a2"]["users"] == ["LAPTOP-BOB\\bob"]
    incidents = {r["_id"]: r for r in snap.objects("security_alerts", "security_incident")}
    assert sorted(incidents) == ["101", "102", "103"]  # not the old one or the one merged into another
    assert (snap.metric("defender.alerts_open"), snap.metric("defender.alerts_open_high")) == (4, 1)
    assert snap.metric("defender.incidents_open") == 2
    # Only accounts and devices are kept from the evidence: never a mail's subject, a command line or
    # Microsoft's description.
    stored = json.dumps(store.run_objects(db, run["id"], "security_alert"))
    for text in (sec.COMMAND_LINE, "Your invoice is overdue", "Microsoft's description", "Microsoft's advice"):
        assert text not in stored
    endpoints = [r["endpoint"] for r in db.execute("SELECT endpoint FROM run_requests WHERE run_id = ?", (run["id"],))]
    assert sorted(set(endpoints)) == ["v1.0 /security/alerts_v2", "v1.0 /security/incidents"]


def test_an_address_is_named_once_whatever_its_case():
    evidence = [
        {"@odata.type": "#microsoft.graph.security.userEvidence", "userAccount": {"userPrincipalName": "Ann@X.com"}},
        {"@odata.type": "#microsoft.graph.security.mailboxEvidence", "primaryAddress": "ann@x.com"},
        {
            "@odata.type": "#microsoft.graph.security.userEvidence",
            "userAccount": {"accountName": "Bob", "domainName": "PC"},
        },
        {"@odata.type": "#microsoft.graph.security.deviceEvidence", "deviceDnsName": "pc.x.local"},
    ]
    assert security_alerts.named(evidence) == (["ann@x.com", "PC\\Bob"], ["pc.x.local"])


def test_report_lists_incidents_and_alerts_by_severity(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = _report(db, alpha)
    assert [s.title for s in report.sections] == ["Open incidents", "Open alerts", "Recently resolved"]
    incidents = _table(report, "Open incidents")
    assert list(incidents) == ["Phishing involving one user", "Execution incident on one endpoint"]
    assert incidents["Phishing involving one user"]["Alerts"] == 1
    assert str(incidents["Phishing involving one user"]["Link"]).startswith("Open in Defender")
    alerts = _table(report, "Open alerts")
    assert list(alerts) == [
        "Email messages containing malicious URL removed after delivery",
        "Suspicious PowerShell command line",
        "Unfamiliar sign-in properties",
        "eDiscovery search started or exported",
    ]
    phish = alerts["Email messages containing malicious URL removed after delivery"]
    assert (phish["Severity"], phish["Status"], phish["Picked up"]) == ("High", "New", "No")
    assert phish["Source"] == "Defender for Office 365" and phish["Category"] == "Initial access"
    powershell = alerts["Suspicious PowerShell command line"]
    assert (powershell["Picked up"], powershell["Assigned to"]) == ("Yes", "helpdesk@msp.example")
    assert list(_flags(report, "Open alerts").values()) == ["high", "medium", "low", None]
    resolved = _table(report, "Recently resolved")
    assert resolved["User clicked a malicious link"]["Outcome"] == "Real threat (phishing)"

    tiles = {t.label: t for t in report.tiles}
    assert [tiles[k].value for k in ("Open alerts", "High severity", "Not picked up", "Open incidents")] == [4, 1, 3, 2]
    assert tiles["Open alerts"].tone == "bad" and tiles["Not picked up"].rows.where == {"Picked up": "No"}

    make_user(db, "admin")
    login(client, "admin")
    for ext in ("", ".csv", ".xlsx"):
        assert client.get(f"/tenants/{alpha}/data/security-alerts{ext}").status_code == 200


def test_check_raises_open_high_and_medium_alerts(db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    result = _check(db, alpha)
    assert result.status == "fail"
    assert result.summary == "2 high or medium severity alerts open in Defender, 1 not picked up"
    assert [(f.severity, f.subject) for f in result.findings] == [
        ("high", "Email messages containing malicious URL removed after delivery"),
        ("medium", "Suspicious PowerShell command line"),
    ]
    assert result.findings[0].detail.startswith("Defender for Office 365, nobody has picked it up, since ")
    assert result.findings[0].detail.endswith("; names ann@x.com")
    assert result.findings[1].detail.startswith("Defender for Endpoint, in progress with helpdesk@msp.example")

    seed_tenant(db, bravo, security_kw={"alerts": [a for a in sec.ALERTS if a["severity"] in ("low", "informational")]})
    result = _check(db, bravo)
    assert result.status == "pass" and result.summary == "No high or medium severity alerts are open"


def test_waits_for_approval(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, our_app={"missing": (ALERTS, INCIDENTS)})
    run = _run(db, alpha)
    assert run["status"] == "succeeded"
    endpoints = [r["endpoint"] for r in db.execute("SELECT endpoint FROM run_requests WHERE run_id = ?", (run["id"],))]
    assert not [e for e in endpoints if "/security/" in e]
    report = _report(db, alpha)
    assert ALERTS in report.notice and "added in Office Sentry 2.4" in report.notice
    assert not report.tiles and report.sections[1].empty.endswith("once the client approves the new permission.")
    assert _check(db, alpha).status == "unavailable"


def test_a_refusal_after_approval_means_defender_has_nothing_for_the_tenant(db, two_tenants):
    """Granted, but refused: no service that reports to Defender is set up. Not a failure, and not waiting."""
    alpha, bravo = two_tenants
    refused = {"status": {sec.ALERTS_URL: (403, idata.DENIED), sec.INCIDENTS_URL: (403, idata.DENIED)}}
    seed_tenant(db, alpha, security_kw=refused)
    assert _run(db, alpha)["status"] == "succeeded"
    report = _report(db, alpha)
    assert report.unavailable.startswith("Microsoft Defender has no alerts to give for this tenant")
    assert _check(db, alpha).summary == "Microsoft Defender has no alerts for this tenant"

    # Refused before the connection check has seen it granted: waiting for approval.
    seed_tenant(db, bravo, security_kw=refused, our_app={"missing": (INCIDENTS,)})
    status = TenantSnapshot(db, store.get_tenant(db, bravo)).objects("security_alerts", "security_alert_status")[0]
    assert status["parts"] == {"alerts": "unavailable", "incidents": "waiting"}


def test_not_offered_in_21vianet(db, two_tenants):
    alpha, _ = two_tenants
    db.execute("UPDATE tenants SET cloud = 'china' WHERE id = ?", (alpha,))
    report = _report(db, alpha)
    assert "21Vianet" in report.unavailable
    assert "21Vianet" in _check(db, alpha).summary
