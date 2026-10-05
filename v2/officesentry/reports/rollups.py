"""Cross-tenant rollups: one row per tenant for every report.

A rollup is a list of columns and one ``RollupRow`` per tenant. The page,
CSV and XLSX all come from the same data. ``available()`` lists the monthly
review, the insurance evidence pack and every detail report in
``data_reports.REPORTS``, so a new detail report gets a rollup without any
code here. A detail report's rollup counts the rows of each table and the
rows flagged for review.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import data_reports, evidence, views
from .monthly_review import rollup_row, setting
from .tables import Table

SEVERITIES = ("critical", "high", "medium", "low")


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
