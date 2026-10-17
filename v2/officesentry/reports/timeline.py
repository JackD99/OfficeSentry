"""A client's timeline: the year as a story, month by month, from what Office Sentry already keeps.

Nothing here calls Microsoft. Every entry comes from a table the portal already writes: the findings
history (found, fixed, came back, accepted), the activity history's admin changes (``adminactivity``),
the reports issued (``issued``) and approved (``report_reviews``), the month-end review's notes and next
steps, failed collections (``runs``) and the client's own milestones (``tenants``, ``permission_requests``,
``audit_feeds``). The twelve-month measures are read from ``metrics`` and from the findings table as it
stood at each month's end.

Someone sees only the parts whose report they open on this client, as What changed does
(``month_changes.only``): findings, notes and the reports with findings (``FINDINGS``), admin changes with
the Admin activity report, each measure with its report (``FIGURE_REPORTS``), failed collections with
``collect`` and staff-only notes with ``month_end.review``. The web layer passes those in as ``opens``,
``collects`` and ``reviews``.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from .. import adminactivity, issued, store
from ..checks import RULES
from ..collectors import REGISTRY
from ..formatting import parse
from ..permission_batch import BATCHES
from ..text import plural, subject_label
from . import charts, designs, findings_report
from .month_changes import FIGURE_REPORTS, FINDINGS, SEV_RANK
from .tables import Table

log = logging.getLogger(__name__)

DEFAULT_MONTHS = 12
PERIODS = (12, 24, 0)  # months back the page can show; 0 is everything kept
MAX_ENTRIES = 500  # the page keeps this many entries, newest first, and says when it cut
MAX_MEASURE_MONTHS = 36
ISO = "%Y-%m-%dT%H:%M:%SZ"
ADMIN_REPORT = "admin-activity"  # what ``opens`` is asked for admin changes
RULE_TITLES = {r.key: r.title for r in RULES}

# The groups the page filters by, in the order of its chips. An entry's ``group`` is one of these keys.
GROUPS = (
    ("findings", "Findings"),
    ("admin", "Admin changes"),
    ("reports", "Reports"),
    ("notes", "Notes"),
    ("collection", "Collection"),
)
GROUP_TITLES = dict(GROUPS)

# What each kind of entry is called on the page, before its title.
KIND_LABELS = {
    "first": "Findings",
    "finding": "Finding",
    "fixed": "Finding",
    "reopened": "Came back",
    "accepted": "Accepted risk",
    "admin": "Admin change",
    "report": "Report",
    "approved": "Report",
    "note": "Note",
    "action": "Next step",
    "collection": "Collection",
    "client": "Client",
}

# Admin changes worth telling a client about over a year (``adminactivity.AREAS`` kinds): admin roles,
# Conditional Access and authentication policies, app consent and credentials, domains and federation,
# partners. Role activations, app role grants, password resets and deleted accounts are the Admin activity
# report's to list in full.
TOLD_KINDS = frozenset(
    {"role_grant", "role_removal", "ca", "auth_policy", "consent", "app_credential", "federation", "domain", "partner"}
)

# The twelve-month measures: (metric key, or "open_severe" from the findings table; label; unit; which
# direction is good). A metric's measure needs its report (``FIGURE_REPORTS``), the findings one findings.
MEASURES = (
    ("secure_score.pct", "Secure Score", "%", "up"),
    ("mfa.registered_pct", "Users with MFA", "%", "up"),
    ("open_severe", "Open critical and high findings", "", "down"),
    ("licences.unassigned_paid", "Unassigned paid licences", "", "down"),
)
OPEN_SEVERE = "open_severe"
SEVERE = ("critical", "high")

SOURCES = (
    "Built from what Office Sentry keeps for this client: the findings history, admin changes from the activity "
    "history, reports sent and approved, the month-end review's notes and next steps, failed collections and the "
    "client's own settings. Nothing is read from Microsoft for it."
)


@dataclass
class Entry:
    """One dated line: a finding found or fixed, an admin change, a report sent, a note, a failed night."""

    at: datetime
    group: str  # a GROUPS key
    kind: str  # a KIND_LABELS key
    title: str
    detail: str = ""
    level: str = ""  # a severity (critical, high, medium, low) or a tone (ok, warn, bad); "" for none
    word: str = ""  # the word beside the level's square ("high", "fixed", "worth a look", "failed")
    target: str = ""  # where the title opens, for the web layer to turn into a link (``href``)
    href: str = ""
    until: datetime | None = None  # the last day of a run of days (collection failed three nights running)
    count: int = 1  # things this line stands for (findings in a group), for the month's summary

    @property
    def day(self) -> date:
        return self.at.date()

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind.capitalize())

    @property
    def when(self) -> str:
        """The day as the page shows it beside the line, inside its month: "3 Oct", or "3 to 5 Oct" for a run."""
        if self.until is None or self.until.date() == self.day:
            return f"{self.at.day} {self.at:%b}"
        if self.until.month == self.at.month:
            return f"{self.at.day} to {self.until.day} {self.at:%b}"
        return f"{self.at.day} {self.at:%b} to {self.until.day} {self.until:%b}"

    @property
    def dates(self) -> str:
        """The day in full for downloads: "2026-10-03", or "2026-10-03 to 2026-10-05"."""
        if self.until is None or self.until.date() == self.day:
            return self.day.isoformat()
        return f"{self.day.isoformat()} to {self.until.date().isoformat()}"


@dataclass
class Month:
    start: date
    entries: list[Entry] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)  # "7 new findings", "Secure Score 38% → 47.5%"
    current: bool = False  # the month in progress

    @property
    def key(self) -> str:
        return self.start.strftime("%Y-%m")

    @property
    def end(self) -> date:
        """The month's last day."""
        return _add_months(self.start, 1) - timedelta(days=1)

    @property
    def label(self) -> str:
        return f"{self.start:%B %Y}" + (" so far" if self.current else "")


@dataclass
class Measure:
    """One number at each month's end: the latest metric captured in the month, or the findings open then."""

    key: str
    label: str
    unit: str
    better: str | None
    months: list[date]
    values: list[float | None]  # one per month, oldest first; None where nothing was captured
    svg: str = ""

    @property
    def points(self) -> list[tuple[date, float]]:
        return [(m, v) for m, v in zip(self.months, self.values, strict=True) if v is not None]

    @property
    def latest(self) -> float | None:
        return self.points[-1][1] if self.points else None

    @property
    def first(self) -> float | None:
        return self.points[0][1] if self.points else None

    @property
    def delta(self) -> float | None:
        if len(self.points) < 2:
            return None
        return round(self.points[-1][1] - self.points[0][1], 1)

    @property
    def trend(self) -> str:
        d = self.delta
        if not d or self.better is None:
            return "flat"
        return "better" if (d > 0) == (self.better == "up") else "worse"

    @property
    def since(self) -> str:
        """The month of the first value, as the page says it: "Jul 2026"."""
        return f"{self.points[0][0]:%b %Y}" if self.points else ""

    def text(self, value: float | None) -> str:
        return "–" if value is None else f"{value:g}{self.unit}"


@dataclass
class Timeline:
    tenant: dict
    now: datetime
    months_back: int  # a PERIODS value
    since: date  # the first day of the period: the measures' first month
    kept_since: date  # the first month anything is recorded for this client
    months: list[Month]  # newest first; only months with something to say
    quiet: list[date]  # months since ``kept_since`` with nothing recorded, oldest first
    counts: Counter  # entries per group in the period, before the page's filter
    total: int
    show: str  # the group the page is filtered to, or ""
    measures: list[Measure]
    left_out: list[str]  # the parts this person can't open on this client, in words
    truncated: int = 0  # entries beyond MAX_ENTRIES, left out
    sources: str = SOURCES

    @property
    def shown(self) -> int:
        return sum(len(m.entries) for m in self.months)

    @property
    def empty(self) -> bool:
        return not self.total and not any(m.points for m in self.measures)

    @property
    def kept_text(self) -> str:
        return f"{self.kept_since:%b %Y}"

    @property
    def quiet_text(self) -> str:
        """The quiet months as runs: "Nov 2025 to Feb 2026, Apr 2026"."""
        runs: list[list[date]] = []
        for m in self.quiet:
            if runs and _add_months(runs[-1][-1], 1) == m:
                runs[-1].append(m)
            else:
                runs.append([m])
        return ", ".join(f"{r[0]:%b %Y}" if len(r) == 1 else f"{r[0]:%b %Y} to {r[-1]:%b %Y}" for r in runs)


# -- Dates -----------------------------------------------------------------------------------------


def _add_months(d: date, n: int) -> date:
    month = d.month - 1 + n
    return date(d.year + month // 12, month % 12 + 1, 1)


def _month_start(value: datetime | date) -> date:
    return date(value.year, value.month, 1)


def _iso(d: date) -> str:
    return datetime(d.year, d.month, d.day, tzinfo=UTC).strftime(ISO)


def _at(value) -> datetime | None:
    return parse(value)


def _short(text: str | None, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _names(items: list[str], keep: int = 2) -> str:
    """ "a, b and 3 more" for a list of names."""
    items = [subject_label(str(i)) for i in items if i]
    if len(items) <= keep + 1:
        return ", ".join(items[:-1]) + (" and " if len(items) > 1 else "") + (items[-1] if items else "")
    return ", ".join(items[:keep]) + f" and {len(items) - keep} more"


# -- Sources ---------------------------------------------------------------------------------------


def _finding_entries(rows: list) -> list[Entry]:
    """Found (grouped per day and check, the first collection's as one line), fixed, came back and accepted."""
    out: list[Entry] = []
    if not rows:
        return out
    first_day = min(r["first_seen"][:10] for r in rows)
    first_look = [r for r in rows if r["first_seen"][:10] == first_day]
    if first_look:
        by_sev = Counter(r["severity"] for r in first_look)
        checks = len({r["rule_key"] for r in first_look})
        out.append(
            Entry(
                _at(min(r["first_seen"] for r in first_look)),
                "findings",
                "first",
                f"First look: {plural(len(first_look), 'finding')} across {plural(checks, 'check')}",
                ", ".join(f"{by_sev[s]} {s}" for s in SEV_RANK if by_sev.get(s)),
                target="findings",
                count=len(first_look),
            )
        )
    groups: dict[tuple, list] = {}
    for r in rows:
        if r["first_seen"][:10] != first_day:
            groups.setdefault(("finding", r["first_seen"][:10], r["rule_key"]), []).append(r)
        if r["reopened_at"] and r["reopened_at"] > r["first_seen"]:
            groups.setdefault(("reopened", r["reopened_at"][:10], r["rule_key"]), []).append(r)
        if r["resolved_at"]:
            groups.setdefault(("fixed", r["resolved_at"][:10], r["rule_key"]), []).append(r)
    stamp = {"finding": "first_seen", "reopened": "reopened_at", "fixed": "resolved_at"}
    for (kind, _, rule_key), found in groups.items():
        worst = min(found, key=lambda r: SEV_RANK.get(r["severity"], 9))["severity"]
        at = _at(min(r[stamp[kind]] for r in found))
        detail = _names([r["subject"] for r in found])
        if kind == "fixed":
            days = sorted((_at(r["resolved_at"]) - _at(r["first_seen"])).days for r in found if _at(r["first_seen"]))
            if days:
                span = f"{days[0]}" if days[0] == days[-1] else f"{days[0]} to {days[-1]}"
                detail += f" · open {span} days" if days[-1] != 1 else " · open 1 day"
            level, word = "ok", "fixed"
        else:
            level, word = worst, worst
        out.append(
            Entry(
                at,
                "findings",
                kind,
                RULE_TITLES.get(rule_key, rule_key),
                detail,
                level,
                word,
                target="resolved" if kind == "fixed" else "findings",
                count=len(found),
            )
        )
    for r in rows:
        if not r["accepted_at"]:
            continue
        parts = [subject_label(r["subject"]), f"by {r['accepted_by']}" if r["accepted_by"] else ""]
        if r["accepted_until"]:
            parts.append(f"until {_day_text(r['accepted_until'])}")
        if r["accepted_note"]:
            parts.append(_short(r["accepted_note"], 120))
        out.append(
            Entry(
                _at(r["accepted_at"]),
                "findings",
                "accepted",
                RULE_TITLES.get(r["rule_key"], r["rule_key"]),
                " · ".join(p for p in parts if p),
                target="findings",
            )
        )
    return [e for e in out if e.at]


def _day_text(iso: str) -> str:
    at = _at(iso)
    return f"{at.day} {at:%b %Y}" if at else iso


def _admin_entries(conn, tenant_id: int, since: datetime, now: datetime) -> list[Entry]:
    found = adminactivity.from_history(conn, tenant_id, since, now)
    if found is None:
        return []
    out = []
    for e in found[0]:
        if e.get("kind") not in TOLD_KINDS or e.get("result") != "success":
            continue
        at = _at(e.get("time"))
        if not at:
            continue
        targets = ", ".join(e.get("targets") or [])
        if e.get("more_targets"):
            targets += f" and {e['more_targets']} more"
        review = adminactivity.review(e)
        parts = [e.get("detail") or "", f"by {e['actor']}" if e.get("actor") else ""]
        if review:
            parts.append(review[1])
        out.append(
            Entry(
                at,
                "admin",
                "admin",
                e["activity"] + (f": {targets}" if targets else ""),
                " · ".join(p for p in parts if p),
                review[0] if review else "",
                "worth a look" if review else "",
                target=ADMIN_REPORT,
            )
        )
    return out


def _file_opens(conn, kind: str, opens: Callable[[str], bool]) -> bool:
    """Whether someone who opens ``opens(<report key>)`` may see an issued file of this kind: a detail report or
    a saved view needs its report, the reviews, evidence and Findings need findings, and a design those and
    the detail reports it shows (as ``web/email._needs`` decides for the Report history page)."""
    report = kind.removeprefix("view:").removeprefix("data:")
    if report != kind and report not in findings_report.TITLES:
        return opens(report)
    if not opens(FINDINGS):
        return False
    if (design_id := designs.id_of(kind)) is not None and (row := designs.get(conn, design_id)) is not None:
        return all(opens(k) for k in designs.detail_reports(row))
    return True


def _report_entries(conn, tenant_id: int, opens: Callable[[str], bool]) -> list[Entry]:
    out = []
    for send in issued.history(conn, tenant_id):
        at = _at(send["issued_at"])
        files = [f for f in send["files"] if _file_opens(conn, f["kind"], opens)]
        if not at or not files:
            continue
        titles = list(dict.fromkeys(f["title"] for f in files))
        to = [r for r in (send["recipients"] or "").split(",") if r.strip()]
        parts = [send["period_label"] or ""]
        if to:
            parts.append(f"sent to {plural(len(to), 'recipient')}")
        parts.append(f"by {send['issued_by']}" if send["issued_by"] else "by the schedule")
        if send["version"] > 1:
            parts.append(f"version {send['version']}")
        if send["reason"]:
            parts.append(_short(send["reason"], 120))
        out.append(
            Entry(
                at,
                "reports",
                "report",
                f"{_names(titles)} {'sent' if to else 'issued'}",
                " · ".join(p for p in parts if p),
                target="report-history",
                count=len(files),
            )
        )
    if opens(FINDINGS):
        for r in conn.execute(
            "SELECT period, approved_by, approved_at, commentary FROM report_reviews"
            " WHERE tenant_id = ? AND approved_at IS NOT NULL",
            (tenant_id,),
        ):
            at = _at(r["approved_at"])
            if not at:
                continue
            parts = [f"by {r['approved_by']}" if r["approved_by"] else "", _short(r["commentary"], 140)]
            out.append(
                Entry(
                    at,
                    "reports",
                    "approved",
                    f"{issued.period_label(r['period'])} report approved",
                    " · ".join(p for p in parts if p),
                    target=f"review:{r['period']}",
                )
            )
    return out


def _note_entries(conn, tenant_id: int, findings: bool, reviews: bool) -> list[Entry]:
    out = []
    if findings or reviews:
        for n in conn.execute("SELECT * FROM finding_notes WHERE tenant_id = ?", (tenant_id,)):
            staff = n["visibility"] == "staff"
            if (staff and not reviews) or (not staff and not findings):
                continue
            at = _at(n["created_at"])
            if not at:
                continue
            title = f"Note on {RULE_TITLES.get(n['rule_key'], n['rule_key'])}"
            if n["subject"]:
                title += f": {subject_label(n['subject'])}"
            parts = [
                _short(n["body"]),
                f"by {n['created_by']}" if n["created_by"] else "",
                "staff only" if staff else "",
            ]
            out.append(Entry(at, "notes", "note", title, " · ".join(p for p in parts if p), target="findings"))
    if findings:
        for a in conn.execute("SELECT * FROM report_actions WHERE tenant_id = ?", (tenant_id,)):
            at = _at(a["created_at"])
            if not at:
                continue
            parts = [_short(a["detail"]), f"by {a['created_by']}" if a["created_by"] else ""]
            period = (a["target_month"] or a["created_at"])[:7]
            out.append(
                Entry(
                    at,
                    "notes",
                    "action",
                    f"Recommended: {a['title']}",
                    " · ".join(p for p in parts if p),
                    target=f"review:{period}",
                )
            )
            done = _at(a["done_at"]) if a["status"] == "done" else None
            if done:
                out.append(
                    Entry(
                        done,
                        "notes",
                        "action",
                        f"Done: {a['title']}",
                        level="ok",
                        word="done",
                        target=f"review:{period}",
                    )
                )
    return out


def _collection_entries(conn, tenant_id: int, since_iso: str) -> list[Entry]:
    """Failed collections, one line per run of nights with the same data sources failing."""
    days: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT id, collector, error, COALESCE(finished_at, created_at) AS at FROM runs"
        " WHERE tenant_id = ? AND status = 'failed' AND COALESCE(finished_at, created_at) >= ? ORDER BY at",
        (tenant_id, since_iso),
    ):
        slot = days.setdefault(r["at"][:10], {"collectors": set(), "error": "", "run": r["id"], "at": r["at"]})
        slot["collectors"].add(r["collector"])
        if r["error"] and not slot["error"]:
            slot["error"] = r["error"]
    runs: list[dict] = []
    for day in sorted(days):
        slot = days[day]
        last = runs[-1] if runs else None
        if (
            last
            and last["collectors"] == slot["collectors"]
            and _at(last["until"]).date() + timedelta(days=1) == _at(slot["at"]).date()
        ):
            last["until"], last["nights"] = slot["at"], last["nights"] + 1
        else:
            runs.append({**slot, "until": slot["at"], "nights": 1})
    out = []
    for run in runs:
        at, until = _at(run["at"]), _at(run["until"])
        if not at:
            continue
        names = _names(sorted(REGISTRY[k].title if k in REGISTRY else k for k in run["collectors"]), 3)
        parts = [f"{run['nights']} nights running" if run["nights"] > 1 else "", _short(run["error"], 140)]
        out.append(
            Entry(
                at,
                "collection",
                "collection",
                f"Collection failed: {names}",
                " · ".join(p for p in parts if p),
                level="bad",
                word="failed",
                target=f"run:{run['run']}",
                until=until if until and until.date() != at.date() else None,
                count=run["nights"],
            )
        )
    return out


def _client_entries(conn, tenant: dict) -> list[Entry]:
    """The client's milestones in Office Sentry: added, access granted, paused, archived, new permissions."""
    out = []

    def add(when, title, detail="", level="", word="", target="manage"):
        at = _at(when)
        if at:
            out.append(Entry(at, "collection", "client", title, detail, level, word, target))

    add(tenant.get("created_at"), "Added to Office Sentry")
    add(
        tenant.get("consented_at"),
        "Access granted",
        "The client approved Office Sentry's read-only access, so collection could start.",
    )
    add(tenant.get("sign_in_approved_at"), "Client sign-in approved", "The client's own people can sign in.")
    add(
        tenant.get("paused_at"),
        "Collection paused",
        "No collections or emails while paused; the data stays.",
        "warn",
        "paused",
    )
    add(tenant.get("archived_at"), "Archived", "Hidden from lists and paused.", "warn", "archived")
    titles = {b.release: b.title for b in BATCHES}
    for r in conn.execute(
        "SELECT release, approved_at FROM permission_requests WHERE tenant_id = ? AND approved_at IS NOT NULL",
        (tenant["id"],),
    ):
        add(r["approved_at"], "New permissions approved", titles.get(r["release"], f"Release {r['release']}"))
    feed = conn.execute("SELECT * FROM audit_feeds WHERE tenant_id = ?", (tenant["id"],)).fetchone()
    if feed:
        add(feed["started_at"], "Audit log feed started", "Office 365 activity is kept from here on.")
        add(feed["stopped_at"], "Audit log feed stopped", _short(feed["stop_reason"], 120), "warn", "stopped")
    return out


# -- Measures --------------------------------------------------------------------------------------


def _open_severe(rows: list, end: datetime) -> int | None:
    """Critical and high findings open at ``end`` (found before it, not fixed or accepted by then); None before
    the first collection."""
    end_iso = end.strftime(ISO)
    if not any(r["first_seen"] < end_iso for r in rows):
        return None
    return sum(
        1
        for r in rows
        if r["severity"] in SEVERE
        and r["first_seen"] < end_iso
        and (not r["resolved_at"] or r["resolved_at"] >= end_iso)
        and not (
            r["accepted_at"]
            and r["accepted_at"] < end_iso
            and (not r["accepted_until"] or r["accepted_until"] >= end_iso)
        )
    )


def _measures(conn, tenant_id: int, months: list[date], now: datetime, opens, findings: list) -> list[Measure]:
    months = months[-MAX_MEASURE_MONTHS:]
    ends = [min(datetime.combine(_add_months(m, 1), datetime.min.time(), tzinfo=UTC), now) for m in months]
    out = []
    for key, label, unit, better in MEASURES:
        if key == OPEN_SEVERE:
            if not opens(FINDINGS):
                continue
            values: list[float | None] = [_open_severe(findings, end) for end in ends]
        else:
            if not opens(FIGURE_REPORTS[key]):
                continue
            history = store.metric_history(conn, tenant_id, key, since=_iso(months[0]))
            values = []
            for m, end in zip(months, ends, strict=True):
                start = _iso(m)
                in_month = [h["value"] for h in history if start <= h["captured_at"] < end.strftime(ISO)]
                values.append(in_month[-1] if in_month else None)
        measure = Measure(key, label, unit, better, months, values)
        measure.svg = charts.month_line(months, values, label=f"{label} by month", unit=unit)
        out.append(measure)
    return out


# -- Months and summaries --------------------------------------------------------------------------


def _summary(entries: list[Entry], metrics: dict[str, list[float]], opens) -> list[str]:
    """The month in one line of parts: findings found and fixed, Secure Score then and now, admin changes,
    reports sent, failed nights. From every entry of the month, whatever the page is filtered to."""
    parts = []
    if opens(FINDINGS):
        new = sum(e.count for e in entries if e.kind == "finding")
        fixed = sum(e.count for e in entries if e.kind == "fixed")
        back = sum(e.count for e in entries if e.kind == "reopened")
        first = next((e for e in entries if e.kind == "first"), None)
        if first:
            parts.append(f"first look: {plural(first.count, 'finding')}")
        if new:
            parts.append(plural(new, "new finding"))
        if fixed:
            parts.append(f"{fixed} fixed")
        if back:
            parts.append(f"{back} came back")
    for key, label in (("secure_score.pct", "Secure Score"), ("mfa.registered_pct", "MFA")):
        values = metrics.get(key) or []
        if len(values) >= 2 and values[0] != values[-1] and opens(FIGURE_REPORTS[key]):
            parts.append(f"{label} {values[0]:g}% → {values[-1]:g}%")
    admin = [e for e in entries if e.kind == "admin"]
    if admin:
        worth = sum(1 for e in admin if e.level)
        parts.append(plural(len(admin), "admin change") + (f", {worth} worth a look" if worth else ""))
    sent = sum(1 for e in entries if e.kind == "report")
    if sent:
        parts.append(plural(sent, "report") + " sent")
    nights = sum(e.count for e in entries if e.kind == "collection")
    if nights:
        parts.append(f"collection failed {plural(nights, 'night')}")
    client = [e.title[0].lower() + e.title[1:] for e in entries if e.kind == "client"]
    if client:
        parts.append(_names(client, 2))
    if parts:  # a line of fragments, so only its first letter is capitalised
        parts[0] = parts[0][0].upper() + parts[0][1:]
    return parts


def _month_metrics(conn, tenant_id: int, since_iso: str) -> dict[str, dict[str, list[float]]]:
    """Secure Score and MFA values captured in each month, oldest first: ``{"2026-10": {key: [values]}}``."""
    out: dict[str, dict[str, list[float]]] = {}
    for key in ("secure_score.pct", "mfa.registered_pct"):
        for h in store.metric_history(conn, tenant_id, key, since=since_iso):
            out.setdefault(h["captured_at"][:7], {}).setdefault(key, []).append(h["value"])
    return out


def _kept_since(conn, tenant: dict, now: datetime) -> date:
    """The first month anything is recorded for this client: added, a finding, a metric or a run."""
    firsts = [tenant.get("created_at")]
    for sql in (
        "SELECT MIN(first_seen) FROM findings WHERE tenant_id = ?",
        "SELECT MIN(captured_at) FROM metrics WHERE tenant_id = ?",
        "SELECT MIN(created_at) FROM runs WHERE tenant_id = ?",
    ):
        firsts.append(conn.execute(sql, (tenant["id"],)).fetchone()[0])
    earliest = min((_at(f) for f in firsts if f), default=None)
    return _month_start(earliest) if earliest and earliest < now else _month_start(now)


def build(
    conn,
    tenant,
    *,
    months: int = DEFAULT_MONTHS,
    show: str = "",
    opens: Callable[[str], bool] = lambda key: True,
    collects: bool = True,
    reviews: bool = True,
    now: datetime | None = None,
) -> Timeline:
    """A tenant's timeline over the last ``months`` months (0 for everything kept), filtered to one group when
    ``show`` names one. ``opens(<report key or FINDINGS>)``, ``collects`` and ``reviews`` say what this person
    may see on this client (see the module's docstring). A source that fails on odd data is logged and left
    out; it never blanks the page."""
    now = now or datetime.now(UTC)
    tenant = dict(tenant)
    tid = tenant["id"]
    months = months if months in PERIODS else DEFAULT_MONTHS
    show = show if show in GROUP_TITLES else ""
    this_month = _month_start(now)
    kept = _kept_since(conn, tenant, now)
    since = _add_months(this_month, 1 - months) if months else kept
    since_dt = datetime(since.year, since.month, 1, tzinfo=UTC)
    since_iso = since_dt.strftime(ISO)
    findings = conn.execute("SELECT * FROM findings WHERE tenant_id = ?", (tid,)).fetchall() if opens(FINDINGS) else []

    entries: list[Entry] = []
    sources: list[tuple[str, Callable[[], list[Entry]]]] = [
        ("findings", lambda: _finding_entries(findings)),
        ("reports", lambda: _report_entries(conn, tid, opens)),
        ("notes", lambda: _note_entries(conn, tid, opens(FINDINGS), reviews)),
        ("client", lambda: _client_entries(conn, tenant)),
    ]
    if opens(ADMIN_REPORT):
        sources.append(("admin changes", lambda: _admin_entries(conn, tid, since_dt, now)))
    if collects:
        sources.append(("collections", lambda: _collection_entries(conn, tid, since_iso)))
    for name, source in sources:
        try:
            entries.extend(source())
        except Exception:  # one source's odd data mustn't blank the whole year
            log.exception("Timeline %s failed for tenant %s", name, tid)
    entries = [e for e in entries if e.at >= since_dt and e.at <= now + timedelta(days=1)]
    if entries:  # a milestone before the first collection (access granted months earlier) is kept too
        kept = min(kept, _month_start(min(e.at for e in entries)))
    counts = Counter(e.group for e in entries)
    total = len(entries)

    month_starts = []  # the period's months, oldest first, for the measures
    m = since
    while m <= this_month:
        month_starts.append(m)
        m = _add_months(m, 1)
    listed = [m for m in month_starts if m >= kept]  # the months the story lists: nothing before the client
    metrics = _month_metrics(conn, tid, since_iso)
    by_month: dict[str, list[Entry]] = {}
    for e in entries:
        by_month.setdefault(e.at.strftime("%Y-%m"), []).append(e)

    shown = [e for e in entries if not show or e.group == show]
    shown.sort(key=lambda e: e.at, reverse=True)
    truncated = max(len(shown) - MAX_ENTRIES, 0)
    shown = shown[:MAX_ENTRIES]
    shown_by_month: dict[str, list[Entry]] = {}
    for e in shown:
        shown_by_month.setdefault(e.at.strftime("%Y-%m"), []).append(e)

    months_out, quiet = [], []
    for start in reversed(listed):
        month = Month(start, current=start == this_month)
        key = month.key
        month.summary = _summary(by_month.get(key, []), metrics.get(key, {}), opens)
        month.entries = shown_by_month.get(key, [])
        if month.entries or month.summary:
            months_out.append(month)
        else:
            quiet.append(start)
    quiet.reverse()

    measures = _measures(conn, tid, month_starts, now, opens, findings)
    return Timeline(
        tenant,
        now,
        months,
        since,
        max(kept, since),
        months_out,
        quiet,
        counts,
        total,
        show,
        measures,
        _left_out(opens, collects),
        truncated,
    )


def _left_out(opens: Callable[[str], bool], collects: bool) -> list[str]:
    """The parts this person can't see on this client, in words, for the page's note."""
    out = []
    if not opens(FINDINGS):
        out.append("Findings")
    if not opens(ADMIN_REPORT):
        out.append("Admin changes")
    for key, label, _, _ in MEASURES:
        if key != OPEN_SEVERE and not opens(FIGURE_REPORTS[key]):
            out.append(label)
    if not collects:
        out.append("Failed collections")
    return out


# -- Downloads -------------------------------------------------------------------------------------


def to_tables(tl: Timeline) -> list[Table]:
    """The timeline as sheets: every entry, each month's summary, and the measures by month."""
    entries = Table(
        "Timeline",
        ["Date", "Month", "Group", "Kind", "Level", "What", "Detail"],
        [
            [e.dates, m.label, GROUP_TITLES.get(e.group, e.group), e.label, e.word, e.title, e.detail]
            for m in tl.months
            for e in m.entries
        ],
    )
    summaries = Table("Months", ["Month", "Summary"], [[m.label, "; ".join(m.summary)] for m in tl.months])
    out = [entries, summaries]
    if tl.measures:
        months = tl.measures[0].months
        out.append(
            Table(
                "By month",
                ["Month", *(f"{x.label}{' (%)' if x.unit else ''}" for x in tl.measures)],
                [
                    [f"{m:%B %Y}", *(x.values[i] for x in tl.measures)]
                    for i, m in enumerate(months)
                    if any(x.values[i] is not None for x in tl.measures)
                ],
            )
        )
    return out
