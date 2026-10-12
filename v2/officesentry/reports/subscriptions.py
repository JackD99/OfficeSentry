"""Licences and subscriptions: what the tenant has bought, when it renews or lapses, and how licences reach people.

For a technician preparing a renewal or fixing licence problems. Every product with
purchased, assigned and available licences and its monthly cost at the MSP's prices;
every subscription behind them with its status and the date it next renews, lapses
or is deleted (trials and subscriptions in their grace period or suspended first);
who holds which licence, directly or through a group; group licensing errors in
plain words; and the groups that hand licences out. Licence waste (unused licences
in money) and Licence right-sizing (cheaper licences from usage) cover the rest.
"""

from __future__ import annotations

from datetime import datetime

from ..devices import parse_ts
from ..licences import Assignment, assignment_error, assignments, is_free, load_prices, sku_name, trial_seats
from ..snapshot import TenantSnapshot
from ..text import plural
from .data_reports import Chart, DataReport, Highlight, Rows, Section
from .identity_reports import Money
from .monthly_review import setting
from .registry import report

# companySubscription.status and subscribedSku.capabilityStatus, in words.
STATUS = {
    "Enabled": "Active",
    "Warning": "Grace period",
    "Suspended": "Suspended",
    "LockedOut": "Locked out",
    "Deleted": "Ended",
    "Expired": "Ended",
}
LAPSING = ("Grace period", "Suspended", "Locked out")
NEEDS_ACTION = ("Trial", *LAPSING)
WINDOWS = (30, 60, 90)
SOON = ("Passed", "30 days")  # "Ends within" values that count as within 30 days
WHAT_HAPPENS = {
    "Trial": "The trial ends on this date. Buy it before then or its licences stop working.",
    "Active": "Renews on this date if it's set to renew automatically; otherwise it expires then. Check the billing "
    "settings.",
    "Grace period": "Expired, but people can still use it. Renew it before this date or it's suspended.",
    "Suspended": "Suspended: people can't use it, though admins can still back up its data. Reactivate it before "
    "this date or it's deleted.",
    "Locked out": "Locked: nobody can use it. Contact Microsoft or the reseller before this date or it's deleted.",
    "Ended": "Ended.",
}
SUBSCRIPTIONS = "Subscriptions"
RENEWALS = "Renewals and expiry"
WHO = "Who has which licence"
PROBLEMS = "Assignment problems"
GROUPS = "Groups that assign licences"
NO_LOCATION = "No usage location"
TWICE = "Directly and by group"
ERROR = "Licence error"


def _status(value, trial: bool = False) -> str:
    text = STATUS.get(value, value if isinstance(value, str) and value else "Unknown")
    return "Trial" if text == "Active" and trial else text


def _within(days: int | None) -> str:
    if days is None:
        return ""
    if days < 0:
        return "Passed"
    return next((f"{w} days" for w in WINDOWS if days <= w), "Later")


def _units(sku: dict, key: str) -> int:
    value = (sku.get("prepaidUnits") or {}).get(key) if isinstance(sku.get("prepaidUnits"), dict) else 0
    return value if isinstance(value, int) else 0


def _consumed(sku: dict) -> int:
    value = sku.get("consumedUnits")
    return value if isinstance(value, int) else 0


def _label(u: dict) -> str:
    return u.get("userPrincipalName") or u.get("displayName") or u.get("_id", "?")


@report(
    "subscriptions",
    "Licences and subscriptions",
    "Every subscription with licences bought, assigned and available, its cost and renewal date, "
    "trials and lapsing subscriptions, and who has which licence, directly or by group.",
    ("tenant_profile", "users", "groups"),
    group="tenant",
    order=20,
    keywords="licenses skus products renewal expiry expiring trial suspended grace period billing invoice "
    "true-up group-based licensing assignment errors usage location csp partner center",
)
def build(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    skus = snap.objects("tenant_profile", "subscribed_sku")
    if skus is None:
        report.unavailable = "Licence data hasn't been collected yet. Refresh Tenant profile and licences."
        return
    skus = [s for s in skus if isinstance(s.get("skuId"), str)]
    by_sku = {s["skuId"]: s for s in skus}
    names = {sid: sku_name(s.get("skuPartNumber")) for sid, s in by_sku.items()}
    prices = load_prices(snap.conn)
    currency = setting(snap.conn, "currency", "GBP")

    status = (snap.objects("tenant_profile", "subscription_status") or [None])[0]
    subs = snap.objects("tenant_profile", "company_subscription") or []
    if status is None:
        report.notes.append("Renewal dates show after the next collection of Tenant profile and licences.")
    elif not status.get("available"):
        report.notes.append(
            "Microsoft didn't return the tenant's subscriptions on the last collection, so renewal "
            "dates aren't shown. The collection log on the client page has the reason."
        )
    known = status is not None and bool(status.get("available"))

    renewals, soon, trials = _renewals(subs, by_sku, now, known)
    subscriptions, cost, priced, unpriced, assigned, bought = _subscriptions(skus, subs, names, prices, currency, now)
    if unpriced:
        report.notes.append(
            f"{plural(unpriced, 'paid product')} {'has' if unpriced == 1 else 'have'} no price, so "
            "the monthly cost leaves them out. An admin can add prices on the Licence prices page."
        )

    users = snap.objects("users", "user")
    groups = {g["_id"]: g for g in snap.objects("groups", "group") or []}
    if users is None:
        report.notes.append("Users haven't been collected yet, so who holds which licence isn't shown.")
    held = {u["_id"]: assignments(u) for u in users or []}
    paths_known = any(a is not None for a in held.values())
    if users is not None and not paths_known:
        report.notes.append(
            "Whether licences are assigned directly or by a group shows after the next collection of Users."
        )
    if snap.objects("groups", "group") is None and paths_known:
        report.notes.append("Groups haven't been collected yet, so groups show by their ID.")

    stopped = {sid for sid, s in by_sku.items() if s.get("capabilityStatus") in ("Suspended", "LockedOut")}
    who, problems, errors, by_group = _people(users or [], held, names, groups, stopped)
    report.sections = [subscriptions, renewals, who, problems, _groups(groups, by_group, names)]

    via_group = sum(1 for row in who.rows if row[2] in ("By group", TWICE))
    report.highlights = [
        Highlight(
            "Monthly cost",
            Money(cost, currency) if priced else "Not priced",
            f"{plural(unpriced, 'product')} not priced" if unpriced and priced else "Licences bought, at your prices",
            rows=Rows(SUBSCRIPTIONS),
        ),
        Highlight("Licences in use", f"{assigned} of {bought}", "Paid licences assigned", rows=Rows(SUBSCRIPTIONS)),
        Highlight(
            "Lapsing within 30 days",
            soon if known else "Not checked",
            "Trials in use, expired or suspended",
            ("bad" if soon else "ok") if known else None,
            Rows(RENEWALS, {"Ends within": list(SOON)}, flagged=True),
        ),
        Highlight(
            "Trials",
            trials if known else "Not checked",
            "Active trial subscriptions",
            "warn" if trials else None,
            Rows(RENEWALS, {"Status": "Trial"}),
        ),
        Highlight(
            "Licence errors",
            errors if paths_known else "Not checked",
            "Licences that couldn't be assigned",
            ("bad" if errors else "ok") if paths_known else None,
            Rows(PROBLEMS, {"Problem": ERROR}),
        ),
        Highlight(
            "Assigned by group",
            f"{via_group} of {len(who.rows)}" if paths_known else "Not checked",
            "Licences that come from a group",
            rows=Rows(WHO, {"Assigned": ["By group", TWICE]}),
        ),
    ]
    report.footnote = (
        "Costs use the monthly prices on the Licence prices page. Microsoft doesn't say whether a paid "
        "subscription renews automatically: check Billing > Your products in the Microsoft 365 admin "
        "center, or Partner Center for subscriptions you sell."
    )


# -- Products and subscriptions -----------------------------------------------------------------


def _next_dates(subs: list[dict], now: datetime) -> dict[str, datetime]:
    """The earliest renewal or end date of each SKU's subscriptions that haven't ended."""
    out: dict[str, datetime] = {}
    for s in subs:
        when = parse_ts(s.get("nextLifecycleDateTime"))
        sid = s.get("skuId")
        if when and isinstance(sid, str) and _status(s.get("status")) != "Ended":
            out[sid] = min(out.get(sid, when), when)
    return out


def _subscriptions(skus, subs, names, prices, currency, now):
    section = Section(
        SUBSCRIPTIONS,
        [
            "Licence",
            "Status",
            "Purchased",
            "Assigned",
            "Available",
            "In grace period",
            "Suspended",
            "Price / month",
            "Cost / month",
            "Next renewal or end",
            "Trial",
        ],
        intro="Every product the tenant has, with licences bought, assigned and still available, and "
        "what they cost a month at your prices. Free products are listed last.",
        empty="The tenant has no subscriptions.",
        optional=("In grace period", "Suspended", "Price / month"),
    )
    section.chart = Chart("Licence", ("Assigned", "Available"), total="Purchased", tones=("1", "muted"))
    next_dates = _next_dates(subs, now)
    trials = {s.get("skuId") for s in subs if s.get("isTrial") is True and _status(s.get("status")) != "Ended"}
    paid_subs = {s.get("skuId") for s in subs if s.get("isTrial") is not True and _status(s.get("status")) != "Ended"}
    free_seats = trial_seats(subs)  # active trials' licences are in the purchased count, but cost nothing
    cost, priced, unpriced, assigned, bought = 0.0, False, 0, 0, 0
    rows = []
    for s in skus:
        sid, part = s["skuId"], s.get("skuPartNumber")
        purchased, used = _units(s, "enabled"), _consumed(s)
        grace, stopped = _units(s, "warning"), _units(s, "suspended") + _units(s, "lockedOut")
        per_user = s.get("appliesTo") != "Company"
        free = is_free(s)
        trial = sid in trials and sid not in paid_subs
        status = _status(s.get("capabilityStatus"), trial)
        price = None if free else prices.get(part)
        paid = max(purchased - free_seats.get(sid, 0), 0)
        if not free and status != "Ended":
            if price is None:
                unpriced += paid > 0
            else:
                priced = True
                cost += paid * price
            if per_user and status not in LAPSING[1:]:  # suspended licences aren't in use
                assigned, bought = assigned + used, bought + purchased + grace
        when = next_dates.get(sid)
        days = (when - now).days if when else None
        flag = None
        if status == "Grace period":
            flag = "high" if used else "medium"
        elif status in ("Suspended", "Locked out"):
            flag = "high" if used > purchased + grace else "low"
        elif per_user and used > purchased + grace:
            flag = "medium"  # more assigned than bought: some people's licences don't work
        elif trial and used and days is not None and days <= 30:
            flag = "medium"
        if free:
            each, total = "Free", ""
        elif purchased and not paid:
            each, total = "Trial", ""  # nothing to pay until the trial is bought
        elif price is None:
            each = total = "Not priced"
        else:
            each, total = Money(price, currency), Money(paid * price, currency)
        rows.append(
            (
                (free, status not in LAPSING and not trial, names[sid].lower()),
                [
                    names[sid],
                    status,
                    purchased,
                    used if per_user else "",
                    purchased + grace - used if per_user else "",
                    grace,
                    stopped,
                    each,
                    total,
                    when.strftime("%Y-%m-%d") if when else "",
                    trial,
                ],
                flag,
            )
        )
    for _, row, flag in sorted(rows, key=lambda r: r[0]):
        section.add(row, flag)
    return section, round(cost, 2), priced, unpriced, assigned, bought


def _renewals(subs, by_sku, now, known):
    section = Section(
        RENEWALS,
        # The subscription ID comes second so What changed tells two subscriptions of one licence apart (it
        # matches rows on the first column, or the first two when the first repeats).
        [
            "Licence",
            "Subscription ID",
            "Status",
            "Licences",
            "Started",
            "Next renewal or end",
            "Ends within",
            "What happens",
        ],
        intro="Each subscription and the date it next renews, lapses or is deleted. Trials and "
        "subscriptions in their grace period, suspended or locked come first.",
        empty="No subscriptions." if known else "Renewal dates haven't been collected.",
    )
    rows = []
    soon = trials = 0
    for s in subs:
        sku = by_sku.get(s.get("skuId")) or {}
        status = _status(s.get("status"), s.get("isTrial") is True)
        when = parse_ts(s.get("nextLifecycleDateTime"))
        days = (when - now).days if when else None
        within = _within(days) if status != "Ended" else ""
        used = _consumed(sku)
        working = _units(sku, "enabled") + _units(sku, "warning")
        flag = None
        if status == "Grace period":
            flag = "high"
        elif status in ("Suspended", "Locked out"):
            flag = "high" if used > working else "low"
        elif status == "Trial" and within in SOON:
            flag = "medium" if used else "low"
        elif status == "Trial" and within in ("60 days", "90 days"):
            flag = "low"
        soon += flag in ("high", "medium") and within in SOON
        trials += status == "Trial"
        total = s.get("totalLicenses")
        name = sku_name(s.get("skuPartNumber") or sku.get("skuPartNumber"))
        started = parse_ts(s.get("createdDateTime"))
        rows.append(
            (
                (status not in NEEDS_ACTION, status == "Ended", days if days is not None else 10**6, name.lower()),
                [
                    name,
                    s.get("commerceSubscriptionId") or s.get("_id") or "",
                    status,
                    total if isinstance(total, int) else "",
                    started.strftime("%Y-%m-%d") if started else "",
                    when.strftime("%Y-%m-%d") if when else "",
                    within,
                    "Free: nothing to renew."
                    if is_free(sku)
                    else WHAT_HAPPENS.get(status, "")
                    if when or status != "Active"
                    else "",
                ],
                flag,
            )
        )
    for _, row, flag in sorted(rows, key=lambda r: r[0]):
        section.add(row, flag)
    return section, soon, trials


# -- How licences are assigned ---------------------------------------------------------------------


def _group_name(gid: str, groups: dict) -> str:
    return (groups.get(gid) or {}).get("displayName") or gid


def _people(users: list[dict], held: dict, names: dict, groups: dict, stopped: set[str]):
    who = Section(
        WHO,
        ["Account", "Licence", "Assigned", "Groups", "State", "Usage location", "Sign-in"],
        intro="One row for each licence each account holds, and whether it was assigned directly or by a "
        "group. Filter by licence to see who has it.",
        empty="Nobody holds a licence.",
    )
    problems = Section(
        PROBLEMS,
        ["Account", "Licence", "Problem", "Group", "What it means", "What to do"],
        intro="Licences that couldn't be assigned, licensed accounts without a usage location, and "
        "licences assigned both directly and by a group.",
        empty="No licence assignment problems.",
    )
    by_group: dict[str, dict] = {}
    errors = 0
    rows, problem_rows = [], []
    for u in users:
        mine: dict[str, Assignment] | None = held.get(u["_id"])
        label = _label(u)
        signin = "Allowed" if u.get("accountEnabled") else "Blocked"
        location = u.get("usageLocation") if isinstance(u.get("usageLocation"), str) else ""
        if mine is None:  # how licences are assigned wasn't collected for this account
            for sid in dict.fromkeys(x for x in u.get("skuIds") or [] if isinstance(x, str)):
                rows.append(
                    (
                        (label.lower(), names.get(sid, "")),
                        [label, names.get(sid, "Unknown licence"), "Not known", [], "Active", location, signin],
                        None,
                    )
                )
            continue
        for sid, a in mine.items():
            name = names.get(sid, "Unknown licence")
            group_names = [_group_name(g, groups) for g in a.groups]
            failed = {e.group for e in a.errors if not e.active}
            for g in a.groups:
                entry = by_group.setdefault(g, {"people": set(), "errors": 0, "skus": set()})
                entry["skus"].add(sid)
                if g not in failed:
                    entry["people"].add(u["_id"])
            if a.active and sid in stopped:
                state = "Subscription suspended"
            elif a.active:
                state = "Active with errors" if a.errors else "Active"
            else:
                state = "Not assigned (error)" if a.errors else "Disabled"
            flag = (
                "medium"
                if (failed and not a.active) or (a.active and sid in stopped)
                else ("low" if a.errors or (a.direct and a.groups) else None)
            )
            rows.append(
                ((label.lower(), name.lower()), [label, name, a.path, group_names, state, location, signin], flag)
            )
            for e in a.errors:
                meaning, todo = assignment_error(e.code)
                errors += 1
                if e.group:
                    by_group.setdefault(e.group, {"people": set(), "errors": 0, "skus": set()})["errors"] += 1
                problem_rows.append(
                    (
                        (0, label.lower(), name.lower()),
                        [
                            label,
                            name,
                            ERROR,
                            _group_name(e.group, groups) if e.group else "Assigned directly",
                            meaning + (" (the licence works, but not every part of it)" if e.active else ""),
                            todo,
                        ],
                        "low" if e.active else "medium",
                    )
                )
            if a.direct and a.groups:
                problem_rows.append(
                    (
                        (2, label.lower(), name.lower()),
                        [
                            label,
                            name,
                            TWICE,
                            group_names,
                            "Removing the account from the group won't remove the licence, which is how leavers keep "
                            "licences.",
                            "Remove the direct assignment so the group manages it",
                        ],
                        "low",
                    )
                )
        if mine and "usageLocation" in u and not location:
            problem_rows.append(
                (
                    (1, label.lower(), ""),
                    [
                        label,
                        [names.get(sid, "Unknown licence") for sid in mine],
                        NO_LOCATION,
                        "",
                        "Microsoft needs a usage location (country) to assign a licence. Groups fall back to the "
                        "organisation's country; assigning one directly fails.",
                        "Set the usage location on the account in the Microsoft 365 admin center",
                    ],
                    "low",
                )
            )
    for _, row, flag in sorted(rows, key=lambda r: r[0]):
        who.add(row, flag)
    for _, row, flag in sorted(problem_rows, key=lambda r: r[0]):
        problems.add(row, flag)
    return who, problems, errors, by_group


def _groups(groups: dict, by_group: dict, names: dict) -> Section:
    section = Section(
        GROUPS,
        ["Group", "Licences it gives", "People licensed through it", "Errors", "Members", "Membership"],
        intro="Groups that hand out licences to their members (group-based licensing), how many "
        "people have a licence through each and how many assignments failed.",
        empty="No group assigns licences: every licence is assigned directly.",
    )
    ids = [gid for gid, g in groups.items() if g.get("licenceSkuIds")] + [g for g in by_group if g not in groups]
    rows = []
    for gid in dict.fromkeys(ids):
        g = groups.get(gid) or {}
        entry = by_group.get(gid) or {"people": set(), "errors": 0, "skus": set()}
        skus = [s for s in g.get("licenceSkuIds") or [] if isinstance(s, str)] or sorted(entry["skus"])
        membership = (
            "Dynamic" if g.get("dynamic") else ("Synced from AD" if g.get("synced") else ("Assigned" if g else ""))
        )
        members = g.get("memberCount") if g.get("examined") and isinstance(g.get("memberCount"), int) else ""
        rows.append(
            (
                (not entry["errors"], (g.get("displayName") or gid).lower()),
                [
                    g.get("displayName") or gid,
                    [names.get(s, "Unknown licence") for s in skus],
                    len(entry["people"]),
                    entry["errors"],
                    members,
                    membership,
                ],
                "medium" if entry["errors"] else None,
            )
        )
    for _, row, flag in sorted(rows, key=lambda r: r[0]):
        section.add(row, flag)
    return section
