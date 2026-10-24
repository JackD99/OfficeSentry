"""Office Sentry never writes to a client tenant: no HTTP call but GET leaves the code, bar named exceptions.

Two layers. The source is scanned for any HTTP call that can send something
(``.post``, ``.put``, ``.patch``, ``.delete``, ``.request``), which may only appear in
the files listed in ``ALLOWED`` with the reason why. And a full collection of a
seeded tenant is run with every request recorded: each is a GET, except Exchange's
``InvokeCommand``, whose cmdlets ``test_phase4.py`` holds to ``Get-``.
"""

import ast
import json
import re
from pathlib import Path
from unittest.mock import patch

import responses

import officesentry
from officesentry import store
from officesentry.collectors import REGISTRY
from officesentry.exchange import READ_ONLY_CMDLETS
from officesentry.graph import GraphClient
from officesentry.worker import execute_run
from tests import graphdata as gd
from tests.seed import exchange_factory, mock_tenant

SOURCE = Path(officesentry.__file__).parent
SENDING = {"post", "put", "patch", "delete", "request"}
# file (relative to officesentry/, so collectors/exchange.py isn't excused by exchange.py) -> why it may send
# something other than GET. Only ever add with the owner's review.
ALLOWED = {
    "mailer.py": "sendMail from the MSP's own mailbox, through its own single-tenant mail app",
    "exchange.py": "Exchange's admin API takes every cmdlet as a POST; only Get- cmdlets are allowed",
    "entra.py": "creating Office Sentry's app (and the audit log add-on's) in the MSP's own tenant (create-app or "
    "the setup wizard), adding a release's new permissions to that app (update-app or the update wizard), and the "
    "device sign-in that allows it",
    "auditlog/feed.py": "the audit log add-on's start and stop of a client's Management Activity feed, which only "
    "tells Microsoft whether to make the client's audit records available to the add-on's app: once per content "
    "type, after a client an admin asked for has approved, or when the add-on is switched off for a client, the "
    "client is deleted or Microsoft says part of its feed is off. Never from a collection, and nothing in the "
    "client's tenant changes",
    "alerts.py": "posts alerts to the MSP's own Teams, Slack or webhook URL, never to Microsoft",
    "sso.py": "sign-in with Microsoft for staff and clients' people: the OpenID Connect token request, creating "
    "the sign-in app in the MSP's own tenant from the setup wizard, and letting that same app accept other "
    "organisations' accounts for client sign-in (its supported account types, in the MSP's tenant only)",
    "recording.py": "live-test's recording session passes on whatever method the caller used (GET)",
    "docker_api.py": "the updater container's calls to this server's own Docker engine (pull a checked release, "
    "restart the portal and worker), never to Microsoft",
}


def _is_telemetry(receiver: ast.expr) -> bool:
    """``self.telemetry.request(...)`` counts a request (ratelimit.Telemetry); it doesn't send one."""
    name = receiver.attr if isinstance(receiver, ast.Attribute) else getattr(receiver, "id", "")
    return name == "telemetry"


def _sending_calls(path: Path, root: Path = SOURCE) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    decorators = {
        id(d)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for d in node.decorator_list
    }  # @bp.post(...) is a Flask route, not an HTTP call
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in SENDING
            and id(node) not in decorators
            and not _is_telemetry(node.func.value)
        ):
            found.append(f"{path.relative_to(root)}:{node.lineno} .{node.func.attr}(")
    return found


def test_no_http_writes_outside_the_named_exceptions():
    found = [
        call
        for path in sorted(SOURCE.rglob("*.py"))
        if path.relative_to(SOURCE).as_posix() not in ALLOWED
        for call in _sending_calls(path)
    ]
    assert not found, (
        "These calls can send something other than GET. Office Sentry is read-only; if this one "
        "really must, it needs the owner's review and an entry in ALLOWED: " + ", ".join(found)
    )


def test_the_allowed_files_still_need_their_exception():
    """An exception nobody uses any more should go, so the list stays honest."""
    for name in ALLOWED:
        assert _sending_calls(SOURCE / name), f"{name} no longer sends anything but GET; remove it from ALLOWED"


def test_the_scan_catches_a_write(tmp_path):
    bad = tmp_path / "collector.py"
    source = "import requests\n\n@bp.post('/x')\ndef route():\n    requests.patch('https://graph')\n"
    bad.write_text(source, encoding="utf-8")
    assert _sending_calls(bad, tmp_path) == ["collector.py:5 .patch("]


def test_a_full_collection_only_reads(db, connection_id):
    tenant = store.create_tenant(db, "Alpha Ltd", "alpha.onmicrosoft.com", connection_id)
    factory = lambda t, c: GraphClient(lambda: "t", sleep=lambda s: None)  # noqa: E731
    with (
        responses.RequestsMock(assert_all_requests_are_fired=False) as rsps,
        patch("officesentry.dnscheck.lookup", gd.fake_dns),
    ):
        mock_tenant(rsps)
        for key in REGISTRY:
            store.enqueue_run(db, tenant, key)
        while (run := store.claim_next_run(db)) is not None:
            execute_run(db, run, factory, exchange_factory)
        calls = [(c.request.method, c.request.url, c.request.body) for c in rsps.calls]
    assert len(calls) > 50
    for method, url, body in calls:
        if method == "GET":
            continue
        assert method == "POST" and re.search(r"/adminapi/beta/[^/]+/InvokeCommand(\?|$)", url), (method, url)
        cmdlet = json.loads(body)["CmdletInput"]["CmdletName"]
        assert cmdlet.startswith("Get-") and cmdlet in READ_ONLY_CMDLETS, cmdlet
