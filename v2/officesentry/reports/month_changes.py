"""Month-over-month change report: what changed in a tenant between two dates.

It has four parts:

* **Key figures**: tracked metrics then and now.
* **Findings**: new and fixed findings from the findings history.
* **Key changes**: the curated feeds in ``changes.all_changes`` (admins,
  Conditional Access, accounts, forwarding, apps, devices, groups).
* **Changes by report**: every detail report in ``data_reports.REPORTS`` is
  built twice, from the data held at the start of the period and from the
  latest data, and its tables are compared row by row. New reports are
  compared automatically. A table opts out with ``Section(compare=False)``
  (event logs, time series), and columns that only move with time
  (``Last ...``, ``... (UTC)``, ``30-day change``) are ignored.

``across`` is one summary line per tenant for What changed across tenants; the
worker stores each tenant's line (``precomputed``) so the page doesn't build
every tenant's report. Nothing here calls Microsoft.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..changes import Change, all_changes
from ..checks import RULES
from ..collectors import REGISTRY
from ..findings import resolved_since
from ..snapshot import TenantSnapshot
from ..text import verb
from . import data_reports
from .precomputed import Rows
from .tables import Table

PERIODS = (7, 30, 90)  # days the report can look back
DEFAULT_DAYS = 30
ISO = "%Y-%m-%dT%H:%M:%SZ"
SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

# Tracked numbers shown as "then → now". (metric key, label, unit, which direction is good)
FIGURES = (
    ("secure_score.pct", "Secure Score", "%", "up"),
    ("mfa.registered_pct", "Users with MFA", "%", "up"),
    ("admins.privileged_principals", "Privileged accounts", "", "down"),
    ("admins.global_admins", "Global admins", "", "down"),
    ("users.licensed", "Licensed users", "", None),
    ("licences.unassigned", "Unassigned licences", "", "down"),
    ("apps.third_party", "Third-party apps", "", None),
    ("mailboxes.forwarding", "Mailboxes forwarding mail", "", "down"),
    ("ca.policies_enabled", "Conditional Access policies on", "", None),
    ("devices.noncompliant", "Non-compliant devices", "", "down"),
    ("groups.ownerless", "Groups without an owner", "", "down"),
)


@dataclass
class Figure:
    key: str
    label: str
    unit: str
    better: str | None
    before: float | None
    after: float | None

    @property
    def delta(self) -> float | None:
        if self.before is None or self.after is None:
            return None
        return round(self.after - self.before, 1)

    @property
    def trend(self) -> str:
        d = self.delta
        if not d or self.better is None:
            return "flat"
        return "better" if (d > 0) == (self.better == "up") else "worse"


@dataclass
class RowChange:
    kind: str  # added | removed | changed
    row: list
    diffs: list[tuple[str, object, object]] = field(default_factory=list)  # (column, before, after)
    columns: list[str] = field(default_factory=list)  # the table's columns, to describe added and removed rows

    @property
    def subject(self) -> str:
        return str(self.row[0]) if self.row else ""

    @property
    def detail(self) -> str:
        if self.kind != "changed":
            return "; ".join(
                f"{c}: {v}" for c, v in zip(self.columns[1:], self.row[1:], strict=False) if v not in ("", None)
            )
        return "; ".join(f"{c}: {_text(a)} → {_text(b)}" for c, a, b in self.diffs)


@dataclass
class SectionChanges:
    title: str
    columns: list[str]
    changes: list[RowChange] = field(default_factory=list)


@dataclass
class ReportChanges:
    key: str
    title: str
    sections: list[SectionChanges] = field(default_factory=list)
    note: str = ""  # why it couldn't be compared

    @property
    def count(self) -> int:
        return sum(len(s.changes) for s in self.sections)


@dataclass
class TenantChanges:
    tenant: dict
    since: datetime
    until: datetime
    figures: list[Figure]
    new_findings: list
    fixed_findings: list
    key_changes: list[Change]
    reports: list[ReportChanges]
    baseline: str | None  # when the oldest data used as the starting point was collected
    notes: list[str] = field(default_factory=list)

    @property
    def report_changes(self) -> int:
        return sum(r.count for r in self.reports)

    @property
    def total(self) -> int:
        return len(self.key_changes) + self.report_changes

    @property
    def has_baseline(self) -> bool:
        return self.baseline is not None


def _text(value) -> str:
    return "blank" if value in ("", None) else str(value)


# Columns that count days from now: an account inactive for 170 days then is 200 days now, with nothing changed.
DAY_COUNT_COLUMNS = frozenset({"Days", "Days waiting", "Days left", "Ends within"})
# Day counts inside text cells (the Devices report's problems), compared without the number.
DAY_COUNT_TEXT = re.compile(r"(No check-in for )\d+( days?)")


def _volatile(column: str) -> bool:
    """Columns that change with the passing of time rather than with the tenant."""
    return (
        column.startswith("Last ")
        or column.endswith("(UTC)")
        or "change" in column.lower()
        or column in DAY_COUNT_COLUMNS
    )


def _same(value) -> str:
    """A cell as compared: text, with day counts that only move with the clock taken out."""
    return DAY_COUNT_TEXT.sub(r"\1N\2", str(value))


def _keyed(section: data_reports.Section, width: int):
    out = {}
    for row in section.rows:
        key = tuple(str(v) for v in row[:width])
        if key in out:
            return None
        out[key] = row
    return out


def compare_sections(old: data_reports.Section | None, new: data_reports.Section | None) -> SectionChanges | None:
    """Row-level differences between one table then and now.

    Rows are matched on their first column, or the first two when the first
    isn't unique. Tables without a unique key fall back to whole rows, which
    only produces added and removed rows.
    """
    ref = new or old
    if ref is None or not ref.compare:
        return None
    columns = ref.columns
    keep = [i for i, c in enumerate(columns) if not _volatile(c) and c not in ref.drifts]

    def stable(row):
        return tuple(_same(row[i]) for i in keep if i < len(row))

    old_rows, new_rows = (old.rows if old else []), (new.rows if new else [])
    out = SectionChanges(ref.title, columns)
    for width in (1, 2):
        before = _keyed(old or data_reports.Section("", columns), width)
        after = _keyed(new or data_reports.Section("", columns), width)
        if before is not None and after is not None:
            break
    else:
        before = after = None
    if before is None:
        # No unique key: compare whole rows (ignoring volatile columns), as multisets.
        remaining = [stable(r) for r in old_rows]
        for row in new_rows:
            s = stable(row)
            if s in remaining:
                remaining.remove(s)
            else:
                out.changes.append(RowChange("added", row))
        leftover = [stable(r) for r in new_rows]
        for row in old_rows:
            s = stable(row)
            if s in leftover:
                leftover.remove(s)
            else:
                out.changes.append(RowChange("removed", row))
    else:
        for key, row in after.items():
            if key not in before:
                out.changes.append(RowChange("added", row))
                continue
            was = before[key]
            diffs = [
                (columns[i], was[i] if i < len(was) else "", row[i] if i < len(row) else "")
                for i in keep
                if _same(was[i] if i < len(was) else "") != _same(row[i] if i < len(row) else "")
            ]
            if diffs:
                out.changes.append(RowChange("changed", row, diffs))
        out.changes += [RowChange("removed", row) for key, row in before.items() if key not in after]
    for change in out.changes:
        change.columns = columns
    return out


def compare_report(
    conn, tenant, key: str, since: datetime, now: datetime, old_snap: TenantSnapshot, new_snap: TenantSnapshot
) -> ReportChanges:
    title, _description, collectors, _builder = data_reports.REPORTS[key]
    result = ReportChanges(key, title)
    old_runs = [old_snap.runs.get(c) for c in collectors]
    new_runs = [new_snap.runs.get(c) for c in collectors]
    if not any(old_runs):
        result.note = "No data from the start of the period to compare with."
        return result
    if [r["id"] if r else None for r in old_runs] == [r["id"] if r else None for r in new_runs]:
        return result  # nothing collected since: nothing can have changed
    missing = [
        REGISTRY[c].title for c, then, latest in zip(collectors, old_runs, new_runs, strict=True) if latest and not then
    ]
    if missing:  # compared with nothing, everything it adds to the report would look new
        result.note = (
            f"{' and '.join(missing)} hadn't been collected by the start of the period, so there's nothing to "
            "compare with."
        )
        return result
    gone = [
        REGISTRY[c].title
        for c, r in zip(collectors, old_runs, strict=True)
        if r and data_reports.detail_removed(conn, c, r)
    ]
    if gone:  # an empty sign-in log then would show every sign-in now as new
        result.note = (
            f"{' and '.join(gone)} from the start of the period {verb(len(gone), 'is', 'are')} no longer held: "
            f"the retention policy keeps {'it' if len(gone) == 1 else 'them'} for less time than the daily history."
        )
        return result
    old = data_reports.build(conn, tenant, key, as_of=since, snap=old_snap)
    new = data_reports.build(conn, tenant, key, now=now, snap=new_snap)
    if old.unavailable or new.unavailable:
        result.note = new.unavailable or old.unavailable or ""
        return result
    old_by_title = {s.title: s for s in old.sections}
    new_by_title = {s.title: s for s in new.sections}
    for title_ in list(dict.fromkeys([*new_by_title, *old_by_title])):
        diff = compare_sections(old_by_title.get(title_), new_by_title.get(title_))
        if diff and diff.changes:
            result.sections.append(diff)
    return result


def _figures(conn, tenant_id: int, since: str, until: str | None = None) -> list[Figure]:
    out = []
    for key, label, unit, better in FIGURES:
        latest = conn.execute(
            "SELECT value FROM metrics WHERE tenant_id = ? AND key = ? AND captured_at <= ?"
            " ORDER BY captured_at DESC, rowid DESC LIMIT 1",
            (tenant_id, key, until or "9999"),
        ).fetchone()
        if latest is None:
            continue
        before = conn.execute(
            "SELECT value FROM metrics WHERE tenant_id = ? AND key = ? AND captured_at <= ?"
            " ORDER BY captured_at DESC, rowid DESC LIMIT 1",
            (tenant_id, key, since),
        ).fetchone()
        out.append(Figure(key, label, unit, better, before["value"] if before else None, latest["value"]))
    return out


def _new_findings(conn, tenant_id: int, since: str, until: str | None = None) -> list:
    """Findings opened or reopened since ``since`` that are still open (or, with ``until``, that were opened
    or reopened between the two and still open at ``until``)."""
    if until is None:
        return conn.execute(
            "SELECT * FROM findings WHERE tenant_id = ? AND resolved_at IS NULL"
            " AND (first_seen >= ? OR reopened_at >= ?)",
            (tenant_id, since, since),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM findings WHERE tenant_id = ? AND (resolved_at IS NULL OR resolved_at > ?)"
        " AND ((first_seen >= ? AND first_seen <= ?) OR (reopened_at >= ? AND reopened_at <= ?))",
        (tenant_id, until, since, until, since, until),
    ).fetchall()


def _fixed(conn, tenant_id: int, since: str, until: str | None) -> list:
    rows = resolved_since(conn, tenant_id, since)
    return [r for r in rows if r["resolved_at"] <= until] if until else list(rows)


def build(
    conn,
    tenant,
    days: int = DEFAULT_DAYS,
    now: datetime | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> TenantChanges:
    """What changed over the last ``days``, or between ``since`` and ``until``: any two dates, the end one read
    as the data stood then (``TenantSnapshot(as_of=until)``)."""
    now = until or now or datetime.now(UTC)
    since = since or now - timedelta(days=days)
    since_iso = since.strftime(ISO)
    until_iso = until.strftime(ISO) if until else None
    tenant = dict(tenant)
    notes = []

    old_snap = TenantSnapshot(conn, tenant, as_of=since)
    new_snap = TenantSnapshot(conn, tenant, as_of=until)
    finished = [r["finished_at"] for r in old_snap.runs.values() if r["finished_at"]]
    baseline = min(finished) if finished else None

    new_findings = _new_findings(conn, tenant["id"], since_iso, until_iso)
    fixed = _fixed(conn, tenant["id"], since_iso, until_iso)
    if baseline is None:
        # Collection started inside the period: everything it found is a first look, not a change.
        new_findings = []
        if any(new_snap.runs.values()):
            notes.append(
                "Collection for this client started during this period, so its findings are on the "
                "Findings tab rather than counted as new here."
            )
    order = lambda r: (SEV_RANK.get(r["severity"], 9), r["rule_key"], r["subject"])  # noqa: E731

    try:
        key_changes = all_changes(conn, tenant["id"], since_iso, until_iso)
    except Exception as exc:  # a curated feed that breaks on odd data mustn't blank the report
        logging.getLogger(__name__).exception("Change feeds failed for tenant %s", tenant["id"])
        key_changes = []
        notes.append(f"Key changes couldn't be worked out ({type(exc).__name__}).")

    reports = []
    for key in data_reports.REPORTS:
        try:
            reports.append(compare_report(conn, tenant, key, since, now, old_snap, new_snap))
        except Exception as exc:
            logging.getLogger(__name__).exception("Comparing %s failed for tenant %s", key, tenant["id"])
            reports.append(
                ReportChanges(key, data_reports.REPORTS[key][0], note=f"Couldn't be compared ({type(exc).__name__}).")
            )

    figures = _figures(conn, tenant["id"], since_iso, until_iso)
    figures.sort(key=lambda f: not f.delta)  # what moved first
    return TenantChanges(
        tenant,
        since,
        now,
        figures,
        sorted(new_findings, key=order),
        sorted(fixed, key=order),
        key_changes,
        reports,
        baseline,
        notes,
    )


RULE_TITLES = {r.key: r.title for r in RULES}


def to_tables(tc: TenantChanges) -> list[Table]:
    period = f"{tc.since.day} {tc.since:%b %Y} to {tc.until.day} {tc.until:%b %Y}"
    tables = [
        Table(
            "Key figures",
            ["Measure", f"On {tc.since.day} {tc.since:%b %Y}", "Now", "Change"],
            [[f.label, f.before, f.after, f.delta] for f in tc.figures],
        ),
        Table(
            "New findings",
            ["Severity", "Check", "Subject", "Detail", "First seen"],
            [
                [
                    r["severity"],
                    RULE_TITLES.get(r["rule_key"], r["rule_key"]),
                    r["subject"],
                    r["detail"],
                    (r["reopened_at"] or r["first_seen"])[:10],
                ]
                for r in tc.new_findings
            ],
        ),
        Table(
            "Fixed findings",
            ["Severity", "Check", "Subject", "Fixed on"],
            [
                [r["severity"], RULE_TITLES.get(r["rule_key"], r["rule_key"]), r["subject"], r["resolved_at"][:10]]
                for r in tc.fixed_findings
            ],
        ),
        Table(
            "Key changes",
            ["Area", "Change", "Subject", "Detail"],
            [[c.area, c.kind.capitalize(), c.subject, c.detail] for c in tc.key_changes],
        ),
        Table(
            "Changes by report",
            ["Report", "Table", "Change", "Subject", "Detail"],
            [
                [r.title, s.title, c.kind.capitalize(), c.subject, c.detail]
                for r in tc.reports
                for s in r.sections
                for c in s.changes
            ],
        ),
    ]
    tables.append(
        Table(
            "About",
            ["Item", "Value"],
            [
                ["Client", tc.tenant["name"]],
                ["Tenant ID", tc.tenant["tenant_ref"]],
                ["Period", period],
                ["Starting data collected (UTC)", (tc.baseline or "none")[:16].replace("T", " ")],
                *[["Note", n] for n in tc.notes],
            ],
        )
    )
    return tables


def all_changes_table(tc: TenantChanges) -> Table:
    """Key changes and report changes in one flat table, for CSV."""
    rows = [[c.area, "", c.kind.capitalize(), c.subject, c.detail] for c in tc.key_changes]
    rows += [
        [r.title, s.title, c.kind.capitalize(), c.subject, c.detail]
        for r in tc.reports
        for s in r.sections
        for c in s.changes
    ]
    return Table("Changes", ["Area or report", "Table", "Change", "Subject", "Detail"], rows)


# -- Across tenants ------------------------------------------------------------------


@dataclass
class Summary:
    tenant: dict
    changes: int
    new_findings: int
    new_severe: int
    fixed: int
    figures: dict[str, Figure]
    has_baseline: bool
    areas: list[str]
    error: str = ""


def summarise(tc: TenantChanges) -> Summary:
    areas = list(dict.fromkeys([c.area for c in tc.key_changes] + [r.title for r in tc.reports if r.count]))
    return Summary(
        tc.tenant,
        tc.total,
        len(tc.new_findings),
        sum(1 for r in tc.new_findings if r["severity"] in ("critical", "high")),
        len(tc.fixed_findings),
        {f.key: f for f in tc.figures},
        tc.has_baseline,
        areas,
    )


def across(
    conn, tenants, days: int = DEFAULT_DAYS, now: datetime | None = None, rebuild: bool = False
) -> list[Summary]:
    """Every tenant's summary, most important first.

    Without ``now`` (the pages, which show the present), each tenant's change count and areas come
    from its stored row (``precomputed``) while that's current, and are built and stored when it
    isn't (always, with ``rebuild``). New and fixed findings and the key figures are read live.
    """
    live = now is None
    now = now or datetime.now(UTC)
    stored = Rows(conn, f"changes:{days}", tenants, now, rebuild) if live else None
    out = []
    for t in tenants:
        try:
            out.append(_summary(conn, t, days, now, stored))
        except Exception as exc:
            logging.getLogger(__name__).exception("Change report failed for tenant %s", t["id"])
            out.append(Summary(dict(t), 0, 0, 0, 0, {}, False, [], error=type(exc).__name__))
    out.sort(key=lambda s: (-s.new_severe, -s.changes - s.new_findings, s.tenant["name"].lower()))
    return out


def _summary(conn, tenant, days: int, now: datetime, stored: Rows | None) -> Summary:
    if stored is None:
        return summarise(build(conn, tenant, days, now))
    since = now - timedelta(days=days)
    fingerprint = stored.fingerprint(tenant, since)
    data = stored.get(tenant, fingerprint)
    if data is None:
        summary = summarise(build(conn, tenant, days, now))
        stored.save(
            tenant,
            fingerprint,
            {"changes": summary.changes, "areas": summary.areas, "has_baseline": summary.has_baseline},
        )
        return summary
    since_iso = since.strftime(ISO)
    new = _new_findings(conn, tenant["id"], since_iso) if data["has_baseline"] else []
    return Summary(
        dict(tenant),
        data["changes"],
        len(new),
        sum(1 for r in new if r["severity"] in ("critical", "high")),
        len(resolved_since(conn, tenant["id"], since_iso)),
        {f.key: f for f in _figures(conn, tenant["id"], since_iso)},
        data["has_baseline"],
        data["areas"],
    )


def across_table(summaries: list[Summary], days: int) -> Table:
    rows = []
    for s in summaries:
        score, mfa = s.figures.get("secure_score.pct"), s.figures.get("mfa.registered_pct")
        rows.append(
            [
                s.tenant["name"],
                s.tenant["tenant_ref"],
                s.changes,
                s.new_findings,
                s.new_severe,
                s.fixed,
                score.after if score else None,
                score.delta if score else None,
                mfa.after if mfa else None,
                mfa.delta if mfa else None,
                ", ".join(s.areas),
                "" if s.has_baseline else f"No data from {days} days ago to compare with",
            ]
        )
    return Table(
        f"Changes, last {days} days",
        [
            "Client",
            "Tenant ID",
            "Changes",
            "New findings",
            "New critical or high",
            "Fixed findings",
            "Secure Score %",
            "Secure Score change",
            "Users with MFA %",
            "MFA change",
            "What changed",
            "Note",
        ],
        rows,
    )
