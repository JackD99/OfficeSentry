"""Licence names, MSP-entered prices, and licence-waste calculation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

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
    parsed = [datetime.fromisoformat(v.replace("Z", "+00:00")) for v in values if v]
    return max(parsed) if parsed else None


def is_stale(user: dict, now: datetime, days: int = STALE_DAYS) -> bool | None:
    """True/False, or None when sign-in activity isn't available for the tenant."""
    if not user.get("signInActivityAvailable"):
        return None
    last = last_sign_in(user)
    if last is not None:
        return last < now - timedelta(days=days)
    created = user.get("createdDateTime")
    if created:
        return datetime.fromisoformat(created.replace("Z", "+00:00")) < now - timedelta(days=30)
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
        purchased = (s.get("prepaidUnits") or {}).get("enabled", 0)
        if purchased == 0 and not s.get("consumedUnits"):
            continue  # expired or suspended subscription with nothing assigned
        part = s.get("skuPartNumber", "")
        line = SkuLine(
            sku_id=s["skuId"], part_number=part, name=sku_name(part), purchased=purchased,
            assigned=s.get("consumedUnits", 0), unassigned=max(purchased - s.get("consumedUnits", 0), 0),
            monthly_price=prices.get(part),
            free=part in FREE_SKUS or purchased >= FREE_UNIT_THRESHOLD,
        )
        by_id[line.sku_id] = line
    for u in users or []:
        stale = is_stale(u, now) if u.get("accountEnabled") else False
        if stale is not None:
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
