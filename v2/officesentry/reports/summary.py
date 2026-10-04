"""Plain-English executive summaries for client-facing reports.

Written for a business owner, not an IT person: one overall verdict, then a
few short sentences on risk, progress and money. Pure functions over the data
the report builders already return, so the page, PDF and email can share them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .branding import money

SEVERITIES = ("critical", "high", "medium", "low")


@dataclass
class Verdict:
    tone: str  # critical | high | medium | ok: drives the colour
    label: str  # two or three words for a status pill
    headline: str  # one sentence


@dataclass
class Action:
    title: str
    why: str
    severity: str
    anchor: str = ""  # id of the section in the report with the detail


@dataclass
class ExecSummary:
    verdict: Verdict
    paragraphs: list[str] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)


def plural(n: int | float, word: str, words: str | None = None) -> str:
    return f"{n:g} {word if n == 1 else (words or word + 's')}"


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def _fmt(value: float, unit: str) -> str:
    return f"{value:g}{unit}"


def monthly(report) -> ExecSummary:
    """Summary of a ``monthly_review.Report``."""
    name = report.tenant["name"]
    failing = report.failing
    by_sev = {s: [r for r in failing if r.severity == s] for s in SEVERITIES}
    ran = len(report.results) - len(report.unavailable)

    if not ran:
        verdict = Verdict("medium", "Not checked yet",
                          f"Nothing has been collected for {name} yet, so no security checks could run.")
    elif by_sev["critical"]:
        verdict = Verdict("critical", "Action needed",
                          f"{name}'s Microsoft 365 has a critical security gap that should be fixed now.")
    elif by_sev["high"]:
        verdict = Verdict("high", "Needs attention",
                          f"{name}'s Microsoft 365 is broadly working, with some important gaps to close.")
    elif failing:
        verdict = Verdict("medium", "In good shape",
                          f"{name}'s Microsoft 365 is in good shape, with a few smaller items to tidy up.")
    else:
        verdict = Verdict("ok", "In good shape",
                          f"{name}'s Microsoft 365 passed every security check we could run.")

    paragraphs = []
    if failing:
        parts = [f"{len(by_sev[s])} {s}" for s in SEVERITIES if by_sev[s]]
        need = "needs" if len(failing) == 1 else "need"
        text = f"We ran {ran} security checks and {len(failing)} {need} attention ({_join(parts)} priority)."
        top = failing[0]
        if top.severity in ("critical", "high"):
            text += f" The most urgent: {_sentence(top.summary[0].lower() + top.summary[1:])}"
        paragraphs.append(text)
    elif ran:
        paragraphs.append(f"We ran {plural(ran, 'security check')} and {'it' if ran == 1 else 'all of them'} passed.")

    kpis = {k.key: k for k in report.kpis}
    progress = []
    for key, what in (("secure_score.pct", "Microsoft Secure Score"), ("mfa.registered_pct", "users with MFA")):
        k = kpis.get(key)
        if k and k.delta:
            direction = "rose" if k.delta > 0 else "fell"
            progress.append(f"{what} {direction} from {_fmt(k.previous, k.unit)} to {_fmt(k.value, k.unit)}")
    if report.resolved:
        progress.append(f"{plural(len(report.resolved), 'issue was', 'issues were')} fixed")
    if report.new_count:
        progress.append(f"{plural(report.new_count, 'new item', 'new items')} came up")
    start = report.period_start
    since = f"{start.day} {start:%B}" if start else "last month"
    if progress:
        paragraphs.append(f"Since {since}, {_join(progress)}.")
    elif not report.changes:
        paragraphs.append("This is the first report with this data, so there's nothing to compare yet. "
                          "From next month it shows what changed.")

    waste = report.licences.monthly_waste if report.licences.lines else None
    if waste:
        paragraphs.append(
            f"About {money(waste, report.currency)} a month ({money(waste * 12, report.currency)} a year) "
            "goes on licences that are unused or assigned to disabled or inactive accounts.")
    if report.unavailable:
        paragraphs.append(f"{plural(len(report.unavailable), 'check', 'checks')} couldn't run because the data or "
                          "a licence they need is missing. They're listed at the end.")

    actions = [Action(r.action, r.summary, r.severity or "info", f"check-{r.key}")
               for r in failing if r.severity in ("critical", "high")][:3]
    return ExecSummary(verdict, paragraphs, actions)


def evidence(pack) -> ExecSummary:
    """Summary of an ``evidence.EvidencePack``."""
    counts = pack.counts
    total = len(pack.controls)
    in_place, partly, missing, unassessed = counts.values()
    name = pack.tenant["name"]
    if missing:
        verdict = Verdict("critical" if missing > 2 else "high", "Gaps to close",
                          f"{name} has {plural(missing, 'control')} insurers ask about that "
                          f"{'is' if missing == 1 else 'are'} not in place.")
    elif partly:
        verdict = Verdict("medium", "Mostly in place",
                          f"{name} has the controls insurers ask about, with some gaps to tidy up.")
    else:
        verdict = Verdict("ok", "In place", f"{name} has every control we could check in place.")

    parts = [f"{n} {'is' if n == 1 else 'are'} {label}" for n, label in (
        (in_place, "in place"), (partly, "partly in place"), (missing, "not in place")) if n]
    paragraphs = [f"Of the {total} controls cyber insurers usually ask about, {_join(parts)}."]
    if unassessed:
        paragraphs.append(f"{plural(unassessed, 'control')} can't be seen in Microsoft 365 data "
                          "(such as backups and staff training) and need evidence from elsewhere.")
    paragraphs.append("Every answer comes from settings and accounts read directly from Microsoft 365, "
                      "not from a self-assessment.")
    actions = [Action(c.control.question, c.gaps[0].strip() if c.gaps else "", "high", f"control-{c.control.key}")
               for c in pack.controls if c.status == "Not in place"][:3]
    return ExecSummary(verdict, paragraphs, actions)
