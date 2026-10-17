"""A grade for every client: A to E, the worst of five parts, with the rules published.

The rules, which the grade page and ``docs/client-grades.md`` publish so an MSP can explain a grade to a
client:

* The checks are grouped into five **parts** (``PARTS``): admin accounts, sign-in and accounts, mail, devices,
  and sharing and apps. Checks about cost, capacity and housekeeping (licences, storage quotas, directory
  sync, a blocked manager) don't count towards a grade (``UNGRADED``).
* A **part's grade** is its worst open finding: a critical finding makes it an E, a high finding a D, a
  medium a C, only low findings a B, and a part whose every check passed is an A. Accepted risks are set
  aside first, so a risk the client has signed off doesn't count against them. An "info" finding is a note,
  not a problem, and doesn't count either.
* A part is only graded when at least half its checks could run; otherwise it says why (no Intune, no
  Entra ID P1, nothing collected yet) and the grade is the worst of the parts that were.
* The **grade** is the worst part, so one critical gap keeps a client out of A and B, and each part says
  what would move it: fix the checks at its worst severity and the part takes its next grade.
* **History** is the grade on each month-end for the last twelve months, worked out from the findings open
  then (first seen by then, not resolved, not an accepted risk), with each finding's current severity and
  the parts graded today, from the client's first collection on. Nothing extra is stored for it.
* The **comparison** sets a client beside this install's other clients of about the same size (``BANDS``
  by licensed users, or every other client when fewer than ``MIN_PEERS`` are in the band): the median grade
  and figures, and how many the client is better than. No other client is ever named, and nothing leaves
  the install. On a staff page the others are the clients that person sees; in a client's monthly review
  they are the MSP's active clients.

``grade()`` is a pure function of check results with accepted risks set aside; ``assess()`` evaluates a
tenant's stored snapshot and grades it; ``rows()`` is each tenant's stored grade for the lists (a
``summary_cache`` kind, ``reports/precomputed.py``). Nothing here calls Microsoft.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from statistics import median

from .checks import RULES, SEVERITY_RANK, RuleResult, evaluate
from .findings import finding_key, lifecycle_map
from .reports.precomputed import Rows, stored_rows
from .snapshot import TenantSnapshot
from .text import plural

log = logging.getLogger(__name__)

ISO = "%Y-%m-%dT%H:%M:%SZ"
KIND = "grade"  # the summary_cache kind
LETTERS = "ABCDE"
# What a part's worst open finding makes it. "info" isn't here: a note, not a problem, it doesn't count.
SEVERITY_GRADE = {"critical": "E", "high": "D", "medium": "C", "low": "B"}
GRADE_SEVERITY = {v: k for k, v in SEVERITY_GRADE.items()}
MONTHS = 12  # months of history
MIN_PEERS = 2  # other clients a comparison needs before anything is said
# Shown in the client's monthly review unless the MSP switches it off (Settings → Branding).
SHOW_SETTING = "grade.in_reports"
COMPARE_SETTING = "grade.compare_in_reports"


@dataclass(frozen=True)
class Part:
    key: str
    title: str
    covers: str  # what it's about, in a line, for the published rules
    checks: tuple[str, ...]


PARTS: tuple[Part, ...] = (
    Part(
        "admins",
        "Admin accounts",
        "Who holds admin roles: whether every admin has MFA registered, uses a phishing-resistant method and is "
        "covered by a policy that requires MFA, how many Global Administrators there are, guests and apps with "
        "admin roles, whether admins are cloud-only, and changes to how domains sign in.",
        (
            "admin_mfa",
            "admins_ca_mfa",
            "admin_phishing_resistant",
            "global_admin_count",
            "guest_admins",
            "service_principal_admins",
            "cloud_only_admins",
            "domain_federation_changed",
        ),
    ),
    Part(
        "signin",
        "Sign-in and accounts",
        "Everyone else's sign-in: whether MFA is enforced and registered, legacy sign-in methods, text or call as "
        "the only second factor, risky sign-ins and at-risk accounts, inactive accounts, password hash sync, and "
        "open Defender alerts.",
        (
            "mfa_enforced",
            "users_mfa_registered",
            "legacy_auth_blocked",
            "phone_only_mfa",
            "risky_sign_ins",
            "risky_users",
            "stale_users",
            "password_hash_sync",
            "defender_alerts",
        ),
    ),
    Part(
        "mail",
        "Mail",
        "Forwarding outside the organisation and inbox rules that hide mail, mail flow rules, spam, spoof and "
        "malware filtering, the audit log and mailbox auditing, SMTP AUTH, shared mailbox sign-in, and each "
        "domain's SPF, DKIM, DMARC, MTA-STS and TLS-RPT.",
        (
            "external_forwarding",
            "suspicious_inbox_rules",
            "auto_forward_policy",
            "shared_mailbox_signin",
            "audit_log",
            "mailbox_auditing",
            "smtp_auth",
            "mail_rule_spam_bypass",
            "mail_rule_external_copy",
            "spam_allowed_domains",
            "spoof_protection",
            "malware_file_filter",
            "spf_record",
            "dkim_enabled",
            "dmarc_policy",
            "mta_sts",
            "tls_rpt",
        ),
    ),
    Part(
        "devices",
        "Devices",
        "Intune enrolment, compliance policies and whether devices meet them, encryption and saved BitLocker "
        "keys, Windows versions still getting updates, devices checking in, old devices left in Entra ID, and "
        "LAPS.",
        (
            "device_compliance",
            "device_encryption",
            "supported_windows",
            "intune_enrolled",
            "device_checkin",
            "stale_devices",
            "bitlocker_keys",
            "laps_backups",
            "intune_compliance_coverage",
        ),
    ),
    Part(
        "sharing",
        "Sharing and apps",
        "Anyone links and sharing outside the organisation, site owners and guests with full control, who can "
        "invite guests and create groups, ownerless groups and group expiry, what users may do with apps and "
        "registrations, and third-party apps' permissions and credentials.",
        (
            "anyone_links",
            "site_owners",
            "site_guest_full_control",
            "outside_edit_access",
            "sharepoint_sharing",
            "guest_invites",
            "pending_guests",
            "guest_owners",
            "ownerless_groups",
            "group_creation",
            "group_expiration",
            "guest_access_level",
            "email_verified_join",
            "tenant_creation",
            "app_registration",
            "admin_consent_workflow",
            "user_consent",
            "risky_app_permissions",
            "app_credentials",
        ),
    ),
)
# Checks about cost, capacity and housekeeping, which say nothing about how exposed a client is.
UNGRADED = frozenset(
    {
        "licensed_disabled",
        "unassigned_licences",
        "licence_assignment_errors",
        "subscriptions_lapsing",
        "mailbox_quota",
        "onedrive_quota",
        "sharepoint_storage_space",
        "directory_sync_running",
        "blocked_manager",
    }
)
PART_OF: dict[str, str] = {check: part.key for part in PARTS for check in part.checks}
BY_KEY: dict[str, Part] = {part.key: part for part in PARTS}


def article(letter: str) -> str:
    """ "an A", "a B"."""
    return "an" if letter in "AE" else "a"


def worse(a: str | None, b: str | None) -> str | None:
    """The worse of two grades (E is worst); None is no grade."""
    if a is None or b is None:
        return a or b
    return max(a, b)


# -- Grading ---------------------------------------------------------------------------------------


@dataclass
class Pull:
    """A failing check that pulls its part down."""

    key: str
    title: str
    summary: str  # what was found
    action: str  # the recommendation's first instruction
    severity: str
    count: int  # accounts or items affected (after accepted risks)

    @property
    def grade(self) -> str:
        return SEVERITY_GRADE[self.severity]


@dataclass
class PartGrade:
    key: str
    title: str
    grade: str | None  # None: not graded (see reason)
    ran: int  # checks that could run
    total: int
    pulls: list[Pull] = field(default_factory=list)  # failing checks, worst first
    reason: str = ""  # why it isn't graded
    why: list[str] = field(default_factory=list)  # the unavailable checks' reasons, distinct

    @property
    def worst(self) -> str | None:
        return self.pulls[0].severity if self.pulls else None

    @property
    def moves(self) -> list[Pull]:
        """The checks at the part's worst severity: fixing them moves the part to ``next_grade``."""
        return [p for p in self.pulls if p.severity == self.worst]

    @property
    def next_grade(self) -> str | None:
        """What the part becomes once ``moves`` are fixed."""
        if self.grade is None or not self.pulls:
            return None
        rest = [p for p in self.pulls if p.severity != self.worst]
        return SEVERITY_GRADE[rest[0].severity] if rest else "A"

    @property
    def partial(self) -> bool:
        return self.grade is not None and self.ran < self.total


@dataclass
class Grade:
    letter: str | None
    parts: list[PartGrade]
    ran: int  # graded checks that could run, across every part
    total: int
    reason: str = ""  # why there's no grade

    @property
    def worst_parts(self) -> list[PartGrade]:
        return [p for p in self.parts if p.grade is not None and p.grade == self.letter]

    @property
    def graded(self) -> list[PartGrade]:
        return [p for p in self.parts if p.grade is not None]

    @property
    def ungraded(self) -> list[PartGrade]:
        return [p for p in self.parts if p.grade is None]

    @property
    def partial(self) -> bool:
        """Fewer than half the graded checks could run: worth saying beside the letter."""
        return self.letter is not None and self.ran * 2 < self.total

    @property
    def next_letter(self) -> str | None:
        """The grade once each worst part's ``moves`` are fixed: what the client gets for the next step."""
        if self.letter is None or self.letter == "A":
            return None
        best: str | None = None
        for p in self.graded:
            best = worse(best, p.next_grade if p.grade == self.letter else p.grade)
        return best

    @property
    def to_fix(self) -> list[Pull]:
        """Every check the next step is made of, worst part first."""
        return [pull for p in self.worst_parts for pull in p.moves]

    def sentence(self, name: str) -> str:
        """The page's opening line: the grade and what holds it there."""
        if self.letter is None:
            return f"{name} isn't graded yet: {self.reason}."
        worst = self.worst_parts
        if self.letter == "A":
            return f"{name} is an A: every check that could run passed."
        names = _join([p.title for p in worst])
        first = worst[0].pulls[0]
        return f"{name} is {article(self.letter)} {self.letter} because of {names}: {_lower(first.summary)}."

    def next_step(self, name: str) -> str:
        """What moves the grade, in a line; "" for an A or an ungraded client."""
        nxt = self.next_letter
        if nxt is None:
            return ""
        return (
            f"Fix {plural(len(self.to_fix), 'check')} in {_join([p.title for p in self.worst_parts])} "
            f"and {name} is {article(nxt)} {nxt}."
        )


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _lower(text: str) -> str:
    """A check's summary inside a sentence: "MFA is not enforced" keeps its capital, "No files..." doesn't."""
    text = text.strip().rstrip(".")
    if not text or text[0].islower() or (len(text) > 1 and text[1].isupper()):
        return text
    return text[0].lower() + text[1:]


def active(results: list[RuleResult], lifecycle: dict | None) -> list[RuleResult]:
    """The results with accepted risks set aside (as the monthly review and the check matrix do): a check
    whose every finding is accepted no longer fails. Without a lifecycle map the results are taken as they
    are (the monthly review has already applied it)."""
    if lifecycle is None:
        return results
    out = []
    for r in results:
        kept = [f for f in r.findings if not ((life := lifecycle.get((r.key, finding_key(f)))) and life.accepted)]
        status = "accepted" if r.status == "fail" and r.findings and not kept else r.status
        out.append(RuleResult(r.key, r.title, r.category, status, r.summary, r.recommendation, kept, r.keep))
    return out


def grade(results: list[RuleResult], lifecycle: dict | None = None) -> Grade:
    """Grade a tenant from its check results, accepted risks set aside (``lifecycle``, from
    ``findings.lifecycle_map``, or already applied)."""
    by_key = {r.key: r for r in active(results, lifecycle)}
    parts = []
    for part in PARTS:
        pulls, why = [], []
        ran = 0
        for key in part.checks:
            r = by_key.get(key)
            if r is None or r.status == "unavailable":
                if r is not None and r.summary and r.summary not in why:
                    why.append(r.summary)
                continue
            ran += 1
            if r.status != "fail":
                continue
            counted = [f for f in r.findings if f.severity in SEVERITY_GRADE]
            if not counted:
                continue
            worst = min((f.severity for f in counted), key=SEVERITY_RANK.__getitem__)
            pulls.append(Pull(r.key, r.title, r.summary, r.action, worst, len(counted)))
        pulls.sort(key=lambda p: (SEVERITY_RANK[p.severity], -p.count, p.title))
        total = len(part.checks)
        if ran * 2 < total:
            reason = "No check could run" if not ran else f"Only {ran} of {total} checks could run"
            parts.append(PartGrade(part.key, part.title, None, ran, total, pulls, reason, why[:3]))
        else:
            letter = SEVERITY_GRADE[pulls[0].severity] if pulls else "A"
            parts.append(PartGrade(part.key, part.title, letter, ran, total, pulls, "", why[:3]))
    graded = [p for p in parts if p.grade is not None]
    ran = sum(p.ran for p in parts)
    total = sum(p.total for p in parts)
    if not graded:
        reason = "no data has been collected yet" if not ran else "too few checks could run"
        return Grade(None, parts, ran, total, reason)
    return Grade(max(p.grade for p in graded), parts, ran, total)


def assess(conn, tenant, now: datetime | None = None, results: list[RuleResult] | None = None) -> Grade:
    """Grade a tenant from its stored data: evaluate every check (or take ``results`` already evaluated, with
    accepted risks still in them) and set accepted risks aside."""
    now = now or datetime.now(UTC)
    if results is None:
        snap = TenantSnapshot(conn, tenant)
        if not snap.runs:
            return Grade(None, grade([]).parts, 0, sum(len(p.checks) for p in PARTS), "no data has been collected yet")
        results = evaluate(snap, now)
    return grade(results, lifecycle_map(conn, tenant["id"], now))


# -- History -------------------------------------------------------------------------------------


@dataclass
class MonthGrade:
    month: date  # the first day of the month
    letter: str | None  # None before the first collection
    current: bool = False  # this month, so far

    @property
    def label(self) -> str:
        return f"{self.month:%b}"

    @property
    def name(self) -> str:
        return f"{self.month:%B %Y}"


@dataclass
class Trend:
    """How today's grade compares with earlier month-ends: "up from D in July"."""

    direction: str  # up | down | same | new
    previous: str | None = None  # the grade before the change
    month: date | None = None  # when the current grade first showed (a month-end), or the first graded month

    @property
    def text(self) -> str:
        when = f"{self.month:%B}" if self.month else ""
        if self.direction == "up":
            return f"Up from {self.previous} in {when}"
        if self.direction == "down":
            return f"Down from {self.previous} in {when}"
        if self.direction == "same":
            return f"Unchanged since {when}"
        return "First graded this month"


def _month_starts(now: datetime, count: int) -> list[date]:
    first = date(now.year, now.month, 1)
    out = [first]
    while len(out) < count:
        d = out[-1]
        out.append(date(d.year - (d.month == 1), 12 if d.month == 1 else d.month - 1, 1))
    return out[::-1]


def _month_end(month: date) -> str:
    """The last instant of the month, as stored timestamps compare."""
    nxt = date(month.year + (month.month == 12), 1 if month.month == 12 else month.month + 1, 1)
    return (datetime(nxt.year, nxt.month, 1, tzinfo=UTC) - timedelta(seconds=1)).strftime(ISO)


def history(conn, tenant_id: int, graded: Grade, now: datetime | None = None, months: int = MONTHS):
    """The grade on each of the last ``months`` month-ends (this month so far), from the findings open then.
    Parts not graded today aren't graded then either, and months before the first collection have no grade.
    ``graded`` is today's grade, whose current month the last entry repeats."""
    now = now or datetime.now(UTC)
    starts = _month_starts(now, months)
    first = conn.execute(
        "SELECT MIN(finished_at) FROM runs WHERE tenant_id = ? AND status IN ('succeeded', 'partial')", (tenant_id,)
    ).fetchone()[0]
    keys = {p.key for p in graded.parts if p.grade is not None}
    out = [MonthGrade(m, None) for m in starts]
    if first is None or not keys:
        if out:
            out[-1] = MonthGrade(starts[-1], graded.letter, current=True)
        return out
    since = datetime(starts[0].year, starts[0].month, 1, tzinfo=UTC).strftime(ISO)
    rows = conn.execute(
        "SELECT rule_key, severity, first_seen, resolved_at, accepted_at, accepted_until FROM findings"
        " WHERE tenant_id = ? AND (resolved_at IS NULL OR resolved_at >= ?)",
        (tenant_id, since),
    ).fetchall()
    found = [
        (
            PART_OF[r["rule_key"]],
            r["severity"],
            r["first_seen"],
            r["resolved_at"],
            r["accepted_at"],
            r["accepted_until"],
        )
        for r in rows
        if r["rule_key"] in PART_OF and r["severity"] in SEVERITY_GRADE and PART_OF[r["rule_key"]] in keys
    ]
    for i, month in enumerate(starts[:-1]):
        at = _month_end(month)
        if at < first:
            continue
        worst: dict[str, str] = {}
        for part, severity, seen, resolved, accepted_at, accepted_until in found:
            if seen > at or (resolved and resolved <= at):
                continue
            if accepted_at and accepted_at <= at and (accepted_until is None or accepted_until > at):
                continue
            if part not in worst or SEVERITY_RANK[severity] < SEVERITY_RANK[worst[part]]:
                worst[part] = severity
        letters = [SEVERITY_GRADE[worst[k]] if k in worst else "A" for k in keys]
        out[i] = MonthGrade(month, max(letters))
    out[-1] = MonthGrade(starts[-1], graded.letter, current=True)
    return out


def trend(months: list[MonthGrade]) -> Trend | None:
    """Today's grade against the month-ends before it, or None without a grade."""
    if not months or months[-1].letter is None:
        return None
    today = months[-1].letter
    earlier = [m for m in months[:-1] if m.letter is not None]
    if not earlier:
        return Trend("new")
    changed = next((m for m in reversed(earlier) if m.letter != today), None)
    if changed is None:
        return Trend("same", today, earlier[0].month)
    after = next(m for m in months if m.month > changed.month)
    return Trend("up" if changed.letter > today else "down", changed.letter, after.month)


# -- Comparison ----------------------------------------------------------------------------------

# Clients of about the same size, by licensed users: (least, most, label).
BANDS: tuple[tuple[int, int | None, str], ...] = (
    (0, 24, "up to 24 users"),
    (25, 99, "25 to 99 users"),
    (100, 499, "100 to 499 users"),
    (500, None, "500 or more users"),
)


def band_of(licensed: float | None) -> tuple[int, int | None, str] | None:
    if licensed is None:
        return None
    for least, most, label in BANDS:
        if licensed >= least and (most is None or licensed <= most):
            return least, most, label
    return None


@dataclass
class Peer:
    """One client's figures for the comparison; never its name."""

    letter: str | None
    licensed: float | None = None
    score: float | None = None  # Secure Score, %
    mfa: float | None = None  # users with MFA, %
    severe: int | None = None  # open critical and high findings
    waste_per_user: float | None = None  # licence waste a month per licensed user


@dataclass
class Figure:
    key: str
    label: str
    value: object  # this client's: a letter, a number or None
    median: object  # the others'
    unit: str = ""  # grade | % | count | currency
    better: str = "down"  # which way is good: up | down (a grade: earlier in the alphabet)

    @property
    def standing(self) -> str:
        """better | worse | same | "": how this client stands against the median."""
        if self.value is None or self.median is None:
            return ""
        if self.value == self.median:
            return "same"
        if self.unit == "grade":
            return "better" if self.value < self.median else "worse"
        return "better" if (self.value > self.median) == (self.better == "up") else "worse"


@dataclass
class Comparison:
    peers: int  # the other clients compared with
    band: str  # "25 to 99 users", or "" when compared with every other client
    better_than: int  # of them, how many have a worse grade
    graded: int  # of them, how many have a grade
    figures: list[Figure]


def _median_grade(letters: list[str]) -> str | None:
    if not letters:
        return None
    ordered = sorted(letters)
    return ordered[(len(ordered) - 1) // 2]  # the lower middle, so a tie reads as the better grade


def _median(values: list) -> float | None:
    known = [v for v in values if v is not None]
    return round(median(known), 1) if known else None


def compare(mine: Peer, others: list[Peer]) -> Comparison | None:
    """Set a client beside ``others`` (never named): the ones in its size band when at least ``MIN_PEERS`` are,
    else every other client, and none when there are still too few."""
    band = band_of(mine.licensed)
    label = ""
    if band is not None:
        least, most, label = band
        same = [
            p for p in others if p.licensed is not None and p.licensed >= least and (most is None or p.licensed <= most)
        ]
        if len(same) >= MIN_PEERS:
            others = same
        else:
            label = ""
    if len(others) < MIN_PEERS:
        return None
    letters = [p.letter for p in others if p.letter is not None]
    figures = [
        Figure("grade", "Grade", mine.letter, _median_grade(letters), "grade"),
        Figure("score", "Secure Score", mine.score, _median([p.score for p in others]), "%", "up"),
        Figure("mfa", "Users with MFA", mine.mfa, _median([p.mfa for p in others]), "%", "up"),
        Figure("severe", "Open critical and high findings", mine.severe, _median([p.severe for p in others]), "count"),
        Figure(
            "waste",
            "Licence waste per user, a month",
            mine.waste_per_user,
            _median([p.waste_per_user for p in others]),
            "currency",
        ),
    ]
    better = sum(1 for p in letters if mine.letter is not None and p > mine.letter)
    return Comparison(len(others), label, better, len(letters), figures)


def peer_of(letter: str | None, kpis: dict, counts: dict) -> Peer:
    """A client's figures from a monthly review's key figures (``monthly_review.Kpi`` by key) and severity
    counts, as the review and the rollup rows carry them."""

    def value(key: str):
        k = kpis.get(key)
        return None if k is None else k.value

    licensed = value("users.licensed")
    waste = value("waste")
    return Peer(
        letter,
        licensed,
        value("secure_score.pct"),
        value("mfa.registered_pct"),
        None if not counts else int(counts.get("critical", 0) + counts.get("high", 0)),
        round(waste / licensed, 2) if waste is not None and licensed else None,
    )


def peers(conn, tenant_ids: list[int]) -> list[Peer]:
    """The stored figures of other clients, as last built (the grade rows and the dashboard's rollup rows),
    in two queries. Clients without a stored grade yet are left out, and nothing is rebuilt here."""
    if not tenant_ids:
        return []
    marks = ",".join("?" * len(tenant_ids))
    letters = {
        r["tenant_id"]: json.loads(r["data"]).get("letter")
        for r in conn.execute(
            f"SELECT tenant_id, data FROM summary_cache WHERE kind = ? AND tenant_id IN ({marks})", (KIND, *tenant_ids)
        )
    }
    return [
        _peer_row(letters[r["tenant_id"]], json.loads(r["data"]))
        for r in conn.execute(f"SELECT tenant_id, data FROM rollup_cache WHERE tenant_id IN ({marks})", tenant_ids)
        if r["tenant_id"] in letters
    ]


def _peer_row(letter: str | None, data: dict) -> Peer:
    """A peer from a stored rollup row (``monthly_review.rollup_row``: the review's key figures and counts)."""
    kpis = {k["key"]: _Value(k.get("value")) for k in data.get("kpis", [])}
    return peer_of(letter, kpis, data.get("counts") or {})


def comparison(conn, tenant_id: int, letter: str | None, others: list[int]) -> Comparison | None:
    """This client, with the grade just worked out and its figures as last stored, beside ``others`` (the
    tenant ids the viewer sees, this one left out). Nothing is rebuilt here."""
    row = conn.execute("SELECT data FROM rollup_cache WHERE tenant_id = ?", (tenant_id,)).fetchone()
    mine = _peer_row(letter, json.loads(row["data"])) if row else Peer(letter)
    return compare(mine, peers(conn, [t for t in others if t != tenant_id]))


@dataclass
class _Value:
    value: object


# -- Stored rows (the lists) ---------------------------------------------------------------------


@dataclass
class Summary:
    """A tenant's grade as the lists show it: the letter, each part's, and how it moved."""

    tenant: dict
    letter: str | None
    parts: list[tuple[str, str, str | None]]  # (key, title, grade)
    ran: int
    total: int
    reason: str = ""
    trend: Trend | None = None
    note: str = ""  # why it couldn't be built

    @property
    def partial(self) -> bool:
        return self.letter is not None and self.ran * 2 < self.total


def summarise(conn, tenant, now: datetime | None = None, graded: Grade | None = None) -> Summary:
    """Build a tenant's summary now (its grade and trend), from ``graded`` when a page has already worked the
    grade out."""
    now = now or datetime.now(UTC)
    g = graded or assess(conn, tenant, now)
    return Summary(
        dict(tenant),
        g.letter,
        [(p.key, p.title, p.grade) for p in g.parts],
        g.ran,
        g.total,
        g.reason,
        trend(history(conn, tenant["id"], g, now)),
    )


def _stored(summary: Summary) -> dict:
    t = summary.trend
    return {
        "letter": summary.letter,
        "parts": [list(p) for p in summary.parts],
        "ran": summary.ran,
        "total": summary.total,
        "reason": summary.reason,
        "trend": [t.direction, t.previous, t.month.isoformat() if t.month else None] if t else None,
    }


def _from_stored(tenant, data: dict) -> Summary:
    t = data.get("trend")
    return Summary(
        dict(tenant),
        data.get("letter"),
        [tuple(p) for p in data.get("parts", [])],
        data.get("ran", 0),
        data.get("total", 0),
        data.get("reason", ""),
        Trend(t[0], t[1], date.fromisoformat(t[2]) if t[2] else None) if t else None,
    )


def rows(
    conn, tenants, now: datetime | None = None, rebuild: bool = False, graded: dict[int, Grade] | None = None
) -> dict[int, Summary]:
    """Each tenant's summary by id. Without ``now`` (the pages, which show the present), from ``precomputed``
    while that's current, else built and stored (always, with ``rebuild``). ``graded`` is a grade a page has
    already worked out for a tenant (the client overview, from its monthly review), used instead of
    evaluating the checks again."""
    stored = stored_rows(conn, KIND, tenants, now, rebuild)
    now = now or datetime.now(UTC)
    return {t["id"]: _row(conn, t, now, stored, (graded or {}).get(t["id"])) for t in tenants}


def _row(conn, tenant, now: datetime, stored: Rows | None, graded: Grade | None = None) -> Summary:
    fingerprint = stored.fingerprint(tenant) if stored else ""
    if stored and (data := stored.get(tenant, fingerprint)) is not None:
        return _from_stored(tenant, data)
    try:
        summary = summarise(conn, tenant, now, graded)
    except Exception as exc:  # one broken tenant mustn't blank the list
        log.exception("Grade failed for tenant %s", tenant["id"])
        return Summary(dict(tenant), None, [], 0, 0, "", None, f"Couldn't be worked out: {type(exc).__name__}")
    if stored:
        stored.save(tenant, fingerprint, _stored(summary))
    return summary


def stored_letter(conn, tenant_id: int) -> str | None:
    """A tenant's grade as last stored, or None: for places that must never build one (the client header on
    every page)."""
    row = conn.execute("SELECT data FROM summary_cache WHERE kind = ? AND tenant_id = ?", (KIND, tenant_id)).fetchone()
    return json.loads(row["data"]).get("letter") if row else None


def stored_letters(conn, tenant_ids) -> dict[int, str | None]:
    """Each of these tenants' grade as last stored (None when there's none yet): for the Clients page, which
    lists every client and so never builds a grade or waits to store one."""
    wanted = set(tenant_ids)
    rows = conn.execute("SELECT tenant_id, data FROM summary_cache WHERE kind = ?", (KIND,)).fetchall()
    found = {r["tenant_id"]: json.loads(r["data"]).get("letter") for r in rows if r["tenant_id"] in wanted}
    return {tid: found.get(tid) for tid in wanted}


# -- Settings ------------------------------------------------------------------------------------


def _on(conn, key: str) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row is None or row["value"] != "0"


def shown_to_clients(conn) -> bool:
    """Whether the grade goes in the client's monthly review (on unless the MSP switched it off)."""
    return _on(conn, SHOW_SETTING)


def compared_for_clients(conn) -> bool:
    """Whether the review also says how the client compares with similar organisations the MSP looks after."""
    return _on(conn, COMPARE_SETTING)


def set_shown(conn, show: bool, compare: bool) -> None:
    for key, value in ((SHOW_SETTING, show), (COMPARE_SETTING, compare)):
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, "1" if value else "0"),
        )


def check_placement() -> list[str]:
    """Checks in no part and not in ``UNGRADED``, or in more than one: a new check must be placed. Empty when
    every check is placed once (``tests/test_grades.py``)."""
    problems = []
    seen: dict[str, str] = {}
    for part in PARTS:
        for key in part.checks:
            if key in seen:
                problems.append(f"{key} is in both {seen[key]} and {part.key}")
            if key in UNGRADED:
                problems.append(f"{key} is in {part.key} and UNGRADED")
            seen[key] = part.key
    known = {r.key for r in RULES}
    for key in list(seen) + sorted(UNGRADED):
        if key not in known:
            problems.append(f"{key} isn't a check")
    for key in sorted(known - set(seen) - UNGRADED):
        problems.append(f"{key} isn't in a part or in UNGRADED")
    return problems
