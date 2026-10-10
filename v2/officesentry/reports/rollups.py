"""Cross-tenant rollups: one row per tenant for every report.

A rollup is a list of columns and one ``RollupRow`` per tenant. The page,
CSV and XLSX all come from the same data. ``available()`` lists the monthly
review, the insurance evidence pack and every detail report in
``data_reports.REPORTS``, so a new detail report gets a rollup without any
code here. A detail report's rollup counts the rows of each table and the
rows flagged for review; ``Rollup.total_row()`` adds up the columns where a
sum means something.

``all_rows()`` is the other way to look across tenants: every row of a
detail report's tables from every tenant, with a Tenant column first, as one
``DataReport`` that the normal table views, filters and exports work on.
``check_matrix()`` is every check's result for every tenant.
"""

from __future__ import annotations

import copy
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..checks import RULES, SEVERITY_RANK, evaluate
from ..findings import finding_key, lifecycle_map
from ..snapshot import TenantSnapshot
from ..text import plural
from . import data_reports, evidence, views
from .data_reports import DataReport, Section, UserLink
from .identity_reports import Money
from .monthly_review import rollup_row, setting
from .precomputed import Rows
from .tables import Table

SEVERITIES = ("critical", "high", "medium", "low")
# Headline figures that are counted per tenant but mean nothing added up across tenants.
NO_TOTAL = ("score", "points", "countr", "largest", "average", "median", "rate", "share", "%")


@dataclass
class Column:
    key: str
    label: str
    kind: str = "num"  # num | pct | money | sev | status | text
    better: str | None = None  # up | down: which direction of change is good, when the row has a delta
    hint: str = ""


@dataclass
class RollupRow:
    tenant: dict
    values: dict = field(default_factory=dict)
    deltas: dict = field(default_factory=dict)
    tones: dict = field(default_factory=dict)  # status column -> ok | warn | bad | info
    note: str = ""  # why the tenant has no data for this report
    # column key -> (target, suffix): the tenant's page behind a figure. target is a detail report key,
    # "findings" or "changes"; suffix is the view query and anchor that pick out the rows.
    links: dict = field(default_factory=dict)
    attention: int = 0  # what the default sort uses: higher first

    def sort_value(self, column: Column):
        value = self.values.get(column.key)
        if column.kind == "sev":
            counts = value or {}
            return sum(counts.get(s, 0) * 1000 ** (len(SEVERITIES) - i) for i, s in enumerate(SEVERITIES))
        if column.kind == "text":
            number = _number(value) if value not in (None, "") else None
            return number if number is not None else value
        return value


@dataclass
class Rollup:
    key: str
    title: str
    description: str
    columns: list[Column]
    rows: list[RollupRow]
    generated_at: datetime
    currency: str = "GBP"
    tenant_link: str = "tenant"  # which per-tenant page a row links to: tenant | review | evidence | data
    footnote: str = ""

    @property
    def with_attention(self) -> int:
        return sum(1 for r in self.rows if r.attention)

    @property
    def without_data(self) -> int:
        return sum(1 for r in self.rows if r.note)

    def total_row(self) -> RollupRow | None:
        """Every tenant added up, in the columns where a sum means something: counts, money, severity
        counts, and how many tenants have each control in place. Percentages, scores and other text
        stay blank. None when there's nothing to add up (fewer than two tenants with data)."""
        rows = [r for r in self.rows if not r.note]
        if len(rows) < 2:
            return None
        total = RollupRow({"name": "Total", "tenant_ref": ""})
        for c in self.columns:
            values = [v for v in (r.values.get(c.key) for r in rows) if v not in (None, "")]
            if not values:
                continue
            if c.kind == "sev":
                total.values[c.key] = {s: sum(v.get(s, 0) for v in values) for s in SEVERITIES}
            elif c.kind in ("num", "money"):
                total.values[c.key] = round(sum(values), 2)
            elif c.kind == "status":
                total.values[c.key] = f"{sum(1 for r in rows if r.tones.get(c.key) == 'ok')} of {len(values)}"
            elif c.kind == "text" and not any(word in c.label.lower() for word in NO_TOTAL):
                if all(type(v) is int for v in values):
                    total.values[c.key] = sum(values)
                elif all(isinstance(v, Money) and v.currency == values[0].currency for v in values):
                    total.values[c.key] = Money(sum(values), values[0].currency)
        return total if total.values else None

    def to_table(self) -> Table:
        header = ["Tenant", "Tenant ID"]
        for c in self.columns:
            if c.kind == "sev":
                header += [f"{c.label}: {s}" for s in SEVERITIES]
            else:
                header.append(c.label + (f" ({self.currency})" if c.kind == "money" else ""))
                if c.better:
                    header.append(f"{c.label}: change")
        header.append("Note")
        rows = []
        for r in self.rows:
            line = [r.tenant["name"], r.tenant["tenant_ref"]]
            for c in self.columns:
                value = r.values.get(c.key)
                if c.kind == "sev":
                    line += [(value or {}).get(s, 0) if value is not None else "" for s in SEVERITIES]
                    continue
                line.append("" if value is None else value)
                if c.better:
                    delta = r.deltas.get(c.key)
                    line.append("" if delta is None else delta)
            line.append(r.note)
            rows.append(line)
        return Table(self.title, header, rows)


# -- Monthly review ----------------------------------------------------------------


def review_rollup(conn, tenants, now: datetime, cached: bool = False) -> Rollup:
    columns = [
        Column("open", "Open findings", "sev", hint="Critical, high, medium and low"),
        Column("new", "New in 4 weeks"),
        Column("resolved", "Fixed in 4 weeks"),
        Column("secure_score.pct", "Secure Score", "pct", "up"),
        Column("mfa.registered_pct", "Users with MFA", "pct", "up"),
        Column("admins.privileged_principals", "Privileged accounts", "num", "down"),
        Column("users.licensed", "Licensed users"),
        Column("stale", "Inactive 90+ days"),
        Column("waste", "Licence waste / month", "money"),
    ]
    rows = []
    for t in tenants:
        try:
            data = rollup_row(conn, t, now, cached=cached)
        except Exception as exc:  # one broken tenant mustn't blank the rollup
            logging.getLogger(__name__).exception("Review rollup failed for tenant %s", t["id"])
            rows.append(RollupRow(dict(t), note=f"Couldn't be built: {type(exc).__name__}"))
            continue
        row = RollupRow(data["tenant"], values={"open": data["counts"], "new": data["new"],
                                                "resolved": data["resolved"]})
        for key, kpi in data["kpis"].items():
            row.values[key] = kpi.value
            if kpi.delta is not None:
                row.deltas[key] = kpi.delta
        row.links = dict(REVIEW_LINKS)
        row.attention = sum(data["counts"].get(s, 0) for s in ("critical", "high"))
        if not any(data["counts"].values()) and all(k.value is None for k in data["kpis"].values()):
            row.note = "No data collected yet."
        rows.append(row)
    return Rollup("review", "Monthly review", "Findings, Secure Score, MFA, admins and licence waste for every "
                  "tenant, with the change over 4 weeks.", columns, rows, now,
                  currency=setting(conn, "currency", "GBP"), tenant_link="review",
                  footnote="Changes are over the last 4 weeks. Licence waste counts licences that are unassigned "
                           "or held by disabled or inactive users, priced under Licence prices.")


# Where each monthly review figure leads for one tenant.
REVIEW_LINKS = {
    "open": ("findings", ""), "new": ("changes", ""), "resolved": ("changes", ""),
    "secure_score.pct": ("secure-score", ""), "mfa.registered_pct": ("mfa", ""),
    "admins.privileged_principals": ("admins", "#t-privileged-role-holders"), "users.licensed": ("licences", ""),
    "stale": ("inactive", "#t-inactive-members"), "waste": ("licences", "#t-waste-by-subscription"),
}


# -- Insurance evidence -------------------------------------------------------------

CONTROL_LABELS = {
    "mfa_all": "MFA for everyone", "mfa_admins": "MFA for admins", "legacy_auth": "Legacy sign-in blocked",
    "privileged": "Admin access limited", "email_auth": "SPF, DKIM, DMARC", "forwarding": "Forwarding controlled",
    "logging": "Audit logging", "accounts": "Leavers disabled", "apps": "App access controlled",
    "sharing": "Sharing restricted", "devices": "Devices managed",
}
STATUS_TONES = {evidence.IN_PLACE: "ok", evidence.PARTLY: "warn", evidence.NOT_IN_PLACE: "bad",
                evidence.NOT_ASSESSED: "info"}


def evidence_rollup(conn, tenants, now: datetime) -> Rollup:
    controls = [c for c in evidence.CONTROLS if not c.outside_scope]
    columns = [Column("in_place", "In place", hint="Controls in place"),
               Column("gaps", "Gaps", hint="Controls partly or not in place")]
    columns += [Column(c.key, CONTROL_LABELS.get(c.key, c.key.replace("_", " ").capitalize()), "status",
                       hint=c.question) for c in controls]
    rows = []
    for t in tenants:
        try:
            pack = evidence.build(conn, t, now)
        except Exception as exc:
            logging.getLogger(__name__).exception("Evidence rollup failed for tenant %s", t["id"])
            rows.append(RollupRow(dict(t), note=f"Couldn't be built: {type(exc).__name__}"))
            continue
        row = RollupRow(dict(t))
        by_key = {c.control.key: c for c in pack.controls}
        for c in controls:
            result = by_key.get(c.key)
            if result:
                row.values[c.key] = result.status
                row.tones[c.key] = STATUS_TONES.get(result.status, "info")
        counts = pack.counts
        row.values["in_place"] = counts[evidence.IN_PLACE]
        row.values["gaps"] = counts[evidence.PARTLY] + counts[evidence.NOT_IN_PLACE]
        row.attention = counts[evidence.NOT_IN_PLACE] * 10 + counts[evidence.PARTLY]
        if counts[evidence.NOT_ASSESSED] == len(pack.controls):
            row.note = "No data collected yet."
        rows.append(row)
    return Rollup("evidence", "Insurance evidence", "The controls cyber-insurance questionnaires ask about, "
                  "answered for every tenant.", columns, rows, now, tenant_link="evidence",
                  footnote="Backups and training are outside Microsoft 365 data and are left out here.")


# -- Detail reports (generic) -------------------------------------------------------


def is_settings(section: data_reports.Section) -> bool:
    """A table of settings ("Setting", "Value", ...) has no meaningful row count, only flags."""
    return section.columns[:2] == ["Setting", "Value"]


def _headlines(report: data_reports.DataReport) -> list:
    """A report's own headline figures (tiles or highlights), if its builder sets any."""
    return list(report.tiles) or list(report.highlights)


def _number(value):
    """Headline values are often text ("12", "40%", "£120"); keep the number for sorting and exports."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = str(value).replace(",", "").strip()
    for ch in "£$€%":
        text = text.replace(ch, "")
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return None


def data_rollup(conn, tenants, key: str, now: datetime) -> Rollup:
    """Columns are the report's headline figures when it has them, else a row count per table."""
    title, description, _collectors, _builder = data_reports.REPORTS[key]
    built = [(t, data_reports.build(conn, t, key, now)) for t in tenants]
    use_headlines = any(_headlines(r) for _t, r in built if not r.unavailable)
    labels: list[str] = []
    for _t, report in built:
        items = [h.label for h in _headlines(report)] if use_headlines else \
            [s.title for s in report.sections if not is_settings(s)]
        labels += [x for x in items if x not in labels]
    columns = [Column("_flagged", "To review", hint="Rows flagged critical, high or medium")]
    columns += [Column(f"c:{name}", name, "text" if use_headlines else "num") for name in labels]
    rows = []
    for t, report in built:
        row = RollupRow(dict(t))
        if report.unavailable:
            row.note = report.unavailable
            rows.append(row)
            continue
        row.values["_flagged"] = sum(s.flagged for s in report.sections)
        row.attention = row.values["_flagged"]
        if row.values["_flagged"]:
            row.links["_flagged"] = (key, views.flagged_link(report))
        if use_headlines:
            targets = views.headline_links(report)
            for h, href in zip(report.tiles or report.highlights,
                               targets["tiles"] if report.tiles else targets["highlights"], strict=True):
                row.values[f"c:{h.label}"] = h.value
                if h.tone:
                    row.tones[f"c:{h.label}"] = h.tone
                if href:
                    row.links[f"c:{h.label}"] = (key, href)
        else:
            for s, table in zip(report.sections, views.table_slugs(report), strict=True):
                if not is_settings(s):
                    row.values[f"c:{s.title}"] = len(s.rows)
                    row.links[f"c:{s.title}"] = (key, f"#{views.anchor(table)}")
        rows.append(row)
    footnote = ("Figures are the report's headline numbers for each tenant." if use_headlines else
                "Numbers count the rows in each of the report's tables.") + " Each tenant's own report has the detail."
    return Rollup(key, title, description, columns, rows, now, tenant_link="data", footnote=footnote)


# -- Every row of a detail report, across tenants ----------------------------------

MAX_ROWS = 10_000  # per table: more than a page or a PDF can sensibly hold
TENANT = "Tenant"


@dataclass
class AllRows:
    """One detail report's tables with every visible tenant's rows, a Tenant column first.

    ``report`` is an ordinary ``DataReport`` (for "All tenants"), so ``views`` filters, sorts and
    hides columns on it and ``data_reports.to_xlsx`` exports it like a tenant's own report.
    """
    report: DataReport
    totals: dict[str, int]  # table title -> rows across tenants before the cap
    without: list[tuple[dict, str]]  # tenants with no data for this report, and why

    def truncated(self, title: str) -> int:
        """How many rows a table had before the cap, or 0 when nothing was left out."""
        section = next((s for s in self.report.sections if s.title == title), None)
        count = self.totals.get(title, 0)
        return count if section is not None and count > len(section.rows) else 0


def _place(out: Section, columns: list[str], row: list, tenant_cell) -> list:
    """A tenant's row laid out in the combined table's columns (they can differ between tenants)."""
    if out.columns[1:] == columns:
        return [tenant_cell, *row]
    for name in columns:
        if name not in out.columns:
            out.columns.append(name)
            for line in out.rows:
                line.append("")
    line = [""] * len(out.columns)
    line[0] = tenant_cell
    for name, value in zip(columns, row, strict=False):
        line[out.columns.index(name)] = value
    return line


def all_rows(conn, tenants, key: str, now: datetime | None = None, href=None, label: str = "All tenants") -> AllRows:
    """Build the report for each tenant (with the same builders as its own page) and stack the rows.

    ``href(tenant)``, when given, is the tenant's own report page: the Tenant cell links to its table.
    ``label`` names the tenants on downloads (a tenant group's name when the page shows one).
    """
    now = now or datetime.now(UTC)
    title, description, _collectors, _builder = data_reports.REPORTS[key]
    combined = DataReport(key, title, description,
                          {"id": 0, "name": label, "tenant_ref": plural(len(tenants), "tenant")}, now)
    sections: dict[str, Section] = {}
    totals: Counter = Counter()
    latest: dict[str, str | None] = {}
    without = []
    for t in tenants:
        report = data_reports.build(conn, t, key, now)
        for source, finished in report.sources:
            latest[source] = max(filter(None, (latest.get(source), finished)), default=None)
        if report.unavailable:
            without.append((dict(t), report.unavailable))
            continue
        for s, slug in zip(report.sections, views.table_slugs(report), strict=True):
            out = sections.get(s.title)
            if out is None:
                out = sections[s.title] = Section(s.title, [TENANT, *s.columns], intro=s.intro, empty=s.empty,
                                                  compare=s.compare)
            cell = UserLink(t["name"], f"{href(t)}#{views.anchor(slug)}") if href else t["name"]
            totals[s.title] += len(s.rows)
            room = max(MAX_ROWS - len(out.rows), 0)
            flags = s.flags or [None] * len(s.rows)
            for row, flag in zip(s.rows[:room], flags[:room], strict=False):
                out.rows.append(_place(out, s.columns, row, cell))
                out.flags.append(flag)
    for s in sections.values():  # rows placed before a later tenant added a column
        for line in s.rows:
            line.extend([""] * (len(s.columns) - len(line)))
    combined.sections = list(sections.values())
    combined.sources = list(latest.items())
    combined.notes = [f"{t['name']}: {reason}" for t, reason in without]
    if not combined.sections:
        combined.unavailable = "No tenant has data for this report yet." if tenants else "No tenants to show."
    return AllRows(combined, dict(totals), without)


def one_table(rows: AllRows, title: str, view: views.View | None = None) -> DataReport:
    """The combined report narrowed to one table, with a table view applied, for downloads."""
    report = views.apply(rows.report, view) if view else rows.report
    out = copy.copy(report)
    out.sections = [s for s in report.sections if s.title == title]
    out.title = f"{report.title}: {title}" if title != report.title else report.title
    out.filtered = [line for line in report.filtered if line.startswith(f"{title}:")]
    cut = rows.truncated(title)
    if cut:
        out.notice = (f"Only the first {MAX_ROWS:,} of {cut:,} rows are included. Filter the table on the page, "
                      "or open each tenant's own report for the rest.")
    return out


# -- Every check, for every tenant ---------------------------------------------------

CHECK_STATUS = {"pass": "Pass", "fail": "Fail", "accepted": "Accepted", "unavailable": "Not checked"}


@dataclass
class CheckCell:
    status: str  # pass | fail | accepted (every finding accepted as a risk) | unavailable
    summary: str
    severity: str | None = None  # the worst open finding's, when failing
    count: int = 0  # open findings not accepted

    @property
    def label(self) -> str:
        if self.status == "fail" and self.severity:
            return f"Fail ({self.severity}, {self.count})"
        return CHECK_STATUS.get(self.status, self.status)


@dataclass
class CheckRow:
    tenant: dict
    cells: dict[str, CheckCell] = field(default_factory=dict)
    note: str = ""

    def count(self, status: str) -> int:
        return sum(1 for c in self.cells.values() if c.status == status)

    def failing(self, severity: str) -> int:
        return sum(1 for c in self.cells.values() if c.status == "fail" and c.severity == severity)

    @property
    def severities(self) -> dict[str, int]:
        """Failing checks by their worst finding's severity, for the coloured counts."""
        return {s: self.failing(s) for s in SEVERITIES}

    @property
    def attention(self) -> int:
        return sum(self.failing(s) * 1000 ** (len(SEVERITIES) - i) for i, s in enumerate(SEVERITIES))


@dataclass
class CheckMatrix:
    checks: list[tuple[str, str, str]]  # (key, title, category), in the checks' own order
    rows: list[CheckRow]
    generated_at: datetime

    @property
    def categories(self) -> list[tuple[str, list[tuple[str, str, str]]]]:
        out: dict[str, list] = {}
        for check in self.checks:
            out.setdefault(check[2], []).append(check)
        return list(out.items())

    @property
    def with_failures(self) -> int:
        return sum(1 for r in self.rows if r.count("fail"))

    def to_table(self) -> Table:
        header = ["Tenant", "Tenant ID", "Failing", "Accepted", "Not checked", "Note"]
        header += [f"{category}: {title}" for _key, title, category in self.checks]
        rows = []
        for r in self.rows:
            line = [r.tenant["name"], r.tenant["tenant_ref"]]
            if r.note:
                rows.append(line + ["", "", "", r.note] + [""] * len(self.checks))
                continue
            line += [r.count("fail"), r.count("accepted"), r.count("unavailable"), ""]
            line += [r.cells[k].label if k in r.cells else "" for k, _t, _c in self.checks]
            rows.append(line)
        return Table("Checks", header, rows)


def check_matrix(conn, tenants, now: datetime | None = None, rebuild: bool = False) -> CheckMatrix:
    """Every check for every tenant, from the latest stored data and the findings' accepted risks.

    The same evaluation and accepted-risk handling as the monthly review; nothing is written but
    the stored rows. Without ``now`` (the page, which shows the present), each tenant's row comes
    from ``precomputed`` while that's current, and is built and stored when it isn't (always, with
    ``rebuild``).
    """
    live = now is None
    now = now or datetime.now(UTC)
    stored = Rows(conn, "checks", tenants, now, rebuild) if live else None
    checks = [(r.key, r.title, r.category) for r in RULES]
    rows = [_check_row(conn, t, now, stored) for t in tenants]
    rows.sort(key=lambda r: (bool(r.note), -r.attention, -r.count("fail"), r.tenant["name"].lower()))
    return CheckMatrix(checks, rows, now)


def _check_row(conn, tenant, now: datetime, stored: Rows | None) -> CheckRow:
    fingerprint = stored.fingerprint(tenant) if stored else ""
    if stored and (data := stored.get(tenant, fingerprint)) is not None:
        return CheckRow(dict(tenant), {k: CheckCell(*cell) for k, cell in data["cells"].items()}, data["note"])
    row = CheckRow(dict(tenant))
    try:
        snap = TenantSnapshot(conn, tenant)
        if not snap.runs:
            row.note = "No data collected yet."
        else:
            results = evaluate(snap, now)
            life = lifecycle_map(conn, tenant["id"], now)
    except Exception as exc:  # one broken tenant mustn't blank the page
        logging.getLogger(__name__).exception("Check matrix failed for tenant %s", tenant["id"])
        row.note = f"Couldn't be built: {type(exc).__name__}"
        return row  # not stored: the next view tries again
    if not row.note:
        for r in results:
            active = [f for f in r.findings
                      if not ((lc := life.get((r.key, finding_key(f)))) and lc.accepted)]
            if r.status == "fail" and (active or not r.findings):
                worst = min((f.severity for f in active), key=SEVERITY_RANK.__getitem__, default=None)
                row.cells[r.key] = CheckCell("fail", r.summary, worst, len(active))
            else:
                row.cells[r.key] = CheckCell("accepted" if r.status == "fail" else r.status, r.summary)
    if stored:
        stored.save(tenant, fingerprint, {"note": row.note, "cells": {
            k: [c.status, c.summary, c.severity, c.count] for k, c in row.cells.items()}})
    return row


# -- Registry --------------------------------------------------------------------------


def available() -> list[tuple[str, str]]:
    """(key, title) of every rollup, in menu order. Read at call time so new reports appear automatically."""
    return [("review", "Monthly review"), ("evidence", "Insurance evidence")] + \
        [(k, v[0]) for k, v in data_reports.REPORTS.items()]


def build(conn, tenants, key: str, now: datetime | None = None) -> Rollup:
    live = now is None  # only a rollup of the present can use the cached rows
    now = now or datetime.now(UTC)
    if key == "review":
        rollup = review_rollup(conn, tenants, now, cached=live)
    elif key == "evidence":
        rollup = evidence_rollup(conn, tenants, now)
    elif key in data_reports.REPORTS:
        rollup = data_rollup(conn, tenants, key, now)
    else:
        raise KeyError(key)
    rollup.rows.sort(key=lambda r: (bool(r.note), -r.attention, r.tenant["name"].lower()))
    return rollup
