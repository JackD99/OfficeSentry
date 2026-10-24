"""Licences: what the account holds, how it got each one, and what they cost."""

from __future__ import annotations

from ...licences import FREE_SKUS, Assignment, assignment_error, assignments, is_stale, load_prices, sku_name
from ...text import plural as _plural
from ..data_reports import Section
from ..identity_reports import Money
from .base import INACTIVE_DAYS, Part, Person, _currency, part


def _licences(p: Person) -> tuple[list[tuple[str, str, float | None]], dict]:
    """(sku ID, name, monthly price) for each licence held, and the tenant's SKUs by ID."""
    skus = {s["skuId"]: s for s in p.snap.objects("tenant_profile", "subscribed_sku") or [] if s.get("skuId")}
    prices = load_prices(p.snap.conn, p.snap.tenant)
    out = []
    for sid in dict.fromkeys(p.user.get("skuIds") or []):
        part_number = (skus.get(sid) or {}).get("skuPartNumber")
        out.append(
            (
                sid,
                sku_name(part_number) if part_number else "Unknown licence",
                prices.get(part_number) if part_number not in FREE_SKUS else 0.0,
            )
        )
    return out, skus


def _assigned(a: Assignment | None, groups: dict) -> str:
    """'Directly', 'By group: Sales' or 'Directly and by group: Sales'."""
    if a is None:
        return ""
    return a.path + (f": {', '.join(groups.get(g) or g for g in a.groups)}" if a.groups else "")


@part(10)
def licences_part(p: Person) -> Part:
    held, skus = _licences(p)
    currency = _currency(p.snap)
    result = Part("licences", "Licences")
    paths = assignments(p.user)  # None when how licences are assigned wasn't collected
    failed = {sid: a for sid, a in (paths or {}).items() if a.errors and not a.active}
    if "usageLocation" in p.user and (held or failed):
        result.facts = [("Usage location", p.user.get("usageLocation") or "Not set")]
        if not p.user.get("usageLocation"):
            p.flag(
                "low",
                "No usage location is set, so licences can only come from groups (which use the "
                "organisation's country). Set one before assigning a licence directly.",
                "licences",
            )
    if not held and not failed:
        result.summary, result.empty = "None", "Holds no licences."
        return result
    groups = {g["_id"]: g.get("displayName") for g in p.snap.objects("groups", "group") or []} if paths else {}
    problems = any(a.errors for a in (paths or {}).values())
    table = Section(
        "Licences",
        ["Licence"]
        + (["Assigned"] if paths is not None else [])
        + ["Cost / month"]
        + (["Problem"] if problems else []),
    )
    names = {sid: name for sid, name, _ in held}
    for sid in failed:
        part_number = (skus.get(sid) or {}).get("skuPartNumber")
        names.setdefault(sid, sku_name(part_number) if part_number else "Unknown licence")
    prices = {sid: price for sid, _, price in held}
    for sid, name in names.items():
        a = (paths or {}).get(sid)
        problem = "; ".join(
            assignment_error(e.code)[0] + (f" ({groups.get(e.group) or 'a group'})" if e.group else "")
            for e in (a.errors if a else [])
        )
        price = prices.get(sid)
        cost = "Not assigned" if sid in failed else ("Not priced" if price is None else Money(price, currency))
        table.add(
            [name] + ([_assigned(a, groups)] if paths is not None else []) + [cost] + ([problem] if problems else []),
            "medium" if sid in failed else ("low" if problem else None),
        )
        for e in a.errors if a else []:
            meaning, todo = assignment_error(e.code)
            source = f" from {groups.get(e.group) or 'a group'}" if e.group else ""
            p.flag(
                "low" if e.active else "medium",
                f"{name}{source} {'only partly works' if e.active else 'was not assigned'}. {meaning}. {todo}.",
                "licences",
            )
        if sid not in failed and (skus.get(sid) or {}).get("capabilityStatus") in ("Suspended", "LockedOut"):
            p.flag(
                "medium",
                f"{name} comes from a suspended subscription, so it doesn't work. Reactivate the "
                "subscription or remove the licence.",
                "licences",
            )
        if a and a.direct and a.groups:
            p.flag(
                "low",
                f"{name} is assigned directly and by {', '.join(groups.get(g) or g for g in a.groups)}, "
                "so taking them out of the group won't remove it.",
                "licences",
            )
    if failed:
        result.tone = "warn"
    priced = [price for _, _, price in held if price is not None]
    total = sum(priced)
    result.tables = [table]
    result.summary = _plural(len(held), "licence") + (f", {Money(total, currency)} a month" if total else "")
    if len(priced) < len(held):
        result.note = "Some licences have no price yet. An admin can add them on the Licence prices page."

    paid = [(sid, name) for sid, name, price in held if price != 0.0]
    cost = f" ({Money(total, currency)} a month)" if total else ""
    if paid and not p.enabled:
        p.flag(
            "medium",
            f"Sign-in is blocked but the account still holds {_plural(len(paid), 'paid licence')}"
            f"{cost}. Remove them or convert the mailbox to shared.",
            "licences",
        )
    elif paid and is_stale(p.user, p.now, INACTIVE_DAYS):
        p.flag(
            "medium",
            f"No sign-in for {INACTIVE_DAYS} days or more but holds {_plural(len(paid), 'paid licence')}{cost}.",
            "licences",
        )
    plans = {
        sid: {sp.get("servicePlanId") for sp in s.get("servicePlans") or [] if sp.get("servicePlanId")}
        for sid, s in skus.items()
    }
    names = {sid: name for sid, name, _ in held}
    for a in names:
        for b in names:
            if a == b or not plans.get(a) or not plans[a] <= plans.get(b, set()) or (plans[a] == plans[b] and a > b):
                continue
            p.flag(
                "low", f"{names[a]} is already included in {names[b]}, so one of them is probably wasted.", "licences"
            )
    return result
