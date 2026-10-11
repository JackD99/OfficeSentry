"""Search every tenant someone may see, for MSP staff.

One box finds people, mailboxes (each opening its own page), where mail is forwarded to, sign-in IP
addresses, apps, devices and domains in each tenant's latest snapshot, so a
responder can answer "which clients forward to this address?" or "who signed in
from this IP?" without opening every tenant. Tenants whose index
(``search_index.py``, kept by the worker) is current are searched through it;
the rest are scanned in Python, so a search never misses what the latest
snapshot holds. Tenants without data are skipped and the search stops at a cap.

To make something searchable, add a ``Kind`` to ``search_index.KINDS`` (the
collector and object kind it reads, and the values to match in each object)
and how to show and link a match to ``SHOW`` here, under the kind's key. Links
open the person's page, or the detail report filtered to the row
(``f=<table>/<column>:<value>``, see ``reports/views.py``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from urllib.parse import urlencode

from flask import Blueprint, abort, g, render_template, request, url_for

from .. import search_index
from ..checks.apps import app_risks
from ..reports import sign_in_reports
from ..reports.group_page import group_type
from ..reports.sharepoint import site_type, size
from ..reports.site_page import site_key
from ..reports.views import slug
from ..search_index import KINDS
from ..snapshot import TenantSnapshot
from ..text import plural
from .access import roles_required, visible_tenants

bp = Blueprint("search", __name__)

MIN_LENGTH = 2
MAX_LENGTH = 200
SHOWN = 8  # results per tenant before "N more"
PER_TENANT = 200  # stop scanning a tenant after this many
TOTAL = 1000  # stop scanning altogether after this many
_IP = re.compile(r"^[0-9a-f:.]+$")


def looks_like_ip(q: str) -> bool:
    """An IPv4 or IPv6 address, or the start of one: matched as a prefix against sign-in addresses."""
    return bool(_IP.match(q)) and ("." in q or ":" in q)


@dataclass
class Hit:
    label: str  # what was found: Person, Mailbox, Forwarding, ...
    title: str
    detail: str
    href: str
    account: bool = False  # links to a person's page: one result per person

    @property
    def key(self):
        return ("account", self.href) if self.account else (self.label, self.title.lower())


@dataclass
class Group:
    tenant: object
    hits: list[Hit] = field(default_factory=list)
    capped: bool = False  # stopped at PER_TENANT


class Tenant:
    """One tenant's latest snapshot, with lookups the link builders share."""

    def __init__(self, conn, tenant):
        self.snap = TenantSnapshot(conn, tenant)
        self.id = tenant["id"]

    @cached_property
    def users(self) -> dict[str, str]:
        """Sign-in name, address or object ID (lower case) -> object ID of every account."""
        out = {}
        for u in self.snap.objects("users", "user") or []:
            for key in (u.get("userPrincipalName"), u.get("mail"), u["_id"]):
                if key:
                    out.setdefault(str(key).lower(), u["_id"])
        return out

    def person(self, name) -> str | None:
        user_id = self.users.get(str(name or "").lower())
        return url_for("people.person", tenant_id=self.id, user_id=user_id) if user_id else None

    def report(self, key: str, table: str | None = None, column: str | None = None, value=None) -> str:
        href = url_for("reports.data_report", tenant_id=self.id, key=key)
        if table and value:
            href += "?" + urlencode({"f": f"{slug(table)}/{slug(column)}:{value}"})
        return href

    @cached_property
    def overview(self) -> str:
        return url_for("tenants.tenant", tenant_id=self.id)

    @cached_property
    def risky_apps(self) -> set[str]:
        """Names in the Apps report's third-party table (apps that hold permissions)."""
        return {a["name"] for a in app_risks(self.snap) or []}

    @cached_property
    def checked_domains(self) -> set[str]:
        return {d["_id"].lower() for d in self.snap.objects("domain_health", "domain") or []}


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _person(t: Tenant, u: dict, _v: str) -> Hit:
    words = [_text(u.get("userPrincipalName"))]
    if u.get("userType") == "Guest":
        words.append("guest")
    if u.get("accountEnabled") is False:
        words.append("sign-in blocked")
    return Hit(
        "Person",
        _text(u.get("displayName")) or _text(u.get("userPrincipalName")) or u["_id"],
        ", ".join(w for w in words if w),
        t.person(u["_id"]) or t.overview,
        account=True,
    )


def _mailbox(t: Tenant, m: dict, _v: str) -> Hit:
    href = t.person(m.get("ExternalDirectoryObjectId"))
    page = href or url_for("mailboxes.mailbox", tenant_id=t.id, mailbox_id=m["_id"])
    kind = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", _text(m.get("RecipientTypeDetails"))).lower() or "mailbox"
    alias = _text(m.get("Alias"))
    return Hit(
        "Mailbox",
        _text(m.get("DisplayName")) or _text(m.get("PrimarySmtpAddress")),
        ", ".join(w for w in (_text(m.get("PrimarySmtpAddress")), kind, f"alias {alias}" if alias else "") if w),
        page,
        account=bool(href),
    )


def _forwarding(t: Tenant, m: dict, target: str) -> Hit:
    return Hit(
        "Forwarding",
        f"{_text(m.get('PrimarySmtpAddress'))} forwards to {target}",
        "Set by an admin on the mailbox",
        t.report("mailboxes", "Forwarding", "Forwards to", target),
    )


def _rule(t: Tenant, r: dict, target: str) -> Hit:
    state = "" if r.get("isEnabled", True) else " (turned off)"
    return Hit(
        "Inbox rule",
        f"{_text(r.get('mailbox'))} sends mail to {target}",
        f'Inbox rule "{_text(r.get("displayName"))}"{state}',
        t.report("mailboxes", "Forwarding", "Forwards to", target),
    )


def _location(t: Tenant, loc: dict, ip: str) -> Hit:
    user = _text(loc.get("user"))
    href = t.person(user)
    return Hit(
        "Sign-in address",
        f"{user} signed in from {ip}",
        ", ".join(w for w in (_text(loc.get("country")), ", ".join(loc.get("cities") or [])) if w) + " (last 7 days)",
        href or t.report("sign-ins"),
    )


def _failure_ip(t: Tenant, i: dict, ip: str) -> Hit:
    failures, tried = int(i.get("failures") or 0), int(i.get("accounts") or 0)
    listed = tried >= 2 or failures >= sign_in_reports.REPEATED_FAILURES  # the report's own threshold
    return Hit(
        "Failed sign-ins",
        f"{ip}: {plural(failures, 'failed sign-in')} against {plural(tried, 'account')}",
        ", ".join(i.get("users") or []),
        t.report("sign-ins", sign_in_reports.ADDRESSES, "IP address", ip) if listed else t.report("sign-ins"),
    )


def _risky(t: Tenant, r: dict, ip: str) -> Hit:
    return Hit(
        "Risky sign-in",
        f"{_text(r.get('user'))} from {ip}",
        ", ".join(w for w in (_text(r.get("level")).capitalize(), _text(r.get("time"))[:10]) if w),
        t.report("sign-ins", sign_in_reports.RISKY, "IP address", ip),
    )


def _app(t: Tenant, sp: dict, _v: str) -> Hit:
    name = _text(sp.get("displayName")) or sp["_id"]
    publisher = (
        "Microsoft"
        if sp.get("microsoft")
        else (_text(sp.get("verifiedPublisher")) or _text(sp.get("publisherName")) or "Unverified publisher")
    )
    href = t.report("apps", "Third-party apps with access", "App", name) if name in t.risky_apps else t.report("apps")
    return Hit("App", name, f"{publisher}, app ID {_text(sp.get('appId'))}", href)


def _registration(t: Tenant, a: dict, _v: str) -> Hit:
    name = _text(a.get("displayName")) or a["_id"]
    return Hit(
        "App registration",
        name,
        f"Registered in this tenant, app ID {_text(a.get('appId'))}",
        t.report("apps", "App registrations and their credentials", "App", name),
    )


def _managed_device(t: Tenant, d: dict, _v: str) -> Hit:
    name = _text(d.get("deviceName"))
    return Hit(
        "Device",
        name,
        ", ".join(w for w in (_text(d.get("operatingSystem")), _text(d.get("userPrincipalName"))) if w),
        t.report("devices", "Managed devices", "Device", name),
    )


def _entra_device(t: Tenant, d: dict, _v: str) -> Hit:
    name = _text(d.get("displayName"))
    return Hit(
        "Device", name, _text(d.get("operatingSystem")), t.report("devices", "Entra ID device records", "Device", name)
    )


def _domain(t: Tenant, _org: dict, name: str) -> Hit:
    checked = name.lower() in t.checked_domains
    return Hit(
        "Domain",
        name,
        "Verified domain" + ("" if checked else ", not checked in DNS"),
        t.report("domains", "Domains", "Domain", name) if checked else t.overview,
    )


def _group(t: Tenant, g: dict, _v: str) -> Hit:
    words = [group_type(g).lower()]
    if g.get("examined") and isinstance(g.get("memberCount"), int):
        words.append(plural(g["memberCount"], "member"))
    if g.get("guestCount"):
        words.append(plural(g["guestCount"], "guest"))
    return Hit(
        "Group",
        _text(g.get("displayName")) or g["_id"],
        ", ".join(w for w in (_text(g.get("mail")), *words) if w),
        url_for("group_pages.group", tenant_id=t.id, gid=g["_id"]),
    )


def _site(t: Tenant, s: dict, _v: str) -> Hit:
    words = [site_type(s).lower(), size(s.get("storageUsed") or 0)]
    if s.get("anyoneLinks"):
        words.append(plural(s["anyoneLinks"], "anyone link"))
    return Hit("Site", s["url"], ", ".join(words), url_for("site_pages.site", tenant_id=t.id, site_id=site_key(s)))


# How a match of each kind in ``search_index.KINDS`` is shown: (tenant, object, the value that matched).
SHOW: dict[str, Callable[[Tenant, dict, str], Hit]] = {
    "person": _person,
    "group": _group,
    "mailbox": _mailbox,
    "forwarding": _forwarding,
    "site": _site,
    "rule": _rule,
    "failure_ip": _failure_ip,
    "risky": _risky,
    "location": _location,
    "app": _app,
    "registration": _registration,
    "managed_device": _managed_device,
    "entra_device": _entra_device,
    "domain": _domain,
}


def _matches(value, q: str, ip: bool) -> bool:
    text = value if isinstance(value, str) else ""
    if not text:
        return False
    return text.lower().startswith(q) if ip else q in text.lower()


def search_tenant(conn, tenant, q: str, limit: int = PER_TENANT) -> Group:
    """Matches for ``q`` (lower case) in one tenant's latest snapshot, scanned in Python, at most ``limit``."""
    t = Tenant(conn, tenant)
    group = Group(tenant)
    if not t.snap.runs:
        return group
    ip_query = looks_like_ip(q)
    seen = set()
    for kind in KINDS:
        if (kind.ip and not ip_query) or not t.snap.has(kind.collector):
            continue
        for obj in t.snap.objects(kind.collector, kind.kind) or []:
            for value in kind.values(obj):
                if not _matches(value, q, kind.ip):
                    continue
                if _add(group, SHOW[kind.key](t, obj, value), seen, limit):
                    return group
                break  # one result per object and kind
    return group


def _add(group: Group, hit: Hit, seen: set, limit: int) -> bool:
    """Add a hit unless it's a repeat. Returns True when the tenant's cap is reached."""
    if hit.key in seen:
        return False
    seen.add(hit.key)
    group.hits.append(hit)
    if len(group.hits) >= limit:
        group.capped = True
        return True
    return False


def indexed_tenant(conn, tenant, q: str, matches: list, limit: int = PER_TENANT) -> Group:
    """The same results as ``search_tenant``, from the index rows ``search_index.find`` returned for the
    tenant (kind key, run id, object id, value), loading only the objects that matched."""
    t = Tenant(conn, tenant)
    group = Group(tenant)
    ip_query = looks_like_ip(q)
    seen: set = set()
    done: set[tuple[str, str]] = set()  # one result per object and kind
    order = {k.key: i for i, k in enumerate(KINDS)}
    for key, run_id, object_id, value in sorted(matches, key=lambda m: order.get(m[0], len(order))):
        kind = search_index.BY_KEY.get(key)
        if kind is None or (kind.ip and not ip_query) or (key, object_id) in done:
            continue
        obj = search_index.load(conn, run_id, kind, object_id)
        if obj is None:
            continue
        matched = next((v for v in kind.values(obj) if _text(v).lower() == value), None)
        if matched is None:  # the object changed under the index: the fingerprint catches this, but be safe
            continue
        done.add((key, object_id))
        if _add(group, SHOW[key](t, obj, matched), seen, limit):
            return group
    return group


def search(conn, tenants, q: str) -> tuple[list[Group], bool]:
    """Every tenant's matches for ``q`` (lower case), through the index where it's current and by scanning
    where it isn't. Returns (groups with hits, whether the search stopped before the last tenant)."""
    current = search_index.current(conn, tenants)
    found = search_index.find(conn, sorted(current), q, looks_like_ip(q)) if current else {}
    groups, total = [], 0
    for tenant in tenants:
        if tenant["id"] in current:
            group = indexed_tenant(conn, tenant, q, found.get(tenant["id"], []))
        else:
            group = search_tenant(conn, tenant, q)
        if group.hits:
            groups.append(group)
            total += len(group.hits)
        if total >= TOTAL:
            return groups, tenant is not tenants[-1]
    return groups, False


@bp.get("/search")
@roles_required("admin", "analyst")
def index():
    q = request.args.get("q", "").strip()[:MAX_LENGTH]
    tenants = visible_tenants(g.db, g.user)
    one = request.args.get("tenant", type=int)
    if one is not None:
        tenants = [t for t in tenants if t["id"] == one]
        if not tenants:
            abort(404)
    groups, truncated, short = [], False, bool(q) and len(q) < MIN_LENGTH
    if q and not short:
        groups, truncated = search(g.db, tenants, q.lower())
    return render_template(
        "search.html",
        q=q,
        short=short,
        groups=groups,
        truncated=truncated,
        one=tenants[0] if one is not None else None,
        searched=len(tenants),
        shown=PER_TENANT if one is not None else SHOWN,
        min_length=MIN_LENGTH,
    )
