"""Licence names, prices (the MSP's, Microsoft's list prices and the margin), and licence-waste calculation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import list_prices
from .devices import parse_ts
from .text import plural

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
    "M365_F1": "Microsoft 365 F1",
    "M365_F1_COMM": "Microsoft 365 F1",
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
    "FLOW_FREE": "Power Automate Free",
    # The editions sold without Teams. Microsoft's part numbers for them are irregular, spaces included.
    "Microsoft_365_ Business_ Premium_(no Teams)": "Microsoft 365 Business Premium (no Teams)",
    "O365_BUSINESS_PREMIUM_NOTEAMS": "Microsoft 365 Business Standard (no Teams)",
    "Microsoft_365_Business_Standard_EEA_(no_Teams)": "Microsoft 365 Business Standard EEA (no Teams)",
    "Microsoft_365_Business_Premium_EEA_(no_Teams)": "Microsoft 365 Business Premium EEA (no Teams)",
    "Microsoft_365_Business_Basic_EEA_(no_Teams)": "Microsoft 365 Business Basic EEA (no Teams)",
}

# Free or trial SKUs that are never "waste", however many units are unassigned.
FREE_SKUS = {
    "FLOW_FREE",
    "POWER_BI_STANDARD",
    "TEAMS_EXPLORATORY",
    "WINDOWS_STORE",
    "MICROSOFT_BUSINESS_CENTER",
    "STREAM",
    "CCIBOTS_PRIVPREV_VIRAL",
    "POWERAPPS_VIRAL",
    "POWERAPPS_DEV",
    "RIGHTSMANAGEMENT_ADHOC",
    "MCOMEETADV",
    "PHONESYSTEM_VIRTUALUSER",
    "Teams_Premium_(for_Departments)_Trial",
}
# Units at or above this are viral/free allocations, not purchases.
FREE_UNIT_THRESHOLD = 10000

STALE_DAYS = 90


def sku_name(part_number: str | None) -> str:
    return SKU_NAMES.get(part_number or "", part_number or "Unknown licence")


# -- Prices -------------------------------------------------------------------------------------------
#
# What a client pays for a licence each month: the MSP's own price for it (``sku_prices``), else Microsoft's
# list price where one is built in for the currency (``list_prices``), plus a margin in percent. The margin is
# the MSP's (setting ``prices.margin``) unless the client has its own (``tenants.price_margin``); a negative
# one is a discount. ``prices.list`` "off" leaves licences the MSP hasn't priced unpriced.

# Reports that put money on licences, whose page says where the prices came from.
PRICED_REPORTS = ("licences", "licence-rightsizing", "subscriptions", "licence-changes")
MARGIN_KEY = "prices.margin"
LIST_KEY = "prices.list"
MARGIN_MIN, MARGIN_MAX = -90.0, 500.0


@dataclass(frozen=True)
class Price:
    monthly: float  # what the client pays per licence per month, margin included
    base: float  # before the margin
    source: str  # "yours" (entered or imported) or "list" (Microsoft's list price)
    margin: float  # percent added; negative for a discount

    @property
    def explained(self) -> str:
        """Where the price came from, in words: "List price + 12%", "Your price"."""
        base = "List price" if self.source == "list" else "Your price"
        if not self.margin:
            return base
        sign = "+" if self.margin > 0 else "−"
        return f"{base} {sign} {abs(self.margin):g}%"


def _setting(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def parse_margin(value) -> float | None:
    """A margin as entered ("12", "-5", "12.5%"), or None when it's blank or out of range."""
    try:
        margin = round(float(str(value).strip().rstrip("%").strip()), 2)
    except (TypeError, ValueError):
        return None
    return margin if MARGIN_MIN <= margin <= MARGIN_MAX else None


def msp_margin(conn) -> float:
    return parse_margin(_setting(conn, MARGIN_KEY)) or 0.0


def own_margin(tenant) -> float | None:
    """The client's own margin, when it has one; None follows the MSP's."""
    try:
        value = tenant["price_margin"]
    except (KeyError, IndexError, TypeError):
        return None
    return parse_margin(value) if value is not None else None


def uses_list_prices(conn) -> bool:
    return _setting(conn, LIST_KEY) != "off"


def currency(conn) -> str:
    return _setting(conn, "currency") or "GBP"


@dataclass
class PriceBook:
    """Every licence price that applies to one client (or, without one, at the MSP's own margin)."""

    entered: dict[str, float]
    currency: str
    margin: float
    use_list: bool = True

    def get(self, part: str | None) -> Price | None:
        part = part or ""
        if part in self.entered:
            base, source = self.entered[part], "yours"
        elif self.use_list and (listed := list_prices.price(part, self.currency)) is not None:
            base, source = listed, "list"
        else:
            return None
        return Price(round(base * (1 + self.margin / 100), 2), base, source, self.margin)

    def monthly(self) -> dict[str, float]:
        """Part number -> monthly price, for everything priced."""
        parts = set(self.entered) | (set(list_prices.PRICES) if self.use_list else set())
        return {p: price.monthly for p in parts if (price := self.get(p)) is not None}

    def note(self, parts) -> str:
        """Where the prices behind these licences came from, for the team (never a client): "Prices: Microsoft's
        list price (1 Jul 2026) for 3 licences and your own for 1, plus a 12% margin." Empty when none is
        priced."""
        prices = [p for part in dict.fromkeys(parts) if (p := self.get(part)) is not None]
        if not prices:
            return ""
        listed = sum(1 for p in prices if p.source == "list")
        yours = len(prices) - listed
        bits = []
        if listed:
            bits.append(f"{list_prices.label()} for {plural(listed, 'licence')}")
        if yours:
            bits.append(f"your own for {plural(yours, 'licence')}")
        text = "Prices: " + " and ".join(bits)
        if self.margin > 0:
            text += f", plus a {self.margin:g}% margin"
        elif self.margin < 0:
            text += f", less a {-self.margin:g}% discount"
        return text + "."


def price_book(conn, tenant=None) -> PriceBook:
    """The prices for one client (its own margin if it has one), or at the MSP's margin without one."""
    entered = {
        r["sku_part_number"]: r["monthly_price"]
        for r in conn.execute("SELECT sku_part_number, monthly_price FROM sku_prices")
    }
    margin = own_margin(tenant) if tenant is not None else None
    if margin is None:
        margin = msp_margin(conn)
    return PriceBook(entered, currency(conn), margin, uses_list_prices(conn))


def load_prices(conn, tenant=None) -> dict[str, float]:
    """Part number -> what the client pays per licence per month (``price_book``)."""
    return price_book(conn, tenant).monthly()


def price_fingerprint(conn) -> list:
    """What every client's prices are worked out from, for the cached rows that put money on licences."""
    entered = conn.execute("SELECT COUNT(*), MAX(updated_at), TOTAL(monthly_price) FROM sku_prices").fetchone()
    return [list(entered), msp_margin(conn), uses_list_prices(conn), list_prices.AS_OF.isoformat()]


def last_sign_in(user: dict) -> datetime | None:
    """When the account last signed in successfully, as far as Microsoft says.

    Microsoft's last interactive and non-interactive sign-in times move on failed attempts too, so a password
    spray on a leaver's account would make it look in use. When Microsoft sent the last successful sign-in
    (``successfulSignInKnown``), that's the answer. Microsoft only records it from December 2023, so with none,
    an attempt from before then is taken as the last sign-in, as it always was. Snapshots from before Office
    Sentry kept the successful time use the newest of the three.
    """
    if user.get("successfulSignInKnown"):
        successful = parse_ts(user.get("lastSuccessfulSignInDateTime"))
        if successful:
            return successful
        older = [dt for dt in map(parse_ts, _attempts(user)) if dt and dt < SUCCESS_RECORDED_FROM]
        return max(older) if older else None
    values = [
        user.get("lastSignInDateTime"),
        user.get("lastNonInteractiveSignInDateTime"),
        user.get("lastSuccessfulSignInDateTime"),
    ]
    parsed = [dt for dt in map(parse_ts, values) if dt]
    return max(parsed) if parsed else None


def never_signed_in(user: dict) -> str:
    """What to show for an account ``last_sign_in`` has no date for: "Never", or that someone tried and failed
    (Microsoft has attempts but no success), which on an unused account is often a password spray."""
    return FAILED_ONLY if any(parse_ts(a) for a in _attempts(user)) else "Never"


FAILED_ONLY = "Failed attempts only"
SUCCESS_RECORDED_FROM = datetime(2023, 12, 1, tzinfo=UTC)  # Microsoft's lastSuccessfulSignInDateTime


def _attempts(user: dict) -> tuple:
    return user.get("lastSignInDateTime"), user.get("lastNonInteractiveSignInDateTime")


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
    trial: int = 0  # of the purchased licences, how many are an active trial's (free until it ends)

    @property
    def wasted_units(self) -> int:
        if self.free:
            return 0
        wasted = self.unassigned + self.on_disabled_users + self.on_stale_users
        # Trial licences cost nothing, so at most the paid ones are wasted.
        return min(wasted, max(self.purchased - self.trial, 0)) if self.trial else wasted

    @property
    def monthly_waste(self) -> float | None:
        if self.monthly_price is None:
            return None
        return round(self.wasted_units * self.monthly_price, 2)


@dataclass
class LicenceSummary:
    lines: list[SkuLine] = field(default_factory=list)
    stale_available: bool = False
    kept: int = 0  # shared mailboxes holding paid licences they need, not counted as waste

    @property
    def wasted_units(self) -> int:
        return sum(line.wasted_units for line in self.lines)

    @property
    def monthly_waste(self) -> float:
        return round(sum(line.monthly_waste or 0 for line in self.lines), 2)

    @property
    def unpriced_waste_units(self) -> int:
        return sum(line.wasted_units for line in self.lines if line.monthly_price is None)


def trial_seats(subscriptions: list[dict] | None) -> dict[str, int]:
    """Licences of active trial subscriptions, by SKU ID: included in a SKU's purchased count, but free."""
    out: dict[str, int] = {}
    for s in subscriptions or []:
        total, sid = s.get("totalLicenses"), s.get("skuId")
        if s.get("isTrial") is True and s.get("status") == "Enabled" and isinstance(sid, str):
            if isinstance(total, int) and not isinstance(total, bool) and total > 0:
                out[sid] = out.get(sid, 0) + total
    return out


def licence_summary(
    skus: list[dict],
    users: list[dict] | None,
    prices: dict[str, float],
    now: datetime | None = None,
    subscriptions: list[dict] | None = None,
    keep: set[str] | None = None,
) -> LicenceSummary:
    """Licences bought, assigned and wasted per SKU. Pass the tenant's subscriptions (company_subscription
    objects) so trial licences aren't counted as bought and unused, and ``keep`` (``shared_needing_licence``)
    so shared mailboxes that need their licence aren't counted as disabled accounts holding one."""
    now = now or datetime.now(UTC)
    summary = LicenceSummary()
    by_id: dict[str, SkuLine] = {}
    trials = trial_seats(subscriptions)
    for s in skus:
        if s.get("appliesTo") != "User":
            continue
        purchased = (s.get("prepaidUnits") or {}).get("enabled") or 0
        assigned = s.get("consumedUnits") or 0
        if (purchased == 0 and not assigned) or not s.get("skuId"):
            continue  # expired or suspended subscription with nothing assigned
        part = s.get("skuPartNumber") or ""
        trial = min(trials.get(s["skuId"], 0), purchased)
        line = SkuLine(
            sku_id=s["skuId"],
            part_number=part,
            name=sku_name(part),
            purchased=purchased,
            assigned=assigned,
            unassigned=max(purchased - trial - assigned, 0),  # unused paid licences: trial ones are free
            monthly_price=prices.get(part),
            free=part in FREE_SKUS or purchased >= FREE_UNIT_THRESHOLD,
            trial=trial,
        )
        by_id[line.sku_id] = line
    for u in users or []:
        stale = is_stale(u, now) if u.get("accountEnabled") else False
        if u.get("accountEnabled") and stale is not None:  # a disabled user says nothing about sign-in data
            summary.stale_available = True
        if keep and u.get("_id") in keep:
            summary.kept += any(sku_id in by_id for sku_id in u.get("skuIds") or [])
            continue
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


# Exchange Online gives a shared mailbox 50 GB without a licence.
SHARED_FREE_BYTES = 50 * 1024**3


def shared_needing_licence(snap) -> set[str]:
    """Shared mailboxes that must keep a licence although their account is blocked from sign-in (as it
    should be): over 50 GB, with an archive or on litigation hold. Their directory object ids."""
    sizes = {r.get("mailboxId") or r["_id"]: r for r in snap.objects("mailbox_usage", "mailbox_size") or []}
    out = set()
    for m in snap.objects("mailboxes", "mailbox") or []:
        if m.get("RecipientTypeDetails") != "SharedMailbox" or not m.get("ExternalDirectoryObjectId"):
            continue
        size = sizes.get(m["_id"]) or {}
        archive = m.get("ArchiveStatus") not in (None, "", "None")
        big = isinstance(size.get("totalBytes"), int) and size["totalBytes"] > SHARED_FREE_BYTES
        if archive or m.get("LitigationHoldEnabled") or big:
            out.add(m["ExternalDirectoryObjectId"])
    return out


def working_units(sku: dict) -> int:
    """Licences that work: enabled ones, plus those of a subscription in its grace period, which Microsoft
    moves from enabled to warning until the subscription is suspended."""
    units = sku.get("prepaidUnits") if isinstance(sku.get("prepaidUnits"), dict) else {}
    return (units.get("enabled") or 0) + (units.get("warning") or 0)


def is_free(sku: dict) -> bool:
    """A free, viral or trial allocation (Power Automate Free, say) rather than something paid for."""
    return (
        sku.get("skuPartNumber") in FREE_SKUS
        or ((sku.get("prepaidUnits") or {}).get("enabled") or 0) >= FREE_UNIT_THRESHOLD
    )


# -- How licences are assigned ----------------------------------------------------------------

# Group-based licensing errors (licenseAssignmentState.error): what each means and what to do.
ASSIGNMENT_ERRORS = {
    "CountViolation": (
        "Not enough licences: every one bought is already assigned",
        "Buy more, or remove the licence from someone who doesn't need it",
    ),
    "MutuallyExclusiveViolation": (
        "Conflicts with another licence the person has, and the two can't be combined",
        "Remove one of the two licences, or take the person out of one of the groups",
    ),
    "DependencyViolation": (
        "Needs another licence the person doesn't have (an add-on without its base licence)",
        "Assign the licence it depends on first, or remove the add-on",
    ),
    "ProhibitedInUsageLocationViolation": (
        "Isn't sold for use in the person's usage location (country)",
        "Correct the person's usage location, or use a licence sold there",
    ),
    "UniquenessViolation": (
        "Clashes with another account, usually an email address used twice",
        "Remove the duplicate email address, then reprocess the group's licences",
    ),
    "Other": (
        "Microsoft couldn't assign it",
        "Reprocess the group's licences in the Entra admin center; if it fails again, check the account",
    ),
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
            a.errors.append(
                AssignmentError(
                    group,
                    s.get("error") if isinstance(s.get("error"), str) else "Other",
                    state == "ActiveWithError",
                    s.get("updated") if isinstance(s.get("updated"), str) else None,
                )
            )
        if state in ("Active", "ActiveWithError"):
            a.active = True
    assigned = user.get("skuIds")
    if isinstance(assigned, list):
        # Microsoft keeps an "Active" state for a licence that is no longer assigned (seen on the test tenant, for a
        # SKU the tenant no longer has): it isn't held, and without its subscription it can't even be named.
        for sid in [
            sid
            for sid, a in out.items()
            if sid not in assigned and a.active and a.direct and not a.groups and not a.errors
        ]:
            del out[sid]
    for sid in user.get("skuIds") or []:  # assigned, so it works whatever the states say
        if isinstance(sid, str):
            out.setdefault(sid, Assignment(sid, direct=True)).active = True
    return out
