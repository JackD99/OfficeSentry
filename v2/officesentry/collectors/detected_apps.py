"""Installed apps: every app version Intune has found on the tenant's managed devices.

Reads ``/deviceManagement/detectedApps`` (needs Intune and DeviceManagementManagedDevices.Read.All, which
the devices collector already uses). Each row is one version of one app with how many devices have it;
Office Sentry doesn't ask which devices, which would be a request per app.
"""

from __future__ import annotations

from ..graph import GraphError, LicenseRequired, PermissionMissing
from ..installed_apps import remote_tool
from .base import Collector, RunContext, register

MAX_PAGES = 200


def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def detected(app: dict) -> dict:
    count = app.get("deviceCount")
    size = app.get("sizeInByte")
    return {
        "name": _text(app.get("displayName")),
        "version": _text(app.get("version")),
        "publisher": _text(app.get("publisher")),
        "platform": _text(app.get("platform")),
        "devices": count if isinstance(count, int) and not isinstance(count, bool) else None,
        "size": size if isinstance(size, int) and not isinstance(size, bool) else None,
    }


@register
class DetectedApps(Collector):
    key = "detected_apps"
    title = "Installed apps"
    description = "Every app and version Intune has found on managed devices, with how many devices have each."
    permissions = ("DeviceManagementManagedDevices.Read.All",)
    needs = ("intune",)

    def collect(self, ctx: RunContext) -> None:
        if ctx.capabilities.get("intune") is False:
            ctx.log("Tenant has no Intune licence; no installed apps to read")
            ctx.objects("detected_app_status", [("status", {"state": "unavailable"})])
            return
        try:
            result = ctx.graph.get_all("/deviceManagement/detectedApps", max_pages=MAX_PAGES)
        except (LicenseRequired, PermissionMissing) as exc:
            ctx.log(f"Installed apps aren't available (Intune isn't set up): {exc.message}")
            ctx.objects("detected_app_status", [("status", {"state": "unavailable"})])
            return
        except GraphError as exc:
            ctx.warn(f"Installed apps weren't readable: {exc.message}")
            ctx.objects("detected_app_status", [("status", {"state": "failed"})])
            return
        apps = [
            (a["id"], detected(a)) for a in ctx.paged(result) if isinstance(a, dict) and isinstance(a.get("id"), str)
        ]
        apps = [(i, a) for i, a in apps if a["name"]]
        ctx.objects("detected_app", apps)
        ctx.objects("detected_app_status", [("status", {"state": "read", "truncated": result.truncated})])
        ctx.metric("apps.detected", len({(a["name"].lower(), a["platform"]) for _, a in apps}))
        ctx.metric("apps.remote_access", len({remote_tool(a["name"]) for _, a in apps} - {None}))
