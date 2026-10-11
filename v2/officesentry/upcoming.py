"""Coming up: what will become a problem in the next 30, 60 or 90 days, across tenants.

Built from stored data only (nothing here calls Microsoft), for the Coming up page
(``web/upcoming.py``, ``/upcoming``) and the weekly Coming up email (a digest
schedule whose ``reports`` is ``["upcoming"]``, see ``delivery.build_upcoming_email``).
Each item has a date when it becomes a problem, and is sorted into a window:

* **Subscriptions**: renewals, trials ending, and subscriptions in their grace
  period, suspended or locked (those need attention now, whatever their date).
* **App secrets and certificates** on the client tenant's app registrations,
  including any that expired in the last 30 days.
* **Office Sentry's own certificates** (``cert_lifecycle``): each app connection's
  and the mail app's. Only for admins and the MSP team's email.
* **Mailboxes near their quota**: at 90% of the send limit now, or forecast to
  reach it from their growth since a collection two to eight weeks earlier.
* **DMARC still at p=none**: no date of its own, so listed under "No date" with
  how long it has been monitor-only.
* **Accepted risks** whose review date comes up, after which they count again.

Paused and archived tenants are left out, as in the tenant digest. Callers pass
only the tenants the person may see (``access.visible_tenants``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from . import cert_lifecycle, dnscheck, mailer, store
from .checks import RULES
from .devices import parse_ts
from .licences import is_free, sku_name
from .reports.data_reports import DataReport, Rows, Section, Tile, UserLink
from .reports.subscriptions import WHAT_HAPPENS
from .snapshot import TenantSnapshot
from .text import day, plural

log = logging.getLogger(__name__)

KEY = "upcoming"
TITLE = "Coming up"
DESCRIPTION = (
    "What will need doing in the next 90 days across your clients: renewals and trials, expiring app "
    "secrets and certificates, mailboxes filling up, DMARC still at p=none and accepted risks due for "
    "review."
)
HORIZON = 90  # days ahead
EXPIRED_DAYS = 30  # app credentials that expired this recently are still listed
QUOTA_NEAR = 0.9  # a mailbox at this share of its send limit needs attention now
BASELINE_DAYS = (14, 56)  # the growth forecast compares with a collection this many days old

# Windows, in the order the page and email show them.
NOW, DAYS_30, DAYS_60, DAYS_90, NO_DATE = "Now", "Next 30 days", "31 to 60 days", "61 to 90 days", "No date"
WINDOWS = (NOW, DAYS_30, DAYS_60, DAYS_90, NO_DATE)

# Item types, as the Type column and the email show them.
SUBSCRIPTION = "Subscription"
TRIAL = "Trial"
APP_KEY_TYPE = "App secret"  # (named so the linter doesn't take the label for a password)
APP_CERTIFICATE = "App certificate"
OUR_CERTIFICATE = "Office Sentry certificate"
QUOTA = "Mailbox quota"
DMARC = "DMARC"
RISK = "Accepted risk"
TYPES = (OUR_CERTIFICATE, SUBSCRIPTION, TRIAL, APP_KEY_TYPE, APP_CERTIFICATE, QUOTA, DMARC, RISK)

SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
RULE_TITLES = {r.key: r.title for r in RULES}
LAPSING = {"Warning": "Grace period", "Suspended": "Suspended", "LockedOut": "Locked out"}
_ISO = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class Item:
    tenant: dict | None  # None for Office Sentry's own certificates
    type: str
    title: str
    detail: str  # what happens, or what to do, in words
    due: date | None
    days: int | None  # until due; negative once it has passed
    severity: str  # high, medium or low
    now: bool = False  # needs attention now whatever the date (lapsing, nearly full)
    href: str = ""  # a path in the portal to see or fix it

    @property
    def window(self) -> str:
        if self.now or (self.days is not None and self.days < 0):
            return NOW
        if self.days is None:
            return NO_DATE
        if self.days <= 30:
            return DAYS_30
        return DAYS_60 if self.days <= 60 else DAYS_90

    @property
    def when(self) -> str:
        """'in 12 days', 'today', 'tomorrow', '3 days ago', or '' without a date."""
        if self.days is None:
            return ""
        if self.days < 0:
            return f"{plural(-self.days, 'day')} ago"
        return {0: "today", 1: "tomorrow"}.get(self.days, f"in {plural(self.days, 'day')}")

    @property
    def tenant_name(self) -> str:
        return self.tenant["name"] if self.tenant else "Office Sentry"

    @property
    def sort_key(self) -> tuple:
        return (
            WINDOWS.index(self.window),
            self.due or date.max,
            SEVERITY_RANK.get(self.severity, 3),
            self.tenant_name.lower(),
            self.title.lower(),
        )


@dataclass
class Upcoming:
    items: list[Item]
    generated_at: datetime
    tenants: int
    without: list[tuple[dict, str]] = field(default_factory=list)  # tenants a source couldn't be read for

    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(WINDOWS, 0)
        for i in self.items:
            out[i.window] += 1
        return out

    def in_window(self, window: str) -> list[Item]:
        return [i for i in self.items if i.window == window]

    @property
    def dated(self) -> list[Item]:
        return [i for i in self.items if i.window != NO_DATE]

    @property
    def soonest(self) -> Item | None:
        """What to lead with: the first item that needs attention now, else the next one due."""
        return next(iter(self.dated), None)

    def by_type(self) -> list[tuple[str, int]]:
        return [(t, n) for t in TYPES if (n := sum(i.type == t for i in self.items))]


def _days(when: date, today: date) -> int:
    return (when - today).days


# -- Each kind of item ------------------------------------------------------------------------


def _subscriptions(snap: TenantSnapshot, tenant, today: date) -> list[Item]:
    subs = snap.objects("tenant_profile", "company_subscription") or []
    skus = {
        s.get("skuId"): s
        for s in snap.objects("tenant_profile", "subscribed_sku") or []
        if isinstance(s.get("skuId"), str)
    }
    href = f"/tenants/{tenant['id']}/data/subscriptions"
    out = []
    for s in subs:
        status = s.get("status")
        when = parse_ts(s.get("nextLifecycleDateTime"))
        sku = skus.get(s.get("skuId")) or {}
        if when is None or status not in ("Enabled", *LAPSING) or (sku and is_free(sku)):
            continue
        due = when.date()
        days = _days(due, today)
        lapsing = LAPSING.get(status)
        if not lapsing and days > HORIZON:
            continue
        if not lapsing and days < 0:
            continue  # a renewal date Microsoft hasn't moved on yet; the next collection will
        name = sku_name(s.get("skuPartNumber") or sku.get("skuPartNumber"))
        total = s.get("totalLicenses")
        used = sku.get("consumedUnits") if isinstance(sku.get("consumedUnits"), int) else 0
        licences = f"{plural(total, 'licence')}. " if isinstance(total, int) and total else ""
        if lapsing:
            out.append(
                Item(
                    tenant,
                    SUBSCRIPTION,
                    f"{name} ({lapsing.lower()})",
                    licences + WHAT_HAPPENS[lapsing],
                    due,
                    days,
                    "high",
                    now=True,
                    href=href,
                )
            )
        elif s.get("isTrial") is True:
            in_use = f"{plural(used, 'person', 'people')} {'uses' if used == 1 else 'use'} it. " if used else ""
            severity = ("high" if days <= 30 else "medium") if used else "low"
            out.append(
                Item(
                    tenant, TRIAL, f"{name} trial ends", in_use + WHAT_HAPPENS["Trial"], due, days, severity, href=href
                )
            )
        else:
            out.append(
                Item(
                    tenant,
                    SUBSCRIPTION,
                    f"{name} renews",
                    licences + WHAT_HAPPENS["Active"],
                    due,
                    days,
                    "medium" if days <= 30 else "low",
                    href=href,
                )
            )
    return out


def _app_credentials(snap: TenantSnapshot, tenant, today: date) -> list[Item]:
    href = f"/tenants/{tenant['id']}/data/apps"
    out = []
    for a in snap.objects("app_consents", "application") or []:
        if str(a.get("appId") or "").lower() in snap.own_app_ids:
            continue  # Office Sentry's own app; its certificate is listed once, as ours
        creds = [c for c in a.get("credentials") or [] if isinstance(c, dict)]
        for c in creds:
            end = parse_ts(c.get("endDateTime"))
            if end is None:
                continue
            days = _days(end.date(), today)
            if days > HORIZON or days < -EXPIRED_DAYS:
                continue
            kind = APP_KEY_TYPE if c.get("type") == "secret" else APP_CERTIFICATE
            # Another credential of the same kind that lasts beyond this one usually means it's been replaced.
            later = [
                o
                for o in creds
                if o is not c
                and o.get("type") == c.get("type")
                and (e := parse_ts(o.get("endDateTime")))
                and e > end
                and _days(e.date(), today) > HORIZON
            ]
            noun = "secret" if kind == APP_KEY_TYPE else "certificate"
            label = f"“{c['displayName']}” " if isinstance(c.get("displayName"), str) and c["displayName"] else ""
            app = a.get("displayName") or a.get("appId") or a.get("_id") or "An app"
            if later:
                detail = (
                    f"A newer {noun} lasts until {day(later[0]['endDateTime'])}, so this one has probably been "
                    f"replaced. Remove it from the app registration once nothing uses it."
                )
                severity = "low"
            elif days < 0:
                detail = (
                    f"Expired. Whatever signs in with it has stopped working, unless the app is no longer used. "
                    f"Add a new {noun} in Entra, or delete the app registration."
                )
                severity = "high"
            else:
                detail = (
                    f"Anything that signs in with it stops working then. Add a new {noun} to the app "
                    "registration in Entra and update whatever uses it."
                )
                severity = "high" if days <= 30 else "medium"
            out.append(Item(tenant, kind, f"{app}: {label}{noun}", detail, end.date(), days, severity, href=href))
    return out


def _gb(n: float) -> str:
    return f"{n / 1024**3:.1f} GB"


def _baseline(conn, tenant_id: int, now: datetime) -> tuple[dict[str, int], datetime] | None:
    """Mailbox sizes from a collection two to eight weeks old (the oldest such), for the growth forecast."""
    newest, oldest = ((now - timedelta(days=d)).strftime(_ISO) for d in BASELINE_DAYS)
    run = conn.execute(
        "SELECT id, finished_at FROM runs WHERE tenant_id = ? AND collector = 'mailbox_usage'"
        " AND status IN ('succeeded', 'partial') AND finished_at BETWEEN ? AND ?"
        " ORDER BY finished_at, id LIMIT 1",
        (tenant_id, oldest, newest),
    ).fetchone()
    when = parse_ts(run["finished_at"]) if run else None
    if when is None:
        return None
    sizes = {
        m["_id"]: m["totalBytes"]
        for m in store.run_objects(conn, run["id"], "mailbox_size")
        if isinstance(m.get("totalBytes"), int)
    }
    return (sizes, when) if sizes else None


def _mailboxes(snap: TenantSnapshot, tenant, now: datetime, today: date) -> list[Item]:
    rows = snap.objects("mailbox_usage", "mailbox_size") or []
    if not rows:
        return []
    base = _baseline(snap.conn, tenant["id"], now)
    href = f"/tenants/{tenant['id']}/data/mailbox-size"
    out = []
    for m in rows:
        used, quota = m.get("totalBytes"), m.get("sendQuotaBytes")
        if not isinstance(used, int) or not isinstance(quota, int) or quota <= 0:
            continue  # unlimited, or not read
        name = m.get("mailbox") if isinstance(m.get("mailbox"), str) and m.get("mailbox") else m["_id"]
        share = used / quota
        sizes = f"{_gb(used)} of {_gb(quota)} ({share:.0%})"
        if share >= 1:
            out.append(
                Item(
                    tenant,
                    QUOTA,
                    f"{name} is full",
                    f"{sizes}. It can't send mail until it's cleaned up "
                    "or given more room (an archive, or a bigger licence).",
                    None,
                    None,
                    "high",
                    now=True,
                    href=href,
                )
            )
            continue
        rate = None
        if base:
            before, then = base
            elapsed = (now - then).total_seconds() / 86400
            grown = used - before.get(m["_id"], used)
            if grown > 0 and elapsed >= 1:
                rate = grown / elapsed
        full = int((quota - used) / rate) if rate else None
        if share >= QUOTA_NEAR:
            due = today + timedelta(days=full) if full is not None and full <= HORIZON else None
            out.append(
                Item(
                    tenant,
                    QUOTA,
                    f"{name} is nearly full",
                    f"{sizes}."
                    + (f" At its current growth it's full around {day(due.isoformat())}." if due else "")
                    + " It stops sending mail when it's full: turn on its archive or clean it up.",
                    due,
                    _days(due, today) if due else None,
                    "high" if due and full <= 30 else "medium",
                    now=True,
                    href=href,
                )
            )
        elif full is not None and full <= HORIZON:
            due = today + timedelta(days=full)
            out.append(
                Item(
                    tenant,
                    QUOTA,
                    f"{name} fills up",
                    f"{sizes}, growing about {_gb(rate * 30)} a month. It stops sending mail when it's "
                    "full: turn on its archive or clean it up before then.",
                    due,
                    full,
                    "medium" if full <= 30 else "low",
                    href=href,
                )
            )
    return out


def _dmarc(snap: TenantSnapshot, tenant, now: datetime) -> list[Item]:
    rows = snap.objects("domain_health", "domain") or []
    href = f"/tenants/{tenant['id']}/data/domains"
    since = {
        r["object_id"]: r
        for r in snap.conn.execute(
            "SELECT object_id, first_seen, accepted_at, accepted_until FROM findings WHERE tenant_id = ?"
            " AND rule_key = 'dmarc_policy' AND resolved_at IS NULL",
            (tenant["id"],),
        )
    }
    out = []
    for row in rows:
        h = dnscheck.DomainHealth.from_dict(row)
        if not h.sends_mail or h.dmarc_policy != "none":
            continue
        finding = since.get(f"{h.domain}:DMARC")
        if (
            finding
            and finding["accepted_at"]
            and (finding["accepted_until"] is None or finding["accepted_until"] > now.strftime(_ISO))
        ):
            continue  # accepted as it is; its review date, if any, is listed as an accepted risk
        start = parse_ts(finding["first_seen"]) if finding else None
        detail = "Monitor-only: spoofed mail from the domain is still delivered."
        if start:
            detail = (
                f"Monitor-only since {day(finding['first_seen'])} ({plural((now - start).days, 'day')}): "
                "spoofed mail from the domain is still delivered."
            )
        out.append(
            Item(
                tenant,
                DMARC,
                f"{h.domain} DMARC is p=none",
                detail + " Check the DMARC reports, then move to p=quarantine and later p=reject.",
                None,
                None,
                "medium",
                href=href,
            )
        )
    return out


def _accepted_risks(conn, tenant, now: datetime, today: date) -> list[Item]:
    until = (now + timedelta(days=HORIZON + 1)).strftime(_ISO)
    href = f"/tenants/{tenant['id']}/findings"
    out = []
    for f in conn.execute(
        "SELECT * FROM findings WHERE tenant_id = ? AND resolved_at IS NULL AND accepted_at IS NOT NULL"
        " AND accepted_until > ? AND accepted_until < ? ORDER BY accepted_until",
        (tenant["id"], now.strftime(_ISO), until),
    ):
        due = parse_ts(f["accepted_until"])
        if due is None:
            continue
        days = _days(due.date(), today)
        who = f" by {f['accepted_by']}" if f["accepted_by"] else ""
        out.append(
            Item(
                tenant,
                RISK,
                f"{RULE_TITLES.get(f['rule_key'], f['rule_key'])}: {f['subject']}",
                f"Accepted{who}. Review it by then: after that it counts as an open finding again.",
                due.date(),
                days,
                "medium" if days <= 30 else "low",
                href=href,
            )
        )
    return out


def _our_certificates(conn, now: datetime, today: date) -> list[Item]:
    found = [cert_lifecycle.connection_expiry(c, now) for c in store.list_connections(conn) if c["client_id"]]
    found.append(cert_lifecycle.mail_expiry(mailer.mail_settings(conn), now))
    out = []
    for e in found:
        if e is None or e.days > HORIZON:
            continue
        end = parse_ts(e.not_after)
        due = end.date() if end else today + timedelta(days=e.days)
        what = "The mail app's certificate" if e.kind == "mail" else f"The {e.name} certificate"
        href = "/admin/email" if e.kind == "mail" else f"/admin/connections#conn-{e.id}"
        out.append(
            Item(
                None,
                OUR_CERTIFICATE,
                what,
                f"{e.consequence} {cert_lifecycle.fix_steps(e)}",
                due,
                _days(due, today),
                "high" if e.days <= 30 else "medium",
                href=href,
            )
        )
    return out


PARTS = (
    ("Subscriptions", lambda snap, t, now, today: _subscriptions(snap, t, today)),
    ("App secrets and certificates", lambda snap, t, now, today: _app_credentials(snap, t, today)),
    ("Mailbox quotas", lambda snap, t, now, today: _mailboxes(snap, t, now, today)),
    ("DMARC", lambda snap, t, now, today: _dmarc(snap, t, now)),
    ("Accepted risks", lambda snap, t, now, today: _accepted_risks(snap.conn, t, now, today)),
)


def collect(conn, tenants, now: datetime | None = None, *, ours: bool = False) -> Upcoming:
    """Everything coming up for ``tenants`` (paused and archived ones are left out), soonest first.

    ``ours`` adds Office Sentry's own certificates (admins and the MSP team only). A part that fails on one
    tenant's data is left out for that tenant and named in ``without``, so one bad snapshot never hides the rest.
    """
    now = now or datetime.now(UTC)
    today = now.date()
    items: list[Item] = []
    without: list[tuple[dict, str]] = []
    active = [t for t in tenants if t["enabled"] and not t["archived_at"]]
    for t in active:
        tenant = dict(t)
        snap = TenantSnapshot(conn, t)
        for title, part in PARTS:
            try:
                items += part(snap, tenant, now, today)
            except Exception:
                log.exception("Coming up: %s for tenant %s failed", title, tenant["id"])
                without.append((tenant, title))
    if ours:
        items += _our_certificates(conn, now, today)
    items.sort(key=lambda i: i.sort_key)
    return Upcoming(items, now, len(active), without)


# -- As a table ---------------------------------------------------------------------------------

WHEN = "When"
DUE = "Due"
TABLE = "Coming up"


def report(up: Upcoming) -> DataReport:
    """The items as one table for the page (``_report_table``, filters and sort) and the downloads."""
    data = DataReport(
        KEY,
        TITLE,
        DESCRIPTION,
        {"id": 0, "name": "All clients", "tenant_ref": plural(up.tenants, "client")},
        up.generated_at,
    )
    section = Section(
        TABLE,
        [WHEN, DUE, "In", "Client", "Type", "What", "Details"],
        intro="Everything due in the next 90 days, soonest first, then what has no date.",
        empty="Nothing is due in the next 90 days.",
        compare=False,
        flag_name="Priority",
        group=WHEN,
        filter_columns=(WHEN, "Client", "Type"),
        review=False,
    )
    for i in up.items:
        tenant = UserLink(i.tenant["name"], f"/tenants/{i.tenant['id']}") if i.tenant else "Office Sentry"
        what = UserLink(i.title, i.href) if i.href else i.title
        section.add([i.window, i.due.isoformat() if i.due else "", i.when, tenant, i.type, what, i.detail], i.severity)
    data.sections = [section]
    counts = up.counts()
    data.tiles = [
        Tile(
            "Needs attention now",
            counts[NOW],
            "Expired, lapsing or nearly full",
            "bad" if counts[NOW] else "ok",
            Rows(TABLE, {WHEN: NOW}),
        ),
        Tile(
            DAYS_30,
            counts[DAYS_30],
            "Due within a month",
            "warn" if counts[DAYS_30] else "ok",
            Rows(TABLE, {WHEN: DAYS_30}),
        ),
        Tile(DAYS_60, counts[DAYS_60], "Due in the second month", None, Rows(TABLE, {WHEN: DAYS_60})),
        Tile(DAYS_90, counts[DAYS_90], "Due in the third month", None, Rows(TABLE, {WHEN: DAYS_90})),
    ]
    if counts[NO_DATE]:
        data.tiles.append(Tile(NO_DATE, counts[NO_DATE], "DMARC still at p=none", "warn", Rows(TABLE, {WHEN: NO_DATE})))
    data.notes = [f"{t['name']}: {part} couldn't be read from the latest data." for t, part in up.without]
    return data
