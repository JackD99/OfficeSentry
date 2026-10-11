"""How long the pages, downloads and background jobs take on a large database.

    cd v2
    python scripts/scale_data.py --data-dir /tmp/scale --tenants 200 --days 90 --no-backup
    python scripts/timings.py --data-dir /tmp/scale --json /tmp/timings.json

Every page in ``PAGES`` is opened as an admin who sees every tenant, first on a database the worker has
never worked on ("first visit": the page builds whatever isn't stored yet), then after the worker's daily
rebuild of every precomputed row and the search index ("normal visit": what people get on a normal day).
The worker's jobs are timed in between on the same data: that daily rebuild, a findings sync for every
tenant (what each night's collection ends with), the retention pass and a compressed backup. The result is
a Markdown table (and ``--json`` for comparing two runs with ``--compare``).

The database is changed only as the worker would change it (precomputed rows, a backup, a timing admin),
so a run can be repeated. ``tests/nightly/test_scale.py`` opens the same pages against budgets.
Development only: it imports nothing from the tests, but ``scale_data.py`` does.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

V2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(V2))

USERNAME, PASSWORD = "timer", "a throwaway password for timing"  # noqa: S105 (a throwaway database)

# Label: path. {t} is the first tenant, {u} one of its accounts, {table} the MFA report's first table,
# {period} this month's month-end period, {ip} an address someone signed in from.
PAGES = {
    "Dashboard": "/",
    "Today": "/today",
    "Clients": "/clients",
    "Client tenants": "/admin/tenants",
    "All tenants: Monthly review": "/across/review",
    "All tenants: Insurance evidence": "/across/evidence",
    "All tenants: Checks": "/across/checks",
    "All tenants: MFA (rollup)": "/across/mfa",
    "All tenants: Licences (rollup)": "/across/licences",
    "All tenants: MFA (every row)": "/across/mfa/rows/{table}",
    "What changed (30 days)": "/changes",
    "What changed (90 days)": "/changes?days=90",
    "Projects": "/projects",
    "Coming up": "/upcoming",
    "Month-end queue": "/reporting/{period}",
    "Search: a name": "/search?q=adele",
    "Search: an address": "/search?q={ip}",
    "Tenant overview": "/tenants/{t}",
    "Tenant findings": "/tenants/{t}/findings",
    "Tenant changes": "/tenants/{t}/changes",
    "Tenant monthly review": "/tenants/{t}/report",
    "Tenant evidence": "/tenants/{t}/evidence",
    "Tenant report: MFA": "/tenants/{t}/data/mfa",
    "Tenant report: All users": "/tenants/{t}/data/all-users",
    "Tenant users": "/tenants/{t}/users",
    "One person": "/tenants/{t}/users/{u}",
}
DOWNLOADS = {
    "All tenants: Monthly review PDF": "/across/review.pdf",
    "All tenants: Monthly review XLSX": "/across/review.xlsx",
    "All tenants: MFA every row XLSX": "/across/mfa/rows/{table}.xlsx",
    "All tenants: MFA every row PDF": "/across/mfa/rows/{table}.pdf",
    "All tenants: Checks XLSX": "/across/checks.xlsx",
    "What changed PDF": "/changes.pdf",
    "Tenant changes PDF": "/tenants/{t}/changes.pdf",
    "Tenant monthly review PDF": "/tenants/{t}/report.pdf",
    "Tenant All users XLSX": "/tenants/{t}/data/all-users.xlsx",
}


def _portal(data_dir: Path):
    from werkzeug.security import generate_password_hash

    from officesentry.config import load_config
    from officesentry.db import connect, utcnow
    from officesentry.web import create_app

    cfg = load_config(
        data_dir=data_dir, testing=True, secure_cookies=False, require_mfa="off", extra={"disable_csrf": True}
    )
    db = connect(cfg.db_path)
    db.execute(
        "INSERT OR IGNORE INTO users (username, password_hash, role, all_tenants, created_at)"
        " VALUES (?, ?, 'admin', 1, ?)",
        (USERNAME, generate_password_hash(PASSWORD, method="pbkdf2:sha256:1"), utcnow()),
    )
    db.close()
    app = create_app(cfg)
    client = app.test_client()
    resp = client.post("/login", data={"username": USERNAME, "password": PASSWORD})
    assert resp.status_code == 302 and "/login" not in resp.headers.get("Location", ""), "couldn't sign in"
    return cfg, app, client


def _placeholders(app, conn) -> dict:
    """The IDs the page list needs, from the first tenant."""
    from officesentry import month_end
    from officesentry.reports import data_reports, views
    from officesentry.snapshot import TenantSnapshot

    tenant = conn.execute("SELECT * FROM tenants ORDER BY id LIMIT 1").fetchone()
    snap = TenantSnapshot(conn, tenant)
    users = snap.objects("users", "user") or [{"_id": "none"}]
    ip = "203.0.113."
    for loc in snap.objects("sign_ins", "sign_in_location") or []:
        if loc.get("ips"):
            ip = loc["ips"][0]
            break
    with app.test_request_context():
        table = views.table_slugs(data_reports.build(conn, tenant, "mfa", snap=snap))[0]
    cfg = app.extensions["officesentry.config"]
    period = month_end.current_period(cfg.timezone)
    return {"t": tenant["id"], "u": users[0]["_id"], "table": table, "ip": ip, "period": period}


def time_pages(client, pages: dict, fill: dict, into: dict[str, dict] | None = None) -> dict[str, dict]:
    """Open each page once, adding the time to ``into`` (label -> {path, status, times, bytes})."""
    out = into if into is not None else {}
    for label, path in pages.items():
        url = path.format(**fill)
        start = time.monotonic()
        resp = client.get(url)
        took = time.monotonic() - start
        entry = out.setdefault(label, {"path": url, "status": None, "times": [], "bytes": 0})
        entry["times"].append(took)
        entry["status"], entry["bytes"] = resp.status_code, len(resp.data)
        print(f"  {label:38} {took:7.2f} s  ({resp.status_code}, {len(resp.data) / 1e3:.0f} kB)", flush=True)
    return out


def time_jobs(cfg, conn, db_path: Path) -> dict[str, dict]:
    """The worker's own work on this data, each as {seconds, detail}."""
    from officesentry import backups, findings, retention
    from officesentry.reports import precomputed
    from officesentry.reports.monthly_review import rollup_row

    tenants = conn.execute("SELECT * FROM tenants WHERE archived_at IS NULL ORDER BY id").fetchall()
    out = {}

    def job(label: str, fn, detail: str = "") -> None:
        start = time.monotonic()
        result = fn()
        took = time.monotonic() - start
        out[label] = {"seconds": took, "detail": detail or (str(result) if result is not None else "")}
        print(f"  {label:38} {took:7.1f} s  {out[label]['detail']}", flush=True)

    def rebuild_rows() -> None:
        conn.execute("DELETE FROM rollup_cache")
        for t in tenants:
            rollup_row(conn, t, cached=True)
            precomputed.refresh(conn, t, rebuild=True)

    def sync_all() -> int:
        return sum(findings.sync(conn, t)["new"] for t in tenants)

    def backup() -> str:
        dest, _ = backups.backup(db_path, db_path.parent / "backups", keep=1)
        return f"{dest.stat().st_size / 1e9:.2f} GB"

    # In the worker's order: a collection ends with its findings sync, then the precompute task.
    job("Findings sync, every tenant", sync_all, f"{len(tenants)} tenants")
    job("Daily rebuild of precomputed rows", rebuild_rows, f"{len(tenants)} tenants")
    job("Retention pass", lambda: sum(retention.apply(conn, cfg).values()), "rows removed: see the worker log")
    job("Compressed backup", backup)
    return out


def markdown(pages: dict, downloads: dict, jobs: dict, size_gb: float, tenants: int) -> str:
    lines = [f"Database: {size_gb:.2f} GB, {tenants} tenants.", ""]
    lines += ["| Page | First visit | Normal visit |", "|---|---|---|"]  # normal: after the worker's rebuild
    for label, r in pages.items():
        lines.append(f"| {label} | {r['times'][0]:.2f} s | {r['times'][-1]:.2f} s |")
    lines += ["", "| Download | First | Again | Size |", "|---|---|---|---|"]
    for label, r in downloads.items():
        lines.append(f"| {label} | {r['times'][0]:.2f} s | {r['times'][-1]:.2f} s | {r['bytes'] / 1e6:.1f} MB |")
    lines += ["", "| Worker job | Time | |", "|---|---|---|"]
    for label, r in jobs.items():
        lines.append(f"| {label} | {r['seconds']:.1f} s | {r['detail']} |")
    return "\n".join(lines)


def compare(before: dict, after: dict) -> str:
    """Before and after, side by side, for the write-up."""
    lines = ["| Page | Before (first / normal) | After (first / normal) |", "|---|---|---|"]
    for section in ("pages", "downloads"):
        for label, r in after[section].items():
            b = before[section].get(label)
            was = f"{b['times'][0]:.2f} s / {b['times'][-1]:.2f} s" if b else "–"
            lines.append(f"| {label} | {was} | {r['times'][0]:.2f} s / {r['times'][-1]:.2f} s |")
    lines += ["", "| Worker job | Before | After |", "|---|---|---|"]
    for label, r in after["jobs"].items():
        b = before["jobs"].get(label)
        was = f"{b['seconds']:.1f} s" if b else "–"
        lines.append(f"| {label} | {was} | {r['seconds']:.1f} s |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, help="a data directory made by scale_data.py")
    parser.add_argument("--json", type=Path, help="write the measurements here")
    parser.add_argument("--visits", type=int, default=2, help="how often to open each page (default 2)")
    parser.add_argument("--no-jobs", action="store_true", help="pages only")
    parser.add_argument("--only", help="only pages and downloads whose label contains this")
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("BEFORE", "AFTER"), help="two --json files")
    args = parser.parse_args()
    if args.compare:
        before, after = (json.loads(p.read_text(encoding="utf-8")) for p in args.compare)
        print(compare(before, after))
        return
    if not args.data_dir:
        parser.error("--data-dir is required")

    cfg, app, client = _portal(args.data_dir)
    from officesentry.db import connect

    conn = connect(cfg.db_path)
    fill = _placeholders(app, conn)
    tenants = conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
    pick = lambda d: {k: v for k, v in d.items() if not args.only or args.only.lower() in k.lower()}  # noqa: E731
    pages: dict[str, dict] = {}
    downloads: dict[str, dict] = {}
    print(f"First visit ({tenants} tenants, nothing precomputed yet):", flush=True)
    time_pages(client, pick(PAGES), fill, pages)
    time_pages(client, pick(DOWNLOADS), fill, downloads)
    jobs = {}
    if not args.no_jobs:
        print("Worker jobs:", flush=True)
        jobs = time_jobs(cfg, conn, cfg.db_path)
    for n in range(1, args.visits):
        print(f"Visit {n + 1} (after the worker's daily rebuild):", flush=True)
        time_pages(client, pick(PAGES), fill, pages)
        time_pages(client, pick(DOWNLOADS), fill, downloads)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size_gb = cfg.db_path.stat().st_size / 1e9
    conn.close()
    result = {
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tenants": tenants,
        "size_gb": size_gb,
        "pages": pages,
        "downloads": downloads,
        "jobs": jobs,
    }
    if args.json:
        args.json.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print()
    print(markdown(pages, downloads, jobs, size_gb, tenants))
    failed = [r["path"] for r in {**pages, **downloads}.values() if r["status"] != 200]
    if failed:
        sys.exit(f"Not 200: {', '.join(failed)}")


if __name__ == "__main__":
    main()
