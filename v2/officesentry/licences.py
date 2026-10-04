"""Licence names, MSP-entered prices, and licence-waste calculation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .devices import parse_ts

# Friendly names for common SKUs. Anything else shows its part number.
SKU_NAMES = {
    "O365_BUSINESS_ESSENTIALS": "Microsoft 365 Business Basic",
    "O365_BUSINESS_PREMIUM": "Microsoft 365 Business Standard",
    "SPB": "Microsoft 365 Business Premium",
    "SMB_BUSINESS": "Microsoft 365 Apps for business",
    "OFFICESUBSCRIPTION": "Microsoft 365 Apps for enterprise",
    "STANDARDPACK": "Office 365 E1",
    "ENTERPRISEPACK": "Office 365 E3",
    "ENTERPRISEPREMIUM": "Office 365 E5",
    "SPE_E3": "Microsoft 365 E3",
    "SPE_E5": "Microsoft 365 E5",
    "SPE_F1": "Microsoft 365 F3",
    "DESKLESSPACK": "Office 365 F3",
    "EXCHANGESTANDARD": "Exchange Online (Plan 1)",
    "EXCHANGEENTERPRISE": "Exchange Online (Plan 2)",
    "EXCHANGEARCHIVE_ADDON": "Exchange Online Archiving",
    "AAD_PREMIUM": "Microsoft Entra ID P1",
    "AAD_PREMIUM_P2": "Microsoft Entra ID P2",
    "EMS": "Enterprise Mobility + Security E3",
    "EMSPREMIUM": "Enterprise Mobility + Security E5",
    "INTUNE_A": "Microsoft Intune Plan 1",
    "ATP_ENTERPRISE": "Microsoft Defender for Office 365 (Plan 1)",
    "THREAT_INTELLIGENCE": "Microsoft Defender for Office 365 (Plan 2)",
    "VISIOCLIENT": "Visio Plan 2",
    "PROJECTPROFESSIONAL": "Project Plan 3",
    "POWER_BI_PRO": "Power BI Pro",
    "MCOEV": "Teams Phone Standard",
    "MCOPSTN1": "Domestic Calling Plan",
    "Microsoft_365_Copilot": "Microsoft 365 Copilot",
}

# Free or trial SKUs that are never "waste", however many units are unassigned.
FREE_SKUS = {
    "FLOW_FREE", "POWER_BI_STANDARD", "TEAMS_EXPLORATORY", "WINDOWS_STORE", "MICROSOFT_BUSINESS_CENTER",
    "STREAM", "CCIBOTS_PRIVPREV_VIRAL", "POWERAPPS_VIRAL", "POWERAPPS_DEV", "RIGHTSMANAGEMENT_ADHOC",
    "MCOMEETADV", "PHONESYSTEM_VIRTUALUSER", "Teams_Premium_(for_Departments)_Trial",
}
# Units at or above this are viral/free allocations, not purchases.
FREE_UNIT_THRESHOLD = 10000

STALE_DAYS = 90


def sku_name(part_number: str | None) -> str:
    return SKU_NAMES.get(part_number or "", part_number or "Unknown licence")


def load_prices(conn) -> dict[str, float]:
    return {r["sku_part_number"]: r["monthly_price"] for r in conn.execute(
        "SELECT sku_part_number, monthly_price FROM sku_prices")}


def last_sign_in(user: dict) -> datetime | None:
    values = [user.get("lastSignInDateTime"), user.get("lastNonInteractiveSignInDateTime"),
              user.get("lastSuccessfulSignInDateTime")]
    parsed = [dt for dt in map(parse_ts, values) if dt]
    return max(parsed) if parsed else None


def is_stale(user: dict, now: datetime, days: int = STALE_DAYS) -> bool | None:
    """True/False, or None when sign-in activity isn't available for the tenant."""
    if not user.get("signInActivityAvailable"):
        return None
    last = last_sign_in(user)
    if last is not None:
        return last < now - timedelta(days=days)
    created = parse_ts(user.get("createdDateTime"))
    if created:
        return created < now - timedelta(days=30)
    return True


@dataclass
class SkuLine:
    sku_id: str
    part_number: str
    name: str
    purchased: int
    assigned: int
    unassigned: int
    on_disabled_users: int = 0
    on_stale_users: int = 0
    monthly_price: float | None = None
    free: bool = False

    @property
    def wasted_units(self) -> int:
        if self.free:
            return 0
        return self.unassigned + self.on_disabled_users + self.on_stale_users

    @property
    def monthly_waste(self) -> float | None:
        if self.monthly_price is None:
            return None
        return round(self.wasted_units * self.monthly_price, 2)


@dataclass
class LicenceSummary:
    lines: list[SkuLine] = field(default_factory=list)
    stale_available: bool = False

    @property
    def wasted_units(self) -> int:
        return sum(line.wasted_units for line in self.lines)

    @property
    def monthly_waste(self) -> float:
        return round(sum(line.monthly_waste or 0 for line in self.lines), 2)

    @property
    def unpriced_waste_units(self) -> int:
        return sum(line.wasted_units for line in self.lines if line.monthly_price is None)


def licence_summary(skus: list[dict], users: list[dict] | None, prices: dict[str, float],
                    now: datetime | None = None) -> LicenceSummary:
    now = now or datetime.now(UTC)
    summary = LicenceSummary()
    by_id: dict[str, SkuLine] = {}
    for s in skus:
        if s.get("appliesTo") != "User":
            continue
        purchased = (s.get("prepaidUnits") or {}).get("enabled") or 0
        assigned = s.get("consumedUnits") or 0
        if (purchased == 0 and not assigned) or not s.get("skuId"):
            continue  # expired or suspended subscription with nothing assigned
        part = s.get("skuPartNumber") or ""
        line = SkuLine(
            sku_id=s["skuId"], part_number=part, name=sku_name(part), purchased=purchased,
            assigned=assigned, unassigned=max(purchased - assigned, 0),
            monthly_price=prices.get(part),
            free=part in FREE_SKUS or purchased >= FREE_UNIT_THRESHOLD,
        )
        by_id[line.sku_id] = line
    for u in users or []:
        stale = is_stale(u, now) if u.get("accountEnabled") else False
        if u.get("accountEnabled") and stale is not None:  # a disabled user says nothing about sign-in data
            summary.stale_available = True
        for sku_id in u.get("skuIds") or []:
            line = by_id.get(sku_id)
            if line is None:
                continue
            if not u.get("accountEnabled"):
                line.on_disabled_users += 1
            elif stale:
                line.on_stale_users += 1
    summary.lines = sorted(by_id.values(), key=lambda line: (line.free, -(line.monthly_waste or 0), line.name))
    return summary


def is_free(sku: dict) -> bool:
    """A free, viral or trial allocation (Power Automate Free, say) rather than something paid for."""
    return sku.get("skuPartNumber") in FREE_SKUS or ((sku.get("prepaidUnits") or {}).get("enabled") or 0) \
        >= FREE_UNIT_THRESHOLD


# -- How licences are assigned ----------------------------------------------------------------

# Group-based licensing errors (licenseAssignmentState.error): what each means and what to do.
ASSIGNMENT_ERRORS = {
    "CountViolation": ("Not enough licences: every one bought is already assigned",
                       "Buy more, or remove the licence from someone who doesn't need it"),
    "MutuallyExclusiveViolation": ("Conflicts with another licence the person has, and the two can't be combined",
                                   "Remove one of the two licences, or take the person out of one of the groups"),
    "DependencyViolation": ("Needs another licence the person doesn't have (an add-on without its base licence)",
                            "Assign the licence it depends on first, or remove the add-on"),
    "ProhibitedInUsageLocationViolation": ("Isn't sold for use in the person's usage location (country)",
                                           "Correct the person's usage location, or use a licence sold there"),
    "UniquenessViolation": ("Clashes with another account, usually an email address used twice",
                            "Remove the duplicate email address, then reprocess the group's licences"),
    "Other": ("Microsoft couldn't assign it",
              "Reprocess the group's licences in the Entra admin center; if it fails again, check the account"),
}
ERROR_STATES = ("Error", "ActiveWithError")


def assignment_error(code: str | None) -> tuple[str, str]:
    """(what it means, what to do) for a licence assignment error code."""
    return ASSIGNMENT_ERRORS.get(code or "Other", ASSIGNMENT_ERRORS["Other"])


@dataclass
class AssignmentError:
    group: str | None  # group object ID; None when assigned directly
    code: str
    active: bool  # ActiveWithError: the licence works, but not all of it
    since: str | None = None


@dataclass
class Assignment:
    """One licence (SKU) for one account: where it comes from and whether it worked."""
    sku_id: str
    direct: bool = False
    groups: list[str] = field(default_factory=list)  # group object IDs
    errors: list[AssignmentError] = field(default_factory=list)
    active: bool = False  # the person can use it

    @property
    def path(self) -> str:
        if self.direct and self.groups:
            return "Directly and by group"
        return "By group" if self.groups else "Directly"


def assignments(user: dict) -> dict[str, Assignment] | None:
    """Every licence an account holds, or should hold through a group, by SKU ID.

    None when how licences are assigned wasn't collected (a Users snapshot from before it was):
    then only the SKUs in ``skuIds`` are known, not where they come from.
    """
    states = user.get("licenceStates")
    if not isinstance(states, list):
        return None
    out: dict[str, Assignment] = {}
    for s in states:
        if not isinstance(s, dict) or not isinstance(s.get("skuId"), str):
            continue
        a = out.setdefault(s["skuId"], Assignment(s["skuId"]))
        group = s.get("group") if isinstance(s.get("group"), str) and s.get("group") else None
        if group is None:
            a.direct = True
        elif group not in a.groups:
            a.groups.append(group)
        state = s.get("state")
        if state in ERROR_STATES:
            a.errors.append(AssignmentError(group, s.get("error") if isinstance(s.get("error"), str) else "Other",
                                            state == "ActiveWithError",
                                            s.get("updated") if isinstance(s.get("updated"), str) else None))
        if state in ("Active", "ActiveWithError"):
            a.active = True
    for sid in user.get("skuIds") or []:  # assigned, so it works whatever the states say
        if isinstance(sid, str):
            out.setdefault(sid, Assignment(sid, direct=True)).active = True
    return out
