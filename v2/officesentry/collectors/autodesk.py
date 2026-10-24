"""Autodesk licences: each opted-in client's Autodesk subscriptions, who holds each seat and how much they use it,
from Autodesk's Business Success Plan Reporting API (``autodesk/api.py``).

Only collected for clients someone gave an Autodesk team (``autodesk.setup.applies``). For each of the client's
teams, signed in with the sign-in that sees it:

1. An export of the team's subscriptions and users (``SUBSCRIPTIONS``, ``USERS``), waited for and downloaded.
2. An export of the last ``USAGE_DAYS`` days' use by product (``USAGE_REPORT``): when each person was given
   each product, when they last used it and on how many days. A team without it still gets its seats; the
   report then says use wasn't read.

Every export names the team, and rows of any other team are dropped, so nothing of another client is kept even
when one sign-in administers several. A team that fails is a warning (the run is partial) and the others are
kept; when none could be read the run fails, so the last good one stays. Nothing is changed in Autodesk.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from ..autodesk import api, exports, setup
from ..autodesk.setup import USAGE_DAYS
from ..text import plural
from .base import Collector, RunContext, register


class Unread(api.AutodeskError):
    """No team could be read: why, in words, keeping Autodesk's status so a busy Autodesk is tried again later
    (``worker.passing``)."""

    def __str__(self) -> str:
        return self.message


def _sign_in(conn, team):
    """The sign-in that reads ``team``: the one it was given with, else any whose last check saw it."""
    if team["sign_in_id"] is not None and (row := setup.get_sign_in(conn, team["sign_in_id"])) is not None:
        return row
    seen = setup.seen_teams(conn).get(team["team_id"])
    return setup.get_sign_in(conn, seen["sign_in_id"]) if seen else None


def _link(links: dict[str, str], report: str, only: bool) -> str | None:
    """The download link of ``report``: by its name, else one named after it (Autodesk names the usage file
    USAGE_REPORT_BY_PRODUCT), else the only file of an export that asked for one report."""
    if report in links:
        return links[report]
    named = [url for kind, url in links.items() if kind.startswith(report)]
    if named:
        return named[0]
    return next(iter(links.values())) if only and len(links) == 1 else None


def _files(client: api.AutodeskClient, team_id: str, reports, start=None, end=None) -> dict:
    export_id = client.start_export(team_id, reports, start, end)
    links = client.wait(export_id, team_id)
    out = {}
    for report in reports:
        if (url := _link(links, report, len(reports) == 1)) is None:
            raise api.ExportFailed(200, "no_file", f"Autodesk's export had no {report.lower()} file")
        out[report] = exports.read(client.download(url))
    return out


def _ours(table: exports.Table, team_id: str) -> tuple[exports.Table, int]:
    """The rows of ``team_id`` (and rows that don't say), and how many of another team were dropped."""
    kept = [r for r in table.rows if r.get("team_id", "") in ("", team_id)]
    return exports.Table(kept, table.unknown), len(table.rows) - len(kept)


def read_team(ctx: RunContext, client: api.AutodeskClient, team, now: datetime) -> dict:
    """One team's subscriptions and seats, as the objects to store."""
    team_id, name = team["team_id"], team["name"] or team["team_id"]
    files = _files(client, team_id, ("SUBSCRIPTIONS", "USERS"))
    usage: exports.Table | None = None
    usage_error = ""
    try:
        usage = _files(client, team_id, ("USAGE_REPORT",), now - timedelta(days=USAGE_DAYS), now)["USAGE_REPORT"]
    except api.AutodeskError as exc:
        usage_error = api.refused(exc) if isinstance(exc, api.AutodeskAccessDenied) else exc.message
        ctx.warn(f"Autodesk team {name}: use of each product couldn't be read ({usage_error[:200]})")

    dropped = 0
    subs_table, n = _ours(files["SUBSCRIPTIONS"], team_id)
    dropped += n
    users_table, n = _ours(files["USERS"], team_id)
    dropped += n
    if usage is not None:
        usage, n = _ours(usage, team_id)
        dropped += n
    if dropped:
        ctx.log(f"Autodesk team {name}: left out {plural(dropped, 'row')} that named another team", "warning")
    for label, table in (("subscriptions", subs_table), ("users", users_table), ("usage", usage)):
        if table is not None and table.unknown:
            ctx.log(f"Autodesk {label} file columns not read: {', '.join(table.unknown[:20])}")

    subscriptions = exports.subscriptions(subs_table)
    seats: dict[tuple[str, str], dict] = {}
    for s in exports.seats(users_table):
        if not s.product:
            continue  # one row per person: their products come from the usage report
        seats[(s.person, s.product.lower())] = {
            "person": s.person,
            "name": s.name,
            "product": s.product,
            "subscription": s.subscription,
            "assignedOn": s.assigned_on,
            "status": s.status,
            "lastSignIn": s.last_sign_in,
            "lastUsed": "",
            "daysUsed": None,
        }
    people = {s.person: s for s in exports.seats(users_table)}
    for u in exports.uses(usage) if usage is not None else []:
        seat = seats.get((u.person, u.product.lower()))
        if seat is None:
            if u.assigned is False:
                continue  # used it once, but holds no seat of it now
            who = people.get(u.person)
            seat = seats[(u.person, u.product.lower())] = {
                "person": u.person,
                "name": u.name or (who.name if who else ""),
                "product": u.product,
                "subscription": "",
                "assignedOn": "",
                "status": who.status if who else "",
                "lastSignIn": who.last_sign_in if who else "",
                "lastUsed": "",
                "daysUsed": None,
            }
        seat["assignedOn"] = seat["assignedOn"] or u.assigned_on
        seat["lastUsed"] = max(seat["lastUsed"], u.last_used)
        if u.days_used is not None:  # a row per version of a product: the most days, not their sum
            seat["daysUsed"] = max(seat["daysUsed"] or 0, u.days_used)

    by_sub = Counter(s["subscription"] for s in seats.values() if s["subscription"])
    by_product = Counter(s["product"].lower() for s in seats.values())
    products = Counter(sub.product.lower() for sub in subscriptions)
    hashed = any("@" not in s["person"] for s in seats.values())
    subs = []
    for sub in subscriptions:
        assigned = sub.assigned
        if assigned is None and by_sub:
            assigned = by_sub.get(sub.id, 0)
        elif assigned is None and products[sub.product.lower()] == 1:
            assigned = by_product.get(sub.product.lower(), 0)
        subs.append(
            (
                f"{team_id}:{sub.id}",
                {
                    "team": team_id,
                    "teamName": name,
                    "subscription": sub.id,
                    "product": sub.product,
                    "seats": sub.seats,
                    "assigned": assigned,
                    "status": sub.status,
                    "start": sub.start,
                    "end": sub.end,
                    "term": sub.term,
                },
            )
        )
    return {
        "team": (
            team_id,
            {
                "name": name,
                "hashed": hashed,
                "usage": "read" if usage is not None else "failed",
                "usageError": usage_error[:300],
                "usageDays": USAGE_DAYS,
                "subscriptions": len(subs),
                "seats": len(seats),
            },
        ),
        "subscriptions": subs,
        "seats": [
            (f"{team_id}:{s['person']}:{s['product'].lower()}", {"team": team_id, "teamName": name, **s})
            for s in seats.values()
        ],
    }


@register
class Autodesk(Collector):
    key = "autodesk"
    title = "Autodesk licences"
    description = (
        "The client's Autodesk subscriptions, who holds each seat and when they last used it, from Autodesk's "
        "reporting API. Only for clients given an Autodesk team on their Settings tab."
    )
    permissions = ()  # not Graph: Autodesk's own API, signed in with the MSP's Autodesk app and a team admin's token
    addon = True

    @classmethod
    def applies(cls, conn, tenant) -> bool:
        return setup.applies(conn, tenant)

    def collect(self, ctx: RunContext) -> None:
        conn, tenant = ctx.conn, ctx.tenant
        teams = setup.teams_for(conn, tenant["id"]) if setup.configured(conn) else []
        if not teams:
            ctx.log("Autodesk isn't set up for this client, so there's nothing to read")
            return
        now = datetime.now(UTC).replace(microsecond=0)
        read, failure = [], None
        for team in teams:
            name = team["name"] or team["team_id"]
            sign_in = _sign_in(conn, team)
            if sign_in is None:
                ctx.warn(
                    f"Autodesk team {name}: no Autodesk sign-in sees it any more. Add one on Settings, Autodesk, "
                    "made by an admin of the team."
                )
                continue
            try:
                read.append(read_team(ctx, ctx.autodesk(sign_in), team, now))
            except api.AutodeskError as exc:
                failure = exc
                why = api.refused(exc) if isinstance(exc, api.AutodeskAccessDenied) else exc.message
                ctx.warn(f"Autodesk team {name}: {why[:300]}")
        if not read:
            why = "; ".join(ctx.warnings) or "No Autodesk team could be read"
            if failure is not None:
                raise Unread(failure.status, failure.code, why) from failure
            raise setup.SetupError(why)
        ctx.objects("autodesk_team", [r["team"] for r in read])
        ctx.objects("autodesk_subscription", [s for r in read for s in r["subscriptions"]])
        ctx.objects("autodesk_seat", [s for r in read for s in r["seats"]])
        subs = [s for r in read for _, s in r["subscriptions"]]
        seats = [s for r in read for _, s in r["seats"]]
        owned = sum(s["seats"] or 0 for s in subs)
        ctx.metric("autodesk.seats", owned)
        ctx.metric("autodesk.assigned", len(seats))
        ctx.metric("autodesk.unassigned", sum(max((s["seats"] or 0) - (s["assigned"] or 0), 0) for s in subs))
        if all(r["team"][1]["usage"] == "read" for r in read):
            cutoff = (now - timedelta(days=USAGE_DAYS)).date().isoformat()
            ctx.metric("autodesk.unused", sum(1 for s in seats if not s["lastUsed"] or s["lastUsed"] < cutoff))
        ctx.log(
            f"Read {plural(len(subs), 'Autodesk subscription')} and {plural(len(seats), 'seat')} from "
            f"{plural(len(read), 'team')}"
        )
