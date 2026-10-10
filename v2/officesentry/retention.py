"""How long personal data is kept: the admin's retention settings and the worker's daily prune.

Settings live in the ``settings`` table under ``retention.*`` keys. A key that
isn't set means today's behaviour, so nothing changes on upgrade:

* snapshots: the last run of each of ``keep_daily`` days and ``keep_monthly``
  months per tenant and data source (defaults from ``OFFICESENTRY_KEEP_DAILY``
  and ``OFFICESENTRY_KEEP_MONTHLY``, 30 and 12);
* daily history (``state_history.py``, each object stored once per version):
  ``state_months``, 25 by default, the same as daily sign-in summaries below.
  It's backfilled from the snapshots above, so it holds no more than they did
  on the day of the upgrade and reaches 25 months as it grows. Each data
  source's latest state is always kept;
* sign-in detail (accounts, addresses, countries and cities from the sign-in
  logs): as long as the snapshot it's in, unless a shorter period is set;
* resolved findings: forever;
* run log info lines: 90 days (warnings and errors always stay);
* activity log: forever, and never less than ``AUDIT_FLOOR_DAYS``;
* issued reports (what clients were sent): forever, never less than
  ``ISSUED_FLOOR_DAYS``, and each tenant's latest file of each report is
  always kept, since month-end reporting compares against it;
* activity history (``events.py``, the monthly event files): raw sign-ins
  180 days, daily sign-in summaries 25 months and directory audit events
  7 years. These are the plan's recommended defaults (decision D18), so they
  apply from the upgrade on; a month older than its period is deleted as a
  file, and rows of a tenant that no longer exists are swept away.

Metrics hold counts only (no names or addresses), so they're kept forever.
Every prune deletes in small batches, each in its own short transaction, so
the portal and collector threads aren't held up. Connections already use
``PRAGMA secure_delete = FAST`` (``db.connect``), so deleted rows are zeroed
when that costs no extra I/O.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime, timedelta

from . import events, store
from .db import transaction
from .text import plural

log = logging.getLogger(__name__)

PREFIX = "retention."
FOREVER = "forever"
SNAPSHOTS = "snapshots"  # sign-in detail: as long as its snapshot

# Activity log entries are the record of who changed what (and of every
# sign-in); security reviews and incident investigations look back at least a
# year, so the log can't be set shorter than this.
AUDIT_FLOOR_DAYS = 365
# A client may ask for last year's report again; month-end reporting also
# reads the latest issued file. Issued reports are never deleted sooner.
ISSUED_FLOOR_DAYS = 365

# Objects that hold sign-in detail: who signed in, from which address, country and city.
# The data sources that store them.
SIGNIN_COLLECTORS = ("sign_ins", "mail_usage")
SIGNIN_KINDS = ("sign_in_status", "sign_in_account", "sign_in_location", "sign_in_country", "sign_in_app",
                "sign_in_failure", "sign_in_failure_ip", "risky_sign_in", "legacy_signin", "legacy_signin_status")

# key: (label, minimum, maximum, what "keep" means). None as a value means forever (or follow snapshots).
LIMITS = {
    "keep_daily": (1, 3650),
    "keep_monthly": (0, 1200),
    "signin_days": (30, 3650),
    "findings_days": (30, 3650),
    "run_log_days": (7, 3650),
    "audit_days": (AUDIT_FLOOR_DAYS, 36500),
    "issued_days": (ISSUED_FLOOR_DAYS, 36500),
    "history_signin_days": (30, 3650),
    "history_daily_months": (1, 120),
    "history_audit_years": (1, 100),
    "state_months": (1, 1200),
}
# Which settings may be "keep forever" (or, for sign-in detail, "as long as the snapshot").
OPEN_ENDED = {"keep_monthly", "signin_days", "findings_days", "run_log_days", "audit_days", "issued_days",
              "history_daily_months", "history_audit_years", "state_months"}
FOREVER_MONTHS = 10**6  # snapshots_to_keep's limit for "every month"


@dataclass(frozen=True)
class Policy:
    keep_daily: int = 30
    keep_monthly: int | None = 12      # None: the last run of every month, forever
    signin_days: int | None = None     # None: as long as the snapshot it's in
    findings_days: int | None = None   # None: resolved findings kept forever
    run_log_days: int | None = store.RUN_LOG_DAYS  # None: info lines kept forever
    audit_days: int | None = None      # None: activity log kept forever
    issued_days: int | None = None     # None: issued reports kept forever
    # Activity history: the plan's recommended defaults (D18). Raw sign-ins can't be kept forever.
    history_signin_days: int = 180
    history_daily_months: int | None = 25  # None: daily sign-in summaries kept forever
    history_audit_years: int | None = 7    # None: directory audit events kept forever
    state_months: int | None = 25          # daily history (state_history.py); None: kept forever


def defaults(cfg) -> Policy:
    """Today's behaviour: what applies when an admin hasn't changed anything."""
    return Policy(keep_daily=cfg.keep_daily, keep_monthly=cfg.keep_monthly)


def _parse(key: str, raw: str | None, default):
    if raw is None:
        return default
    if raw in (FOREVER, SNAPSHOTS):
        return None if key in OPEN_ENDED else default
    try:
        value = int(raw)
    except ValueError:
        return default
    low, high = LIMITS[key]
    return min(max(value, low), high)  # the floors hold even if the table was edited by hand


def load(conn, cfg) -> Policy:
    base = defaults(cfg)
    stored = {r["key"][len(PREFIX):]: r["value"] for r in conn.execute(
        "SELECT key, value FROM settings WHERE key LIKE 'retention.%'")}
    return Policy(**{f.name: _parse(f.name, stored.get(f.name), getattr(base, f.name)) for f in fields(Policy)})


def validate(form: dict) -> tuple[Policy | None, str | None]:
    """A Policy from the settings form (``<key>`` = number, ``<key>_forever`` = "1"), or an error to show."""
    values = {}
    for f in fields(Policy):
        key = f.name
        if key in OPEN_ENDED and form.get(f"{key}_forever") == "1":
            values[key] = None
            continue
        raw = (form.get(key) or "").strip()
        try:
            value = int(raw)
        except ValueError:
            return None, f"Enter a whole number for {LABELS[key].lower()}."
        low, high = LIMITS[key]
        if not low <= value <= high:
            why = " (see why below)" if key in ("audit_days", "issued_days") else ""
            return None, f"{LABELS[key]} must be between {low:,} and {high:,}{why}."
        values[key] = value
    return Policy(**values), None


def save(conn, policy: Policy) -> None:
    """Store every setting (in the caller's transaction)."""
    for key, value in asdict(policy).items():
        stored = str(value) if value is not None else (SNAPSHOTS if key == "signin_days" else FOREVER)
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (PREFIX + key, stored))


LABELS = {
    "keep_daily": "Daily snapshots",
    "keep_monthly": "Monthly snapshots",
    "signin_days": "Sign-in detail",
    "findings_days": "Resolved findings",
    "run_log_days": "Run logs",
    "audit_days": "Activity log",
    "issued_days": "Issued reports",
    "history_signin_days": "Sign-in history",
    "history_daily_months": "Daily sign-in summaries",
    "history_audit_years": "Directory audit history",
    "state_months": "Daily history",
}
UNITS = {"keep_daily": "day", "keep_monthly": "month", "history_daily_months": "month",
         "history_audit_years": "year", "state_months": "month"}


def describe(key: str, value) -> str:
    """One setting in words, for the audit log and the page ("30 days", "forever")."""
    if value is None:
        return "as long as its snapshot" if key == "signin_days" else "forever"
    return plural(value, UNITS.get(key, "day"))


def changes(old: Policy, new: Policy) -> list[str]:
    a, b = asdict(old), asdict(new)
    return [f"{LABELS[k]}: {describe(k, a[k])} → {describe(k, b[k])}" for k in a if a[k] != b[k]]


# -- Pruning ---------------------------------------------------------------------------------------


def _cutoff(days: int, now: datetime | None) -> str:
    return ((now or datetime.now(UTC)) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _months(policy: Policy) -> int:
    return FOREVER_MONTHS if policy.keep_monthly is None else policy.keep_monthly


def _delete_ids(conn, table: str, ids: list[int], batch: int) -> int:
    deleted = 0
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        with transaction(conn, immediate=True):
            deleted += conn.execute(f"DELETE FROM {table} WHERE id IN ({','.join('?' * len(chunk))})",
                                    chunk).rowcount
    return deleted


_SIGNIN_MARKS = ",".join("?" * len(SIGNIN_KINDS))
_SIGNIN_RUNS = (
    f"SELECT id FROM runs WHERE collector IN {SIGNIN_COLLECTORS} AND finished_at < ?"
    f" AND EXISTS (SELECT 1 FROM objects o WHERE o.run_id = runs.id AND o.kind IN ({_SIGNIN_MARKS})) ORDER BY id")


def prune_signin_detail(conn, days: int | None, batch: int = 100, now: datetime | None = None) -> int:
    """Delete sign-in detail from runs that finished more than ``days`` ago. Returns objects deleted."""
    if days is None:
        return 0
    runs = [r["id"] for r in conn.execute(_SIGNIN_RUNS, (_cutoff(days, now), *SIGNIN_KINDS))]
    deleted = 0
    for i in range(0, len(runs), batch):
        chunk = runs[i:i + batch]
        where = f"run_id IN ({','.join('?' * len(chunk))}) AND kind IN ({_SIGNIN_MARKS})"
        with transaction(conn, immediate=True):
            deleted += conn.execute(f"SELECT COALESCE(SUM(items), 0) FROM objects WHERE {where}",
                                    (*chunk, *SIGNIN_KINDS)).fetchone()[0]
            conn.execute(f"DELETE FROM objects WHERE {where}", (*chunk, *SIGNIN_KINDS))
    return deleted


def _signin_count(conn, days: int | None, now: datetime | None) -> int:
    if days is None:
        return 0
    return conn.execute(
        f"SELECT COALESCE(SUM(items), 0) FROM objects WHERE kind IN ({_SIGNIN_MARKS}) AND run_id IN ({_SIGNIN_RUNS})",
        (*SIGNIN_KINDS, _cutoff(days, now), *SIGNIN_KINDS)).fetchone()[0]


# Each tenant's earliest findings are kept: alert emails leave out a tenant's first scan by
# comparing with MIN(first_seen) (delivery.alert_items), so that marker mustn't move. Accepted
# risks are kept too: findings.sync carries an acceptance over when the finding reopens, so
# deleting one would bring it back unaccepted and alert on it.
_FINDINGS = (
    "SELECT id FROM findings WHERE resolved_at IS NOT NULL AND resolved_at < ? AND accepted_at IS NULL"
    " AND first_seen > (SELECT MIN(f.first_seen) FROM findings f WHERE f.tenant_id = findings.tenant_id)"
    " ORDER BY id")


def prune_findings(conn, days: int | None, batch: int = 500, now: datetime | None = None) -> int:
    """Delete findings resolved more than ``days`` ago. Open and accepted findings always stay."""
    if days is None:
        return 0
    return _delete_ids(conn, "findings", [r["id"] for r in conn.execute(_FINDINGS, (_cutoff(days, now),))], batch)


def prune_audit_log(conn, days: int | None, batch: int = 1000, now: datetime | None = None) -> int:
    """Delete activity log entries older than ``days`` (never fewer than AUDIT_FLOOR_DAYS)."""
    if days is None:
        return 0
    cutoff = _cutoff(max(days, AUDIT_FLOOR_DAYS), now)
    deleted = 0
    while True:
        with transaction(conn, immediate=True):
            n = conn.execute("DELETE FROM audit_log WHERE id IN (SELECT id FROM audit_log WHERE ts < ? LIMIT ?)",
                             (cutoff, batch)).rowcount
        deleted += n
        if n < batch:
            return deleted


# Every file of each tenant's latest send of each report is never deleted: month-end reporting
# reads it ("new findings since the last report", re-issuing the last approved files). One send
# stores its PDF and XLSX under the same kind and issued_at, so match on the send, not one row.
_ISSUED = (
    "SELECT id FROM issued_reports r WHERE issued_at < ?"
    " AND issued_at < (SELECT MAX(l.issued_at) FROM issued_reports l"
    " WHERE l.tenant_id = r.tenant_id AND l.kind = r.kind) ORDER BY id")


def prune_issued(conn, days: int | None, batch: int = 50, now: datetime | None = None) -> int:
    """Delete issued reports older than ``days`` (never fewer than ISSUED_FLOOR_DAYS)."""
    if days is None:
        return 0
    cutoff = _cutoff(max(days, ISSUED_FLOOR_DAYS), now)
    return _delete_ids(conn, "issued_reports", [r["id"] for r in conn.execute(_ISSUED, (cutoff,))], batch)


def _months_ago(now: datetime, months: int) -> datetime:
    index = now.year * 12 + now.month - 1 - months
    year, month = divmod(index, 12)
    return now.replace(year=year, month=month + 1, day=min(now.day, calendar.monthrange(year, month + 1)[1]))


def history_cutoffs(policy: Policy, now: datetime | None = None) -> dict[str, datetime | None]:
    """Per activity history store, the time before which its rows go (None: kept forever)."""
    now = now or datetime.now(UTC)
    return {
        "history_signin_days": now - timedelta(days=policy.history_signin_days),
        "history_daily_months": None if policy.history_daily_months is None
        else _months_ago(now, policy.history_daily_months),
        "history_audit_years": None if policy.history_audit_years is None
        else _months_ago(now, 12 * policy.history_audit_years),
    }


HISTORY_STORES = {"history_signin_days": "signins", "history_daily_months": "daily",
                  "history_audit_years": "audits"}


def prune_state(conn, months: int | None, now: datetime | None = None, dry_run: bool = False) -> int:
    """Forget (or count) the daily history from more than ``months`` ago. Returns recorded runs removed."""
    from . import state_history

    if months is None:
        return 0
    before = _months_ago(now or datetime.now(UTC), months).strftime("%Y-%m-%dT%H:%M:%SZ")
    return state_history.prune(conn, before, dry_run=dry_run)


def prune_history(conn, policy: Policy, now: datetime | None = None, dry_run: bool = False) -> dict[str, int]:
    """Delete (or count) activity history older than its period, per setting."""
    out = {}
    for key, cutoff in history_cutoffs(policy, now).items():
        out[key] = 0 if cutoff is None else events.prune(conn, HISTORY_STORES[key], cutoff, dry_run=dry_run)
    return out


def dry_run(conn, policy: Policy, tz: str = "UTC", now: datetime | None = None) -> dict[str, int]:
    """Roughly what the next daily prune would delete under ``policy``, per setting. Deletes nothing."""
    snapshots = len(store.snapshots_to_prune(conn, policy.keep_daily, _months(policy), tz))
    count = {
        "snapshots": snapshots,
        "signin_days": _signin_count(conn, policy.signin_days, now),
        "findings_days": 0, "audit_days": 0, "issued_days": 0,
        "state_months": prune_state(conn, policy.state_months, now, dry_run=True),
        "run_log_days": 0 if policy.run_log_days is None else store.run_logs_to_prune(conn, policy.run_log_days, now),
        **prune_history(conn, policy, now, dry_run=True),
    }
    if policy.findings_days is not None:
        count["findings_days"] = conn.execute(f"SELECT COUNT(*) FROM ({_FINDINGS})",
                                              (_cutoff(policy.findings_days, now),)).fetchone()[0]
    if policy.audit_days is not None:
        count["audit_days"] = conn.execute("SELECT COUNT(*) FROM audit_log WHERE ts < ?", (
            _cutoff(max(policy.audit_days, AUDIT_FLOOR_DAYS), now),)).fetchone()[0]
    if policy.issued_days is not None:
        count["issued_days"] = conn.execute(f"SELECT COUNT(*) FROM ({_ISSUED})", (
            _cutoff(max(policy.issued_days, ISSUED_FLOOR_DAYS), now),)).fetchone()[0]
    return count


def apply(conn, cfg, now: datetime | None = None) -> dict[str, int]:
    """The worker's daily prune under the stored policy. Returns what each step removed."""
    policy = load(conn, cfg)
    done = {"snapshots": store.prune_snapshots(conn, policy.keep_daily, _months(policy), cfg.timezone)}
    if done["snapshots"]:
        log.info("Retention: removed the stored objects of %s", plural(done["snapshots"], "older run"))
    steps = (
        ("state_months", lambda: prune_state(conn, policy.state_months, now), "collection from the daily history"),
        ("signin_days", lambda: prune_signin_detail(conn, policy.signin_days, now=now), "sign-in detail row"),
        ("findings_days", lambda: prune_findings(conn, policy.findings_days, now=now), "resolved finding"),
        ("run_log_days", lambda: 0 if policy.run_log_days is None
         else store.prune_run_logs(conn, policy.run_log_days, now=now), "old run log line"),
        ("audit_days", lambda: prune_audit_log(conn, policy.audit_days, now=now), "old activity log entry"),
        ("issued_days", lambda: prune_issued(conn, policy.issued_days, now=now), "old issued report"),
    )
    for key, step, noun in steps:
        done[key] = step()
        if done[key]:
            log.info("Retention: removed %s", plural(done[key], noun))
    for key, n in prune_history(conn, policy, now).items():
        done[key] = n
        if n:
            log.info("Retention: removed %s from the activity history (%s)", plural(n, "row"), LABELS[key].lower())
    done["history_orphans"] = events.sweep(conn)
    if done["history_orphans"]:
        log.info("Retention: removed %s of deleted tenants from the activity history",
                 plural(done["history_orphans"], "row"))
    return done
