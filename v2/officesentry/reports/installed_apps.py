"""Installed apps: every app Intune has found on the tenant's managed devices, remote access tools first.

Reads what ``collectors/detected_apps.py`` stores: one row per version of an app, with how many devices have
that version. A remote access tool someone didn't expect is how many ransomware attacks start, and an app on
many versions is one that isn't being kept up to date.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .. import installed_apps as ia
from ..snapshot import TenantSnapshot
from .data_reports import DataReport, Rows, Section, Tile, metric_trend
from .registry import report

NOT_COLLECTED = 'Installed apps haven\'t been collected yet. Refresh "Installed apps" on the client page.'
NO_INTUNE = "Installed apps come from Intune, which this tenant doesn't have (or hasn't set up)."
REMOTE = "Remote access tools"
APPS = "Apps"
VERSIONS = "Every version"
SPREAD = 3  # an app on this many versions or more is pointed out


@report(
    "installed-apps",
    "Installed apps",
    "Every app Intune has found on managed devices and how many devices have it, remote access tools first, "
    "and each app's versions.",
    ("detected_apps",),
    group="devices",
    order=12,
    keywords="software inventory detected apps installed programs applications versions remote access tools "
    "anydesk teamviewer screenconnect rmm intune",
)
def installed_apps_report(snap: TenantSnapshot, now: datetime, out: DataReport) -> None:
    status = ia.status(snap)
    rows = ia.apps(snap)
    if status is None or rows is None:
        out.unavailable = NOT_COLLECTED
        return
    if status.get("state") == "unavailable":
        out.unavailable = NO_INTUNE
        return
    if status.get("state") != "read":
        out.unavailable = "Installed apps couldn't be read; see the Installed apps collection's log."
        return

    # One app is its name on one platform; Intune lists each version separately.
    by_app: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        by_app[(r["name"].lower(), ia.platform(r.get("platform")))].append(r)

    def versions(group: list[dict]) -> list[str]:
        found = {r.get("version") or "" for r in group} - {""}
        return sorted(found, key=ia.version_key, reverse=True)

    def installs(group: list[dict]) -> int:
        return sum(r.get("devices") or 0 for r in group)

    remote = Section(
        REMOTE,
        ["App", "Tool", "Publisher", "Platform", "Versions", "Devices"],
        intro="Apps that let someone control a computer from elsewhere. Check each is one you or the client use: "
        "attackers install these too, and an unexpected one is often how ransomware gets in.",
        empty="No remote access tools were found.",
    )
    apps = Section(
        APPS,
        ["App", "Publisher", "Platform", "Versions", "Newest version", "Devices"],
        intro=f"Each app once, most installed first. Devices adds up every version, so a device with two versions "
        f"of an app counts twice. Apps on {SPREAD} or more versions are worth updating everywhere.",
        empty="Intune hasn't found any apps.",
        optional=("Publisher",),
    )
    ordered = sorted(by_app.items(), key=lambda kv: (-installs(kv[1]), kv[0]))
    for (_, shown_platform), group in ordered:
        name = group[0]["name"]
        found = versions(group)
        publisher = next((r["publisher"] for r in group if r.get("publisher")), "")
        if tool := ia.remote_tool(name):
            remote.add([name, tool, publisher, shown_platform, ", ".join(found), installs(group)], "low")
        apps.add([name, publisher, shown_platform, len(found), found[0] if found else "", installs(group)])

    every = Section(
        VERSIONS,
        ["App", "Version", "Publisher", "Platform", "Devices"],
        intro="Each version Intune found, newest first, for finding the devices still on an old one (in Intune: "
        "Apps > Monitor > Discovered apps).",
        compare=False,
        optional=("Publisher",),
    )
    for (_, shown_platform), group in sorted(by_app.items()):
        for r in sorted(group, key=lambda r: ia.version_key(r.get("version")), reverse=True):
            every.add([r["name"], r.get("version") or "", r.get("publisher") or "", shown_platform, r.get("devices")])
    out.sections = [remote, apps, every]

    tools = len({row[1] for row in remote.rows})
    out.tiles = [
        Tile(
            "Apps found",
            len(apps.rows),
            "Different apps on managed devices",
            rows=Rows(APPS),
            trend=metric_trend(snap, "apps.detected", now),
        ),
        Tile(
            "Remote access tools",
            tools,
            "Check each is expected" if tools else "None found",
            "warn" if tools else "ok",
            Rows(REMOTE),
        ),
    ]
    if status.get("truncated"):
        out.notes.append("Intune listed more apps than Office Sentry reads in one collection, so some are missing.")
    out.footnote = (
        "From Intune's discovered apps, which it refreshes as devices check in. Only devices enrolled in Intune "
        "are included."
    )
