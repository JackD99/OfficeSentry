"""Serve a recorded live run (``live-test --record``) back through ``responses``.

Graph requests match on path and query, Exchange calls on cmdlet and
parameters, DNS on name and record type. A request the recording doesn't
have gets a 404, so a collector that starts asking for something new fails
loudly instead of silently reading nothing.
"""

import json
import re
from pathlib import Path

import responses

from officesentry.exchange import EXCHANGE_ROOT
from officesentry.graph import GRAPH_ROOT
from officesentry.recording import request_key

FIXTURES = Path(__file__).parent / "fixtures"
# Collectors ask for "the last 30 days" with a timestamp in the query; match on the shape, not the time.
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z")


def load(name: str = "live_tenant.json") -> dict:
    return json.loads((FIXTURES / name).read_text())


def _graph_key(url: str) -> str:
    return TIMESTAMP.sub("<time>", json.dumps(request_key(url)))


def _cmdlet_key(url: str, cmdlet: dict) -> str:
    return json.dumps([request_key(url), cmdlet.get("CmdletName"), cmdlet.get("Parameters") or {}], sort_keys=True)


def mock(rsps, fixture: dict) -> list[str]:
    """Register the recording on ``rsps``. Returns a list that collects unmatched requests."""
    graph: dict[str, list] = {}
    exchange: dict[str, list] = {}
    for e in fixture["entries"]:
        if e["api"] == "graph":
            graph.setdefault(_graph_key(e["url"]), []).append(e)
        elif e["api"] == "exchange":
            exchange.setdefault(_cmdlet_key(e["url"], e["cmdlet"]), []).append(e)
    missing: list[str] = []

    def serve(table: dict, key: str, label: str):
        recorded = table.get(key)
        if not recorded:
            missing.append(label)
            return 404, {}, json.dumps({"error": {"code": "NotRecorded", "message": f"not in fixture: {label}"}})
        e = recorded.pop(0) if len(recorded) > 1 else recorded[0]
        return e["status"], {}, json.dumps(e["body"]) if e["body"] is not None else ""

    def graph_cb(request):
        return serve(graph, _graph_key(request.url), request.url)

    def exchange_cb(request):
        cmdlet = json.loads(request.body)["CmdletInput"]
        return serve(exchange, _cmdlet_key(request.url, cmdlet), cmdlet.get("CmdletName"))

    rsps.add_callback(responses.GET, re.compile(re.escape(GRAPH_ROOT) + "/.*"), callback=graph_cb,
                      content_type="application/json")
    rsps.add_callback(responses.POST, re.compile(re.escape(EXCHANGE_ROOT) + "/adminapi/.*"), callback=exchange_cb,
                      content_type="application/json")
    return missing


def fake_dns(fixture: dict):
    answers = {(e["name"], e["rdtype"]): e["answer"] for e in fixture["entries"] if e["api"] == "dns"}
    return lambda name, rdtype: answers.get((name, rdtype), [])
