"""Exchange Online: mailboxes, forwarding, delegated access and org mail settings.

Uses the read-only Exchange admin API (see ``officesentry/exchange.py``).
"""

from __future__ import annotations

from ..exchange import ExchangeAccessDenied, ExchangeError
from .base import Collector, RunContext, register

PER_MAILBOX_LIMIT = 2000

MAILBOX_FIELDS = (
    "ExternalDirectoryObjectId", "Guid", "UserPrincipalName", "PrimarySmtpAddress", "DisplayName",
    "RecipientTypeDetails", "ForwardingSmtpAddress", "ForwardingAddress", "DeliverToMailboxAndForward",
    "GrantSendOnBehalfTo", "HiddenFromAddressListsEnabled", "AuditEnabled", "WhenMailboxCreated",
    "Name", "Alias", "ArchiveStatus", "LitigationHoldEnabled",
    "IssueWarningQuota", "ProhibitSendQuota", "ProhibitSendReceiveQuota",
)
# Per-mailbox client access: which old protocols and SMTP AUTH each mailbox allows.
CAS_FIELDS = (
    "ExternalDirectoryObjectId", "Guid", "PrimarySmtpAddress", "SmtpClientAuthenticationDisabled",
    "PopEnabled", "ImapEnabled", "ActiveSyncEnabled", "EwsEnabled", "MAPIEnabled", "OWAEnabled",
)
SELF = "NT AUTHORITY\\SELF"
NOT_PEOPLE = {SELF, "NULL SID"}


def _pick(row: dict, fields) -> dict:
    return {f: row.get(f) for f in fields}


def _mailbox_id(m: dict) -> str:
    return m.get("ExternalDirectoryObjectId") or m.get("Guid") or m.get("PrimarySmtpAddress") or "?"


@register
class Mailboxes(Collector):
    key = "mailboxes"
    title = "Mailboxes"
    description = "Mailboxes, forwarding, delegated access and organisation mail settings (needs Global Reader)."
    permissions = ("Exchange.ManageAsApp",)

    def collect(self, ctx: RunContext) -> None:
        if ctx.capabilities.get("exchange") is False:
            ctx.log("Tenant has no Exchange Online licence; nothing to collect")
            ctx.objects("exchange_status", [("status", {"available": False})])
            return
        exo = ctx.exchange
        domains = exo.invoke("Get-AcceptedDomain")
        ctx.objects("accepted_domain", [(d.get("DomainName", "?").lower(), {
            "DomainName": (d.get("DomainName") or "").lower(), "DomainType": d.get("DomainType"),
            "Default": d.get("Default")}) for d in domains])

        mailboxes = exo.invoke("Get-Mailbox", {"ResultSize": "Unlimited"})
        rows = []
        for m in mailboxes:
            row = _pick(m, MAILBOX_FIELDS)
            row["GrantSendOnBehalfTo"] = list(row["GrantSendOnBehalfTo"] or [])
            row["ForwardingRecipient"] = None
            if row["ForwardingAddress"]:
                row["ForwardingRecipient"] = self._recipient(ctx, row["ForwardingAddress"])
            rows.append((_mailbox_id(m), row))
        ctx.objects("mailbox", rows)

        ctx.objects("mailbox_permission", self._full_access(ctx, [r for _, r in rows]))
        send_as = [p for p in exo.invoke("Get-RecipientPermission", {"ResultSize": "Unlimited"})
                   if p.get("Trustee") not in NOT_PEOPLE and not p.get("IsInherited")]
        ctx.objects("send_as", [(f"{p.get('Identity')}|{p.get('Trustee')}", {
            "Identity": p.get("Identity"), "Trustee": p.get("Trustee"),
            "AccessRights": p.get("AccessRights") or []}) for p in send_as])

        self._client_access(ctx)

        org = (exo.invoke("Get-OrganizationConfig") or [{}])[0]
        transport = (exo.invoke("Get-TransportConfig") or [{}])[0]
        ctx.objects("mail_settings", [("org", {
            "AuditDisabled": org.get("AuditDisabled"),
            "SmtpClientAuthenticationDisabled": transport.get("SmtpClientAuthenticationDisabled"),
        })])
        ctx.objects("outbound_spam_policy", [(p.get("Name", "?"), _pick(p, (
            "Name", "AutoForwardingMode", "IsDefault"))) for p in exo.invoke("Get-HostedOutboundSpamFilterPolicy")])
        ctx.objects("remote_domain", [(d.get("Name", "?"), _pick(d, (
            "Name", "DomainName", "AutoForwardEnabled"))) for d in exo.invoke("Get-RemoteDomain")])

        data = [r for _, r in rows]
        ctx.metric("mailboxes.total", len(data))
        ctx.metric("mailboxes.shared", sum(r["RecipientTypeDetails"] == "SharedMailbox" for r in data))
        ctx.metric("mailboxes.forwarding", sum(bool(r["ForwardingSmtpAddress"] or r["ForwardingAddress"])
                                               for r in data))

    @staticmethod
    def _client_access(ctx: RunContext) -> None:
        try:
            cas = ctx.exchange.invoke("Get-CASMailbox", {"ResultSize": "Unlimited"})
        except ExchangeAccessDenied:
            raise
        except ExchangeError as exc:
            ctx.warn(f"Could not read mailbox protocol settings: {exc.message}")
            return
        ctx.objects("cas_mailbox", [(_mailbox_id(c), _pick(c, CAS_FIELDS)) for c in cas
                                    if c.get("ExternalDirectoryObjectId")])  # system mailboxes have none

    @staticmethod
    def _recipient(ctx: RunContext, identity: str) -> dict | None:
        try:
            found = ctx.exchange.invoke("Get-Recipient", {"Identity": identity})
        except ExchangeAccessDenied:
            raise
        except ExchangeError as exc:
            ctx.log(f"Could not resolve forwarding recipient {identity}: {exc.message}", "warning")
            return None
        if not found:
            return None
        r = found[0]
        return {"PrimarySmtpAddress": r.get("PrimarySmtpAddress"), "RecipientType": r.get("RecipientType"),
                "ExternalEmailAddress": r.get("ExternalEmailAddress")}

    @staticmethod
    def _full_access(ctx: RunContext, mailboxes: list[dict]) -> list[tuple[str, dict]]:
        if len(mailboxes) > PER_MAILBOX_LIMIT:
            ctx.warn(f"Checked mailbox permissions on the first {PER_MAILBOX_LIMIT} of {len(mailboxes)} mailboxes")
            mailboxes = mailboxes[:PER_MAILBOX_LIMIT]
        rows, failed = [], 0
        for m in mailboxes:
            ident = m.get("Guid") or m.get("PrimarySmtpAddress")
            try:
                perms = ctx.exchange.invoke("Get-MailboxPermission", {"Identity": ident})
            except ExchangeAccessDenied:
                raise
            except ExchangeError:
                failed += 1
                continue
            for p in perms:
                user = p.get("User") or ""
                if user == SELF or p.get("IsInherited") or p.get("Deny") or user.startswith("S-1-5-"):
                    continue
                rows.append((f"{_mailbox_id(m)}|{user}", {
                    "mailboxId": _mailbox_id(m), "mailbox": m.get("PrimarySmtpAddress"),
                    "mailboxType": m.get("RecipientTypeDetails"), "User": user,
                    "AccessRights": p.get("AccessRights") or []}))
        if failed:
            ctx.warn(f"Could not read permissions on {failed} mailbox(es)")
        return rows
