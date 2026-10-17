"""Client grades (``grades.py``): every check placed once, the published rules, twelve months of history
from the findings table, the anonymous comparison, and the stored rows the lists read."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from officesentry import findings, grades, store
from officesentry.checks import RULES, Finding, RuleResult, evaluate
from officesentry.findings import Lifecycle, lifecycle_map
from officesentry.reports import monthly_review, precomputed
from officesentry.snapshot import TenantSnapshot
from tests.seed import seed_tenant
from tests.test_rollups import _backdate

ISO = "%Y-%m-%dT%H:%M:%SZ"
ADMINS = grades.BY_KEY["admins"].checks
MAIL = grades.BY_KEY["mail"].checks


def _result(key, status="pass", severities=(), summary="", recommendation="Fix it. Then more.") -> RuleResult:
    rule = next(r for r in RULES if r.key == key)
    found = [Finding(s, f"thing {i}", "detail", f"obj{i}") for i, s in enumerate(severities)]
    return RuleResult(key, rule.title, rule.category, status, summary or f"{len(found)} found", recommendation, found)


def _all_pass(**overrides) -> list[RuleResult]:
    return [overrides.get(r.key) or _result(r.key) for r in RULES]


def _tenant(db, tenant_id):
    return db.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()


# -- The rules ----------------------------------------------------------------------------------


def test_every_check_is_in_exactly_one_part_or_ungraded():
    assert grades.check_placement() == []
    assert {c for p in grades.PARTS for c in p.checks} | grades.UNGRADED == {r.key for r in RULES}


def test_a_client_whose_every_check_passes_is_an_a():
    g = grades.grade(_all_pass())
    assert g.letter == "A" and [p.grade for p in g.parts] == ["A"] * 5
    assert g.sentence("Alpha") == "Alpha is an A: every check that could run passed."
    assert g.next_step("Alpha") == "" and g.next_letter is None and g.to_fix == []


def test_a_parts_grade_is_its_worst_open_finding():
    for severity, letter in grades.SEVERITY_GRADE.items():
        g = grades.grade(_all_pass(dkim_enabled=_result("dkim_enabled", "fail", [severity, "low"])))
        part = next(p for p in g.parts if p.key == "mail")
        assert (part.grade, g.letter) == (letter, letter), severity
        assert part.worst == severity and [p.key for p in part.moves] == ["dkim_enabled"]
    # Only the mail part moved; the others stay A.
    assert [p.grade for p in g.parts if p.key != "mail"] == ["A"] * 4


def test_an_info_finding_is_a_note_not_a_problem():
    g = grades.grade(_all_pass(dkim_enabled=_result("dkim_enabled", "fail", ["info"])))
    assert g.letter == "A"


def test_a_check_that_doesnt_count_never_moves_the_grade():
    g = grades.grade(_all_pass(unassigned_licences=_result("unassigned_licences", "fail", ["critical"])))
    assert g.letter == "A"


def test_the_grade_is_the_worst_part_and_each_part_says_what_moves_it():
    results = _all_pass(
        admin_mfa=_result("admin_mfa", "fail", ["critical", "critical"], "2 admins without MFA"),
        guest_admins=_result("guest_admins", "fail", ["high"], "1 guest with admin roles"),
        dkim_enabled=_result("dkim_enabled", "fail", ["medium"], "1 mail domain without DKIM"),
        spf_record=_result("spf_record", "fail", ["medium", "low"], "2 domains with SPF problems"),
        mta_sts=_result("mta_sts", "fail", ["low"]),
    )
    g = grades.grade(results)
    assert g.letter == "E" and [p.grade for p in g.parts] == ["E", "A", "C", "A", "A"]
    admins, mail = g.parts[0], g.parts[2]
    assert [p.key for p in admins.pulls] == ["admin_mfa", "guest_admins"]
    assert [p.key for p in admins.moves] == ["admin_mfa"] and admins.next_grade == "D"
    # Pulls sort by severity, then by how many things are affected, then by title.
    assert [p.key for p in mail.pulls] == ["spf_record", "dkim_enabled", "mta_sts"]
    assert [p.count for p in mail.pulls] == [2, 1, 1]
    assert [p.key for p in mail.moves] == ["spf_record", "dkim_enabled"] and mail.next_grade == "B"
    assert [p.key for p in g.worst_parts] == ["admins"]
    assert g.next_letter == "D" and [p.key for p in g.to_fix] == ["admin_mfa"]
    assert g.sentence("Alpha Ltd") == "Alpha Ltd is an E because of Admin accounts: 2 admins without MFA."
    assert g.next_step("Alpha Ltd") == "Fix 1 check in Admin accounts and Alpha Ltd is a D."
    assert g.to_fix[0].action == "Fix it"


def test_two_parts_at_the_worst_grade_both_hold_it_there():
    results = _all_pass(
        guest_admins=_result("guest_admins", "fail", ["high"], "1 guest with admin roles"),
        dkim_enabled=_result("dkim_enabled", "fail", ["high"]),
        mta_sts=_result("mta_sts", "fail", ["medium"]),
    )
    g = grades.grade(results)
    assert g.letter == "D" and [p.key for p in g.worst_parts] == ["admins", "mail"]
    assert g.next_letter == "C"  # mail still has a medium finding once its high one is fixed
    assert g.sentence("Alpha") == "Alpha is a D because of Admin accounts and Mail: 1 guest with admin roles."
    assert g.next_step("Alpha") == "Fix 2 checks in Admin accounts and Mail and Alpha is a C."


def test_accepted_risks_are_set_aside():
    results = _all_pass(admin_mfa=_result("admin_mfa", "fail", ["critical", "high"]))
    life = {("admin_mfa", "obj0"): Lifecycle("2026-01-01T00:00:00Z", None, True, "admin", "known", None, 1)}
    g = grades.grade(results, life)
    assert g.letter == "D"  # the critical finding is accepted, the high one isn't
    life[("admin_mfa", "obj1")] = Lifecycle("2026-01-01T00:00:00Z", None, True, "admin", "known", None, 2)
    assert grades.grade(results, life).letter == "A"
    assert next(r for r in grades.active(results, life) if r.key == "admin_mfa").status == "accepted"
    assert grades.grade(results, {}).letter == "E"


def test_a_part_is_only_graded_when_half_its_checks_could_run():
    missing = {k: _result(k, "unavailable", summary="Needs Intune") for k in grades.BY_KEY["devices"].checks[:5]}
    g = grades.grade(_all_pass(**missing))
    devices = next(p for p in g.parts if p.key == "devices")
    assert (
        devices.grade is None and devices.reason == "Only 4 of 9 checks could run" and devices.why == ["Needs Intune"]
    )
    assert g.letter == "A" and g.ungraded == [devices] and len(g.graded) == 4
    assert not g.partial and g.ran == g.total - 5
    missing = {k: _result(k, "unavailable", summary="Needs Intune") for k in grades.BY_KEY["devices"].checks}
    g = grades.grade(_all_pass(**missing))
    assert g.parts[3].reason == "No check could run"
    # Four checks that ran out of nine: graded, but flagged as partial.
    missing = {k: _result(k, "unavailable") for k in grades.BY_KEY["devices"].checks[:4]}
    assert grades.grade(_all_pass(**missing)).parts[3].partial


def test_no_grade_without_data():
    g = grades.grade([])
    assert g.letter is None and g.reason == "no data has been collected yet"
    assert g.sentence("Alpha") == "Alpha isn't graded yet: no data has been collected yet."
    g = grades.grade([_result(k, "unavailable") for k in ADMINS + MAIL])
    assert g.letter is None and g.reason == "no data has been collected yet"  # nothing could run
    g = grades.grade([_result(p.checks[0]) for p in grades.PARTS])  # one check a part: too few for a grade
    assert g.letter is None and g.reason == "too few checks could run" and g.ran == 5
    missing = {k: _result(k, "unavailable") for k in ADMINS + MAIL + grades.BY_KEY["sharing"].checks}
    g = grades.grade(_all_pass(**missing))
    assert g.letter == "A" and g.partial  # graded, from fewer than half the checks


def test_assess_grades_the_stored_snapshot_with_accepted_risks_aside(db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    tenant = _tenant(db, alpha)
    now = datetime.now(UTC)
    g = grades.assess(db, tenant, now)
    live = grades.grade(evaluate(TenantSnapshot(db, tenant), now), lifecycle_map(db, alpha, now))
    assert g.letter == live.letter == "E" and [p.grade for p in g.parts] == [p.grade for p in live.parts]
    assert g.ran > g.total // 2
    # Accepting every finding of the worst check moves the grade.
    worst = g.to_fix[0]
    for row in db.execute("SELECT id FROM findings WHERE tenant_id = ? AND rule_key = ?", (alpha, worst.key)):
        findings.accept(db, row["id"], "admin", "known", None)
    after = grades.assess(db, tenant, now)
    assert worst.key not in {p.key for p in after.to_fix}
    assert after.letter == g.next_letter or after.letter == g.letter  # another check at that severity may remain
    # Nothing collected: no grade, and every part says so.
    none = grades.assess(db, _tenant(db, bravo), now)
    assert none.letter is None and none.reason == "no data has been collected yet"
    assert [p.reason for p in none.parts] == ["No check could run"] * 5


# -- History ------------------------------------------------------------------------------------


def _month(now: datetime, back: int) -> date:
    return grades._month_starts(now, back + 1)[0]


def test_month_starts_and_ends():
    now = datetime(2026, 3, 15, tzinfo=UTC)
    assert grades._month_starts(now, 4) == [date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)]
    assert grades._month_end(date(2026, 2, 1)) == "2026-02-28T23:59:59Z"
    assert grades._month_end(date(2025, 12, 1)) == "2025-12-31T23:59:59Z"


def test_history_is_the_grade_on_each_month_end_from_the_findings_open_then(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    tenant = _tenant(db, alpha)
    now = datetime(2026, 10, 16, 12, tzinfo=UTC)
    _backdate(db, alpha, 100)  # first collected in early July
    first = (now - timedelta(days=100)).strftime(ISO)
    db.execute("UPDATE runs SET finished_at = ?, created_at = ? WHERE tenant_id = ?", (first, first, alpha))
    db.execute("UPDATE findings SET first_seen = ?, last_seen = ? WHERE tenant_id = ?", (first, first, alpha))
    today = grades.assess(db, tenant, now)
    assert today.letter == "E"
    critical = db.execute(
        "SELECT id FROM findings WHERE tenant_id = ? AND severity = 'critical' AND resolved_at IS NULL", (alpha,)
    ).fetchall()
    assert critical
    # The critical findings were only found in September, and one high finding was fixed in August.
    db.execute(
        f"UPDATE findings SET first_seen = '2026-09-10T00:00:00Z' WHERE id IN ({','.join('?' * len(critical))})",
        [r["id"] for r in critical],
    )
    months = grades.history(db, alpha, today, now)
    assert len(months) == grades.MONTHS and months[-1].current and months[-1].letter == "E"
    by_name = {m.label: m.letter for m in months}
    assert by_name["Jun"] is None  # before the first collection
    assert by_name["Jul"] == "D" and by_name["Aug"] == "D" and by_name["Sep"] == "E"
    assert months[0].name == "November 2025" and not months[0].current
    # An accepted risk doesn't count for the months it was accepted in.
    db.execute(
        f"UPDATE findings SET accepted_at = '2026-09-20T00:00:00Z', accepted_until = '2026-10-05T00:00:00Z'"
        f" WHERE id IN ({','.join('?' * len(critical))})",
        [r["id"] for r in critical],
    )
    assert {m.label: m.letter for m in grades.history(db, alpha, today, now)}["Sep"] == "D"
    # A finding resolved before a month-end doesn't count for it; one resolved after does.
    db.execute("UPDATE findings SET accepted_at = NULL, accepted_until = NULL WHERE tenant_id = ?", (alpha,))
    db.execute(
        "UPDATE findings SET resolved_at = '2026-08-15T00:00:00Z' WHERE tenant_id = ? AND severity IN ('high')",
        (alpha,),
    )
    by_name = {m.label: m.letter for m in grades.history(db, alpha, today, now)}
    assert by_name["Jul"] == "D" and by_name["Aug"] == "C" and by_name["Sep"] == "E"
    # A part that isn't graded today isn't graded for earlier months either.
    partial = grades.Grade("A", [p for p in today.parts if p.key != "admins"], 0, 0)
    assert "E" not in {m.letter for m in grades.history(db, alpha, partial, now)}


def test_history_without_a_grade_today(db, two_tenants):
    alpha, _ = two_tenants
    none = grades.assess(db, _tenant(db, alpha))
    months = grades.history(db, alpha, none)
    assert [m.letter for m in months] == [None] * grades.MONTHS and months[-1].current
    assert grades.trend(months) is None


def test_trend_reads_the_month_ends_before_today():
    def months(*letters):
        starts = grades._month_starts(datetime(2026, 10, 16, tzinfo=UTC), len(letters))
        return [
            grades.MonthGrade(m, x, current=i == len(letters) - 1)
            for i, (m, x) in enumerate(zip(starts, letters, strict=True))
        ]

    assert grades.trend(months(None, None, "C")).direction == "new"
    assert grades.trend(months(None, None, "C")).text == "First graded this month"
    t = grades.trend(months(None, "D", "D", "C"))
    assert (t.direction, t.previous, t.month) == ("up", "D", date(2026, 10, 1)) and t.text == "Up from D in October"
    t = grades.trend(months("B", "B", "C", "C"))
    assert (t.direction, t.previous, t.month) == (
        "down",
        "B",
        date(2026, 9, 1),
    ) and t.text == "Down from B in September"
    t = grades.trend(months(None, "C", "C", "C"))
    assert (t.direction, t.month) == ("same", date(2026, 8, 1)) and t.text == "Unchanged since August"
    assert grades.trend(months("B", "C", "B", "B")).text == "Up from C in September"
    assert grades.trend([]) is None


# -- Comparison ---------------------------------------------------------------------------------


def test_size_bands():
    assert grades.band_of(0)[2] == "up to 24 users" and grades.band_of(24)[2] == "up to 24 users"
    assert grades.band_of(25)[2] == "25 to 99 users" and grades.band_of(99.0)[2] == "25 to 99 users"
    assert grades.band_of(100)[2] == "100 to 499 users" and grades.band_of(5000)[2] == "500 or more users"
    assert grades.band_of(None) is None


def test_compare_uses_the_size_band_when_it_has_enough_peers_else_everyone():
    mine = grades.Peer("C", 40, 60, 80, 2, 1.5)
    same = [grades.Peer("D", 30, 40, 70, 5, 3.0), grades.Peer("B", 90, 70, 90, 1, 0.5), grades.Peer("E", 50, 20, 50, 9)]
    big = [grades.Peer("A", 800, 90, 99, 0, 0.0)]
    c = grades.compare(mine, same + big)
    assert c.peers == 3 and c.band == "25 to 99 users" and c.graded == 3 and c.better_than == 2
    figures = {f.key: f for f in c.figures}
    assert figures["grade"].median == "D" and figures["grade"].standing == "better"
    assert figures["score"].median == 40 and figures["score"].standing == "better"
    assert figures["mfa"].median == 70 and figures["mfa"].standing == "better"
    assert figures["severe"].median == 5 and figures["severe"].standing == "better"
    assert figures["waste"].median == 1.8 and figures["waste"].standing == "better"  # of the two that have one
    # Too few in the band: every other client, and no band label.
    c = grades.compare(mine, same[:1] + big)
    assert c.peers == 2 and c.band == "" and c.better_than == 1
    # Too few altogether: nothing is said.
    assert grades.compare(mine, same[:1]) is None and grades.compare(mine, []) is None
    # A client without a grade is still a peer for the figures, but not for the grades.
    c = grades.compare(mine, [grades.Peer(None, 30, 10), grades.Peer("C", 30, 30)])
    assert c.graded == 1 and c.better_than == 0 and {f.key: f.standing for f in c.figures}["grade"] == "same"
    assert grades.compare(grades.Peer(None, 30), same).better_than == 0


def test_median_grade_reads_as_the_better_of_a_tie():
    assert grades._median_grade(["B", "D"]) == "B" and grades._median_grade(["E", "C", "A"]) == "C"
    assert grades._median_grade([]) is None and grades._median([None, None]) is None


def test_figure_standing():
    assert grades.Figure("k", "l", 3, 5, "count").standing == "better"
    assert grades.Figure("k", "l", 3, 5, "%", "up").standing == "worse"
    assert grades.Figure("k", "l", None, 5).standing == "" and grades.Figure("k", "l", 5, 5).standing == "same"
    assert grades.Figure("k", "l", "B", "C", "grade").standing == "better"


def test_peer_of_takes_the_reviews_key_figures():
    kpis = {k: grades._Value(v) for k, v in {"users.licensed": 40, "waste": 60.0, "secure_score.pct": 55.5}.items()}
    p = grades.peer_of("B", kpis, {"critical": 1, "high": 2, "medium": 9})
    assert (p.letter, p.licensed, p.score, p.mfa, p.severe, p.waste_per_user) == ("B", 40, 55.5, None, 3, 1.5)
    assert grades.peer_of(None, {}, {}).severe is None


def test_peers_read_only_what_is_stored_and_never_a_name(db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha, precompute=True)
    seed_tenant(db, bravo, precompute=True)
    db.execute("DELETE FROM rollup_cache")
    assert grades.peers(db, [alpha, bravo]) == []  # nothing stored yet
    tenants = db.execute("SELECT * FROM tenants ORDER BY id").fetchall()
    grades.rows(db, tenants)
    assert grades.peers(db, [alpha, bravo]) == []  # a grade, but no rollup row yet
    for t in tenants:
        monthly_review.rollup_row(db, t, cached=True)
    peers = grades.peers(db, [alpha, bravo])
    assert len(peers) == 2 and {p.letter for p in peers} == {"E"} and all(p.licensed for p in peers)
    assert all(p.score is not None and p.severe is not None for p in peers)
    assert "Alpha" not in repr(peers) and "Bravo" not in repr(peers)
    assert grades.peers(db, [alpha]) and len(grades.peers(db, [alpha])) == 1 and grades.peers(db, []) == []


# -- Stored rows --------------------------------------------------------------------------------


def test_rows_are_stored_and_say_what_the_live_build_says(db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    tenants = db.execute("SELECT * FROM tenants ORDER BY id").fetchall()
    now = datetime.now(UTC)
    rows = grades.rows(db, tenants)
    assert set(rows) == {alpha, bravo}
    stored = {r["tenant_id"] for r in db.execute("SELECT tenant_id FROM summary_cache WHERE kind = 'grade'")}
    assert stored == {alpha, bravo}
    assert rows[alpha].letter == "E" and rows[alpha].trend.direction == "new" and not rows[alpha].note
    assert rows[bravo].letter is None and rows[bravo].reason == "no data has been collected yet"
    assert [p[0] for p in rows[alpha].parts] == [p.key for p in grades.PARTS]
    live = grades.rows(db, tenants, now=now)  # a row of a moment is never read from the store
    assert {k: grades._stored(v) for k, v in rows.items()} == {k: grades._stored(v) for k, v in live.items()}
    again = grades.rows(db, tenants)
    assert grades._stored(again[alpha]) == grades._stored(rows[alpha])
    assert grades.stored_letter(db, alpha) == "E" and grades.stored_letter(db, bravo) is None
    assert grades.stored_letter(db, 999) is None


def test_rows_follow_accepted_risks_and_new_findings(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    tenants = db.execute("SELECT * FROM tenants WHERE id = ?", (alpha,)).fetchall()
    before = grades.rows(db, tenants)[alpha]
    worst = grades.assess(db, tenants[0]).to_fix
    for pull in worst:
        for row in db.execute("SELECT id FROM findings WHERE tenant_id = ? AND rule_key = ?", (alpha, pull.key)):
            findings.accept(db, row["id"], "admin", "known", None)
    after = grades.rows(db, tenants)[alpha]
    assert after.letter < before.letter  # the stored row wasn't reused: accepted risks are in the fingerprint
    assert grades.stored_letter(db, alpha) == after.letter
    # A rebuild ignores a row that looks current.
    db.execute("UPDATE summary_cache SET data = '{\"letter\": \"Z\"}' WHERE kind = 'grade'")
    assert grades.rows(db, tenants)[alpha].letter == "Z"
    assert grades.rows(db, tenants, rebuild=True)[alpha].letter == after.letter


def test_a_broken_tenant_doesnt_blank_the_list(db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    tenants = db.execute("SELECT * FROM tenants WHERE id = ?", (alpha,)).fetchall()

    def boom(*a, **k):
        raise ValueError("bad data")

    monkeypatch.setattr(grades, "assess", boom)
    row = grades.rows(db, tenants)[alpha]
    assert row.letter is None and row.note == "Couldn't be worked out: ValueError"
    assert db.execute("SELECT COUNT(*) FROM summary_cache WHERE kind = 'grade'").fetchone()[0] == 0


def test_the_worker_builds_the_grade_row_with_the_others(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    precomputed.refresh(db, store.get_tenant(db, alpha))
    assert grades.stored_letter(db, alpha) == "E"


def test_the_grade_can_be_kept_out_of_client_reports(db):
    assert grades.shown_to_clients(db) and grades.compared_for_clients(db)
    grades.set_shown(db, True, False)
    assert grades.shown_to_clients(db) and not grades.compared_for_clients(db)
    grades.set_shown(db, False, True)
    assert not grades.shown_to_clients(db) and grades.compared_for_clients(db)
