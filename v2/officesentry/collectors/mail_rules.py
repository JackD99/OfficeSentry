"""Inbox rules for every mailbox, read through Graph (MailboxSettings.Read).

Graph is used rather than Exchange's Get-InboxRule so this works even before a
tenant assigns the app an Exchange role. Graph only gives a folder's ID, and
naming it needs mail access the app doesn't have, so folder names come from
Get-InboxRule, for mailboxes with a rule that moves mail.
"""

from __future__ import annotations

import base64
import binascii

from .. import store
from ..exchange import ExchangeAccessDenied, ExchangeError
from ..graph import GraphError, PermissionMissing
from ..text import plural
from . import mailbox_window
from .base import Collector, NothingToRead, RunContext, register

MAILBOX_TYPES = {"UserMailbox", "SharedMailbox"}
# What Graph answers for an account with no Exchange Online mailbox (or one hosted on-premises).
NO_MAILBOX_CODES = {"MailboxNotEnabledForRESTAPI", "ResourceNotFound", "ErrorInvalidUser"}


def _targets(ctx: RunContext) -> list[dict]:
    """Mailboxes to read: from the Exchange snapshot, else licensed enabled users."""
    run = store.latest_run(ctx.conn, ctx.tenant["id"], "mailboxes")
    if run:
        boxes = store.run_objects(ctx.conn, run["id"], "mailbox")
        if boxes:
            return [
                {
                    "id": m["ExternalDirectoryObjectId"],
                    "upn": m.get("UserPrincipalName") or m.get("PrimarySmtpAddress"),
                    "type": m.get("RecipientTypeDetails"),
                }
                for m in boxes
                if m.get("ExternalDirectoryObjectId") and m.get("RecipientTypeDetails") in MAILBOX_TYPES
            ]
    run = store.latest_run(ctx.conn, ctx.tenant["id"], "users")
    if run is None:
        raise NothingToRead(
            "Neither mailboxes nor users have been collected for this client yet, so there were no mailboxes to "
            "read. The next collection reads them first, then this."
        )
    users = store.run_objects(ctx.conn, run["id"], "user")
    return [
        {"id": u["_id"], "upn": u.get("userPrincipalName"), "type": "UserMailbox"}
        for u in users
        if u.get("licensed") and u.get("accountEnabled")
    ]


@register
class InboxRules(Collector):
    key = "inbox_rules"
    title = "Inbox rules"
    description = "Every mailbox's inbox rules, to spot forwarding and rules that hide mail."
    permissions = ("MailboxSettings.Read",)

    def collect(self, ctx: RunContext) -> None:
        if ctx.capabilities.get("exchange") is False:
            ctx.log("Tenant has no Exchange Online licence; nothing to collect")
            ctx.objects("inbox_rule", [])
            return
        window = mailbox_window.plan(ctx, self.key, "inbox_rule", _targets(ctx), lambda t: t["id"], "inbox rules")
        targets = window.mailboxes

        rows, failed, denied, no_mailbox = [], 0, 0, 0
        exchange_ok = True  # until Exchange refuses the app, then no more folder names this run
        for t in targets:
            try:
                rules = ctx.paged(ctx.graph.get_all(f"/users/{t['id']}/mailFolders/inbox/messageRules"))
            except PermissionMissing:
                denied += 1
                continue
            except GraphError as exc:
                # Without a mailbox snapshot (no Global Reader in Exchange) every licensed account is tried,
                # and one with no Exchange Online mailbox (licensed for other apps, or on-premises) isn't a
                # failure: it has no inbox rules to read. Counting it made those runs partial every night.
                if exc.status == 404 or exc.code in NO_MAILBOX_CODES:
                    no_mailbox += 1
                else:
                    failed += 1
                continue
            folders = {}
            if exchange_ok and any((r.get("actions") or {}).get("moveToFolder") for r in rules):
                folders, exchange_ok = _folder_names(ctx, t)
            for r in rules:
                folder_id = (r.get("actions") or {}).get("moveToFolder")
                rows.append(
                    (
                        f"{t['id']}:{r['id']}",
                        {
                            "mailboxId": t["id"],
                            "mailbox": t["upn"],
                            "mailboxType": t["type"],
                            "displayName": r.get("displayName"),
                            "isEnabled": r.get("isEnabled", True),
                            "hasError": r.get("hasError"),
                            "sequence": r.get("sequence"),
                            "conditions": r.get("conditions") or {},
                            "actions": r.get("actions") or {},
                            "moveToFolderName": _folder_of(r, folders) if folder_id else None,
                        },
                    )
                )
        if targets and denied == len(targets):
            raise PermissionMissing(
                403,
                "Authorization_RequestDenied",
                "Reading inbox rules was refused for every mailbox; check MailboxSettings.Read "
                "consent and any Exchange application access policy",
            )
        if denied or failed:
            ctx.warn(f"Could not read inbox rules for {denied + failed} of {plural(len(targets), 'mailbox')}")
        if no_mailbox:
            ctx.log(f"{plural(no_mailbox, 'account')} had no Exchange Online mailbox to read")
        rows += window.carried
        ctx.objects("inbox_rule", rows)
        window.save(ctx)
        ctx.metric("mail.inbox_rules", len(rows))
        # Mailboxes the stored rules cover: read this run, or kept from an earlier run.
        ctx.metric("mail.mailboxes_checked", len(targets) - denied - failed - no_mailbox + window.carried_mailboxes)


def _folder_names(ctx: RunContext, target: dict) -> tuple[dict[str, str], bool]:
    """Each rule's destination folder, by rule identity and by name, from Get-InboxRule, and whether
    Exchange let the app in."""
    try:
        found = ctx.exchange.invoke("Get-InboxRule", {"Mailbox": target["id"]})
    except ExchangeAccessDenied:
        ctx.log("Inbox rule folder names need the app's Global Reader role in Exchange", "warning")
        return {}, False
    except ExchangeError as exc:
        ctx.log(f"Could not read folder names for {target['upn']}'s inbox rules: {exc.message}", "warning")
        return {}, True
    names = {}
    for rule in found:
        folder = _folder(rule.get("MoveToFolder"))
        if folder:
            names[str(rule.get("RuleIdentity"))] = folder
            names.setdefault(rule.get("Name"), folder)
    return names, True


def _folder_of(rule: dict, folders: dict[str, str]) -> str | None:
    """Graph's rule ID is Exchange's RuleIdentity as 8 little-endian bytes in base64, written with - for /
    (seen on a real tenant); the rule's name is the fallback."""
    graph_id = str(rule.get("id") or "")
    for text in (graph_id.replace("-", "/").replace("_", "+"), graph_id.replace("-", "+").replace("_", "/")):
        try:
            raw = base64.b64decode(text + "==")
        except (binascii.Error, ValueError):
            continue
        if len(raw) == 8 and str(int.from_bytes(raw, "little")) in folders:
            return folders[str(int.from_bytes(raw, "little"))]
    return folders.get(rule.get("displayName"))


def _folder(value: str | None) -> str | None:
    """'Deleted Items', or a folder in another mailbox as 'mailbox:\\Deleted Items'."""
    if not value:
        return None
    return str(value).split(":\\", 1)[-1] or None
