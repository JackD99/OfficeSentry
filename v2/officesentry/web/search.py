"""Search every tenant someone may see, for MSP staff.

One box finds people, mailboxes, where mail is forwarded to, sign-in IP
addresses, apps, devices and domains in each tenant's latest snapshot, so a
responder can answer "which clients forward to this address?" or "who signed in
from this IP?" without opening every tenant. It scans stored objects in Python
(no index); tenants without data are skipped and the scan stops at a cap.

To make something searchable, add a ``Kind`` to ``KINDS``: the collector and
object kind it reads, the values to match in each object, and how to show and
link a match. Links open the person's page, or the detail report filtered to
the row (``f=<table>/<column>:<value>``, see ``reports/views.py``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import cached_property
from urllib.parse import urlencode

from flask import Blueprint, abort, g, render_template, request, url_for

from ..checks.apps import app_risks
from ..mailflow import inbox_rule_risk, mailbox_forwarding
from ..reports import sign_in_reports
from ..reports.views import slug
from ..snapshot import TenantSnapshot
from ..text import plural
from .access import can, needs, visible_tenants

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


# What a search hit from each collector needs: nobody finds what they couldn't open.
COLLECTOR_CAPABILITY = {
    "users": "reports.people",
    "mailboxes": "reports.email",
    "inbox_rules": "reports.email",
    "sign_ins": "reports.identity",
    "app_consents": "reports.devices",
    "devices": "reports.devices",
    "tenant_profile": "reports.tenant",
}


@dataclass(frozen=True)
class Kind:
    collector: str
    kind: str
    values: Callable[[dict], Iterable]  # what a query is matched against
    show: Callable[[Tenant, dict, str], Hit]  # (tenant, object, the value that matched)
    ip: bool = False  # values are IP addresses: matched by prefix, and only for an IP-like query


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
    kind = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", _text(m.get("RecipientTypeDetails"))).lower() or "mailbox"
    alias = _text(m.get("Alias"))
    return Hit(
        "Mailbox",
        _text(m.get("DisplayName")) or _text(m.get("PrimarySmtpAddress")),
        ", ".join(w for w in (_text(m.get("PrimarySmtpAddress")), kind, f"alias {alias}" if alias else "") if w),
        href or t.report("shared-mailboxes"),
        account=bool(href),
    )


def _forwarding(t: Tenant, m: dict, target: str) -> Hit:
    return Hit(
        "Forwarding",
        f"{_text(m.get('PrimarySmtpAddress'))} forwards to {target}",
        "Set by an admin on the mailbox",
        t.report("mailboxes", "Forwarding", "Forwards to", target),
    )


def _rule_targets(r: dict) -> list[str]:
    return inbox_rule_risk(r, set()).forwards_to


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


def _domain_names(org: dict) -> list[str]:
    return [d["name"] for d in org.get("verifiedDomains") or [] if d.get("name")]


def _domain(t: Tenant, _org: dict, name: str) -> Hit:
    checked = name.lower() in t.checked_domains
    return Hit(
        "Domain",
        name,
        "Verified domain" + ("" if checked else ", not checked in DNS"),
        t.report("domains", "Domains", "Domain", name) if checked else t.overview,
    )


# In the order results are listed within a tenant.
KINDS: tuple[Kind, ...] = (
    Kind("users", "user", lambda u: (u.get("displayName"), u.get("userPrincipalName"), u.get("mail")), _person),
    Kind(
        "mailboxes",
        "mailbox",
        lambda m: (
            m.get("DisplayName"),
            m.get("PrimarySmtpAddress"),
            m.get("UserPrincipalName"),
            m.get("Alias"),
            m.get("Name"),
        ),
        _mailbox,
    ),
    Kind("mailboxes", "mailbox", lambda m: (mailbox_forwarding(m),), _forwarding),
    Kind("inbox_rules", "inbox_rule", _rule_targets, _rule),
    Kind("sign_ins", "sign_in_failure_ip", lambda i: (i.get("ip"),), _failure_ip, ip=True),
    Kind("sign_ins", "risky_sign_in", lambda r: (r.get("ip"),), _risky, ip=True),
    Kind("sign_ins", "sign_in_location", lambda loc: loc.get("ips") or (), _location, ip=True),
    Kind("app_consents", "service_principal", lambda sp: (sp.get("displayName"), sp.get("appId")), _app),
    Kind("app_consents", "application", lambda a: (a.get("displayName"), a.get("appId")), _registration),
    Kind("devices", "managed_device", lambda d: (d.get("deviceName"),), _managed_device),
    Kind("devices", "entra_device", lambda d: (d.get("displayName"),), _entra_device),
    Kind("tenant_profile", "organization", _domain_names, _domain),
)


def _matches(value, q: str, ip: bool) -> bool:
    text = value if isinstance(value, str) else ""
    if not text:
        return False
    return text.lower().startswith(q) if ip else q in text.lower()


def search_tenant(conn, tenant, q: str, limit: int = PER_TENANT, allowed=None) -> Group:
    """Matches for ``q`` (lower case) in one tenant's latest snapshot, at most ``limit``, of the kinds
    ``allowed(capability)`` lets this person see (everything when None)."""
    t = Tenant(conn, tenant)
    group = Group(tenant)
    if not t.snap.runs:
        return group
    ip_query = looks_like_ip(q)
    seen = set()
    for kind in KINDS:
        if (kind.ip and not ip_query) or not t.snap.has(kind.collector):
            continue
        if allowed is not None and not allowed(COLLECTOR_CAPABILITY[kind.collector]):
            continue
        for obj in t.snap.objects(kind.collector, kind.kind) or []:
            for value in kind.values(obj):
                if not _matches(value, q, kind.ip):
                    continue
                hit = kind.show(t, obj, value)
                if hit.key in seen:
                    continue
                seen.add(hit.key)
                group.hits.append(hit)
                if len(group.hits) >= limit:
                    group.capped = True
                    return group
                break  # one result per object and kind
    return group


@bp.get("/search")
@needs("clients.compare")
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
        total = 0
        for tenant in tenants:
            group = search_tenant(g.db, tenant, q.lower(), allowed=lambda cap, t=tenant: can(cap, t["id"]))
            if group.hits:
                groups.append(group)
                total += len(group.hits)
            if total >= TOTAL:
                truncated = tenant is not tenants[-1]
                break
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
