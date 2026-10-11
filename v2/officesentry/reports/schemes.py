"""Evidence packs by scheme: the certification, framework or form a client is going for, judged control by control.

A ``Scheme`` (Cyber Essentials, a cyber insurance proposal, ISO 27001's
Microsoft 365 controls, ...) is its requirement set, paraphrased and cited
with its version, grouped into the scheme's own themes. Each ``Control`` says
which checks evidence it, or which questionnaire topic answers it when the
data can't (firewalls, patch timing, backups), and what Microsoft 365 can and
can't show for it.

``assess()`` judges every control for one tenant from the stored data and the
check results: **Pass**, **Fail** (or **Partly** where the scheme allows a
partial answer), **Your evidence** (outside what Microsoft 365 shows; the
engineer's saved answer fills it) or **Not collected**. A certification scheme
is ``strict``: any gap in a control Microsoft 365 can judge is a Fail, since
the assessor won't take "mostly". Failing controls carry the exact figures and
the fix. The engineer's saved answers (``evidence_answers`` overrides, audited)
win over the suggestion, so a documented exception can turn a Fail into a
Pass with the justification on record.

``compare()`` judges the same scheme as the data stood on an earlier day, so a
renewal shows which controls changed since the last submission.

Schemes are data: a module registers one with ``register()``, which refuses a
check, topic or question set that doesn't exist, so a mistake fails at import.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .. import coverage, state_history
from ..checks import RULES, RuleResult, evaluate
from ..findings import finding_key, lifecycle_map
from ..snapshot import TenantSnapshot
from ..text import WHOLE_ORGANISATION, plural
from . import evidence_answers, questionnaires
from .evidence_base import NO, NOT_KNOWN, YES, Answer
from .evidence_base import PARTLY as PARTLY_ANSWER
from .monthly_review import build as build_review
from .summary import Verdict

log = logging.getLogger(__name__)

PASS, FAIL, PARTLY, MANUAL, UNKNOWN = "Pass", "Fail", "Partly", "Your evidence", "Not collected"
VERDICTS = (PASS, FAIL, PARTLY, MANUAL, UNKNOWN)
TONE = {PASS: "ok", FAIL: "bad", PARTLY: "warn", MANUAL: "accent", UNKNOWN: ""}
REPORT_TONE = {PASS: "ok", FAIL: "critical", PARTLY: "medium", MANUAL: "none", UNKNOWN: "none"}
RULE_KEYS = {r.key for r in RULES}
GAP_LIMIT = 5  # specific gaps listed under a control before "and N more"

# Schemes are grouped on the client's Evidence page, in this order.
GROUPS = (
    ("uk", "UK certification"),
    ("forms", "Insurance and supplier questionnaires"),
    ("international", "Frameworks and benchmarks"),
)


@dataclass(frozen=True)
class Control:
    """One requirement of a scheme and how Microsoft 365 data speaks to it."""

    key: str
    ref: str  # the scheme's own reference ("A7.17", "5.1.2")
    title: str  # the requirement in a few words, as a goal ("MFA for every user of cloud services")
    requirement: str  # what the scheme asks for, paraphrased
    rules: tuple[str, ...] = ()  # checks whose results decide the verdict
    topic: str = ""  # evidence_answers topic: its text is what to paste, and it decides the verdict without rules
    support: tuple[str, ...] = ()  # checks shown as supporting evidence that don't decide the verdict
    scope: str = ""  # what Microsoft 365 can and can't show for this control
    report: str = ""  # detail report (data_reports key) with the rows behind the verdict
    table: str = ""  # section title in that report
    fix: str = ""  # what to do when it fails, for a control judged by its topic rather than checks
    autofail: bool = False  # the scheme fails outright on this control
    strict: bool | None = None  # any gap is a Fail; None follows the scheme
    figures: Callable | None = None  # (Context) -> [(label, value)], the exact numbers to quote


@dataclass(frozen=True)
class Theme:
    key: str
    title: str
    intro: str  # the theme in the scheme's words
    controls: tuple[Control, ...]


@dataclass(frozen=True)
class Audit:
    """What an auditor or assessor tests on site, and what Microsoft 365 shows for it ahead of time."""

    title: str
    what: str
    m365: str
    controls: tuple[str, ...] = ()  # control keys whose verdicts predict the test's result


@dataclass(frozen=True)
class Scheme:
    key: str
    title: str
    short: str  # for the switch at the top of the page
    body: str  # who sets the scheme (NCSC and IASME, the insurer, ISO)
    group: str  # GROUPS key
    intro: str  # one or two sentences for the page
    themes: tuple[Theme, ...]
    version: str = ""  # the requirement set's version and date
    source: str = ""  # where the requirements come from, cited
    source_url: str = ""
    strict: bool = False  # a certification: any gap is a Fail
    questions: str = ""  # questionnaires.SETS key: the form's own questions, answered to paste
    audit: tuple[Audit, ...] = ()  # the on-site tests (Cyber Essentials Plus)
    notes: tuple[str, ...] = ()  # what the engineer should know before using the pack
    renewal: str = ""  # how often it's renewed, for the "since" comparison ("12 months")

    @property
    def controls(self) -> list[Control]:
        return [c for t in self.themes for c in t.controls]

    @property
    def control(self) -> dict[str, Control]:
        return {c.key: c for c in self.controls}


SCHEMES: dict[str, Scheme] = {}


def register(scheme: Scheme) -> Scheme:
    """Add a scheme to the page and exports, after checking every reference it makes."""
    if scheme.key in SCHEMES:
        raise ValueError(f"scheme {scheme.key!r} already exists")
    if scheme.group not in dict(GROUPS):
        raise ValueError(f"scheme {scheme.key!r} is in unknown group {scheme.group!r}")
    keys = [c.key for c in scheme.controls]
    if len(keys) != len(set(keys)):
        raise ValueError(f"scheme {scheme.key!r} repeats a control key")
    for c in scheme.controls:
        unknown = sorted(set(c.rules + c.support) - RULE_KEYS)
        if unknown:
            raise ValueError(f"scheme {scheme.key!r} control {c.key!r} names unknown checks: {', '.join(unknown)}")
        if c.topic and c.topic not in evidence_answers.KEYS:
            raise ValueError(f"scheme {scheme.key!r} control {c.key!r} names unknown topic {c.topic!r}")
        if not c.rules and not c.topic:
            raise ValueError(f"scheme {scheme.key!r} control {c.key!r} has no checks and no topic")
    if scheme.questions and scheme.questions not in questionnaires.SETS:
        raise ValueError(f"scheme {scheme.key!r} names unknown question set {scheme.questions!r}")
    for a in scheme.audit:
        unknown = sorted(set(a.controls) - set(keys))
        if unknown:
            raise ValueError(f"scheme {scheme.key!r} audit {a.title!r} names unknown controls: {', '.join(unknown)}")
    SCHEMES[scheme.key] = scheme
    return scheme


def grouped() -> list[tuple[str, list[Scheme]]]:
    """Every scheme by group, in GROUPS order, leaving out empty groups."""
    out = []
    for key, title in GROUPS:
        found = [s for s in SCHEMES.values() if s.group == key]
        if found:
            out.append((title, found))
    return out


# -- The data for one tenant ---------------------------------------------------------------


@dataclass
class Context:
    """Everything a scheme is judged from, built once per tenant and shared by every scheme."""

    tenant: dict
    snap: TenantSnapshot
    generated_at: datetime
    results: dict[str, RuleResult]
    answers: dict[str, Answer]
    oldest_data: str | None

    def names(self) -> dict[str, str]:
        from ..checks.base import _names

        return _names(self.snap)


def context(conn, tenant, now: datetime | None = None, as_of: datetime | None = None) -> Context:
    """The latest data (``as_of`` None), or the data as it stood at ``as_of``: the runs finished by then
    and the checks judged as of then. The engineer's saved answers are today's in both cases, so a
    comparison shows what the data changed, not what someone wrote since."""
    now = now or datetime.now(UTC)
    if as_of is None:
        review = build_review(conn, tenant, now)
        snap = TenantSnapshot(conn, tenant)
        results = {r.key: r for r in review.results}
        generated_at = review.generated_at
    else:
        snap = TenantSnapshot(conn, tenant, as_of)
        results = {r.key: r for r in _accepted(evaluate(snap, as_of), lifecycle_map(conn, tenant["id"], as_of))}
        generated_at = as_of
    answers = evidence_answers.apply_overrides(
        evidence_answers.suggest(snap, results), evidence_answers.overrides(conn, tenant["id"])
    )
    finished = [r["finished_at"] for r in snap.runs.values() if r["finished_at"]]
    return Context(dict(tenant), snap, generated_at, results, {a.key: a for a in answers}, min(finished, default=None))


def _accepted(results: list[RuleResult], lifecycle: dict) -> list[RuleResult]:
    """Leave accepted risks out, as the monthly review does: a check whose every finding is accepted is
    ``accepted``, not failing."""
    for r in results:
        if r.status != "fail":
            continue
        active = [f for f in r.findings if not ((life := lifecycle.get((r.key, finding_key(f)))) and life.accepted)]
        if not active:
            r.status = "accepted"
        r.findings = active
    return results


# -- Judging one control --------------------------------------------------------------------


@dataclass
class ControlResult:
    control: Control
    verdict: str
    evidence: list[str] = field(default_factory=list)  # what the data shows, with its figures
    gaps: list[str] = field(default_factory=list)  # the things wrong, named
    figures: list[tuple[str, str]] = field(default_factory=list)  # (label, value)
    fix: str = ""  # the first thing to do
    fix_detail: str = ""  # the full recommendation
    answer: Answer | None = None  # the topic's answer: text to paste, the engineer's saved answer
    reason: str = ""  # why it's Not collected

    @property
    def tone(self) -> str:
        return TONE[self.verdict]

    @property
    def needs_input(self) -> bool:
        return self.verdict == MANUAL

    @property
    def set_by(self) -> str:
        """Who decided the verdict, when it was the engineer."""
        return self.answer.override.set_by if self.answer and self.answer.override else ""

    @property
    def text(self) -> str:
        """What to paste or quote for this control: the saved or suggested wording, else the evidence."""
        if self.answer is not None and (self.answer.override or self.answer.text):
            return self.answer.text
        found = [e for e in self.evidence if not e.startswith(("Supporting: ", "But: "))]
        return " ".join(found + [g for g in self.gaps if not g.startswith("  ")]) or self.reason

    @property
    def blanks(self) -> list[str]:
        return self.answer.blanks if self.answer and self.text == self.answer.text else []

    @property
    def segments(self) -> list[tuple[str, bool]]:
        """The text in pieces, each [part to fill in] marked, for the page to highlight."""
        from .evidence_base import BLANK

        text, out, at = self.text, [], 0
        for m in BLANK.finditer(text):
            out += [(text[at : m.start()], False), (m.group(0), True)]
            at = m.end()
        return [(t, blank) for t, blank in out + [(text[at:], False)] if t]


def _sentence(text: str) -> str:
    text = (text or "").strip()
    return (text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")) if text else ""


def _lower(text: str) -> str:
    return text[:1].lower() + text[1:] if text else text


def _from_rules(ran: list[RuleResult], strict: bool) -> str:
    bad = [r for r in ran if r.status not in ("pass", "accepted")]
    if not bad:
        return PASS
    if strict:
        return FAIL
    if not any(r.status in ("pass", "accepted") for r in ran):
        failing = [f for r in bad for f in r.findings]
        if any(f.severity in ("critical", "high") or f.subject in WHOLE_ORGANISATION for f in failing):
            return FAIL
    return PARTLY


def _from_answer(answer: str, strict: bool) -> str:
    if answer == YES:
        return PASS
    if answer == NO:
        return FAIL
    if answer == PARTLY_ANSWER:
        return FAIL if strict else PARTLY
    return MANUAL


def _describe(r: RuleResult, evidence: list[str], gaps: list[str]) -> None:
    if r.status in ("pass",):
        evidence.append(_sentence(r.summary))
    elif r.status == "accepted":
        evidence.append(f"{r.title}: the exceptions are accepted as a documented risk.")
    elif r.status == "unavailable":
        evidence.append(f"Not checked: {_lower(r.title)} ({r.summary.rstrip('.')}).")
    else:
        gaps.append(_sentence(r.summary))
        for f in r.findings[:GAP_LIMIT]:
            gaps.append(f"  {f.detail}" if f.subject in WHOLE_ORGANISATION else f"  {f.subject}: {f.detail}")
        if len(r.findings) > GAP_LIMIT:
            gaps.append(f"  and {len(r.findings) - GAP_LIMIT} more")


def judge(c: Control, scheme: Scheme, ctx: Context) -> ControlResult:
    strict = scheme.strict if c.strict is None else c.strict
    answer = ctx.answers.get(c.topic) if c.topic else None
    out = ControlResult(c, MANUAL, answer=answer)
    results = [ctx.results[k] for k in c.rules if k in ctx.results]
    ran = [r for r in results if r.status != "unavailable"]
    if c.rules:
        for r in results:
            _describe(r, out.evidence, out.gaps)
        if not ran:
            out.verdict = UNKNOWN
            out.reason = _sentence(results[0].summary) if results else "The data hasn't been collected yet."
        else:
            out.verdict = _from_rules(ran, strict)
            failing = [r for r in ran if r.status not in ("pass", "accepted")]
            if failing:
                worst = max(failing, key=lambda r: (r.severity or "") == "critical", default=failing[0])
                out.fix, out.fix_detail = worst.action, worst.recommendation
    elif answer is not None:
        if answer.needs_input:
            out.verdict = MANUAL
        elif answer.suggested == NOT_KNOWN:
            out.verdict, out.reason = UNKNOWN, answer.why
        else:
            out.verdict = _from_answer(answer.suggested, strict)
            (out.evidence if out.verdict == PASS else out.gaps).append(_sentence(answer.why))
    if answer is not None and answer.override:  # the engineer's decision, on record with who and when
        out.verdict = _from_answer(answer.override.answer, strict)
        if out.verdict == MANUAL:
            out.verdict = UNKNOWN if not answer.override.note else PASS  # a saved text with no yes or no
    if out.verdict in (FAIL, PARTLY) and not out.fix:
        out.fix = out.fix_detail = c.fix
    for k in c.support:
        r = ctx.results.get(k)
        if r is not None and r.status != "unavailable":
            out.evidence.append(("Supporting: " if r.status == "pass" else "But: ") + _sentence(r.summary))
    if c.figures:
        try:
            out.figures = [(label, str(value)) for label, value in c.figures(ctx)]
        except Exception:  # damaged stored data: the verdict stands without the figures
            log.exception("Figures for %s failed", c.key)
    return out


# -- A scheme judged for one tenant ----------------------------------------------------------


@dataclass
class ThemeResult:
    theme: Theme
    controls: list[ControlResult]

    @property
    def verdict(self) -> str:
        """The theme's worst verdict: a Fail anywhere, else a gap to review, else something not collected,
        else evidence still to write; Pass only when every control passes."""
        for v in (FAIL, PARTLY, UNKNOWN, MANUAL):
            if any(c.verdict == v for c in self.controls):
                return v
        return PASS

    @property
    def tone(self) -> str:
        return TONE[self.verdict]

    @property
    def counts(self) -> dict[str, int]:
        return {v: sum(1 for c in self.controls if c.verdict == v) for v in VERDICTS}


@dataclass
class Change:
    control: Control
    before: str
    after: str

    @property
    def better(self) -> bool:
        order = (FAIL, PARTLY, UNKNOWN, MANUAL, PASS)
        return order.index(self.after) > order.index(self.before)


@dataclass
class Comparison:
    """The same scheme judged from the data as it stood on an earlier day."""

    since: datetime
    available: bool
    note: str = ""  # why there's nothing to compare
    data_from: str | None = None  # the oldest run the earlier verdicts rest on
    changes: list[Change] = field(default_factory=list)
    counts_then: dict[str, int] = field(default_factory=dict)

    @property
    def improved(self) -> list[Change]:
        return [c for c in self.changes if c.better]

    @property
    def worsened(self) -> list[Change]:
        return [c for c in self.changes if not c.better]


@dataclass
class SchemeResult:
    scheme: Scheme
    tenant: dict
    generated_at: datetime
    oldest_data: str | None
    themes: list[ThemeResult]
    sheet: questionnaires.Sheet | None = None  # the form's questions answered, when the scheme has one
    since: Comparison | None = None

    @property
    def controls(self) -> list[ControlResult]:
        return [c for t in self.themes for c in t.controls]

    @property
    def by_key(self) -> dict[str, ControlResult]:
        return {c.control.key: c for c in self.controls}

    @property
    def counts(self) -> dict[str, int]:
        return {v: sum(1 for c in self.controls if c.verdict == v) for v in VERDICTS}

    @property
    def to_fix(self) -> list[ControlResult]:
        """Failing controls, auto-fail ones first."""
        return sorted((c for c in self.controls if c.verdict == FAIL), key=lambda c: not c.control.autofail)

    @property
    def to_review(self) -> list[ControlResult]:
        return [c for c in self.controls if c.verdict == PARTLY]

    @property
    def manual(self) -> list[ControlResult]:
        return [c for c in self.controls if c.verdict == MANUAL]

    @property
    def judged(self) -> int:
        """Controls Microsoft 365 could judge."""
        return sum(1 for c in self.controls if c.verdict in (PASS, FAIL, PARTLY))

    @property
    def headline(self) -> Verdict:
        n, name, short = self.counts, self.tenant["name"], self.scheme.short
        if n[FAIL]:
            what = plural(n[FAIL], "control")
            if self.scheme.strict:
                return Verdict("critical", "Not ready", f"{name} has {what} to fix before applying for {short}.")
            return Verdict("critical" if n[FAIL] > 2 else "high", "Gaps to close", f"{name} has {what} not in place.")
        if n[PARTLY]:
            label = "Nearly ready" if self.scheme.strict else "Mostly in place"
            return Verdict("medium", label, f"{name} has {plural(n[PARTLY], 'control')} with gaps to review.")
        if n[UNKNOWN] and not n[PASS]:
            return Verdict("medium", "Not collected yet", f"Nothing has been collected for {name} yet.")
        if n[UNKNOWN]:
            return Verdict(
                "medium",
                "Nearly there",
                f"Every control Microsoft 365 could judge passes for {name}; "
                f"{plural(n[UNKNOWN], 'control')} couldn't be checked yet.",
            )
        tail = f", and {plural(n[MANUAL], 'control')} need evidence from outside Microsoft 365" if n[MANUAL] else ""
        return Verdict("ok", "Passes", f"Every control Microsoft 365 can judge passes for {name}{tail}.")

    @property
    def breakdown(self) -> list[tuple[str, int, str]]:
        """Every control once, for the PDF's bar: (label, count, report.css tone)."""
        n = self.counts
        return [(v, n[v], REPORT_TONE[v]) for v in VERDICTS if n[v] or v in (PASS, FAIL)]


def _build(ctx: Context, scheme: Scheme) -> SchemeResult:
    themes = [ThemeResult(t, [judge(c, scheme, ctx) for c in t.controls]) for t in scheme.themes]
    sheet = None
    if scheme.questions:
        pack = _Pack(ctx)
        sheet = questionnaires.sheet(pack, scheme.questions)
    return SchemeResult(scheme, ctx.tenant, ctx.generated_at, ctx.oldest_data, themes, sheet)


@dataclass
class _Pack:
    """What ``questionnaires.sheet`` needs of an evidence pack."""

    ctx: Context

    @property
    def answers(self) -> list[Answer]:
        return list(self.ctx.answers.values())

    @property
    def tenant(self) -> dict:
        return self.ctx.tenant

    @property
    def generated_at(self) -> datetime:
        return self.ctx.generated_at

    @property
    def oldest_data(self) -> str | None:
        return self.ctx.oldest_data


def assess(conn, tenant, key: str, now: datetime | None = None, since: datetime | None = None) -> SchemeResult:
    """Scheme ``key`` judged for ``tenant`` from the latest data; with ``since``, compared with that day."""
    scheme = SCHEMES[key]
    ctx = context(conn, tenant, now)
    result = _build(ctx, scheme)
    if since is not None:
        result.since = compare(conn, tenant, scheme, result, since)
    return result


def assess_all(conn, tenant, now: datetime | None = None) -> list[SchemeResult]:
    """Every scheme for one tenant, from one evaluation of the checks (the Evidence page's cards)."""
    ctx = context(conn, tenant, now)
    return [_build(ctx, s) for s in SCHEMES.values()]


def compare(conn, tenant, scheme: Scheme, result: SchemeResult, since: datetime) -> Comparison:
    then = context(conn, tenant, since, as_of=since)
    if not then.snap.runs:
        first = state_history.earliest(conn, tenant["id"])
        when = f"{since.day} {since:%B %Y}"
        note = f"No data from {when}: the oldest data held is from {_day(first)}." if first else "No data is held."
        return Comparison(since, False, note)
    before = _build(then, scheme)
    now_by, then_by = result.by_key, before.by_key
    changes = [
        Change(c.control, then_by[k].verdict, c.verdict)
        for k, c in now_by.items()
        if k in then_by and then_by[k].verdict != c.verdict
    ]
    return Comparison(since, True, data_from=then.oldest_data, changes=changes, counts_then=before.counts)


def _day(value: str | None) -> str:
    if not value:
        return ""
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return f"{when.day} {when:%B %Y}"


# -- Figures schemes can quote -----------------------------------------------------------------


def figures_mfa_users(ctx: Context) -> list[tuple[str, object]]:
    """MFA registration and enforcement across users, as a form or assessor wants it quoted."""
    out: list[tuple[str, object]] = []
    checked, pct = ctx.snap.metric("mfa.users_checked"), ctx.snap.metric("mfa.registered_pct")
    if pct is not None and checked:
        out.append(("Users with an MFA method registered", f"{pct:g}% of {int(checked)}"))
    defaults = ctx.snap.objects("conditional_access", "security_defaults")
    if defaults and defaults[0].get("isEnabled"):
        out.append(("Security Defaults", "on (MFA required for everyone)"))
        return out
    cov = coverage.user_coverage(ctx.snap)
    if cov:
        out.append(("Accounts covered by an enforced MFA policy", f"{sum(c.mfa for c in cov)} of {len(cov)}"))
        missing = sorted(c.label for c in cov if not c.mfa)
        if missing:
            out.append(("Not covered", ", ".join(missing[:GAP_LIMIT]) + (" …" if len(missing) > GAP_LIMIT else "")))
    return out


def figures_admins(ctx: Context) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    admins = ctx.snap.metric("admins.privileged_principals")
    if admins is not None:
        out.append(("Accounts holding privileged roles", int(admins)))
    r = ctx.results.get("admins_ca_mfa")
    if r is not None and r.status == "fail":
        out.append(("Admins with no MFA policy", len({f.subject for f in r.findings})))
    r = ctx.results.get("admin_mfa")
    if r is not None and r.status == "fail":
        out.append(("Admins with no MFA method registered", len({f.subject for f in r.findings})))
    return out


def figures_devices(ctx: Context) -> list[tuple[str, object]]:
    from .. import devices

    out: list[tuple[str, object]] = []
    managed = devices.objects(ctx.snap, "managed_device")
    if managed is not None:
        computers = [d for d in managed if devices.is_computer(d)]
        out.append(("Computers managed by Intune", len(computers)))
        encrypted = sum(1 for d in computers if d.get("isEncrypted"))
        if computers:
            out.append(("Of which encrypted", encrypted))
    r = ctx.results.get("supported_windows")
    if r is not None and r.status == "fail":
        ended = {f.subject for f in r.findings if f.severity != "low"}
        soon = {f.subject for f in r.findings if f.severity == "low"}
        if ended:
            out.append(("Windows computers out of support", len(ended)))
        if soon:
            out.append((f"Reaching end of support within {devices.SOON_DAYS} days", len(soon)))
    r = ctx.results.get("device_compliance")
    if r is not None and r.status == "fail":
        out.append(("Not compliant", len({f.subject for f in r.findings})))
    return out
