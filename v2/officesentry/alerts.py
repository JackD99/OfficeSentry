"""Alerts to Microsoft Teams, Slack or any webhook.

Sent by the worker when:
  * a sync opens findings (new or reopened) at or above the chosen severity,
    grouped into one message per tenant per sync (the worker syncs once a
    tenant has no more runs queued, so normally once per nightly collection);
  * a collector run fails after its previous run succeeded (state changes only,
    so a tenant that keeps failing doesn't alert every night);
  * optionally, a collector that was failing works again.

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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import requests

from . import outbound
from .checks import RULES, SEVERITIES, SEVERITY_RANK
from .collectors import REGISTRY
from .crypto import Vault
from .db import audit
from .store import log_event
from .text import plural

log = logging.getLogger(__name__)

FORMATS = {"teams": "Microsoft Teams (Workflows)", "slack": "Slack", "generic": "Generic JSON webhook"}
ALERT_SEVERITIES = ("critical", "high", "medium", "low")
DEFAULTS = {"alerts.enabled": "0", "alerts.format": "teams", "alerts.min_severity": "high",
            "alerts.on_failures": "1", "alerts.on_recovery": "0", "alerts.url_enc": "", "alerts.secret_enc": "",
            # Problems with Office Sentry itself (system_alerts.py), by email to the team contacts and here.
            "alerts.system": "1"}
# Settings encrypted with the vault; rotate-keys re-encrypts them (__main__.ENCRYPTED_SETTINGS).
ENCRYPTED_KEYS = ("alerts.url_enc", "alerts.secret_enc")
MAX_ITEMS = 10          # findings listed in a Teams or Slack message
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


@dataclass
class Message:
    event: str  # findings.new | collection.failed | collection.recovered | certificate.expiry | system.* | test
    title: str
    text: str
    link: str
    tenant: dict | None = None
    items: list[Item] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


# -- Settings ----------------------------------------------------------------


def settings(conn) -> dict:
    keys = tuple(DEFAULTS)
    rows = conn.execute(f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(keys))})", keys)
    return {**DEFAULTS, **{r["key"]: r["value"] for r in rows}}


def save_settings(conn, values: dict) -> None:
    for key, value in values.items():
        if key not in DEFAULTS:
            raise KeyError(key)
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


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
        raise AlertError(f"{host} resolves to an internal address ({ip}), so it was not called. "
                         f"Set {ALLOW_PRIVATE_ENV}=1 to allow internal webhooks.") from None


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post(url: str, payload: dict, secret: str | None = None) -> None:
    address = check_destination(url)
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "OfficeSentry-Alerts"}
    if secret:
        headers["X-OfficeSentry-Signature"] = sign(secret, body)
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


def _deliver(conn, alerting: Alerting, s: dict, message: Message, run_id: int | None = None) -> bool:
    """Send and record the outcome. Never raises."""
    tenant_id = message.tenant["id"] if message.tenant else None
    what = f"{message.event} via {s.get('alerts.format')}"
    try:
        send(conn, alerting.vault, message, s)
    except AlertError as exc:
        outcome, ok = ("alert_failed", f"{what}: {exc}"), False
    except Exception as exc:  # a bug here must never stop collections
        log.exception("Sending an alert failed")
        outcome, ok = ("alert_failed", f"{what}: {type(exc).__name__}"), False
    else:
        found = f", {plural(len(message.items), 'finding')}" if message.items else ""
        outcome, ok = ("alert_sent", what + found), True
    try:
        audit(conn, None, outcome[0], tenant_id, outcome[1][:500])
        if run_id is not None:
            log_event(conn, run_id, "info" if ok else "warning",
                      ("Alert sent" if ok else "Alert not sent: " + outcome[1])[:500])
    except Exception:
        log.exception("Recording an alert outcome failed")
    return ok


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
        (tenant_id, *ids)).fetchall()
    items = []
    for row in rows:
        if SEVERITY_RANK.get(row["severity"], len(SEVERITIES)) > limit:
            continue
        if row["accepted_at"] and (row["accepted_until"] is None or row["accepted_until"] > now):
            continue
        items.append(Item(row["severity"], titles.get(row["rule_key"], row["rule_key"]), row["subject"],
                          row["detail"], {"id": row["id"], "check": row["rule_key"], "first_seen": row["first_seen"],
                                          "reopened": row["reopened_at"] is not None}))
    items.sort(key=lambda i: (SEVERITY_RANK.get(i.severity, 9), i.title.lower(), i.subject.lower()))
    return items


def after_sync(conn, alerting: Alerting | None, tenant, stats: dict, run_id: int | None = None) -> bool | None:
    """Alert on findings opened by one ``findings.sync``: the webhook, then any alert emails
    (``delivery.after_sync``). Returns whether a webhook message was sent, None if not needed."""
    if alerting is None or tenant is None:
        return None
    sent = _webhook_after_sync(conn, alerting, tenant, stats, run_id)
    if alerting.app is not None and stats.get("opened"):
        try:
            from . import delivery  # imports this module
            delivery.after_sync(alerting.app, conn, alerting.vault, tenant, stats["opened"], run_id)
        except Exception:
            log.exception("Sending alert emails failed")
    return sent


def _webhook_after_sync(conn, alerting: Alerting, tenant, stats: dict, run_id: int | None) -> bool | None:
    try:
        s = settings(conn)
        if s["alerts.enabled"] != "1" or not s["alerts.url_enc"]:
            return None
        items = opened_items(conn, tenant["id"], stats.get("opened") or (), s["alerts.min_severity"])
        if not items:
            return None
        n = len(items)
        message = Message(
            event="findings.new",
            title=f"{tenant['name']}: {n} new finding{'s' if n != 1 else ''}",
            text=f"Office Sentry found {n} new or returning issue{'s' if n != 1 else ''} "
                 f"rated {s['alerts.min_severity']} or worse.",
            link=tenant_link(alerting.base_url, tenant), tenant=dict(tenant), items=items)
        return _deliver(conn, alerting, s, message, run_id)
    except Exception:
        log.exception("Preparing a findings alert failed")
        return False


def after_run(conn, alerting: Alerting | None, run_id: int, status: str) -> bool | None:
    """Alert when a collector starts failing (or, if chosen, recovers). Returns whether a message was sent."""
    try:
        if alerting is None:
            return None
        s = settings(conn)
        if s["alerts.enabled"] != "1" or not s["alerts.url_enc"] or s["alerts.on_failures"] != "1":
            return None
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            return None
        previous = conn.execute(
            "SELECT status FROM runs WHERE tenant_id = ? AND collector = ? AND id < ?"
            " AND status IN ('succeeded', 'partial', 'failed') ORDER BY id DESC LIMIT 1",
            (run["tenant_id"], run["collector"], run_id)).fetchone()
        if previous is None:
            return None
        failed, was_failed = status == "failed", previous["status"] == "failed"
        if failed == was_failed or (not failed and s["alerts.on_recovery"] != "1"):
            return None
        tenant = conn.execute("SELECT * FROM tenants WHERE id = ?", (run["tenant_id"],)).fetchone()
        if tenant is None:
            return None
        cls = REGISTRY.get(run["collector"])
        name = cls.title if cls else run["collector"]
        link = tenant_link(alerting.base_url, tenant)
        if failed:
            error = (run["error"] or "no details").splitlines()[0][:300]
            message = Message("collection.failed", f"{tenant['name']}: {name} collection failed",
                              f"{name} worked last time but failed now: {error}", link, dict(tenant),
                              extra={"collector": run["collector"], "run_id": run_id, "error": error})
        else:
            message = Message("collection.recovered", f"{tenant['name']}: {name} collection working again",
                              f"{name} collected successfully after failing.", link, dict(tenant),
                              extra={"collector": run["collector"], "run_id": run_id})
        return _deliver(conn, alerting, s, message, run_id)
    except Exception:
        log.exception("Preparing a collection alert failed")
        return False


def test_message(base_url: str) -> Message:
    return Message("test", "Office Sentry test alert", "Alerts from Office Sentry are working.", base_url.rstrip("/"))


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
    # Check titles describe the passing state ("Every admin has MFA registered").
    return f"{_clean(i.subject, 150)}" + (f": {_clean(i.detail, 200)}" if i.detail else "") \
        + f" (check: {_clean(i.title, 120)})"


def build_payload(fmt: str, message: Message) -> dict:
    return {"teams": teams_payload, "slack": slack_payload}.get(fmt, generic_payload)(message)


def teams_payload(message: Message) -> dict:
    body = [
        {"type": "TextBlock", "text": _teams_text(message.title), "weight": "Bolder", "size": "Medium", "wrap": True},
        {"type": "TextBlock", "text": _teams_text(message.text, 600), "wrap": True},
    ]
    shown = message.items[:MAX_ITEMS]
    if shown:
        body.append({"type": "FactSet", "facts": [
            {"title": i.severity.capitalize(), "value": _teams_text(_line(i))} for i in shown]})
    if more := _more(message, len(shown)):
        body.append({"type": "TextBlock", "text": more.capitalize() + ".", "isSubtle": True, "wrap": True})
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
        "actions": [{"type": "Action.OpenUrl", "title": "Open in Office Sentry", "url": message.link}],
    }
    return {"type": "message",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
                             "content": card}]}


def slack_payload(message: Message) -> dict:
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": _clean(message.title, 150)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _slack_text(message.text, 600)}},
    ]
    shown = message.items[:MAX_ITEMS]
    if shown:
        lines = [f"• *{i.severity.capitalize()}* {_slack_text(_line(i))}" for i in shown]
        if more := _more(message, len(shown)):
            lines.append(f"_{more}_")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}})
    blocks.append({"type": "actions", "elements": [
        {"type": "button", "text": {"type": "plain_text", "text": "Open in Office Sentry"}, "url": message.link}]})
    fallback = _slack_text(message.title) + (f": {plural(len(message.items), 'finding')}" if message.items else "")
    return {"text": fallback, "blocks": blocks}


def generic_payload(message: Message) -> dict:
    shown = message.items[:MAX_GENERIC_ITEMS]
    out = {
        "event": message.event,
        "title": message.title,
        "text": message.text,
        "url": message.link,
        "sent_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tenant": ({"id": message.tenant["id"], "name": message.tenant["name"],
                    "tenant_ref": message.tenant.get("tenant_ref"), "url": message.link}
                   if message.tenant else None),
        "findings": [{"severity": i.severity, "check_title": i.title, "subject": i.subject, "detail": i.detail,
                      **i.data} for i in shown],
        "findings_total": len(message.items),
        "findings_more": max(0, len(message.items) - len(shown)),
    }
    out.update(message.extra)
    return out
