"""Checks over subscriptions and how licences are assigned."""

from __future__ import annotations

from datetime import datetime

from ..devices import parse_ts
from ..licences import assignment_error, assignments, sku_name
from ..snapshot import TenantSnapshot
from ..text import plural
from .base import Finding, _label, _unavailable, rule

TRIAL_DAYS = 30


@rule(
    "licence_assignment_errors",
    "Every licence assignment works",
    "Licences",
    "Fix each failed licence from the group's Licenses page in the Entra admin center: buy more licences when "
    "there aren't enough, remove licences that conflict, and correct usage locations. The Licences and "
    "subscriptions report explains each error.",
)
def licence_assignment_errors(snap: TenantSnapshot, now: datetime):
    users = snap.objects("users", "user")
    if users is None:
        return _unavailable("Users haven't been collected yet")
    held = [(u, assignments(u)) for u in users]
    if all(a is None for _, a in held):
        return _unavailable("How licences are assigned hasn't been collected yet")
    skus = {s.get("skuId"): s.get("skuPartNumber") for s in snap.objects("tenant_profile", "subscribed_sku") or []}
    groups = {g["_id"]: g.get("displayName") for g in snap.objects("groups", "group") or []}
    findings = []
    for u, mine in held:
        for sid, a in (mine or {}).items():
            for e in a.errors:
                meaning, _ = assignment_error(e.code)
                source = f" from {groups.get(e.group) or 'a group'}" if e.group else ""
                outcome = "only partly works" if e.active else "wasn't assigned"
                findings.append(
                    Finding(
                        "low" if e.active else "medium",
                        _label(u),
                        f"{sku_name(skus.get(sid)) if skus.get(sid) else 'A licence'}{source} {outcome}. {meaning}.",
                        u["_id"],
                    )
                )
    if not findings:
        return ("pass", "Every licence assignment worked", [])
    people = len({f.object_id for f in findings})
    return ("fail", f"{plural(len(findings), 'licence assignment')} failed for {plural(people, 'account')}", findings)


@rule(
    "subscriptions_lapsing",
    "No subscriptions are about to lapse",
    "Licences",
    "Renew subscriptions in their grace period, buy or cancel trials people use before they end, and move people "
    "off suspended subscriptions. Billing > Your products in the Microsoft 365 admin center (or Partner Center for "
    "subscriptions you sell) shows each one.",
)
def subscriptions_lapsing(snap: TenantSnapshot, now: datetime):
    skus = snap.objects("tenant_profile", "subscribed_sku")
    if skus is None:
        return _unavailable("Licences haven't been collected yet")
    status = (snap.objects("tenant_profile", "subscription_status") or [None])[0]
    if status is None:
        return _unavailable("Subscription renewal dates haven't been collected yet")
    if not status.get("available"):
        return _unavailable("Microsoft didn't return the tenant's subscriptions")
    by_sku = {s.get("skuId"): s for s in skus}
    subs = snap.objects("tenant_profile", "company_subscription") or []
    findings = []
    for s in subs:
        sku = by_sku.get(s.get("skuId")) or {}
        name = sku_name(s.get("skuPartNumber") or sku.get("skuPartNumber"))
        units = sku.get("prepaidUnits") if isinstance(sku.get("prepaidUnits"), dict) else {}
        used = sku.get("consumedUnits") if isinstance(sku.get("consumedUnits"), int) else 0
        working = sum(v for v in (units.get("enabled"), units.get("warning")) if isinstance(v, int))
        when = parse_ts(s.get("nextLifecycleDateTime"))
        on = f" on {when.day} {when:%b %Y}" if when else ""
        total = s.get("totalLicenses") if isinstance(s.get("totalLicenses"), int) else 0
        state = s.get("status")
        if state == "Warning":
            findings.append(
                Finding(
                    "high",
                    name,
                    f"Expired and in its grace period: {plural(total, 'licence')} stop working{on} unless it's renewed",
                    s["_id"],
                )
            )
        elif state in ("Suspended", "LockedOut") and used > working:
            findings.append(
                Finding(
                    "high",
                    name,
                    f"{'Suspended' if state == 'Suspended' else 'Locked out'}, yet "
                    f"assigned to {plural(used - working, 'account')} that can't use it",
                    s["_id"],
                )
            )
        elif state == "Enabled" and s.get("isTrial") is True and used and when and (when - now).days <= TRIAL_DAYS:
            findings.append(
                Finding(
                    "medium",
                    name,
                    f"Trial ends{on} and {plural(used, 'account')} {'uses' if used == 1 else 'use'} it",
                    s["_id"],
                )
            )
    if findings:
        return ("fail", f"{plural(len(findings), 'subscription')} about to lapse or already lapsed", findings)
    return ("pass", f"{plural(len(subs), 'subscription')} checked; none is lapsing" if subs else "No subscriptions", [])
