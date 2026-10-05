"""Per-mailbox checks on big tenants rotate through every mailbox and keep earlier results."""

import json
import re

import pytest
import responses

from officesentry import store
from officesentry.collectors import mailbox_window
from officesentry.exchange import EXCHANGE_ROOT
from officesentry.graph import GraphClient
from officesentry.worker import execute_run
from tests.seed import V1, exchange_callback, exchange_factory

IDS = ["u3", "u0", "u4", "u1", "u2"]  # Exchange's order isn't the ID order
EXCHANGE = {
    "Get-Mailbox": [{"ExternalDirectoryObjectId": i, "Guid": f"g-{i}", "PrimarySmtpAddress": f"{i}@x.com",
                     "UserPrincipalName": f"{i}@x.com", "RecipientTypeDetails": "UserMailbox",
                     "GrantSendOnBehalfTo": []} for i in IDS],
    "Get-MailboxPermission": {f"g-{i}": [{"User": f"delegate-{i}@x.com", "AccessRights": ["FullAccess"],
                                          "IsInherited": False}] for i in IDS},
    "Get-MailboxStatistics": {f"g-{i}": [{"TotalItemSize": "1 KB (1,024 bytes)", "ItemCount": 1}] for i in IDS},
}
COLLECTORS = ("mailboxes", "inbox_rules", "mailbox_usage")
KINDS = {"mailboxes": "mailbox_permission", "inbox_rules": "inbox_rule", "mailbox_usage": "mailbox_size"}


def _factory(tenant, connection):
    return GraphClient(lambda: "t", sleep=lambda s: None)


def _round(db, tenant_id) -> tuple[dict, dict]:
    """Run the three per-mailbox collectors once. Returns each one's run and the mailboxes it asked about."""
    asked = {c: [] for c in COLLECTORS}
    answer = exchange_callback(EXCHANGE)

    def exchange(request):
        body = json.loads(request.body)["CmdletInput"]
        who = {"Get-MailboxPermission": "mailboxes", "Get-MailboxStatistics": "mailbox_usage"}.get(body["CmdletName"])
        if who:
            asked[who].append(body["Parameters"]["Identity"].removeprefix("g-"))
        return answer(request)

    def rules(request):
        uid = request.path_url.split("/")[3]
        asked["inbox_rules"].append(uid)
        return 200, {}, json.dumps({"value": [{"id": "r1", "displayName": f"rule of {uid}"}]})

    runs = {}
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add_callback(responses.POST, re.compile(re.escape(EXCHANGE_ROOT) + r"/adminapi/beta/[^/]+/InvokeCommand"),
                          callback=exchange, content_type="application/json")
        rsps.add_callback(responses.GET, re.compile(re.escape(V1) + r"/users/[^/]+/mailFolders/inbox/messageRules"),
                          callback=rules, content_type="application/json")
        for collector in COLLECTORS:
            store.enqueue_run(db, tenant_id, collector)
            run = store.claim_next_run(db)
            execute_run(db, run, _factory, exchange_factory)
            runs[collector] = db.execute("SELECT * FROM runs WHERE id = ?", (run["id"],)).fetchone()
    return runs, asked


def _covered(db, runs) -> dict:
    """The mailboxes each collector's stored rows cover."""
    return {c: sorted({o["mailboxId"] for o in store.run_objects(db, runs[c]["id"], KINDS[c])}) for c in COLLECTORS}


def test_under_the_limit_every_mailbox_is_checked_every_run(db, two_tenants):
    alpha, _ = two_tenants
    for _ in range(2):
        runs, asked = _round(db, alpha)
        for c in COLLECTORS:
            assert runs[c]["status"] == "succeeded", (c, runs[c]["error"])
            assert sorted(asked[c]) == sorted(IDS)
        assert _covered(db, runs) == {c: sorted(IDS) for c in COLLECTORS}


def test_over_the_limit_rotates_through_mailboxes_and_keeps_earlier_results(db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    monkeypatch.setattr(mailbox_window, "PER_MAILBOX_LIMIT", 2)

    runs, asked = _round(db, alpha)
    assert {c: sorted(a) for c, a in asked.items()} == {c: ["u0", "u1"] for c in COLLECTORS}
    assert _covered(db, runs) == {c: ["u0", "u1"] for c in COLLECTORS}

    runs, asked = _round(db, alpha)
    assert {c: sorted(a) for c, a in asked.items()} == {c: ["u2", "u3"] for c in COLLECTORS}  # not the same two again
    assert _covered(db, runs) == {c: ["u0", "u1", "u2", "u3"] for c in COLLECTORS}
    rules = {r["mailboxId"]: r for r in store.run_objects(db, runs["inbox_rules"]["id"], "inbox_rule")}
    assert rules["u0"]["displayName"] == "rule of u0"  # copied as it was, not just counted
    metric = db.execute("SELECT value FROM metrics WHERE run_id = ? AND key = 'mail.mailboxes_checked'",
                        (runs["inbox_rules"]["id"],)).fetchone()
    assert metric["value"] == 4

    runs, asked = _round(db, alpha)
    assert {c: sorted(a) for c, a in asked.items()} == {c: ["u0", "u4"] for c in COLLECTORS}  # never checked first
    assert _covered(db, runs) == {c: sorted(IDS) for c in COLLECTORS}


def test_over_the_limit_warning_says_what_was_covered(db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    monkeypatch.setattr(mailbox_window, "PER_MAILBOX_LIMIT", 2)

    runs, _ = _round(db, alpha)
    assert runs["inbox_rules"]["status"] == "partial"
    assert runs["inbox_rules"]["error"] == (
        "Checked inbox rules for 2 of 5 mailboxes this run (2 per run, so each mailbox is checked about every "
        "3 runs); 3 mailboxes not checked yet")

    runs, _ = _round(db, alpha)
    day = store.run_objects(db, runs["mailboxes"]["id"], "mailbox_permission_coverage")[0]["checkedAt"]["u0"][:10]
    assert runs["mailboxes"]["error"] == (
        "Checked mailbox permissions for 2 of 5 mailboxes this run (2 per run, so each mailbox is checked about "
        f"every 3 runs); showing earlier results for 2 mailboxes, last checked on {day}; 1 mailbox not checked yet")
    assert runs["mailbox_usage"]["error"].startswith("Checked mailbox sizes for 2 of 5 mailboxes this run")


@pytest.mark.parametrize("collector", COLLECTORS)
def test_under_the_limit_has_no_warning(db, two_tenants, collector):
    alpha, _ = two_tenants
    runs, _ = _round(db, alpha)
    assert runs[collector]["error"] is None
