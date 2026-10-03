"""Recorded-style Graph payloads for SharePoint, OneDrive and shared files."""

import json
import re
import zlib
from datetime import UTC, datetime, timedelta

import responses

from officesentry.graph import GRAPH_ROOT

V1 = f"{GRAPH_ROOT}/v1.0"
BETA = f"{GRAPH_ROOT}/beta"


def days_ago(n: int) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).date().isoformat()


def _site(url, template, *, activity=5, storage=0, anyone=0, guest=0, company=0, deleted=False, owner="Owner"):
    return {"reportRefreshDate": days_ago(2), "siteId": f"11111111-1111-1111-1111-{zlib.crc32(url.encode()):012d}",
            "siteUrl": url, "ownerDisplayName": owner, "ownerPrincipalName": f"{owner.lower()}@x.com",
            "isDeleted": deleted, "lastActivityDate": days_ago(activity) if activity is not None else None,
            "fileCount": 10, "activeFileCount": 2, "pageViewCount": 4, "visitedPageCount": 2,
            "storageUsedInBytes": storage, "storageAllocatedInBytes": 27487790694400,
            "externalSharing": bool(anyone or guest), "unmanagedDevicePolicy": "None", "siteSensitivityLabelId": "",
            "anonymousLinkCount": anyone, "companyLinkCount": company, "secureLinkForGuestCount": guest,
            "secureLinkForMemberCount": 0, "rootWebTemplate": template, "reportPeriod": "30"}


FINANCE = "https://x.sharepoint.com/sites/finance"
OLD = "https://x.sharepoint.com/sites/old-projects"
SITE_USAGE = [
    _site(FINANCE, "Group", storage=5 * 1024 ** 3, anyone=2, guest=1, company=1),
    _site(OLD, "Site Page Publishing", activity=200, storage=1024 ** 3),
    _site("https://x-admin.sharepoint.com", "Tenant Admin Site", activity=None, storage=2 * 1024 ** 2),
    _site("https://x.sharepoint.com/sites/gone", "Team Site", deleted=True),
]


def _drive(upn, storage, activity=3):
    return {"reportRefreshDate": days_ago(2), "siteId": "22222222-2222-2222-2222-222222222222",
            "siteUrl": f"https://x-my.sharepoint.com/personal/{upn.replace('@', '_').replace('.', '_')}",
            "ownerDisplayName": upn.split("@")[0].title(), "ownerPrincipalName": upn, "isDeleted": False,
            "lastActivityDate": days_ago(activity), "fileCount": 50, "activeFileCount": 5,
            "storageUsedInBytes": storage, "storageAllocatedInBytes": 1024 ** 4, "reportPeriod": "30"}


ONEDRIVE_USAGE = [_drive("ann@x.com", 3 * 1024 ** 3), _drive("bob@x.com", 2 * 1024 ** 3, activity=120),
                  _drive("gone@x.com", 1024 ** 3, activity=400)]


def _storage(site_type, start, step):
    return [{"reportRefreshDate": days_ago(2), "siteType": site_type, "reportDate": days_ago(2 + i),
             "storageUsedInBytes": start - i * step, "reportPeriod": "180"} for i in range(0, 180, 10)]


SITE_STORAGE = _storage("All", 6 * 1024 ** 3, 20 * 1024 ** 2)
ONEDRIVE_STORAGE = _storage("All", 6 * 1024 ** 3, 10 * 1024 ** 2) + _storage("OneDrive", 1, 0)

CONCEALED_SITE = dict(SITE_USAGE[0], siteUrl=None, siteId="00000000-0000-0000-0000-000000000000",
                      ownerDisplayName="82D28824CBDAF3EA9AD693254DE8CC08", ownerPrincipalName=None)

# -- Shared files ------------------------------------------------------------------

SITES = [
    {"id": "site-fin", "displayName": "Finance", "webUrl": FINANCE},
    {"id": "site-ann", "displayName": "Ann", "webUrl": "https://x-my.sharepoint.com/personal/ann_x_com",
     "isPersonalSite": True},
]
DRIVES = {
    "site-fin": [{"id": "drv-fin", "name": "Documents", "webUrl": FINANCE + "/Shared Documents",
                  "driveType": "documentLibrary"}],
    "site-ann": [{"id": "drv-ann", "name": "OneDrive", "webUrl": SITES[1]["webUrl"] + "/Documents",
                  "driveType": "business", "owner": {"user": {"email": "ann@x.com", "displayName": "Ann"}}}],
}
USER_DRIVES = {"u1": DRIVES["site-ann"][0]}


def _item(iid, name, parent, *, shared=True, folder=False):
    item = {"id": iid, "name": name, "webUrl": f"https://x/{name}", "lastModifiedDateTime": "2026-09-01T00:00:00Z",
            "parentReference": {"id": parent, "path": "/drives/d/root:"}, "size": 100}
    item["folder" if folder else "file"] = {}
    if shared:
        item["shared"] = {"scope": "users"}
    return item


DELTA = {
    "drv-fin": [{"id": "root-fin", "root": {}, "folder": {}, "shared": {}},
                _item("it-clients", "Clients", "root-fin", folder=True),
                _item("it-inside", "Clients plan.docx", "it-clients"),
                _item("it-budget", "Budget.xlsx", "root-fin"),
                _item("it-policy", "Policy.docx", "root-fin"),
                _item("it-internal", "Internal.docx", "root-fin"),
                _item("it-private", "Notes.txt", "root-fin", shared=False),
                dict(_item("it-deleted", "Old.docx", "root-fin"), deleted={})],
    "drv-ann": [_item("it-holiday", "holiday.jpg", "root-ann")],
}
PERMISSIONS = {
    "it-clients": [{"id": "p1", "roles": ["write"], "link": {"scope": "anonymous", "type": "edit"}},
                   {"id": "p0", "roles": ["owner"], "grantedToV2": {"siteGroup": {"displayName": "Owners"}},
                    "inheritedFrom": {"id": "root-fin"}}],
    "it-budget": [{"id": "p2", "roles": ["read"], "link": {"scope": "users", "type": "view"},
                   "grantedToIdentitiesV2": [{"user": {"email": "partner@other.com"}}]}],
    "it-policy": [{"id": "p3", "roles": ["read"], "link": {"scope": "organization", "type": "view"}}],
    "it-internal": [{"id": "p4", "roles": ["write"], "grantedToV2": {"user": {"email": "bob@x.com"}}}],
    "it-holiday": [{"id": "p5", "roles": ["read"], "link": {"scope": "anonymous", "type": "view"},
                    "expirationDateTime": "2099-01-01T00:00:00Z"}],
}


def mock_sharepoint(rsps, *, sites=None, files=True):
    rsps.get(f"{BETA}/reports/getSharePointSiteUsageDetail(period='D30')",
             json={"value": SITE_USAGE if sites is None else sites})
    rsps.get(f"{BETA}/reports/getOneDriveUsageAccountDetail(period='D30')", json={"value": ONEDRIVE_USAGE})
    rsps.get(f"{BETA}/reports/getSharePointSiteUsageStorage(period='D180')", json={"value": SITE_STORAGE})
    rsps.get(f"{BETA}/reports/getOneDriveUsageStorage(period='D180')", json={"value": ONEDRIVE_STORAGE})

    if not files:
        rsps.get(f"{V1}/sites/getAllSites", status=403,
                 json={"error": {"code": "accessDenied", "message": "Access denied"}})
        return
    rsps.get(f"{V1}/sites/getAllSites", json={"value": SITES})

    def by_id(table, index, status_if_missing=404):
        def callback(request):
            key = request.path_url.split("?")[0].split("/")[index]
            if key not in table:
                return status_if_missing, {}, json.dumps({"error": {"code": "itemNotFound", "message": "nope"}})
            value = table[key]
            return 200, {}, json.dumps({"value": value} if isinstance(value, list) else value)
        return callback

    rsps.add_callback(responses.GET, re.compile(re.escape(V1) + r"/sites/[^/]+/drives"),
                      callback=by_id(DRIVES, 3), content_type="application/json")
    rsps.add_callback(responses.GET, re.compile(re.escape(V1) + r"/users/[^/]+/drive(\?.*)?$"),
                      callback=by_id(USER_DRIVES, 3), content_type="application/json")
    rsps.add_callback(responses.GET, re.compile(re.escape(V1) + r"/drives/[^/]+/root/delta"),
                      callback=by_id(DELTA, 3), content_type="application/json")
    rsps.add_callback(responses.GET, re.compile(re.escape(V1) + r"/drives/[^/]+/items/[^/]+/permissions"),
                      callback=by_id(PERMISSIONS, 5), content_type="application/json")
