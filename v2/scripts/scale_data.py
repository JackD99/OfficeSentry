"""A large database for sizing and timing: N tenants with up to two years of nightly history.

    cd v2
    python scripts/scale_data.py --data-dir /tmp/scale --tenants 200 --days 730
    python scripts/scale_data.py --data-dir /tmp/scale-old --tenants 200 --days 730 --plain --compact

A few template tenants of different sizes are collected first, the way ``demo_data.py`` collects its demo
tenants (every collector against the recorded-style Graph responses in ``tests/``, with extra staff). Each of
the N tenants is then a copy of one template, with history built as two years of the default retention
would leave it:

- a run row of every collector and its metrics, every night (neither is ever pruned);
- run log lines for the last 90 nights (``store.RUN_LOG_DAYS``);
- objects only for the runs retention keeps: the last 30 nights and the last run of each of 12 months;
- the template's open findings, a contact, and a place on one month-end report plan (send on approval), so
  the month-end queue, the Clients home page and the findings pages have the work a real MSP's would.

Nightly snapshots are copies of their template, so What changed finds nothing to report, and the data
dedupes perfectly; compression doesn't depend on that (each pack is compressed on its own). ``--plain``
stores objects the way Office Sentry did before compression (one row of JSON text per object), and
``--compact`` then runs ``python -m officesentry compact`` on it, so one run measures before and after.
It prints the file size, the largest tables and how long a compressed backup takes.

``--history`` then measures the daily history (``state_history.py``): it backfills it from the kept snapshots,
as the worker does after the upgrade, and records ``--history-nights`` more nights for every tenant in which
``--churn`` of the objects change and the fast-changing fields (``state_history.VOLATILE``: last sign-ins,
mailbox sizes, usage) move for ``--active`` of them. The snapshots here are identical copies, so the backfill is
as small as it can be; the nights after it show what real change costs, per night and over 25 months.

Development only: it imports the test fixtures, which aren't in the Docker image.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

V2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(V2))
sys.path.insert(0, str(V2 / "scripts"))

import demo_data  # noqa: E402

from officesentry import store  # noqa: E402
from officesentry.certs import generate_certificate  # noqa: E402
from officesentry.db import connect, migrate, transaction  # noqa: E402

# Staff per template tenant (the fixtures add about 10 more accounts each). Tenants take these in turn, so
# the average is about 110 accounts, as in the 200-tenant review (sizes 15 to 275).
SIZES = (8, 25, 45, 70, 95, 125, 160, 200, 265)
KEEP_DAILY, KEEP_MONTHLY = 30, 12


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _unpack(db) -> None:
    """Objects as stored before compression: one row of JSON text per object."""
    for row in db.execute("SELECT run_id, kind, object_id, data FROM objects WHERE typeof(data) = 'blob'").fetchall():
        objects = [json.loads(line) for line in store.pack_lines(row["data"])]
        db.execute(
            "DELETE FROM objects WHERE run_id = ? AND kind = ? AND object_id = ?",
            (row["run_id"], row["kind"], row["object_id"]),
        )
        db.executemany(
            "INSERT INTO objects (run_id, kind, object_id, data) VALUES (?, ?, ?, ?)",
            [(row["run_id"], row["kind"], i, json.dumps(d, sort_keys=True)) for i, d in objects],
        )


def build_templates(path: Path, vault, plain: bool) -> list[dict]:
    """Collect one tenant of each size into ``path``. Returns {tenant, users, runs: {collector: run id}}."""
    db = connect(path)
    migrate(db)
    cid = store.create_connection(db, vault, "Scale app", generate_certificate("scale"))
    now = datetime.now(UTC)
    out = []
    for i, staff in enumerate(SIZES):
        t = dict(demo_data.TENANTS[i % len(demo_data.TENANTS)], staff=staff)
        tid = store.create_tenant(db, f"Template {i + 1}", f"template{i + 1}.onmicrosoft.com", cid)
        demo_data.collect(db, tid, t, now, first=False)
        runs = {
            r["collector"]: r["id"]
            for r in db.execute(
                "SELECT collector, MAX(id) AS id FROM runs WHERE tenant_id = ? GROUP BY collector", (tid,)
            )
        }
        users = store.run_objects(db, runs["users"], "user") if "users" in runs else []
        out.append({"tenant": tid, "users": len(users), "runs": runs})
        print(f"  template {i + 1}: {len(users)} accounts, {len(runs)} collectors", flush=True)
    if plain:
        _unpack(db)
    db.close()
    return out


def kept_days(days: int) -> set[int]:
    """Days ago (0 = last night) whose run the default retention keeps objects for."""
    end = datetime.now(UTC).date()
    keep = set(range(min(days, KEEP_DAILY)))
    months: dict[tuple[int, int], int] = {}
    for ago in range(days):
        day = end - timedelta(days=ago)
        months.setdefault((day.year, day.month), ago)  # newest first, so the month's last night
    keep |= set(sorted(months.values())[:KEEP_MONTHLY])
    return keep


def add_tenant(db, n: int, template: dict, days: int, keep: set[int], cid: int) -> None:
    """One tenant: a copy of ``template`` with ``days`` nights of history."""
    tid = store.create_tenant(db, f"Client {n:04}", f"client{n:04}.onmicrosoft.com", cid)
    db.execute(
        "UPDATE tenants SET consented_at = ?, capabilities = (SELECT capabilities FROM tpl.tenants"
        " WHERE id = ?) WHERE id = ?",
        (_iso(datetime.now(UTC)), template["tenant"], tid),
    )
    # Each night's collection starts an hour or two before now's time of day, not at 02:00: the change report's
    # stored rows are keyed on the last run before "30 days ago", which moves run by run while that passes a
    # night's collection, so between 02:00 and 02:15 every visit rebuilt the report (#200 on 17 October).
    end = (datetime.now(UTC) - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    db.execute(
        "CREATE TEMP TABLE IF NOT EXISTS plan (run_id INTEGER, template_run INTEGER, at TEXT,"
        " objects INTEGER, log INTEGER)"
    )
    db.execute("DELETE FROM plan")
    for ago in range(days - 1, -1, -1):
        night = end - timedelta(days=ago)
        for k, (collector, template_run) in enumerate(sorted(template["runs"].items())):
            at = _iso(night + timedelta(seconds=20 * k))
            run_id = db.execute(
                "INSERT INTO runs (tenant_id, collector, status, trigger, scheduled_for, created_at, started_at,"
                " finished_at, object_count) SELECT ?, ?, status, 'schedule', ?, ?, ?, ?, object_count"
                " FROM tpl.runs WHERE id = ?",
                (tid, collector, at, at, at, at, template_run),
            ).lastrowid
            db.execute("INSERT INTO plan VALUES (?, ?, ?, ?, ?)", (run_id, template_run, at, ago in keep, ago < 90))
    db.execute(
        "INSERT INTO metrics (run_id, tenant_id, key, value, captured_at) SELECT p.run_id, ?, m.key,"
        " m.value, p.at FROM plan p JOIN tpl.metrics m ON m.run_id = p.template_run",
        (tid,),
    )
    db.execute(
        "INSERT INTO run_events (run_id, ts, level, message) SELECT p.run_id, p.at, e.level, e.message"
        " FROM plan p JOIN tpl.run_events e ON e.run_id = p.template_run WHERE p.log ORDER BY p.run_id, e.id"
    )
    db.execute(
        "INSERT INTO objects (run_id, kind, object_id, data, items) SELECT p.run_id, o.kind, o.object_id,"
        " o.data, o.items FROM plan p JOIN tpl.objects o ON o.run_id = p.template_run WHERE p.objects"
    )
    db.execute(
        "INSERT INTO findings (tenant_id, rule_key, object_id, severity, subject, detail, first_seen, last_seen)"
        " SELECT ?, rule_key, object_id, severity, subject, detail, first_seen, last_seen FROM tpl.findings"
        " WHERE tenant_id = ? AND resolved_at IS NULL",
        (tid, template["tenant"]),
    )
    db.execute(
        "INSERT INTO contacts (tenant_id, name, email, created_at) VALUES (?, ?, ?, ?)",
        (tid, f"Contact {n:04}", f"it@client{n:04}.example", _iso(datetime.now(UTC))),
    )


def add_plan(db) -> None:
    """Every tenant on one month-end plan that waits for approval, as an MSP reviewing reports would have."""
    from officesentry import month_end

    values = {
        "name": "Monthly security report",
        "reports": ["review"],
        "include_xlsx": 1,
        "day_of_month": 1,
        "hour": 8,
        "timezone": "",
        "subject": month_end.DEFAULT_SUBJECT,
        "message": "",
        "mode": "approval",
        "delivery": "email",
        "bcc_team": 0,
    }
    with transaction(db, immediate=True):
        plan = month_end.get_plan(db, month_end.save_plan(db, values, "scale"))
        ids = [r["id"] for r in db.execute("SELECT id FROM tenants ORDER BY id")]
        month_end.apply_plan(db, plan, ids, "scale", "UTC")


def table_sizes(db) -> list[tuple[str, int]]:
    """Bytes per table, its indexes included, largest first."""
    owner = {r["name"]: r["tbl_name"] for r in db.execute("SELECT name, tbl_name FROM sqlite_master")}
    sizes: dict[str, int] = {}
    for r in db.execute("SELECT name, SUM(pgsize) AS bytes FROM dbstat GROUP BY name"):
        table = owner.get(r["name"], r["name"])
        sizes[table] = sizes.get(table, 0) + r["bytes"]
    return sorted(sizes.items(), key=lambda kv: -kv[1])


def report(db, db_path: Path, label: str) -> None:
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    gb = db_path.stat().st_size / 1e9
    objects = db.execute("SELECT COALESCE(SUM(items), 0) FROM objects").fetchone()[0]
    print(
        f"\n{label}: {gb:.2f} GB, {objects:,} objects in {db.execute('SELECT COUNT(*) FROM objects').fetchone()[0]:,}"
        " rows"
    )
    for table, size in table_sizes(db)[:6]:
        print(f"  {table:24} {size / 1e9:6.2f} GB")


def timed_backup(db_path: Path) -> None:
    from officesentry import backups

    start = time.monotonic()
    dest, _ = backups.backup(db_path, db_path.parent / "backups", keep=1)
    print(f"  compressed backup: {dest.stat().st_size / 1e9:.2f} GB in {time.monotonic() - start:.0f} s")


def state_bytes(db) -> int:
    return sum(size for table, size in table_sizes(db) if table.startswith("state_"))


def _churned(objects: dict, night: int, rng: random.Random, churn: float, active: float) -> dict:
    """One more night of a run's objects: ``churn`` of them edited, a few added and removed, and the fast-changing
    fields of ``active`` of them moved."""
    from officesentry import state_history

    out = {}
    for kind, items in objects.items():
        fields = state_history.VOLATILE.get(kind, ())
        new = {}
        for oid, data in items.items():
            roll = rng.random()
            if roll < churn / 10:
                continue  # removed
            if isinstance(data, dict):
                if roll < churn:
                    data = dict(data, edited=night)
                if fields and rng.random() < active:
                    data = dict(data, **{f: f"2026-{night:04}" for f in fields if f in data})
            new[oid] = data
            if roll > 1 - churn / 10:
                new[f"{oid}-{night}"] = data  # added
        out[kind] = new
    return out


def measure_history(db, nights: int, churn: float, active: float) -> None:
    from officesentry import state_history

    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    snapshots = sum(size for table, size in table_sizes(db) if table == "objects")
    start = time.monotonic()
    done, _ = state_history.catch_up(db, seconds=None)
    took = time.monotonic() - start
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    backfilled = state_bytes(db)
    st = state_history.status(db)
    print(
        f"\nDaily history backfill: {done:,} runs in {took:.0f} s ({took / max(done, 1) * 1000:.1f} ms a run), "
        f"{st['objects']:,} objects as {st['versions']:,} versions: {backfilled / 1e9:.3f} GB "
        f"(the kept snapshots take {snapshots / 1e9:.3f} GB)"
    )
    if not nights:
        return
    rng = random.Random(7)  # noqa: S311 (test data)
    series = db.execute(
        "SELECT run_id, tenant_id, collector, finished_at FROM state_runs WHERE run_id IN"
        " (SELECT MAX(run_id) FROM state_runs GROUP BY tenant_id, collector)"
    ).fetchall()
    latest = {(r["tenant_id"], r["collector"]): state_history._snapshot(db, r["run_id"]) for r in series}
    start, recorded = time.monotonic(), 0
    for night in range(1, nights + 1):
        with transaction(db, immediate=True):
            for (tenant_id, collector), objects in latest.items():
                objects = latest[tenant_id, collector] = _churned(objects, night, rng, churn, active)
                at = _iso(datetime.now(UTC) + timedelta(days=night))
                run_id = db.execute(
                    "INSERT INTO runs (tenant_id, collector, status, trigger, created_at, started_at,"
                    " finished_at, object_count) VALUES (?, ?, 'succeeded', 'schedule', ?, ?, ?, ?)",
                    (tenant_id, collector, at, at, at, sum(map(len, objects.values()))),
                ).lastrowid
                run = {"id": run_id, "tenant_id": tenant_id, "collector": collector, "finished_at": at}
                state_history.record_run(db, run, objects)
                recorded += 1
        if night % 10 == 0 or night == nights:
            print(f"  {night} nights recorded ({time.monotonic() - start:.0f} s)", flush=True)
    took = time.monotonic() - start
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    grown = state_bytes(db)
    per_night = (grown - backfilled) / nights
    st = state_history.status(db)
    print(
        f"Daily history after {nights} more nights ({churn:.1%} of objects changed and {active:.0%} of sign-ins "
        f"and sizes moved each night): {grown / 1e9:.3f} GB, {st['versions']:,} versions; "
        f"{per_night / 1e6:.1f} MB a night, {took / recorded * 1000:.1f} ms a run"
    )
    print(
        f"  At 25 months (760 nights): about {(backfilled + per_night * 760) / 1e9:.2f} GB for a day-by-day "
        f"history, against {snapshots / 1e9:.2f} GB for the snapshots kept today"
    )
    for table, size in table_sizes(db):
        if table.startswith("state_"):
            print(f"  {table:24} {size / 1e9:6.3f} GB")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", required=True, type=Path, help="a new, empty data directory")
    parser.add_argument("--tenants", type=int, default=200)
    parser.add_argument("--days", type=int, default=730, help="nights of history (default: two years)")
    parser.add_argument("--plain", action="store_true", help="store objects as before compression")
    parser.add_argument("--compact", action="store_true", help="then run the compact command and measure again")
    parser.add_argument("--no-backup", action="store_true", help="don't time a compressed backup")
    parser.add_argument("--history", action="store_true", help="then backfill and measure the daily history")
    parser.add_argument("--history-nights", type=int, default=30, help="with --history: nights to record after it")
    parser.add_argument("--churn", type=float, default=0.01, help="with --history: share of objects edited a night")
    parser.add_argument(
        "--active", type=float, default=0.7, help="with --history: share whose sign-in times and sizes move a night"
    )
    args = parser.parse_args()

    from officesentry.config import load_config
    from officesentry.crypto import Vault

    if args.data_dir.exists() and any(args.data_dir.iterdir()):
        sys.exit(f"{args.data_dir} isn't empty; use a new data directory.")
    cfg = load_config(data_dir=args.data_dir)
    vault = Vault(cfg.encryption_keys)
    started = time.monotonic()
    print(f"Collecting {len(SIZES)} template tenants...", flush=True)
    templates = build_templates(args.data_dir / "templates.db", vault, args.plain)

    db = connect(cfg.db_path)
    migrate(db)
    db.execute("PRAGMA synchronous = OFF")
    db.execute("ATTACH DATABASE ? AS tpl", (str(args.data_dir / "templates.db"),))
    cid = store.create_connection(db, vault, "Scale app", generate_certificate("scale"))
    keep = kept_days(args.days)
    order = random.Random(1).sample(range(args.tenants), args.tenants)  # noqa: S311 (test data)
    accounts = 0
    for n in range(args.tenants):
        template = templates[order[n] % len(templates)]
        accounts += template["users"]
        with transaction(db, immediate=True):
            add_tenant(db, n + 1, template, args.days, keep, cid)
        if (n + 1) % 10 == 0 or n + 1 == args.tenants:
            print(f"  {n + 1} tenants ({time.monotonic() - started:.0f} s)", flush=True)
    db.execute("DETACH DATABASE tpl")
    (args.data_dir / "templates.db").unlink()
    add_plan(db)
    db.execute("ANALYZE")
    print(
        f"\n{args.tenants} tenants, {accounts / args.tenants:.0f} accounts on average, {args.days} nights, "
        f"objects kept for {len(keep)} of them"
    )
    report(db, cfg.db_path, "Database (plain, as before compression)" if args.plain else "Database (compressed)")
    if not args.no_backup:
        timed_backup(cfg.db_path)
    if args.compact:
        from officesentry.packing import compact

        start = time.monotonic()
        compact(db, report=lambda line: print("  " + line))
        print(f"  compact took {time.monotonic() - start:.0f} s")
        report(db, cfg.db_path, "After compact")
        if not args.no_backup:
            timed_backup(cfg.db_path)
    if args.history:
        measure_history(db, args.history_nights, args.churn, args.active)
    db.close()
    print(
        f"\nDone in {time.monotonic() - started:.0f} s. Portal: OFFICESENTRY_DATA_DIR={args.data_dir} "
        "python -m officesentry web (create an admin with create-admin first)"
    )


if __name__ == "__main__":
    main()
