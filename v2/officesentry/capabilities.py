"""What a client's licences cover, and what that unlocks in Office Sentry.

A tenant's capabilities come from the service plans inside the licences it has bought (``from_skus``, run by
the Tenant profile collector and stored on the client). Collectors declare the capabilities they need, so a
report's licence needs follow from the collectors it reads (``report_needs``), and so does the plain
sentence a page shows when a licence is why something is empty. Checks declare theirs on ``@rule``.

Nothing here calls Graph or writes anything.
"""

from __future__ import annotations

import re

from .formatting import CAPABILITY_LABELS
from .licences import working_units

# Service plan name -> capability flag.
PLANS = {
    "AAD_PREMIUM": "entra_p1",
    "AAD_PREMIUM_P2": "entra_p2",
    "INTUNE_A": "intune",
    "ATP_ENTERPRISE": "defender_o365_p1",
    "THREAT_INTELLIGENCE": "defender_o365_p2",
}
# Mailboxes and SharePoint come in families of plans named per edition and cloud: EXCHANGE_S_ENTERPRISE in
# E3, _GOV in Office 365 G3 (GCC runs on the commercial cloud), the same name in A3 education, DESKLESS in F3
# frontline, EXCHANGE_B_STANDARD in Business. The foundation, archive and add-on plans that ride along with
# other products (Power BI, Project) give no mailbox, and Office for the web or extra storage no sites.
FAMILIES = {
    "exchange": (re.compile(r"EXCHANGE_[SBL]_"), re.compile(r"FOUNDATION|ARCHIVE|ADDON")),
    "sharepoint": (re.compile(r"SHAREPOINT"), re.compile(r"WAC|STORAGE|PROJECT|ADDON")),
}
IMPLIED = {"entra_p2": "entra_p1", "defender_o365_p2": "defender_o365_p1"}
FLAGS = tuple(dict.fromkeys((*PLANS.values(), *FAMILIES)))

# What each one is, for someone deciding whether a client needs it. Office Sentry reports on these; it never
# turns one on.
WHAT_IT_IS = {
    "entra_p1": "Conditional Access, sign-in logs kept for 30 days, group licensing and self-service password "
    "reset. In Microsoft 365 Business Premium, E3 and E5.",
    "entra_p2": "Risky users and sign-in risk (Identity Protection), access reviews and Privileged Identity "
    "Management. In Microsoft 365 E5, or as an add-on.",
    "intune": "Managing and reporting on computers and phones: compliance, configuration and the apps on "
    "them. In Microsoft 365 Business Premium, E3 and E5.",
    "defender_o365_p1": "Safe Links, Safe Attachments and anti-phishing beyond Exchange Online Protection. In "
    "Microsoft 365 Business Premium, E5, or as an add-on.",
    "defender_o365_p2": "Threat investigation, attack simulation and automated response on top of P1. In "
    "Microsoft 365 E5.",
    "exchange": "Mailboxes in Exchange Online: mail flow, rules, permissions, holds and mailbox size.",
    "sharepoint": "SharePoint sites and OneDrive: sharing, permissions, storage and files shared outside.",
}


def label(flag: str) -> str:
    return CAPABILITY_LABELS.get(flag, flag.replace("_", " ").title())


def capability_of(plan_name: str) -> str | None:
    if plan_name in PLANS:
        return PLANS[plan_name]
    for flag, (family, not_it) in FAMILIES.items():
        if family.match(plan_name) and not not_it.search(plan_name):
            return flag
    return None


def working_sku(sku: dict) -> bool:
    """Whether a licence's features count: bought, and not suspended or locked out. A subscription in its
    grace period still provides them."""
    return sku.get("capabilityStatus") in ("Enabled", "Warning") and working_units(sku) > 0


def plans_of(sku: dict) -> set[str]:
    """The capabilities a licence provides, from the service plans in it that Microsoft has provisioned."""
    out = set()
    for plan in sku.get("servicePlans") or []:
        flag = capability_of(plan.get("servicePlanName") or "")
        if flag and plan.get("provisioningStatus") in ("Success", "PendingActivation", "PendingProvisioning"):
            out.add(flag)
    for higher, lower in IMPLIED.items():
        if higher in out:
            out.add(lower)
    return out


def from_skus(skus: list[dict]) -> dict:
    """Every capability flag, and whether the tenant's licences provide it."""
    caps = dict.fromkeys(FLAGS, False)
    for sku in skus:
        if working_sku(sku):
            for flag in plans_of(sku):
                caps[flag] = True
    return caps


def providers(skus: list[dict]) -> dict[str, list[str]]:
    """Capability flag -> the part numbers of the tenant's licences that provide it."""
    out: dict[str, list[str]] = {}
    for sku in skus:
        if not working_sku(sku):
            continue
        for flag in plans_of(sku):
            part = sku.get("skuPartNumber")
            if isinstance(part, str) and part not in out.setdefault(flag, []):
                out[flag].append(part)
    return out


def held_by(sku_ids, by_sku: dict[str, dict]) -> set[str]:
    """The capabilities one person's licences give them. ``sku_ids`` are the licences assigned to them (a
    user snapshot's ``skuIds``) and ``by_sku`` the tenant's licences by sku ID. A part of a licence an admin
    has turned off for one person isn't collected, so it counts as theirs."""
    out: set[str] = set()
    for sku_id in sku_ids or []:
        sku = by_sku.get(sku_id)
        if sku and working_sku(sku):
            out |= plans_of(sku)
    return out


# -- What a capability unlocks ------------------------------------------------------------------


def collector_needs(key: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """A collector's ``needs`` (nothing without them) and ``more_with`` (some of it needs them)."""
    from .collectors.base import REGISTRY

    collector = REGISTRY.get(key)
    return (getattr(collector, "needs", ()), getattr(collector, "more_with", ())) if collector else ((), ())


def report_needs(key: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """What a report's licence needs are, from the collectors it reads: the capabilities without which it has
    nothing to show, and those that fill in part of it."""
    from .reports.registry import DECLARED

    declaration = DECLARED.get(key)
    if not declaration or not declaration.collectors:
        return ((), ())
    # A report is named for the source it's declared with first, and that one decides: a report of mailboxes
    # has nothing to show without Exchange Online. The other sources it reads along the way are left out,
    # since almost every report reads the accounts, and a column of theirs isn't a licence problem.
    return collector_needs(declaration.collectors[0])


def missing(caps: dict | None, flags: tuple[str, ...]) -> tuple[str, ...]:
    """The capabilities in ``flags`` the tenant's licences are known not to provide. Unknown counts as there:
    a client whose licences haven't been read yet is told nothing about them."""
    return tuple(f for f in flags if (caps or {}).get(f) is False)


def reports_needing(flag: str) -> list[str]:
    """The report keys this capability unlocks, hard needs first."""
    from .reports.registry import DECLARED

    hard = [k for k in DECLARED if flag in report_needs(k)[0]]
    return hard + [k for k in DECLARED if flag in report_needs(k)[1]]


def checks_needing(flag: str) -> list[str]:
    """The keys of the checks this capability unlocks."""
    from .checks.base import RULES

    return [r.key for r in RULES if flag in r.needs]
