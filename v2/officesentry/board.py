"""The office board (page in ``web/board.py``): every client on one dark screen for the office TV, the ones that
need someone first, refreshing itself.

Each client is a stamp: its grade, its open critical and high findings, and its Secure Score, or one word or two
when something needs someone (critical or high findings new in the last day, a collection that failed or is out
of date, access not granted yet, something due this week). Above the stamps, the day in five tiles: new critical
and high findings, clients read in the last 36 hours, what's due this week, lines nobody holds yet, and what was
fixed this week.

It's Today's ledger (``home.today``, over the last day) and the Clients page's rows
(``monthly_review.rollup_row``) drawn for a wall, so it reads stored state only, never Microsoft, and shows of
each client only what the person signed in may see there: ``shows(kind, tenant id)`` for the ledger's kinds of
line (findings, collections, what's due, what was fixed) and ``scores(tenant id)`` for the Secure Score.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import grades, home, upcoming
from .reports.monthly_review import rollup_row
from .text import plural

NEW_WITHIN = timedelta(days=1)  # "new" on the board: the last day, whoever looked since
FIXED_WITHIN = timedelta(days=7)
REFRESH = 300  # seconds between the page reloading itself
NAMED = 3  # clients named on a tile before "and N more"
SCORE = "secure_score.pct"
_ISO = "%Y-%m-%dT%H:%M:%SZ"

# Worst first, so a stamp's place follows its grade when nothing else sets it apart.
_GRADE_RANK = {"E": 0, "D": 1, "C": 2, "B": 3, "A": 4}

# What the Due tile counts each kind of Coming up item as.
_DUE_NOUNS = {
    upcoming.SUBSCRIPTION: "renewal",
    upcoming.TRIAL: "trial",
    upcoming.APP_KEY_TYPE: "app secret",
    upcoming.APP_CERTIFICATE: "app certificate",
    upcoming.QUOTA: "mailbox",
    upcoming.GROUP: "group",
    upcoming.DMARC: "domain",
    upcoming.RISK: "accepted risk",
}

# A stamp's place, before its grade: new critical findings, then a client that can't be read, new high findings,
# anything else that needs someone, and last the quiet ones.
NEW_CRITICAL, NOT_READ, NEW_HIGH, OTHER, QUIET = range(5)


@dataclass
class Stamp:
    tenant: dict
    findings: bool  # the person sees this client's findings, so its grade and counts show
    grade: str | None = None
    critical: int = 0
    high: int = 0
    score: float | None = None  # Secure Score %, when the person sees it
    word: str = ""  # what needs someone, in a word or two; nothing when nothing does
    tone: str = ""  # the word's colour: critical | high | bad | now | warn
    flagged: bool = False  # its collection failed, so what it shows is old
    rank: int = QUIET

    @property
    def quiet(self) -> bool:
        return not self.word

    @property
    def order(self) -> tuple:
        return (
            self.rank,
            _GRADE_RANK.get(self.grade or "", len(_GRADE_RANK)),
            -self.critical,
            -self.high,
            self.score if self.score is not None else 101,
            self.tenant["name"].lower(),
        )


@dataclass
class Tile:
    label: str
    value: str
    foot: str
    tone: str = ""  # bad | warn | ok, or nothing


@dataclass
class Board:
    stamps: list[Stamp]
    tiles: list[Tile] = field(default_factory=list)

    @property
    def quiet(self) -> int:
        return sum(s.quiet for s in self.stamps)


def _named(names: list[str]) -> str:
    """'A, B, C and 2 more'."""
    shown = names[:NAMED]
    rest = len(names) - len(shown)
    return home.join(shown + ([f"{rest} more"] if rest else []))


def _word(stamp: Stamp, entries: list[home.Entry], stale: bool) -> None:
    """The stamp's word, colour and place, from the client's lines on the ledger: the one that matters most."""
    new = [e for e in entries if e.kind == "finding" and e.new]
    critical = sum(e.count for e in new if e.level == "critical")
    high = sum(e.count for e in new if e.level == "high")
    reading = next((e for e in entries if e.kind in ("collection", "access")), None)
    due = sorted((e for e in entries if e.kind == "due"), key=lambda e: e.order)
    stamp.flagged = bool(reading and reading.level == "bad")
    if critical:
        stamp.word, stamp.tone, stamp.rank = f"{critical + high} new", "critical", NEW_CRITICAL
    elif stamp.flagged:
        stamp.word, stamp.tone, stamp.rank = "Not read", "bad", NOT_READ
    elif high:
        stamp.word, stamp.tone, stamp.rank = f"{high} new", "high", NEW_HIGH
    elif reading:
        stamp.word = "No access yet" if reading.kind == "access" else reading.word  # Out of date, Retrying
        stamp.tone, stamp.rank = "warn", OTHER
    elif due:
        first = due[0]
        stamp.word = {"Now": "Due now", "This week": "Due this week"}.get(first.word, first.word)  # Due today, ...
        stamp.tone, stamp.rank = ("now" if first.level == "now" else "warn"), OTHER
    elif stale:
        # Nothing read lately and no line says why (the person doesn't see collections, or the first is to come).
        stamp.word, stamp.tone, stamp.rank = "No recent data", "warn", OTHER


def _fixed(conn, tenant_ids: list[int], now: datetime) -> tuple[int, int]:
    """Critical and high findings a collection no longer saw this week, and how many clients they were on."""
    if not tenant_ids:
        return 0, 0
    marks = ",".join("?" * len(tenant_ids))
    row = conn.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT tenant_id) FROM findings WHERE tenant_id IN ({marks})"
        " AND resolved_at >= ? AND severity IN ('critical', 'high')",
        (*tenant_ids, (now - FIXED_WITHIN).strftime(_ISO)),
    ).fetchone()
    return row[0], row[1]


def _tiles(
    conn, day: home.Today, stamps: list[Stamp], shows: Callable[[str, int], bool], now: datetime, since
) -> list[Tile]:
    tiles = []
    if "finding" in day.sees:
        names = [s.tenant["name"] for s in stamps if s.rank in (NEW_CRITICAL, NEW_HIGH)]
        tiles.append(
            Tile(
                "New critical and high",
                str(day.new_severe),
                _named(names) if names else "nothing new in the last day",
                "bad" if day.new_severe else "",
            )
        )
    if "collection" in day.sees:
        read = [s for s in stamps if shows("collection", s.tenant["id"])]
        behind = {e.tenant["id"] for e in day.entries if e.kind in ("collection", "access")}
        behind |= {s.tenant["id"] for s in read if s.word == "No recent data"}  # the first collection is to come
        failed = {e.tenant["id"] for e in day.entries if e.kind == "collection" and e.level == "bad"}
        names = [s.tenant["name"] for s in read if s.tenant["id"] in behind]
        tiles.append(
            Tile(
                "Read in the last 36 hours",
                f"{len(read) - len(names)} of {len(read)}",
                f"{_named(names)} not read" if names else "every client",
                "bad" if failed else ("warn" if names else ""),
            )
        )
    if "due" in day.sees:
        kinds: dict[str, int] = {}
        for e in day.entries:
            if e.kind == "due":
                kind = e.key.split(":")[1]  # due:<kind> or due:<kind>:<item>
                kinds[kind] = kinds.get(kind, 0) + e.count
        parts = [plural(n, _DUE_NOUNS.get(k, "item")) for k, n in sorted(kinds.items(), key=lambda kv: -kv[1])]
        tiles.append(
            Tile("Due this week", str(day.due), home.join(parts) if parts else "nothing", "warn" if day.due else "")
        )
    open_lines = [e for e in day.entries if e.acts]
    if open_lines:
        nobody = [e for e in open_lines if not e.handed_to]
        oldest = min((e.when for e in nobody), default=None)
        tiles.append(
            Tile(
                "Not handed to anyone",
                str(len(nobody)),
                since(oldest) if oldest else "every line has someone",
            )
        )
    if "fixed" in day.sees:
        count, clients = _fixed(conn, [s.tenant["id"] for s in stamps if shows("fixed", s.tenant["id"])], now)
        tiles.append(
            Tile(
                "Fixed this week",
                str(count),
                f"across {plural(clients, 'client')}" if count else "in the last 7 days",
                "ok" if count else "",
            )
        )
    return tiles


def build(
    conn,
    tenants,
    now: datetime | None = None,
    shows: Callable[[str, int], bool] | None = None,
    scores: Callable[[int], bool] | None = None,
    since: Callable[[datetime], str] | None = None,
) -> Board:
    """The board over ``tenants`` (paused and archived ones are left out). ``since(when)`` words the oldest line
    nobody holds ("oldest since 06:38"); the page gives it the MSP's timezone."""
    now = now or datetime.now(UTC)
    shows = shows or (lambda kind, tenant_id: True)
    scores = scores or (lambda tenant_id: True)
    since = since or (lambda when: f"oldest since {when:%H:%M}")
    day = home.today(conn, tenants, now, shows=shows, since=now - NEW_WITHIN, since_label="the last day")
    active = [dict(t) for t in tenants if t["enabled"] and not t["archived_at"]]
    lines: dict[int, list[home.Entry]] = {}
    for e in day.entries:
        lines.setdefault(e.tenant["id"], []).append(e)
    letters = grades.stored_letters(conn, [t["id"] for t in active if shows("finding", t["id"])])
    stamps = []
    for t in active:
        tid = t["id"]
        stamp = Stamp(t, shows("finding", tid))
        row = rollup_row(conn, t, cached=True) if stamp.findings or scores(tid) else None
        if row and stamp.findings:
            stamp.grade = letters.get(tid)
            stamp.critical, stamp.high = row["counts"].get("critical", 0), row["counts"].get("high", 0)
        if row and scores(tid) and SCORE in row["kpis"]:
            stamp.score = row["kpis"][SCORE].value
        _word(stamp, lines.get(tid, []), bool(row and row["stale_data"]))
        stamps.append(stamp)
    stamps.sort(key=lambda s: s.order)
    return Board(stamps, _tiles(conn, day, stamps, shows, now, since))
