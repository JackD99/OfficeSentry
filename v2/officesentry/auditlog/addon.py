"""The audit log add-on's switch, its app, and each client's opt-in.

The switch
  ``enabled()``: the ``audit_addon`` setting. Off until an admin creates the add-on app (App connection, Audit
  log add-on), which switches it on; off again hides the add-on everywhere (the client's Settings, the
  onboarding tracker's column, the report) for an MSP that doesn't want it. It can't be switched off while
  any client's feed is on.

The app
  An ``app_connections`` row of kind ``audit`` per Microsoft cloud, made by the setup wizard (``app_setup``,
  purpose "audit") or by hand. Its certificate is replaced like the shared app's (``cert_lifecycle``).

Each client (``audit_feeds``, one row per client the add-on was asked for), through ``STATES``:

  Not sent -> Sent -> Approved -> On, and Off.

  1. An admin sends the client its add-on link (``mark_sent``), which also records that they want the add-on
     on for that client (``wanted_at``).
  2. The client's Global Administrator approves the add-on on Microsoft's consent page and lands on the
     thank-you page (``client_approved``). That's noted, never believed: an ``audit_check`` task signs in as
     the add-on app and reads the client's feed subscriptions (``confirm``, one GET). Only that sets
     ``consented_at``. The ``audit_checks`` periodic job (``jobs.py``) checks again while it waits.
  3. Once confirmed, and only for a client an admin asked for, the same task starts the feed: one POST per
     content type (``feed.start``). Then it queues the first collection.
  4. Switching off (``switch_off``) marks the client Off at once, so nothing more is collected, and queues an
     ``audit_stop`` task that sends a stop call for each content type that may still be on (``feed_left``: a
     start that half worked counts) and, when asked, deletes what was collected. Deleting the client does the
     same (``on_delete``), and so does Microsoft saying part of the feed is off (``check_running``). A stop
     refused at the MSP's end is sent again (``stops_due``). A client who removes the add-on app from their
     tenant is noticed at the next collection and shown as Off with the reason, and its approval is forgotten
     (``mark_removed``): switching it on again needs the link sent and approved again.

No step here writes to a client's tenant: the feed's start and stop only tell Microsoft whether to make the
client's new audit records available to this app.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from .. import clouds, store, tasks
from ..connection_check import explain
from ..db import audit, transaction, utcnow
from ..devices import parse_ts
from ..discovery import tenant_id_for_domain
from ..text import plural
from . import feed

SETTING = "audit_addon"
COLLECTOR = "audit_activity"  # the collector that reads the feed (collectors/audit_activity.py)
ACTOR = "Audit log add-on"
AUDIT_NAME = "Office Sentry audit log add-on"  # the add-on app's name in Entra

# How often a client still waiting is checked by signing in (``due``), as for the main app's consent.
CHECK_SOON = timedelta(minutes=2)  # just after the client came back from the consent page...
SOON_FOR = timedelta(hours=1)  # ...for this long,
CHECK_APPROVED = timedelta(minutes=30)  # then this often...
APPROVED_FOR = timedelta(days=7)  # ...for this long.
CHECK_SENT = timedelta(hours=1)  # A sent link nobody has come back from yet...
SENT_FOR = timedelta(days=30)  # ...for this long.
# Microsoft asks for 15 minutes between requests to start a feed, so a start that failed is tried again after this.
RETRY_START = timedelta(minutes=20)
START_FOR = timedelta(days=7)
# A stop refused at the MSP's end (the add-on app's certificate, an outage) is sent again this often, for this long.
RETRY_STOP = timedelta(hours=1)
STOP_FOR = timedelta(days=7)


class AddonError(Exception):
    """Something the admin asked for can't be done; the message says why, in words for people."""


# -- The switch and the app -----------------------------------------------------------------------------------


def enabled(conn) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (SETTING,)).fetchone()
    return bool(row) and row[0] == "1"


def on_names(conn) -> list[str]:
    """The clients whose feed is on, or being started (approved and asked for): either may have something on at
    Microsoft that only switching the client off stops. Paused and archived clients count; archived ones are
    marked, since most lists leave them out."""
    return [
        r["name"] + (" (archived)" if r["archived_at"] else "")
        for r in conn.execute(
            "SELECT t.name, t.archived_at FROM audit_feeds f JOIN tenants t ON t.id = f.tenant_id"
            " WHERE f.stopped_at IS NULL"
            " AND (f.started_at IS NOT NULL OR (f.consented_at IS NOT NULL AND f.wanted_at IS NOT NULL))"
            " ORDER BY t.name COLLATE NOCASE"
        )
    ]


def switch(conn, on: bool, user: str) -> None:
    """Switch the add-on on or off for the whole install. Off is refused while any client's feed is on."""
    if on == enabled(conn):
        return
    if not on and (names := on_names(conn)):
        shown = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
        raise AddonError(
            f"The add-on is on for {plural(len(names), 'client')}: {shown}. Switch it off for each of them first "
            "(their Settings tab, Audit log add-on), so their feeds are stopped."
        )
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SETTING, "1" if on else "0"),
    )
    audit(conn, user, "audit_addon_switched_on" if on else "audit_addon_switched_off")


def connections(conn) -> list:
    return store.list_connections(conn, kind=store.AUDIT)


def connection_for(conn, cloud: str):
    """The add-on app for ``cloud``: one with a client ID first, else one waiting for it, else None."""
    return conn.execute(
        "SELECT * FROM app_connections WHERE kind = 'audit' AND cloud = ? ORDER BY client_id IS NULL, id LIMIT 1",
        (cloud,),
    ).fetchone()


def ready_connection(conn, cloud: str):
    """The add-on app for ``cloud`` that has its client ID, or None."""
    found = connection_for(conn, cloud)
    return found if found is not None and found["client_id"] else None


def app_changed(conn, connection_id: int, old_client_id: str, user: str) -> int:
    """The add-on's app connection now names another app registration (its client ID was changed): approvals
    of the old app say nothing about the new one, and links already sent name the old one. Each client on it
    goes back to Not sent, and a feed that was on shows as Off (it was the old app's, so nothing is stopped from
    here). Returns how many clients that was."""
    rows = conn.execute("SELECT * FROM audit_feeds WHERE connection_id = ?", (connection_id,)).fetchall()
    now = utcnow()
    for r in rows:
        conn.execute(
            "UPDATE audit_feeds SET sent_at = NULL, sent_by = NULL, approved_at = NULL, consented_at = NULL,"
            " checked_at = NULL, check_error = NULL, content_types = NULL WHERE tenant_id = ?",
            (r["tenant_id"],),
        )
        if is_on(r):
            conn.execute(
                "UPDATE audit_feeds SET stopped_at = ?, stopped_by = ?, stop_reason = ? WHERE tenant_id = ?",
                (
                    now,
                    user,
                    "The add-on's app changed, so the client must approve the new one. Send them the add-on link "
                    "again, then switch it on.",
                    r["tenant_id"],
                ),
            )
        audit(
            conn,
            user,
            "audit_consent_reset",
            r["tenant_id"],
            f"the add-on app's client ID changed from {old_client_id}",
        )
    return len(rows)


# -- Each client ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class State:
    key: str
    label: str
    tone: str  # pill tone
    detail: str  # one sentence: what's happening or what's next


STATES = {  # key -> (label, tone), in the order a client moves through them
    "not_sent": ("Not sent", "info"),
    "sent": ("Sent", "info"),
    "approved": ("Approved", "accent"),
    "on": ("On", "ok"),
    "off": ("Off", "warn"),
}
CONFIRMING = "Approved, confirming"  # an approval the client came back saying they gave, not yet signed in to


def _state(key: str, detail: str, label: str | None = None) -> State:
    name, tone = STATES[key]
    return State(key, label or name, tone, detail)


def row_for(conn, tenant_id: int):
    return conn.execute("SELECT * FROM audit_feeds WHERE tenant_id = ?", (tenant_id,)).fetchone()


def rows_for(conn, tenant_ids) -> dict:
    ids = list(tenant_ids)
    if not ids:
        return {}
    return {
        r["tenant_id"]: r
        for r in conn.execute(f"SELECT * FROM audit_feeds WHERE tenant_id IN ({','.join('?' * len(ids))})", ids)
    }


def is_on(row) -> bool:
    """The client's feed was started and hasn't been switched off since."""
    return row is not None and bool(row["started_at"]) and not row["stopped_at"]


def _statuses(row) -> dict:
    """What Microsoft last said of each of the client's content types (``content_types``), or {}."""
    try:
        said = json.loads(row["content_types"] or "{}") if row is not None else {}
    except ValueError:
        return {}
    return said if isinstance(said, dict) else {}


def feed_left(row) -> list[str]:
    """The content types of the client's feed that may still be on at Microsoft for the add-on app: once the
    client's approval was confirmed, each one Microsoft hasn't said is off. A start that worked for one content
    type and failed for the other counts, and so does a feed Microsoft says is off for one of them."""
    if row is None or not row["consented_at"]:
        return []  # the app has never signed in to the client, so it can't have started anything
    said = _statuses(row)
    return [c for c in feed.CONTENT_TYPES if said.get(c) != "disabled"]


def asked(row) -> bool:
    """Whether the add-on was asked for on this client (its link sent, or switched on), or its feed was ever
    on or switched off. A client that only came back from the consent link, or whose request was taken back
    before anything started, wasn't."""
    return row is not None and bool(row["sent_at"] or row["wanted_at"] or row["started_at"] or row["stopped_at"])


def shown(conn, tenant_id: int) -> bool:
    """Whether what the add-on collected shows for this client (its report, people's timelines): while the
    add-on is on, for a client it was asked for."""
    return enabled(conn) and asked(row_for(conn, tenant_id))


def active(conn, tenant) -> bool:
    """Whether the add-on collects for this client now: switched on, the client's feed on, and the client
    neither paused nor archived."""
    return (
        bool(tenant["enabled"]) and not tenant["archived_at"] and enabled(conn) and is_on(row_for(conn, tenant["id"]))
    )


def state(row) -> State:
    """Where the client has got, from its ``audit_feeds`` row (or None)."""
    if row is None:
        return _state("not_sent", "Send the client the add-on's approval link.")
    if row["stopped_at"]:
        return _state("off", row["stop_reason"] or "Switched off.")
    if row["started_at"]:
        return _state("on", "Office Sentry reads the client's audit log every night.")
    error = (
        row["check_error"] if row["checked_at"] and (row["checked_at"] or "") >= (row["approved_at"] or "") else None
    )
    if row["consented_at"]:
        if not row["wanted_at"]:
            return _state("approved", "The client approved the add-on. Switch it on to start reading its audit log.")
        if error:
            return _state("approved", f"The client approved, but the feed hasn't started yet. {error}")
        return _state("approved", "The client approved. Office Sentry is starting the feed.")
    if row["approved_at"]:  # someone came back from the link saying so: not confirmed until the sign-in works
        if error:
            return _state("approved", f"The client approved, but Office Sentry can't sign in yet. {error}", CONFIRMING)
        return _state("approved", "The client approved. Office Sentry is signing in to confirm.", CONFIRMING)
    if row["sent_at"]:
        return _state("sent", "Waiting for the client's Global Administrator to approve the add-on.")
    return _state("not_sent", "Send the client the add-on's approval link.")


def problem(conn, tenant) -> str | None:
    """Why the add-on can't be asked for on this client yet, or None when its link can be sent."""
    cloud = clouds.of(tenant)
    if why := clouds.missing(cloud, COLLECTOR):
        return why
    if ready_connection(conn, cloud.key) is None:
        return f"No audit log add-on app is set up for {cloud.label} yet (App connection, Audit log add-on)."
    if not tenant["consented_at"]:
        return "Connect the client first: the add-on adds to the reports Office Sentry already makes for it."
    return None


def tenant_guid(tenant, session=None) -> str | None:
    """The client's directory (tenant) ID, which the feed needs: its own, or the one Microsoft publishes for its
    domain."""
    ref = (tenant["tenant_ref"] or "").lower()
    if feed.GUID.match(ref):
        return ref
    return tenant_id_for_domain(ref, session=session, cloud=clouds.of(tenant).key)


def _upsert(conn, tenant_id: int, **values) -> None:
    columns = ", ".join(values)
    marks = ", ".join("?" * len(values))
    updates = ", ".join(f"{k} = excluded.{k}" for k in values)
    conn.execute(
        f"INSERT INTO audit_feeds (tenant_id, created_at, {columns}) VALUES (?, ?, {marks})"
        f" ON CONFLICT (tenant_id) DO UPDATE SET {updates}",
        (tenant_id, utcnow(), *values.values()),
    )


def mark_sent(conn, tenant_ids, user: str, sent: bool = True) -> int:
    """Record that the add-on link went to these clients, which says the admin wants it on for them (or, with
    ``sent=False``, take that back). Clients already approved, on or off are left as they are."""
    ids = list(tenant_ids)
    now = utcnow()
    n = 0
    with transaction(conn, immediate=True):
        rows = rows_for(conn, ids)
        for tenant_id in ids:
            row = rows.get(tenant_id)
            if row is not None and (row["consented_at"] or row["started_at"] or row["stopped_at"]):
                continue
            if sent:
                tenant = store.get_tenant(conn, tenant_id)
                found = connection_for(conn, clouds.of(tenant).key) if tenant else None
                _upsert(
                    conn,
                    tenant_id,
                    sent_at=now,
                    sent_by=user,
                    wanted_at=now,
                    wanted_by=user,
                    connection_id=found["id"] if found else None,
                )
            elif row is not None:
                conn.execute(
                    "UPDATE audit_feeds SET sent_at = NULL, sent_by = NULL, wanted_at = NULL, wanted_by = NULL"
                    " WHERE tenant_id = ?",
                    (tenant_id,),
                )
            audit(conn, user, "audit_link_sent" if sent else "audit_link_unsent", tenant_id)
            n += 1
    return n


def client_approved(conn, tenant) -> None:
    """Someone came back from the add-on's consent link saying they approved. Not proof: note it and queue a
    sign-in to confirm."""
    _upsert(conn, tenant["id"], approved_at=utcnow())
    queue_check(conn, tenant["id"], priority=tasks.PRIORITY_WAITING)


def queue_check(conn, tenant_id: int, priority: int = tasks.PRIORITY_BACKGROUND, user: str | None = None) -> int:
    return tasks.enqueue(
        conn, "audit_check", key=f"audit_check:{tenant_id}", tenant_id=tenant_id, priority=priority, user=user
    )


def switch_on(conn, tenant, user: str) -> None:
    """Ask for the add-on on this client again (after it was switched off, or for a client that approved without
    being asked): the feed is started once its approval is confirmed. Refused while a stop is still to be sent,
    which would otherwise stop the feed this starts."""
    if problem(conn, tenant) and not row_for(conn, tenant["id"]):
        raise AddonError(problem(conn, tenant))
    now = utcnow()
    with transaction(conn, immediate=True):
        if tasks.pending(conn, _stop_key(tenant)):
            raise AddonError(
                "Office Sentry is still stopping this client's feed at Microsoft. Reload in a minute, then switch it "
                "on again."
            )
        _upsert(conn, tenant["id"], wanted_at=now, wanted_by=user)
        conn.execute(
            "UPDATE audit_feeds SET started_at = NULL, started_by = NULL, stopped_at = NULL, stopped_by = NULL,"
            " stop_reason = NULL, check_error = NULL WHERE tenant_id = ?",
            (tenant["id"],),
        )
        audit(conn, user, "audit_addon_requested", tenant["id"])
    queue_check(conn, tenant["id"], priority=tasks.PRIORITY_WAITING, user=user)


def switch_off(conn, tenant, user: str, delete: bool = False, reason: str | None = None) -> None:
    """Switch the add-on off for this client: nothing more is collected from now, and a task stops the feed at
    Microsoft (and, with ``delete``, deletes what was collected). Collected events otherwise stay until retention
    removes them."""
    row = row_for(conn, tenant["id"])
    if row is None:
        raise AddonError("The add-on was never asked for on this client.")
    with transaction(conn, immediate=True):
        conn.execute(
            "UPDATE audit_feeds SET stopped_at = ?, stopped_by = ?, stop_reason = ?, wanted_at = NULL,"
            " wanted_by = NULL WHERE tenant_id = ?",
            (utcnow(), user, reason or f"Switched off by {user}.", tenant["id"]),
        )
        conn.execute(
            "DELETE FROM runs WHERE tenant_id = ? AND collector = ? AND status = 'queued'", (tenant["id"], COLLECTOR)
        )
        audit(
            conn,
            user,
            "audit_addon_switched_off_client",
            tenant["id"],
            "and deleted what was collected" if delete else "",
        )
        _queue_stop(conn, tenant, row, user, delete)


def on_delete(conn, tenant, user: str) -> None:
    """The client is being deleted (inside the delete's transaction): stop whatever of its feed may still be on
    at Microsoft afterwards, whether it was on, half started or marked Off. Its collected events go with the
    rest of its activity history."""
    row = row_for(conn, tenant["id"])
    if feed_left(row):
        _queue_stop(conn, tenant, row, user, delete=False)


def _stop_key(tenant) -> str:
    return f"audit_stop:{tenant['id']}"


def _queue_stop(conn, tenant, row, user: str, delete: bool) -> None:
    """Queue the task that stops what may be left of the client's feed (``feed_left``) and, with ``delete``,
    deletes what was collected; inside the caller's transaction. A stop already queued for the client takes on
    this one's content types and its delete, so neither is lost. The task carries everything it needs, and no
    tenant id, so a delete straight after can't take it."""
    key, stop = _stop_key(tenant), feed_left(row)
    queued = tasks.pending(conn, key)
    if queued is None:
        tasks.enqueue(
            conn,
            "audit_stop",
            key=key,
            payload={
                "tenant_id": tenant["id"],
                "tenant_ref": tenant["tenant_ref"],
                "cloud": clouds.of(tenant).key,
                "connection_id": row["connection_id"],
                "name": tenant["name"],
                "user": user,
                "delete": delete,
                "stop": stop,
            },
            user=user,
        )
    elif queued["status"] == "running":  # it read what to do when it began; a delete can't be added now
        if delete:
            raise AddonError(
                "Office Sentry is stopping this client's feed at Microsoft right now. Try again in a minute."
            )
    else:
        payload = json.loads(queued["payload"] or "{}")
        payload["delete"] = bool(payload.get("delete")) or delete
        payload["stop"] = [c for c in feed.CONTENT_TYPES if c in stop or c in (payload.get("stop") or [])]
        conn.execute("UPDATE tasks SET payload = ? WHERE id = ?", (json.dumps(payload), queued["id"]))


# -- Confirming, starting and stopping (background tasks) ------------------------------------------------------


@dataclass
class Outcome:
    ok: bool
    message: str


def _note(conn, tenant_id: int, error: str | None, **values) -> None:
    _upsert(conn, tenant_id, checked_at=utcnow(), check_error=error[:500] if error else None, **values)


# Sign-in refusals that mean the add-on app isn't in the client's tenant, or can't be used there: the client
# hasn't approved it, removed it, or disabled it. Anything else (an expired certificate, say) is the MSP's to fix.
NOT_IN_TENANT = {"AADSTS700016", "AADSTS7000229", "AADSTS65001", "AADSTS7000112"}
REMOVED = (
    "The client has removed or disabled the audit log add-on in their tenant, so Office Sentry can't read its "
    "audit log any more. Send them the add-on link again to switch it back on."
)
STOP_REMOVED = "The client has removed the add-on, so its feed can't be read any more."


def removed(exc: feed.FeedError) -> bool:
    """Whether a refusal means the client doesn't have the add-on (any more), rather than a problem at the MSP's
    end such as the add-on app's certificate."""
    if not isinstance(exc, feed.FeedAccessDenied):
        return False
    if exc.code in feed.NOT_APPROVED:
        return True
    known = explain(exc.message, exc.code)
    return known is None or known.code in NOT_IN_TENANT


def _feed_problem(exc: feed.FeedError) -> str:
    """Why the add-on couldn't read a client's feed, in words for people."""
    if exc.code == "network_error":
        return "Office Sentry couldn't reach Microsoft's audit log feed. It tries again shortly."
    if isinstance(exc, feed.FeedAccessDenied):
        if not removed(exc):
            known = explain(exc.message, exc.code)
            return f"{known.title} {known.fix}"
        return (
            "The client hasn't approved the add-on yet, or has removed it from their tenant. Send them the add-on "
            "link again."
        )
    return f"Microsoft's audit log feed refused: {exc.message[:300] or exc.code}"


def client_for(conn, vault, tenant, connection=None, session=None, **kwargs) -> feed.FeedClient:
    """A feed client for this tenant, signing in as its cloud's add-on app. Raises ``AddonError`` when there's
    no app or no tenant ID to sign in with."""
    cloud = clouds.of(tenant)
    if why := clouds.missing(cloud, COLLECTOR):
        raise AddonError(why)
    connection = connection or ready_connection(conn, cloud.key)
    if connection is None or not connection["client_id"]:
        raise AddonError(f"No audit log add-on app is set up for {cloud.label}.")
    guid = tenant_guid(tenant, session)
    if guid is None:
        raise AddonError("Microsoft didn't say which tenant ID this client's domain belongs to. Enter its tenant ID.")
    return feed.client(guid, connection, vault.decrypt(connection["key_enc"]), session=session, **kwargs)


def confirm(conn, vault, tenant, session=None) -> Outcome:
    """Sign in to the client as the add-on app and read its feed subscriptions (GET). When that works the
    approval is recorded; then, for a client an admin asked for whose feed hasn't been started, the feed is
    started (``feed.start``, one POST per content type) and the first collection queued."""
    row = row_for(conn, tenant["id"])
    if row is None or row["stopped_at"]:
        return Outcome(False, "The add-on isn't asked for on this client.")
    try:
        client = client_for(conn, vault, tenant, session=session)
        subscriptions = client.subscriptions()
    except AddonError as exc:
        _note(conn, tenant["id"], str(exc))
        return Outcome(False, str(exc))
    except feed.FeedError as exc:
        message = _feed_problem(exc)
        with transaction(conn, immediate=True):
            _note(conn, tenant["id"], message)
            if removed(exc) and row["consented_at"] and not row["started_at"]:  # it was approved, and removed since
                _forget_approval(conn, tenant["id"])
        return Outcome(False, message)
    connection = ready_connection(conn, clouds.of(tenant).key)
    with transaction(conn, immediate=True):
        newly = not row["consented_at"]
        _note(conn, tenant["id"], None, connection_id=connection["id"], content_types=json.dumps(subscriptions))
        conn.execute(
            "UPDATE audit_feeds SET consented_at = COALESCE(consented_at, ?) WHERE tenant_id = ?",
            (utcnow(), tenant["id"]),
        )
        if newly:
            audit(conn, ACTOR, "audit_addon_approved", tenant["id"], "confirmed by signing in as the add-on app")
    row = row_for(conn, tenant["id"])
    if row["wanted_at"] and not row["started_at"]:
        return _start(conn, client, tenant, row, subscriptions)
    if row["started_at"]:
        return check_running(conn, tenant, subscriptions)
    return Outcome(True, "The client approved the add-on.")


def _start(conn, client: feed.FeedClient, tenant, row, subscriptions: dict[str, str]) -> Outcome:
    statuses = dict(subscriptions)
    for content_type in feed.CONTENT_TYPES:
        if statuses.get(content_type) == "enabled":
            continue
        try:
            statuses[content_type] = client.start(content_type)
        except feed.FeedError as exc:
            message = (
                f"{_feed_problem(exc)} Office Sentry tries again in {int(RETRY_START.total_seconds() // 60)} minutes."
            )
            _note(conn, tenant["id"], message, content_types=json.dumps(statuses))
            return Outcome(False, message)
    who = row["wanted_by"] or ACTOR
    with transaction(conn, immediate=True):
        conn.execute(
            "UPDATE audit_feeds SET started_at = ?, started_by = ?, content_types = ?, check_error = NULL,"
            " checked_at = ? WHERE tenant_id = ?",
            (utcnow(), who, json.dumps(statuses), utcnow(), tenant["id"]),
        )
        audit(
            conn,
            who,
            "audit_feed_started",
            tenant["id"],
            ", ".join(feed.CONTENT_TYPES) + " (sent by Office Sentry once the client's approval was confirmed)",
        )
    if tenant["enabled"] and not tenant["archived_at"] and enabled(conn):
        store.enqueue_run(conn, tenant["id"], COLLECTOR, requested_by=ACTOR)
    return Outcome(True, "The feed is on. Microsoft can take up to 12 hours to make the first records available.")


def check_running(conn, tenant, subscriptions: dict[str, str]) -> Outcome:
    """For a client whose feed is on: when Microsoft says it's off for a content type (the client's admin
    turned it off, say), the client is marked Off with the reason, and a stop is queued for the rest of it, so
    nothing is left on that nobody reads. It's never started again without an admin."""
    off = [c for c in feed.CONTENT_TYPES if subscriptions.get(c) != "enabled"]
    if not off:
        _note(conn, tenant["id"], None, content_types=json.dumps(subscriptions))
        return Outcome(True, "The feed is on.")
    with transaction(conn, immediate=True):
        _note(conn, tenant["id"], None, content_types=json.dumps(subscriptions))
        row = row_for(conn, tenant["id"])
        left = feed_left(row)
        reason = (
            f"Microsoft says the feed is off ({', '.join(off)}): someone in the client's tenant may have turned it "
            "off. " + ("Office Sentry stops the rest of it too. " if left else "") + "Switch it on again to restart it."
        )
        if _mark_off(conn, tenant["id"], reason) and left:
            _queue_stop(conn, tenant, row, ACTOR, delete=False)
    return Outcome(False, "The feed is off at Microsoft.")


def mark_off(conn, tenant_id: int, reason: str) -> None:
    """The client's feed stopped without an admin switching it off: shown as Off with ``reason``; nothing is
    sent to Microsoft."""
    with transaction(conn, immediate=True):
        _mark_off(conn, tenant_id, reason)


def mark_removed(conn, tenant_id: int) -> None:
    """The client removed the add-on app, so it can't be signed in to: shown as Off (``REMOVED``), and its
    approval is gone too (``_forget_approval``)."""
    with transaction(conn, immediate=True):
        _mark_off(conn, tenant_id, REMOVED)
        _forget_approval(conn, tenant_id)


def _forget_approval(conn, tenant_id: int) -> None:
    """Inside the caller's transaction. The add-on app isn't in the client's tenant any more: its link has to be
    sent again and approved before it's on, and nothing it started there can be stopped (``feed_left``)."""
    conn.execute(
        "UPDATE audit_feeds SET sent_at = NULL, sent_by = NULL, approved_at = NULL, consented_at = NULL"
        " WHERE tenant_id = ?",
        (tenant_id,),
    )


def _mark_off(conn, tenant_id: int, reason: str) -> bool:
    """Inside the caller's transaction. Returns whether the client was marked Off (it wasn't Off already)."""
    changed = conn.execute(
        "UPDATE audit_feeds SET stopped_at = ?, stopped_by = ?, stop_reason = ?, wanted_at = NULL,"
        " wanted_by = NULL WHERE tenant_id = ? AND stopped_at IS NULL",
        (utcnow(), ACTOR, reason[:500], tenant_id),
    ).rowcount
    if changed:
        audit(conn, ACTOR, "audit_feed_off", tenant_id, reason[:300])
    return bool(changed)


@tasks.handler("audit_check")
def _check_task(ctx: tasks.TaskContext) -> dict:
    tenant = ctx.tenant
    if tenant is None or not tenant["enabled"] or tenant["archived_at"] or not enabled(ctx.conn):
        return {"skipped": "gone, paused, archived or the add-on is off"}  # the hourly check carries on once resumed
    outcome = confirm(ctx.conn, ctx.vault, tenant)
    return {"ok": outcome.ok}


@tasks.handler("audit_stop")
def _stop_task(ctx: tasks.TaskContext) -> dict:
    """Stop a client's feed at Microsoft (one POST for each content type that may still be on), then, when
    asked, delete what was collected. A client that removed the add-on can't be signed in to, and its feed can't
    be read anyway. A stop refused at the MSP's end (the add-on app's certificate, an outage) is sent again
    (``stops_due``)."""
    from .. import events

    p = ctx.payload
    tenant_id = p.get("tenant_id")
    tenant = store.get_tenant(ctx.conn, tenant_id) if tenant_id else None
    stopped, problems, gone = [], [], False
    if p.get("stop"):
        fake = {"tenant_ref": p.get("tenant_ref") or "", "cloud": p.get("cloud")}
        connection = store.get_connection(ctx.conn, p.get("connection_id") or 0, store.AUDIT)
        try:
            client = client_for(ctx.conn, ctx.vault, fake, connection=connection)
        except AddonError as exc:
            problems.append(str(exc))
        else:
            for content_type in [c for c in feed.CONTENT_TYPES if c in p["stop"]]:
                try:
                    client.stop(content_type)
                    stopped.append(content_type)
                except feed.FeedNotStarted:
                    stopped.append(content_type)  # already off
                except feed.FeedAccessDenied as exc:
                    gone = removed(exc)
                    problems.append(STOP_REMOVED if gone else _feed_problem(exc))
                    break
                except feed.FeedError as exc:
                    problems.append(_feed_problem(exc))
    deleted = 0
    if p.get("delete") and tenant is not None:
        deleted = events.delete_activity(ctx.conn, tenant["id"])
    with transaction(ctx.conn):
        if tenant is not None:
            said = _statuses(row_for(ctx.conn, tenant["id"]))  # one that couldn't be stopped still shows as on
            said.update({c: "disabled" for c in stopped})
            ctx.conn.execute(
                "UPDATE audit_feeds SET content_types = ?, check_error = ?, checked_at = ? WHERE tenant_id = ?",
                (json.dumps(said), "; ".join(problems)[:500] or None, utcnow(), tenant["id"]),
            )
            if gone:
                _forget_approval(ctx.conn, tenant["id"])
        detail = f"{p.get('name')}: " + (", ".join(stopped) if stopped else "nothing stopped")
        if deleted:
            detail += f"; {plural(deleted, 'collected event')} deleted"
        if problems:
            detail += "; " + " ".join(problems)
        audit(ctx.conn, p.get("user") or ACTOR, "audit_feed_stopped", tenant["id"] if tenant else None, detail[:500])
    return {"stopped": stopped, "deleted": deleted, "problems": problems}


def stops_due(conn, now: datetime) -> list[int]:
    """Clients switched off whose last stop was refused at the MSP's end, with something of their feed left
    (``feed_left``): sent again every ``RETRY_STOP``, for ``STOP_FOR`` after they were switched off."""
    out = []
    for r in conn.execute(
        "SELECT f.* FROM audit_feeds f JOIN tenants t ON t.id = f.tenant_id"
        " WHERE f.stopped_at IS NOT NULL AND f.check_error IS NOT NULL"
    ):
        stopped, checked = parse_ts(r["stopped_at"]), parse_ts(r["checked_at"])
        if not feed_left(r) or stopped is None or now - stopped > STOP_FOR:
            continue
        if checked is None or now - checked >= RETRY_STOP:
            out.append(r["tenant_id"])
    return out


def retry_stop(conn, tenant_id: int) -> None:
    """Queue the stop again for a client ``stops_due`` names, unless one is queued or running already."""
    tenant = store.get_tenant(conn, tenant_id)
    if tenant is None:
        return
    with transaction(conn, immediate=True):
        row = row_for(conn, tenant_id)
        if row is not None and row["stopped_at"] and feed_left(row) and not tasks.pending(conn, _stop_key(tenant)):
            _queue_stop(conn, tenant, row, ACTOR, delete=False)


def due(conn, now: datetime) -> list[int]:
    """Active clients whose next check is due: waiting for their approval to be confirmed (on the main app's
    consent schedule), or approved and asked for but whose feed hasn't started (every ``RETRY_START``)."""
    if not enabled(conn):
        return []
    out = []
    for r in conn.execute(
        "SELECT f.* FROM audit_feeds f JOIN tenants t ON t.id = f.tenant_id WHERE t.enabled = 1"
        " AND t.archived_at IS NULL AND f.stopped_at IS NULL AND f.started_at IS NULL"
    ):
        approved, sent, checked = parse_ts(r["approved_at"]), parse_ts(r["sent_at"]), parse_ts(r["checked_at"])
        consented, wanted = parse_ts(r["consented_at"]), parse_ts(r["wanted_at"])
        if consented:
            if not wanted or now - max(consented, wanted) > START_FOR:
                continue
            every = RETRY_START
        elif approved and now - approved <= APPROVED_FOR:
            every = CHECK_SOON if now - approved <= SOON_FOR else CHECK_APPROVED
        elif sent and now - sent <= SENT_FOR:
            every = CHECK_SENT
        else:
            continue
        if checked is None or now - checked >= every:
            out.append(r["tenant_id"])
    return out


# -- What the client is approving, in their words (the add-on's consent pack and email) ------------------------

CLIENT_WORDS = (
    "Who deleted, restored, downloaded, moved or copied files in SharePoint and OneDrive, and when.",
    "Sharing: links created, used or removed (anyone links called out), and people added to shared files and sites.",
    "Mailboxes opened by someone other than their owner (an admin or a delegate), and mail sent as or on behalf "
    "of someone else.",
    "Inbox rules and mail forwarding set up, changed or removed, and mail flow rules.",
    "Mailbox and folder permissions granted or removed, and mailbox exports requested.",
    "Changes to the audit log's own settings, such as auditing being turned off.",
)
NEVER_READS = (
    "What's inside files, emails or attachments: the audit log says that a file was downloaded and by whom, "
    "not what was in it.",
    "Anyone's own reading of their own mail, or the files they open and edit day to day.",
    "Teams chats or channel messages.",
    "Anything it could change: the add-on can only read the audit log, and has no other permission.",
)
