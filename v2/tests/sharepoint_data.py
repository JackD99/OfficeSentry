"""Recorded-style Graph payloads for SharePoint, OneDrive and shared files."""

import json
import re
import zlib
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import responses

from officesentry.graph import GRAPH_ROOT

V1 = f"{GRAPH_ROOT}/v1.0"
GB = 1024**3
MB = 1024**2
BETA = f"{GRAPH_ROOT}/beta"


def days_ago(n: int) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).date().isoformat()


def _site(url, template, *, activity=5, storage=0, anyone=0, guest=0, company=0, deleted=False, owner="Owner"):
    return {
        "reportRefreshDate": days_ago(2),
        "siteId": f"11111111-1111-1111-1111-{zlib.crc32(url.encode()):012d}",
        "siteUrl": url,
        "ownerDisplayName": owner,
        "ownerPrincipalName": f"{owner.lower()}@x.com",
        "isDeleted": deleted,
        "lastActivityDate": days_ago(activity) if activity is not None else None,
        "fileCount": 10,
        "activeFileCount": 2,
        "pageViewCount": 4,
        "visitedPageCount": 2,
        "storageUsedInBytes": storage,
        "storageAllocatedInBytes": 27487790694400,
        "externalSharing": bool(anyone or guest),
        "unmanagedDevicePolicy": "None",
        "siteSensitivityLabelId": "",
        "anonymousLinkCount": anyone,
        "companyLinkCount": company,
        "secureLinkForGuestCount": guest,
        "secureLinkForMemberCount": 0,
        "rootWebTemplate": template,
        "reportPeriod": "30",
    }


FINANCE = "https://x.sharepoint.com/sites/finance"
OLD = "https://x.sharepoint.com/sites/old-projects"
SITE_USAGE = [
    _site(FINANCE, "Group", storage=5 * 1024**3, anyone=2, guest=1, company=1),
    _site(OLD, "Site Page Publishing", activity=200, storage=1024**3),
    _site("https://x-admin.sharepoint.com", "Tenant Admin Site", activity=None, storage=2 * 1024**2),
    _site("https://x.sharepoint.com/sites/gone", "Team Site", deleted=True),
]


def _drive(upn, storage, activity=3):
    return {
        "reportRefreshDate": days_ago(2),
        "siteId": "22222222-2222-2222-2222-222222222222",
        "siteUrl": f"https://x-my.sharepoint.com/personal/{upn.replace('@', '_').replace('.', '_')}",
        "ownerDisplayName": upn.split("@")[0].title(),
        "ownerPrincipalName": upn,
        "isDeleted": False,
        "lastActivityDate": days_ago(activity),
        "fileCount": 50,
        "activeFileCount": 5,
        "storageUsedInBytes": storage,
        "storageAllocatedInBytes": 1024**4,
        "reportPeriod": "30",
    }


ONEDRIVE_USAGE = [
    _drive("ann@x.com", 3 * 1024**3),
    _drive("bob@x.com", 2 * 1024**3, activity=120),
    _drive("gone@x.com", 1024**3, activity=400),
]


def _storage(site_type, start, step):
    return [
        {
            "reportRefreshDate": days_ago(2),
            "siteType": site_type,
            "reportDate": days_ago(2 + i),
            "storageUsedInBytes": start - i * step,
            "reportPeriod": "180",
        }
        for i in range(0, 180, 10)
    ]


SITE_STORAGE = _storage("All", 6 * 1024**3, 20 * 1024**2)
ONEDRIVE_STORAGE = _storage("All", 6 * 1024**3, 10 * 1024**2) + _storage("OneDrive", 1, 0)

CONCEALED_SITE = dict(
    SITE_USAGE[0],
    siteUrl=None,
    siteId="00000000-0000-0000-0000-000000000000",
    ownerDisplayName="82D28824CBDAF3EA9AD693254DE8CC08",
    ownerPrincipalName=None,
)

# -- Shared files ------------------------------------------------------------------

SITES = [
    {"id": "site-fin", "displayName": "Finance", "webUrl": FINANCE},
    {
        "id": "site-ann",
        "displayName": "Ann",
        "webUrl": "https://x-my.sharepoint.com/personal/ann_x_com",
        "isPersonalSite": True,
    },
    {"id": "site-old", "displayName": "Old Projects", "webUrl": OLD},  # no group, one disabled administrator
]
DRIVES = {
    "site-fin": [
        {"id": "drv-fin", "name": "Documents", "webUrl": FINANCE + "/Shared Documents", "driveType": "documentLibrary"}
    ],
    "site-old": [],
    "site-ann": [
        {
            "id": "drv-ann",
            "name": "OneDrive",
            "webUrl": SITES[1]["webUrl"] + "/Documents",
            "driveType": "business",
            "owner": {"user": {"email": "ann@x.com", "displayName": "Ann"}},
            "quota": {"used": 950 * GB, "deleted": 10 * GB, "total": 1024 * GB, "state": "nearing"},
        }
    ],
}
USER_DRIVES = {"u1": DRIVES["site-ann"][0]}


def _item(iid, name, parent, *, shared=True, folder=False, by="ann@x.com", at="2026-08-20T10:00:00Z"):
    item = {
        "id": iid,
        "name": name,
        "webUrl": f"https://x/{name}",
        "lastModifiedDateTime": "2026-09-01T00:00:00Z",
        "parentReference": {"id": parent, "path": "/drives/d/root:"},
        "size": 100,
    }
    item["folder" if folder else "file"] = {}
    if shared:
        item["shared"] = {
            "scope": "users",
            "sharedBy": {"user": {"email": by, "displayName": "Ann"}},
            "sharedDateTime": at,
        }
    return item


DELTA = {
    "drv-fin": [
        {"id": "root-fin", "root": {}, "folder": {}, "shared": {}},
        _item("it-clients", "Clients", "root-fin", folder=True),
        _item("it-inside", "Clients plan.docx", "it-clients"),
        _item("it-budget", "Budget.xlsx", "root-fin", by="bob@x.com", at=days_ago(5) + "T09:00:00Z"),
        _item("it-policy", "Policy.docx", "root-fin"),
        _item("it-internal", "Internal.docx", "root-fin"),
        _item("it-private", "Notes.txt", "root-fin", shared=False),
        dict(_item("it-deleted", "Old.docx", "root-fin"), deleted={}),
    ],
    "drv-ann": [_item("it-holiday", "holiday.jpg", "root-ann")],
}
# What each library's delta link answers on a later run: only what changed since (``mock_sharepoint``
# serves these for a delta request carrying a token). Empty by default: nothing changed.
DELTA_CHANGES: dict[str, list] = {}
PERMISSIONS = {
    "it-clients": [
        {"id": "p1", "roles": ["write"], "link": {"scope": "anonymous", "type": "edit"}},
        {
            "id": "p0",
            "roles": ["owner"],
            "grantedToV2": {"siteGroup": {"displayName": "Owners"}},
            "inheritedFrom": {"id": "root-fin"},
        },
    ],
    "it-budget": [
        {
            "id": "p2",
            "roles": ["read"],
            "link": {"scope": "users", "type": "view"},
            "grantedToIdentitiesV2": [{"user": {"email": "partner@other.com"}}],
            "expirationDateTime": days_ago(-10) + "T00:00:00Z",  # expires in 10 days
        },
        {
            "id": "p2b",
            "roles": ["read"],
            "link": {"scope": "users", "type": "view"},
            "grantedToIdentitiesV2": [{"user": {"email": "old@other.com"}}],
            "expirationDateTime": days_ago(3) + "T00:00:00Z",  # expired
        },
    ],
    "it-policy": [{"id": "p3", "roles": ["read"], "link": {"scope": "organization", "type": "view"}}],
    "it-internal": [
        {"id": "p4", "roles": ["write"], "grantedToV2": {"user": {"email": "bob@x.com"}}},
        {"id": "p4b", "roles": ["read"], "grantedToV2": {"group": {"displayName": "Sales", "id": "g1"}}},
    ],
    "it-holiday": [
        {
            "id": "p5",
            "roles": ["read"],
            "link": {"scope": "anonymous", "type": "view"},
            "expirationDateTime": "2099-01-01T00:00:00Z",
        }
    ],
}


# -- Site access --------------------------------------------------------------------


def _uil(title, login, *, email=None, admin=False, group=False, deleted=False):
    """One row of a site's hidden User Information List, as Graph returns list items."""
    return {
        "id": str(zlib.crc32(login.encode())),
        "contentType": {"name": "SharePointGroup" if group else "Person"},
        "fields": {
            "Title": title,
            "EMail": email or "",
            "Name": login,
            "UserName": email or "",
            "IsSiteAdmin": admin,
            "Deleted": deleted,
        },
    }


EVERYONE_BUT_GUESTS = "c:0-.f|rolemanager|spo-grid-all-users/1b7b2a3c-0000-0000-0000-000000000001"
USER_INFO = {
    # Finance is a group-connected site (g1 Sales in the group fixtures): its owners come from the group.
    "site-fin": [
        _uil("System Account", "SHAREPOINT\\system"),
        _uil("Sales Owners", "Sales Owners", group=True),
        _uil("Sales Members", "Sales Members", group=True),
        _uil("Sales Visitors", "Sales Visitors", group=True),
        _uil("Sales", "c:0o.c|federateddirectoryclaimprovider|g1"),
        _uil("Sales", "c:0o.c|federateddirectoryclaimprovider|g1_o"),
        _uil("Ann", "i:0#.f|membership|ann@x.com", email="ann@x.com", admin=True),
        _uil("Eve", "i:0#.f|membership|eve@x.com", email="eve@x.com"),
        _uil("Pat Supplier", "i:0#.f|membership|pat_supplier.com#ext#@x.onmicrosoft.com", email="pat@supplier.com"),
        _uil("Old Guest", "i:0#.f|membership|old_gone.com#ext#@x.onmicrosoft.com", email="old@gone.com", deleted=True),
        _uil("Office Sentry", "i:0i.t|00000003-0000-0ff1-ce00-000000000000|app@sharepoint"),
    ],
    # Old Projects has no group: its only administrator is Bob, whose account is disabled.
    "site-old": [
        _uil("Old Projects Owners", "Old Projects Owners", group=True),
        _uil("Old Projects Members", "Old Projects Members", group=True),
        _uil("Bob", "i:0#.f|membership|bob@x.com", email="bob@x.com", admin=True),
        _uil("Everyone except external users", EVERYONE_BUT_GUESTS),
    ],
}
ROOT_PERMISSIONS = {
    "site-fin": [
        {"id": "r1", "roles": ["owner"], "grantedToV2": {"siteGroup": {"displayName": "Sales Owners", "id": "3"}}},
        {"id": "r2", "roles": ["write"], "grantedToV2": {"siteGroup": {"displayName": "Sales Members", "id": "5"}}},
        {"id": "r3", "roles": ["read"], "grantedToV2": {"siteGroup": {"displayName": "Sales Visitors", "id": "4"}}},
        {
            "id": "r4",
            "roles": ["read"],
            "grantedToV2": {
                "siteUser": {
                    "displayName": "Pat Supplier",
                    "email": "pat@supplier.com",
                    "loginName": "i:0#.f|membership|pat_supplier.com#ext#@x.onmicrosoft.com",
                }
            },
        },
    ],
    "site-old": [
        {
            "id": "r5",
            "roles": ["owner"],
            "grantedToV2": {"siteGroup": {"displayName": "Old Projects Owners", "id": "3"}},
        },
        {
            "id": "r6",
            "roles": ["read"],
            "grantedToV2": {
                "siteUser": {"displayName": "Everyone except external users", "loginName": EVERYONE_BUT_GUESTS}
            },
        },
    ],
}


# -- Storage ------------------------------------------------------------------------

SITE_QUOTA = {
    "site-fin": {"used": 5 * GB, "deleted": 1 * GB, "total": 27487790694400, "state": "normal"},
    "site-old": {"used": 0, "deleted": 0, "total": 27487790694400, "state": "normal"},
}
LISTS = {
    "site-fin": [
        {
            "id": "lst-docs",
            "name": "Shared Documents",
            "displayName": "Documents",
            "webUrl": FINANCE + "/Shared%20Documents",
            "list": {"template": "documentLibrary", "hidden": False},
        },
        {
            "id": "lst-hold",
            "name": "PreservationHoldLibrary",
            "displayName": "Preservation Hold Library",
            "webUrl": FINANCE + "/PreservationHoldLibrary",
            "list": {"template": "documentLibrary", "hidden": False},
            "system": {},
        },
        {
            "id": "lst-events",
            "name": "Events",
            "displayName": "Events",
            "webUrl": FINANCE + "/Lists/Events",
            "list": {"template": "events", "hidden": False},
        },
    ],
    "site-old": [],
}


def _li(ref, *, total, current=None, version="1.0", folder=False, files=0, modified=None, by="ann@x.com"):
    fields = {
        "FileLeafRef": ref.rsplit("/", 1)[-1],
        "FileRef": ref,
        "FSObjType": "1" if folder else "0",
        "SMTotalSize": str(total),
        "SMTotalFileStreamSize": str(0 if folder else (total if current is None else current)),
        "SMTotalFileCount": str(files),
        "_UIVersionString": version,
    }
    return {
        "id": str(zlib.crc32(ref.encode())),
        "webUrl": "https://x.sharepoint.com" + ref.replace(" ", "%20"),
        "lastModifiedDateTime": modified or days_ago(10) + "T09:00:00Z",
        "lastModifiedBy": {"user": {"email": by, "displayName": by.split("@")[0].title()}},
        "fields": fields,
    }


DOCS = "/sites/finance/Shared Documents"
LIST_ITEMS = {
    "lst-docs": [
        _li(DOCS + "/Clients", total=3 * GB, folder=True, files=2),
        _li(DOCS + "/Clients/Mail export.pst", total=2 * GB),
        _li(DOCS + "/Clients/Plan.xlsx", total=1 * GB, current=100 * MB, version="320.0", by="bob@x.com"),
        _li(DOCS + "/Old", total=500 * MB, folder=True, files=1),
        _li(DOCS + "/Old/Archive.zip", total=500 * MB, modified=days_ago(1200) + "T09:00:00Z"),
    ],
    "lst-hold": [_li("/sites/finance/PreservationHoldLibrary/Plan_v1.xlsx", total=300 * MB)],
    "list-ann": [_li("/personal/ann_x_com/Documents/Videos/raw.mp4", total=50 * GB, current=40 * GB, version="3.0")],
}
DRIVE_LISTS = {
    "drv-ann": {
        "id": "list-ann",
        "name": "Documents",
        "displayName": "Documents",
        "webUrl": "https://x-my.sharepoint.com/personal/ann_x_com/Documents",
    }
}
RECYCLE_BIN = {
    "site-fin": [
        {
            "id": "r1",
            "name": "Q1 scans.zip",
            "size": 800 * MB,
            "deletedDateTime": "2026-09-20T10:00:00Z",
            "deletedFromLocation": "sites/finance/Shared Documents/Scans",
        },
        {
            "id": "r2",
            "name": "notes.txt",
            "size": 2048,
            "deletedDateTime": "2026-09-21T10:00:00Z",
            "deletedFromLocation": "sites/finance/Shared Documents",
        },
    ]
}


def mock_sharepoint(rsps, *, sites=None, files=True, site_quota=None, rest=True):
    """Graph's SharePoint answers; ``rest`` adds SharePoint's own API (True), refuses it (``"consent"``, the
    SharePoint permission not approved yet) or leaves it out (False)."""
    rsps.get(
        f"{BETA}/reports/getSharePointSiteUsageDetail(period='D30')",
        json={"value": SITE_USAGE if sites is None else sites},
    )
    rsps.get(f"{BETA}/reports/getOneDriveUsageAccountDetail(period='D30')", json={"value": ONEDRIVE_USAGE})
    rsps.get(f"{BETA}/reports/getSharePointSiteUsageStorage(period='D180')", json={"value": SITE_STORAGE})
    rsps.get(f"{BETA}/reports/getOneDriveUsageStorage(period='D180')", json={"value": ONEDRIVE_STORAGE})

    if not files:
        rsps.get(
            f"{V1}/sites/getAllSites", status=403, json={"error": {"code": "accessDenied", "message": "Access denied"}}
        )
        return
    rsps.get(f"{V1}/sites/getAllSites", json={"value": SITES})
    if rest:
        mock_sharepoint_rest(rsps, approved=rest != "consent")

    def by_id(table, index, status_if_missing=404):
        def callback(request):
            key = request.path_url.split("?")[0].split("/")[index]
            if key not in table:
                return status_if_missing, {}, json.dumps({"error": {"code": "itemNotFound", "message": "nope"}})
            value = table[key]
            return 200, {}, json.dumps({"value": value} if isinstance(value, list) else value)

        return callback

    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(V1) + r"/sites/[^/]+/drives"),
        callback=by_id(DRIVES, 3),
        content_type="application/json",
    )
    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(V1) + r"/users/[^/]+/drive(\?.*)?$"),
        callback=by_id(USER_DRIVES, 3),
        content_type="application/json",
    )

    def delta(request):  # a whole library, with a delta link; or, with a token, what changed since
        drive = request.path_url.split("?")[0].split("/")[3]
        if "token=" in request.path_url:
            return 200, {}, json.dumps({"value": DELTA_CHANGES.get(drive, []), "@odata.deltaLink": request.url})
        if drive not in DELTA:
            return 404, {}, json.dumps({"error": {"code": "itemNotFound", "message": "nope"}})
        link = f"{V1}/drives/{drive}/root/delta?token=t-{drive}"
        return 200, {}, json.dumps({"value": DELTA[drive], "@odata.deltaLink": link})

    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(V1) + r"/drives/[^/]+/root/delta"),
        callback=delta,
        content_type="application/json",
    )
    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(V1) + r"/drives/[^/]+/items/[^/]+/permissions"),
        callback=by_id(PERMISSIONS, 5),
        content_type="application/json",
    )

    quotas = SITE_QUOTA if site_quota is None else site_quota
    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(V1) + r"/sites/[^/]+/drive(\?.*)?$"),
        callback=by_id({k: {"id": "d", "quota": v} for k, v in quotas.items()}, 3),
        content_type="application/json",
    )
    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(V1) + r"/sites/[^/]+/lists(\?.*)?$"),
        callback=by_id(LISTS, 3),
        content_type="application/json",
    )
    user_lists = {sid: v for sid, v in USER_INFO.items()}

    def list_items(request):  # a library's items by list id, or a site's User Information List
        parts = request.path_url.split("?")[0].split("/")
        if parts[5] == "User%20Information%20List":
            return by_id(user_lists, 3)(request)
        return by_id(LIST_ITEMS, 5)(request)

    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(V1) + r"/sites/[^/]+/lists/[^/]+/items"),
        callback=list_items,
        content_type="application/json",
    )
    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(V1) + r"/sites/[^/]+/drive/root/permissions"),
        callback=by_id(ROOT_PERMISSIONS, 3),
        content_type="application/json",
    )
    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(V1) + r"/drives/[^/]+/list(\?.*)?$"),
        callback=by_id(DRIVE_LISTS, 3),
        content_type="application/json",
    )
    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(V1) + r"/drives/[^/]+/list/items"),
        callback=by_id({"drv-ann": LIST_ITEMS["list-ann"]}, 3),
        content_type="application/json",
    )
    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(BETA) + r"/sites/[^/]+/recycleBin/items"),
        callback=by_id({**{s["id"]: [] for s in SITES}, **RECYCLE_BIN}, 3),
        content_type="application/json",
    )


# -- Site permissions (SharePoint's own REST API) --------------------------------------


def _principal(pid, title, login, kind=1, email=""):
    return {"Id": pid, "Title": title, "LoginName": login, "PrincipalType": kind, "Email": email}


def _assignment(member, *levels, hidden=()):
    return {
        "PrincipalId": member["Id"],
        "Member": member,
        "RoleDefinitionBindings": [{"Name": n, "Hidden": False} for n in levels]
        + [{"Name": n, "Hidden": True} for n in hidden],
    }


def _sp_group(gid, title, users, owner=None):
    return {"Id": gid, "Title": title, "LoginName": title, "OwnerTitle": owner or title, "Users": users}


PAT = "i:0#.f|membership|pat_supplier.com#ext#@x.onmicrosoft.com"
DAN = "i:0#.f|membership|dan_contractor.com#ext#@x.onmicrosoft.com"
SALES_OWNERS, SALES_MEMBERS, SALES_VISITORS, BOARD = (
    _principal(3, "Sales Owners", "Sales Owners", 8),
    _principal(5, "Sales Members", "Sales Members", 8),
    _principal(4, "Sales Visitors", "Sales Visitors", 8),
    _principal(7, "Board pack readers", "Board pack readers", 8),
)
OLD_OWNERS, OLD_MEMBERS = (
    _principal(3, "Old Projects Owners", "Old Projects Owners", 8),
    _principal(5, "Old Projects Members", "Old Projects Members", 8),
)
BOB = _principal(11, "Bob", "i:0#.f|membership|bob@x.com", email="bob@x.com")
ANN = _principal(10, "Ann", "i:0#.f|membership|ann@x.com", email="ann@x.com")
EVE = _principal(12, "Eve", "i:0#.f|membership|eve@x.com", email="eve@x.com")
DAN_GUEST = _principal(14, "Dan Contractor", DAN, email="dan@contractor.com")
SYSTEM = _principal(1, "System Account", "SHAREPOINT\\system")
BOARD_URL = "https://x.sharepoint.com/sites/finance/Board pack"
ARCHIVE = "https://x.sharepoint.com/sites/finance/archive"
SP_REST = {
    FINANCE: {
        "/web": {
            "Id": "w-fin",
            "Title": "Finance",
            "Url": FINANCE,
            "HasUniqueRoleAssignments": True,
            "AssociatedOwnerGroup": {"Id": 3},
            "AssociatedMemberGroup": {"Id": 5},
            "AssociatedVisitorGroup": {"Id": 4},
        },
        "/web/sitegroups": [
            _sp_group(
                3, "Sales Owners", [_principal(20, "Sales Owners", "c:0o.c|federateddirectoryclaimprovider|g1_o", 4)]
            ),
            _sp_group(
                5,
                "Sales Members",
                [
                    _principal(21, "Sales Members", "c:0o.c|federateddirectoryclaimprovider|g1", 4),
                    _principal(13, "Pat Supplier", PAT, email="pat@supplier.com"),
                ],
                owner="Sales Owners",
            ),
            _sp_group(4, "Sales Visitors", [_principal(22, "Everyone except external users", EVERYONE_BUT_GUESTS, 4)]),
            _sp_group(7, "Board pack readers", [EVE], owner="Sales Owners"),
        ],
        "/web/roleassignments": [
            _assignment(SALES_OWNERS, "Full Control"),
            _assignment(SALES_MEMBERS, "Edit"),
            _assignment(SALES_VISITORS, "Read"),
            _assignment(BOB, "Contribute"),
            _assignment(DAN_GUEST, "Full Control"),
            _assignment(EVE, hidden=("Limited Access",)),  # given one file: left out
            _assignment(SYSTEM, "Full Control"),
        ],
        "/web/lists": [
            {
                "Id": "l-docs",
                "Title": "Documents",
                "BaseType": 1,
                "HasUniqueRoleAssignments": False,
                "ItemCount": 40,
                "RootFolder": {"ServerRelativeUrl": "/sites/finance/Shared Documents"},
            },
            {
                "Id": "l-board",
                "Title": "Board pack",
                "BaseType": 1,
                "HasUniqueRoleAssignments": True,
                "ItemCount": 12,
                "RootFolder": {"ServerRelativeUrl": "/sites/finance/Board pack"},
            },
            {
                "Id": "l-exp",
                "Title": "Expenses",
                "BaseType": 0,
                "HasUniqueRoleAssignments": True,
                "ItemCount": 230,
                "RootFolder": {"ServerRelativeUrl": "/sites/finance/Lists/Expenses"},
            },
            {
                "Id": "l-style",
                "Title": "Style Library",
                "BaseType": 1,
                "HasUniqueRoleAssignments": True,
                "IsSystemList": True,
                "ItemCount": 3,
                "RootFolder": {"ServerRelativeUrl": "/sites/finance/Style Library"},
            },
        ],
        "/web/lists(guid'l-board')/roleassignments": [
            _assignment(SALES_OWNERS, "Full Control"),
            _assignment(BOARD, "Read"),
        ],
        "/web/lists(guid'l-exp')/roleassignments": [
            _assignment(SALES_OWNERS, "Full Control"),
            _assignment(ANN, "Edit"),
            _assignment(_principal(13, "Pat Supplier", PAT, email="pat@supplier.com"), "Read"),
        ],
        "/web/webs": [
            {"Title": "Archive", "Url": ARCHIVE, "HasUniqueRoleAssignments": True},
            {"Title": "Team wiki", "Url": FINANCE + "/wiki", "HasUniqueRoleAssignments": False},
        ],
    },
    ARCHIVE: {"/web/roleassignments": [_assignment(SALES_OWNERS, "Full Control"), _assignment(BOB, "Read")]},
    OLD: {
        "/web": {
            "Id": "w-old",
            "Title": "Old Projects",
            "Url": OLD,
            "AssociatedOwnerGroup": {"Id": 3},
            "AssociatedMemberGroup": {"Id": 5},
            "AssociatedVisitorGroup": None,
        },
        "/web/sitegroups": [
            _sp_group(3, "Old Projects Owners", [BOB]),
            _sp_group(
                5, "Old Projects Members", [_principal(22, "Everyone except external users", EVERYONE_BUT_GUESTS, 4)]
            ),
        ],
        "/web/roleassignments": [_assignment(OLD_OWNERS, "Full Control"), _assignment(OLD_MEMBERS, "Edit")],
        "/web/lists": [
            {
                "Id": "l-old",
                "Title": "Documents",
                "BaseType": 1,
                "HasUniqueRoleAssignments": False,
                "ItemCount": 2,
                "RootFolder": {"ServerRelativeUrl": "/sites/old-projects/Shared Documents"},
            },
        ],
        "/web/webs": [],
    },
}


def mock_sharepoint_rest(rsps, *, approved=True, rest=None, statuses=None):
    """SharePoint's REST API on the tenant's host: each site's answers from ``SP_REST`` (or ``rest``), or 401
    everywhere until the client approves the SharePoint permission. ``statuses`` (site address -> HTTP status)
    answers every call for those sites with that status instead."""
    table = SP_REST if rest is None else rest

    def callback(request):
        site = unquote(request.url.split("?")[0].partition("/_api")[0])
        if site in (statuses or {}):
            return statuses[site], {}, json.dumps({"error": "No"})
        if not approved:
            body = {"error_description": "Unsupported app only token."}
            return 401, {}, json.dumps(body)
        url = request.url.split("?")[0]
        site, _, api = url.partition("/_api")
        answers = table.get(unquote(site))
        if answers is None or unquote(api) not in answers:
            err = {
                "odata.error": {
                    "code": "-2147024894, System.IO.FileNotFoundException",
                    "message": {"value": "Not found"},
                }
            }
            return 404, {}, json.dumps(err)
        value = answers[unquote(api)]
        return 200, {}, json.dumps({"value": value} if isinstance(value, list) else value)

    rsps.add_callback(
        responses.GET,
        re.compile(r"https://[a-z0-9-]+\.sharepoint\.com(/.*)?/_api/.*"),
        callback=callback,
        content_type="application/json",
    )
