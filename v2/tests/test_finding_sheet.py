"""Acting on a finding: the sheet (reports/finding_sheet.py, web/finding.py) and what the team does from it.

Notes, hand-offs and snoozes live on the ``findings`` row (migration 0048), are staff-only, and never reach a
client. A finding can only be handed to someone who opens that client's findings.
"""

from datetime import UTC, datetime, timedelta

import pytest

from officesentry import findings, store
from officesentry.checks import Finding, RuleResult
from officesentry.reports import finding_sheet
from tests.conftest import login, make_user
from tests.seed import seed_tenant


def _finding(db, tenant_id, rule_key):
    return db.execute(
        "SELECT * FROM findings WHERE tenant_id = ? AND rule_key = ? AND resolved_at IS NULL ORDER BY id",
        (tenant_id, rule_key),
    ).fetchone()


def _audits(db, action):
    return [r["detail"] for r in db.execute("SELECT detail FROM audit_log WHERE action = ?", (action,))]


@pytest.fixture
def world(db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    seed_tenant(db, bravo, mark="Zebedee")
    make_user(db, "analyst", role="analyst", tenants=[alpha])
    make_user(db, "both", role="analyst", tenants=[alpha, bravo])
    make_user(db, "bravo-only", role="analyst", tenants=[bravo])
    make_user(db, "client", role="client", tenants=[alpha])
    make_user(db, "admin", role="admin", all_tenants=True)
    return alpha, bravo


def test_steps_are_one_sentence_each():
    text = (
        "Have every admin without MFA register a strong method (Authenticator or a FIDO2 key), or remove their "
        "role. Break-glass accounts should use FIDO2 keys kept offline."
    )
    assert finding_sheet.steps(text) == [
        "Have every admin without MFA register a strong method (Authenticator or a FIDO2 key), or remove their role.",
        "Break-glass accounts should use FIDO2 keys kept offline.",
    ]
    assert finding_sheet.steps("  ") == []
    assert finding_sheet.steps("Turn on DKIM for each domain, e.g. contoso.com. Then check it.") == [
        "Turn on DKIM for each domain, e.g. contoso.com.",
        "Then check it.",
    ]


def test_builder_gathers_evidence_history_fix_and_the_ticket(db, world):
    alpha, bravo = world
    tenant, other = store.get_tenant(db, alpha), store.get_tenant(db, bravo)
    # The admin whose account is in the Users data (the break-glass holder u4 only appears in role data).
    row = db.execute(
        "SELECT * FROM findings WHERE tenant_id = ? AND rule_key = 'admin_mfa' AND object_id = 'u3'", (alpha,)
    ).fetchone()
    sheet = finding_sheet.build(db, tenant, row, others=[other], link="https://portal.example/f/1")
    assert sheet.title == "Every admin has MFA registered" and sheet.category == "Admins"
    assert sheet.severity == row["severity"] and sheet.status == "open"
    # The finding is about an account: the person's page, and its facts as evidence.
    assert sheet.subject_kind == "Account" and sheet.subject_href.startswith(f"/tenants/{alpha}/users/")
    labels = [label for label, _ in sheet.evidence]
    assert "Account type" in labels and "Sign-in" in labels and "MFA set up" in labels
    # The admin roles part has a table, not facts: its rows become one fact naming each role.
    roles = dict(sheet.evidence)["Admin roles"]
    assert roles == "Exchange Administrator (Active, Whole directory)"
    assert len(sheet.evidence) <= finding_sheet.EVIDENCE_LIMIT
    # Where the check read its data, worked out by watching it run.
    names = [name for name, _ in sheet.sources]
    assert "Admin roles" in names and "MFA registration" in names and all(at for _, at in sheet.sources)
    assert sheet.summary and sheet.fix and len(sheet.fix) == 2
    assert sheet.history[0][0] == "First seen" and "(in the first collection)" in sheet.history[0][1]
    # The same check fails at the other client the viewer sees.
    assert sheet.others == 1 and [e.tenant["id"] for e in sheet.elsewhere] == [bravo]
    assert sheet.elsewhere[0].count >= 1 and sheet.elsewhere[0].href == f"/tenants/{bravo}/findings"
    ticket = sheet.ticket
    assert ticket.startswith(f"Alpha Ltd: Every admin has MFA registered\nAccount: {row['subject']}\n")
    assert f"Severity: {row['severity'].capitalize()} (open since" in ticket
    assert "Evidence:\n- " in ticket and "Fix:\n1. " in ticket and "2. " in ticket
    assert "Source: Office Sentry, " in ticket and "Admin roles" in ticket
    assert ticket.endswith("Link: https://portal.example/f/1")
    # Nobody else to compare with: no "Elsewhere" at all.
    alone = finding_sheet.build(db, tenant, row, others=[])
    assert alone.others == 0 and alone.elsewhere == []


def test_builder_for_something_that_is_not_an_account(db, world):
    alpha, _ = world
    tenant = store.get_tenant(db, alpha)
    row = db.execute(
        "SELECT * FROM findings WHERE tenant_id = ? AND resolved_at IS NULL AND subject = 'Tenant' LIMIT 1", (alpha,)
    ).fetchone()
    assert row is not None
    sheet = finding_sheet.build(db, tenant, row)
    assert sheet.subject_kind == "Item" and sheet.subject_href is None and sheet.evidence == []
    assert sheet.fix and "Item: Tenant" in sheet.ticket


def test_builder_shows_the_team_s_doings(db, world):
    alpha, _ = world
    tenant = store.get_tenant(db, alpha)
    row = _finding(db, alpha, "admin_mfa")
    who = db.execute("SELECT id FROM users WHERE username = 'analyst'").fetchone()["id"]
    findings.accept(db, row["id"], "admin", "Break-glass, monitored", "2020-01-01T00:00:00Z")  # lapsed
    findings.hand(db, row["id"], who, "admin")
    findings.snooze(db, row["id"], (datetime.now(UTC) + timedelta(days=3)).strftime(findings.ISO), "admin")
    db.execute(
        "INSERT INTO finding_notes (tenant_id, rule_key, subject, body, visibility, created_by, created_at)"
        " VALUES (?, 'admin_mfa', '', 'Spoke to the client', 'staff', 'admin', '2026-10-01T00:00:00Z')",
        (alpha,),
    )
    db.execute(
        "INSERT INTO report_actions (tenant_id, title, rule_key, created_at, updated_at)"
        " VALUES (?, 'Register MFA for every admin', 'admin_mfa', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')",
        (alpha,),
    )
    row = db.execute("SELECT * FROM findings WHERE id = ?", (row["id"],)).fetchone()
    sheet = finding_sheet.build(db, tenant, row, staff=True)
    assert sheet.status == "lapsed"
    history = dict(sheet.history)
    assert "now due for review" in history["Was accepted"] and "Break-glass, monitored" in history["Was accepted"]
    assert history["Handed to"].startswith("analyst by admin on ")
    assert history["Snoozed"].startswith("until ")
    assert [n["body"] for n in sheet.notes] == ["Spoke to the client"]
    assert [a["title"] for a in sheet.actions] == ["Register MFA for every admin"]
    assert sheet.project is not None and sheet.project[1].startswith(f"/tenants/{alpha}/projects#project-")
    # A client's copy leaves the staff-only note out.
    assert finding_sheet.build(db, tenant, row, staff=False).notes == []


def test_sheet_page_and_fragment(client, db, world):
    alpha, bravo = world
    login(client, "analyst")
    row = _finding(db, alpha, "admin_mfa")
    page = client.get(f"/tenants/{alpha}/findings/{row['id']}").get_data(as_text=True)
    assert 'id="finding-sheet"' in page and f'data-finding="{row["id"]}"' in page
    assert f'data-finding="{row["id"]}" aria-current="true"' in page  # the open row is marked
    assert 'data-copy="#ticket-text"' in page and 'id="ticket-text"' in page
    assert "Accept risk" in page and "Hand to" in page and f'action="/findings/{row["id"]}/note"' in page
    assert 'id="t-open-findings"' in page  # the table is still there, beside the sheet
    assert "Columns" in page and "3/5" in page  # on its compact columns, the rest a click away
    assert f'href="/tenants/{alpha}/findings#t-open-findings"' in page  # Close goes back to the table
    fragment = client.get(f"/findings/{row['id']}/sheet").get_data(as_text=True)
    assert fragment.lstrip().startswith("<aside") and "<html" not in fragment
    assert "Every admin has MFA registered" in fragment
    # The sheet's link is the one in the ticket.
    assert f"Link: http://localhost/tenants/{alpha}/findings/{row['id']}" in fragment
    # A view carried on the sheet page stays on the table's links and the Close link.
    filtered = client.get(f"/tenants/{alpha}/findings/{row['id']}?f=open-findings/flag:high").get_data(as_text=True)
    assert f'href="/tenants/{alpha}/findings?f=open-findings%2Fflag%3Ahigh#t-open-findings"' in filtered
    # Another tenant's finding, or this one under the wrong tenant, is refused.
    other = _finding(db, bravo, "admin_mfa")
    assert client.get(f"/tenants/{bravo}/findings/{other['id']}").status_code == 404
    assert client.get(f"/findings/{other['id']}/sheet").status_code == 404
    assert client.get(f"/tenants/{bravo}/findings/{row['id']}").status_code == 404
    assert client.get(f"/tenants/{alpha}/findings/{other['id']}").status_code == 404
    assert client.get(f"/tenants/{alpha}/findings/999999").status_code == 404


def test_elsewhere_only_names_clients_the_person_sees(client, db, world):
    alpha, bravo = world
    row = _finding(db, alpha, "admin_mfa")
    login(client, "analyst")  # sees alpha only
    page = client.get(f"/findings/{row['id']}/sheet").get_data(as_text=True)
    assert "Elsewhere" not in page and "Bravo" not in page and "Zebedee" not in page
    client.post("/logout")
    login(client, "both")
    page = client.get(f"/findings/{row['id']}/sheet").get_data(as_text=True)
    assert "Elsewhere" in page and f'href="/tenants/{bravo}/findings#check-admin_mfa"' in page
    assert "Zebedee" not in page  # the other client is named, never its people


def test_resolved_finding_opens_on_the_resolved_tab(client, db, world):
    alpha, _ = world
    row = _finding(db, alpha, "admin_mfa")
    db.execute("UPDATE findings SET resolved_at = '2026-10-01T00:00:00Z' WHERE id = ?", (row["id"],))
    login(client, "analyst")
    page = client.get(f"/tenants/{alpha}/findings/{row['id']}").get_data(as_text=True)
    assert 'id="t-resolved-findings"' in page and "resolved 1 Oct 2026" in page
    assert "Accept risk" not in page and "Hand to" not in page and "Resolved</dt><dd>1 Oct 2026" in page
    assert f'href="/tenants/{alpha}/findings/resolved#t-resolved-findings"' in page


def test_email_the_contact_opens_the_mail_app(client, db, world):
    alpha, _ = world
    row = _finding(db, alpha, "admin_mfa")
    login(client, "analyst")
    page = client.get(f"/findings/{row['id']}/sheet").get_data(as_text=True)
    assert "Email the contact" not in page and "Add a contact to email" in page
    from officesentry import delivery

    delivery.add_contact(db, alpha, "Pat Lee", "pat@alpha.example")
    page = client.get(f"/findings/{row['id']}/sheet").get_data(as_text=True)
    assert "Email the contact" in page and "Pat Lee" in page
    assert 'href="mailto:pat@alpha.example?subject=Alpha%20Ltd%3A%20Every%20admin%20has%20MFA%20registered' in page
    assert "body=Hello%20Pat%2C" in page and "Fix%3A" in page


def test_note_is_kept_and_never_logged(client, db, world):
    alpha, _ = world
    row = _finding(db, alpha, "admin_mfa")
    login(client, "analyst")
    resp = client.post(f"/findings/{row['id']}/note", data={"note": "  Waiting on HR to confirm the leaver  "})
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/tenants/{alpha}/findings/{row['id']}")
    saved = db.execute("SELECT * FROM findings WHERE id = ?", (row["id"],)).fetchone()
    assert saved["note"] == "Waiting on HR to confirm the leaver" and saved["note_by"] == "analyst" and saved["note_at"]
    assert _audits(db, "finding_noted") == [f"admin_mfa:{row['subject']}"]  # never the text
    page = client.get(f"/findings/{row['id']}/sheet").get_data(as_text=True)
    assert "Waiting on HR to confirm the leaver" in page and "Edit note" in page
    client.post(f"/findings/{row['id']}/note", data={"note": "whatever", "remove": "1"})
    saved = db.execute("SELECT * FROM findings WHERE id = ?", (row["id"],)).fetchone()
    assert saved["note"] is None and saved["note_by"] is None
    assert _audits(db, "finding_note_removed") == [f"admin_mfa:{row['subject']}"]
    # Too long is cut, not refused.
    client.post(f"/findings/{row['id']}/note", data={"note": "x" * 5000})
    assert len(db.execute("SELECT note FROM findings WHERE id = ?", (row["id"],)).fetchone()[0]) == findings.NOTE_LENGTH


def test_clients_see_the_evidence_and_fix_but_nothing_of_the_team(client, db, world):
    alpha, _ = world
    row = _finding(db, alpha, "admin_mfa")
    findings.note(db, row["id"], "Client is difficult about this", "analyst")
    who = db.execute("SELECT id FROM users WHERE username = 'analyst'").fetchone()["id"]
    findings.hand(db, row["id"], who, "admin")
    login(client, "client")
    page = client.get(f"/tenants/{alpha}/findings/{row['id']}").get_data(as_text=True)
    assert 'id="finding-sheet"' in page and "Copy for a ticket" in page and "Fix</h3>" in page
    for word in ("Client is difficult", "Hand to", "Handed to", "Email the contact", "Accept risk", "Note", "analyst"):
        assert word not in page, word
    assert client.post(f"/findings/{row['id']}/note", data={"note": "x"}).status_code == 403
    assert client.post(f"/findings/{row['id']}/hand", data={"to": ""}).status_code == 403
    assert (
        db.execute("SELECT note FROM findings WHERE id = ?", (row["id"],)).fetchone()[0]
        == "Client is difficult about this"
    )


def test_hand_to_only_someone_who_sees_the_client(client, db, world):
    alpha, bravo = world
    row = _finding(db, alpha, "admin_mfa")
    ids = {u["username"]: u["id"] for u in db.execute("SELECT id, username FROM users")}
    login(client, "analyst")
    page = client.get(f"/findings/{row['id']}/sheet").get_data(as_text=True)
    picker = page.split('name="to"')[1].split("</select>")[0]
    assert f'value="{ids["analyst"]}"' in picker and f'value="{ids["both"]}"' in picker
    assert f'value="{ids["admin"]}"' in picker  # sees every client
    assert f'value="{ids["bravo-only"]}"' not in picker and f'value="{ids["client"]}"' not in picker
    assert "(me)" in picker
    # Someone who can't see the client is refused, and nothing changes.
    client.post(f"/findings/{row['id']}/hand", data={"to": str(ids["bravo-only"])})
    assert db.execute("SELECT handed_to FROM findings WHERE id = ?", (row["id"],)).fetchone()[0] is None
    client.post(f"/findings/{row['id']}/hand", data={"to": str(ids["client"])})
    assert db.execute("SELECT handed_to FROM findings WHERE id = ?", (row["id"],)).fetchone()[0] is None
    assert _audits(db, "finding_handed") == []
    resp = client.post(f"/findings/{row['id']}/hand", data={"to": str(ids["both"])})
    assert resp.headers["Location"].endswith(f"/tenants/{alpha}/findings/{row['id']}")
    saved = db.execute("SELECT * FROM findings WHERE id = ?", (row["id"],)).fetchone()
    assert saved["handed_to"] == ids["both"] and saved["handed_by"] == "analyst" and saved["handed_at"]
    assert _audits(db, "finding_handed") == [f"admin_mfa:{row['subject']} to both"]
    page = client.get(f"/findings/{row['id']}/sheet").get_data(as_text=True)
    assert "Handed to</dt><dd>both by analyst on" in page
    client.post(f"/findings/{row['id']}/hand", data={"to": ""})
    saved = db.execute("SELECT * FROM findings WHERE id = ?", (row["id"],)).fetchone()
    assert saved["handed_to"] is None and saved["handed_by"] is None
    # The model itself refuses what no caller could mean.
    with pytest.raises(ValueError):
        findings.hand(db, row["id"], ids["client"], "analyst")
    with pytest.raises(ValueError):
        findings.hand(db, row["id"], 999999, "analyst")
    db.execute("UPDATE users SET disabled = 1 WHERE id = ?", (ids["both"],))
    with pytest.raises(ValueError):
        findings.hand(db, row["id"], ids["both"], "analyst")
    # Deleting the holder's account hands the finding back.
    findings.hand(db, row["id"], ids["admin"], "analyst")
    db.execute("DELETE FROM users WHERE id = ?", (ids["admin"],))
    assert db.execute("SELECT handed_to FROM findings WHERE id = ?", (row["id"],)).fetchone()[0] is None


def test_snooze(db, world):
    alpha, _ = world
    row = _finding(db, alpha, "admin_mfa")
    later = (datetime.now(UTC) + timedelta(days=2)).strftime(findings.ISO)
    findings.snooze(db, row["id"], later, "analyst")
    saved = db.execute("SELECT * FROM findings WHERE id = ?", (row["id"],)).fetchone()
    assert saved["snoozed_until"] == later and saved["snoozed_by"] == "analyst" and saved["snoozed_at"]
    assert findings.is_snoozed(saved) and not findings.is_snoozed(saved, datetime.now(UTC) + timedelta(days=3))
    findings.snooze(db, row["id"], None, "analyst")
    saved = db.execute("SELECT * FROM findings WHERE id = ?", (row["id"],)).fetchone()
    assert saved["snoozed_until"] is None and not findings.is_snoozed(saved)


def test_sync_keeps_the_holder_on_resolve_and_clears_both_on_reopen(db, world, monkeypatch):
    alpha, _ = world
    tenant = store.get_tenant(db, alpha)
    row = _finding(db, alpha, "admin_mfa")
    who = db.execute("SELECT id FROM users WHERE username = 'analyst'").fetchone()["id"]
    findings.hand(db, row["id"], who, "admin")
    findings.snooze(db, row["id"], "2099-01-01T00:00:00Z", "admin")
    findings.note(db, row["id"], "Chased twice", "admin")
    passing = RuleResult("admin_mfa", "Every admin has MFA registered", "Admins", "pass", "All admins have MFA")
    monkeypatch.setattr(findings, "evaluate", lambda snap, now: [passing])
    findings.sync(db, tenant)
    saved = db.execute("SELECT * FROM findings WHERE id = ?", (row["id"],)).fetchone()
    assert saved["resolved_at"] and saved["handed_to"] == who and saved["snoozed_until"] is None
    assert saved["note"] == "Chased twice"
    back = RuleResult(
        "admin_mfa",
        "Every admin has MFA registered",
        "Admins",
        "fail",
        "1 admin without MFA",
        findings=[Finding(row["severity"], row["subject"], row["detail"], row["object_id"])],
    )
    monkeypatch.setattr(findings, "evaluate", lambda snap, now: [back])
    findings.snooze(db, row["id"], "2099-01-01T00:00:00Z", "admin")
    findings.sync(db, tenant)
    saved = db.execute("SELECT * FROM findings WHERE id = ?", (row["id"],)).fetchone()
    assert saved["resolved_at"] is None and saved["reopened_at"]
    assert saved["handed_to"] is None and saved["handed_by"] is None and saved["snoozed_until"] is None
    assert saved["note"] == "Chased twice"


def test_accept_from_the_sheet_stays_on_the_sheet(client, db, world):
    alpha, _ = world
    row = _finding(db, alpha, "admin_mfa")
    login(client, "analyst")
    resp = client.post(f"/findings/{row['id']}/accept", data={"note": "Break-glass", "from": "sheet"})
    assert resp.headers["Location"].endswith(f"/tenants/{alpha}/findings/{row['id']}")
    page = client.get(f"/tenants/{alpha}/findings/{row['id']}").get_data(as_text=True)
    assert "accepted risk" in page and "Undo accept" in page and 'id="t-accepted-risks"' in page
    resp = client.post(f"/findings/{row['id']}/unaccept", data={"from": "sheet"})
    assert resp.headers["Location"].endswith(f"/tenants/{alpha}/findings/{row['id']}")
    # Without the flag, the forms go back to the table as before.
    resp = client.post(f"/findings/{row['id']}/accept", data={"note": "Break-glass"})
    assert resp.headers["Location"].endswith(f"/tenants/{alpha}/findings#t-accepted-risks")
