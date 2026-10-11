"""The setup wizard: create Office Sentry's app from the portal, without the command line.

On the App connection page an admin presses "Sign in to Microsoft". Office Sentry asks Microsoft for a
device code (``entra.start_device_sign_in``) and shows it with Microsoft's link; the admin signs in there,
in their own browser, as someone who can create app registrations in the MSP's own tenant. The page checks
back every few seconds (``check``), and each check asks Microsoft once whether the sign-in has finished. When
it has, that same request creates the read-only app (``register_app``) and lets the token go.

What is and isn't kept:

- The access token lives only in the memory of the one request that receives it. It's never stored,
  logged or sent to the browser, and no refresh token is asked for, so it can't be renewed.
- The device code is what's kept between checks, because the portal runs several processes and the next
  check may reach another one. It's encrypted (``setup_sign_ins.device_code_enc``, re-encrypted by
  ``rotate-keys``), only usable for the 15 minutes Microsoft allows, and erased as soon as the sign-in
  finishes, fails or is cancelled.

The wizard asks for exactly what the ``create-app`` command asks for (``entra.setup_scope``) and creates the
same app, and both finish with ``register_app``. That reuses a certificate generated earlier for the same
cloud (a connection still "Needs client ID") rather than making a second connection, which used to leave
newcomers with two connections and a consent link for the wrong one.

The same sign-in also updates an existing app (purpose ``"update"``, from the App connection page when a
release adds a batch of permissions, ``permission_batch``; the ``update-app`` command does the same):
``update_app`` adds the permissions the app doesn't ask for yet, keeps the rest, and records the batch on
the connection. Each client then approves them with its own link.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import requests

from . import clouds, entra, permission_batch, sso, store
from .certs import generate_certificate
from .db import audit

DEFAULT_NAME = "Office Sentry (read-only)"
ANY_TENANT = "organizations"  # Microsoft works out the tenant from the account that signs in
TENANT_RE = re.compile(r"^([0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}|([a-z0-9-]+\.)+[a-z]{2,})$")
# A check that started creating the app and never finished (its process died) is given up after this.
STUCK_AFTER = timedelta(minutes=5)
INTERRUPTED = (
    "Setup was interrupted while the app was being created. Before starting again, look in the "
    'Entra admin centre under App registrations: if an app named "{name}" is there with no '
    "matching connection on this page, delete it."
)
NO_ANSWER = "Microsoft didn't answer just now. Office Sentry will keep checking."

# Statuses: waiting (for the admin to sign in), working (one check is talking to Microsoft), done, failed.
ACTIVE = ("waiting", "working")
# What a sign-in is for: creating the read-only app connection, the staff sign-in app (sso.py), or adding a
# permission batch to an existing connection's app (``connection_id`` names it from the start).
PURPOSES = ("connection", "sign_in", "update")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def tenant_ref(value: str) -> str | None:
    """What to sign in to: blank means any tenant, else a domain or tenant ID. None if it's neither."""
    value = (value or "").strip().lower()
    if value in ("", ANY_TENANT):
        return ANY_TENANT
    return value if TENANT_RE.match(value) else None


def pending_connection(conn, cloud: str):
    """A connection in ``cloud`` whose certificate was generated but whose app was never saved."""
    return conn.execute(
        "SELECT * FROM app_connections WHERE client_id IS NULL AND cloud = ? ORDER BY id LIMIT 1", (cloud,)
    ).fetchone()


def ready_connection(conn, cloud: str):
    """A connection in ``cloud`` that already has its app."""
    return conn.execute(
        "SELECT * FROM app_connections WHERE client_id IS NOT NULL AND cloud = ? ORDER BY id LIMIT 1", (cloud,)
    ).fetchone()


def register_app(
    conn,
    vault,
    token: str,
    redirect_uri: str,
    name: str = DEFAULT_NAME,
    cloud: str = clouds.DEFAULT,
    session: requests.Session | None = None,
) -> tuple[int, dict]:
    """Create the multi-tenant read-only app with ``token`` and save it as an app connection.

    Reuses the certificate of a connection in the same cloud that's still waiting for its client ID, so
    there's one connection per app. Returns the connection's id and Microsoft's application object.
    Raises ``entra.SignInError`` in words for people when Microsoft refuses.
    """
    pending = pending_connection(conn, cloud)
    cert = None if pending else generate_certificate(f"Office Sentry - {name}"[:64])
    try:
        app = entra.create_app_registration(
            token,
            pending["cert_pem"] if pending else cert.cert_pem,
            redirect_uri,
            display_name=name,
            session=session,
            cloud=cloud,
        )
    except requests.RequestException as exc:
        raise entra.SignInError(entra.create_problem(exc)) from exc
    client_id = str(app["appId"]).lower()
    if pending:
        conn_id = pending["id"]
        conn.execute("UPDATE app_connections SET client_id = ? WHERE id = ?", (client_id, conn_id))
    else:
        conn_id = store.create_connection(conn, vault, name[:100], cert, client_id=client_id, cloud=cloud)
    # A new app asks for every permission, the latest batch's included.
    conn.execute(
        "UPDATE app_connections SET permissions_release = ? WHERE id = ?", (permission_batch.CURRENT.release, conn_id)
    )
    return conn_id, app


def update_app(conn, token: str, connection, actor: str, how: str, session: requests.Session | None = None) -> int:
    """Add every permission Office Sentry asks for to ``connection``'s app registration with ``token`` and record
    the batch on the connection. Returns how many permissions were added (0 when the app already asked for them
    all). Raises ``entra.SignInError`` in words for people when Microsoft refuses."""
    try:
        updated = entra.update_app_permissions(
            token, connection["client_id"], session=session, cloud=connection["cloud"]
        )
    except entra.AppNotFound as exc:
        raise entra.SignInError(
            f"The tenant you signed in to has no app registration with client ID {connection['client_id']}. Sign in "
            "to the tenant the app was created in (your own, the MSP's), as someone who can change it."
        ) from exc
    except requests.RequestException as exc:
        raise entra.SignInError(entra.update_problem(exc)) from exc
    permission_batch.mark_app(conn, connection["id"], actor, how)
    return updated.added


# -- The wizard's sign-in, one row per admin --------------------------------------------------------------


def current(conn, user_id: int, purpose: str = "connection"):
    return conn.execute(
        "SELECT * FROM setup_sign_ins WHERE user_id = ? AND purpose = ? ORDER BY id DESC LIMIT 1", (user_id, purpose)
    ).fetchone()


def start(
    conn,
    vault,
    user_id: int,
    actor: str,
    tenant: str = ANY_TENANT,
    cloud: str = clouds.DEFAULT,
    name: str = DEFAULT_NAME,
    session: requests.Session | None = None,
    now: datetime | None = None,
    purpose: str = "connection",
    connection=None,
):
    """Get a device code from Microsoft and remember it (encrypted) for this admin. Replaces any earlier one
    for the same purpose. With ``connection``, the sign-in updates that connection's app instead of creating
    one (purpose "update"). Raises ``entra.SignInError`` if Microsoft won't give one."""
    now = now or datetime.now(UTC)
    if connection is not None:
        purpose, cloud, name = "update", clouds.of(connection).key, connection["name"]
    if purpose not in PURPOSES:
        raise ValueError(purpose)
    scope = sso.setup_scope(cloud) if purpose == "sign_in" else None
    code = entra.start_device_sign_in(tenant, cloud, session=session, scope=scope)
    conn.execute("DELETE FROM setup_sign_ins WHERE user_id = ? AND purpose = ?", (user_id, purpose))
    conn.execute(
        "INSERT INTO setup_sign_ins (user_id, cloud, tenant, name, device_code_enc, user_code, verification_uri,"
        " poll_seconds, expires_at, next_poll_at, status, created_at, updated_at, purpose, connection_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting', ?, ?, ?, ?)",
        (
            user_id,
            cloud,
            tenant,
            name,
            vault.encrypt(code.device_code),
            code.user_code,
            code.verification_uri,
            max(code.interval, 1),
            _iso(now + timedelta(seconds=code.expires_in)),
            _iso(now),
            _iso(now),
            _iso(now),
            purpose,
            connection["id"] if connection is not None else None,
        ),
    )
    audit(
        conn,
        actor,
        "app_update_started" if purpose == "update" else "app_setup_started",
        detail=(cloud if tenant == ANY_TENANT else f"{cloud} {tenant}")
        + (" (sign-in app)" if purpose == "sign_in" else ""),
    )
    return current(conn, user_id, purpose)


def cancel(conn, user_id: int, purpose: str = "connection") -> None:
    """Forget this admin's sign-in, and with it the device code."""
    conn.execute("DELETE FROM setup_sign_ins WHERE user_id = ? AND purpose = ?", (user_id, purpose))


def _fail(conn, row, message: str, actor: str) -> None:
    conn.execute(
        "UPDATE setup_sign_ins SET status = 'failed', error = ?, device_code_enc = NULL, updated_at = ? WHERE id = ?",
        (message, _iso(datetime.now(UTC)), row["id"]),
    )
    audit(conn, actor, "app_setup_failed", detail=row["cloud"])


def check(
    conn,
    vault,
    user_id: int,
    actor: str,
    redirect_uri: str,
    session: requests.Session | None = None,
    now: datetime | None = None,
    purpose: str = "connection",
):
    """Take the wizard one step: ask Microsoft once whether the admin has signed in and, when they have,
    create the app. Safe to call as often as the page likes: Microsoft is asked at most once per its
    interval, and only one call at a time (across portal processes) gets to ask. Returns the row."""
    now = now or datetime.now(UTC)
    row = current(conn, user_id, purpose)
    if row is None or row["status"] not in ACTIVE:
        return row
    if row["status"] == "working":
        if _parse(row["updated_at"]) + STUCK_AFTER <= now:
            _fail(conn, row, INTERRUPTED.format(name=row["name"]), actor)
        return current(conn, user_id, purpose)
    if _parse(row["expires_at"]) <= now:
        _fail(conn, row, entra.SIGN_IN_PROBLEMS["expired_token"], actor)
        return current(conn, user_id, purpose)
    if row["next_poll_at"] and _parse(row["next_poll_at"]) > now:
        return row
    claimed = conn.execute(
        "UPDATE setup_sign_ins SET status = 'working', updated_at = ? WHERE id = ? AND status = 'waiting'",
        (_iso(now), row["id"]),
    ).rowcount
    if not claimed:
        return current(conn, user_id, purpose)  # another check got there first

    def wait(seconds: int, note: str | None = None) -> None:
        conn.execute(
            "UPDATE setup_sign_ins SET status = 'waiting', poll_seconds = ?, next_poll_at = ?, error = ?,"
            " updated_at = ? WHERE id = ?",
            (seconds, _iso(now + timedelta(seconds=max(seconds - 1, 1))), note, _iso(now), row["id"]),
        )

    try:
        poll = entra.poll_device_sign_in(
            vault.decrypt(row["device_code_enc"]), row["tenant"], row["cloud"], session=session
        )
    except entra.SignInError as exc:
        _fail(conn, row, str(exc), actor)
        return current(conn, user_id, purpose)
    except requests.RequestException:
        wait(row["poll_seconds"], NO_ANSWER)
        return current(conn, user_id, purpose)
    if poll.token is None:
        wait(row["poll_seconds"] + (5 if poll.slower else 0))
        return current(conn, user_id, purpose)

    home = entra.token_tenant(poll.token)
    if purpose == "update":
        return _finish_update(conn, row, poll.token, home, actor, user_id, session, now)
    if purpose == "sign_in":
        return _finish_sign_in_app(conn, vault, row, poll.token, redirect_uri, actor, home, now, session)
    try:
        conn_id, app = register_app(conn, vault, poll.token, redirect_uri, row["name"], row["cloud"], session=session)
    except entra.SignInError as exc:
        _fail(conn, row, str(exc), actor)
        return current(conn, user_id)
    conn.execute(
        "UPDATE setup_sign_ins SET status = 'done', device_code_enc = NULL, error = NULL, connection_id = ?,"
        " client_id = ?, home_tenant = ?, updated_at = ? WHERE id = ?",
        (conn_id, str(app["appId"]).lower(), home, _iso(now), row["id"]),
    )
    audit(conn, actor, "connection_created", detail=f"{row['name']} {app['appId']} {row['cloud']} (setup wizard)")
    return current(conn, user_id)


def _finish_update(conn, row, token: str, home: str | None, actor: str, user_id: int, session, now: datetime):
    connection = store.get_connection(conn, row["connection_id"]) if row["connection_id"] else None
    if connection is None or not connection["client_id"]:
        _fail(conn, row, "That app connection was removed, or has no client ID, so there's nothing to update.", actor)
        return current(conn, user_id, "update")
    try:
        added = update_app(conn, token, connection, actor, "update wizard", session=session)
    except entra.SignInError as exc:
        _fail(conn, row, str(exc), actor)
        return current(conn, user_id, "update")
    conn.execute(
        "UPDATE setup_sign_ins SET status = 'done', device_code_enc = NULL, error = NULL, client_id = ?,"
        " home_tenant = ?, updated_at = ? WHERE id = ?",
        (connection["client_id"], home, _iso(now), row["id"]),
    )
    audit(
        conn, actor, "app_updated", detail=f"{connection['name']} {connection['client_id']}: {added} permissions added"
    )
    return current(conn, user_id, "update")


def _finish_sign_in_app(conn, vault, row, token: str, redirect_uri: str, actor: str, home, now, session):
    """The sign-in app (sso.py), created with the wizard's token and saved as the staff sign-in settings."""
    if not home:
        _fail(conn, row, "Microsoft didn't say which tenant you signed in to. Start again.", actor)
        return current(conn, row["user_id"], "sign_in")
    cert = generate_certificate("Office Sentry sign-in", years=2)
    try:
        created = sso.create_sign_in_app(token, cert.cert_pem, redirect_uri, row["name"], row["cloud"], session=session)
    except sso.SignInError as exc:
        _fail(conn, row, str(exc), actor)
        return current(conn, row["user_id"], "sign_in")
    created.tenant = created.tenant or home
    sso.store_app(conn, vault, created, cert, row["name"], row["cloud"], actor)
    conn.execute(
        "UPDATE setup_sign_ins SET status = 'done', device_code_enc = NULL, error = NULL, client_id = ?,"
        " home_tenant = ?, updated_at = ? WHERE id = ?",
        (created.client_id, home, _iso(now), row["id"]),
    )
    audit(conn, actor, "sign_in_app_created", detail=f"{row['name']} {created.client_id} {row['cloud']} (setup wizard)")
    return current(conn, row["user_id"], "sign_in")


def seconds_left(row, now: datetime | None = None) -> int:
    return max(0, int((_parse(row["expires_at"]) - (now or datetime.now(UTC))).total_seconds()))


def prune(conn, now: datetime | None = None) -> int:
    """Daily (``worker.prune``): erase device codes nobody came back for, and forget sign-ins over a day old."""
    now = now or datetime.now(UTC)
    conn.execute(
        "UPDATE setup_sign_ins SET device_code_enc = NULL, status = 'failed', error = ?"
        " WHERE status IN ('waiting', 'working') AND expires_at <= ?",
        (entra.SIGN_IN_PROBLEMS["expired_token"], _iso(now)),
    )
    return conn.execute("DELETE FROM setup_sign_ins WHERE updated_at < ?", (_iso(now - timedelta(days=1)),)).rowcount
