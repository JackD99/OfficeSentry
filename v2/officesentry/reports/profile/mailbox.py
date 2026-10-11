"""Mailbox: forwarding, inbox rules, protocols and who can get into it."""

from __future__ import annotations

from ...mailflow import inbox_rule_risk, is_external, mailbox_forwarding, tenant_domains
from ...text import plural as _plural
from ..data_reports import PageLink, Section, _describe_actions, _describe_conditions, _type
from ..mail_reports import _account, _Directory, _grants, _size
from .base import Part, Person, part

PROTOCOLS = (("PopEnabled", "POP"), ("ImapEnabled", "IMAP"), ("ActiveSyncEnabled", "ActiveSync"), ("EwsEnabled", "EWS"))


@part(50)
def mailbox_part(p: Person) -> Part:
    result = Part("mailbox", "Mailbox")
    boxes = p.snap.objects("mailboxes", "mailbox")
    status = p.snap.objects("mailboxes", "exchange_status")
    if status and not status[0].get("available"):
        result.unavailable = "This tenant has no Exchange Online licence."
        return result
    if boxes is None:
        result.unavailable = "Mailboxes haven't been collected yet. Refresh Mailboxes on the client page."
        return result
    users = p.snap.objects("users", "user") or []
    d = _Directory(boxes, users)
    box = d.mailbox(p.id) or next((d.mailbox(k) for k in p.keys if d.mailbox(k)), None)
    domains = tenant_domains(p.snap) or set()
    if box is not None:
        result.facts.append(("Address", box.get("PrimarySmtpAddress")))
        result.facts.append(("Type", _type(box.get("RecipientTypeDetails"))))
        sizes = {s.get("mailboxId"): s for s in p.snap.objects("mailbox_usage", "mailbox_size") or []}
        s = sizes.get(box["_id"])
        if s and s.get("totalBytes") is not None:
            quota = s.get("sendQuotaBytes")
            used = f"{_size(s['totalBytes'])}" + (
                f" of {_size(quota)} ({100 * s['totalBytes'] / quota:.0f}%)" if quota else ""
            )
            result.facts.append(("Size", used))
            if quota and s["totalBytes"] >= 0.9 * quota:
                p.flag(
                    "medium",
                    f"The mailbox is {100 * s['totalBytes'] / quota:.0f}% full and stops sending at {_size(quota)}.",
                    "mailbox",
                )
            result.summary = _size(s["totalBytes"])
        result.facts.append(("Archive", "On" if box.get("ArchiveStatus") not in (None, "", "None") else "Off"))
        result.facts.append(("Litigation hold", "On" if box.get("LitigationHoldEnabled") else "Off"))
        target = mailbox_forwarding(box)
        if target:
            outside = is_external(target, domains)
            result.facts.append(
                (
                    "Forwarding",
                    f"{target}{' (outside the organisation)' if outside else ''}"
                    + ("" if box.get("DeliverToMailboxAndForward") else ", no copy kept"),
                )
            )
            if outside:
                p.flag("high", f"The mailbox forwards all mail to {target}, outside the organisation.", "mailbox")
                result.summary, result.tone = "Forwards outside", "bad"
        else:
            result.facts.append(("Forwarding", "None"))
        if box.get("HiddenFromAddressListsEnabled"):
            result.facts.append(("Hidden from the address book", "Yes"))
        cas = {c["_id"]: c for c in p.snap.objects("mailboxes", "cas_mailbox") or []}.get(box["_id"])
        if cas is not None:
            allowed = [name for key, name in PROTOCOLS if cas.get(key)]
            if cas.get("SmtpClientAuthenticationDisabled") is False:
                allowed.append("SMTP AUTH")
            result.facts.append(("Older protocols allowed", ", ".join(allowed) or "None"))
        result.summary = result.summary or _type(box.get("RecipientTypeDetails"))
        result.facts.append(
            (
                "More",
                PageLink("Everything about this mailbox", f"/tenants/{p.snap.tenant['id']}/mailboxes/{box['_id']}"),
            )
        )

    rules = [
        r
        for r in p.snap.objects("inbox_rules", "inbox_rule") or []
        if r.get("mailboxId") == p.id or p.named(r.get("mailbox"))
    ]
    if rules:
        table = Section("Inbox rules", ["Rule", "Enabled", "What it does", "Why it stands out"])
        for r in sorted(rules, key=lambda r: r.get("sequence") or 0):
            risk = inbox_rule_risk(r, domains)
            table.add(
                [
                    r.get("displayName"),
                    r.get("isEnabled", True),
                    f"{_describe_conditions(r.get('conditions') or {})}: {_describe_actions(r)}",
                    "; ".join(risk.reasons),
                ],
                risk.severity,
            )
            if risk.severity in ("high", "medium"):
                p.flag(risk.severity, f'Inbox rule "{r.get("displayName")}": {"; ".join(risk.reasons)}.', "mailbox")
        result.tables.append(table)

    grants = _grants(p.snap, d)
    into = [g for g in grants if box is not None and g[0] is not None and g[0]["_id"] == box["_id"]]
    if into:
        table = Section("Who else can open or send from it", ["Person", "Access", "Account"])
        for _, _, person, user, right in sorted(into, key=lambda g: (g[2].lower(), g[4])):
            words, flag = _account(user, person, domains)
            table.add([person, right, words], flag)
            if flag == "high":
                p.flag("high", f"{person} ({words.lower()}) has {right.lower()} to this mailbox.", "mailbox")
        result.tables.append(table)
    out = [g for g in grants if (g[3] or {}).get("_id") == p.id or (g[3] is None and p.named(g[2]))]
    out = [g for g in out if box is None or g[0] is None or g[0]["_id"] != box["_id"]]
    if out:
        table = Section("Mailboxes this person can open or send from", ["Mailbox", "Type", "Access"])
        rights: dict[str, tuple[dict | None, list[str]]] = {}
        for b, label, _, _, right in out:
            entry = rights.setdefault(label, (b, []))
            if right not in entry[1]:
                entry[1].append(right)
        for label, (b, held) in sorted(rights.items(), key=lambda kv: kv[0].lower()):
            table.add([label, _type((b or {}).get("RecipientTypeDetails")), held])
        result.tables.append(table)
        if not p.enabled:
            p.flag(
                "low",
                f"Sign-in is blocked but the account can still open or send from "
                f"{_plural(len({g[1] for g in out}), 'other mailbox', 'other mailboxes')}.",
                "mailbox",
            )
    if box is None and not result.tables:
        result.summary, result.empty = "None", "No mailbox, and no access to anyone else's."
    elif box is None:
        result.summary = "No mailbox of its own"
    return result
