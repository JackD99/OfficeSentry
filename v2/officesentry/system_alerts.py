"""System alerts: tell the MSP's team when Office Sentry itself needs attention.

Sent through the channels Office Sentry already uses: an email to the team contacts
(when email delivery is set up) and the Teams, Slack or webhook alert (when it's on),
for these problems found by ``system_health``:

* **worker**: the background worker has stopped (down for ``WORKER_GRACE``, so a restart
  during an update doesn't alert). The worker can't report its own absence, so the
  portal watches it: ``start_watchdog`` runs a thread in each web process;
* **backup**: a backup job's last attempt failed, or one is overdue (26 hours for the
  nightly copies, 8 days for the weekly restore check), once backups are set up at all;
* **disk**: the data folder's disk is over 85% full (cleared under 80%, so it doesn't
  flap around the line);
* **collection**: most tenants failed the last scheduled collection, or nothing has
  been collected for longer than the schedule allows while the worker runs;
* **erase**: what a deleted tenant left on disk couldn't be erased (``lifecycle.erase``).

The worker checks the last four once a minute (the ``system_alerts`` periodic job).
Certificate expiry has its own warnings at 60, 30 and 7 days (``cert_lifecycle``).

Each problem alerts once when it starts, again every ``REMIND`` while it lasts, and once
when it clears. An alert that reached none of the channels set up (the mail server or the
webhook refused it) isn't counted as sent: it's tried again after ``RETRY``. Its state is a
``settings`` row (``system_alert.<key>``) changed in a write transaction before anything is
sent, so two web processes never both send.
The admin turns these on or off with ``alerts.system`` on the Alerts page.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import alerts, mailer, system_health
from .db import audit, connect, transaction
from .devices import parse_ts
from .schedule import periodic
from .text import duration

log = logging.getLogger(__name__)

WORKER_GRACE = timedelta(minutes=10)
REMIND = timedelta(hours=24)
RETRY = timedelta(minutes=15)  # after an alert no channel took
DISK_CLEAR = 80  # percent: a disk alert clears below this
STATE_PREFIX = "system_alert."
WORKER_KEYS = ("backup", "disk", "collection", "erase")  # checked by the worker
WEB_KEYS = ("worker",)  # checked by the portal
ISO = "%Y-%m-%dT%H:%M:%SZ"
STATUS_PATH = "/admin/status"


@dataclass
class Problem:
    key: str
    title: str  # "Office Sentry's background worker has stopped"
    text: str  # what is happening and what it stops
    fix: str  # what to do
    since: datetime | None = None  # when it started, when known better than "now"
    data: dict = field(default_factory=dict)


RESOLVED = {
    "worker": "Office Sentry's background worker is running again",
    "backup": "Office Sentry's backups are working again",
    "disk": f"Office Sentry's disk is under {DISK_CLEAR}% full again",
    "collection": "Collection is working again",
    "erase": "Deleted client data is now erased from the server",
}
LABELS = {
    "worker": "Worker stopped",
    "backup": "Backup failed",
    "disk": f"Disk over {system_health.DISK_WARN}% full",
    "collection": "Most clients failed to collect",
    "erase": "Deleted client data not erased",
}


# -- Detecting ---------------------------------------------------------------------------------


def detect(key: str, health: system_health.Health, state: dict | None, now: datetime) -> Problem | None:
    check = health.check(key)
    if check is None:
        return None
    if key == "worker":
        if check.status == system_health.OK:
            return None
        seen = parse_ts(check.data.get("last_seen"))
        text = (
            f"It last checked in {duration((now - seen).total_seconds())} ago." if seen else "It isn't checking in."
        ) + (
            " Until it runs again, no client is collected and no scheduled "
            "report, email alert or certificate warning goes out."
        )
        return Problem(
            key,
            "Office Sentry's background worker has stopped",
            text,
            system_health.WORKER_FIX,
            since=seen,
            data=check.data,
        )
    if key == "disk":
        used = check.data.get("used_percent") or 0
        if used < system_health.DISK_WARN and not (state and used >= DISK_CLEAR):
            return None
        return Problem(
            key,
            f"Office Sentry's disk is {used:.0f}% full",
            f"{check.summary}. When it fills, collections, backups and database upgrades fail.",
            system_health.DISK_FIX,
            data=check.data,
        )
    if key == "backup":
        if check.status == system_health.OK or not check.data.get("configured"):
            return None  # an install that never set up backups sees it on the page, not in a nightly alert
        return Problem(
            key,
            "Office Sentry's backup needs attention: " + check.summary[0].lower() + check.summary[1:],
            "Without a recent backup, a failed disk or a bad update loses data.",
            check.fix,
            data=check.data,
        )
    if key == "erase":
        if check.status == system_health.OK:
            return None
        return Problem(
            key,
            check.summary,
            "Until it is, the deleted client's data can still be read from the database files on the server.",
            check.fix,
            data=check.data,
        )
    if key == "collection":
        if check.status == system_health.OK or check.data.get("last_success") is None:
            return None  # nothing ever collected is onboarding, not an outage
        running = health.check("worker")
        if running is not None and running.status != system_health.OK:
            return None  # the worker alert already says why
        return Problem(
            key,
            check.summary,
            "Reports and checks show older data until collection works again.",
            check.fix,
            data=check.data,
        )
    return None


# -- State ---------------------------------------------------------------------------------------


def _load(conn, key: str) -> dict | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (STATE_PREFIX + key,)).fetchone()
    try:
        return json.loads(row["value"]) if row else None
    except ValueError:
        return None


def _save(conn, key: str, state: dict) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (STATE_PREFIX + key, json.dumps(state)),
    )


def states(conn) -> dict[str, dict]:
    """Every problem being tracked now, by key: {"since", "sent_at", "title"} (for the System status page)."""
    out = {}
    for row in conn.execute("SELECT key, value FROM settings WHERE key LIKE ?", (STATE_PREFIX + "%",)):
        try:
            out[row["key"][len(STATE_PREFIX) :]] = json.loads(row["value"])
        except ValueError:
            continue
    return out


def _decide(conn, key: str, health, now: datetime) -> tuple[str, Problem | None, dict | None] | None:
    """Update ``key``'s state in one write transaction. Returns (action, problem, old state) to send, or None."""
    stamp = now.strftime(ISO)
    with transaction(conn, immediate=True):
        state = _load(conn, key)
        problem = detect(key, health, state, now)
        if problem is None:
            if state is None:
                return None
            conn.execute("DELETE FROM settings WHERE key = ?", (STATE_PREFIX + key,))
            return ("resolved", None, state) if state.get("sent_at") else None
        if state is None:
            state = {"since": (problem.since or now).strftime(ISO), "sent_at": None}
        state["title"] = problem.title
        sent = parse_ts(state.get("sent_at"))
        since = parse_ts(state.get("since")) or now
        retry = parse_ts(state.get("retry_at"))
        action = None
        if retry and now < retry:
            pass  # the last try reached nobody; wait before the next
        elif sent is None and (key != "worker" or now - since >= WORKER_GRACE):
            action = "start"
        elif sent is not None and now - sent >= REMIND:
            action = "reminder"
        if action:
            state["previous_sent_at"] = state.get("sent_at")
            state["sent_at"] = stamp
            state.pop("retry_at", None)
        _save(conn, key, state)
        return (action, problem, state) if action else None


def update(app, conn, vault, cfg, keys, now: datetime | None = None) -> list[tuple[str, str]]:
    """Check ``keys`` and send what's due. Returns [(key, action)] for each alert decided on."""
    now = now or datetime.now(UTC)
    health = system_health.assess(conn, cfg, now, only=tuple(keys) + (("worker",) if "collection" in keys else ()))
    done = []
    for key in keys:
        try:
            decided = _decide(conn, key, health, now)
        except Exception:
            log.exception("Checking for a %s system alert failed", key)
            continue
        if decided:
            action, problem, state = decided
            if _send(app, conn, vault, cfg, key, action, problem, state, now) is False and action != "resolved":
                _not_sent(conn, key, state, now)
            done.append((key, action))
    return done


def _not_sent(conn, key: str, state: dict, now: datetime) -> None:
    """Every channel set up refused the alert: undo "sent", so it goes again after ``RETRY``."""
    try:
        with transaction(conn, immediate=True):
            current = _load(conn, key)
            if current is None or current.get("sent_at") != state.get("sent_at"):
                return  # cleared, or another process sent it since
            current["sent_at"] = current.pop("previous_sent_at", None)
            current["retry_at"] = (now + RETRY).strftime(ISO)
            _save(conn, key, current)
    except Exception:
        log.exception("Recording that a %s system alert wasn't sent failed", key)


# -- Sending -------------------------------------------------------------------------------------


def _message(cfg, key: str, action: str, problem: Problem | None, state: dict | None, now: datetime) -> alerts.Message:
    link = cfg.base_url.rstrip("/") + STATUS_PATH
    if action == "resolved":
        since = parse_ts((state or {}).get("since"))
        lasted = f" It lasted about {duration((now - since).total_seconds())}." if since else ""
        return alerts.Message(
            f"system.{key}.resolved",
            RESOLVED[key],
            f"Resolved: {(state or {}).get('title') or LABELS[key]}.{lasted}",
            link,
            extra={"system": {"problem": key, "state": "resolved"}},
        )
    title = ("Still not fixed: " if action == "reminder" else "") + problem.title
    return alerts.Message(
        f"system.{key}",
        title,
        f"{problem.text} {problem.fix}",
        link,
        extra={"system": {"problem": key, "state": action, **problem.data}},
    )


def _email(app, conn, vault, cfg, key: str, action: str, problem: Problem | None, message: alerts.Message) -> str:
    """Email the team contacts. Returns what happened, for the audit log."""
    from . import delivery

    team = [c["email"] for c in delivery.team_contacts(conn)]
    if app is None or not team or not mailer.is_configured(mailer.mail_settings(conn)):
        return "email not set up"
    look, inline = delivery.email_brand(conn)
    subject = ("Resolved: " if action == "resolved" else "Action needed: ") + message.title
    html = delivery.render(
        app,
        "emails/system.html",
        subject=subject,
        action=action,
        title=message.title,
        text=problem.text if problem else message.text,
        fix=problem.fix if problem else "",
        href=message.link,
        **look,
    )
    email = mailer.Email(
        to=team, subject=subject, html=html, inline=inline, text=f"{message.title}\n\n{message.text}\n\n{message.link}"
    )
    try:
        used, error = mailer.send(conn, vault, email), None
    except mailer.MailError as exc:
        used, error = None, str(exc)
    delivery.record(
        conn, kind="alert", email=email, subject=subject, error=error, transport=used, trigger="schedule", user=None
    )
    return f"email failed: {error}"[:200] if error else "email sent"


def _send(
    app, conn, vault, cfg, key: str, action: str, problem: Problem | None, state: dict | None, now: datetime
) -> bool | None:
    """Send one system alert by email and webhook. Never raises. Returns True when a channel took it, False
    when every channel set up failed (or sending broke), and None when there was nothing to send it through."""
    try:
        s = alerts.settings(conn)
        if s.get("alerts.system") != "1":
            return None
        message = _message(cfg, key, action, problem, state, now)
        mailed = _email(app, conn, vault, cfg, key, action, problem, message)
        outcome, tried, took = [mailed], mailed != "email not set up", mailed == "email sent"
        if s.get("alerts.enabled") == "1" and s.get("alerts.url_enc"):
            hooked = alerts._deliver(conn, alerts.Alerting(cfg.base_url, vault, app), s, message)
            outcome.append(f"webhook {'sent' if hooked else 'failed'}")
            tried, took = True, took or bool(hooked)
        if tried and not took:
            outcome.append(f"trying again in {duration(RETRY.total_seconds())}")
        audit(conn, None, "system_alert", detail=f"{LABELS[key]}, {action}: {'; '.join(outcome)}"[:500])
        return took if tried else None
    except Exception:
        log.exception("Sending a %s system alert failed", key)
        return False


@periodic("system_alerts")
def _job(ctx) -> None:
    """Registered with the worker through ``worker.JOB_MODULES``."""
    update(ctx.app, ctx.conn, ctx.vault, ctx.cfg, WORKER_KEYS, ctx.now)


# -- Watching the worker from the portal -----------------------------------------------------------

WATCH_EVERY = 60  # seconds
_watching: dict[str, int | None] = {"pid": None}
_watch_lock = threading.Lock()


def start_watchdog(app, cfg) -> bool:
    """Start this web process's worker watchdog, once per process (gunicorn forks after loading the app,
    so it starts on the first request each process serves). Returns whether it started one now."""
    if cfg.testing:
        return False
    pid = os.getpid()
    if _watching["pid"] == pid:
        return False
    with _watch_lock:
        if _watching["pid"] == pid:
            return False
        _watching["pid"] = pid
    threading.Thread(target=_watch, args=(app, cfg), name="system-watchdog", daemon=True).start()
    return True


def _watch(app, cfg) -> None:
    vault = app.extensions["officesentry.vault"]
    while True:
        time.sleep(WATCH_EVERY)
        try:
            conn = connect(cfg.db_path)
            try:
                update(app, conn, vault, cfg, WEB_KEYS)
            finally:
                conn.close()
        except Exception:  # say "database is locked": try again next minute
            log.exception("Watching the worker failed; trying again in a minute")
