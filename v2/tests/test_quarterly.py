"""The quarterly review: the quarter's figures and the report designer's quarterly blocks and starter."""

from datetime import UTC, datetime

from officesentry import projects, store
from officesentry.reports import designs, quarterly
from officesentry.reports.monthly_review import ISO, _month_starts, build
from tests.conftest import login, make_user
from tests.seed import seed_tenant


def _text(resp) -> str:
    return resp.get_data(as_text=True)


def test_quarter_label_names_the_three_months_ending_now():
    assert quarterly.quarter_label(datetime(2026, 10, 10, tzinfo=UTC)) == "August to October 2026"
    assert quarterly.quarter_label(datetime(2027, 1, 5, tzinfo=UTC)) == "November 2026 to January 2027"


def test_quarter_from_a_seeded_tenant(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    now = datetime.now(UTC)
    starts = _month_starts(now, 3)
    # A score stored last month, and a finding fixed then.
    last = datetime(starts[1].year, starts[1].month, 1, 9, tzinfo=UTC).strftime(ISO)
    db.execute(
        "INSERT INTO metrics (run_id, tenant_id, key, value, captured_at) VALUES (NULL, ?, 'secure_score.pct', 30, ?)",
        (alpha, last),
    )
    db.execute(
        "INSERT INTO findings (tenant_id, rule_key, object_id, severity, subject, detail, first_seen, last_seen,"
        " resolved_at) VALUES (?, 'legacy_auth', 'x', 'high', 'whole tenant', '', ?, ?, ?)",
        (alpha, "2020-01-01T00:00:00Z", last, last),
    )
    db.execute("INSERT INTO sku_prices (sku_part_number, monthly_price, updated_at) VALUES ('SPB', 18.1, ?)", (last,))
    tenant = store.get_tenant(db, alpha)
    q = quarterly.build(db, tenant, build(db, tenant, now), now)
    assert q.start == starts[0] and q.end == now.date() and q.label == quarterly.quarter_label(now)
    assert [m.month for m in q.months] == starts
    assert [m.score for m in q.months] == [None, 30, 42] and (q.score_from, q.score_to) == (30, 42)
    assert q.months[-1].found == 0 and q.months[-1].fixed == 0  # the seed's findings are the baseline
    assert q.fixed_count == 1 and q.fixed[0].subjects == ["whole tenant"]
    assert "Findings found and fixed each month" in q.chart
    assert [(ln.name, ln.purchased, ln.monthly_cost) for ln in q.spend] == [
        ("Microsoft 365 Business Premium", 10, 181.0)
    ]
    assert q.monthly_spend == 181.0 and q.monthly_waste == q.spend[0].monthly_waste > 0 and q.unpriced == 0
    assert q.projects.projects or q.projects.quick_fixes
    assert all(i.window != "No date" for i in q.upcoming)


def test_an_empty_tenant_has_an_empty_quarter(db, two_tenants):
    _, bravo = two_tenants
    tenant = store.get_tenant(db, bravo)
    q = quarterly.build(db, tenant, build(db, tenant), datetime.now(UTC))
    assert [m.score for m in q.months] == [None] * 3 and [m.found for m in q.months] == [None] * 3
    assert q.chart == "" and q.fixed == [] and q.spend == [] and q.upcoming == []
    assert q.score_from is None and q.score_to is None


def test_the_quarterly_starter_renders_for_a_client(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    tenant = store.get_tenant(db, alpha)
    assessment = projects.assess(db, tenant)
    if assessment.projects:
        projects.set_status(db, alpha, assessment.projects[0].project.key, "agreed", "Go ahead", "admin")
    db.execute("INSERT INTO sku_prices (sku_part_number, monthly_price, updated_at) VALUES ('SPB', 18.1, '2026-01-01')")
    make_user(db, "admin")
    login(client, "admin")
    did = designs.create(db, "Quarterly", "admin", designs.STARTERS["quarterly"][2])
    page = _text(client.get(f"/tenants/{alpha}/designs/{did}"))
    now = datetime.now(UTC)
    assert "Quarterly review" in page and quarterly.quarter_label(now) in page
    for heading in ("The last three months", "Licence spend", "Projects", "What we recommend next"):
        assert heading in page, heading
    assert '<table class="quarter">' in page and "Secure Score" in page
    assert "Licences a month" in page and "Licences a year" in page and "Wasted a month" in page
    if assessment.projects:
        assert assessment.projects[0].project.title in page and 'class="proj-status st-agreed"' in page
    # Nothing fixed this quarter and nothing due: those blocks fall away rather than showing empty tables.
    assert "What was fixed" not in page
    # An empty tenant still opens, with the words and none of the data blocks.
    assert client.get(f"/tenants/{bravo}/designs/{did}").status_code == 200
    preview = _text(client.get(f"/reporting/designs/{did}/preview?tenant={bravo}"))
    assert "Nothing has been collected for this client yet." in preview
    assert "No paid licences in the data yet." in preview


def test_quarterly_widgets_are_in_the_palette_and_take_their_settings():
    palette = {w["key"]: w for g in designs.palette() for w in g["widgets"]}
    for key in ("quarter_trend", "quarter_fixed", "projects", "licence_spend", "upcoming"):
        assert key in palette and palette[key]["halfable"] is False, key
    doc = designs.clean(
        {"blocks": [{"kind": "quarter_fixed", "settings": {"max": 999, "subjects": "2"}}, {"kind": "upcoming"}]}
    )
    assert doc["blocks"][0]["settings"] == {"heading": "What was fixed", "max": 50, "subjects": 2}
    assert doc["blocks"][1]["settings"] == {"heading": "Coming up", "max": 10}
