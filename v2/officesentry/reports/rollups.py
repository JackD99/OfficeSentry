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
from .precomputed import Rows, stored_rows
from .tables import Table

SEVERITIES = ("critical", "high", "medium", "low")
# Headline figures that are counted per tenant but mean nothing added up across tenants.
NO_TOTAL = ("score", "points", "countr", "largest", "average", "median", "rate", "share", "%")


@dataclass
class Column:
    key: str
    label: str
    kind: str = "num"  # num | pct | money | sev | status | text
    # up | down: which direction of change is good, when the row has a delta (None: neither, so it's neutral)
    better: str | None = None
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
    # Why the table scrolls sideways on a laptop, when it's a matrix too wide to fit (data-scrolls on the page)
    scrolls: str = ""
    uncut: Rollup | None = None  # every client, when a PDF keeps only the first rows (printing.cap_rollup)

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
        header = ["Client", "Tenant ID"]
        for c in self.columns:
            if c.kind == "sev":
                header += [f"{c.label}: {s}" for s in SEVERITIES]
            else:
                header.append(c.label + {"money": f" ({self.currency})", "pct": " %"}.get(c.kind, ""))
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
                line.append("" if value is None else _whole(value))
                if c.better:
                    delta = r.deltas.get(c.key)
                    line.append("" if delta is None else _whole(delta))
            line.append(r.note)
            rows.append(line)
        return Table(self.title, header, rows)


def _whole(value):
    """A stored figure as downloads write it: 54.0 licensed users as 54, so a spreadsheet shows a count."""
    return int(value) if type(value) is float and value.is_integer() else value


# -- Monthly review ----------------------------------------------------------------


def review_rollup(conn, tenants, now: datetime, cached: bool = False, built: dict | None = None) -> Rollup:
    """Every tenant's monthly review figures. ``built`` (tenant id -> ``rollup_row``) reuses rows the page
    has already built for its headline figures, so each tenant is read once."""
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
            data = built[t["id"]] if built and t["id"] in built else rollup_row(conn, t, now, cached=cached)
        except Exception as exc:  # one broken tenant mustn't blank the rollup
            logging.getLogger(__name__).exception("Review rollup failed for tenant %s", t["id"])
            rows.append(RollupRow(dict(t), note=f"Couldn't be built: {type(exc).__name__}"))
            continue
        row = RollupRow(
            data["tenant"], values={"open": data["counts"], "new": data["new"], "resolved": data["resolved"]}
        )
        for key, kpi in data["kpis"].items():
            row.values[key] = kpi.value
            if kpi.delta is not None:
                row.deltas[key] = kpi.delta
        row.links = dict(REVIEW_LINKS)
        row.attention = sum(data["counts"].get(s, 0) for s in ("critical", "high"))
        if not any(data["counts"].values()) and all(k.value is None for k in data["kpis"].values()):
            row.note = "No data collected yet."
        rows.append(row)
    return Rollup(
        "review",
        "Monthly review",
        "Findings, Secure Score, MFA, admins and licence waste for every client, with the change over 4 weeks.",
        columns,
        rows,
        now,
        currency=setting(conn, "currency", "GBP"),
        footnote="Changes are over the last 4 weeks. Licence waste counts licences that are unassigned "
        "or held by disabled or inactive users, priced under Licence prices.",
    )


# Where each monthly review figure leads for one tenant.
REVIEW_LINKS = {
    "open": ("findings", ""),
    "new": ("changes", ""),
    "resolved": ("changes", ""),
    "secure_score.pct": ("secure-score", ""),
    "mfa.registered_pct": ("mfa", ""),
    "admins.privileged_principals": ("admins", "#t-privileged-role-holders"),
    "users.licensed": ("licences", ""),
    "stale": ("inactive", "#t-inactive-members"),
    "waste": ("licences", "#t-waste-by-subscription"),
}


# -- Insurance evidence -------------------------------------------------------------

CONTROL_LABELS = {
    "mfa_all": "MFA for everyone",
    "mfa_admins": "MFA for admins",
    "legacy_auth": "Legacy sign-in blocked",
    "privileged": "Admin access limited",
    "email_auth": "SPF, DKIM, DMARC",
    "forwarding": "Forwarding controlled",
    "logging": "Audit logging",
    "accounts": "Leavers disabled",
    "apps": "App access controlled",
    "sharing": "Sharing restricted",
    "devices": "Devices managed",
}
STATUS_TONES = {
    evidence.IN_PLACE: "ok",
    evidence.PARTLY: "warn",
    evidence.NOT_IN_PLACE: "bad",
    evidence.NOT_ASSESSED: "info",
}


def evidence_rollup(conn, tenants, now: datetime | None = None, rebuild: bool = False) -> Rollup:
    """Every tenant's controls. Without ``now`` (the page), each tenant's row comes from ``precomputed``
    while that's current, and is built and stored when it isn't (always, with ``rebuild``)."""
    stored = stored_rows(conn, "evidence", tenants, now, rebuild)
    now = now or datetime.now(UTC)
    controls = [c for c in evidence.CONTROLS if not c.outside_scope]
    columns = [
        Column("in_place", "In place", hint="Controls in place"),
        Column("gaps", "Gaps", hint="Controls partly or not in place"),
    ]
    columns += [
        Column(c.key, CONTROL_LABELS.get(c.key, c.key.replace("_", " ").capitalize()), "status", hint=c.question)
        for c in controls
    ]
    rows = []
    for t in tenants:
        data = _evidence_row(conn, t, now, stored)
        row = RollupRow(dict(t), note=data["note"], attention=data["attention"])
        if not row.note or data["controls"]:
            row.values["in_place"], row.values["gaps"] = data["in_place"], data["gaps"]
        for key, status in data["controls"].items():
            row.values[key] = status
            row.tones[key] = STATUS_TONES.get(status, "info")
        rows.append(row)
    return Rollup(
        "evidence",
        "Insurance evidence",
        "The controls cyber-insurance questionnaires ask about, answered for every client.",
        columns,
        rows,
        now,
        tenant_link="evidence",
        footnote="Backups and training are outside Microsoft 365 data and are left out here.",
        scrolls="a column per control",
    )


def _evidence_row(conn, tenant, now: datetime, stored: Rows | None) -> dict:
    """{note, in_place, gaps, attention, controls: {key: status}}, from the store or built (and stored)."""
    fingerprint = stored.fingerprint(tenant) if stored else ""
    if stored and (data := stored.get(tenant, fingerprint)) is not None:
        return data
    try:
        pack = evidence.build(conn, tenant, now)
    except Exception as exc:
        logging.getLogger(__name__).exception("Evidence rollup failed for tenant %s", tenant["id"])
        note = f"Couldn't be built: {type(exc).__name__}"
        return {"note": note, "in_place": 0, "gaps": 0, "attention": 0, "controls": {}}
    counts = pack.counts
    data = {
        "note": "No data collected yet." if counts[evidence.NOT_ASSESSED] == len(pack.controls) else "",
        "in_place": counts[evidence.IN_PLACE],
        "gaps": counts[evidence.PARTLY] + counts[evidence.NOT_IN_PLACE],
        "attention": counts[evidence.NOT_IN_PLACE] * 10 + counts[evidence.PARTLY],
        "controls": {c.control.key: c.status for c in pack.controls if not c.control.outside_scope},
    }
    if stored:
        stored.save(tenant, fingerprint, data)
    return data


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


def _encode(value):
    """A headline value as JSON: Money keeps its currency, anything not JSON becomes its text."""
    if isinstance(value, Money):
        return {"money": float(value), "currency": value.currency}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _decode(value):
    if isinstance(value, dict) and "money" in value:
        return Money(value["money"], value["currency"])
    return value


def _data_row(
    conn, tenant, key: str, now: datetime, stored: Rows | None, snap: TenantSnapshot | None = None, built=None
) -> dict:
    """One tenant's line on a detail report's rollup, from the store or built (and stored):
    {note, flagged, flag_link, headlines: [[label, value, tone, href]], tables: [[title, rows, anchor]]}.
    Both shapes are kept, since the rollup shows headlines when any tenant's report has them. ``built`` holds
    reports already built from ``snap`` by key (``month_changes.Current``)."""
    fingerprint = stored.fingerprint(tenant) if stored else ""
    if stored and (data := stored.get(tenant, fingerprint)) is not None:
        return data
    report = built.get(key) if built is not None else None
    if report is None:
        report = data_reports.build(conn, tenant, key, now, snap=snap)
    data = {"note": report.unavailable or "", "flagged": 0, "flag_link": "", "headlines": [], "tables": []}
    if not report.unavailable:
        data["flagged"] = sum(s.flagged for s in report.sections)
        data["flag_link"] = views.flagged_link(report) if data["flagged"] else ""
        targets = views.headline_links(report)
        for h, href in zip(
            _headlines(report), targets["tiles"] if report.tiles else targets["highlights"], strict=True
        ):
            data["headlines"].append([h.label, _encode(h.value), h.tone, href])
        for s, table in zip(report.sections, views.table_slugs(report), strict=True):
            if not is_settings(s):
                data["tables"].append([s.title, len(s.rows), f"#{views.anchor(table)}"])
    if stored:
        stored.save(tenant, fingerprint, data)
    return data


def data_rollup(
    conn,
    tenants,
    key: str,
    now: datetime | None = None,
    rebuild: bool = False,
    snap: TenantSnapshot | None = None,
    reports: dict | None = None,
) -> Rollup:
    """Columns are the report's headline figures when it has them, else a row count per table.

    Without ``now`` (the page), each tenant's line comes from ``precomputed`` while that's current, and is
    built and stored when it isn't (always, with ``rebuild``). ``snap`` is one tenant's snapshot, when the
    caller has it (the worker building every report's rollup for a tenant), and ``reports`` the reports
    already built from it.
    """
    stored = stored_rows(conn, f"rollup:{key}", tenants, now, rebuild)
    now = now or datetime.now(UTC)
    title, description, _collectors, _builder = data_reports.REPORTS[key]
    one = len(tenants) == 1
    built = [
        (t, _data_row(conn, t, key, now, stored, snap if one else None, reports if one else None)) for t in tenants
    ]
    use_headlines = any(d["headlines"] for _t, d in built if not d["note"])
    labels: list[str] = []
    for _t, data in built:
        items = [h[0] for h in data["headlines"]] if use_headlines else [tb[0] for tb in data["tables"]]
        labels += [x for x in items if x not in labels]
    columns = [Column("_flagged", "To review", hint="Rows flagged critical, high or medium")]
    columns += [Column(f"c:{name}", name, "text" if use_headlines else "num") for name in labels]
    rows = []
    for t, data in built:
        row = RollupRow(dict(t))
        if data["note"]:
            row.note = data["note"]
            rows.append(row)
            continue
        row.values["_flagged"] = data["flagged"]
        row.attention = data["flagged"]
        if data["flag_link"]:
            row.links["_flagged"] = (key, data["flag_link"])
        if use_headlines:
            for label, value, tone, href in data["headlines"]:
                row.values[f"c:{label}"] = _decode(value)
                if tone:
                    row.tones[f"c:{label}"] = tone
                if href:
                    row.links[f"c:{label}"] = (key, href)
        else:
            for table_title, count, anchor in data["tables"]:
                row.values[f"c:{table_title}"] = count
                row.links[f"c:{table_title}"] = (key, anchor)
        rows.append(row)
    footnote = (
        "Figures are the report's headline numbers for each client."
        if use_headlines
        else "Numbers count the rows in each of the report's tables."
    ) + " Each client's own report has the detail."
    return Rollup(key, title, description, columns, rows, now, tenant_link="data", footnote=footnote)


# -- Every row of a detail report, across tenants ----------------------------------

MAX_ROWS = 10_000  # per table: more than a page or a PDF can sensibly hold
TENANT = "Client"


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


def all_rows(conn, tenants, key: str, now: datetime | None = None, href=None, label: str = "All clients") -> AllRows:
    """Build the report for each tenant (with the same builders as its own page) and stack the rows.

    ``href(tenant)``, when given, is the tenant's own report page: the Tenant cell links to its table.
    ``label`` names the tenants on downloads (a tenant group's name when the page shows one).
    """
    now = now or datetime.now(UTC)
    title, description, _collectors, _builder = data_reports.REPORTS[key]
    combined = DataReport(
        key, title, description, {"id": 0, "name": label, "tenant_ref": plural(len(tenants), "client")}, now
    )
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
                out = sections[s.title] = Section(
                    s.title,
                    [TENANT, *s.columns],
                    intro=s.intro,
                    empty=s.empty,
                    compare=s.compare,
                    chart=s.chart,
                    optional=s.optional + s.optional_across,
                )
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
        combined.unavailable = "No client has data for this report yet." if tenants else "No clients to show."
    return AllRows(combined, dict(totals), without)


def one_table(rows: AllRows, title: str, view: views.View | None = None) -> DataReport:
    """The combined report narrowed to one table, with a table view applied, for downloads."""
    report = views.apply(rows.report, view or views.View())
    out = copy.copy(report)
    out.sections = [s for s in report.sections if s.title == title]
    out.title = f"{report.title}: {title}" if title != report.title else report.title
    out.filtered = [line for line in report.filtered if line.startswith(f"{title}:")]
    cut = rows.truncated(title)
    if cut:
        out.notice = (
            f"Only the first {MAX_ROWS:,} of {cut:,} rows are included. Filter the table on the page, "
            "or open each client's own report for the rest."
        )
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
        header = ["Client", "Tenant ID", "Failing", "Accepted", "Not checked", "Note"]
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
            active = [f for f in r.findings if not ((lc := life.get((r.key, finding_key(f)))) and lc.accepted)]
            if r.status == "fail" and (active or not r.findings):
                worst = min((f.severity for f in active), key=SEVERITY_RANK.__getitem__, default=None)
                row.cells[r.key] = CheckCell("fail", r.summary, worst, len(active))
            else:
                row.cells[r.key] = CheckCell("accepted" if r.status == "fail" else r.status, r.summary)
    if stored:
        stored.save(
            tenant,
            fingerprint,
            {"note": row.note, "cells": {k: [c.status, c.summary, c.severity, c.count] for k, c in row.cells.items()}},
        )
    return row


# -- Registry --------------------------------------------------------------------------


def available() -> list[tuple[str, str]]:
    """(key, title) of every rollup, in menu order. Read at call time so new reports appear automatically."""
    return [("review", "Monthly review"), ("evidence", "Insurance evidence")] + [
        (k, v[0]) for k, v in data_reports.REPORTS.items()
    ]


def build(conn, tenants, key: str, now: datetime | None = None) -> Rollup:
    """Without ``now`` (the pages, which show the present) each tenant's row comes from the stored rows
    (``rollup_cache`` for the monthly review, ``precomputed`` for the rest) while those are current."""
    if key == "review":
        rollup = review_rollup(conn, tenants, now or datetime.now(UTC), cached=now is None)
    elif key == "evidence":
        rollup = evidence_rollup(conn, tenants, now)
    elif key in data_reports.REPORTS:
        rollup = data_rollup(conn, tenants, key, now)
    else:
        raise KeyError(key)
    rollup.rows.sort(key=lambda r: (bool(r.note), -r.attention, r.tenant["name"].lower()))
    return rollup
