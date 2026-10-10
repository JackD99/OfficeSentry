"""Microsoft 365 usage: when the person last used each service and the Office apps."""

from __future__ import annotations

from ...licences import load_prices, sku_name
from ...text import plural as _plural
from ..data_reports import Section
from ..identity_reports import Money
from .base import Part, Person, _currency, _day, part
from .licences import _licences


@part(15)
def usage_part(p: Person) -> Part | None:
    from ... import usage

    status = usage.status(p.snap)
    if status is None:
        return None  # usage isn't collected for this tenant yet; leave the area out
    result = Part("usage", "Microsoft 365 use")
    if not status.get("available"):
        result.unavailable = "Usage reports weren't readable. Check Reports.Read.All consent on the client page."
        return result
    if status.get("concealed"):
        result.unavailable = usage.CONCEALED_NOTICE
        return result
    row = usage.person(p.snap, p.upn)
    days = status.get("days") or usage.ACTIVE_DAYS
    if row is None:
        result.summary, result.empty = "Nothing recorded", f"Not in Microsoft's usage reports for the last {days} days."
        return result
    used = usage.used_services(row)
    for key, name in usage.SERVICES:
        last = (row.get("lastActive") or {}).get(key)
        if (row.get("active") or {}).get(key):
            text = f"Used (last {_day(last)})" if last else "Used"
        else:
            text = f"Not in the last {days} days" + (f" (last {_day(last)})" if last else "")
        result.facts.append((name, text))
    platforms = row.get("platforms")
    if platforms is not None:
        on = [name for key, name in usage.PLATFORMS if platforms.get(key)]
        result.facts.append(("Office apps used on", ", ".join(on) or "None"))
    if row.get("email"):
        e = row["email"]
        result.facts.append(("Email", f"{e.get('sent') or 0:,} sent, {e.get('received') or 0:,} received"))
    if row.get("teamsCounts"):
        t = row["teamsCounts"]
        result.facts.append(
            (
                "Teams",
                f"{_plural(t.get('messages') or 0, 'message')}, "
                f"{_plural(t.get('meetings') or 0, 'meeting')}, "
                f"{_plural(t.get('calls') or 0, 'call')}",
            )
        )
    if row.get("activations"):
        table = Section("Office installs", ["Product", "Windows", "Mac", "Mobile", "Last activated"])
        for a in row["activations"]:
            table.add(
                [
                    a.get("product"),
                    a.get("windows") or 0,
                    a.get("mac") or 0,
                    a.get("mobile") or 0,
                    _day(a.get("lastActivated")),
                ]
            )
        result.tables.append(table)

    _, skus = _licences(p)
    sku_parts = {sid: s.get("skuPartNumber") or "" for sid, s in skus.items()}
    held = usage.paid_parts(row, p.user, sku_parts).parts
    box = {s.get("mailboxId"): s for s in p.snap.objects("mailbox_usage", "mailbox_size") or []}.get(p.id)
    prices = load_prices(p.snap.conn)
    currency = _currency(p.snap)
    suggestions = usage.rightsize(row, held, box) if p.enabled else []
    if suggestions:
        table = Section("Cheaper licence options", ["Licence", "Option", "Why", "Saving / month"])
        for sg in suggestions:
            saving = sg.saving(prices)
            table.add(
                [
                    sku_name(sg.current),
                    sg.action,
                    sg.why + (f". {sg.caution}" if sg.caution else ""),
                    Money(saving, currency) if saving else "",
                ],
                "low",
            )
            p.flag(
                "low",
                f"{sku_name(sg.current)}: {sg.action.lower() if sg.target == '' else sg.action}. {sg.why}."
                + (f" Saves {Money(saving, currency)} a month." if saving else ""),
                "usage",
            )
        result.tables.append(table)
    if used:
        result.summary = ", ".join(used)
    else:
        result.summary, result.tone = f"No activity in {days} days", "warn" if held else None
    result.note = "From Microsoft's usage reports, which run a couple of days behind" + (
        f" (as of {_day(status.get('reportDate'))})." if status.get("reportDate") else "."
    )
    return result
