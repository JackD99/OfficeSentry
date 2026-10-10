"""The live canary: has Microsoft changed what it sends since the fixture was recorded?

``live-test --canary REPORT`` collects from the test tenant as usual, scrubs
the responses in memory as ``--record`` does, and compares their *shape* with
``tests/fixtures/live_tenant.json``, the recording CI replays: for each call
(the endpoint catalogue's name for it, plus its ``$select``), which fields come
back and what type each holds. Values are never compared or written down, so
the report is safe to put in a GitHub issue. It also lists the ``Deprecation``
and ``Sunset`` headers Microsoft sends on APIs it is retiring.

What counts as a change (and fails the canary):

- a field the fixture has that no longer comes back, where the object around it still does;
- a field whose type changed (text to number, object to list); null is any type's;
- a call that used to succeed and now fails (403, 404, 400...);
- a Deprecation or Sunset header the fixture doesn't have for that call.

Re-recording the fixture keeps the headers, which acknowledges them: from then on
they're listed under "For information" with their dates, as are new fields and
calls the fixture hasn't recorded yet.
Lists are compared item by item, and an empty list has nothing to compare, so
data that differs from night to night (no sign-ins today) isn't a change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

from . import endpoints

GUID = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
# OData's own annotations come and go with paging and counting; @odata.type is part of the data.
KEEP_ANNOTATIONS = {"@odata.type"}


def call_key(entry: dict) -> str | None:
    """How a recorded call is named in the report: ``v1.0 /users ($select=id,mail)`` or ``Exchange Get-Mailbox``."""
    if entry["api"] == "exchange":
        name = (entry.get("cmdlet") or {}).get("CmdletName")
        return f"Exchange {name}" if name else None
    if entry["api"] != "graph":
        return None
    key = endpoints.graph_key(entry["url"])
    query = dict(parse_qsl(urlsplit(entry["url"]).query))
    select = ",".join(sorted(f for f in query.get("$select", "").split(",") if f))
    return f"{key} ($select={select})" if select else key


def _type(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true/false"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, list):
        return "list"
    return "object"


def shape(value, path: str = "", out: dict[str, set[str]] | None = None) -> dict[str, set[str]]:
    """Every field path in ``value`` (``value[].assignedLicenses[].skuId``) and the types seen there.

    An object that names its type (``@odata.type``) has its fields under that type
    (``value[]<group>.mail``), so a list mixing users and groups compares each kind with itself."""
    out = {} if out is None else out
    if isinstance(value, dict) and isinstance(value.get("@odata.type"), str):
        path = f"{path}<{value['@odata.type'].rsplit('.', 1)[-1].lstrip('#')}>"
    out.setdefault(path, set()).add(_type(value))
    if isinstance(value, dict):
        for key, item in value.items():
            if key.startswith("@") and key not in KEEP_ANNOTATIONS:
                continue
            name = "{id}" if GUID.match(key) else key
            shape(item, f"{path}.{name}" if path else name, out)
    elif isinstance(value, list):
        for item in value:
            shape(item, f"{path}[]", out)
    return out


@dataclass
class Call:
    statuses: set[int] = field(default_factory=set)
    errors: set[str] = field(default_factory=set)
    fields: dict[str, set[str]] = field(default_factory=dict)
    headers: dict[str, set[str]] = field(default_factory=dict)


def calls(fixture: dict) -> dict[str, Call]:
    """The recording's calls by ``call_key``, with the shape of every successful response merged."""
    out: dict[str, Call] = {}
    for entry in fixture.get("entries", []):
        key = call_key(entry)
        if key is None:
            continue
        call = out.setdefault(key, Call())
        call.statuses.add(entry["status"])
        body = entry.get("body")
        if 200 <= entry["status"] < 300:
            if body is not None:
                shape(body, "", call.fields)
        elif isinstance(body, dict) and isinstance(body.get("error"), dict) and body["error"].get("code"):
            call.errors.add(str(body["error"]["code"]))
        for name, value in (entry.get("headers") or {}).items():
            call.headers.setdefault(name, set()).add(value)
    return out


def _parent(path: str) -> str:
    return path.rsplit(".", 1)[0] if "." in path else ""


def _ok(call: Call) -> bool:
    return any(200 <= s < 300 for s in call.statuses)


@dataclass
class Report:
    changes: dict[str, list[str]] = field(default_factory=dict)
    notices: dict[str, list[str]] = field(default_factory=dict)
    info: dict[str, list[str]] = field(default_factory=dict)
    failed_collectors: list[str] = field(default_factory=list)
    compared: int = 0

    @property
    def failed(self) -> bool:
        return bool(self.changes or self.notices or self.failed_collectors)

    def markdown(self) -> str:
        lines = []
        if self.failed:
            lines.append("**The live canary found something to look at.** Microsoft's answers no longer match "
                         "the recording the tests replay (`v2/tests/fixtures/live_tenant.json`).")
        else:
            lines.append("**Nothing changed.** Microsoft's answers match the recording the tests replay.")
        lines += ["", f"Calls compared with the recording: {self.compared}.", ""]
        if self.failed_collectors:
            lines += ["### Collectors that failed", "",
                      *[f"- {name}" for name in self.failed_collectors],
                      "", "If every collector failed, signing in failed: check the test app's federated "
                      "credential (`v2/tests/nightly/README.md`, \"Live canary\"). Otherwise run "
                      "`python -m officesentry live-test --collector <key>` with the test tenant's settings to "
                      "see the error.", ""]
        for title, section in (("What Microsoft changed", self.changes),
                               ("Deprecation and sunset notices", self.notices),
                               ("For information (not a failure)", self.info)):
            if not section:
                continue
            lines += [f"### {title}", ""]
            for key in sorted(section):
                lines.append(f"- `{key}`")
                lines += [f"  - {line}" for line in section[key]]
            lines.append("")
        if self.failed:
            lines += ["### What to do", "",
                      "1. If a field moved or changed type, fix the collector that reads it, with a test.",
                      "2. If a call is being retired, move to its replacement before the sunset date "
                      "(the endpoint catalogue, `officesentry/endpoints.py`, says what each call falls back to).",
                      "3. Re-record the fixture (`python -m officesentry live-test --record "
                      "tests/fixtures/live_tenant.json`) so the tests replay what Microsoft sends now.", ""]
        return "\n".join(lines)


def compare(baseline: dict, current: dict, failed_collectors: list[str] = ()) -> Report:
    """Compare a fresh scrubbed recording (``current``) with the committed fixture (``baseline``)."""
    old, new = calls(baseline), calls(current)
    report = Report(failed_collectors=sorted(failed_collectors))
    for key, now in new.items():
        before = old.get(key)
        known = before.headers if before else {}
        for name, values in sorted(now.headers.items()):
            for value in sorted(values):
                if value in known.get(name, set()):
                    report.info.setdefault(key, []).append(f"known retirement notice, {name}: {value}")
                else:
                    report.notices.setdefault(key, []).append(f"{name}: {value}")
        if before is None:
            report.info.setdefault(key, []).append("not in the recording yet (re-record to cover it)")
            continue
        report.compared += 1
        if _ok(before) and not _ok(now):
            codes = ", ".join(sorted(map(str, now.statuses)) + sorted(now.errors))
            report.changes.setdefault(key, []).append(f"used to answer, now fails ({codes})")
            continue
        if not _ok(before) and _ok(now):
            report.info.setdefault(key, []).append("used to fail, now answers")
            continue
        lines = _field_changes(before.fields, now.fields)
        if lines:
            report.changes.setdefault(key, []).extend(lines)
        added = sorted(p for p in now.fields if p not in before.fields and "object" in before.fields.get(
            _parent(p), set()) and not p.endswith(("[]", ">")))
        if added:
            report.info.setdefault(key, []).append("new fields: " + ", ".join(f"`{p}`" for p in added))
    return report


def _field_changes(before: dict[str, set[str]], now: dict[str, set[str]]) -> list[str]:
    lines = []
    for path, types in sorted(before.items()):
        if not path or path.endswith(("[]", ">")):
            continue  # the body itself, list items (an empty list has none) and objects of one kind
        if path not in now:
            if "object" in now.get(_parent(path), set()):
                lines.append(f"`{path}` no longer comes back")
            continue
        was, is_ = types - {"null"}, now[path] - {"null"}
        if was and is_ and not was & is_:
            lines.append(f"`{path}` was {' or '.join(sorted(was))}, now {' or '.join(sorted(is_))}")
    return lines
