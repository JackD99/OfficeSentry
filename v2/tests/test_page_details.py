"""Small details on staff pages: back links, month names, prices as money, the activity log in words and the
apps a client deletes when they leave."""

from __future__ import annotations

import re
from pathlib import Path

from officesentry.db import audit
from tests.auditdata import addon_on
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_limited_roles import _limited
from tests.test_mailbox_page import _seed


def test_person_and_mailbox_pages_link_back_to_their_list(client, db, two_tenants):
    alpha, _ = two_tenants
    _seed(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    person = client.get(f"/tenants/{alpha}/users/u1").get_data(as_text=True)
    assert f'<div class="crumbs"><a href="/tenants/{alpha}/users">&larr; People</a></div>' in person
    # Open findings sit under the account's facts, beside Needs attention rather than below it.
    side = person[person.index('<aside aria-label="Account summary">') :]
    assert "<h2>Account</h2>" in side and "<h2>Open findings</h2>" in side[: side.index("</aside>")]
    mailbox = client.get(f"/tenants/{alpha}/mailboxes/u1").get_data(as_text=True)
    assert f'<div class="crumbs"><a href="/tenants/{alpha}/mailboxes">&larr; Mailboxes</a></div>' in mailbox


def test_email_reports_keep_primary_buttons_for_forms(client, db, two_tenants):
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/emails").get_data(as_text=True)
    assert "Set up a digest" in page and "Email a saved view" in page
    assert not re.search(r'class="btn primary[^"]*"[^>]*>(<svg[^>]*>.*?</svg>)? ?(Set up|Email a saved)', page)


def test_month_end_arrows_name_their_months(client, db, two_tenants):
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/reporting/2026-01").get_data(as_text=True)
    assert 'aria-label="Previous month, December 2025">&larr; December</a>' in page
    assert 'aria-label="Next month, February 2026">February &rarr;</a>' in page
    # Named arrows widen the buttons, so they wrap under the title rather than squeeze it onto two lines.
    assert '<div class="page-head month-head">' in page
    css = (Path(__file__).parents[1] / "officesentry/web/static/app.css").read_text(encoding="utf-8")
    assert ".page-head.month-head > :first-child:not(:only-child) { flex-basis: auto; }" in css


def test_the_activity_log_reads_in_words_and_names_only_clients_you_see(client, db, two_tenants):
    alpha, bravo = two_tenants
    audit(db, "admin", "user_access_changed", detail=f"jordan.desk: +collect@{alpha}, -export@{bravo}, -reports.email")
    audit(db, "admin", "evidence_answer_set", alpha, "patching_14_days: Yes (suggested Not known)")
    audit(db, "admin", "evidence_answer_cleared", alpha, "patching_14_days")
    audit(db, "admin", "user_access_changed", detail="sam.desk: none")
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/admin/activity").get_data(as_text=True)
    assert (
        "jordan.desk: allowed Refresh data for Alpha Ltd; not allowed Download files for Bravo Inc; "
        "not allowed Email reports"
    ) in page
    question = "Are critical and high-risk security updates installed within 14 days of release?"
    assert f"{question} Answer: Yes (suggested: Not known)" in page
    assert f"{question} Back to the suggested answer" in page
    assert "sam.desk: no exceptions to their role" in page
    # The export keeps the rows as they were written.
    assert f"jordan.desk: +collect@{alpha}" in client.get("/admin/activity.csv").get_data(as_text=True)
    # Someone who sees only Alpha never reads Bravo's name in a row about access.
    _limited(db, "rev", ["reports.security", "activity_log.read"], tenants=[alpha])
    client.post("/logout")
    login(client, "rev")
    page = client.get("/admin/activity").get_data(as_text=True)
    assert "Bravo Inc" not in page and f"Download files for Client #{bravo} (deleted or not visible)" in page


def test_offboarding_names_every_app_the_client_deletes(client, db, vault, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")

    def steps():
        page = client.get(f"/tenants/{alpha}/offboard").get_data(as_text=True)
        return page[page.index("<textarea") : page.index("</textarea>")]

    text = steps()
    assert "2. Goes to Enterprise applications" in text and "3." not in text
    assert "Office Sentry sign-in" not in text and "audit log add-on" not in text

    db.execute("UPDATE tenants SET sign_in_approved_at = '2026-01-01T00:00:00Z' WHERE id = ?", (alpha,))
    addon_on(db, vault)
    text = steps()
    assert '3. Does the same for "Office Sentry sign-in", the app your people sign in to Office Sentry with.' in text
    assert '4. Does the same for "Office Sentry audit log add-on", the audit log add-on, if it\'s there.' in text

    addon_on(db, vault, tenants=[alpha])
    assert "the audit log add-on." in steps()
