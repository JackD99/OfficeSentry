"""Licence features: what a client's licences actually cover, and what that unlocks in Office Sentry.

For the person deciding whether a client needs Business Premium rather than Business Standard, and for
anyone wondering why a report or a check on this client has nothing to show. Each feature Office Sentry
reports on, whether the client's licences provide it, which licences those are, how many people hold one,
and the reports and checks it unlocks; then every licence with the features inside it; then what this
client's licences leave out.

Features come from the service plans inside the licences (``officesentry.capabilities``), which is the same
reading the collectors go by, so this page says exactly why something elsewhere is empty.
"""

from __future__ import annotations

from datetime import datetime

from .. import capabilities
from ..checks.base import RULES
from ..licences import sku_name, working_units
from ..snapshot import TenantSnapshot
from ..text import plural
from .data_reports import DataReport, Highlight, Rows, Section
from .registry import DECLARED, report

FEATURES = "What your licences cover"
INSIDE = "What each licence includes"
BLOCKED = "What your licences leave out"
COVERED = "Covered"
NOT_COVERED = "Not covered"


def _titles(keys, declared) -> list[str]:
    return [declared[k] for k in keys if k in declared]


@report(
    "licence-features",
    "Licence features",
    "What the client's licences cover (Entra ID P1 and P2, Intune, Defender for Office 365, Exchange "
    "Online, SharePoint), which licences provide each feature, how many people hold one, and the reports "
    "and checks each feature unlocks.",
    ("tenant_profile", "users"),
    group="tenant",
    order=22,
    keywords="capabilities features service plans what does business premium include upsell upgrade gap "
    "entra id p1 p2 intune defender for office 365 exchange online sharepoint unlocks not available "
    "licence limited why is this empty",
    uses=("renewal", "licences", "quarterly"),
)
def build(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    skus = snap.objects("tenant_profile", "subscribed_sku")
    if skus is None:
        report.unavailable = "Licence data hasn't been collected yet. Refresh Tenant profile and licences."
        return
    skus = [s for s in skus if isinstance(s, dict)]
    by_sku = {s.get("skuId"): s for s in skus if isinstance(s.get("skuId"), str)}
    caps = capabilities.from_skus(skus)
    provided = capabilities.providers(skus)

    users = snap.objects("users", "user")
    if users is None:
        report.notes.append("Accounts haven't been collected yet, so how many people hold each feature isn't shown.")
    people: dict[str, int] = dict.fromkeys(capabilities.FLAGS, 0)
    for user in users or []:
        if user.get("accountEnabled") is False:
            continue
        for flag in capabilities.held_by(user.get("skuIds"), by_sku):
            people[flag] += 1

    report_titles = {k: d.title for k, d in DECLARED.items()}
    check_titles = {r.key: r.title for r in RULES}
    features = _features(caps, provided, people, users is not None, report_titles, check_titles)
    report.sections = [features, _inside(skus, users, by_sku), _blocked(caps, report_titles, check_titles)]

    covered = sum(1 for flag in capabilities.FLAGS if caps.get(flag))
    reports_off = {k for k in DECLARED if capabilities.missing(caps, capabilities.report_needs(k)[0])}
    checks_off = {r.key for r in RULES if capabilities.missing(caps, r.needs)}
    report.highlights = [
        Highlight(
            "Features covered",
            f"{covered} of {len(capabilities.FLAGS)}",
            "From the licences this client has bought",
            rows=Rows(FEATURES, {COVERED: "Yes"}),
        ),
        Highlight(
            "Reports they allow",
            f"{len(DECLARED) - len(reports_off)} of {len(DECLARED)}",
            f"{plural(len(reports_off), 'report')} {'has' if len(reports_off) == 1 else 'have'} nothing to show",
            rows=Rows(BLOCKED, {"Kind": "Report"}),
        ),
        Highlight(
            "Checks they allow",
            f"{len(RULES) - len(checks_off)} of {len(RULES)}",
            f"{plural(len(checks_off), 'check')} can't run on these licences",
            rows=Rows(BLOCKED, {"Kind": "Check"}),
        ),
        Highlight(
            "People with Entra ID P1",
            people["entra_p1"] if users is not None else "Not known",
            "Active accounts whose licence includes it",
            rows=Rows(FEATURES, {"Feature": capabilities.label("entra_p1")}),
        ),
    ]
    report.footnote = (
        "Features come from the service plans inside each licence, the same reading the collectors go by, "
        "and a subscription in its grace period still counts. A licence an admin has switched part of off "
        "for one person isn't collected, so that person is still counted as holding the feature. Office "
        "Sentry reports on these features; it never turns one on."
    )


def _features(caps, provided, people, know_people, report_titles, check_titles) -> Section:
    section = Section(
        FEATURES,
        [
            "Feature",
            COVERED,
            "From these licences",
            "People covered",
            "Reports it unlocks",
            "Checks it unlocks",
            "What it is",
        ],
        intro="Each feature Office Sentry reports on, and whether this client's licences provide it. The "
        "reports and checks named are the ones that have nothing to show without it.",
        empty="No features to show.",
        optional=("What it is",),
        filter_columns=(COVERED,),
    )
    for flag in capabilities.FLAGS:
        has = bool(caps.get(flag))
        reports = _titles(capabilities.reports_needing(flag), report_titles)
        checks = _titles(capabilities.checks_needing(flag), check_titles)
        section.add(
            [
                capabilities.label(flag),
                has,
                sorted(sku_name(p) for p in provided.get(flag, [])),
                people[flag] if know_people else "",
                reports,
                checks,
                capabilities.WHAT_IT_IS.get(flag, ""),
            ]
        )
    return section


def _inside(skus, users, by_sku) -> Section:
    """Every licence the client has bought and can use, with the features inside it. A suspended or
    used-up one is left out, as it gives nothing (Licences and subscriptions lists those)."""
    section = Section(
        INSIDE,
        ["Licence", "Purchased", "Assigned", "Features it provides"],
        intro="What each licence the client has bought brings with it, so you can see which licence to "
        "change when a feature is missing.",
        empty="The client has no licences.",
    )
    held: dict[str, int] = {}
    for user in users or []:
        for sku_id in user.get("skuIds") or []:
            if sku_id in by_sku:
                held[sku_id] = held.get(sku_id, 0) + 1
    rows = []
    for sku in skus:
        sku_id = sku.get("skuId")
        features = sorted(capabilities.label(f) for f in capabilities.plans_of(sku))
        if not features or not capabilities.working_sku(sku):
            continue  # add-ons and free products that bring none of the features Office Sentry reports on
        name = sku_name(sku.get("skuPartNumber"))
        rows.append((name.lower(), [name, working_units(sku), held.get(sku_id, 0), features]))
    for _, row in sorted(rows):
        section.add(row)
    return section


def _blocked(caps, report_titles, check_titles) -> Section:
    """The reports and checks this client's licences stop, so nobody hunts for data that can't exist."""
    section = Section(
        BLOCKED,
        ["Report or check", "Kind", "Needs", "Why"],
        intro="Reports with nothing to show and checks that can't run on this client's licences. Everything "
        "else works as usual.",
        empty="Nothing: every report and check Office Sentry has works on these licences.",
    )
    for key, title in report_titles.items():
        for flag in capabilities.missing(caps, capabilities.report_needs(key)[0]):
            section.add([title, "Report", capabilities.label(flag), f"Its data needs {capabilities.label(flag)}."])
    for rule in RULES:
        for flag in capabilities.missing(caps, rule.needs):
            section.add(
                [
                    check_titles[rule.key],
                    "Check",
                    capabilities.label(flag),
                    f"It can't be checked without {capabilities.label(flag)}.",
                ]
            )
    return section
