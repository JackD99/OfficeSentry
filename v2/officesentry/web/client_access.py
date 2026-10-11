"""Client sign-in for one client (``/tenants/<id>/sign-in``, under its Settings tab): who at the client can sign
in to Office Sentry and how, the link their own administrator uses to approve the sign-in app, a message to
send them, and adding a person who signs in with their own work Microsoft account (sso.py, client sign-in).

A person added here is an ordinary client account (the Client viewer role, granted this client only) with no
password, found in the client's directory as Office Sentry last collected it and connected then to that
Microsoft account (its object ID in this client's own tenant), which is the only thing it's matched on when
they sign in. An existing password account of this client's can be connected the same way. Changing or
removing one is done on its user page, as for any account.
"""

from __future__ import annotations

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for

from .. import permissions, roles, sso, store
from ..db import audit, transaction, utcnow
from .access import needs, tenant_or_404
from .admin import SIGN_IN_ADDRESS_RE as EMAIL_RE
from .admin import _givable, _out_of_reach
from .sso import approval_link

bp = Blueprint("client_access", __name__)

MAX_USERNAME = 64  # the users page's limit


def _cfg():
    return current_app.extensions["officesentry.config"]


def _page(tenant_id: int):
    return redirect(url_for("client_access.page", tenant_id=tenant_id))


def _contacts(tenant_id: int, taken: set[str]) -> list:
    """The client's report contacts who don't have an account yet: the likeliest people to add."""
    rows = g.db.execute(
        "SELECT name, email FROM contacts WHERE tenant_id = ? ORDER BY name COLLATE NOCASE, email", (tenant_id,)
    ).fetchall()
    return [r for r in rows if r["email"].lower() not in taken and EMAIL_RE.match(r["email"])]


@bp.get("/tenants/<int:tenant_id>/sign-in")
@needs("users.manage", kind="staff")
def page(tenant_id: int):
    t = tenant_or_404(tenant_id)
    s = sso.settings(g.db)
    people = sso.client_people(g.db, t["id"])
    taken = {u["username"].lower() for u in g.db.execute("SELECT username FROM users")}
    portal = _cfg().base_url.rstrip("/")
    tid = sso.directory_id(g.db, t)
    own = tid is not None and tid == (s.get("sso_tenant") or "").lower()
    return render_template(
        "client_sign_in.html",
        tenant=t,
        people=people,
        with_microsoft=sum(1 for u in people if u["entra_oid"]),
        configured=sso.is_configured(s),
        clients_enabled=sso.clients_enabled(s),
        approval=None if own else approval_link(s, t),
        approved=t["sign_in_approved_at"],
        known=tid is not None,
        own=own,
        same_cloud=t["cloud"] == sso.cloud(s).key,
        contacts=_contacts(t["id"], taken) if sso.clients_enabled(s) else [],
        sign_in_url=portal + url_for("auth.login"),
        start_url=portal + url_for("sso.start"),
        can_add=_givable(roles.by_key(g.db, "client_viewer")),
    )


def _existing(person: dict, email: str):
    """An account already named like this person, or already connected to their Microsoft account."""
    return g.db.execute(
        "SELECT * FROM users WHERE entra_oid = ? OR lower(username) IN (?, ?) ORDER BY entra_oid = ? DESC LIMIT 1",
        (person["oid"], email.lower(), person["upn"].lower(), person["oid"]),
    ).fetchone()


def _connectable(user, t, tid: str) -> bool:
    """Can this existing account be connected to the person's Microsoft account? Only a client account of this
    client's, connected to nothing yet, that sees no client in another organisation and is within reach."""
    if user["role"] != "client" or user["entra_oid"] or _out_of_reach(user):
        return False
    granted = [
        store.get_tenant(g.db, r[0])
        for r in g.db.execute("SELECT tenant_id FROM user_tenants WHERE user_id = ?", (user["id"],))
    ]
    return t["id"] in {x["id"] for x in granted} and all(sso.directory_id(g.db, x) == tid for x in granted)


@bp.post("/tenants/<int:tenant_id>/sign-in/people")
@needs("users.manage", kind="staff")
def add(tenant_id: int):
    """Add someone at this client who signs in with their own work Microsoft account: found in the client's
    directory by address, then matched only on their object ID there (``sso.directory_person``)."""
    t = tenant_or_404(tenant_id)
    s = sso.settings(g.db)
    role = roles.by_key(g.db, "client_viewer")
    email = request.form.get("email", "").strip()
    tid = sso.directory_id(g.db, t)
    person = existing = None
    problem = None
    if not sso.clients_enabled(s):
        problem = "Client sign-in with Microsoft is off. An admin turns it on under Settings → Microsoft sign-in."
    elif t["cloud"] != sso.cloud(s).key:
        problem = f"{t['name']} is in another Microsoft cloud, so its people can't sign in with Microsoft."
    elif tid is None:
        problem = f"Office Sentry doesn't know {t['name']}'s tenant ID yet. It learns it from the first collection."
    elif tid == (s.get("sso_tenant") or "").lower():
        problem = f"{t['name']} is your own tenant: its people sign in as staff."
    elif role is None or role.kind != permissions.CLIENT or not _givable(role):
        problem = "Only someone who can give the Client viewer role can add people here."
    elif not EMAIL_RE.match(email) or len(email) > MAX_USERNAME:
        problem = "Enter the address they sign in to Microsoft with, like jo@client.com (up to 64 characters)."
    else:
        try:
            person = sso.directory_person(g.db, t, email)
        except sso.Refused as exc:
            problem = str(exc)
    if person and len(person["upn"]) > MAX_USERNAME:
        problem = (
            f"{person['upn']} is longer than Office Sentry's 64-character usernames. Give them a password account."
        )
    elif person and (existing := _existing(person, email)) and not _connectable(existing, t, tid):
        problem = (
            f"{existing['username']} can already sign in to {t['name']}."
            if existing["entra_oid"] == person["oid"] and existing["role"] == "client"
            else f"An account named {existing['username']} already exists. Change it on its user page."
        )
    if problem:
        flash(problem, "error")
        return _page(t["id"])
    now = utcnow()
    with transaction(g.db):
        if existing:
            uid = existing["id"]
            g.db.execute(
                "UPDATE users SET entra_oid = ?, entra_tid = ?, entra_upn = ?, entra_linked_at = ? WHERE id = ?",
                (person["oid"], tid, person["upn"], now, uid),
            )
            audit(g.db, g.user["username"], "microsoft_connected", t["id"], f"{existing['username']}: {person['upn']}")
        else:
            uid = g.db.execute(
                "INSERT INTO users (username, password_hash, role, role_id, all_tenants, created_at, entra_oid,"
                " entra_tid, entra_upn, entra_linked_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
                (
                    person["upn"].lower(),
                    sso.NO_PASSWORD,
                    role.column,
                    role.id,
                    now,
                    person["oid"],
                    tid,
                    person["upn"],
                    now,
                ),
            ).lastrowid
            g.db.execute("INSERT INTO user_tenants (user_id, tenant_id) VALUES (?, ?)", (uid, t["id"]))
            audit(
                g.db,
                g.user["username"],
                "user_created",
                t["id"],
                f"{person['upn']} ({role.name}), signs in with Microsoft, client {t['name']}",
            )
    flash(
        f"{existing['username']} can sign in with their work Microsoft account now, as well as their password."
        if existing
        else f"Added {person['upn']}. They can sign in with their work Microsoft account now; copy the message below "
        "to tell them how.",
        "success",
    )
    return redirect(url_for("client_access.page", tenant_id=t["id"]) + "#message")
