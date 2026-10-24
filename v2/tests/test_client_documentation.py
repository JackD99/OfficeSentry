"""Client documentation (designs.TOPICS and the "documentation" starter): how a client's Microsoft 365 is set up,
one topic per block, rebuilt whenever it's sent."""

from __future__ import annotations

import re

from officesentry import store
from officesentry.reports import data_reports, designs
from tests.conftest import login, make_user
from tests.seed import seed_tenant


def _text(resp) -> str:
    return resp.get_data(as_text=True)


def test_every_topic_names_tables_its_report_has(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    tenant = store.get_tenant(db, alpha)
    for key, topic in designs.TOPICS.items():
        assert topic.report in data_reports.REPORTS, key
        sections = data_reports.build(db, tenant, topic.report).sections
        titles = [s.title for s in sections]
        # A renamed table would quietly drop out of every client's documentation, and a renamed column back in.
        assert set(topic.sections) <= set(titles), (key, sorted(set(topic.sections) - set(titles)))
        assert set(topic.drop) <= {c for s in sections if s.title in topic.sections for c in s.columns}, key
        assert not any("attention" in t.lower() for t in topic.sections), key  # documentation, not a to-do list


def test_documentation_starter_documents_the_setup(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    resp = client.post("/reporting/designs", data={"starter": "documentation"})
    design_id = int(re.search(r"/reporting/designs/(\d+)", resp.headers["Location"]).group(1))
    row = designs.get(db, design_id)
    assert row["name"] == "Client documentation"
    assert designs.detail_reports(row) >= {"domains", "conditional-access", "intune-policies", "devices"}
    kinds = [b["kind"] for b in designs.document(row)["blocks"]]
    assert kinds.count("doc_topic") == len(designs.TOPICS)

    page = _text(client.get(f"/tenants/{alpha}/designs/{design_id}"))
    assert "Microsoft 365 documentation" in page and "How Alpha Ltd&#39;s Microsoft 365 is set up" in page
    for topic in designs.TOPICS.values():
        assert f">{topic.title}<" in page, topic.title
    assert ">Policies <" in page and ">Compliance policies <" in page  # the tables that describe the setup
    assert "Needs attention" not in page  # and not the reports' to-do lists

    # A client with nothing collected yet: the page opens, and the preview says why each section is empty.
    assert client.get(f"/tenants/{bravo}/designs/{design_id}").status_code == 200
    preview = _text(client.get(f"/reporting/designs/{design_id}/preview?tenant={bravo}"))
    assert "has nothing to show" in preview


def test_a_section_needs_its_report(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    tenant = store.get_tenant(db, alpha)
    ctx = designs.Context(db, tenant, opens=lambda key: key != "conditional-access")
    assert designs.b_doc_topic(ctx, {"topic": "conditional_access"}) == {
        "empty": "Left out: you can't open this report for this client."
    }
    assert designs.b_doc_topic(ctx, {"topic": "nope"})["empty"].startswith("Choose")
    built = designs.b_doc_topic(ctx, {"topic": "devices", "max_rows": 3})
    assert built["title"] == "Devices" and [s["section"].title for s in built["sections"]] == [
        "Operating systems",
        "Managed devices",
    ]
    assert all(len(s["rows"]) <= 3 for s in built["sections"])
