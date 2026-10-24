"""Charts on report tables (drawn from the rows the view leaves, each bar opening its rows), sparklines on
headline figures and the dashboard, and the storage forecast."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from html import unescape
from types import SimpleNamespace

from officesentry import store
from officesentry.reports import charts, export, views
from officesentry.reports.data_reports import Chart, DataReport, Section, metric_trend
from officesentry.reports.monthly_review import dashboard_trends
from officesentry.reports.sharepoint import forecast
from officesentry.snapshot import TenantSnapshot
from tests.conftest import login, make_user
from tests.seed import seed_tenant

NOW = datetime(2026, 10, 10, 9, tzinfo=UTC)
ISO = "%Y-%m-%dT%H:%M:%SZ"


def _bars(html: str) -> list[tuple[str, str]]:
    """(label, value) of each bar, in order."""
    return [
        (unescape(a), unescape(b))
        for a, b in re.findall(r'<span class="bar-label">(.*?)</span>.*?<span class="bar-value">(.*?)</span>', html)
    ]


def _hrefs(html: str) -> list[str]:
    return [unescape(h) for h in re.findall(r'<a class="bar[^"]*" href="([^"]+)"', html)]


def _report(*sections: Section) -> DataReport:
    return DataReport("test", "Test", "", {"id": 1, "name": "Test"}, NOW, sections=list(sections))


def _countries() -> Section:
    s = Section("Sign-ins by country", ["Country", "Sign-ins", "Succeeded", "Failed"])
    s.chart = Chart("Country", ("Succeeded", "Failed"), total="Sign-ins", tones=("1", "bad"), rest="Prompts")
    for row in (["GB", 100, 90, 4], ["FR", 10, 10, 0], ["RU", 30, 0, 30]):
        s.add(row)
    return s


# -- Bars --------------------------------------------------------------------------------------


def test_bars_escape_text_and_carry_a_legend_for_two_series():
    html = charts.bars(
        [charts.Bar("<b>A</b>", [3, 1], "4", href="/x?f=a&b"), charts.Bar("B", [0, 0], "0")],
        label='Chart "one"',
        series=[("Yes", "1"), ("No", "bad")],
        title="By thing",
    )
    assert "<b>" not in html and "&lt;b&gt;A&lt;/b&gt;" in html and 'aria-label="Chart &quot;one&quot;"' in html
    assert 'class="legend"' in html and "Yes" in html and "No" in html
    assert 'href="/x?f=a&amp;b"' in html and "aria-current" not in html and "aria-pressed" not in html
    assert 'class="zero"' in html  # a zero bar still shows where it would start
    assert html.count('class="gap"') == 1  # 2px between the two parts of A, none before the first


def test_one_series_has_no_legend_and_no_bars_draw_nothing():
    html = charts.bars([charts.Bar("A", [1], "1")], label="x", title="By A")
    assert 'class="legend"' not in html and '<span class="chart-title">By A</span>' in html
    assert charts.bars([], label="x") == ""


# -- Charts that follow the table --------------------------------------------------------------


def test_chart_ranks_by_the_stated_total_and_greys_what_the_series_leave():
    report = views.figures(_report(_countries()))
    html = report.sections[0].figure
    # Sign-ins rank the bars; the 6 GB sign-ins that neither succeeded nor failed are the grey "Prompts" part.
    assert _bars(html) == [("GB", "100"), ("RU", "30"), ("FR", "10")]
    assert "Prompts" in html and "tone-muted" in html and "tone-bad" in html
    assert "GB: 100 sign-ins: 90 succeeded, 4 failed, 6 prompts" in unescape(html)
    assert "<a " not in html  # a download's bars aren't links
    assert '<span class="chart-title">' not in html  # "Sign-ins by country" already says "by country"


def test_bars_link_to_their_rows_and_the_chart_follows_the_filter():
    report = _report(_countries())
    tv = views.table_view(report, 0, views.View(), "/r")
    assert _hrefs(tv.chart)[0] == "/r?f=sign-ins-by-country%2Fcountry%3AGB#t-sign-ins-by-country"
    view = views.View.parse("f=sign-ins-by-country/country:GB")
    tv = views.table_view(report, 0, view, "/r")
    assert _bars(tv.chart) == [("GB", "100")] and len(tv.rows) == 1
    assert 'class="bar on"' in tv.chart and 'aria-current="true"' in tv.chart  # links can't be aria-pressed
    assert _hrefs(tv.chart) == ["/r#t-sign-ins-by-country"]  # clicking it again shows every row
    # The download applies the same view, so its chart is the filtered one too.
    assert _bars(views.apply(report, view).sections[0].figure) == [("GB", "100")]


def test_list_cells_count_each_item_once_and_the_rest_fold_into_other():
    s = Section("Admins", ["Account", "Roles"])
    s.chart = Chart("Roles", title="By role", top=2)
    for name, roles in (("a", ["Global", "User"]), ("b", ["Global", "Global"]), ("c", ["Exchange"]), ("d", ["Teams"])):
        s.add([name, roles])
    html = views.figures(_report(s)).sections[0].figure
    assert _bars(html) == [("Global", "2"), ("Exchange", "1"), ("Other (2)", "2")]
    assert '<span class="chart-title">By role</span>' in html
    assert views.chart(s, "admins", []) == ""  # no rows left, no chart


def test_a_chart_by_the_flag_column_names_the_levels():
    s = Section("Devices", ["Device"])
    s.chart = Chart(views.FLAG, ranked=False)
    for name, flag in (("a", "high"), ("b", None), ("c", "high")):
        s.add([name], flag)
    html = views.figures(_report(s)).sections[0].figure
    assert _bars(html) == [("High", "2"), ("No flag", "1")]
    tv = views.table_view(_report(s), 0, views.View(), "/r")
    assert "f=devices%2Fflag%3Ahigh" in _hrefs(tv.chart)[0]


def test_sizes_add_up_and_read_as_sizes():
    s = Section("Sites", ["Site", "Storage"])
    s.chart = Chart("Site", ("Storage",), title="Largest sites", other=False, top=1)
    for row in (["A", "1.5 GB"], ["B", "512 MB"], ["A", "0.5 GB"]):
        s.add(row)
    assert _bars(views.figures(_report(s)).sections[0].figure) == [("A", "2.0 GB")]


def test_a_chart_naming_a_missing_column_draws_nothing():
    s = Section("T", ["A"], rows=[["x"]])
    s.chart = Chart("Nope")
    assert views.chart(s, "t", [0]) == ""


# -- Real reports ------------------------------------------------------------------------------


def test_sign_ins_page_draws_its_charts_and_a_bar_opens_its_rows(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/data/sign-ins").get_data(as_text=True)
    assert page.count('<figure class="barchart"') >= 2  # by country and by app
    href = next(h for h in _hrefs(page) if "country%3A" in h)
    country = href.split("country%3A")[1].split("#")[0]
    filtered = client.get(href).get_data(as_text=True)
    assert 'class="bar on"' in filtered
    chart = filtered.split('aria-label="Sign-ins by country"')[1].split("</figure>")[0]
    assert len(_bars(chart)) == 1 and unescape(country.replace("+", " ")) in chart.replace("%20", " ")


def test_the_pdf_carries_the_chart(client, db, two_tenants, monkeypatch):
    monkeypatch.setattr(export, "pdf_available", lambda: False)  # the PDF's HTML, served as a print page
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    html = client.get(f"/tenants/{alpha}/data/sign-ins.pdf").get_data(as_text=True)
    assert '<figure class="barchart" aria-label="Sign-ins by country"' in html
    assert '<a class="bar' not in html


# -- Sparklines --------------------------------------------------------------------------------


def test_sparkline_needs_three_points_and_says_its_range():
    assert charts.sparkline([1, 2], label="x") == ""
    html = charts.sparkline([10, 12, 11, 15], label="Since 1 Jul: from 10 to 15")
    assert 'role="img"' in html and "<title>Since 1 Jul: from 10 to 15</title>" in html
    assert "spark-up" in html and html.count("<path") == 1 and "spark-end" in html
    assert "spark-flat" in charts.sparkline([5, 5, 5], label="x")


def test_a_small_wobble_stays_small():
    ys = [float(y) for y in re.findall(r"[ML][\d.]+,([\d.]+)", charts.sparkline([100, 101, 100], label="x"))]
    assert max(ys) - min(ys) < 4  # 1% of the value isn't drawn across the whole 16px height


def _metric(db, tenant_id, key, value, when):
    db.execute(
        "INSERT INTO metrics (run_id, tenant_id, key, value, captured_at) VALUES (NULL, ?, ?, ?, ?)",
        (tenant_id, key, value, when.strftime(ISO)),
    )


def test_metric_trend_takes_each_days_last_value(db, two_tenants):
    alpha, _ = two_tenants
    snap = TenantSnapshot(db, store.get_tenant(db, alpha))
    for days, value in ((100, 1), (60, 5), (60, 6), (30, 8)):
        _metric(db, alpha, "x.count", value, NOW - timedelta(days=days, hours=-1 if value == 6 else 0))
    assert metric_trend(snap, "x.count", NOW) is None  # two days inside 90: no line
    _metric(db, alpha, "x.count", 9, NOW - timedelta(days=1))
    trend = metric_trend(snap, "x.count", NOW, fmt=lambda v: f"{v:.0f} things")
    assert trend.values == [6, 8, 9]
    assert trend.label == "Since 11 Aug 2026: from 6 things to 9 things"


def test_report_tiles_carry_their_trend(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    for days, value in ((60, 50), (30, 70)):
        _metric(db, alpha, "signins.total", value, datetime.now(UTC) - timedelta(days=days))
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/data/sign-ins").get_data(as_text=True)
    assert 'class="value has-spark"' in page and '<svg class="spark' in page


# -- Storage forecast --------------------------------------------------------------------------


def test_forecast_runs_to_the_quota_or_three_months_ahead():
    gb = 1024**3
    points = [(date(2026, 8, 1), 50 * gb), (date(2026, 9, 9), 60 * gb), (date(2026, 10, 9), 90 * gb)]
    ((day, value),) = forecast(points, NOW)  # 30 GB in 30 days, a GB a day
    assert day == date(2026, 10, 9) + timedelta(days=91) and round(value / gb) == 181
    ((day, value),) = forecast(points, NOW, quota=100 * gb)
    assert day == date(2026, 10, 19) and round(value / gb) == 100  # the day it reaches the limit
    assert forecast(points, NOW, quota=80 * gb) == []  # already over
    assert forecast(list(reversed(points[1:])), NOW) == []  # shrinking
    assert forecast(points[-1:], NOW) == []  # no history a month back


# -- Dashboard ---------------------------------------------------------------------------------


def _row(tenant_id, history, critical=0, high=0):
    return {
        "tenant": {"id": tenant_id},
        "kpis": {"secure_score.pct": SimpleNamespace(history=history)},
        "counts": {"critical": critical, "high": high},
    }


def test_dashboard_trends_average_the_score_and_count_open_severe_findings(db, two_tenants):
    alpha, bravo = two_tenants
    day = lambda n: (NOW - timedelta(days=n)).date().isoformat()  # noqa: E731
    rows = [
        _row(alpha, [[day(80), 40], [day(40), 50], [day(1), 60]], critical=1, high=1),
        _row(bravo, [[day(80), 60], [day(1), 80]]),
    ]
    ran = (NOW - timedelta(days=50)).strftime(ISO)
    db.execute(
        "INSERT INTO runs (tenant_id, collector, status, trigger, created_at, started_at, finished_at) "
        "VALUES (?, 'x', 'succeeded', 'manual', ?, ?, ?)",
        (alpha, ran, ran, ran),
    )
    for first, resolved, accepted in ((30, None, None), (45, 10, None), (45, None, 20)):
        db.execute(
            "INSERT INTO findings (tenant_id, rule_key, object_id, severity, subject, detail, first_seen,"
            " last_seen, resolved_at, accepted_at) VALUES (?, ?, '', 'high', '', '', ?, ?, ?, ?)",
            (
                alpha,
                f"r{first}{resolved}{accepted}",
                (NOW - timedelta(days=first)).strftime(ISO),
                NOW.strftime(ISO),
                *[(NOW - timedelta(days=d)).strftime(ISO) if d else None for d in (resolved, accepted)],
            ),
        )
    out = dashboard_trends(db, rows, NOW)
    score = out["score_trend"]
    assert score.values[0] == 50 and score.values[-1] == 70 and score.first == "50%" and score.last == "70%"
    severe = out["severe_trend"]
    assert severe.values[-1] == 2  # today's live count closes the line
    assert severe.values[0] == 0 and max(severe.values) == 3  # none before the first run; all three at one point
    assert dashboard_trends(db, [], NOW) == {}


def test_dashboard_shows_the_lines(client, db, two_tenants):
    alpha, _ = two_tenants
    for days, value in ((60, 30), (30, 35), (10, 38)):
        _metric(db, alpha, "secure_score.pct", value, datetime.now(UTC) - timedelta(days=days))
    seed_tenant(db, alpha)  # after the history, so the cached dashboard row has it
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/").get_data(as_text=True)
    assert 'class="kpi-cell"' in page and '<svg class="spark' in page
    assert "the line under a figure is its last 90 days" in page


def test_a_clients_overview_draws_its_figures_lines_and_how_its_findings_split(client, db, two_tenants):
    alpha, _ = two_tenants
    for days, value in ((60, 30), (30, 35), (10, 38)):
        _metric(db, alpha, "secure_score.pct", value, datetime.now(UTC) - timedelta(days=days))
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}").get_data(as_text=True)
    tiles = page[page.index('<div class="tiles">') : page.index('<div class="grid-2">')]
    score = tiles[tiles.index(">Secure Score<") :]
    score = score[: score.index("</a>")]
    assert 'class="value has-spark"' in score and '<svg class="spark' in score
    assert "Secure Score since" in score  # the line says its range
    # Every level with findings has its part of the bar, and the counts are there in words too.
    bar = page[page.index('<div class="sev-split">') :]
    bar = bar[: bar.index("</div>")]
    key = re.findall(r"</i>(\d+) (critical|high|medium|low)</span>", bar)
    assert [level for _, level in key] == ["critical", "high", "medium", "low"]
    assert {level for n, level in key if int(n)} == set(re.findall(r'<rect class="(\w+)"', bar))
    widths = [float(w) for w in re.findall(r'<rect [^>]*width="([\d.]+)%"', bar)]
    assert widths and all(w >= 2 for w in widths) and 99 <= sum(widths) <= 104


def test_month_columns_draws_two_columns_a_month_with_counts_and_no_data_tracks():
    from datetime import date

    from officesentry.reports.charts import month_columns

    months = [(date(2026, 8, 1), None, None), (date(2026, 9, 1), 3, 1), (date(2026, 10, 1), 0, 2)]
    svg = month_columns(months, label="Found & fixed", series=("Found", "Fixed"))
    assert 'aria-label="Found &amp; fixed"' in svg
    assert svg.count('class="nodata"') == 1 and "August 2026: no data yet" in svg
    assert svg.count('class="part tone-2"') == 1 and svg.count('class="part tone-1"') == 2  # a zero draws no column
    assert svg.count('class="count"') == 4 and "September 2026: 3 found, 1 fixed" in svg
    assert ">Aug 2026<" in svg and ">Sep<" in svg and ">Oct<" in svg
    assert month_columns([], label="x") == ""
