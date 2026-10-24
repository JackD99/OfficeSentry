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
from ..snapshot import TenantSnapshot
from ..usage import (
    ACTIVE_DAYS,
    APPS,
    COLLECTOR,
    COPILOT,
    COPILOT_APPS,
    PLATFORMS,
    SERVICES,
    blocked,
    concealed,
    part_for_product,
)
from ..usage import licensed as is_licensed
from .base import Collector, RunContext, register
from .connection import awaiting

PERIOD = f"D{ACTIVE_DAYS}"
JSON = {"$format": "application/json"}  # v1.0 only returns CSV; beta returns JSON
ACTIVE_USERS = f"/reports/getOffice365ActiveUserDetail(period='{PERIOD}')"
APP_USERS = f"/reports/getM365AppUserDetail(period='{PERIOD}')"
ACTIVATIONS = "/reports/getOffice365ActivationsUserDetail"
EMAIL = f"/reports/getEmailActivityUserDetail(period='{PERIOD}')"
TEAMS = f"/reports/getTeamsUserActivityUserDetail(period='{PERIOD}')"
# Which mail apps each person connected with, and the Outlook versions.
MAIL_APPS = f"/reports/getEmailAppUsageUserDetail(period='{PERIOD}')"
# Copilot's report doesn't accept D30; D90 still gives each person's last activity date.
COPILOT_USAGE = "/copilot/reports/getMicrosoft365CopilotUsageUserDetail(period='D90')"
# How many people with Copilot used it each day, per app, for Microsoft's last 90 days.
COPILOT_TREND = "/copilot/reports/getMicrosoft365CopilotUserCountTrend(period='D90')"

REPORT_SETTINGS = "ReportSettings.Read.All"  # added in 2.2: says outright whether names are concealed

# Office365ActiveUserDetail field prefix per service key.
FIELDS = {"exchange": "exchange", "teams": "teams", "onedrive": "oneDrive", "sharepoint": "sharePoint"}
# EmailAppUsageUserDetail field per mail app key (``usage.MAIL_APPS``).
MAIL_FIELDS = {
    "windows": "outlookForWindows",
    "mac": "outlookForMac",
    "mobile": "outlookForMobile",
    "web": "outlookForWeb",
    "macMail": "mailForMac",
    "otherMobile": "otherForMobile",
    "pop": "pop3App",
    "imap": "imap4App",
    "smtp": "smtpApp",
}


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
        "mailApps": None,
        "copilotLastActive": None,
        "copilotActive": None,
        # Only people with a Copilot licence are in Copilot's report; for them, the last day they used it in
        # each app (``usage.COPILOT_APPS`` keys).
        "copilotLicensed": False,
        "copilotApps": None,
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


def _versions(value) -> list[str]:
    """An email apps report cell: the versions seen ("Outlook 2016", or "Undetermined"), or none when unused."""
    if isinstance(value, str):
        value = value.split(";")
    if not isinstance(value, list):
        return []
    return sorted({v.strip() for v in value if isinstance(v, str) and v.strip()})


def merge_mail_apps(rows: dict[str, dict], items: list[dict]) -> None:
    """Which mail apps each person connected with, from EmailAppUsageUserDetail."""
    for r in items:
        row = _row(rows, r)
        if row is not None:
            row["mailApps"] = {key: _versions(r.get(field)) for key, field in MAIL_FIELDS.items()}


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


def merge_copilot(rows: dict[str, dict], items: list[dict]) -> str | None:
    """Each Copilot holder's last use, overall and per app. Returns the date Microsoft last refreshed the report."""
    refreshed = None
    for r in items:
        refreshed = max(refreshed or "", _day(r.get("reportRefreshDate")) or "") or None
        row = _row(rows, r)
        if row is not None:
            row["copilotLastActive"] = _day(r.get("lastActivityDate"))
            row["copilotActive"] = _recent(row["copilotLastActive"], r.get("reportRefreshDate"))
            row["copilotLicensed"] = True
            row["copilotApps"] = {key: _day(r.get(f"{prefix}LastActivityDate")) for key, _, prefix, _ in COPILOT_APPS}
    return refreshed


def copilot_days(items: list[dict]) -> list[tuple[str, dict]]:
    """One object per day from Copilot's daily counts: people with Copilot, how many used it, and in each app."""
    days: dict[str, dict] = {}
    for item in items:
        for d in (item.get("adoptionByDate") if isinstance(item, dict) else None) or []:
            day = _day(d.get("reportDate")) if isinstance(d, dict) else None
            if day:
                days[day] = {
                    "date": day,
                    "enabled": _int(d.get("anyAppEnabledUsers")),
                    "active": _int(d.get("anyAppActiveUsers")),
                    "apps": {key: _int(d.get(f"{prefix}ActiveUsers")) for key, _, _, prefix in COPILOT_APPS},
                }
    return sorted(days.items())


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
    permissions = ("Reports.Read.All", REPORT_SETTINGS)

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
            ("mail_apps", MAIL_APPS, merge_mail_apps),
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
        copilot_date = None
        if why := clouds.missing(cloud, "usage.copilot"):
            read["copilot"] = False
            ctx.log(why)  # nobody there can hold Copilot, so it isn't worth a warning
        else:
            try:
                copilot_date = merge_copilot(rows, ctx.paged(ctx.graph.get_all(COPILOT_USAGE, JSON, version="beta")))
                read["copilot"] = True
                if any(r["copilotLicensed"] for r in rows.values()):
                    read["copilot_trend"] = self._copilot_trend(ctx)
            except GraphError as exc:
                read["copilot"] = False
                if _has_copilot(rows):  # only matters when someone holds Copilot
                    ctx.warn(f"Copilot usage report unavailable: {exc.message}")
                else:
                    ctx.log(f"Copilot usage report unavailable: {exc.message}")

        setting = self._conceal_setting(ctx)
        hidden = setting is True or any(r["concealed"] for r in rows.values())
        report_date = max((r["reportDate"] for r in rows.values() if r["reportDate"]), default=None)
        ctx.objects(
            "usage_status",
            [
                (
                    "status",
                    {
                        "available": True,
                        "concealed": hidden,
                        # The tenant's "Display concealed user, group, and site names" setting (None: not read).
                        "concealSetting": setting,
                        "reportDate": report_date,
                        "copilotReportDate": copilot_date,
                        "days": ACTIVE_DAYS,
                        "read": read,
                    },
                )
            ],
        )
        if hidden:
            ctx.log("Microsoft is concealing names in usage reports; people show as codes", "warning")
        ctx.objects("user_usage", list(rows.items()))

        # As the usage report counts them: accounts blocked from sign-in can't use anything, so they're left out.
        disabled = blocked(TenantSnapshot(ctx.conn, ctx.tenant))
        licensed = [r for upn, r in rows.items() if is_licensed(r) and upn not in disabled]
        ctx.metric("usage.people", len(licensed))
        ctx.metric("usage.active", sum(any(r["active"].values()) for r in licensed))
        for key, _ in SERVICES:
            ctx.metric(f"usage.active.{key}", sum(r["active"][key] for r in rows.values()))
        if read["copilot"]:
            holders = [r for upn, r in rows.items() if r["copilotLicensed"] and upn not in disabled]
            ctx.metric("copilot.licensed", len(holders))
            ctx.metric("copilot.active", sum(bool(r["copilotActive"]) for r in holders))
        if read["apps"]:
            ctx.metric(
                "usage.desktop_apps",
                sum(
                    bool((r["platforms"] or {}).get("windows") or (r["platforms"] or {}).get("mac"))
                    for r in rows.values()
                ),
            )

    @staticmethod
    def _copilot_trend(ctx: RunContext) -> bool:
        """Copilot's daily counts, for the Copilot report's chart. Only asked for when someone holds Copilot."""
        try:
            days = copilot_days(ctx.paged(ctx.graph.get_all(COPILOT_TREND, JSON, version="beta")))
        except GraphError as exc:
            ctx.log(f"Copilot's daily counts weren't readable: {exc.message}", "warning")
            return False
        ctx.objects("copilot_day", days)
        return True

    @staticmethod
    def _conceal_setting(ctx: RunContext) -> bool | None:
        """Whether the tenant conceals names in usage reports, from its report settings. None while the client
        hasn't approved the permission or the settings can't be read: the codes in the reports still tell."""
        if awaiting(ctx, REPORT_SETTINGS):
            return None
        try:
            return bool(ctx.graph.get("/admin/reportSettings").get("displayConcealedNames"))
        except GraphError as exc:
            ctx.log(f"The usage report settings weren't readable: {exc.message}")
            return None
