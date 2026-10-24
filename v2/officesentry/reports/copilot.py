"""The Microsoft 365 Copilot report: who uses the Copilot licences a client pays for, where, and who could next.

Reads the per-person usage rows (``collectors/usage.py``: each Copilot holder's last use, overall and in each app),
Copilot's daily counts (``copilot_day`` objects) and the tenant's licences and accounts. A Copilot licence is told
by its service plan (``usage.COPILOT_PLAN``), so the add-on, Copilot Business and suites that include Copilot all
count; only an add-on's price is what an unused one costs (``usage.copilot_addon``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .. import clouds, usage
from ..devices import parse_day, parse_ts
from ..licences import last_sign_in, load_prices, never_signed_in, sku_name, trial_seats
from ..snapshot import TenantSnapshot
from ..text import plural, verb
from ..usage import ACTIVE_DAYS, APPS, COPILOT_APPS
from .charts import line_chart
from .data_reports import Chart, DataReport, Rows, Section, Tile, metric_trend
from .identity_reports import Money, _day
from .monthly_review import setting
from .registry import report
from .usage_reports import _load, _office_licensed, _product, _share

NEW_DAYS = 14  # a Copilot licence given this recently isn't called unused yet
NEXT_PEOPLE = 25  # how many people "Who could use Copilot next" lists
UNUSED = "Unused Copilot licences"
NOT_READ = (
    "Microsoft's Copilot usage report couldn't be read at the last collection, so who uses Copilot isn't known. "
    "The licences below come from the accounts."
)
HIDDEN = (
    "Microsoft hides names in this tenant's usage reports, so Copilot's report can't be matched to accounts: "
    "people are the codes Microsoft gives them, and when each last signed in isn't shown."
)


@dataclass
class Holder:
    """Someone with a Copilot licence: their usage row (None when Copilot's report doesn't list them yet), their
    account (None when names are hidden or accounts weren't collected) and the Copilot licence they hold."""

    label: str
    row: dict | None
    user: dict | None
    sku: dict | None

    @property
    def active(self) -> bool:
        return bool(self.row and self.row.get("copilotActive"))

    @property
    def blocked(self) -> bool:
        return bool(self.user is not None and self.user.get("accountEnabled") is False)

    @property
    def last_used(self) -> str | None:
        return (self.row or {}).get("copilotLastActive") or None


def _recent(value, ref: date) -> bool:
    day = parse_day(value) if isinstance(value, str) and value else None
    return bool(day and day > ref - timedelta(days=ACTIVE_DAYS))


def _given(user: dict | None, sku_id: str | None, now: datetime) -> int | None:
    """Days since the Copilot licence was given (its assignment last changed), when Microsoft says."""
    for state in (user or {}).get("licenceStates") or []:
        if isinstance(state, dict) and state.get("skuId") == sku_id:
            when = parse_ts(state.get("updated"))
            if when:
                return max((now - when).days, 0)
    return None


def _holders(snap: TenantSnapshot, rows: dict[str, dict], skus: dict[str, dict], hidden: bool) -> list[Holder]:
    """Everyone with Copilot: in Copilot's report, or holding a Copilot licence on their account (names hidden,
    only the report: its codes can't be matched to accounts)."""
    users = snap.objects("users", "user")
    by_upn = {u["userPrincipalName"].lower(): u for u in users or [] if isinstance(u.get("userPrincipalName"), str)}
    by_part = {s.get("skuPartNumber"): s for s in skus.values()}
    out: dict[str, Holder] = {}
    for upn, row in rows.items():
        if row.get("copilotLicensed") is True:
            user = None if hidden else by_upn.get(upn)
            mine = [skus[s] for s in (user or {}).get("skuIds") or [] if s in skus]
            from_products = [by_part[p] for p in map(usage.part_for_product, row.get("products") or []) if p in by_part]
            out[upn] = Holder(usage.label(row), row, user, (mine or from_products or [None])[0])
    if not hidden:
        for upn, user in by_upn.items():
            mine = [skus[s] for s in user.get("skuIds") or [] if s in skus]
            if mine and upn not in out and user.get("userType") != "Guest":
                out[upn] = Holder(user["userPrincipalName"], rows.get(upn), user, mine[0])
    return sorted(out.values(), key=lambda h: h.label.lower())


@report(
    "copilot",
    "Microsoft 365 Copilot",
    "Who uses the Copilot licences you pay for and in which apps, which licences go unused and what they cost, and "
    "who could use Copilot next.",
    ("m365_usage", "users", "tenant_profile"),
    group="tenant",
    order=55,
    uses=("licences",),
    keywords="ai adoption copilot chat agents prompts unused licenses seats reassign trial e7",
)
def copilot_report(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    if why := clouds.missing(clouds.of(snap.tenant), "usage.copilot"):
        report.unavailable = why
        return
    loaded = _load(snap, report)
    if loaded is None:
        return
    status, rows = loaded
    read = status.get("read") or {}
    hidden = bool(status.get("concealed"))
    known = read.get("copilot") is True  # Copilot's report was read, so who uses Copilot is known
    report.parts = {"copilot": "read" if known else "failed"}
    if not known:
        report.notes.append(NOT_READ)
    elif hidden:
        report.notes.append(HIDDEN)
    ref = parse_day(status.get("copilotReportDate") or status.get("reportDate") or "") or now.date()
    skus = usage.copilot_skus(snap.objects("tenant_profile", "subscribed_sku"))
    holders = _holders(snap, rows, skus, hidden)
    prices = load_prices(snap.conn, snap.tenant)
    currency = setting(snap.conn, "currency", "GBP")
    trials = trial_seats(snap.objects("tenant_profile", "company_subscription"))

    unused, new = [], 0
    for h in holders:
        if h.active or not known:
            continue
        given = _given(h.user, (h.sku or {}).get("skuId"), now)
        if given is not None and given < NEW_DAYS and not h.blocked:
            new += 1
        else:
            unused.append(h)

    report.sections = [
        _by_app(holders, ref),
        _people(holders, unused, ref, known),
        _unused(unused, prices, currency, trials),
        _licences(skus, holders, unused, prices, currency, trials),
        _next(snap, rows, holders),
    ]
    if not skus:
        report.sections = [s for s in report.sections if s.title != "Copilot licences"]
    if new:
        report.notes.append(
            f"{plural(new, 'Copilot licence')} given in the last {NEW_DAYS} days {verb(new, 'is', 'are')}n't counted "
            "as unused yet."
        )
    if any(trials.get(s) for s in skus):
        report.notes.append(
            "Some Copilot licences are a trial's, free until the trial ends, so they aren't counted as a cost."
        )
    report.tiles = _tiles(snap, now, holders, unused, skus, prices, currency, trials, known)

    points = [
        (parse_day(d.get("date")), d.get("active"))
        for d in snap.objects("m365_usage", "copilot_day") or []
        if isinstance(d.get("active"), int) and parse_day(d.get("date") or "")
    ]
    if len(points) >= 2:
        report.chart = line_chart(sorted(points), label="People using Copilot", unit="", y_max=None)
        report.chart_title = "People who used Copilot each day, Microsoft's last 90 days"


def _by_app(holders: list[Holder], ref: date) -> Section:
    known = [h for h in holders if h.row is not None and isinstance(h.row.get("copilotApps"), dict)]
    section = Section(
        "Copilot in each app",
        ["App", "People who used it", "Share of people with Copilot"],
        intro=f"How many people with a Copilot licence used it in each app in the last {ACTIVE_DAYS} days.",
        empty="Nobody here has a Copilot licence.",
        chart=Chart("App", ("People who used it",), title="People who used Copilot, by app", ranked=False),
    )
    if not known:
        return section
    for key, name, _, _ in COPILOT_APPS:
        used = sum(1 for h in known if _recent(h.row["copilotApps"].get(key), ref))
        section.add([name, used, _share(used, len(known))])
    return section


def _apps_used(h: Holder, ref: date) -> list[str]:
    apps = (h.row or {}).get("copilotApps")
    return [name for key, name, _, _ in COPILOT_APPS if isinstance(apps, dict) and _recent(apps.get(key), ref)]


def _licence(h: Holder) -> str:
    return sku_name(h.sku.get("skuPartNumber")) if h.sku else "Microsoft 365 Copilot"


def _people(holders: list[Holder], unused: list[Holder], ref: date, known: bool) -> Section:
    section = Section(
        "People with Copilot",
        ["Person", "Last used Copilot", f"Apps used, last {ACTIVE_DAYS} days", "Licence"],
        intro=f"Everyone with a Copilot licence, when they last used it and where. Flagged: not used in the last "
        f"{ACTIVE_DAYS} days.",
        empty="Nobody here has a Copilot licence.",
        drifts=(f"Apps used, last {ACTIVE_DAYS} days",),
    )
    idle = {id(h) for h in unused}
    for h in holders:
        last = h.last_used
        if not known:
            when = "Not known"
        elif last:
            when = _day(last)
        else:
            when = "Never" if (h.row or {}).get("copilotLicensed") else "Not in Microsoft's report yet"
        section.add([h.label, when, _apps_used(h, ref), _licence(h)], "medium" if id(h) in idle else None)
    return section


def _cost(h: Holder, prices: dict[str, float], currency: str, trials: dict[str, int]) -> object:
    """What one unused licence costs a month: an add-on's price, or why there's none."""
    if h.sku is None:
        return "Not known"
    part = h.sku.get("skuPartNumber") or ""
    if not usage.copilot_addon(h.sku):
        return f"Part of {sku_name(part)}"
    if trials.get(h.sku.get("skuId")) and trials[h.sku["skuId"]] >= (
        (h.sku.get("prepaidUnits") or {}).get("enabled") or 0
    ):
        return "Trial"
    price = prices.get(part)
    return Money(price, currency) if price is not None else "Not priced"


def _unused(unused: list[Holder], prices: dict[str, float], currency: str, trials: dict[str, int]) -> Section:
    section = Section(
        UNUSED,
        ["Person", "Last used Copilot", "Last signed in", "Why", "Cost / month"],
        intro=f'Copilot licences nobody used in the last {ACTIVE_DAYS} days. Give them to someone under "Who could '
        f"use Copilot next\", or drop them at renewal. Licences given in the last {NEW_DAYS} days aren't listed.",
        empty=f"Everyone with Copilot used it in the last {ACTIVE_DAYS} days.",
    )
    for h in unused:
        last = h.last_used
        if h.user is None:
            signed_in = ""
        else:
            when = last_sign_in(h.user)
            signed_in = _day(when) if when else never_signed_in(h.user)
        if h.blocked:
            why = "Account blocked from sign-in"
        elif not (h.row or {}).get("copilotLicensed"):
            why = "Not in Microsoft's Copilot report yet"
        else:
            why = f"Not used in the last {ACTIVE_DAYS} days" if last else "Never used"
        section.add(
            [h.label, _day(last) if last else "Never", signed_in, why, _cost(h, prices, currency, trials)], "medium"
        )
    return section


@dataclass
class _Line:
    sku: dict
    bought: int
    trial: int
    assigned: int
    used: int
    unused: int

    @property
    def unassigned(self) -> int:
        return max(self.bought - self.trial - self.assigned, 0)

    @property
    def wasted(self) -> int:
        """Licences paid for and not used: unused and unassigned ones, but never more than the paid ones."""
        return min(self.unused + self.unassigned, max(self.bought - self.trial, 0))


def _lines(skus: dict[str, dict], holders: list[Holder], unused: list[Holder], trials: dict[str, int]) -> list[_Line]:
    idle = {id(h) for h in unused}
    out = []
    for sku_id, sku in skus.items():
        mine = [h for h in holders if h.sku is not None and h.sku.get("skuId") == sku_id]
        bought = (sku.get("prepaidUnits") or {}).get("enabled") if isinstance(sku.get("prepaidUnits"), dict) else 0
        assigned = sku.get("consumedUnits")
        out.append(
            _Line(
                sku,
                bought if isinstance(bought, int) else 0,
                min(trials.get(sku_id, 0), bought if isinstance(bought, int) else 0),
                assigned if isinstance(assigned, int) else len(mine),
                sum(1 for h in mine if h.active),
                sum(1 for h in mine if id(h) in idle),
            )
        )
    return sorted(out, key=lambda line: sku_name(line.sku.get("skuPartNumber")))


def _licences(
    skus: dict[str, dict],
    holders: list[Holder],
    unused: list[Holder],
    prices: dict[str, float],
    currency: str,
    trials: dict[str, int],
) -> Section:
    section = Section(
        "Copilot licences",
        ["Licence", "Bought", "Assigned", "Not assigned", f"Used in the last {ACTIVE_DAYS} days", "Price / month"],
        intro="Each licence that includes Copilot. A suite that includes it (E7, a bundle) has no Copilot price of its "
        "own, so its unused licences aren't costed here.",
        empty="No licence here includes Copilot.",
    )
    for line in _lines(skus, holders, unused, trials):
        part = line.sku.get("skuPartNumber") or ""
        price = prices.get(part)
        section.add(
            [
                sku_name(part),
                line.bought,
                line.assigned,
                line.unassigned,
                line.used,
                (Money(price, currency) if price is not None else "Not priced")
                if usage.copilot_addon(line.sku)
                else "In the suite's price",
            ],
            "medium" if line.unassigned else None,
        )
    return section


def _next(snap: TenantSnapshot, rows: dict[str, dict], holders: list[Holder]) -> Section:
    section = Section(
        "Who could use Copilot next",
        ["Person", "Meetings", "Teams messages", "Emails sent", "Office apps used", "Licence"],
        intro=f"The {NEXT_PEOPLE} busiest people without Copilot in the last {ACTIVE_DAYS} days, by Teams meetings, "
        "then messages and emails sent, then Office apps used: who would get most from an unused licence or a trial.",
        empty="Nobody without Copilot used Microsoft 365 in the last 30 days.",
        compare=False,
    )
    held = {id(h.row) for h in holders if h.row is not None}
    users = {
        u["userPrincipalName"].lower(): u
        for u in snap.objects("users", "user") or []
        if isinstance(u.get("userPrincipalName"), str)
    }
    blocked = usage.blocked(snap)
    found = []
    for upn, r in rows.items():
        user = users.get(upn)
        if id(r) in held or upn in blocked or (user and user.get("userType") == "Guest"):
            continue
        if not usage.any_activity(r) or not _office_licensed(r):
            continue
        teams = r.get("teamsCounts") if isinstance(r.get("teamsCounts"), dict) else {}
        email = r.get("email") if isinstance(r.get("email"), dict) else {}
        meetings, messages, sent = (
            _count(teams.get("meetings")),
            _count(teams.get("messages")),
            _count(email.get("sent")),
        )
        apps = r.get("apps") if isinstance(r.get("apps"), dict) else {}
        office = [name for key, name in APPS if apps.get(key) and key != "teams"]
        products = [_product(p) for p in r.get("products") or [] if isinstance(p, str) and "FREE" not in p.upper()]
        found.append(
            (
                (-meetings, -(messages + sent), -len(office), usage.label(r).lower()),
                [usage.label(r), meetings, messages, sent, office, products],
            )
        )
    for _, row in sorted(found, key=lambda x: x[0])[:NEXT_PEOPLE]:
        section.add(row)
    return section


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _tiles(
    snap: TenantSnapshot,
    now: datetime,
    holders: list[Holder],
    unused: list[Holder],
    skus: dict[str, dict],
    prices: dict[str, float],
    currency: str,
    trials: dict[str, int],
    known: bool,
) -> list[Tile]:
    lines = _lines(skus, holders, unused, trials)
    bought = sum(line.bought for line in lines)
    unassigned = sum(line.unassigned for line in lines)
    listed = [h for h in holders if (h.row or {}).get("copilotLicensed")]
    active = sum(1 for h in listed if h.active)
    if lines:
        foot = f"{bought} bought, {unassigned} not assigned"
    else:
        foot = "From Microsoft's Copilot report" if holders else "Nobody here has Microsoft 365 Copilot"
    tiles = [
        Tile("Copilot licences", len(holders), foot, rows=Rows("People with Copilot")),
        Tile(
            "Using Copilot",
            _share(active, len(listed)) if known else "Not known",
            f"{active} of {len(listed)} used it in the last {ACTIVE_DAYS} days"
            if known
            else "Microsoft's Copilot report couldn't be read",
            ("ok" if active == len(listed) else "warn") if known and listed else None,
            Rows("People with Copilot"),
            trend=metric_trend(snap, "copilot.active", now),
        ),
        Tile(
            "Unused licences",
            len(unused) + unassigned,
            f"Not used in {ACTIVE_DAYS} days, or not assigned",
            "warn" if unused or unassigned else "ok",
            Rows(UNUSED),
        ),
    ]
    addons = [line for line in lines if usage.copilot_addon(line.sku) and line.wasted]
    priced = [line for line in addons if prices.get(line.sku.get("skuPartNumber")) is not None]
    total = round(sum(line.wasted * prices[line.sku.get("skuPartNumber")] for line in priced), 2)
    tiles.append(
        Tile(
            "Unused cost / month",
            Money(total, currency) if priced or not addons else "Not priced",
            "Unused and unassigned add-on licences, at your prices"
            if len(priced) == len(addons)
            else "Some Copilot licences have no price yet: add one on the Licence prices page",
            "warn" if total else None,
            Rows(UNUSED),
        )
    )
    return tiles
