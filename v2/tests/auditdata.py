"""The audit log add-on in tests: switching it on, and Microsoft's Management Activity feed simulated at the HTTP
level with ``responses`` (subscriptions, content listings and batches of audit records).

The records are shaped like real ones from a tenant's unified audit log (names and addresses made up). Each
record's ``Id`` is unique, so storing a batch twice keeps one copy.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import responses

from officesentry import events, store
from officesentry.auditlog import addon
from officesentry.auditlog.feed import FeedClient
from officesentry.certs import generate_certificate
from officesentry.db import utcnow
from officesentry.graph import GraphClient
from officesentry.worker import execute_run

CLIENT_ID = "abababab-1111-2222-3333-444444444444"  # the add-on's app
MSP = "12121212-3434-5656-7878-909090909090"  # the MSP's own tenant, where the add-on's app was created
FEED = "https://manage.office.com"


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def addon_on(db, vault, tenants=(), started_at: str | None = None) -> int:
    """The add-on switched on with its app (commercial), and each of ``tenants`` approved with its feed on.
    Returns the add-on app's connection id."""
    conn_id = store.create_connection(
        db, vault, addon.AUDIT_NAME, generate_certificate("audit"), client_id=CLIENT_ID, kind=store.AUDIT
    )
    db.execute("UPDATE app_connections SET home_tenant = ? WHERE id = ?", (MSP, conn_id))
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('audit_addon', '1') ON CONFLICT(key) DO UPDATE SET value = '1'"
    )
    now = utcnow()
    for tenant_id in tenants:
        db.execute(
            "INSERT INTO audit_feeds (tenant_id, connection_id, sent_at, sent_by, wanted_at, wanted_by, approved_at,"
            " consented_at, checked_at, started_at, started_by, content_types, created_at)"
            " VALUES (?, ?, ?, 'admin', ?, 'admin', ?, ?, ?, ?, 'admin', ?, ?)",
            (
                tenant_id,
                conn_id,
                now,
                now,
                now,
                now,
                now,
                started_at or now,
                json.dumps(dict.fromkeys(("Audit.SharePoint", "Audit.Exchange"), "enabled")),
                now,
            ),
        )
    db.commit()
    return conn_id


def everywhere(db, vault) -> int:
    """The add-on on for every client there is, for tests that open every report (its report is only there
    for a client the add-on was asked for)."""
    return addon_on(db, vault, [t["id"] for t in db.execute("SELECT id FROM tenants").fetchall()])


# -- Audit records ------------------------------------------------------------------------------------------

SITE = "https://contoso.sharepoint.com/sites/finance"


def _sp(n: int, when: datetime, operation: str, user: str, **extra) -> dict:
    record = {
        "Id": f"sp-{n:04d}-0000-0000-0000-000000000000",
        "RecordType": 6,
        "CreationTime": when.strftime("%Y-%m-%dT%H:%M:%S"),
        "Operation": operation,
        "OrganizationId": "org",
        "UserType": 0,
        "Workload": "SharePoint",
        "UserId": user,
        "ClientIP": "203.0.113.7",
        "SiteUrl": SITE + "/",
        "SourceRelativeUrl": "Shared Documents",
        "SourceFileName": "Budget 2027.xlsx",
        "ObjectId": f"{SITE}/Shared Documents/Budget 2027.xlsx",
    }
    record.update(extra)
    return record


def _ex(n: int, when: datetime, operation: str, user: str, **extra) -> dict:
    record = {
        "Id": f"ex-{n:04d}-0000-0000-0000-000000000000",
        "RecordType": 1,
        "CreationTime": when.strftime("%Y-%m-%dT%H:%M:%S"),
        "Operation": operation,
        "OrganizationId": "org",
        "UserType": 2,
        "Workload": "Exchange",
        "UserId": user,
        "ClientIP": "198.51.100.4",
        "ResultStatus": "True",
    }
    record.update(extra)
    return record


def _params(**values) -> list[dict]:
    return [{"Name": k, "Value": v} for k, v in values.items()]


def sharepoint_records(at: datetime) -> list[dict]:
    """A SharePoint and OneDrive batch: what the add-on keeps, counts and drops."""
    return [
        _sp(1, at, "FileDeleted", "jo@contoso.com"),
        _sp(2, at, "FileDownloaded", "sam@contoso.com", ClientIP="192.0.2.10"),
        _sp(3, at, "AnonymousLinkCreated", "jo@contoso.com", EventData="<Type>View</Type>"),
        _sp(
            4,
            at,
            "SharingSet",
            "jo@contoso.com",
            TargetUserOrGroupName="guest@partner.example",
            TargetUserOrGroupType="Guest",
        ),
        _sp(
            5,
            at,
            "FileMoved",
            "sam@contoso.com",
            DestinationRelativeUrl="Shared Documents/Archive",
            DestinationFileName="Budget 2027.xlsx",
        ),
        _sp(6, at, "FileSyncDownloadedFull", "sam@contoso.com"),
        _sp(7, at, "FileSyncDownloadedFull", "sam@contoso.com", SourceFileName="Plan.docx"),
        _sp(8, at, "FileAccessed", "jo@contoso.com"),  # opened by its owner: dropped
        _sp(9, at, "PageViewed", "jo@contoso.com"),  # dropped
    ]


def exchange_records(at: datetime) -> list[dict]:
    """An Exchange batch: mailboxes opened by others, rules and forwarding, permissions."""
    return [
        _ex(
            1,
            at,
            "MailItemsAccessed",
            "admin@contoso.com",
            LogonType=1,
            MailboxOwnerUPN="ceo@contoso.com",
            ClientInfoString="Client=OWA",
            Folders=[
                {
                    "Path": "\\Inbox",
                    "FolderItems": [{"InternetMessageId": "<1@contoso.com>", "Subject": "Board pay review"}],
                }
            ],
        ),
        _ex(
            2,
            at,
            "MailItemsAccessed",
            "ceo@contoso.com",
            LogonType=0,
            MailboxOwnerUPN="ceo@contoso.com",
        ),  # the owner reading their own mail: dropped
        _ex(
            3,
            at,
            "New-InboxRule",
            "jo@contoso.com",
            ObjectId="jo@contoso.com\\Move it",
            Parameters=_params(Identity="jo@contoso.com", Name="Move it", ForwardTo="drop@mailbox.example"),
        ),
        _ex(
            4,
            at,
            "Set-Mailbox",
            "admin@contoso.com",
            ObjectId="sam@contoso.com",
            Parameters=_params(Identity="sam@contoso.com", ForwardingSmtpAddress="smtp:drop@mailbox.example"),
        ),
        _ex(
            5,
            at,
            "Set-Mailbox",
            "admin@contoso.com",
            ObjectId="sam@contoso.com",
            Parameters=_params(Identity="sam@contoso.com", ProhibitSendQuota="49GB"),
        ),  # not forwarding: dropped
        _ex(
            6,
            at,
            "Add-MailboxPermission",
            "admin@contoso.com",
            ObjectId="ceo@contoso.com",
            Parameters=_params(Identity="ceo@contoso.com", User="pa@contoso.com", AccessRights="FullAccess"),
        ),
        _ex(7, at, "SendAs", "pa@contoso.com", SendAsUserSmtp="ceo@contoso.com", LogonType=2),
    ]


# -- The feed -----------------------------------------------------------------------------------------------


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "")).replace(tzinfo=None)


def mock_feed(rsps, tenant: str, batches: dict[str, list[tuple[datetime, list[dict]]]], status: str = "enabled"):
    """Simulate a client's feed: ``batches`` maps a content type to (when Microsoft made it available, its
    records). Listings return the batches made available in the asked window."""
    root = f"{FEED}/api/v1.0/{tenant}/activity/feed"
    blobs: dict[str, list[dict]] = {}
    listed: dict[str, list[dict]] = {}
    for content_type, items in batches.items():
        for n, (created, records) in enumerate(items):
            content_id = f"{content_type.split('.')[-1].lower()}{n}"
            blobs[content_id] = records
            listed.setdefault(content_type, []).append(
                {
                    "contentType": content_type,
                    "contentId": content_id,
                    "contentUri": f"{root}/audit/{content_id}",
                    "contentCreated": created.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "contentExpiration": (created + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                }
            )

    def subscriptions(request):
        body = [{"contentType": c, "status": status, "webhook": None} for c in ("Audit.SharePoint", "Audit.Exchange")]
        return 200, {}, json.dumps(body)

    def content(request):
        q = parse_qs(urlsplit(request.url).query)
        start, end = _parse(q["startTime"][0]), _parse(q["endTime"][0])
        found = [b for b in listed.get(q["contentType"][0], []) if start <= _parse(b["contentCreated"][:19]) < end]
        return 200, {}, json.dumps(found)

    def blob(request):
        content_id = urlsplit(request.url).path.rsplit("/", 1)[-1]
        return 200, {}, json.dumps(blobs.get(content_id, []))

    rsps.add_callback("GET", re.compile(re.escape(root) + r"/subscriptions/list"), callback=subscriptions)
    rsps.add_callback("GET", re.compile(re.escape(root) + r"/subscriptions/content"), callback=content)
    rsps.add_callback("GET", re.compile(re.escape(root) + r"/audit/"), callback=blob)
    return root


TENANT = "c0c0c0c0-aaaa-bbbb-cccc-dddddddddddd"  # the client's directory ID the feed is asked for


def feed_factory(tenant: str = TENANT):
    def factory(conn, t):
        return FeedClient(tenant, lambda: "t", sleep=lambda s: None)

    return factory


def collect(db, tenant_id: int, batches, now: datetime, tenant: str = TENANT, status: str = "enabled", rsps=None):
    """Run the add-on's collection for one client against a simulated feed, as the worker would. Returns the
    run's status and every request sent."""

    def run(mock) -> str:
        mock_feed(mock, tenant, batches, status=status)
        store.enqueue_run(db, tenant_id, "audit_activity")
        claimed = store.claim_next_run(db)
        graph = lambda t, c: GraphClient(lambda: "t", sleep=lambda s: None)  # noqa: E731
        return execute_run(db, claimed, graph, feed_factory=feed_factory(tenant))

    with patch.object(events, "clock", lambda: now):
        if rsps is not None:
            return run(rsps), list(rsps.calls)
        with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
            return run(mock), list(mock.calls)


def sample(now: datetime) -> dict:
    """An hour-old batch of each kind of record."""
    at = (now - timedelta(hours=1)).replace(tzinfo=None)
    return {
        "Audit.SharePoint": [(at + timedelta(minutes=20), sharepoint_records(at))],
        "Audit.Exchange": [(at + timedelta(minutes=20), exchange_records(at))],
    }
