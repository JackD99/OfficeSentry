"""Autodesk's app, its sign-ins and which Autodesk team belongs to which client.

The app
  The MSP registers one app at aps.autodesk.com, bound to the Business Success Plan Reporting API only, and
  saves its client ID and secret on Settings, Autodesk (``save_app``). The secret is encrypted
  (``ENCRYPTED_KEYS``) and never shown again. Nothing about Autodesk shows anywhere until it's saved
  (``configured``).

Sign-ins (``autodesk_sign_ins``)
  An admin of one or more Autodesk teams makes a personal access token for that app (Autodesk Account: Profile,
  Security, Personal access tokens) and it's entered here with a label (``add_sign_in``), encrypted. One token
  sees every team its person administers, so a technician who is an admin on several clients' teams needs one.
  ``check`` asks Autodesk which teams that is and keeps the answer for the settings page.

Each client's teams (``autodesk_teams``)
  Someone who manages every client picks, on a client's Settings tab, which of those teams are the client's
  (``assign``). A team belongs to one client at most, and the collector only asks Autodesk for a client's own
  teams, by ID, so one sign-in across many clients never mixes them. Team names come from other clients'
  Autodesk accounts, so only people who see every client are shown the teams nobody has been given yet.
"""

from __future__ import annotations

import json
import sqlite3

from ..db import audit, utcnow
from . import api

CLIENT_ID = "autodesk.client_id"
SECRET = "autodesk.client_secret_enc"  # noqa: S105 (a settings key; the value is encrypted)
SAVED = "autodesk.saved_at"
KEYS = (CLIENT_ID, SECRET, SAVED)
ENCRYPTED_KEYS = (SECRET,)
COLLECTOR = "autodesk"  # the collector that reads it (collectors/autodesk.py)
USAGE_DAYS = 90  # how far back each collection reads use of each product, and what counts as "not used"
MAX_SIGN_INS = 50
REPORTING = "ReportingAPIAccess"  # the benefit a team needs for Autodesk to answer its exports


class SetupError(Exception):
    """Something asked for can't be done; the message says why, in words for people."""


# -- The app ---------------------------------------------------------------------------------------------------


def _settings(conn) -> dict:
    rows = conn.execute(f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(KEYS))})", KEYS)
    return {r["key"]: r["value"] for r in rows}


def app(conn) -> dict:
    """The app as the settings page shows it: its client ID, whether a secret is saved, and when."""
    s = _settings(conn)
    return {"client_id": s.get(CLIENT_ID) or "", "secret": bool(s.get(SECRET)), "saved_at": s.get(SAVED)}


def configured(conn) -> bool:
    s = _settings(conn)
    return bool(s.get(CLIENT_ID) and s.get(SECRET))


def _put(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def save_app(conn, vault, client_id: str, secret: str, user: str) -> None:
    """Save the app's client ID and, when given, its secret (left as it is when blank)."""
    client_id = (client_id or "").strip()
    secret = (secret or "").strip()
    if not api.CLIENT_ID.match(client_id):
        raise SetupError("Enter the app's client ID as aps.autodesk.com shows it under My Apps (letters and numbers).")
    s = _settings(conn)
    changed = s.get(CLIENT_ID) not in (None, client_id)
    if not secret and (changed or not s.get(SECRET)):  # a secret is only ever used with the app it was made for
        raise SetupError("Enter the app's client secret too.")
    if len(secret) > 200:
        raise SetupError("That client secret is too long.")
    _put(conn, CLIENT_ID, client_id)
    if secret:
        _put(conn, SECRET, vault.encrypt(secret))
    _put(conn, SAVED, utcnow())
    audit(conn, user, "autodesk_app_saved", detail=client_id + (" (new app)" if changed else ""))


def forget_app(conn, user: str) -> None:
    """Remove the app and every sign-in made for it. Which team is whose stays, for when it's set up again."""
    conn.execute(f"DELETE FROM settings WHERE key IN ({','.join('?' * len(KEYS))})", KEYS)
    conn.execute("DELETE FROM autodesk_sign_ins")
    audit(conn, user, "autodesk_app_removed")


def _secret(conn, vault) -> tuple[str, str]:
    s = _settings(conn)
    if not (s.get(CLIENT_ID) and s.get(SECRET)):
        raise SetupError("Autodesk isn't set up: save the app's client ID and secret on Settings, Autodesk.")
    try:
        return s[CLIENT_ID], vault.decrypt(s[SECRET])
    except Exception as exc:  # the key that encrypted it was removed
        raise SetupError("The Autodesk app's secret can't be decrypted. Enter it again on Settings, Autodesk.") from exc


# -- Sign-ins ---------------------------------------------------------------------------------------------------


def _teams(row) -> list[dict]:
    try:
        found = json.loads(row["teams"] or "[]")
    except ValueError:
        return []
    return [t for t in found if isinstance(t, dict) and isinstance(t.get("id"), str)] if isinstance(found, list) else []


def sign_ins(conn) -> list[dict]:
    """Every sign-in with the teams it saw at its last check (never its token)."""
    rows = conn.execute(
        "SELECT id, label, added_by, added_at, checked_at, check_error, teams FROM autodesk_sign_ins ORDER BY id"
    ).fetchall()
    return [{**{k: r[k] for k in r.keys() if k != "teams"}, "teams": _teams(r)} for r in rows]


def get_sign_in(conn, sign_in_id: int):
    return conn.execute("SELECT * FROM autodesk_sign_ins WHERE id = ?", (sign_in_id,)).fetchone()


def add_sign_in(conn, vault, label: str, pat: str, user: str) -> int:
    label = " ".join((label or "").split())[:100]
    pat = (pat or "").strip()
    if not label:
        raise SetupError("Give the sign-in a name, such as whose Autodesk account made the token.")
    if not pat or len(pat) > 500 or any(c.isspace() for c in pat):
        raise SetupError("Paste the personal access token exactly as Autodesk showed it.")
    if conn.execute("SELECT COUNT(*) FROM autodesk_sign_ins").fetchone()[0] >= MAX_SIGN_INS:
        raise SetupError(f"Office Sentry keeps {MAX_SIGN_INS} Autodesk sign-ins at most.")
    cur = conn.execute(
        "INSERT INTO autodesk_sign_ins (label, pat_enc, added_by, added_at) VALUES (?, ?, ?, ?)",
        (label, vault.encrypt(pat), user, utcnow()),
    )
    audit(conn, user, "autodesk_sign_in_added", detail=label)
    return cur.lastrowid


def remove_sign_in(conn, sign_in_id: int, user: str) -> None:
    row = get_sign_in(conn, sign_in_id)
    if row is None:
        raise SetupError("That Autodesk sign-in was already removed.")
    conn.execute("DELETE FROM autodesk_sign_ins WHERE id = ?", (sign_in_id,))
    audit(conn, user, "autodesk_sign_in_removed", detail=row["label"])


def client_for(conn, vault, sign_in, **kwargs) -> api.AutodeskClient:
    """An Autodesk client signing in as the app with ``sign_in``'s token."""
    client_id, secret = _secret(conn, vault)
    try:
        pat = vault.decrypt(sign_in["pat_enc"])
    except Exception as exc:
        raise SetupError(f"The token for {sign_in['label']} can't be decrypted. Enter it again.") from exc
    return api.AutodeskClient(client_id, secret, pat, **kwargs)


def check(conn, vault, sign_in_id: int, **kwargs) -> list[dict]:
    """Ask Autodesk which teams a sign-in administers, and keep the answer (or why it failed)."""
    row = get_sign_in(conn, sign_in_id)
    if row is None:
        raise SetupError("That Autodesk sign-in was removed.")
    try:
        teams = client_for(conn, vault, row, **kwargs).contexts()
    except api.AutodeskError as exc:
        error = api.refused(exc)
        conn.execute(
            "UPDATE autodesk_sign_ins SET checked_at = ?, check_error = ? WHERE id = ?", (utcnow(), error, sign_in_id)
        )
        raise SetupError(error) from exc
    conn.execute(
        "UPDATE autodesk_sign_ins SET checked_at = ?, check_error = NULL, teams = ? WHERE id = ?",
        (utcnow(), json.dumps(teams), sign_in_id),
    )
    conn.execute(
        "UPDATE autodesk_teams SET sign_in_id = ? WHERE sign_in_id IS NULL AND team_id IN "
        f"({','.join('?' * len(teams)) or 'NULL'})",
        (sign_in_id, *[t["id"] for t in teams]),
    )
    return teams


# -- Each client's teams ------------------------------------------------------------------------------------------


def seen_teams(conn) -> dict[str, dict]:
    """Every team a sign-in saw at its last check, by ID: {"id", "name", "benefits", "sign_in_id", "label"}.
    A team two sign-ins see is listed once, under the first."""
    out: dict[str, dict] = {}
    for s in sign_ins(conn):
        for t in s["teams"]:
            out.setdefault(t["id"], {**t, "sign_in_id": s["id"], "label": s["label"]})
    return out


def teams_for(conn, tenant_id: int) -> list:
    return conn.execute(
        "SELECT t.*, s.label AS sign_in FROM autodesk_teams t LEFT JOIN autodesk_sign_ins s ON s.id = t.sign_in_id"
        " WHERE t.tenant_id = ? ORDER BY t.name COLLATE NOCASE, t.team_id",
        (tenant_id,),
    ).fetchall()


def free_teams(conn) -> list[dict]:
    """Teams a sign-in sees that no client has been given yet. Only for people who see every client."""
    taken = {r[0] for r in conn.execute("SELECT team_id FROM autodesk_teams")}
    return sorted((t for t in seen_teams(conn).values() if t["id"] not in taken), key=lambda t: t["name"].lower())


def assign(conn, tenant, team_id: str, user: str) -> str:
    """Give ``team_id`` to ``tenant``. Returns the team's name."""
    team = seen_teams(conn).get(team_id or "")
    if team is None:
        raise SetupError("No Autodesk sign-in sees that team. Check the sign-ins on Settings, Autodesk.")
    try:
        conn.execute(
            "INSERT INTO autodesk_teams (team_id, tenant_id, sign_in_id, name, added_by, added_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (team["id"], tenant["id"], team["sign_in_id"], team["name"], user, utcnow()),
        )
    except sqlite3.IntegrityError as exc:
        raise SetupError("That Autodesk team already belongs to a client.") from exc
    audit(conn, user, "autodesk_team_added", tenant["id"], f"{team['name']} ({team['id']})")
    return team["name"]


def unassign(conn, tenant, team_id: str, user: str) -> None:
    row = conn.execute(
        "SELECT name FROM autodesk_teams WHERE team_id = ? AND tenant_id = ?", (team_id, tenant["id"])
    ).fetchone()
    if row is None:
        raise SetupError("That Autodesk team isn't this client's.")
    conn.execute("DELETE FROM autodesk_teams WHERE team_id = ? AND tenant_id = ?", (team_id, tenant["id"]))
    audit(conn, user, "autodesk_team_removed", tenant["id"], f"{row['name']} ({team_id})")


def applies(conn, tenant) -> bool:
    """Whether the client's Autodesk licences are collected: Autodesk is set up and the client has a team."""
    return configured(conn) and bool(
        conn.execute("SELECT 1 FROM autodesk_teams WHERE tenant_id = ? LIMIT 1", (tenant["id"],)).fetchone()
    )
