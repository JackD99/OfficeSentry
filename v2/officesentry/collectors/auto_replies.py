"""Automatic replies (out of office) set on each mailbox, read through Graph (MailboxSettings.Read).

Graph rather than Exchange's Get-MailboxAutoReplyConfiguration, like inbox rules: it's the same
setting, the permission is already granted and it works before a tenant assigns Global Reader.
One call per mailbox, in the rotating window other per-mailbox reads use.

Only mailboxes with a reply turned on or scheduled are stored, with the first few hundred
characters of each message as plain text, enough to see what it tells people.
"""

from __future__ import annotations

import html
import re

from ..graph import GraphError, PermissionMissing
from ..text import plural
from . import mailbox_window
from .base import Collector, RunContext, register
from .mail_rules import _targets

PREVIEW = 300  # characters of each reply kept
_TAGS = re.compile(r"<(script|style)\b.*?</\1\s*>|<[^>]+>", re.IGNORECASE | re.DOTALL)
_SPACE = re.compile(r"\s+")


def plain_text(message, limit: int = PREVIEW) -> str:
    """A reply's HTML as one line of plain text, cut at ``limit`` characters."""
    if not isinstance(message, str):
        return ""
    text = _SPACE.sub(" ", html.unescape(_TAGS.sub(" ", message))).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _when(value) -> dict:
    value = value if isinstance(value, dict) else {}
    return {"dateTime": value.get("dateTime"), "timeZone": value.get("timeZone")}


@register
class AutoReplies(Collector):
    key = "auto_replies"
    title = "Automatic replies"
    description = "Which mailboxes have an out-of-office reply on or scheduled, and who it goes to."
    permissions = ("MailboxSettings.Read",)

    def collect(self, ctx: RunContext) -> None:
        if ctx.capabilities.get("exchange") is False:
            ctx.log("Tenant has no Exchange Online licence; nothing to collect")
            ctx.objects("auto_reply", [])
            return
        window = mailbox_window.plan(ctx, self.key, "auto_reply", _targets(ctx), lambda t: t["id"], "automatic replies")
        targets = window.mailboxes
        rows, failed, denied = [], 0, 0
        for t in targets:
            try:
                s = ctx.graph.get(f"/users/{t['id']}/mailboxSettings/automaticRepliesSetting")
            except PermissionMissing:
                denied += 1
                continue
            except GraphError:  # e.g. no mailbox yet, or mailbox on-premises
                failed += 1
                continue
            status = s.get("status") if isinstance(s.get("status"), str) else None
            if status in (None, "disabled"):
                continue
            rows.append(
                (
                    t["id"],
                    {
                        "mailboxId": t["id"],
                        "mailbox": t["upn"],
                        "mailboxType": t["type"],
                        "status": status,
                        "externalAudience": s.get("externalAudience"),
                        "start": _when(s.get("scheduledStartDateTime")),
                        "end": _when(s.get("scheduledEndDateTime")),
                        "internalText": plain_text(s.get("internalReplyMessage")),
                        "externalText": plain_text(s.get("externalReplyMessage")),
                    },
                )
            )
        if targets and denied == len(targets):
            raise PermissionMissing(
                403,
                "Authorization_RequestDenied",
                "Reading automatic replies was refused for every mailbox; check "
                "MailboxSettings.Read consent and any Exchange application access policy",
            )
        if denied or failed:
            ctx.warn(f"Could not read automatic replies for {denied + failed} of {plural(len(targets), 'mailbox')}")
        rows += window.carried
        ctx.objects("auto_reply", rows)
        window.save(ctx)
        ctx.metric("mail.auto_replies", len(rows))
        ctx.metric("mail.auto_reply_mailboxes_checked", len(targets) - denied - failed + window.carried_mailboxes)
