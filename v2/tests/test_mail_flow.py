"""Mail flow: delivered, Junk, quarantined and failed mail per domain from the message trace, kept as daily
counts read a little at a time, and Microsoft's top spam and phishing recipients.

Exchange is simulated with ``responses`` (``tests/mailflowdata.py``), through a full seeded collection.
"""

import copy
import json
from datetime import UTC, datetime, timedelta

from officesentry import store
from officesentry.collectors import mail_flow
from officesentry.reports import data_reports
from tests import graphdata as gd
from tests import seed
from tests.seed import seed_tenant
from tests.test_collectors import SKUS

NOW = datetime.now(UTC)
DENIED = (403, {"error": {"code": "Forbidden", "message": "Access denied."}})


def _report(db, tenant_id):
    return data_reports.build(db, store.get_tenant(db, tenant_id), "mail-flow", now=NOW)


def _table(report, title):
    section = next(s for s in report.sections if s.title == title)
    return [dict(zip(section.columns, row, strict=True)) for row in section.rows]


def _run(db, tenant_id):
    return store.latest_run(db, tenant_id, "mail_flow")


def test_counts_mail_per_domain_and_lists_the_top_spam_and_phishing_recipients(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    run = _run(db, alpha)
    assert run["status"] == "succeeded"
    report = _report(db, alpha)
    assert report.unavailable is None

    # In: two newsletters, one to Junk, one quarantined. Out: one delivered, one bounced. One between
    # colleagues. A list expansion, mail that isn't ours and mail from the last hour aren't counted.
    (domain,) = _table(report, "Mail per domain")
    assert domain == {
        "Domain": "x.com",
        "Received": 4,
        "Sent": 2,
        "Between colleagues": 1,
        "Delivered": 4,
        "Junk": 1,
        "Quarantined": 1,
        "Failed": 1,
        "Failed share": "14%",
    }
    assert next(s for s in report.sections if s.title == "Mail per domain").flags == ["medium"]
    assert _table(report, "Top phishing recipients") == [{"Rank": 1, "Recipient": "ann@x.com", "Phishing messages": 3}]
    assert [(r["Recipient"], r["Spam messages"]) for r in _table(report, "Top spam recipients")] == [
        ("ann@x.com", 41),
        ("bob@x.com", 7),
    ]
    daily = _table(report, "Mail per day")
    assert sum(r["Delivered"] for r in daily) == 4 and daily[0]["Date"] >= daily[-1]["Date"]
    tiles = {t.label: t.value for t in report.tiles}
    assert tiles == {"Messages": 7, "Delivered": "57%", "Quarantined": 1, "Failed": 1, "Phishing recipients": 1}
    assert not report.notes and "an hour behind" in report.footnote

    # Only counts are kept: no subjects, senders or recipients from the trace.
    stored = json.dumps([store.run_objects(db, run["id"], kind) for kind in ("mail_flow", "mail_flow_status")])
    assert "Private subject" not in stored and "news@outside.example" not in stored and "ann@" not in stored


def test_trace_is_read_a_day_at_a_time_and_only_what_is_new(db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    monkeypatch.setattr(seed, "PAGE", 1000)  # one page a day, so the calls count the days
    seed_tenant(db, alpha)
    first = _run(db, alpha)
    (calls,) = db.execute(
        "SELECT calls FROM run_requests WHERE run_id = ? AND endpoint = 'Exchange Get-MessageTraceV2'", (first["id"],)
    ).fetchone()
    assert 10 <= calls <= 11  # ten days, cut at midnight
    status = store.run_objects(db, first["id"], "mail_flow_status")[0]
    read_to = datetime.fromisoformat(status["readTo"].replace("Z", "+00:00"))
    assert timedelta(minutes=59) < datetime.now(UTC) - read_to < timedelta(minutes=62)

    # A second collection adds only what arrived since, so nothing is counted twice.
    seed_tenant(db, alpha)
    assert _run(db, alpha)["id"] != first["id"]
    assert {t.label: t.value for t in _report(db, alpha).tiles}["Messages"] == 7
    assert store.run_objects(db, _run(db, alpha)["id"], "mail_flow_status")[0]["watchedFrom"] == status["watchedFrom"]


def test_query_budget_leaves_the_rest_for_next_run(db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    monkeypatch.setattr(mail_flow, "TRACE_QUERIES", 3)
    seed_tenant(db, alpha)
    run = _run(db, alpha)
    assert run["status"] == "partial"
    status = store.run_objects(db, run["id"], "mail_flow_status")[0]
    assert status["readTo"] < (NOW - timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert {t.label: t.value for t in _report(db, alpha).tiles}["Messages"] == 0  # the rows are all newer

    monkeypatch.setattr(mail_flow, "TRACE_QUERIES", 30)
    seed_tenant(db, alpha)
    assert {t.label: t.value for t in _report(db, alpha).tiles}["Messages"] == 7


def test_without_the_message_trace_the_top_recipients_still_show(db, two_tenants):
    alpha, _ = two_tenants
    data = copy.deepcopy(gd.EXCHANGE)
    data["Get-MessageTraceV2"] = DENIED
    seed_tenant(db, alpha, exchange=data)
    assert _run(db, alpha)["status"] == "partial"
    report = _report(db, alpha)
    assert report.notes and "message trace" in report.notes[0] and "Access denied" in report.notes[0]
    assert _table(report, "Mail per domain")[0]["Received"] == 0
    assert len(_table(report, "Top spam recipients")) == 2


def test_without_the_summary_the_tile_says_not_read(db, two_tenants):
    alpha, _ = two_tenants
    data = copy.deepcopy(gd.EXCHANGE)
    data["Get-MailTrafficSummaryReport"] = DENIED
    seed_tenant(db, alpha, exchange=data)
    report = _report(db, alpha)
    assert {t.label: t.value for t in report.tiles}["Phishing recipients"] == "Not read"
    section = next(s for s in report.sections if s.title == "Top phishing recipients")
    assert not section.rows and "wasn't available" in section.empty
    assert _table(report, "Mail per domain")[0]["Received"] == 4


def test_unavailable_before_collection_and_without_exchange(db, two_tenants):
    alpha, beta = two_tenants
    assert "hasn't been collected" in _report(db, beta).unavailable
    no_exchange = [{**SKUS[0], "servicePlans": [{"servicePlanName": "AAD_PREMIUM", "provisioningStatus": "Success"}]}]
    seed_tenant(db, alpha, skus=no_exchange)
    assert _report(db, alpha).unavailable == "This tenant has no Exchange Online licence."


def test_classify_counts_a_message_against_one_of_our_domains():
    ours = {"x.com", "x.co.uk"}
    row = {"Status": "Delivered", "SenderAddress": "a@outside.example", "RecipientAddress": "B@X.co.uk"}
    assert mail_flow.classify(row, ours) == ("x.co.uk", "in", "delivered")
    assert mail_flow.classify({**row, "SenderAddress": "a@x.com"}, ours) == ("x.co.uk", "internal", "delivered")
    assert mail_flow.classify({**row, "SenderAddress": "a@x.com", "RecipientAddress": "c@o.example"}, ours) == (
        "x.com",
        "out",
        "delivered",
    )
    assert mail_flow.classify({**row, "Status": "Expanded"}, ours) is None
    assert mail_flow.classify({**row, "RecipientAddress": "c@o.example"}, ours) is None
