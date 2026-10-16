"""What a client reads, as their managing director would: the home page, the report email, the verdict, the
two-page summary and the sign-in page. Each says the same as the client's copy of the month, counts checks as
the report does, and leads with the answer and the MSP's own words."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from markupsafe import escape

from officesentry import delivery, month_end, store
from officesentry.reports import monthly_review, summary
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_email import _row, _schedule
from tests.test_month_end import this_month
from tests.test_portal_branding import _brand


def _text(resp) -> str:
    return resp.get_data(as_text=True)


def _urgent(report) -> int:
    return sum(1 for r in report.failing if r.severity in ("critical", "high"))


def test_the_client_home_is_the_clients_copy_of_the_month(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    period = this_month()
    report = monthly_review.build(db, store.get_tenant(db, alpha))
    left, kept = report.failing[0], report.failing[1]
    month_end.leave_out(db, alpha, period, f"check:{left.key}", "admin")
    month_end.add_action(db, alpha, "Switch on disk encryption", "We can do it remotely, out of hours.", "admin")
    month_end.update_review(db, alpha, period, "admin", commentary="A good month.\n\nOne decision for you.")
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")

    # In review: what was left out is already gone, and nothing the reviewer wrote shows yet.
    page = _text(client.get("/"))
    assert str(escape(left.summary.rstrip("."))) not in page and str(escape(kept.summary.rstrip("."))) in page
    assert "A good month." not in page and "Switch on disk encryption" not in page
    assert "What needs attention" in page
    # Counted by check, as the report counts them, never by the accounts they affect.
    copy = monthly_review.leave_out(report, {f"check:{left.key}"})
    assert f"{_urgent(copy)} critical or high" in page and _urgent(copy) != sum(
        len(r.findings) for r in copy.failing if r.severity in ("critical", "high")
    )

    # Approved: the account manager's note and recommendations lead.
    month_end.update_review(db, alpha, period, "admin", state="approved")
    page = _text(client.get("/"))
    assert page.index("A good month.") < page.index("One decision for you.") < page.index("What we recommend next")
    assert "Switch on disk encryption" in page and "We can do it remotely, out of hours." in page
    assert "Planned" in page and str(escape(left.summary.rstrip("."))) not in page


def test_the_verdict_names_the_most_urgent_item(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = monthly_review.build(db, store.get_tenant(db, alpha))
    es = summary.monthly(report)
    top = report.failing[0]
    assert top.severity in ("critical", "high")
    assert es.verdict.detail.startswith("Most urgent: ") and top.summary.rstrip(".") in es.verdict.detail
    assert top.summary.rstrip(".") not in es.paragraphs[0]  # said once, under the verdict
    # A month with nothing critical or high has no "most urgent".
    calm = monthly_review.leave_out(
        report, {f"check:{r.key}" for r in report.failing if r.severity in ("critical", "high")}
    )
    assert summary.monthly(calm).verdict.detail == ""


def test_the_report_email_carries_the_month_as_the_pdf_does(app, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    period = this_month()
    t = store.get_tenant(db, alpha)
    s = _row(db, "email_schedules", _schedule(db, alpha, [], reports=()))
    review = monthly_review.build(db, t)

    built = delivery.build_report_email(app, db, s, t, [], attach=False, period=period).email
    counts = {sev: sum(1 for r in review.failing if r.severity == sev) for sev in summary.SEVERITIES}
    breakdown = ", ".join(f"{n} {sev}" for sev, n in counts.items() if n)
    assert f"Needs attention: {len(review.failing)} checks ({breakdown})" in built.text
    assert f"{counts['high']} high" in built.html and "findings)" not in built.html
    verdict = summary.monthly(review).verdict
    assert verdict.detail and verdict.detail in built.text and str(escape(verdict.detail)) in built.html
    assert "What we recommend next" not in built.text  # no recommendations of the MSP's own: the checks

    month_end.update_review(db, alpha, period, "admin", commentary="A good month for sign-in security.")
    month_end.add_action(db, alpha, "Give the break-glass account a key", "No second sign-in step yet.", "admin")
    built = delivery.build_report_email(app, db, s, t, [], attach=False, period=period).email
    for part, headline in ((built.text, verdict.headline), (built.html, str(escape(verdict.headline)))):
        assert part.index(headline) < part.index("A good month for sign-in security.")
        assert part.index("A good month for sign-in security.") < part.index("What we recommend next")
        assert "Give the break-glass account a key" in part and "No second sign-in step yet." in part
    assert "From Office Sentry:" in built.text  # the MSP's name, Office Sentry until branding is set up


def test_the_summary_pdf_is_two_pages_with_a_band_for_a_cover(client, db, two_tenants, monkeypatch):
    from officesentry.reports import export

    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    month_end.update_review(db, alpha, this_month(), "admin", commentary="Our note for the month.", state="approved")
    make_user(db, "admin")
    login(client, "admin")
    html = []
    monkeypatch.setattr(export, "to_pdf", lambda page, base: html.append(page) or b"%PDF-1.7")
    client.get(f"/tenants/{alpha}/report.pdf?appendix=0")
    client.get(f"/tenants/{alpha}/report.pdf")
    summary_only, full = html
    assert 'class="cover compact"' in summary_only and 'class="toc"' not in summary_only
    assert "Our note for the month." in summary_only.split('class="cover-status"')[1].split("</header>")[0]
    # The full report's cover is a page of a fixed height with its contents list: the note opens the next page.
    assert 'class="cover compact"' not in full and 'class="toc"' in full
    cover = full.split("</header>")[0]
    assert "Our note for the month." not in cover and "Our note for the month." in full
    assert full.index("Our note for the month.") < full.index('id="summary"')


def test_the_sign_in_page_says_who_to_ask(client, db):
    make_user(db, "admin")
    assert "Trouble signing in?" not in _text(client.get("/login"))
    _brand(db, name="Harbour IT", support_email="help@harbour.it", support_phone="01234 567890")
    page = _text(client.get("/login"))
    assert "Trouble signing in? Contact Harbour IT at" in page
    assert 'href="mailto:help@harbour.it"' in page and 'href="tel:01234567890"' in page
    # Without support details, the contact line the reports carry.
    db.execute("DELETE FROM settings WHERE key IN ('brand_support_email', 'brand_support_phone')")
    _brand(db, contact="0161 496 0000")
    assert "Trouble signing in? Contact Harbour IT (0161 496 0000)." in _text(client.get("/login"))


def test_changes_in_a_percentage_are_in_points_wherever_a_client_reads_them(app, client, db, two_tenants, monkeypatch):
    """Secure Score from 35% to 42% is 7 points, as on the Overview: in What changed and its PDF, and in the digest."""
    from officesentry.reports import export

    alpha, _ = two_tenants
    old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute(
        "INSERT INTO metrics (run_id, tenant_id, key, value, captured_at) VALUES (NULL, ?, ?, ?, ?)",
        (alpha, "secure_score.pct", 35.0, old),
    )
    seed_tenant(db, alpha)  # now 42%
    make_user(db, "admin")
    login(client, "admin")
    page = _text(client.get(f"/tenants/{alpha}/changes"))
    assert "42%" in page and "+7 pts</span>" in page and "+7</span>" not in page
    html = []
    monkeypatch.setattr(export, "to_pdf", lambda page, base: html.append(page) or b"%PDF-1.7")
    client.get(f"/tenants/{alpha}/changes.pdf")
    assert "+7 pts</span>" in html[0] and "+7</span>" not in html[0]

    digest = delivery.build_digest_email(
        app, db, _row(db, "email_schedules", _schedule(db, None, [], kind="digest")), []
    )
    assert "+7 pts</span>" in digest.email.html and "+7</span>" not in digest.email.html
