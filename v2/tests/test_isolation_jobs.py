"""Tenant isolation in the background: what the worker emails about one client never carries another's.

``tests/test_isolation_walk.py`` opens every page and download; the worker builds its emails and files on its
own, outside any request. Here client A has one of everything the worker sends about a client: a report email
with every report it can include (each detail report, the monthly review and its summary, the insurance
evidence pack and a report design) and saved views of A's reports, a digest and saved views for A's group,
an alert email and the month-end plan's approved month. B is seeded alongside with its mark (``B_MARK``) in
every name, and with the audit log add-on's records. Every email is read whole: its bodies, and each
attachment with spreadsheets, Word files and zips read inside and PDFs as the page they're made from.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from officesentry import delivery, month_end, store
from officesentry.delivery import ScheduledView
from tests.test_email import _smtp_ready, fake_smtp, sends  # noqa: F401 (autouse fixture)
from tests.test_isolation_walk import B_MARK, B_WORDS, MARKED_REPORTS, _pdf_as_page, world  # noqa: F401 (fixtures)

TEAM = "team@msp.example"


def _readable(msg) -> bytes:
    """Every part of an email as a person would read it: the bodies, and each attachment with the files inside
    a spreadsheet, Word file or zip read out."""
    out = []
    for part in msg.walk():
        data = part.get_payload(decode=True)
        if not data:
            continue
        if data[:4] == b"PK\x03\x04":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                data = b"\n".join(z.read(name) for name in z.namelist())
        out.append(data)
    return b"\n".join(out)


def _views(formats=("xlsx", "csv", "pdf")) -> list[ScheduledView]:
    """A saved view of each report where B's mark would show, in each format a view can be sent in."""
    return [
        ScheduledView(key, f"Everyone {fmt}", f"sort={key}/name" if key == "all-users" else "", fmt)
        for key in MARKED_REPORTS
        for fmt in formats
    ]


def _group_schedule(db, reports: list[str], group_id: int, contact: int) -> int:
    sid = db.execute(
        "INSERT INTO email_schedules (kind, reports, include_xlsx, frequency, hour, group_id, created_at)"
        " VALUES ('digest', ?, 1, 'weekly', 8, ?, '2026-01-01T00:00:00Z')",
        (json.dumps(reports), group_id),
    ).lastrowid
    delivery.set_recipients(db, "schedule", sid, [contact])
    return sid


@pytest.fixture
def emails_about_alpha(app, db, vault, world, _pdf_as_page):  # noqa: F811
    """Everything the worker sends about A or A's group, as (subject, recipients, readable bytes)."""
    alpha, bravo, ids = world
    _smtp_ready(db)
    # Nothing of B's goes out: whatever is sent below is about A.
    db.execute("UPDATE email_schedules SET enabled = 0 WHERE tenant_id = ?", (bravo,))
    db.execute("UPDATE email_alerts SET enabled = 0 WHERE tenant_id = ?", (bravo,))
    mine = ids["alpha"]
    contact = mine["contact_id"]
    tenant = store.get_tenant(db, alpha)

    # A's report email: every report a schedule can include, with spreadsheets, and saved views of A's reports.
    every = [o.key for o in delivery.catalog(db)]
    assert f"design:{ids['design_id']}" in every and "evidence" in every
    db.execute(
        "UPDATE email_schedules SET reports = ?, include_xlsx = 1 WHERE id = ?",
        (json.dumps(every), mine["schedule_id"]),
    )
    delivery.set_recipients(db, "schedule", mine["schedule_id"], [contact])
    delivery.set_schedule_views(db, mine["schedule_id"], _views())

    # The team's digest and saved views, for A's group.
    team = delivery.add_contact(db, None, "Team", TEAM)
    _group_schedule(db, [], ids["Alpha group"], team)
    views = _group_schedule(db, [delivery.VIEWS_KEY], ids["Alpha group"], team)
    delivery.set_schedule_views(db, views, _views())

    for s in db.execute("SELECT * FROM email_schedules WHERE enabled = 1 AND plan_id IS NULL").fetchall():
        assert delivery.send_schedule(app, db, vault, s, trigger="schedule") is None, s["id"]

    # An alert about every one of A's findings (one stays from an earlier scan, so the rest count as new).
    delivery.set_recipients(db, "alert", mine["alert_id"], [contact])
    db.execute("UPDATE email_alerts SET min_severity = 'high' WHERE id = ?", (mine["alert_id"],))
    findings = [r[0] for r in db.execute("SELECT id FROM findings WHERE tenant_id = ? ORDER BY id", (alpha,))]
    db.execute("UPDATE findings SET first_seen = '2026-01-01T00:00:00Z' WHERE id = ?", (findings[0],))
    assert delivery.after_sync(app, db, vault, tenant, findings) == 1

    # A's month, approved and sent by its report plan.
    _plan, link = month_end.plan_for(db, alpha)
    delivery.set_recipients(db, "schedule", link["schedule_id"], [contact])
    period = month_end.current_period("UTC")
    assert month_end.approve(app, db, vault, tenant, period, "admin") is None

    return [(msg["Subject"], to, _readable(msg)) for _, _, msg, to in sends()]


def test_every_email_the_worker_sends_about_a_client_keeps_to_that_client(emails_about_alpha):
    subjects = [subject for subject, _, _ in emails_about_alpha]
    # The report email, the digest, the saved views, the alert and the month-end email all went.
    assert len(emails_about_alpha) == 5, subjects
    for subject, to, body in emails_about_alpha:
        assert b"Alpha Ltd" in body, subject  # each is about A
        leaks = [word for word in B_WORDS if word in body]
        assert not leaks, (subject, to, leaks)
        assert not any("bravo" in address for address in to), (subject, to)


def test_the_same_emails_about_b_show_its_mark(app, db, vault, world, _pdf_as_page):  # noqa: F811
    """So a leak above could be seen: B's own report email carries B's mark."""
    _alpha, bravo, ids = world
    _smtp_ready(db)
    db.execute(
        "UPDATE email_schedules SET reports = ?, include_xlsx = 1 WHERE id = ?",
        (json.dumps(["review", *(f"data:{key}" for key in MARKED_REPORTS)]), ids["schedule_id"]),
    )
    delivery.set_recipients(db, "schedule", ids["schedule_id"], [ids["contact_id"]])
    s = db.execute("SELECT * FROM email_schedules WHERE id = ?", (ids["schedule_id"],)).fetchone()
    assert delivery.send_schedule(app, db, vault, s, trigger="schedule") is None
    [(_, _, msg, _)] = sends()
    assert B_MARK.encode() in _readable(msg)
