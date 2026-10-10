"""Unused apps: every enterprise app from outside Microsoft, when it last signed in, and what it can still reach.

For a technician tidying up a tenant or reviewing app access: apps nobody has used in 90 days that still hold
application permissions or consent are the ones to remove first. Built from what Apps and consents stores
(service principals, application permissions, delegated consent) and the last sign-ins App sign-ins reads
from Entra ID's service principal sign-in activity report. Nothing here calls Microsoft.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..devices import parse_ts
from ..snapshot import TenantSnapshot
from ..text import plural
from .data_reports import DataReport, Rows, Section, Tile

UNUSED_DAYS = 90
APPS = "Enterprise apps"
RECENT = "Last 30 days"
QUIET = "30 to 90 days ago"
OVER = f"Over {UNUSED_DAYS} days ago"
NONE = "No sign-in on record"
UNUSED = (OVER, NONE)
USES = (("delegatedClient", "To sign people in"), ("appClient", "On its own"),
        ("delegatedResource", "As an API"), ("appResource", "As an API"))

NOT_COLLECTED = "App sign-ins haven't been collected yet. Refresh App sign-ins on the tenant page."
NO_APPS = "Apps haven't been collected yet. Refresh Apps and consents on the tenant page."
NO_LICENCE = ("Microsoft only keeps app sign-in activity for tenants with Entra ID P1 (included in Microsoft 365 "
              "Business Premium, E3 and E5). This tenant doesn't have it, so when each app last signed in isn't "
              "known. The Apps and consents report still lists every app and what it can reach.")
NO_PERMISSION = ("Office Sentry can't read this tenant's app sign-in activity. It needs the AuditLog.Read.All "
                 "permission: open the tenant page, send the consent link to the client's Global Admin again and "
                 "ask them to accept.")


def _activity(last: datetime | None, now: datetime) -> str:
    if last is None:
        return NONE
    if last >= now - timedelta(days=30):
        return RECENT
    return QUIET if last >= now - timedelta(days=UNUSED_DAYS) else OVER


def _made_by(sp: dict) -> str:
    if sp.get("ownTenant") is True:
        return "This organisation"
    verified = sp.get("verifiedPublisher")
    if isinstance(verified, str) and verified:
        return f"{verified} (verified)"
    publisher = sp.get("publisherName")
    return f"{publisher} (unverified)" if isinstance(publisher, str) and publisher else "Unknown publisher"


def _consent(grants: list[dict]) -> str:
    if any(g.get("consentType") == "AllPrincipals" for g in grants):
        return "Everyone"
    people = {g.get("principalId") for g in grants if g.get("principalId")}
    return plural(len(people), "person", "people") if people else ""


def build(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    status = (snap.objects("app_sign_ins", "sign_in_status") or [None])[0]
    principals = snap.objects("app_consents", "service_principal")
    if status is None:
        report.unavailable = NOT_COLLECTED
        return
    if not status.get("available"):
        report.unavailable = NO_PERMISSION if status.get("reason") == "permission" else NO_LICENCE
        return
    if principals is None:
        report.unavailable = NO_APPS
        return

    activity = {a["_id"].lower(): a for a in snap.objects("app_sign_ins", "app_activity") or []
                if isinstance(a.get("_id"), str)}
    permissions: dict[str, list[str]] = {}
    for p in snap.objects("app_consents", "app_permission") or []:
        if isinstance(p.get("permission"), str):
            permissions.setdefault(p.get("clientId"), []).append(p["permission"])
    grants: dict[str, list[dict]] = {}
    for g in snap.objects("app_consents", "delegated_grant") or []:
        grants.setdefault(g.get("clientId"), []).append(g)

    section = Section(APPS, ["App", "Made by", "Last sign-in", "Activity", "Used", "Application permissions",
                             "Consented for", "Enabled"],
                      intro=f"Apps from outside Microsoft in this tenant and when each last signed in. Apps unused "
                            f"for {UNUSED_DAYS} days that still hold permissions are flagged: remove them, or ask "
                            "whoever added them whether they're still needed.",
                      empty="No apps from outside Microsoft.")
    rows = []
    for sp in principals:
        app_id = sp.get("appId") if isinstance(sp.get("appId"), str) else ""
        if sp.get("microsoft") is True or sp.get("type") not in ("Application", None) or \
                app_id.lower() in snap.own_app_ids:
            continue
        seen = activity.get(app_id.lower()) or {}
        times = [t for t in (parse_ts(seen.get(k)) for k in ("last", *(k for k, _ in USES))) if t]
        last = max(times) if times else None
        state = _activity(last, now)
        uses = list(dict.fromkeys(label for key, label in USES if parse_ts(seen.get(key))))
        held = sorted(set(permissions.get(sp.get("_id"), [])))
        consent = _consent(grants.get(sp.get("_id"), []))
        enabled = sp.get("accountEnabled") is not False
        flag = None
        if enabled and state in UNUSED:
            flag = "medium" if held or consent else "low"
        name = sp.get("displayName") or app_id or sp.get("_id")
        rows.append(((not flag, flag != "medium", UNUSED.index(state) if state in UNUSED else 2,
                      str(name).lower()),
                     [name, _made_by(sp), last.date().isoformat() if last else "", state, uses, held, consent,
                      enabled], flag))
    for _, row, flag in sorted(rows, key=lambda r: r[0]):
        section.add(row, flag)
    report.sections = [section]

    live = [r for _, r, _ in rows if r[-1] is True]
    unused = sum(r[3] in UNUSED for r in live)
    holding = sum(f == "medium" for _, _, f in rows)
    never = sum(r[3] == NONE for r in live)
    report.tiles = [
        Tile(f"Unused for {UNUSED_DAYS}+ days", unused, f"Of {plural(len(live), 'enabled app')}",
             "warn" if unused else "ok", Rows(APPS, {"Activity": list(UNUSED), "Enabled": "Yes"})),
        Tile("Unused but still has access", holding, "Holds permissions or consent nobody is using",
             "bad" if holding else "ok", Rows(APPS, flagged=True)),
        Tile("No sign-in on record", never, "Enabled, never seen signing in", rows=Rows(APPS, {"Activity": NONE,
                                                                                             "Enabled": "Yes"})),
        Tile("Used in the last 30 days", sum(r[3] == RECENT for r in live), "Enabled apps",
             rows=Rows(APPS, {"Activity": RECENT, "Enabled": "Yes"})),
    ]
    report.footnote = (
        "From Entra ID's app sign-in activity, which keeps the latest time each app signed people in, signed in on "
        "its own or was called as an API. \"No sign-in on record\" means Microsoft has none: the app may never "
        "have been used, or not for a long time. Microsoft's own apps and Office Sentry's are left out. Remove an "
        "app in the Microsoft Entra admin center (Enterprise applications); Office Sentry only reads them.")


REPORTS = {
    "unused-apps": ("Unused apps",
                    f"Every app from outside Microsoft with when it last signed in, and those unused for {UNUSED_DAYS} "
                    "days that still hold permissions.",
                    ("app_sign_ins", "app_consents"), build),
}
