"""The app certificates' lives: expiry warnings, and replacing a certificate without a gap.

One certificate signs Office Sentry in to every client tenant of its cloud, and
it lasts two years (``certs.generate_certificate``). When it expires, collection
stops for all of them at once, so:

* ``expiring()`` grades every certificate in use (each app connection's, and the
  mail app's when it signs in with one): 60, 30 and 7 days left, then expired.
  The App connection page, the admin banner and the warning emails show it.
* Rollover keeps two certificates on a connection during the switch. ``start``
  makes the replacement (``next_*`` columns); the admin uploads its .cer to the
  app registration *next to* the old one; ``test`` signs in with it; ``switch``
  makes it the one collection uses. The old one's thumbprint stays (``old_*``)
  until the admin confirms it's gone from Entra (``forget_old``), which is also
  the "certificate leaked" path in SECURITY.md.
* The ``certificate_expiry`` periodic job sends one warning per threshold to the
  MSP's team contacts by email and to the alert destinations (``alerts.deliver``).

Nothing here writes to a client tenant. The only Microsoft call is ``test``,
which signs in and reads the organisation's name (``connection_check.check``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import alerts, mailer, sso, store
from .certs import GeneratedCert, generate_certificate
from .db import audit, transaction, utcnow
from .devices import parse_ts
from .schedule import periodic
from .text import day, plural

log = logging.getLogger(__name__)

ISO = "%Y-%m-%dT%H:%M:%SZ"
WARN_DAYS = (60, 30, 7)  # a warning when a certificate gets this close to expiring, then one when it has
RANK = {None: 0, "60": 1, "30": 2, "7": 3, "expired": 4}
RETRY = timedelta(minutes=15)  # after a warning no channel took
RETRY_PREFIX = "cert_warn_retry."  # settings: when a warning that reached nobody may be tried again


class RolloverError(Exception):
    """A rollover step can't be done now. The message says why, for the person on the page."""


def days_left(not_after: str | None, now: datetime | None = None) -> int | None:
    """Whole days until ``not_after`` (or since, negative), and never 0 once it has passed."""
    end = parse_ts(not_after)
    if end is None:
        return None
    seconds = (end - (now or datetime.now(UTC))).total_seconds()
    days = int(seconds / 86400)  # towards zero, both ways
    return min(days, -1) if seconds < 0 else days


def level(days: int | None) -> str | None:
    """The warning a certificate this many days from expiry is at: '60', '30', '7', 'expired' or None."""
    if days is None:
        return None
    if days < 0:
        return "expired"
    return next((str(w) for w in reversed(WARN_DAYS) if days < w), None)


@dataclass
class Expiry:
    kind: str  # "connection" (an app connection's certificate), "mail" (the mail app's) or "sign_in" (staff sign-in)
    id: int | None  # the app connection, for kind "connection"
    name: str
    thumbprint: str
    not_after: str
    days: int
    level: str | None
    replacing: bool = False  # a replacement certificate is ready on the connection

    @property
    def tone(self) -> str:
        return {"expired": "bad", "7": "bad", "30": "warn", "60": "warn"}.get(self.level, "ok")

    @property
    def relative(self) -> str:
        """'in 23 days', 'today' or '3 days ago'."""
        if self.days < 0:
            return f"{plural(-self.days, 'day')} ago"
        return "today" if self.days == 0 else f"in {plural(self.days, 'day')}"

    @property
    def when(self) -> str:
        """'expires in 23 days, on 1 Dec 2026' / 'expires today, 1 Dec 2026' / 'expired 3 days ago, on ...'."""
        on = day(self.not_after)
        if self.days < 0:
            return f"expired {self.relative}, on {on}"
        return f"expires today, {on}" if self.days == 0 else f"expires {self.relative}, on {on}"

    @property
    def stops(self) -> str:
        """What stops when it expires."""
        if self.kind == "mail":
            return "Email from Office Sentry"
        if self.kind == "sign_in":
            return "Signing in with Microsoft"
        return "Collection from every client"

    @property
    def consequence(self) -> str:
        return f"{self.stops} has stopped." if self.days < 0 else f"{self.stops} stops when it expires."

    @property
    def headline(self) -> str:
        what = {"mail": "The mail app's certificate", "sign_in": "The sign-in app's certificate"}.get(
            self.kind, f"The {self.name} certificate"
        )
        return f"{what} {self.when}."


def connection_expiry(c, now: datetime | None = None) -> Expiry | None:
    days = days_left(c["not_after"], now)
    if days is None:
        return None
    return Expiry(
        "connection",
        c["id"],
        c["name"],
        c["thumbprint"],
        c["not_after"],
        days,
        level(days),
        replacing=bool(c["next_thumbprint"]),
    )


def mail_expiry(settings: dict, now: datetime | None = None) -> Expiry | None:
    """The mail app's certificate, when email goes through Microsoft 365 and signs in with one."""
    if mailer.transport(settings) != "graph" or mailer.graph_auth(settings) != "certificate":
        return None
    days = days_left(settings.get("graph_cert_not_after"), now)
    if days is None:
        return None
    return Expiry(
        "mail",
        None,
        "Office Sentry mail",
        settings.get("graph_thumbprint") or "",
        settings["graph_cert_not_after"],
        days,
        level(days),
    )


def sign_in_expiry(settings: dict, now: datetime | None = None) -> Expiry | None:
    """The staff sign-in app's certificate, when Microsoft sign-in is set up (``sso.py``)."""
    if not sso.is_configured(settings):
        return None
    days = days_left(settings.get("sso_cert_not_after"), now)
    if days is None:
        return None
    return Expiry(
        "sign_in",
        None,
        settings.get("sso_app_name") or sso.DEFAULT_NAME,
        settings.get("sso_thumbprint") or "",
        settings["sso_cert_not_after"],
        days,
        level(days),
        replacing=bool(settings.get("sso_next_thumbprint")),
    )


def expiring(conn, now: datetime | None = None) -> list[Expiry]:
    """Certificates in use that are within 60 days of expiry or past it, the soonest first.

    A connection without a client ID isn't signing in to anything yet, so it isn't listed.
    """
    found = [connection_expiry(c, now) for c in store.list_connections(conn, kind=None) if c["client_id"]]
    found.append(mail_expiry(mailer.mail_settings(conn), now))
    found.append(sign_in_expiry(sso.settings(conn), now))
    return sorted((e for e in found if e and e.level), key=lambda e: e.days)


# -- Rollover ---------------------------------------------------------------------------------


def _connection(conn, conn_id: int):
    c = store.get_connection(conn, conn_id)
    if c is None:
        raise RolloverError("That app connection doesn't exist.")
    return c


def start(conn, vault, conn_id: int, user: str) -> GeneratedCert:
    """Make a replacement certificate for the connection. Collection keeps using the current one."""
    c = _connection(conn, conn_id)
    if c["next_thumbprint"]:
        raise RolloverError("A replacement certificate is already waiting. Finish switching to it, or cancel it first.")
    cert = generate_certificate(f"Office Sentry - {c['name']}"[:64])
    with transaction(conn):
        conn.execute(
            "UPDATE app_connections SET next_cert_pem = ?, next_key_enc = ?, next_thumbprint = ?,"
            " next_not_after = ?, next_created_at = ?, next_checked_at = NULL WHERE id = ?",
            (
                cert.cert_pem,
                vault.encrypt(cert.key_pem),
                cert.thumbprint,
                cert.not_after.strftime(ISO),
                utcnow(),
                conn_id,
            ),
        )
        audit(conn, user, "certificate_rollover_started", detail=f"{c['name']}: new {cert.thumbprint}")
    return cert


def test(conn, vault, conn_id: int, tenant_ref: str, user: str):
    """Sign in to ``tenant_ref`` with the replacement certificate (read-only, one Graph call).

    Returns ``connection_check.CheckResult``. A success is recorded, and is what
    ``switch`` waits for: it proves the new certificate is on the app registration.
    """
    from . import clouds
    from .connection_check import check, sign_in_check

    c = _connection(conn, conn_id)
    if not c["next_thumbprint"]:
        raise RolloverError("There's no replacement certificate to test. Start replacing the certificate first.")
    if not c["client_id"]:
        raise RolloverError("Save the app's client ID first.")
    test_with = sign_in_check if c["kind"] == store.AUDIT else check  # the add-on's app can't read Graph
    result = test_with(
        tenant_ref, c["client_id"], vault.decrypt(c["next_key_enc"]), c["next_thumbprint"], clouds.of(c).key
    )
    if result.ok:
        conn.execute("UPDATE app_connections SET next_checked_at = ? WHERE id = ?", (utcnow(), conn_id))
    audit(
        conn,
        user,
        "certificate_rollover_tested",
        detail=f"{c['name']}: {c['next_thumbprint']} {'worked' if result.ok else 'failed'}",
    )
    return result


def switch(conn, conn_id: int, user: str) -> None:
    """Collection signs in with the replacement from now on. The old certificate is remembered until removed."""
    c = _connection(conn, conn_id)
    if not c["next_thumbprint"]:
        raise RolloverError("There's no replacement certificate to switch to.")
    if not c["next_checked_at"]:
        raise RolloverError(
            "Test the new certificate first, so collection doesn't stop if it isn't on the app registration yet."
        )
    with transaction(conn):
        conn.execute(
            "UPDATE app_connections SET old_thumbprint = thumbprint, old_not_after = not_after, switched_at = ?,"
            " cert_pem = next_cert_pem, key_enc = next_key_enc, thumbprint = next_thumbprint,"
            " not_after = next_not_after, expiry_warned = NULL, next_cert_pem = NULL, next_key_enc = NULL,"
            " next_thumbprint = NULL, next_not_after = NULL, next_created_at = NULL, next_checked_at = NULL"
            " WHERE id = ?",
            (utcnow(), conn_id),
        )
        audit(conn, user, "certificate_switched", detail=f"{c['name']}: {c['thumbprint']} -> {c['next_thumbprint']}")


def cancel(conn, conn_id: int, user: str) -> None:
    """Throw the replacement away (its key with it). Remove its .cer from Entra if it was uploaded."""
    c = _connection(conn, conn_id)
    if not c["next_thumbprint"]:
        return
    with transaction(conn):
        conn.execute(
            "UPDATE app_connections SET next_cert_pem = NULL, next_key_enc = NULL, next_thumbprint = NULL,"
            " next_not_after = NULL, next_created_at = NULL, next_checked_at = NULL WHERE id = ?",
            (conn_id,),
        )
        audit(conn, user, "certificate_rollover_cancelled", detail=f"{c['name']}: {c['next_thumbprint']}")


def forget_old(conn, conn_id: int, user: str) -> None:
    """The admin removed the old certificate from the app registration in Entra."""
    c = _connection(conn, conn_id)
    if not c["old_thumbprint"]:
        return
    with transaction(conn):
        conn.execute("UPDATE app_connections SET old_thumbprint = NULL, old_not_after = NULL WHERE id = ?", (conn_id,))
        audit(conn, user, "certificate_old_removed", detail=f"{c['name']}: {c['old_thumbprint']}")


def test_tenant(conn, conn_id: int) -> str | None:
    """A client tenant to test a replacement against: one on this connection that has consented (for the audit
    log add-on's app, the tenant it was created in)."""
    c = conn.execute("SELECT kind, home_tenant FROM app_connections WHERE id = ?", (conn_id,)).fetchone()
    if c is not None and c["kind"] == store.AUDIT:
        return c["home_tenant"]
    row = conn.execute(
        "SELECT tenant_ref FROM tenants WHERE connection_id = ? AND consented_at IS NOT NULL"
        " AND archived_at IS NULL ORDER BY enabled DESC, id LIMIT 1",
        (conn_id,),
    ).fetchone()
    return row["tenant_ref"] if row else None


# -- Warnings -----------------------------------------------------------------------------------


def _warned(conn, e: Expiry) -> str | None:
    if e.kind == "connection":
        row = conn.execute("SELECT expiry_warned FROM app_connections WHERE id = ?", (e.id,)).fetchone()
        return row["expiry_warned"] if row else None
    key = "sso_cert_warned" if e.kind == "sign_in" else "graph_cert_warned"
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    thumbprint, _, warned = (row["value"] if row else "").partition(":")
    return warned if thumbprint == e.thumbprint else None  # a new certificate starts again


def _mark_warned(conn, e: Expiry) -> None:
    if e.kind == "connection":
        conn.execute("UPDATE app_connections SET expiry_warned = ? WHERE id = ?", (e.level, e.id))
    else:
        key = "sso_cert_warned" if e.kind == "sign_in" else "graph_cert_warned"
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, f"{e.thumbprint}:{e.level}"),
        )


def fix_steps(e: Expiry) -> str:
    if e.kind == "sign_in":
        return (
            "Open Settings → Microsoft sign-in in Office Sentry and press Make a new certificate. Upload its .cer to "
            "the sign-in app in Entra, then switch to it."
            if not e.replacing
            else "A replacement is ready. Open Settings → Microsoft sign-in, check its .cer is on the app in Entra and "
            "switch to it."
        )
    if e.kind == "mail":
        return (
            "Open Settings → Email delivery in Office Sentry, make a new certificate, upload its .cer to the "
            "mail app in Entra and send a test."
        )
    if e.replacing:
        return "A replacement is ready. Open App connection in Office Sentry, test it and switch to it."
    return (
        "Open App connection in Office Sentry and press Replace certificate. It walks you through adding the "
        "new one in Entra and switching over without a gap."
    )


def _href(base_url: str, e: Expiry) -> str:
    if e.kind == "sign_in":
        return base_url.rstrip("/") + "/admin/sign-in"
    return base_url.rstrip("/") + ("/admin/email" if e.kind == "mail" else f"/admin/connections#conn-{e.id}")


def _message(base_url: str, e: Expiry) -> alerts.Message:
    return alerts.Message(
        event="certificate.expiry",
        title=e.headline,
        link=_href(base_url, e),
        text=f"{e.consequence} {fix_steps(e)}",
        severity={"expired": "critical", "7": "high", "30": "medium"}.get(e.level, "low"),
        key=f"certificate:{e.kind}:{e.thumbprint}",  # each warning about one certificate shares it
        extra={
            "certificate": {
                "kind": e.kind,
                "name": e.name,
                "thumbprint": e.thumbprint,
                "not_after": e.not_after,
                "days_left": e.days,
                "level": e.level,
            }
        },
    )


def _email(app, conn, vault, e: Expiry) -> str | None:
    """Email the MSP's team contacts. Returns the error, "" when it went, or None when there was no one to send
    it to or no way to send it."""
    from . import delivery

    team = [c["email"] for c in delivery.team_contacts(conn)]
    if app is None or not team or not mailer.is_configured(mailer.mail_settings(conn)):
        return None
    if e.kind == "mail" and e.level == "expired":
        return None  # it can't send any more; the banner says so
    look, inline = delivery.email_brand(conn)
    href = _href(app.extensions["officesentry.config"].base_url, e)
    subject = f"Action needed: {e.headline.rstrip('.')}"
    html = delivery.render(app, "emails/certificate.html", subject=subject, e=e, steps=fix_steps(e), href=href, **look)
    email = mailer.Email(
        to=team,
        subject=subject,
        html=html,
        inline=inline,
        text=f"{e.headline}\n\n{e.consequence}\n\n{fix_steps(e)}\n\n{href}",
    )
    try:
        used, error = mailer.send(conn, vault, email), None
    except mailer.MailError as exc:
        used, error = None, str(exc)
    delivery.record(
        conn, kind="alert", email=email, subject=subject, error=error, transport=used, trigger="schedule", user=None
    )
    return error or ""


def _retry_key(e: Expiry) -> str:
    return RETRY_PREFIX + (f"connection.{e.id}" if e.kind == "connection" else f"mail.{e.thumbprint}")


def warn(app, conn, vault, base_url: str, now: datetime | None = None) -> int:
    """Send each certificate's warning once per threshold it reaches. Returns how many were sent."""
    now = now or datetime.now(UTC)
    sent = 0
    s = alerts.settings(conn)
    for e in expiring(conn, now):
        if RANK[e.level] <= RANK[_warned(conn, e)]:
            continue
        retry = conn.execute("SELECT value FROM settings WHERE key = ?", (_retry_key(e),)).fetchone()
        if retry and (at := parse_ts(retry["value"])) and now < at:
            continue  # the last try reached nobody; wait before the next
        error = _email(app, conn, vault, e)
        hooked = alerts.deliver(conn, alerts.Alerting(base_url, vault, app), _message(base_url, e), s=s)
        # Marked warned once a channel took it, or when none is set up (the banner is all there is). When every
        # channel set up refused it, it goes again after RETRY instead of never.
        nobody = (error is not None or hooked is not None) and error != "" and not hooked
        if nobody:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_retry_key(e), (now + RETRY).strftime(ISO)),
            )
        else:
            _mark_warned(conn, e)
            conn.execute("DELETE FROM settings WHERE key = ?", (_retry_key(e),))
        audit(
            conn,
            None,
            "certificate_expiry_warned",
            detail=f"{e.name} {e.thumbprint}: {e.level}"
            + (f"; email failed: {error}"[:300] if error else "")
            + ("" if hooked is None else f"; alert {'posted' if hooked else 'not posted'}")
            + ("; trying again in 15 minutes" if nobody else ""),
        )
        sent += 0 if nobody else 1
    return sent


@periodic("certificate_expiry")
def _expiry_job(ctx) -> None:
    """Registered with the worker through ``worker.JOB_MODULES``."""
    warn(ctx.app, ctx.conn, ctx.vault, ctx.cfg.base_url, ctx.now)
