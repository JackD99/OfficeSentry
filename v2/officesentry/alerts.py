"""Alerts to Microsoft Teams, Slack or any webhook.

Sent by the worker when:
  * a sync opens findings (new or reopened) at or above the chosen severity,
    grouped into one message per tenant per sync (the worker syncs once a
    tenant has no more runs queued, so normally once per nightly collection);
  * a collector run fails after its previous run succeeded (state changes only,
    so a tenant that keeps failing doesn't alert every night);
  * optionally, a collector that was failing works again;
  * optionally, a client's report was sent or published (``report_ready``): a link to the tenant's
    report history, never the files themselves, so only people signed in and granted the tenant can
    open them;
  * a risky change made in a client's tenant, such as someone made a Global Administrator, once each
    (``activity_alerts.py``, the kinds in ``ACTIVITY_ALERTS`` the admin ticked).

Nothing goes out about a client that is paused or archived, even one paused while its collection ran
(``alerted_tenant`` reads the tenant again).

Every alert goes out through ``deliver``, to each destination that is set up: the webhook, and any other a
module adds with ``register`` (a ticketing system, say). Each message says how bad it is (``severity``) and
carries a ``key`` that stays the same for the same problem, so a destination can update one ticket instead of
opening another; ``resolves`` marks the message that says it's over.

Settings live in the ``settings`` table. The webhook URL (which usually carries
its own access key) and the optional signing secret are encrypted with the
Vault and never shown again after saving. Delivery never raises into the
worker: every outcome is written to the audit log.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import requests

from . import issued, outbound
from .checks import RULES, SEVERITIES, SEVERITY_RANK
from .collectors import REGISTRY
from .crypto import Vault
from .db import audit
from .store import log_event
from .text import plural

log = logging.getLogger(__name__)

FORMATS = {"teams": "Microsoft Teams (Workflows)", "slack": "Slack", "generic": "Generic JSON webhook"}
ALERT_SEVERITIES = ("critical", "high", "medium", "low")
INFO = "info"  # a message's severity when nothing is wrong: a report sent, a collection working again, a test
# Changes in a client's tenant that can be alerted on (activity_alerts.py), as the settings page offers them.
ACTIVITY_ALERTS = {
    "admin_role": "Someone is given an admin role",
    "ca_off": "A Conditional Access policy is deleted, turned off or set to report-only",
    "security_defaults_off": "Security defaults are turned off",
    "admin_mfa_removed": "A sign-in method is removed from an admin",
}
DEFAULTS = {
    "alerts.enabled": "0",
    "alerts.format": "teams",
    "alerts.min_severity": "high",
    "alerts.on_failures": "1",
    "alerts.on_recovery": "0",
    "alerts.url_enc": "",
    "alerts.secret_enc": "",
    "alerts.on_reports": "0",
    # Comma-separated ACTIVITY_ALERTS keys; empty sends none.
    "alerts.activity": ",".join(ACTIVITY_ALERTS),
    # Problems with Office Sentry itself (system_alerts.py), by email to the team contacts and here.
    "alerts.system": "1",
}
# Settings encrypted with the vault; rotate-keys re-encrypts them (__main__.ENCRYPTED_SETTINGS).
ENCRYPTED_KEYS = ("alerts.url_enc", "alerts.secret_enc")
MAX_ITEMS = 10  # findings listed in a Teams or Slack message
MAX_GENERIC_ITEMS = 100  # findings included in a generic payload
TIMEOUT = 10
ALLOW_PRIVATE_ENV = "OFFICESENTRY_ALLOW_PRIVATE_WEBHOOKS"


class AlertError(Exception):
    """Delivery failed. The message is safe to show and log: it never contains the URL."""


@dataclass
class Alerting:
    """What the worker needs to send alerts: links back to the portal, the vault for the URL, and the
    web app for rendering alert emails (None skips email)."""

    base_url: str
    vault: Vault
    app: Any = None


@dataclass
class Item:
    severity: str
    title: str
    subject: str
    detail: str
    data: dict = field(default_factory=dict)
    label: str = ""  # a change's kind ("Admin role"), shown in its severity's place
    key: str = ""  # the same each time this item is sent: "finding:<id>", "change:<id>"


@dataclass
class Message:
    event: str  # findings.new | collection.failed | collection.recovered | report.ready | certificate.expiry
    #             | activity.changes | system.* | test
    title: str
    text: str
    link: str
    tenant: dict | None = None
    items: list[Item] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    button: str = "Open in Office Sentry"
    noun: str = "finding"  # what the items are: "finding", or "change" for activity alerts
    severity: str = ""  # one of ALERT_SEVERITIES or INFO; left empty, the worst item's (INFO with none)
    # The same each time it's about the same problem ("collection:12:users"); left empty, the event, the tenant
    # and the items' keys (or the title, with no items).
    key: str = ""
    resolves: bool = False  # the problem with the same key is over: a collection working again, a system problem fixed

    def __post_init__(self):
        if not self.severity:
            ranked = sorted((i.severity for i in self.items), key=lambda v: SEVERITY_RANK.get(v, len(SEVERITIES)))
            self.severity = ranked[0] if ranked else INFO
        if not self.key:
            parts = sorted(i.key or f"{i.title}|{i.subject}" for i in self.items) or [self.title]
            digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]
            self.key = f"{self.event}:{self.tenant['id'] if self.tenant else '-'}:{digest}"


# -- Settings ----------------------------------------------------------------


def settings(conn) -> dict:
    keys = tuple(DEFAULTS)
    rows = conn.execute(f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(keys))})", keys)
    return {**DEFAULTS, **{r["key"]: r["value"] for r in rows}}


def save_settings(conn, values: dict) -> None:
    for key, value in values.items():
        if key not in DEFAULTS:
            raise KeyError(key)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def activity_kinds(s: dict) -> set[str]:
    """The changes in a client's tenant the admin chose to be alerted about."""
    return {k for k in (s.get("alerts.activity") or "").split(",") if k in ACTIVITY_ALERTS}


def url_problem(url: str) -> str | None:
    """Syntax check for a webhook URL (the address check happens when sending)."""
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises on a bad port
    except ValueError:
        return "That isn't a valid URL."
    if parts.scheme != "https":
        return "The webhook URL must start with https://."
    if not parts.hostname:
        return "The webhook URL needs a host name."
    if parts.username or parts.password:
        return "Put credentials in the signing secret, not in the URL."
    if len(url) > 2048:
        return "The webhook URL is too long."
    return None


def masked_url(vault: Vault, s: dict) -> str | None:
    """Host only, so the URL's access key is never sent back to the browser."""
    if not s.get("alerts.url_enc"):
        return None
    try:
        host = urlsplit(vault.decrypt(s["alerts.url_enc"])).hostname or "?"
    except Exception:  # e.g. the key that encrypted it was removed
        return "saved, but it can't be decrypted (enter it again)"
    return f"https://{host}/…"


# -- Delivery ----------------------------------------------------------------


def check_destination(url: str) -> str | None:
    """Refuse non-https URLs and hosts that resolve to internal addresses (SSRF guard).

    Returns the vetted address that ``post`` then connects to (so DNS can't change in between),
    or None when internal webhooks are allowed.
    """
    if problem := url_problem(url):
        raise AlertError(problem)
    if os.environ.get(ALLOW_PRIVATE_ENV) == "1":
        return None
    parts = urlsplit(url)
    host = parts.hostname
    try:
        return outbound.public_address(host, parts.port or 443)
    except outbound.LookupFailed as exc:
        raise AlertError(f"Couldn't look up {host}.") from exc
    except outbound.NotPublic as exc:
        ip = ipaddress.ip_address(exc.address)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        raise AlertError(
            f"{host} resolves to an internal address ({ip}), so it was not called. "
            f"Set {ALLOW_PRIVATE_ENV}=1 to allow internal webhooks."
        ) from None


def key_id(secret: str) -> str:
    """Which signing secret signed a request (the start of its SHA-256), so a receiver can hold the old and
    the new secret while it changes."""
    return hashlib.sha256(secret.encode()).hexdigest()[:8]


def sign(secret: str, body: bytes, timestamp: int) -> dict[str, str]:
    """The signature headers: an HMAC-SHA256 of "<timestamp>.<body>", so a receiver that refuses old
    timestamps can't be sent a captured request again (DEPLOY.md, "Signed webhooks")."""
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return {
        "X-OfficeSentry-Timestamp": str(timestamp),
        "X-OfficeSentry-Key-Id": key_id(secret),
        "X-OfficeSentry-Signature": f"v1={mac}",
    }


def post(url: str, payload: dict, secret: str | None = None) -> None:
    address = check_destination(url)
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "OfficeSentry-Alerts"}
    if secret:
        headers |= sign(secret, body, int(time.time()))
    http, host_header = outbound.pinned_session(url, address)
    try:
        with http:
            resp = http.post(url, data=body, headers=headers | host_header, timeout=TIMEOUT, allow_redirects=False)
    except requests.Timeout as exc:
        raise AlertError(f"No answer within {TIMEOUT} seconds.") from exc
    except requests.ConnectionError as exc:
        raise AlertError("Couldn't connect to the webhook.") from exc
    except requests.RequestException as exc:
        # The exception text can include the URL and its access key: keep only the type.
        raise AlertError(f"Request failed ({type(exc).__name__}).") from exc
    if 300 <= resp.status_code < 400:
        raise AlertError(f"The webhook answered with a redirect (HTTP {resp.status_code}), which isn't followed.")
    if resp.status_code >= 400:
        raise AlertError(f"The webhook answered HTTP {resp.status_code}.")


def send(conn, vault: Vault, message: Message, s: dict | None = None) -> None:
    """Build and post one message. Raises AlertError; callers decide how to record it."""
    s = s or settings(conn)
    if not s.get("alerts.url_enc"):
        raise AlertError("No webhook URL is saved.")
    try:
        url = vault.decrypt(s["alerts.url_enc"])
        secret = vault.decrypt(s["alerts.secret_enc"]) if s.get("alerts.secret_enc") else None
    except Exception as exc:
        raise AlertError("The saved webhook can't be decrypted. Enter it again.") from exc
    post(url, build_payload(s.get("alerts.format", "teams"), message), secret)


@dataclass(frozen=True)
class Destination:
    """Somewhere alerts are posted: the webhook (``WEBHOOK``), or another a module adds with ``register``."""

    name: str  # in the audit log, "findings.new via <name>"
    ready: Callable[[Any, dict], bool]  # (conn, settings(conn)): set up and turned on
    # (conn, alerting, message, settings(conn)). Raises AlertError, whose text is safe to show, when it fails;
    # returns False to leave this message out (nothing is recorded); anything else means it took it.
    send: Callable[[Any, Alerting, Message, dict], bool | None]
    # (conn, settings(conn)): the least severe new finding it posts. A findings.new message carries every one
    # opened, and each destination gets only those at or above its own (``at_least``); None takes them all.
    min_severity: Callable[[Any, dict], str | None] | None = None


DESTINATIONS: list[Destination] = []


def register(destination: Destination) -> None:
    """Add a destination, or replace the one with the same name."""
    DESTINATIONS[:] = [d for d in DESTINATIONS if d.name != destination.name] + [destination]


def destinations(conn, s: dict | None = None) -> list[Destination]:
    """The destinations set up and turned on now."""
    s = s or settings(conn)
    out = []
    for d in DESTINATIONS:
        try:
            if d.ready(conn, s):
                out.append(d)
        except Exception:
            log.exception("Checking the %s alert destination failed", d.name)
    return out


def wanted(conn, s: dict | None = None) -> bool:
    """Whether an alert would go anywhere, so callers don't build one for nobody."""
    return bool(destinations(conn, s))


def deliver(
    conn, alerting: Alerting, message: Message, run_id: int | None = None, s: dict | None = None
) -> bool | None:
    """Post one alert to every destination set up, recording each outcome in the audit log (and the run's log).
    Every alert goes out through here. Never raises. Returns True when one took it, False when every one tried
    failed, and None when none is set up or each left it out."""
    try:
        s = s or settings(conn)
        ready = destinations(conn, s)
    except Exception:
        log.exception("Finding where to send an alert failed")
        return False
    took = None
    for d in ready:
        ok = _deliver_to(conn, alerting, s, message, d, run_id)
        if ok is not None:
            took = ok if took is None else took or ok
    return took


def _deliver_to(conn, alerting: Alerting, s: dict, message: Message, d: Destination, run_id: int | None) -> bool | None:
    """Send to one destination and record the outcome. Never raises."""
    tenant_id = message.tenant["id"] if message.tenant else None
    via = s.get("alerts.format") if d is WEBHOOK else d.name
    what = f"{message.event} via {via}"
    try:
        message = at_least(message, d.min_severity(conn, s) if d.min_severity else None)
        if message is None or d.send(conn, alerting, message, s) is False:
            return None
    except AlertError as exc:
        outcome, ok = ("alert_failed", f"{what}: {exc}"), False
    except Exception as exc:  # a bug here must never stop collections
        log.exception("Sending an alert to %s failed", d.name)
        outcome, ok = ("alert_failed", f"{what}: {type(exc).__name__}"), False
    else:
        found = f", {plural(len(message.items), message.noun)}" if message.items else ""
        outcome, ok = ("alert_sent", what + found), True
    try:
        audit(conn, None, outcome[0], tenant_id, outcome[1][:500])
        if run_id is not None:
            sent = "Alert sent" if d is WEBHOOK else f"Alert sent via {d.name}"
            log_event(
                conn, run_id, "info" if ok else "warning", (sent if ok else "Alert not sent: " + outcome[1])[:500]
            )
    except Exception:
        log.exception("Recording an alert outcome failed")
    return ok


def _webhook_ready(conn, s: dict) -> bool:
    return s.get("alerts.enabled") == "1" and bool(s.get("alerts.url_enc"))


def _webhook_send(conn, alerting: Alerting, message: Message, s: dict) -> None:
    send(conn, alerting.vault, message, s)


WEBHOOK = Destination("webhook", _webhook_ready, _webhook_send, lambda conn, s: s.get("alerts.min_severity"))
register(WEBHOOK)


# -- Events ------------------------------------------------------------------


def tenant_link(base_url: str, tenant) -> str:
    return f"{base_url.rstrip('/')}/tenants/{tenant['id']}"


def _rule_titles() -> dict[str, str]:
    return {r.key: r.title for r in RULES}


def opened_items(conn, tenant_id: int, finding_ids, min_severity: str) -> list[Item]:
    """Open, not accepted findings among ``finding_ids`` at or above ``min_severity``, worst first."""
    ids = [int(i) for i in finding_ids]
    if not ids:
        return []
    limit = SEVERITY_RANK.get(min_severity, SEVERITY_RANK["high"])
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    titles = _rule_titles()
    rows = conn.execute(
        f"SELECT * FROM findings WHERE tenant_id = ? AND resolved_at IS NULL AND id IN ({','.join('?' * len(ids))})",
        (tenant_id, *ids),
    ).fetchall()
    items = []
    for row in rows:
        if SEVERITY_RANK.get(row["severity"], len(SEVERITIES)) > limit:
            continue
        if row["accepted_at"] and (row["accepted_until"] is None or row["accepted_until"] > now):
            continue
        items.append(
            Item(
                row["severity"],
                titles.get(row["rule_key"], row["rule_key"]),
                row["subject"],
                row["detail"],
                {
                    "id": row["id"],
                    "check": row["rule_key"],
                    "first_seen": row["first_seen"],
                    "reopened": row["reopened_at"] is not None,
                },
                key=f"finding:{row['id']}",
            )
        )
    items.sort(key=lambda i: (SEVERITY_RANK.get(i.severity, 9), i.title.lower(), i.subject.lower()))
    return items


def alerted_tenant(conn, tenant_id: int):
    """The tenant as it is now, or None once it's paused, archived or deleted. A collection that was running
    when someone paused the client still finishes, and nothing is alerted or emailed about it (lifecycle.py)."""
    tenant = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    return tenant if tenant is not None and tenant["enabled"] and not tenant["archived_at"] else None


def after_sync(conn, alerting: Alerting | None, tenant, stats: dict, run_id: int | None = None) -> bool | None:
    """Alert on findings opened by one ``findings.sync``: the webhook, then any alert emails
    (``delivery.after_sync``). Returns whether a webhook message was sent, None if not needed."""
    if alerting is None or tenant is None:
        return None
    tenant = alerted_tenant(conn, tenant["id"])  # the row the run started with may be out of date
    if tenant is None:
        return None
    sent = _post_after_sync(conn, alerting, tenant, stats, run_id)
    if alerting.app is not None and stats.get("opened"):
        try:
            from . import delivery  # imports this module

            delivery.after_sync(alerting.app, conn, alerting.vault, tenant, stats["opened"], run_id)
        except Exception:
            log.exception("Sending alert emails failed")
    return sent


def _post_after_sync(conn, alerting: Alerting, tenant, stats: dict, run_id: int | None) -> bool | None:
    try:
        s = settings(conn)
        if not wanted(conn, s):
            return None
        # Every severity: each destination keeps those at or above its own (at_least).
        items = opened_items(conn, tenant["id"], stats.get("opened") or (), SEVERITIES[-1])
        if not items:
            return None
        return deliver(
            conn, alerting, findings_message(tenant_link(alerting.base_url, tenant), tenant, items), run_id, s
        )
    except Exception:
        log.exception("Preparing a findings alert failed")
        return False


def findings_message(link: str, tenant, items: list[Item], min_severity: str | None = None) -> Message:
    n = len(items)
    rated = f" rated {min_severity} or worse" if min_severity and min_severity != SEVERITIES[-1] else ""
    return Message(
        event="findings.new",
        title=f"{tenant['name']}: {n} new finding{'s' if n != 1 else ''}",
        text=f"Office Sentry found {n} new or returning issue{'s' if n != 1 else ''}{rated}.",
        link=link,
        tenant=dict(tenant),
        items=items,
    )


def at_least(message: Message, min_severity: str | None) -> Message | None:
    """A findings.new message with only the findings at or above ``min_severity``, as one destination posts it,
    or None when none are. Any other message, or no ``min_severity``, is returned as it is."""
    if message.event != "findings.new" or not min_severity:
        return message
    limit = SEVERITY_RANK.get(min_severity, SEVERITY_RANK["high"])
    kept = [i for i in message.items if SEVERITY_RANK.get(i.severity, len(SEVERITIES)) <= limit]
    return findings_message(message.link, message.tenant, kept, min_severity) if kept else None


def after_run(conn, alerting: Alerting | None, run_id: int, status: str) -> bool | None:
    """Alert when a collector starts failing (or, if chosen, recovers). Returns whether a message was sent."""
    try:
        if alerting is None:
            return None
        s = settings(conn)
        if s["alerts.on_failures"] != "1" or not wanted(conn, s):
            return None
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None or (status == "failed" and run["retry_at"]):
            return None  # it's tried again by itself (store.plan_retry): the alert waits for the last try
        # Failures that were tried again never alerted, so a run is compared with the one before them.
        previous = conn.execute(
            "SELECT status FROM runs WHERE tenant_id = ? AND collector = ? AND id < ?"
            " AND status IN ('succeeded', 'partial', 'failed') AND NOT (status = 'failed' AND retry_at IS NOT NULL)"
            " ORDER BY id DESC LIMIT 1",
            (run["tenant_id"], run["collector"], run_id),
        ).fetchone()
        if previous is None:
            return None
        failed, was_failed = status == "failed", previous["status"] == "failed"
        if failed == was_failed or (not failed and s["alerts.on_recovery"] != "1"):
            return None
        tenant = alerted_tenant(conn, run["tenant_id"])
        if tenant is None:
            return None
        cls = REGISTRY.get(run["collector"])
        name = cls.title if cls else run["collector"]
        link = tenant_link(alerting.base_url, tenant)
        key = f"collection:{tenant['id']}:{run['collector']}"  # the failure and the recovery share it
        if failed:
            error = (run["error"] or "no details").splitlines()[0][:300]
            message = Message(
                "collection.failed",
                f"{tenant['name']}: {name} collection failed",
                f"{name} worked last time but failed now: {error}",
                link,
                dict(tenant),
                extra={"collector": run["collector"], "run_id": run_id, "error": error},
                severity="medium",
                key=key,
            )
        else:
            message = Message(
                "collection.recovered",
                f"{tenant['name']}: {name} collection working again",
                f"{name} collected successfully after failing.",
                link,
                dict(tenant),
                extra={"collector": run["collector"], "run_id": run_id},
                key=key,
                resolves=True,
            )
        return deliver(conn, alerting, message, run_id, s)
    except Exception:
        log.exception("Preparing a collection alert failed")
        return False


def report_history_link(base_url: str, tenant) -> str:
    return f"{tenant_link(base_url, tenant)}/report-history"


def ready_title(tenant_name: str, label: str) -> str:
    """ "The October 2026 report for Contoso is ready" (a period label from ``issued.period_label``)."""
    if label.lower().startswith("week"):
        return f"The report for {tenant_name} for {label[:1].lower() + label[1:]} is ready"
    return f"The {label} report for {tenant_name} is ready" if label else f"A report for {tenant_name} is ready"


def report_ready_message(
    base_url: str, tenant, period: str | None, titles: list[str], *, emailed: bool = True
) -> Message:
    """A link to the tenant's report history and what's in it. Never the files: they stay behind the sign-in."""
    label = issued.period_label(period)
    how = "Emailed to the client" if emailed else "Published to the client portal"
    text = f"{how}: {', '.join(titles)}." if titles else f"{how}."
    return Message(
        "report.ready",
        ready_title(tenant["name"], label),
        text,
        report_history_link(base_url, tenant),
        dict(tenant),
        extra={"period": period, "reports": list(titles), "emailed": emailed},
        button="Open the report history",
        key=f"report.ready:{tenant['id']}:{period or '-'}",
    )


def report_ready(
    conn, alerting: Alerting | None, tenant, period: str | None, titles: list[str], *, emailed: bool = True
) -> bool | None:
    """Post that a client's report went out (or was published to the portal), when that's turned on.
    Returns whether it was posted, None if not wanted. Never raises."""
    try:
        if alerting is None or tenant is None:
            return None
        s = settings(conn)
        if s["alerts.on_reports"] != "1" or not wanted(conn, s):
            return None
        return deliver(
            conn, alerting, report_ready_message(alerting.base_url, tenant, period, titles, emailed=emailed), s=s
        )
    except Exception:
        log.exception("Preparing a report-ready alert failed")
        return False


def test_message(base_url: str) -> Message:
    return Message(
        "test", "Office Sentry test alert", "Alerts from Office Sentry are working.", base_url.rstrip("/"), key="test"
    )


def sample_report_message(base_url: str) -> Message:
    """What a report-ready post looks like, for the settings page's test button. Names no real tenant."""
    message = report_ready_message(
        base_url,
        {"id": 0, "name": "Contoso (example)", "tenant_ref": None},
        datetime.now(UTC).strftime("%Y-%m"),
        ["Monthly Security & Licensing Review", "Mail forwarding: Forwarding outside"],
    )
    message.event, message.link = "test", base_url.rstrip("/")
    return message


# -- Payloads ----------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(value, limit: int = 300) -> str:
    text = _CONTROL.sub(" ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _teams_text(value, limit: int = 300) -> str:
    # Names come from the tenant: stop them forming Markdown links in the card.
    return _clean(value, limit).replace("](", "] (")


def _slack_text(value, limit: int = 300) -> str:
    # Slack's only control characters; escaping them also stops <url|text> links.
    return _clean(value, limit).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _more(message: Message, shown: int) -> str | None:
    extra = len(message.items) - shown
    return f"and {extra} more" if extra > 0 else None


def _line(i: Item) -> str:
    if i.label:  # a change: what happened, then who made it and when
        return _clean(i.subject, 200) + (f" ({_clean(i.detail, 200)})" if i.detail else "")
    # Check titles describe the passing state ("Every admin has MFA registered").
    return (
        f"{_clean(i.subject, 150)}"
        + (f": {_clean(i.detail, 200)}" if i.detail else "")
        + f" (check: {_clean(i.title, 120)})"
    )


def _kind(i: Item) -> str:
    return i.label or i.severity.capitalize()


def build_payload(fmt: str, message: Message) -> dict:
    return {"teams": teams_payload, "slack": slack_payload}.get(fmt, generic_payload)(message)


def teams_payload(message: Message) -> dict:
    body = [
        {"type": "TextBlock", "text": _teams_text(message.title), "weight": "Bolder", "size": "Medium", "wrap": True},
        {"type": "TextBlock", "text": _teams_text(message.text, 600), "wrap": True},
    ]
    shown = message.items[:MAX_ITEMS]
    if shown:
        body.append(
            {
                "type": "FactSet",
                "facts": [{"title": _teams_text(_kind(i), 60), "value": _teams_text(_line(i))} for i in shown],
            }
        )
    if more := _more(message, len(shown)):
        body.append({"type": "TextBlock", "text": more.capitalize() + ".", "isSubtle": True, "wrap": True})
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
        "actions": [{"type": "Action.OpenUrl", "title": message.button, "url": message.link}],
    }
    return {
        "type": "message",
        "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}
        ],
    }


def slack_payload(message: Message) -> dict:
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": _clean(message.title, 150)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _slack_text(message.text, 600)}},
    ]
    shown = message.items[:MAX_ITEMS]
    if shown:
        lines = [f"• *{_slack_text(_kind(i), 60)}* {_slack_text(_line(i))}" for i in shown]
        if more := _more(message, len(shown)):
            lines.append(f"_{more}_")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}})
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": message.button}, "url": message.link}
            ],
        }
    )
    fallback = _slack_text(message.title) + (f": {plural(len(message.items), message.noun)}" if message.items else "")
    return {"text": fallback, "blocks": blocks}


def generic_payload(message: Message) -> dict:
    shown = message.items[:MAX_GENERIC_ITEMS]
    if message.noun != "finding":  # activity alerts: "changes", each as activity_alerts.item describes it
        key = message.noun + "s"
        rows = [{"severity": i.severity, "text": i.subject, **i.data} for i in shown]
    else:
        key = "findings"
        rows = [
            {"severity": i.severity, "check_title": i.title, "subject": i.subject, "detail": i.detail, **i.data}
            for i in shown
        ]
    out = {
        "event": message.event,
        "title": message.title,
        "text": message.text,
        "url": message.link,
        "severity": message.severity,
        "key": message.key,
        "resolves": message.resolves,
        "sent_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tenant": (
            {
                "id": message.tenant["id"],
                "name": message.tenant["name"],
                "tenant_ref": message.tenant.get("tenant_ref"),
                "url": message.link,
            }
            if message.tenant
            else None
        ),
        key: rows,
        f"{key}_total": len(message.items),
        f"{key}_more": max(0, len(message.items) - len(shown)),
    }
    out.update(message.extra)
    return out
