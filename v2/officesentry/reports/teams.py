"""Teams: channels, the guests and people from other organisations in private and shared channels, the apps
added to each team, and teams nobody has used lately.

Reads ``collectors/teams.py``. Channels, channel members and apps need permissions added in 2.2; while the
client hasn't approved them the report says so (``permission_batch.waiting_text``) and shows what it has.
A team is inactive when Microsoft's activity report shows nothing in it for ``INACTIVE_DAYS`` and it is
older than that.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..devices import parse_day
from ..permission_batch import waiting_text
from ..snapshot import TenantSnapshot
from ..text import plural, verb
from ..usage import CONCEALED_NOTICE
from .data_reports import DataReport, Rows, Section, Tile
from .registry import report

NOT_COLLECTED = "Teams haven't been collected yet. Refresh Teams on the client page."
INACTIVE_DAYS = 90
PRIVACY = {"Private": "Private", "Public": "Public", "HiddenMembership": "Hidden membership"}
CHANNEL_TYPES = {"standard": "Standard", "private": "Private", "shared": "Shared"}
GUEST = "Guest"
EXTERNAL = "From another organisation"
CUSTOM_UPLOAD = "Custom upload"

EVERY = "Every team"
INACTIVE = "Inactive teams"
CHANNELS = "Private and shared channels"
PEOPLE = "Guests and people from other organisations"
APPS = "Apps in teams"


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _count(value) -> int | str:
    return value if isinstance(value, int) else ""


def _status(snap: TenantSnapshot) -> dict:
    rows = snap.objects("teams", "teams_status") or []
    return rows[0] if rows and isinstance(rows[0], dict) else {}


def _inactive(team: dict, now: datetime, activity_read: bool) -> str | None:
    """Why a team counts as inactive (its last activity, in words), or None."""
    if not activity_read:
        return None
    created = parse_day(team.get("createdDateTime"))
    cutoff = (now - timedelta(days=INACTIVE_DAYS)).date()
    if created is None or created > cutoff:
        return None
    last = parse_day(team.get("lastActivity"))
    if last is None:
        return "No activity recorded"
    return last.isoformat() if last <= cutoff else None


@report(
    "teams",
    "Teams channels, guests and apps",
    "Each team's private and shared channels and who is in them, guests and people from other organisations, "
    "the apps added to each team, and teams nobody has used lately.",
    ("teams",),
    group="files",
    order=55,
    keywords="teams channels private channels shared channels guests external people apps installed "
    "inactive teams unused teams microsoft teams clean up cleanup",
    uses=("sharing",),
)
def teams_report(snap: TenantSnapshot, now: datetime, out: DataReport) -> None:
    teams = snap.objects("teams", "team")
    if teams is None:
        out.unavailable = NOT_COLLECTED
        return
    status = _status(snap)
    parts = status.get("parts") if isinstance(status.get("parts"), dict) else {}
    out.parts = dict(parts)
    have_channels = parts.get("channels") == "read"
    have_members = have_channels and parts.get("members") == "read"
    have_apps = parts.get("apps") == "read"
    activity_read = status.get("activity") is True
    waiting = [p for p in status.get("waiting") or [] if isinstance(p, str)]
    notices = []
    if waiting:
        notices.append(waiting_text(snap.tenant["name"], waiting))
    if status.get("concealed"):
        notices.append(CONCEALED_NOTICE)
    out.notice = " ".join(notices)

    def missing(part: str, what: str) -> str:
        if parts.get(part) == "waiting":
            return f"{what} will show once the client approves the new permission."
        return f"{what} couldn't be read; see the Teams collection's log."

    def unread(part: str) -> tuple[str, str]:
        """A tile's value and foot for a part that wasn't read."""
        if parts.get(part) == "waiting":
            return "Waiting", "For the client's approval"
        return "Not read", "See the collection's log"

    every = Section(
        EVERY,
        [
            "Team",
            "Privacy",
            "Channels",
            "Private channels",
            "Shared channels",
            "Guests in channels",
            "From other organisations",
            "Apps",
            "Last activity",
            f"Active people ({INACTIVE_DAYS} days)",
            "Created",
        ],
        empty="This tenant has no teams.",
        drifts=(f"Active people ({INACTIVE_DAYS} days)",),
    )
    inactive = Section(
        INACTIVE,
        ["Team", "Last activity", "Privacy", "Channels", "Created"],
        empty=f"Every team older than {INACTIVE_DAYS} days was used in the last {INACTIVE_DAYS} days."
        if activity_read
        else "Microsoft's Teams activity report wasn't available, so inactive teams can't be told.",
    )
    channels = Section(
        CHANNELS,
        ["Team", "Channel", "Type", "Members", "Owners", "Guests", "From other organisations", "Created"],
        empty="No team has a private or shared channel." if have_channels else missing("channels", "Channels"),
    )
    people = Section(
        PEOPLE,
        ["Team", "Channel", "Channel type", "Person", "Email", "Who"],
        empty="No guests or people from other organisations are in a private or shared channel."
        if have_members
        else missing("members" if have_channels else "channels", "Who is in each private and shared channel"),
    )
    apps = Section(
        APPS,
        ["Team", "App", "Source", "Version"],
        empty="No apps are added to any team." if have_apps else missing("apps", "The apps in each team"),
    )
    n_private = n_shared = n_guests = n_external = n_custom = 0
    stale = 0
    for team in sorted(teams, key=lambda t: _text(t.get("displayName")).lower()):
        name = _text(team.get("displayName")) or "Unnamed team"
        privacy = PRIVACY.get(_text(team.get("visibility")), _text(team.get("visibility")))
        listed = [c for c in team.get("channels") or [] if isinstance(c, dict)] if have_channels else []
        read = isinstance(team.get("channels"), list)
        guests = external = 0
        for c in listed:
            kind = _text(c.get("type")) or "standard"
            if kind not in ("private", "shared"):
                continue
            n_private += kind == "private"
            n_shared += kind == "shared"
            members_read = c.get("membersRead") is True
            g, x = _count(c.get("guestCount")), _count(c.get("externalCount"))
            guests += g or 0
            external += x or 0
            no_owner = members_read and not c.get("ownerCount")
            channels.add(
                [
                    name,
                    _text(c.get("name")),
                    CHANNEL_TYPES.get(kind, kind),
                    _count(c.get("memberCount")) if members_read else "Not read",
                    _count(c.get("ownerCount")) if members_read else "",
                    g if members_read else "",
                    x if members_read else "",
                    _text(c.get("createdDateTime"))[:10],
                ],
                "medium" if no_owner else "low" if (g or x) else None,
            )
            for p in c.get("members") or [] if members_read else []:
                if not isinstance(p, dict) or not (p.get("external") or p.get("role") == "guest"):
                    continue
                people.add(
                    [
                        name,
                        _text(c.get("name")),
                        CHANNEL_TYPES.get(kind, kind),
                        _text(p.get("name")),
                        _text(p.get("email")),
                        EXTERNAL if p.get("external") else GUEST,
                    ],
                    "low",
                )
        n_guests += guests
        n_external += external
        team_apps = [a for a in team.get("apps") or [] if isinstance(a, dict)] if have_apps else []
        for a in team_apps:
            custom = a.get("distribution") == "sideloaded"
            n_custom += custom
            apps.add(
                [name, _text(a.get("name")), _text(a.get("source")) or "Unknown", _text(a.get("version"))],
                "medium" if custom else None,
            )
        why = _inactive(team, now, activity_read)
        stale += why is not None
        if why is not None:
            inactive.add(
                [name, why, privacy, len(listed) if read else "", _text(team.get("createdDateTime"))[:10]],
                "low",
            )
        every.add(
            [
                name,
                privacy,
                len(listed) if read else "",
                sum(c.get("type") == "private" for c in listed) if read else "",
                sum(c.get("type") == "shared" for c in listed) if read else "",
                guests if have_members and read else "",
                external if have_members and read else "",
                len(team_apps) if have_apps and isinstance(team.get("apps"), list) else "",
                _text(team.get("lastActivity")) or ("None recorded" if activity_read else ""),
                _count(team.get("activeUsers")),
                _text(team.get("createdDateTime"))[:10],
            ],
            "low" if why is not None else None,
        )

    n = len(teams)
    every.intro = (
        f"{plural(n, 'team')}. Guests and people from other organisations are counted in private and shared "
        "channels; a team's own guests are in the Groups and Teams report."
    )
    inactive.intro = (
        f"{plural(stale, 'team')} older than {INACTIVE_DAYS} days with no activity in the last {INACTIVE_DAYS} "
        f"days (Microsoft's Teams activity report runs a couple of days behind). Ask {verb(stale, 'its', 'their')} "
        "owners whether they're still needed; archiving a team in the Teams admin centre keeps its content "
        "read-only."
    )
    channels.intro = (
        "Private channels are only visible to their members. Shared channels can include people from other "
        "organisations, who never become guests here. A channel without an owner has nobody to manage its "
        "members."
    )
    people.intro = (
        "Guests (accounts invited into this tenant) and people from other organisations in each private and "
        "shared channel. Check each still needs access."
    )
    apps.intro = (
        "Apps added to each team. Custom uploads (sideloaded apps) bypass the Teams store and your "
        "organisation's app catalogue, so check each is expected."
    )
    if status.get("limit"):
        out.notes.append(f"Channels and apps were read for the first {status['limit']} teams only.")
    out.tiles = [
        Tile("Teams", n, "Teams in this tenant", rows=Rows(EVERY)),
        Tile(
            "Inactive teams",
            stale if activity_read else "Not read",
            f"No activity in {INACTIVE_DAYS} days" if activity_read else "Needs Microsoft's activity report",
            ("warn" if stale else "ok") if activity_read else None,
            Rows(INACTIVE),
        ),
        Tile(
            "Private and shared channels",
            n_private + n_shared if have_channels else unread("channels")[0],
            f"{n_private} private, {n_shared} shared" if have_channels else unread("channels")[1],
            rows=Rows(CHANNELS),
        ),
        Tile(
            "Guests and outsiders in channels",
            n_guests + n_external if have_members else unread("members" if have_channels else "channels")[0],
            f"{plural(n_guests, 'guest')}, {n_external} from other organisations"
            if have_members
            else unread("members" if have_channels else "channels")[1],
            ("warn" if n_guests + n_external else "ok") if have_members else None,
            Rows(PEOPLE),
        ),
        Tile(
            "Custom uploaded apps",
            n_custom if have_apps else unread("apps")[0],
            "Apps added outside the store and catalogue" if have_apps else unread("apps")[1],
            ("warn" if n_custom else "ok") if have_apps else None,
            Rows(APPS, {"Source": CUSTOM_UPLOAD}),
        ),
    ]
    out.sections = [inactive, channels, people, apps, every]
