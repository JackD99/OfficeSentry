"""Risky users (Entra ID Protection): collector, report, check and the account page."""

import re
from urllib.parse import parse_qs, urlsplit

import responses

from officesentry import store
from officesentry.graph import GRAPH_ROOT, GraphClient
from officesentry.reports import data_reports, library, user_profile, views
from officesentry.rules import evaluate
from officesentry.snapshot import TenantSnapshot
from officesentry.worker import execute_run
from tests import graphdata as gd
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.stored import unpack
from tests.test_collectors import SKUS, USERS

V1 = f"{GRAPH_ROOT}/v1.0"
RISKY = f"{V1}/identityProtection/riskyUsers"
P2_SKUS = [
    dict(
        SKUS[0],
        servicePlans=SKUS[0]["servicePlans"] + [{"servicePlanName": "AAD_PREMIUM_P2", "provisioningStatus": "Success"}],
    )
]


def _seed(db, tenant_id, **kwargs):
    seed_tenant(db, tenant_id, users=[*USERS, gd.INFO_USER], **kwargs)
    return store.get_tenant(db, tenant_id)


def _run(db, tenant_id, caps=None):
    if caps is not None:
        store.set_capabilities(db, tenant_id, caps)
    store.enqueue_run(db, tenant_id, "risky_users")
    run = store.claim_next_run(db)
    return run["id"], execute_run(db, run, lambda t, c: GraphClient(lambda: "t", sleep=lambda s: None))


def _metrics(db, tenant_id) -> dict:
    return {
        r["key"]: r["value"] for r in db.execute("SELECT key, value FROM metrics WHERE tenant_id = ?", (tenant_id,))
    }


# -- Collector --------------------------------------------------------------------------------


@responses.activate
def test_collector_keeps_open_and_recently_resolved_risk(db, two_tenants):
    alpha, _ = two_tenants
    responses.get(RISKY, json={"value": gd.RISKY_USERS[:3], "@odata.nextLink": f"{RISKY}?$skiptoken=page2"})
    responses.get(RISKY, json={"value": gd.RISKY_USERS[3:]})
    run_id, status = _run(db, alpha, {"entra_p2": True, "entra_p1": True})
    assert status == "succeeded"
    query = parse_qs(urlsplit(responses.calls[0].request.url).query)
    assert "riskState" in query["$select"][0] and query["$top"] == ["500"]
    assert all(c.request.method == "GET" for c in responses.calls)
    kept = {r["user"]: r for r in store.run_objects(db, run_id, "risky_user")}
    assert set(kept) == {"ann@x.com", "bob@x.com", "info@x.com", "gone@x.com"}  # not old or never risky
    ann = kept["ann@x.com"]
    assert (ann["_id"], ann["level"], ann["state"], ann["deleted"]) == ("u1", "medium", "atRisk", False)
    assert kept["gone@x.com"]["deleted"] is True
    [st] = store.run_objects(db, run_id, "risky_user_status")
    assert st["available"] and st["read"] == len(gd.RISKY_USERS) and st["days"] == 90
    m = _metrics(db, alpha)
    assert (m["risky_users.at_risk"], m["risky_users.high"], m["risky_users.compromised"]) == (2, 1, 1)


@responses.activate
def test_without_entra_p2_nothing_is_asked(db, two_tenants):
    alpha, bravo = two_tenants
    run_id, status = _run(db, alpha, {"entra_p2": False})
    assert status == "succeeded" and not responses.calls
    assert store.run_objects(db, run_id, "risky_user_status")[0]["reason"] == "licence"
    tenant = store.get_tenant(db, alpha)
    report = data_reports.build(db, tenant, "risky-users")
    assert "Entra ID P2" in report.unavailable and not report.sections
    result = {r.key: r for r in evaluate(TenantSnapshot(db, tenant))}["risky_users"]
    assert result.status == "unavailable" and "Entra ID P2" in result.summary

    # Licence not known: Microsoft refuses, the run is partial rather than failed.
    responses.get(
        RISKY, status=403, json={"error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges"}}
    )
    run_id, status = _run(db, bravo)
    assert status == "partial"
    assert store.run_objects(db, run_id, "risky_user_status")[0]["reason"] == "permission"
    tenant = store.get_tenant(db, bravo)
    assert "IdentityRiskyUser.Read.All" in data_reports.build(db, tenant, "risky-users").unavailable
    result = {r.key: r for r in evaluate(TenantSnapshot(db, tenant))}["risky_users"]
    assert result.status == "unavailable" and "IdentityRiskyUser" in result.summary


def test_not_collected_yet(db, two_tenants):
    alpha, _ = two_tenants
    tenant = store.get_tenant(db, alpha)
    assert "haven't been collected" in data_reports.build(db, tenant, "risky-users").unavailable
    result = {r.key: r for r in evaluate(TenantSnapshot(db, tenant))}["risky_users"]
    assert result.status == "unavailable"


def test_seeded_tenant_without_p2_is_unavailable(db, two_tenants):
    alpha, _ = two_tenants
    tenant = _seed(db, alpha)
    assert TenantSnapshot(db, tenant).runs["risky_users"]["status"] == "succeeded"
    assert "Entra ID P2" in data_reports.build(db, tenant, "risky-users").unavailable


# -- Report and check -------------------------------------------------------------------------


def test_report_and_check_with_entra_p2(client, db, two_tenants):
    alpha, _ = two_tenants
    tenant = _seed(db, alpha, skus=P2_SKUS)
    report = data_reports.build(db, tenant, "risky-users")
    assert report.unavailable is None
    at_risk, resolved = report.sections
    assert (at_risk.title, resolved.title) == ("At risk now", "Recently resolved")
    assert [r[0] for r in at_risk.rows] == ["bob@x.com", "ann@x.com", "gone@x.com"]
    assert at_risk.flags == ["high", "medium", "low"]
    assert at_risk.rows[0][2:5] == ["High", "Confirmed compromised", "An admin confirmed the account compromised"]
    assert isinstance(at_risk.rows[1][0], data_reports.UserLink)  # linked to the account page
    assert [r[0] for r in resolved.rows] == ["info@x.com"]
    assert resolved.rows[0][3:5] == ["Remediated", "The person reset their password securely"]
    tiles = {t.label: t for t in report.tiles}
    assert (tiles["At risk"].value, tiles["High risk"].value, tiles["Confirmed compromised"].value) == (2, 1, 1)
    assert tiles["At risk"].tone == "bad" and "1 deleted account" in tiles["At risk"].foot
    view, _ = views.rows_view(report, tiles["High risk"].rows)
    assert [r[0] for r in views.apply(report, view).sections[0].rows] == ["bob@x.com"]

    result = {r.key: r for r in evaluate(TenantSnapshot(db, tenant))}["risky_users"]
    assert result.status == "fail"
    assert result.summary == "2 accounts at risk in Entra ID Protection, 1 high or confirmed compromised"
    assert [(f.severity, f.subject, f.object_id) for f in result.findings] == [
        ("high", "bob@x.com", "u2"),
        ("medium", "ann@x.com", "u1"),
    ]
    assert all(re.search(r" since \d{1,2} [A-Z][a-z]{2} 20\d\d$", f.detail) for f in result.findings)  # a day, not ISO

    make_user(db, "admin")
    login(client, "admin")
    path = f"/tenants/{alpha}/data/risky-users"
    page = client.get(path)
    assert page.status_code == 200 and "Confirmed compromised" in page.get_data(as_text=True)
    for href in views.headline_links(report, path)["tiles"]:
        assert href and client.get(href.split("#")[0]).status_code == 200


def test_check_passes_when_nobody_is_at_risk(db, two_tenants):
    alpha, _ = two_tenants
    calm = [u for u in gd.RISKY_USERS if u["riskState"] not in ("atRisk", "confirmedCompromised")]
    tenant = _seed(db, alpha, skus=P2_SKUS, risky_users=calm)
    result = {r.key: r for r in evaluate(TenantSnapshot(db, tenant))}["risky_users"]
    assert (result.status, result.summary) == ("pass", "No accounts are at risk")
    tiles = {t.label: t for t in data_reports.build(db, tenant, "risky-users").tiles}
    assert tiles["At risk"].value == 0 and tiles["At risk"].tone == "ok"


@responses.activate
def test_check_is_unavailable_when_the_list_was_cut_short(db, two_tenants):
    alpha, _ = two_tenants
    calm = [u for u in gd.RISKY_USERS if u["riskState"] not in ("atRisk", "confirmedCompromised")]
    tenant = _seed(db, alpha, skus=P2_SKUS, risky_users=calm)
    # An account at risk could be past the page cap, so finding none proves nothing.
    unpack(db)
    db.execute("UPDATE objects SET data = json_set(data, '$.truncated', json('true')) WHERE kind = 'risky_user_status'")
    result = {r.key: r for r in evaluate(TenantSnapshot(db, tenant))}["risky_users"]
    assert result.status == "unavailable"


def test_listed_with_identity_reports_and_found_by_search():
    group = next(g for g in data_reports.grouped() if g.key == "identity")
    assert "risky-users" in [k for k, _, _ in group.reports]
    entry = next(e for a in library.catalogue() for e in a.entries if e.key == "risky-users")
    assert "compromised" in entry.search
    assert "risky-users" in next(u for u in library.USES if u.key == "compromise").reports


# -- Account page ------------------------------------------------------------------------------


def test_account_page_flags_risk(db, two_tenants):
    alpha, _ = two_tenants
    tenant = _seed(db, alpha, skus=P2_SKUS)
    bob = user_profile.build(db, tenant, "u2")
    part = {p.key: p for p in bob.parts}["risk"]
    assert part.summary == "High risk, confirmed compromised" and part.tone == "bad"
    assert any(a.severity == "high" and a.part == "risk" and "high risk" in a.text for a in bob.attention)
    info = user_profile.build(db, tenant, "u7")
    part = {p.key: p for p in info.parts}["risk"]
    assert part.summary == "Remediated" and part.tone == "ok"
    assert not any(a.part == "risk" for a in info.attention)
    # Not risky, or no P2: no part at all.
    assert "risk" not in {p.key for p in user_profile.build(db, tenant, "u3").parts}
