"""Microsoft 365 usage: who actually uses email, Teams, OneDrive, SharePoint and the Office apps.

Reads Microsoft's per-person usage reports (Reports.Read.All, GET only) and
merges them into one row per person. The licence right-sizing and usage
reports, and any page about one account, read that row through ``usage.py``.

Microsoft's usage reports run a couple of days behind. When the tenant's
"Display concealed user, group, and site names" setting is on, names come back
as 32-character codes; the codes match across reports, so the merge still works.
"""

from __future__ import annotations

from datetime import timedelta

from .. import clouds
from ..devices import parse_day
from ..graph import GraphError, PermissionMissing
from ..usage import ACTIVE_DAYS, APPS, COLLECTOR, COPILOT, PLATFORMS, SERVICES, concealed, part_for_product
from ..usage import licensed as is_licensed
from .base import Collector, RunContext, register

PERIOD = f"D{ACTIVE_DAYS}"
JSON = {"$format": "application/json"}  # v1.0 only returns CSV; beta returns JSON
ACTIVE_USERS = f"/reports/getOffice365ActiveUserDetail(period='{PERIOD}')"
APP_USERS = f"/reports/getM365AppUserDetail(period='{PERIOD}')"
ACTIVATIONS = "/reports/getOffice365ActivationsUserDetail"
EMAIL = f"/reports/getEmailActivityUserDetail(period='{PERIOD}')"
TEAMS = f"/reports/getTeamsUserActivityUserDetail(period='{PERIOD}')"
# Copilot's report doesn't accept D30; D90 still gives each person's last activity date.
COPILOT_USAGE = "/copilot/reports/getMicrosoft365CopilotUsageUserDetail(period='D90')"

# Office365ActiveUserDetail field prefix per service key.
FIELDS = {"exchange": "exchange", "teams": "teams", "onedrive": "oneDrive", "sharepoint": "sharePoint"}


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _day(value) -> str | None:
    day = parse_day(value) if value else None
    return day.isoformat() if day else None


def _recent(last: str | None, report_date: str | None) -> bool:
    """Active within the period, counted back from the date Microsoft last refreshed the report."""
    last_day, ref = parse_day(last) if last else None, parse_day(report_date) if report_date else None
    if last_day is None:
        return False
    return ref is None or last_day > ref - timedelta(days=ACTIVE_DAYS)


def _new_row(upn: str, report_date: str | None) -> dict:
    return {
        "upn": upn,
        "name": None,
        "concealed": concealed(upn),
        "reportDate": report_date,
        "products": [],
        "licensed": {k: False for k in FIELDS},
        "lastActive": {k: None for k, _ in SERVICES},
        "active": {k: False for k, _ in SERVICES},
        "platforms": None,
        "apps": {},
        "activations": [],
        "email": None,
        "teamsCounts": None,
        "copilotLastActive": None,
        "copilotActive": None,
    }


def merge_active_users(items: list[dict]) -> dict[str, dict]:
    """One row per person from Office365ActiveUserDetail: licences and last activity per service."""
    rows: dict[str, dict] = {}
    for r in items:
        upn = r.get("userPrincipalName")
        if not upn or r.get("isDeleted"):
            continue
        row = _new_row(upn, r.get("reportRefreshDate"))
        row["name"] = None if row["concealed"] else r.get("displayName")
        row["products"] = [p for p in r.get("assignedProducts") or [] if p]
        for key, prefix in FIELDS.items():
            row["licensed"][key] = bool(r.get(f"has{prefix[0].upper()}{prefix[1:]}License"))
            last = _day(r.get(f"{prefix}LastActivityDate"))
            row["lastActive"][key] = last
            row["active"][key] = _recent(last, row["reportDate"])
        rows[upn.lower()] = row
    return rows


def _row(rows: dict[str, dict], r: dict) -> dict | None:
    """The person's row from the active users report; other reports only add detail to it."""
    upn = r.get("userPrincipalName")
    if not upn or r.get("isDeleted"):
        return None
    return rows.get(upn.lower())


def merge_apps(rows: dict[str, dict], items: list[dict]) -> None:
    """Office apps by platform from M365AppUserDetail: which apps each person used, and where."""
    for r in items:
        row = _row(rows, r)
        if row is None:
            continue
        details = next(
            (d for d in r.get("details") or [] if _int(d.get("reportPeriod")) == ACTIVE_DAYS),
            (r.get("details") or [{}])[0],
        )
        row["platforms"] = {k: bool(details.get(k)) for k, _ in PLATFORMS}
        row["apps"] = {app: [p for p, _ in PLATFORMS if details.get(f"{app}{p[0].upper()}{p[1:]}")] for app, _ in APPS}
        row["apps"] = {app: where for app, where in row["apps"].items() if where or details.get(app)}
        last = _day(r.get("lastActivityDate"))
        row["lastActive"]["office"] = last
        row["active"]["office"] = any(row["platforms"].values()) or _recent(last, r.get("reportRefreshDate"))


def merge_activations(rows: dict[str, dict], items: list[dict]) -> None:
    """Office installs per person: which product, on how many PCs, Macs and phones."""
    for r in items:
        row = _row(rows, r)
        if row is None:
            continue
        row["activations"] = [
            {
                "product": a.get("productType"),
                "windows": _int(a.get("windows")),
                "mac": _int(a.get("mac")),
                "mobile": _int(a.get("ios")) + _int(a.get("android")) + _int(a.get("windows10Mobile")),
                "shared": bool(a.get("activatedOnSharedComputer")),
                "lastActivated": _day(a.get("lastActivatedDate")),
            }
            for a in r.get("userActivationCounts") or []
        ]


def merge_email(rows: dict[str, dict], items: list[dict]) -> None:
    for r in items:
        row = _row(rows, r)
        if row is not None:
            row["email"] = {
                "sent": _int(r.get("sendCount")),
                "received": _int(r.get("receiveCount")),
                "read": _int(r.get("readCount")),
            }
            if not row["name"] and not row["concealed"]:
                row["name"] = r.get("displayName")


def merge_teams(rows: dict[str, dict], items: list[dict]) -> None:
    for r in items:
        if r.get("isExternal"):
            continue
        row = _row(rows, r)
        if row is not None:
            row["teamsCounts"] = {
                "messages": _int(r.get("teamChatMessageCount")) + _int(r.get("privateChatMessageCount")),
                "calls": _int(r.get("callCount")),
                "meetings": _int(r.get("meetingCount")),
            }


def merge_copilot(rows: dict[str, dict], items: list[dict]) -> None:
    for r in items:
        row = _row(rows, r)
        if row is not None:
            row["copilotLastActive"] = _day(r.get("lastActivityDate"))
            row["copilotActive"] = _recent(row["copilotLastActive"], r.get("reportRefreshDate"))


def _has_copilot(rows: dict[str, dict]) -> bool:
    return any(part_for_product(p) == COPILOT for row in rows.values() for p in row["products"])


@register
class Usage(Collector):
    key = COLLECTOR
    title = "Microsoft 365 usage"
    description = (
        f"Who used email, Teams, OneDrive, SharePoint and the Office apps in the last {ACTIVE_DAYS} "
        "days, from Microsoft's usage reports."
    )
    permissions = ("Reports.Read.All",)

    def collect(self, ctx: RunContext) -> None:
        try:
            rows = merge_active_users(ctx.paged(ctx.graph.get_all(ACTIVE_USERS, JSON, version="beta")))
        except PermissionMissing as exc:
            ctx.warn(f"Microsoft 365 usage reports aren't available: {exc.message}")
            ctx.objects("usage_status", [("status", {"available": False, "reason": "permission"})])
            return
        read = {}
        cloud = clouds.of(ctx.tenant)
        for name, path, merge in (
            ("apps", APP_USERS, merge_apps),
            ("activations", ACTIVATIONS, merge_activations),
            ("email", EMAIL, merge_email),
            ("teams", TEAMS, merge_teams),
        ):
            if why := clouds.missing(cloud, f"usage.{name}"):
                read[name] = False
                ctx.warn(why)
                continue
            try:
                merge(rows, ctx.paged(ctx.graph.get_all(path, JSON, version="beta")))
                read[name] = True
            except GraphError as exc:
                read[name] = False
                ctx.warn(f"Usage report unavailable ({name}): {exc.message}")
        if why := clouds.missing(cloud, "usage.copilot"):
            read["copilot"] = False
            ctx.log(why)  # nobody there can hold Copilot, so it isn't worth a warning
        else:
            try:
                merge_copilot(rows, ctx.paged(ctx.graph.get_all(COPILOT_USAGE, JSON, version="beta")))
                read["copilot"] = True
            except GraphError as exc:
                read["copilot"] = False
                if _has_copilot(rows):  # only matters when someone holds Copilot
                    ctx.warn(f"Copilot usage report unavailable: {exc.message}")
                else:
                    ctx.log(f"Copilot usage report unavailable: {exc.message}")

        hidden = any(r["concealed"] for r in rows.values())
        report_date = max((r["reportDate"] for r in rows.values() if r["reportDate"]), default=None)
        ctx.objects(
            "usage_status",
            [
                (
                    "status",
                    {
                        "available": True,
                        "concealed": hidden,
                        "reportDate": report_date,
                        "days": ACTIVE_DAYS,
                        "read": read,
                    },
                )
            ],
        )
        if hidden:
            ctx.log("Microsoft is concealing names in usage reports; people show as codes", "warning")
        ctx.objects("user_usage", list(rows.items()))

        licensed = [r for r in rows.values() if is_licensed(r)]
        ctx.metric("usage.people", len(licensed))
        ctx.metric("usage.active", sum(any(r["active"].values()) for r in licensed))
        for key, _ in SERVICES:
            ctx.metric(f"usage.active.{key}", sum(r["active"][key] for r in rows.values()))
        if read["apps"]:
            ctx.metric(
                "usage.desktop_apps",
                sum(
                    bool((r["platforms"] or {}).get("windows") or (r["platforms"] or {}).get("mac"))
                    for r in rows.values()
                ),
            )
