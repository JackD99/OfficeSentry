"""Report designs: the widgets, the document rules, the editor and sending a design with a plan."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from officesentry import delivery, month_end, store
from officesentry.reports import designs
from officesentry.reports.data_reports import REPORTS
from tests.conftest import login, make_user
from tests.seed import seed_tenant


def _text(resp) -> str:
    return resp.get_data(as_text=True)


def _staff(client, db, role="analyst"):
    make_user(db, role, role=role, all_tenants=True)
    login(client, role)


def _doc(*blocks: dict, **settings) -> dict:
    return {"settings": settings, "blocks": [dict(b) for b in blocks]}


def _block(kind: str, width: str = "full", **settings) -> dict:
    return {"kind": kind, "width": width, "settings": settings}


# -- The document ---------------------------------------------------------------------------------------


def test_clean_keeps_known_blocks_and_coerces_every_setting():
    doc = designs.clean(
        {
            "settings": {"title": " Quarterly review ", "cover_verdict": 0, "cover": "sideways", "notes": 1, "junk": 1},
            "blocks": [
                {"kind": "kpis", "width": "half", "settings": {"keys": ["waste", "nope"], "columns": 99, "heading": 7}},
                {
                    "kind": "findings",
                    "width": "sideways",
                    "settings": {"severities": "critical", "max_rows": "x", "style": "weird"},
                },
                {"kind": "not_a_widget", "settings": {}},
                "garbage",
                {"kind": "text", "width": "half", "settings": {"text": "a" * 5000}, "id": "Same ID!"},
                {"kind": "text", "width": "half", "id": "sameid"},
                {"kind": "page_break"},
            ],
        }
    )
    assert doc["settings"] == {
        "title": "Quarterly review",
        "subtitle": "",
        "cover": "full",
        "cover_verdict": False,
        "contents": True,
        "notes": True,
    }
    kinds = [b["kind"] for b in doc["blocks"]]
    assert kinds == ["kpis", "findings", "text", "text", "page_break"]
    kpis, findings, text, text2, brk = doc["blocks"]
    assert kpis["settings"] == {"heading": "7", "keys": ["waste"], "columns": 4} and kpis["width"] == "full"
    assert findings["settings"]["severities"] == [] and findings["settings"]["max_rows"] == 5
    assert findings["settings"]["style"] == "detailed" and findings["width"] == "full"
    assert len(text["settings"]["text"]) == designs.LONG_TEXT_MAX and text["width"] == "half"
    assert text["id"] == "sameid" and text2["id"] == "sameidx"  # ids are unique, whatever the editor sent
    assert designs.clean("nonsense") == {
        "settings": {
            "title": "",
            "subtitle": "",
            "cover": "full",
            "cover_verdict": True,
            "contents": True,
            "notes": False,
        },
        "blocks": [],
    }
    assert len(designs.clean({"blocks": [{"kind": "text"}] * 200})["blocks"]) == designs.MAX_BLOCKS


def test_starters_are_valid_documents():
    for key, (label, description, doc) in designs.STARTERS.items():
        cleaned = designs.clean(doc)
        assert len(cleaned["blocks"]) == len(doc["blocks"]), key
        for raw, kept in zip(doc["blocks"], cleaned["blocks"], strict=True):
            assert raw["settings"].items() <= kept["settings"].items(), (key, raw["kind"])  # nothing coerced away
        assert label and description
    assert set(designs.STARTERS["full"][2]["blocks"][0].keys()) == {"kind", "width", "settings"}


def test_palette_lists_every_widget_with_its_fields():
    palette = designs.palette()
    keys = [w["key"] for group in palette for w in group["widgets"]]
    assert sorted(keys) == sorted(designs.WIDGETS)
    table = next(w for g in palette for w in g["widgets"] if w["key"] == "data_table")
    report = next(f for f in table["fields"] if f["key"] == "report")
    assert [o[0] for o in report["options"]] == list(REPORTS)
    assert all(w["halfable"] is False for g in palette for w in g["widgets"] if w["key"] in ("findings", "licences"))


# -- Rendering ----------------------------------------------------------------------------------------------


@pytest.fixture
def design(db):
    blocks = [_block(kind) for kind in designs.WIDGETS]
    for b in blocks:
        if b["kind"] == "data_table":
            b["settings"] = {"report": "licences", "max_rows": 5}
        if b["kind"] == "text":
            b["settings"] = {"text": "Dear {client},\n\nthis is {month} from {msp} on {date}."}
    return designs.create(db, "Everything", "test", _doc(*blocks, title="All blocks for {client}"))


def test_every_widget_renders_on_a_seeded_and_an_empty_tenant(client, db, two_tenants, design):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    _staff(client, db)
    for tenant in (alpha, bravo):
        resp = client.get(f"/tenants/{tenant}/designs/{design}")
        assert resp.status_code == 200, tenant
    page = _text(client.get(f"/tenants/{alpha}/designs/{design}"))
    assert "All blocks for Alpha Ltd" in page and "Dear Alpha Ltd," in page
    assert "Key figures" in page and "Licences" in page and "In good shape" in page
    assert "has nothing to show" not in page  # only the preview names empty blocks
    # The empty tenant: the data-driven blocks fall away, the words stay, the page still opens.
    page = _text(client.get(f"/tenants/{bravo}/designs/{design}"))
    assert "Dear Bravo Inc," in page
    assert '<table class="licences">' not in page and "In good shape" not in page
    preview = _text(client.get(f"/reporting/designs/{design}/preview?tenant={bravo}"))
    assert preview.count("has nothing to show") >= 5 and "Page break" in preview


def test_half_width_blocks_share_a_row(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    did = designs.create(
        db,
        "Rows",
        "test",
        _doc(_block("breakdown", "half"), _block("contact", "half"), _block("verdict", "half"), _block("summary_text")),
    )
    _staff(client, db)
    page = _text(client.get(f"/tenants/{alpha}/designs/{did}"))
    assert page.count('class="block-row"') == 2  # breakdown + contact, then the lone verdict
    assert 'class="block block-summary_text' in page


def test_projects_block_words_quick_fixes_as_the_projects_pages_do(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    did = designs.create(db, "Projects", "test", _doc(_block("projects", quick=25)))
    _staff(client, db)
    page = _text(client.get(f"/tenants/{alpha}/designs/{did}"))
    block = page[page.index('class="block block-projects') :]
    # What was found leads, the check's goal under it; a signal leads with its name. No green tick: these are open.
    assert "Goal: Every admin has MFA" in block and "<li>No Defender for Office 365<span" in block
    assert '<ul class="passing">' not in block


def test_a_broken_widget_never_takes_the_report_down(client, db, two_tenants, monkeypatch, caplog):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    did = designs.create(db, "Broken", "test", _doc(_block("kpis"), _block("text", text="Still here")))

    def boom(ctx, settings):
        raise RuntimeError("no")

    monkeypatch.setitem(designs.WIDGETS, "kpis", designs.Widget("kpis", "Key figures", "", "Summary", (), boom))
    _staff(client, db)
    page = _text(client.get(f"/tenants/{alpha}/designs/{did}"))
    assert "Still here" in page and "Key figures" not in page
    assert "Report design block kpis failed" in caplog.text
    assert "couldn't be built" in _text(client.get(f"/reporting/designs/{did}/preview?tenant={alpha}"))


def test_pdf_and_print_fallback(client, db, two_tenants, design, monkeypatch):
    from officesentry.reports import export

    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    _staff(client, db)
    resp = client.get(f"/tenants/{alpha}/designs/{design}.pdf")
    assert resp.status_code == 200 and resp.data[:4] == b"%PDF"
    assert (
        db.execute("SELECT detail FROM audit_log WHERE action = 'report_downloaded'").fetchone()[0]
        == f"design:{design} pdf"
    )
    monkeypatch.setattr(export, "pdf_available", lambda: False)
    resp = client.get(f"/tenants/{alpha}/designs/{design}.pdf")
    assert resp.status_code == 200 and b"print-view" in resp.data


def test_commentary_and_curated_actions_come_from_month_end(client, db, two_tenants, app):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    period = month_end.current_period("UTC")
    month_end.update_review(db, alpha, period, "lead", commentary="A good month.\n\nKeep going.")
    db.execute(
        "INSERT INTO report_actions (tenant_id, title, detail, status, created_at, updated_at)"
        " VALUES (?, 'Roll out passkeys', 'For the finance team first', 'planned', ?, ?)",
        (alpha, "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z"),
    )
    did = designs.create(db, "Month", "test", _doc(_block("commentary"), _block("actions")))
    _staff(client, db)
    page = _text(client.get(f"/tenants/{alpha}/designs/{did}"))
    assert "A good month." in page and "Keep going." in page and "Roll out passkeys" in page


# -- The editor -------------------------------------------------------------------------------------------


def test_designs_are_made_edited_duplicated_and_removed(client, db, two_tenants):
    alpha, _ = two_tenants
    _staff(client, db, "admin")
    assert "No designs yet" in _text(client.get("/reporting/designs"))
    resp = client.post("/reporting/designs", data={"name": "QBR pack", "starter": "executive"})
    did = int(resp.headers["Location"].rsplit("/", 1)[1])
    row = designs.get(db, did)
    assert row["name"] == "QBR pack" and len(designs.document(row)["blocks"]) == 6
    assert client.post("/reporting/designs", data={"starter": "nope"}).status_code == 400

    page = _text(client.get(f"/reporting/designs/{did}?tenant={alpha}"))
    assert 'id="designer-data"' in page and "Status verdict" in page and "Alpha Ltd" in page
    assert f"/reporting/designs/{did}/preview" in page

    body = {
        "name": "QBR pack v2",
        "description": "For quarterly reviews",
        "settings": {"title": "QBR {month}"},
        "blocks": [_block("verdict"), _block("bogus"), _block("text", "half", text="Hello {client}")],
    }
    resp = client.post(f"/reporting/designs/{did}", data=json.dumps(body), content_type="application/json")
    assert resp.status_code == 200
    stored = resp.get_json()
    assert stored["name"] == "QBR pack v2" and [b["kind"] for b in stored["document"]["blocks"]] == ["verdict", "text"]
    row = designs.get(db, did)
    assert row["description"] == "For quarterly reviews" and json.loads(row["settings"])["title"] == "QBR {month}"
    assert client.post(f"/reporting/designs/{did}", data="[]", content_type="application/json").status_code == 400
    assert (
        client.post(
            f"/reporting/designs/{did}", data="x" * (designs.TEXT_MAX * 2000), content_type="application/json"
        ).status_code
        == 413
    )

    resp = client.post(f"/reporting/designs/{did}/duplicate")
    copy = int(resp.headers["Location"].rsplit("/", 1)[1])
    assert designs.get(db, copy)["name"] == "QBR pack v2 (copy)" and designs.document(
        designs.get(db, copy)
    ) == designs.document(designs.get(db, did))
    page = _text(client.get("/reporting/designs"))
    assert "QBR pack v2" in page and "(copy)" in page

    client.post(f"/reporting/designs/{did}/rename", data={"name": "", "description": ""})
    assert designs.get(db, did)["name"] == "QBR pack v2"
    client.post(f"/reporting/designs/{did}/rename", data={"name": "Final", "description": "d"})
    assert designs.get(db, did)["name"] == "Final"

    assert client.post(f"/reporting/designs/{copy}/delete").status_code == 302
    assert designs.get(db, copy) is None
    actions = [r[0] for r in db.execute("SELECT action FROM audit_log WHERE action LIKE 'report_design_%'")]
    assert actions.count("report_design_created") == 2 and "report_design_deleted" in actions


def test_who_may_do_what(client, db, two_tenants):
    alpha, _ = two_tenants
    did = designs.create(db, "D", "test", designs.STARTERS["executive"][2])
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    for url in (
        "/reporting/designs",
        f"/reporting/designs/{did}",
        f"/reporting/designs/{did}/preview",
        f"/tenants/{alpha}/designs",
        f"/tenants/{alpha}/designs/{did}",
        f"/tenants/{alpha}/designs/{did}.pdf",
    ):
        assert client.get(url).status_code == 403, url
    assert client.post(f"/reporting/designs/{did}", data="{}", content_type="application/json").status_code == 403
    client.post("/logout")
    _staff(client, db, "analyst")
    assert client.get(f"/reporting/designs/{did}").status_code == 200
    assert client.post(f"/reporting/designs/{did}/delete").status_code == 403  # removing one is an admin's call
    assert client.get("/reporting/designs/424242").status_code == 404


def test_preview_only_shows_tenants_the_person_sees(client, db, two_tenants):
    alpha, bravo = two_tenants
    did = designs.create(db, "D", "test", _doc(_block("text", text="For {client}")))
    make_user(db, "analyst", role="analyst", tenants=[alpha])
    login(client, "analyst")
    page = _text(client.get(f"/reporting/designs/{did}/preview?tenant={bravo}"))
    assert "For Alpha Ltd" in page and "Bravo" not in page  # falls back to a tenant they do see
    page = _text(client.get(f"/reporting/designs/{did}?tenant={bravo}"))
    assert "Bravo Inc" not in page


def test_tenant_reports_tab_lists_designs(client, db, two_tenants):
    alpha, _ = two_tenants
    _staff(client, db)
    page = _text(client.get(f"/tenants/{alpha}/designs"))
    assert "No report designs yet" in page
    did = designs.create(db, "Board pack", "test", designs.STARTERS["executive"][2])
    page = _text(client.get(f"/tenants/{alpha}/designs"))
    assert "Board pack" in page and f"/tenants/{alpha}/designs/{did}.pdf" in page
    assert "Custom reports" in _text(client.get(f"/tenants/{alpha}/data/{next(iter(REPORTS))}"))


# -- Sending ------------------------------------------------------------------------------------------------


def test_a_plan_sends_a_design_as_a_pdf(client, db, two_tenants, app):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    did = designs.create(db, "Board pack", "test", designs.STARTERS["executive"][2], "For the board")
    key = designs.key_of(did)
    assert key in {o.key for o in delivery.catalog(db)} and key not in {o.key for o in delivery.catalog()}
    tenant = store.get_tenant(db, alpha)
    assert delivery.attachment_plan(tenant, ["review", key], False, db) == [
        ("review", "security-review-alpha-ltd.pdf"),
        (key, "board-pack-alpha-ltd.pdf"),
    ]
    assert delivery.report_titles(json.dumps([key, "review"]), db)[0] == "Board pack"
    files = delivery._attachments(app, db, tenant, [key], False, datetime.now(UTC))
    assert [(f[0], f[1], f[2][:4]) for f in files] == [("board-pack-alpha-ltd.pdf", "application/pdf", b"%PDF")]

    _staff(client, db, "admin")
    page = _text(client.get("/reporting/plans/new"))
    assert "Board pack" in page and "For the board" in page
    client.post(
        "/reporting/plans/new",
        data={
            "name": "Gold",
            "reports": ["review", key],
            "day_of_month": 1,
            "hour": 8,
            "mode": "auto",
            "delivery": "email",
            "tenants": [alpha],
        },
    )
    plan = db.execute("SELECT * FROM report_plans").fetchone()
    assert json.loads(plan["reports"]) == ["review", key]
    assert "Board pack" in _text(client.get("/reporting/plans"))
    assert designs.usage(db) == {did: 2}  # the plan and the schedule it made for Alpha

    # Removing the design takes it out of the plan and the schedule rather than leaving a dead key.
    client.post(f"/reporting/designs/{did}/delete")
    assert json.loads(db.execute("SELECT reports FROM report_plans").fetchone()[0]) == ["review"]
    assert json.loads(db.execute("SELECT reports FROM email_schedules").fetchone()[0]) == ["review"]
