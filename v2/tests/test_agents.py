"""AI agents (2.6): the collector, the AI agents report and the waiting state while the client hasn't approved
AgentIdentity.Read.All. Graph is simulated at the HTTP level (``tests.seed``, ``tests.agentdata``)."""

from datetime import UTC, datetime

import pytest

from officesentry import store
from officesentry.reports import data_reports
from officesentry.snapshot import TenantSnapshot
from tests import agentdata as agd
from tests import graphdata as gd
from tests import intunedata as idata
from tests.conftest import login, make_user
from tests.seed import seed_tenant

PERMISSION = "AgentIdentity.Read.All"


@pytest.fixture
def made_with(monkeypatch):
    """The apps the agents were made with, in the tenant's service principals."""
    monkeypatch.setattr(gd, "SERVICE_PRINCIPALS", gd.SERVICE_PRINCIPALS + agd.SERVICE_PRINCIPALS)


def _report(db, tenant_id):
    return data_reports.build(db, store.get_tenant(db, tenant_id), "agents", datetime.now(UTC))


def _table(report, title) -> dict[str, dict]:
    section = next(s for s in report.sections if s.title == title)
    return {str(row[0]): dict(zip(section.columns, row, strict=True)) for row in section.rows}


def _flags(report, title) -> dict[str, str | None]:
    section = next(s for s in report.sections if s.title == title)
    return {str(row[0]): flag for row, flag in zip(section.rows, section.flags, strict=True)}


def _run(db, tenant_id):
    return db.execute(
        "SELECT * FROM runs WHERE tenant_id = ? AND collector = 'agents' ORDER BY id DESC LIMIT 1", (tenant_id,)
    ).fetchone()


def _endpoints(db, run) -> set[str]:
    return {r["endpoint"] for r in db.execute("SELECT endpoint FROM run_requests WHERE run_id = ?", (run["id"],))}


def _status(db, tenant_id) -> dict:
    row = TenantSnapshot(db, store.get_tenant(db, tenant_id)).objects("agents", "agents_status")[0]
    return {k: v for k, v in row.items() if not k.startswith("_")}


def test_collector_keeps_each_agent_and_its_permissions(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    run = _run(db, alpha)
    assert run["status"] == "succeeded"
    assert "v1.0 /servicePrincipals/microsoft.graph.agentIdentity" in _endpoints(db, run)
    snap = TenantSnapshot(db, store.get_tenant(db, alpha))
    agents = {a["_id"]: a for a in snap.objects("agents", "agent")}
    assert set(agents) == {"ag-mail", "ag-flagged", "ag-off", "ag-new"}
    assert agents["ag-off"]["enabled"] is False and agents["ag-mail"]["blueprint"] == agd.BLUEPRINT
    assert agents["ag-flagged"]["disabledByMicrosoft"] == "DisabledDueToViolationOfServicesAgreement"
    held = {(p["agentId"], p["permission"]) for p in snap.objects("agents", "agent_permission")}
    assert held == {("ag-mail", "Mail.Read"), ("ag-off", "User.Read.All")}
    assert snap.metric("agents.count") == 4 and snap.metric("agents.with_app_permissions") == 2
    assert _status(db, alpha) == {"state": "read", "unread": 0}


def test_report_flags_what_needs_a_look(client, db, two_tenants, made_with):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = _report(db, alpha)
    assert report.unavailable is None
    assert [s.title for s in report.sections] == ["AI agents", "What agents can reach"]
    agents = _table(report, "AI agents")
    # Those that need a look first, then by name.
    assert list(agents) == ["Inbox triage agent", "Invoice chaser", "Old FAQ agent", "Onboarding buddy"]
    assert _flags(report, "AI agents") == {
        "Inbox triage agent": "high",  # reads every mailbox on its own
        "Invoice chaser": "high",  # switched off by Microsoft
        "Old FAQ agent": None,
        "Onboarding buddy": None,
    }
    mail = agents["Inbox triage agent"]
    assert mail["Made with"] == "Copilot Studio" and mail["Blueprint"] == "Helpdesk agent blueprint"
    assert mail["Application permissions"].parts == ("Mail.Read",) and mail["State"] == "On"
    assert agents["Invoice chaser"]["State"] == "Switched off by Microsoft"
    assert agents["Old FAQ agent"]["State"] == "Off"
    assert agents["Onboarding buddy"]["Made with"] == "app-unknown"  # not an app the tenant knows
    reach = next(s for s in report.sections if s.title == "What agents can reach")
    assert [r[:4] for r in reach.rows] == [
        ["Inbox triage agent", "Microsoft Graph", "Mail.Read", "On its own"],
        ["Old FAQ agent", "Microsoft Graph", "User.Read.All", "On its own"],
    ]
    assert reach.flags == ["high", None]

    tiles = {t.label: (t.value, t.foot) for t in report.tiles}
    assert tiles["AI agents"] == (4, "2 switched on")
    assert tiles["Need a look"][0] == 2 and tiles["With admin roles"][0] == 0
    assert tiles["New in 30 days"][0] == 1

    make_user(db, "admin")
    login(client, "admin")
    for ext in ("", ".csv", ".xlsx"):
        assert client.get(f"/tenants/{alpha}/data/agents{ext}").status_code == 200


def test_admin_roles_and_consent_are_joined(db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    role = {
        "id": "a-agent",
        "roleDefinitionId": gd.READER,
        "principalId": "ag-new",
        "directoryScopeId": "/",
        "principal": {
            "@odata.type": "#microsoft.graph.agentIdentity",
            "id": "ag-new",
            "displayName": "Onboarding buddy",
        },
    }
    grant = {
        "id": "g-agent",
        "clientId": "ag-new",
        "consentType": "AllPrincipals",
        "resourceId": "sp-graph",
        "scope": "Files.Read.All User.Read",
    }
    monkeypatch.setattr(gd, "ROLE_ASSIGNMENTS", gd.ROLE_ASSIGNMENTS + [role])
    monkeypatch.setattr(gd, "DELEGATED_GRANTS", gd.DELEGATED_GRANTS + [grant])
    seed_tenant(db, alpha)
    report = _report(db, alpha)
    buddy = _table(report, "AI agents")["Onboarding buddy"]
    assert buddy["Admin roles"].parts == ("Directory Readers",) and buddy["Consented for"] == "Everyone"
    assert _flags(report, "AI agents")["Onboarding buddy"] == "medium"
    reach = [r for r in next(s for s in report.sections if s.title == "What agents can reach").rows]
    assert ["Onboarding buddy", "Microsoft Graph", "Files.Read.All", "For everyone", ""] in reach
    assert {t.label: t.value for t in report.tiles}["With admin roles"] == 1


def test_waits_for_the_client_to_approve_rather_than_fails(db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha, our_app={"missing": (PERMISSION,)})
    run = _run(db, alpha)
    assert run["status"] == "succeeded"
    assert "v1.0 /servicePrincipals/microsoft.graph.agentIdentity" not in _endpoints(db, run)  # not even asked
    assert _status(db, alpha) == {"state": "waiting"}
    report = _report(db, alpha)
    assert report.unavailable.startswith("Waiting for Alpha Ltd to approve the new read-only permission")
    assert PERMISSION in report.unavailable and "2.6" in report.unavailable

    # Approved by the connection check's reckoning, but Microsoft still refuses: waiting, not a failure.
    seed_tenant(db, bravo, agents_kw={"status": (403, idata.DENIED)})
    assert _run(db, bravo)["status"] == "succeeded"
    assert _status(db, bravo) == {"state": "waiting"}


def test_an_unknown_endpoint_is_unavailable_and_an_error_is_a_warning(db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha, agents_kw={"status": (400, {"error": {"code": "BadRequest", "message": "No segment"}})})
    assert _run(db, alpha)["status"] == "succeeded"
    assert (
        _report(db, alpha).unavailable == "Microsoft doesn't offer agent identities (Entra Agent ID) for this tenant."
    )
    seed_tenant(db, bravo, agents_kw={"status": (500, {"error": {"code": "InternalServerError", "message": "x"}})})
    assert _run(db, bravo)["status"] == "partial"
    assert "couldn't read" in _report(db, bravo).unavailable


def test_a_cloud_that_may_not_offer_it_says_unavailable(db, two_tenants):
    alpha, _ = two_tenants
    db.execute("UPDATE tenants SET cloud = 'gcc_high' WHERE id = ?", (alpha,))
    seed_tenant(db, alpha, our_app={"missing": (PERMISSION,)}, agents_kw={"status": (403, idata.DENIED)})
    assert _run(db, alpha)["status"] == "succeeded"
    assert _status(db, alpha) == {"state": "unavailable"}


def test_no_agents(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, agents_kw={"agents": []})
    report = _report(db, alpha)
    assert report.unavailable is None and not _table(report, "AI agents")
    assert {t.label: t.value for t in report.tiles}["AI agents"] == 0


def test_tenants_only_see_their_own_agents(db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    seed_tenant(db, bravo, agents_kw={"agents": agd.AGENTS[:1]})
    assert len(_table(_report(db, alpha), "AI agents")) == 4
    assert list(_table(_report(db, bravo), "AI agents")) == ["Inbox triage agent"]
