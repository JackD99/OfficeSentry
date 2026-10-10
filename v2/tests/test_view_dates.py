"""Dates in report cells are read as dates by the table views, whichever way a builder writes them."""

from datetime import UTC, date, datetime

from officesentry.reports import views
from officesentry.reports.data_reports import _seen

TODAY = date(2026, 10, 2)


def test_seen_today_is_still_a_date():
    now = datetime(2026, 10, 2, 15, 0, tzinfo=UTC)
    cell = _seen("2026-10-02T09:00:00Z", now)
    assert cell == "2026-10-02 (today)"
    assert views._age_matches(cell, "lt30", TODAY) and not views._age_matches(cell, "none", TODAY)


def test_prose_dates_get_age_presets_and_sort_by_date():
    assert views.as_date("3 Sep 2026") == date(2026, 9, 3)
    assert views.as_date("31 Feb 2026") is None and views.as_date("Never") is None
    cells = ["3 Sep 2026", "12 Jan 2025", "Never"]
    assert views._kind(cells) == "date"
    assert views._age_matches("3 Sep 2026", "lt30", TODAY)
    assert views._age_matches("12 Jan 2025", "gt365", TODAY)
    assert sorted(cells[:2], key=views.sort_key) == ["12 Jan 2025", "3 Sep 2026"]
