"""An index of what the sidebar search box finds, so a search reads one table instead of every snapshot.

``web/search.py`` finds people, mailboxes, forwarding addresses, sign-in IP addresses, apps, devices
and domains in each tenant's latest snapshot. Scanning those objects in Python grows with every tenant
and every account: at 200 small tenants a search took most of a second, and a real estate of large
tenants would take many. So the worker keeps ``search_index``: one row per tenant, object and value
to match (``KINDS``, lower case), with the run the object came from, built by ``refresh`` in the
``precompute`` task after each tenant's collection. A search then asks SQLite for the rows that
match (``find``) and loads only the matching objects.

The index is used only while it is current: ``fingerprint`` is the runs it was built from, kept in
``summary_cache`` (kind ``search``), and ``current()`` says which tenants' rows match their latest
runs now. A tenant whose index is behind (its collection finished a moment ago, or the worker is
down) is scanned the old way, so a search never misses what the latest snapshot holds. Rows carry
the tenant and every query names the tenants the person may see, so the index never shows anyone a
tenant they couldn't open; ``lifecycle.delete_tenant`` removes a tenant's rows through the foreign key.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from . import store
from .db import transaction, utcnow
from .mailflow import inbox_rule_risk, mailbox_forwarding
from .reports.monthly_review import _code_version
from .snapshot import TenantSnapshot

log = logging.getLogger(__name__)

STATE = "search"  # the summary_cache kind holding each tenant's fingerprint
CHUNK = 500  # tenants per query


@dataclass(frozen=True)
class Kind:
    """One kind of thing a search finds: the collector and object kind it reads, and the values to match."""

    key: str  # names the kind in the index and picks how web/search.py shows a match
    collector: str
    kind: str
    values: Callable[[dict], Iterable]
    ip: bool = False  # values are IP addresses: matched by prefix, and only for an IP-like query
    each: bool = False  # each value is a thing of its own (an organisation's domains): a result per value matched


def _rule_targets(r: dict) -> list[str]:
    return inbox_rule_risk(r, set()).forwards_to


def _domain_names(org: dict) -> list[str]:
    return [d["name"] for d in org.get("verifiedDomains") or [] if d.get("name")]


# In the order results are listed within a tenant.
KINDS: tuple[Kind, ...] = (
    Kind("person", "users", "user", lambda u: (u.get("displayName"), u.get("userPrincipalName"), u.get("mail"))),
    Kind("group", "groups", "group", lambda g: (g.get("displayName"), g.get("mail"))),
    Kind(
        "mailbox",
        "mailboxes",
        "mailbox",
        lambda m: (
            m.get("DisplayName"),
            m.get("PrimarySmtpAddress"),
            m.get("UserPrincipalName"),
            m.get("Alias"),
            m.get("Name"),
        ),
    ),
    Kind("forwarding", "mailboxes", "mailbox", lambda m: (mailbox_forwarding(m),)),
    Kind(
        "site",
        "sharepoint_usage",
        "site",
        lambda s: (s["url"],) if isinstance(s.get("url"), str) and s["url"] and not s.get("deleted") else (),
    ),
    Kind("rule", "inbox_rules", "inbox_rule", _rule_targets),
    Kind("failure_ip", "sign_ins", "sign_in_failure_ip", lambda i: (i.get("ip"),), ip=True),
    Kind("risky", "sign_ins", "risky_sign_in", lambda r: (r.get("ip"),), ip=True),
    Kind("location", "sign_ins", "sign_in_location", lambda loc: loc.get("ips") or (), ip=True),
    Kind("app", "app_consents", "service_principal", lambda sp: (sp.get("displayName"), sp.get("appId"))),
    Kind("registration", "app_consents", "application", lambda a: (a.get("displayName"), a.get("appId"))),
    Kind("managed_device", "devices", "managed_device", lambda d: (d.get("deviceName"),)),
    Kind("entra_device", "devices", "entra_device", lambda d: (d.get("displayName"),)),
    Kind("domain", "tenant_profile", "organization", _domain_names, each=True),
)
BY_KEY = {k.key: k for k in KINDS}
IP_KINDS = tuple(k.key for k in KINDS if k.ip)
COLLECTORS = tuple(dict.fromkeys(k.collector for k in KINDS))


def _text(value) -> str:
    return value.lower() if isinstance(value, str) and value else ""


def fingerprint(snap: TenantSnapshot) -> str:
    """The runs the tenant's index is built from: the latest run of each collector searched, and the code."""
    runs = [[c, snap.runs[c]["id"]] for c in COLLECTORS if c in snap.runs]
    return json.dumps([_code_version(), runs])


def rows(snap: TenantSnapshot) -> list[tuple[str, int, str, str]]:
    """(kind key, run id, object id, value) for everything searchable in the snapshot, in result order."""
    out = []
    for kind in KINDS:
        run = snap.runs.get(kind.collector)
        if run is None:
            continue
        for obj in snap.objects(kind.collector, kind.kind) or []:
            seen: set[str] = set()
            for value in kind.values(obj):
                text = _text(value)
                if text and text not in seen:
                    seen.add(text)
                    out.append((kind.key, run["id"], obj["_id"], text))
    return out


def refresh(conn, tenant, snap: TenantSnapshot | None = None, rebuild: bool = False) -> bool:
    """Bring the tenant's index up to its latest runs. Returns whether anything was written.

    A busy database only means the next search scans this tenant, as it would without an index."""
    snap = snap or TenantSnapshot(conn, tenant)
    want = fingerprint(snap)
    if not rebuild:
        have = conn.execute(
            "SELECT fingerprint FROM summary_cache WHERE tenant_id = ? AND kind = ?", (tenant["id"], STATE)
        ).fetchone()
        if have and have["fingerprint"] == want:
            return False
    try:
        with transaction(conn, immediate=True):
            conn.execute("DELETE FROM search_index WHERE tenant_id = ?", (tenant["id"],))
            entries = rows(snap)
            conn.executemany(
                "INSERT INTO search_index (tenant_id, kind, run_id, object_id, value) VALUES (?, ?, ?, ?, ?)",
                [(tenant["id"], *row) for row in entries],
            )
            conn.execute(
                "INSERT INTO summary_cache (tenant_id, kind, fingerprint, data, built_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(tenant_id, kind) DO UPDATE SET fingerprint = excluded.fingerprint,"
                " data = excluded.data, built_at = excluded.built_at",
                (tenant["id"], STATE, want, json.dumps({"rows": len(entries)}), utcnow()),
            )
    except sqlite3.OperationalError:
        log.warning("Couldn't update the search index for tenant %s", tenant["id"], exc_info=True)
        return False
    return True


def current(conn, tenants) -> set[int]:
    """The tenants whose index was built from their latest runs, so a search may read it."""
    ids = [t["id"] for t in tenants]
    stored: dict[int, str] = {}
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start : start + CHUNK]
        stored.update(
            (r["tenant_id"], r["fingerprint"])
            for r in conn.execute(
                f"SELECT tenant_id, fingerprint FROM summary_cache WHERE kind = ? AND tenant_id IN"
                f" ({','.join('?' * len(chunk))})",
                (STATE, *chunk),
            )
        )
    by_id = {t["id"]: t for t in tenants}
    return {tid for tid, fp in stored.items() if fp == fingerprint(TenantSnapshot(conn, by_id[tid]))}


def find(conn, tenant_ids: list[int], q: str, ip: bool) -> dict[int, list[tuple[str, int, str, str]]]:
    """Index rows matching ``q`` (lower case) for the tenants given: tenant id -> [(kind key, run id,
    object id, value)] in result order. Values match ``q`` anywhere in them, as a scan matches; when ``q`` looks
    like an IP address (``ip``), IP addresses match from the start instead."""
    out: dict[int, list] = {}
    kinds = ",".join("?" * len(IP_KINDS))
    for start in range(0, len(tenant_ids), CHUNK):
        chunk = tenant_ids[start : start + CHUNK]
        marks = ",".join("?" * len(chunk))
        if ip:  # a range the index on value answers, for the addresses; "cafe.de" is still found in a name
            queries = [
                (
                    f"SELECT rowid, * FROM search_index WHERE value >= ? AND value < ? AND tenant_id IN ({marks})"
                    f" AND kind IN ({kinds})",
                    (q, q[:-1] + chr(ord(q[-1]) + 1), *chunk, *IP_KINDS),
                ),
                (
                    f"SELECT rowid, * FROM search_index WHERE instr(value, ?) > 0 AND tenant_id IN ({marks})"
                    f" AND kind NOT IN ({kinds})",
                    (q, *chunk, *IP_KINDS),
                ),
            ]
        else:
            queries = [
                (f"SELECT rowid, * FROM search_index WHERE instr(value, ?) > 0 AND tenant_id IN ({marks})", (q, *chunk))
            ]
        found = sorted((r for sql, args in queries for r in conn.execute(sql, args)), key=lambda r: r["rowid"])
        for r in found:
            out.setdefault(r["tenant_id"], []).append((r["kind"], r["run_id"], r["object_id"], r["value"]))
    return out


def load(conn, run_id: int, kind: Kind, object_id: str) -> dict | None:
    """The indexed object itself, as ``TenantSnapshot.objects`` would give it."""
    data = store.find_object(conn, run_id, kind.kind, object_id)
    return dict(data, _kind=kind.kind, _id=object_id) if data is not None else None
