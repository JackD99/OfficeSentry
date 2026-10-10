"""The Findings page as a report: open findings, accepted risks and resolved findings as tables.

Built from the ``findings`` table (``findings.sync`` keeps it), not from a snapshot, so it isn't a
registered detail report. It returns the same ``DataReport`` of ``Section``s, so the page uses the
shared report table (``views``: value filters, saved views, sort and hidden columns) and the PDF,
XLSX and CSV downloads apply the same view. Each table groups its rows by check under collapsible
headings (``Section.group``); row keys are finding ids, for the accept and undo actions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..checks import RULES, SEVERITY_RANK
from ..snapshot import TenantSnapshot
from .data_reports import DataReport, Rows, Section, Tile, UserLink, people_index, user_path

KEY = "findings"  # saved views for the Open page
RESOLVED_KEY = "findings-resolved"  # and for the Resolved page
TITLES = {KEY: "Findings", RESOLVED_KEY: "Resolved findings"}

OPEN = "Open findings"
ACCEPTED = "Accepted risks"
RESOLVED = "Resolved findings"
CHECK = "Check"
SEVERITY = "Severity"
FIRST_SEEN = "First seen"

_ISO = "%Y-%m-%dT%H:%M:%SZ"
_ORDER = (
    "CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2"
    " WHEN 'low' THEN 3 ELSE 4 END, rule_key, subject COLLATE NOCASE"
)


def is_accepted(row, now: datetime) -> bool:
    """Accepted and not past its review date (the same test as ``findings.lifecycle_map``)."""
    return bool(row["accepted_at"]) and (row["accepted_until"] is None or row["accepted_until"] > now.strftime(_ISO))


def _day(value: str | None) -> str:
    return (value or "")[:10]


def _by_check(rows: list) -> list:
    """One check's findings together, the check with the worst finding first (the rows arrive worst first)."""
    order: dict[str, int] = {}
    for row in rows:
        order.setdefault(row["rule_key"], len(order))
    return sorted(rows, key=lambda r: order[r["rule_key"]])


class _Builder:
    def __init__(self, conn, tenant, now: datetime):
        self.tenant = dict(tenant)
        self.now = now
        self.rules = {r.key: r for r in RULES}
        snap = TenantSnapshot(conn, tenant)
        self.people = people_index(snap)
        self.user_ids = set(self.people.values())
        self.sources = [
            (
                "Microsoft 365",
                max((r["finished_at"] for r in snap.runs.values() if r and r["finished_at"]), default=None),
            )
        ]
        self.anchors: set[str] = set()

    def title(self, rule_key: str) -> str:
        rule = self.rules.get(rule_key)
        return rule.title if rule else rule_key

    def subject(self, row):
        """The account or item, linked to the person's page when it is one."""
        user_id = (
            row["object_id"] if row["object_id"] in self.user_ids else self.people.get(str(row["subject"]).lower())
        )
        return UserLink(row["subject"], user_path(self.tenant["id"], user_id)) if user_id else row["subject"]

    def section(self, title: str, columns: list[str], rows: list, cells_of, intro: str, empty: str) -> Section:
        section = Section(
            title,
            columns,
            intro=intro,
            empty=empty,
            flag_name=SEVERITY,
            group=CHECK,
            filter_columns=(CHECK,),
            review=title == OPEN,
        )
        for row in _by_check(rows):
            check = self.title(row["rule_key"])
            cells = cells_of(row, check)
            cells[0] = self.subject(row)
            section.add(cells, row["severity"] if row["severity"] in SEVERITY_RANK else None, row["id"])
            # "#check-<key>" (from Recommended projects and the check matrix) opens the check's first group.
            anchor = f"check-{row['rule_key']}"
            if check not in section.group_ids and anchor not in self.anchors:
                section.group_ids[check] = anchor
                self.anchors.add(anchor)
        return section


def build(conn, tenant, resolved: bool = False, now: datetime | None = None) -> DataReport:
    """Open findings and accepted risks, or (``resolved``) the findings a later collection no longer saw."""
    now = now or datetime.now(UTC)
    b = _Builder(conn, tenant, now)
    if resolved:
        report = DataReport(
            RESOLVED_KEY,
            "Resolved findings",
            "Findings a later collection no longer saw. They're kept for the period set in Data "
            "retention, and reopen if the problem comes back.",
            b.tenant,
            now,
            sources=b.sources,
        )
        rows = conn.execute(
            f"SELECT * FROM findings WHERE tenant_id = ? AND resolved_at IS NOT NULL ORDER BY {_ORDER}", (tenant["id"],)
        ).fetchall()
        section = b.section(
            RESOLVED,
            ["Account or item", CHECK, "Detail", FIRST_SEEN, "Resolved"],
            rows,
            lambda f, check: [f["subject"], check, f["detail"], _day(f["first_seen"]), _day(f["resolved_at"])],
            "",
            "Nothing resolved yet. Findings move here when a later collection no longer sees the problem.",
        )
        report.sections = [section]
        recent = sum(1 for f in rows if (f["resolved_at"] or "") >= (now - timedelta(days=30)).strftime(_ISO))
        report.tiles = [
            Tile(RESOLVED, len(rows), "kept for the retention period", "ok" if rows else None),
            Tile(
                "Resolved in the last 30 days",
                recent,
                "",
                "ok" if recent else None,
                rows=Rows(RESOLVED, age=("Resolved", "lt30")),
            ),
        ]
        return report

    report = DataReport(
        KEY,
        "Findings",
        "Each row is an account or setting a check flagged. Findings close on their own once the "
        "next collection no longer sees the problem.",
        b.tenant,
        now,
        sources=b.sources,
    )
    rows = conn.execute(
        f"SELECT * FROM findings WHERE tenant_id = ? AND resolved_at IS NULL ORDER BY {_ORDER}", (tenant["id"],)
    ).fetchall()
    open_rows = [f for f in rows if not is_accepted(f, now)]
    accepted = [f for f in rows if is_accepted(f, now)]
    category = {r.key: r.category for r in RULES}
    findings = b.section(
        OPEN,
        ["Account or item", CHECK, "Detail", "Category", FIRST_SEEN],
        open_rows,
        lambda f, check: [f["subject"], check, f["detail"], category.get(f["rule_key"], ""), _day(f["first_seen"])],
        "",
        "No open findings. Either everything passed or no data has been collected yet.",
    )
    risks = b.section(
        ACCEPTED,
        ["Account or item", CHECK, "Detail", "Accepted by", "Reason", "Review on", FIRST_SEEN],
        accepted,
        lambda f, check: [
            f["subject"],
            check,
            f["detail"],
            f["accepted_by"],
            f["accepted_note"],
            _day(f["accepted_until"]),
            _day(f["first_seen"]),
        ],
        "Accepted on purpose, so they're left out of the totals and the monthly review's findings. "
        "Each one comes back on its review date.",
        "No accepted risks. When something is intentional, accept the risk from its row above and "
        "it moves here with the reason.",
    )
    report.sections = [findings, risks]

    severe = sum(1 for f in open_rows if f["severity"] in ("critical", "high"))
    since = now - timedelta(days=30)
    month_ago = since.strftime(_ISO)
    new = sum(1 for f in open_rows if (f["first_seen"] or "") >= month_ago)
    old = sum(1 for f in open_rows if (f["first_seen"] or "") < (now - timedelta(days=90)).strftime(_ISO))
    checks = len({f["rule_key"] for f in open_rows})
    report.tiles = [
        Tile(
            OPEN,
            len(open_rows),
            f"from {checks} check{'s' if checks != 1 else ''}" if open_rows else "",
            "warn" if open_rows else "ok",
        ),
        Tile(
            "Critical or high",
            severe,
            "fix these first" if severe else "",
            "bad" if severe else "ok",
            rows=Rows(OPEN, {SEVERITY: ["critical", "high"]}),
        ),
        Tile(
            "New in 30 days", new, f"first seen since {since.day} {since:%b}", rows=Rows(OPEN, age=(FIRST_SEEN, "lt30"))
        ),
        Tile("Open over 90 days", old, "", "warn" if old else None, rows=Rows(OPEN, age=(FIRST_SEEN, "gt90"))),
        Tile(ACCEPTED, len(accepted), "left out of the totals"),
    ]
    return report
