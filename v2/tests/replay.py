"""Serve a recorded live run (``live-test --record``) back through ``responses``.

Graph requests match on path and query, Exchange calls on cmdlet and
parameters, DNS on name and record type. A request the recording doesn't
have gets a 404, so a collector that starts asking for something new fails
loudly instead of silently reading nothing.

Until the fixture is re-recorded, a test can name the exceptions: ``stand_ins``
answers a new Graph request (a path, where ``{id}`` stands for any one segment;
``<path>?$skiptoken`` for the later pages of a recorded call whose next link
the collector didn't follow when the recording was made)
or a new Exchange cmdlet (``"Get-..."``, any parameters) with a body of the
documented shape, and
``new_fields`` lets a request whose ``$select`` asks for more fields than were
recorded get the recorded response (the new fields are simply absent, as for a
tenant that doesn't set them). Anything else unrecorded is still a 404 and is
reported.
"""

import json
import re
from pathlib import Path

import responses

from officesentry.exchange import EXCHANGE_ROOT
from officesentry.graph import GRAPH_ROOT
from officesentry.recording import TIMESTAMP, request_key

FIXTURES = Path(__file__).parent / "fixtures"
PAGED = "?$skiptoken"  # on a stand-in path: the later pages of a recorded call


def load(name: str = "live_tenant.json") -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _cmdlet_key(url: str, cmdlet: dict) -> str:
    # Times in parameters (the message trace's StartDate) are blanked, as in Graph queries.
    return TIMESTAMP.sub(
        "<time>",
        json.dumps([request_key(url), cmdlet.get("CmdletName"), cmdlet.get("Parameters") or {}], sort_keys=True),
    )


def _select(query) -> tuple[set[str], list[tuple[str, str]]]:
    fields = {f for k, v in query if k == "$select" for f in v.split(",")}
    return fields, [(k, v) for k, v in query if k != "$select"]


def _recorded_subset(graph: dict[str, list], url: str) -> list | None:
    """Recorded entries for the same path and parameters whose $select is a subset of the one asked for."""
    path, query = request_key(url)
    fields, rest = _select(query)
    for key, entries in graph.items():
        recorded_path, recorded_query = json.loads(key)
        recorded_fields, recorded_rest = _select([tuple(q) for q in recorded_query])
        if recorded_path == path and recorded_rest == rest and recorded_fields and recorded_fields <= fields:
            return entries
    return None


def mock(
    rsps, fixture: dict, stand_ins: dict[str, dict] | None = None, new_fields: frozenset[str] = frozenset()
) -> list[str]:
    """Register the recording on ``rsps``. Returns a list that collects unmatched requests.

    ``stand_ins`` maps Graph paths (``/v1.0/...``) to bodies for requests the recording doesn't have;
    ``new_fields`` names Graph paths whose ``$select`` may ask for fields the recording didn't.
    """
    stand_ins = stand_ins or {}
    graph: dict[str, list] = {}
    exchange: dict[str, list] = {}
    for e in fixture["entries"]:
        if e["api"] == "graph":
            graph.setdefault(json.dumps(request_key(e["url"])), []).append(e)
        elif e["api"] == "exchange":
            exchange.setdefault(_cmdlet_key(e["url"], e["cmdlet"]), []).append(e)
    missing: list[str] = []

    def serve(table: dict, key: str, label: str):
        recorded = table.get(key)
        if not recorded:
            missing.append(label)
            return 404, {}, json.dumps({"error": {"code": "NotRecorded", "message": f"not in fixture: {label}"}})
        e = recorded.pop(0) if len(recorded) > 1 else recorded[0]
        return e["status"], e.get("headers") or {}, json.dumps(e["body"]) if e["body"] is not None else ""

    patterns = [
        (
            re.compile(
                "".join("[^/]+" if part == "{id}" else re.escape(part) for part in re.split(r"(\{id\})", path)) + r"\Z"
            ),
            paged,
            body,
        )
        for path, paged, body in (
            (path.removesuffix(PAGED), path.endswith(PAGED), body) for path, body in stand_ins.items()
        )
        if path.startswith("/")
    ]

    def graph_cb(request):
        path, query = request_key(request.url)
        key = json.dumps((path, query))
        if key not in graph and path in new_fields:
            entries = _recorded_subset(graph, request.url)
            if entries:
                graph[key] = entries
        if key not in graph:
            later_page = any(k == "$skiptoken" for k, _ in query)
            for pattern, paged, body in patterns:
                if pattern.match(path) and paged == later_page:
                    return 200, {}, json.dumps(body)
        return serve(graph, key, request.url)

    def exchange_cb(request):
        cmdlet = json.loads(request.body)["CmdletInput"]
        key = _cmdlet_key(request.url, cmdlet)
        if key not in exchange and cmdlet.get("CmdletName") in stand_ins:
            return 200, {}, json.dumps(stand_ins[cmdlet["CmdletName"]])
        return serve(exchange, key, cmdlet.get("CmdletName"))

    rsps.add_callback(
        responses.GET, re.compile(re.escape(GRAPH_ROOT) + "/.*"), callback=graph_cb, content_type="application/json"
    )
    rsps.add_callback(
        responses.POST,
        re.compile(re.escape(EXCHANGE_ROOT) + "/adminapi/.*"),
        callback=exchange_cb,
        content_type="application/json",
    )
    return missing


def fake_dns(fixture: dict):
    answers = {(e["name"], e["rdtype"]): e["answer"] for e in fixture["entries"] if e["api"] == "dns"}
    return lambda name, rdtype: answers.get((name, rdtype), [])
