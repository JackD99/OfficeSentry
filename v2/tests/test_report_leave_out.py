"""Leaving things out of one client's month: the review screen's Leave out and Put back, its preview of the
client's copy, and every copy the client gets (their page, the PDFs, the spreadsheet and the email) without
them. Setup notes written for the MSP stay out of the client's copy unless the design includes them."""

from __future__ import annotations

import json

import pytest

from officesentry import delivery, lifecycle, month_end, store
from officesentry.reports import designs, export, monthly_review
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_email import _smtp_ready, fake_smtp, sends  # noqa: F401 (autouse fixture)
from tests.test_month_end import PERIOD, _plan, this_month, work

JSON = {"Accept": "application/json"}


@pytest.fixture(autouse=True)
def _this_month():
    """This month as the test starts, not as the module loaded: a run may pass midnight at a month's end."""
    global PERIOD
    PERIOD = this_month()


NOTICE = "Microsoft is hiding people's names. Sign in to admin.microsoft.com as a Global Admin to show them."


def _text(resp) -> str:
    return resp.get_data(as_text=True)


def _out(client, tenant, *items, op="out"):
    return client.post(f"/reporting/{PERIOD}/{tenant}/leave-out", data={"op": op, "item": list(items)}, headers=JSON)


@pytest.fixture
def board(db):
    """The Executive summary starter, with a table that carries a setup note."""
    doc = json.loads(json.dumps(designs.STARTERS["executive"][2]))
    doc["blocks"].append({"kind": "data_table", "width": "full", "settings": {"report": "licences", "max_rows": 5}})
    return designs.create(db, "Board pack", "test", doc)


@pytest.fixture
def noticed(monkeypatch):
    """Every detail report says what a usage report says when Microsoft conceals names."""
    built = designs.Context.report

    def with_notice(self, key):
        report = built(self, key)
        report.notice = NOTICE
        return report

    monkeypatch.setattr(designs.Context, "report", with_notice)


def test_leave_out_drops_checks_and_figures_and_counts_again(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = monthly_review.build(db, store.get_tenant(db, alpha))
    rule = report.failing[0]
    kept = monthly_review.leave_out(report, {f"check:{rule.key}", "kpi:waste", "part:licences"})
    assert rule.key not in {r.key for r in kept.results} and rule.key in {r.key for r in report.results}
    assert [k.key for k in kept.kpis] == [k.key for k in report.kpis if k.key != "waste"]
    assert sum(kept.counts.values()) == sum(report.counts.values()) - len(rule.findings)
    assert monthly_review.leave_out(report, {"part:licences"}) is report  # parts are the templates' to drop


def test_only_real_items_of_this_client_can_be_left_out(db, two_tenants, board):
    alpha, bravo = two_tenants
    theirs = month_end.add_action(db, bravo, "Bravo's own", "", "admin")
    block = designs.document(designs.get(db, board))["blocks"][0]["id"]
    for item in ("part:licences", "kpi:waste", "check:admin_mfa", f"block:{board}:{block}", f"notes:{board}"):
        assert month_end.valid_item(db, alpha, item), item
    for item in (
        "part:nope",
        "kpi:nope",
        "check:nope",
        f"block:{board}:nope",
        "block:999:x",
        "notes:999",
        f"action:{theirs}",
        "check:admin_mfa; DROP",
        "",
    ):
        assert not month_end.valid_item(db, alpha, item), item
    assert month_end.valid_item(db, bravo, f"action:{theirs}")


def test_left_out_items_stay_out_of_everything_the_client_gets(client, db, two_tenants, board, monkeypatch):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    pdfs = []
    monkeypatch.setattr(delivery.export, "to_pdf", lambda html, base: pdfs.append(html) or b"%PDF-1.7 frozen")
    sheets = []
    to_xlsx = export.to_xlsx
    monkeypatch.setattr(delivery.export, "to_xlsx", lambda report, **kw: sheets.append(report) or to_xlsx(report, **kw))
    make_user(db, "admin")
    login(client, "admin")
    client.post(
        "/admin/email",
        data={
            "mail_transport": "smtp",
            "smtp_host": "smtp.example.com",
            "smtp_port": "587",
            "smtp_security": "starttls",
            "smtp_user": "r@msp.com",
            "smtp_password": "x",
            "smtp_from": "r@msp.com",
        },
    )
    delivery.add_contact(db, alpha, "IT", "it@alpha.com")
    _plan(client, [alpha], reports=["review", designs.key_of(board)])
    rule = monthly_review.build(db, store.get_tenant(db, alpha)).failing[0]
    contact = next(b["id"] for b in designs.document(designs.get(db, board))["blocks"] if b["kind"] == "contact")

    # The review screen lists everything, ticked, and the preview shows the client's copy.
    page = _text(client.get(f"/reporting/{PERIOD}/{alpha}"))
    assert f'value="check:{rule.key}"' in page and 'value="part:licences"' in page and "Board pack" in page
    assert f'value="block:{board}:{contact}"' in page and 'id="left-out" hidden' in page
    preview = client.get(f"/reporting/{PERIOD}/{alpha}/preview?report=review")
    assert preview.status_code == 200 and "script-src 'none'" in preview.headers["Content-Security-Policy"]
    assert f'id="check-{rule.key}"' in _text(preview) and 'id="licences"' in _text(preview)

    resp = _out(client, alpha, f"check:{rule.key}", "kpi:waste", f"block:{board}:{contact}")
    assert resp.get_json() == {"left_out": sorted([f"check:{rule.key}", "kpi:waste", f"block:{board}:{contact}"])}
    # The ticks: every part of the monthly review in scope, Licences no longer shown.
    scope = [f"part:{k}" for k in month_end.PARTS]
    resp = client.post(
        f"/reporting/{PERIOD}/{alpha}/leave-out",
        data={"op": "set", "scope": scope, "shown": [s for s in scope if s != "part:licences"], "anchor": "contents"},
    )
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/reporting/{PERIOD}/{alpha}#contents")
    assert month_end.hidden(db, alpha, PERIOD) == {
        f"check:{rule.key}",
        "kpi:waste",
        f"block:{board}:{contact}",
        "part:licences",
    }
    assert month_end.state_of(db, alpha, PERIOD) == "in_review"
    assert _out(client, alpha, "part:nope").status_code == 400
    assert _out(client, alpha, op="out").status_code == 400

    # Staff still see each item on the review screen, marked, with Put back.
    page = _text(client.get(f"/reporting/{PERIOD}/{alpha}"))
    assert f'class="review-check left-out" id="check-{rule.key}"' in page and "4 things" in page
    preview = _text(client.get(f"/reporting/{PERIOD}/{alpha}/preview?report=review"))
    assert f'id="check-{rule.key}"' not in preview and 'id="licences"' not in preview
    assert "Licence waste / month" not in preview
    preview = _text(client.get(f"/reporting/{PERIOD}/{alpha}/preview?report={designs.key_of(board)}"))
    assert f'id="b{contact}"' not in preview and "left out of" not in preview  # the client's copy, not the editor's
    page = _text(client.get(f"/tenants/{alpha}/report"))
    assert "4 things are left out of Alpha Ltd" in page and f'id="check-{rule.key}"' not in page
    assert 'id="licences"' not in page
    editor = _text(client.get(f"/reporting/designs/{board}/preview?tenant={alpha}"))
    assert "is left out of Alpha Ltd's copy this month" in editor

    # Approving freezes and sends files and an email without them.
    client.post(f"/reporting/{PERIOD}/{alpha}/approve", data={})
    assert work(client) == 1
    review_html, design_html = pdfs
    assert f'id="check-{rule.key}"' not in review_html and 'id="licences"' not in review_html
    assert "Licence waste / month" not in review_html and rule.summary.rstrip(".") not in review_html
    assert f'id="b{contact}"' not in design_html and "Licence waste / month" not in design_html
    (sheet,) = sheets
    assert rule.key not in {r.key for r in sheet.results} and "waste" not in {k.key for k in sheet.kpis}
    ((_, _, msg, _),) = sends()
    body = msg.get_body(("plain",)).get_content()
    assert rule.summary.rstrip(".") not in body and "Licence waste" not in body

    # Putting everything back is one button, and each change is audited by its item, never its words.
    assert _out(client, alpha, op="all").get_json() == {"left_out": []}
    audit = [
        (r["action"], r["detail"])
        for r in db.execute("SELECT * FROM audit_log WHERE action LIKE 'report_item_%' ORDER BY id")
    ]
    assert (("report_item_left_out", f"{PERIOD}: part:licences")) in audit
    assert sum(a == "report_item_put_back" for a, _ in audit) == 4


def test_a_left_out_recommendation_waits_for_next_month(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "analyst", role="analyst", all_tenants=True)
    login(client, "analyst")
    action = month_end.add_action(db, alpha, "Book a licence review", "", "analyst")
    _out(client, alpha, f"action:{action}")
    assert month_end.report_context(db, alpha, PERIOD, staff=False)["month_end"]["actions"] == []
    nxt = month_end.shift_period(PERIOD, 1)
    assert [a["title"] for a in month_end.report_context(db, alpha, nxt, staff=False)["month_end"]["actions"]] == [
        "Book a licence review"
    ]


def test_setup_notes_stay_out_of_client_copies_unless_the_design_includes_them(
    client, db, two_tenants, board, noticed, app, monkeypatch
):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "analyst", role="analyst", all_tenants=True)
    login(client, "analyst")
    tenant, key = store.get_tenant(db, alpha), designs.key_of(board)
    escaped = NOTICE.replace("'", "&#39;")

    def sent() -> str:
        """The design's PDF as a plan sends it, as HTML."""
        pdfs = []
        monkeypatch.setattr(delivery.export, "to_pdf", lambda html, base: pdfs.append(html) or b"%PDF-1.7")
        context = month_end.report_context(db, alpha, PERIOD, staff=False)
        delivery._attachments(app, db, tenant, [key], False, None, review_context=context)
        return pdfs[0]

    def preview() -> str:
        return _text(client.get(f"/reporting/{PERIOD}/{alpha}/preview?report={key}"))

    # The portal page and the editor show it to the team, saying the client won't get it.
    page = _text(client.get(f"/tenants/{alpha}/designs/{board}"))
    assert escaped in page and "Only your team sees this" in page
    assert "Only your team sees this" in _text(client.get(f"/reporting/designs/{board}/preview?tenant={alpha}"))
    assert escaped not in preview() and escaped not in sent()

    # Included by the design: the client's copy has it, plain, unless the review leaves it out this month.
    doc = designs.document(designs.get(db, board))
    doc["settings"]["notes"] = True
    designs.save(db, board, doc)
    assert escaped in preview() and escaped in sent() and "Only your team sees this" not in preview()
    _out(client, alpha, f"notes:{board}")
    assert escaped not in preview() and escaped not in sent()


def test_deleting_a_tenant_deletes_what_was_left_out(db, two_tenants):
    alpha, bravo = two_tenants
    for t in (alpha, bravo):
        month_end.leave_out(db, t, PERIOD, "part:licences", "admin")
    lifecycle.delete_tenant(db, store.get_tenant(db, alpha), "admin")
    assert [r[0] for r in db.execute("SELECT tenant_id FROM report_hidden")] == [bravo]
