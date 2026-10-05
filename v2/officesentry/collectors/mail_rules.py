"""Inbox rules for every mailbox, read through Graph (MailboxSettings.Read).

Graph is used rather than Exchange's Get-InboxRule so this works even before a
tenant assigns the app an Exchange role.
"""

from __future__ import annotations

from .. import store
from ..graph import GraphError, PermissionMissing
from ..text import plural
from . import mailbox_window
from .base import Collector, RunContext, register

MAILBOX_TYPES = {"UserMailbox", "SharedMailbox"}


def _targets(ctx: RunContext) -> list[dict]:
    """Mailboxes to read: from the Exchange snapshot, else licensed enabled users."""
    run = store.latest_run(ctx.conn, ctx.tenant["id"], "mailboxes")
    if run:
        boxes = store.run_objects(ctx.conn, run["id"], "mailbox")
        if boxes:
            return [{"id": m["ExternalDirectoryObjectId"], "upn": m.get("UserPrincipalName") or m.get(
                "PrimarySmtpAddress"), "type": m.get("RecipientTypeDetails")}
                for m in boxes if m.get("ExternalDirectoryObjectId") and m.get("RecipientTypeDetails") in MAILBOX_TYPES]
    run = store.latest_run(ctx.conn, ctx.tenant["id"], "users")
    users = store.run_objects(ctx.conn, run["id"], "user") if run else []
    return [{"id": u["_id"], "upn": u.get("userPrincipalName"), "type": "UserMailbox"}
            for u in users if u.get("licensed") and u.get("accountEnabled")]


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

        rows, failed, denied = [], 0, 0
        for t in targets:
            try:
                rules = ctx.paged(ctx.graph.get_all(f"/users/{t['id']}/mailFolders/inbox/messageRules"))
            except PermissionMissing:
                denied += 1
                continue
            except GraphError:  # e.g. no mailbox yet, or mailbox on-premises
                failed += 1
                continue
            folders: dict[str, str | None] = {}
            for r in rules:
                folder_id = (r.get("actions") or {}).get("moveToFolder")
                if folder_id and folder_id not in folders:
                    folders[folder_id] = self._folder_name(ctx, t["id"], folder_id)
                rows.append((f"{t['id']}:{r['id']}", {
                    "mailboxId": t["id"], "mailbox": t["upn"], "mailboxType": t["type"],
                    "displayName": r.get("displayName"), "isEnabled": r.get("isEnabled", True),
                    "hasError": r.get("hasError"), "sequence": r.get("sequence"),
                    "conditions": r.get("conditions") or {}, "actions": r.get("actions") or {},
                    "moveToFolderName": folders.get(folder_id) if folder_id else None,
                }))
        if targets and denied == len(targets):
            raise PermissionMissing(403, "Authorization_RequestDenied",
                                    "Reading inbox rules was refused for every mailbox; check MailboxSettings.Read "
                                    "consent and any Exchange application access policy")
        if denied or failed:
            ctx.warn(f"Could not read inbox rules for {denied + failed} of {plural(len(targets), 'mailbox')}")
        rows += window.carried
        ctx.objects("inbox_rule", rows)
        window.save(ctx)
        ctx.metric("mail.inbox_rules", len(rows))
        # Mailboxes the stored rules cover: read this run, or kept from an earlier run.
        ctx.metric("mail.mailboxes_checked", len(targets) - denied - failed + window.carried_mailboxes)

    @staticmethod
    def _folder_name(ctx: RunContext, user_id: str, folder_id: str) -> str | None:
        try:
            return ctx.graph.get(f"/users/{user_id}/mailFolders/{folder_id}",
                                 params={"$select": "displayName"}).get("displayName")
        except GraphError:
            return None
