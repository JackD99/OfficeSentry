"""Apps the person has consented to."""

from __future__ import annotations

from ...checks.apps import SENSITIVE_DELEGATED
from ...text import plural as _plural
from ..data_reports import Section
from .base import Part, Person, part


@part(80)
def apps_part(p: Person) -> Part:
    result = Part("apps", "Apps this person allowed")
    grants = p.snap.objects("app_consents", "delegated_grant")
    if grants is None:
        result.unavailable = "Apps haven't been collected yet. Refresh Apps and consents on the tenant page."
        return result
    sps = {s["_id"]: s for s in p.snap.objects("app_consents", "service_principal") or []}
    own = p.snap.own_app_ids
    mine = [g for g in grants if g.get("consentType") == "Principal" and g.get("principalId") == p.id]
    if not mine:
        result.summary, result.empty = "None", "Hasn't consented to any app on their own behalf."
        return result
    by_app: dict[str, dict] = {}
    for g in mine:
        sp = sps.get(g.get("clientId")) or {}
        a = by_app.setdefault(g.get("clientId") or "?", {"sp": sp, "scopes": set()})
        a["scopes"].update(s for s in g.get("scopes") or [] if s)
    table = Section("Apps this person allowed", ["App", "Publisher", "Permissions", "From"])
    for a in sorted(by_app.values(), key=lambda a: (a["sp"].get("displayName") or "").lower()):
        sp = a["sp"]
        name = sp.get("displayName") or "Unknown app"
        third = not sp.get("microsoft") and (sp.get("appId") or "").lower() not in own and not sp.get("ownTenant")
        risky = sorted(a["scopes"] & SENSITIVE_DELEGATED)
        publisher = sp.get("verifiedPublisher") or sp.get("publisherName") or ""
        if third:
            publisher += " (verified)" if sp.get("verifiedPublisher") else (" (unverified)" if publisher
                                                                            else "Unverified")
        table.add([name, publisher or "Microsoft", sorted(a["scopes"]),
                   "Third party" if third else ("Microsoft" if sp.get("microsoft") else "This organisation")],
                  ("medium" if risky else "low") if third else None)
        if third and risky:
            p.flag("medium", f"Gave {name} access to {', '.join(risky)}. Check it's an app the business uses.",
                   "apps")
    result.tables = [table]
    result.summary = _plural(len(by_app), "app")
    return result
