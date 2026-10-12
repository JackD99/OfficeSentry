"""The audit operations the add-on keeps, as data: the design note's appendix A.

Microsoft's feed carries everything SharePoint, OneDrive and Exchange log, mostly files opened and mail read by
its owner. ``keep()`` turns one audit record into what the activity history stores (``events.Activity``), or
None for the rest, which is dropped as it's read. Each ``Operation`` names Microsoft's ``Operation`` value, its
group (``GROUPS``), what it means in words, and, where only some records matter, ``when``: a mailbox opened by
its owner, or a ``Set-Mailbox`` that doesn't touch forwarding, is dropped. Sync traffic is only counted, per
person and day (``counted``). ``attention()`` says when a kept record goes in the report's "Needs attention".

Spellings are Microsoft's and are confirmed against a real tenant's audit log before they're relied on (a
spelling that never matches only means the event is never kept). Adding one is a line in ``OPERATIONS``.

Email subjects that some Exchange records carry are taken out before a record is stored: the add-on records
that something happened to a mailbox, never what the mail said.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

GROUPS = {
    "files": "Files",
    "sharing": "Sharing",
    "mailbox": "Mailbox opened by someone else",
    "rules": "Inbox rules and forwarding",
    "permissions": "Mailbox and folder permissions",
    "audit": "Audit settings",
}

# Exchange's LogonType: who opened the mailbox. 0 is its owner; 3 and 4 are Exchange itself.
LOGON_TYPES = {
    0: "Owner",
    1: "Admin",
    2: "Delegate",
    3: "Transport",
    4: "System",
    5: "Best access",
    6: "Delegated admin",
}
SOMEONE_ELSE = {1, 2, 6}
SYSTEM = {3, 4}

FORWARDING = ("ForwardingSmtpAddress", "ForwardingAddress", "DeliverToMailboxAndForward")
RULE_FORWARDS = ("ForwardTo", "ForwardAsAttachmentTo", "RedirectTo")
RULE_DELETES = ("DeleteMessage", "SoftDeleteMessage", "PermanentDelete")
EMPTY = {"", "$null", "null", "none", "false"}


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _pairs(record: dict, key: str) -> dict[str, str]:
    """A list of ``{"Name": ..., "Value": ...}`` (``Parameters``, ``OperationProperties``) as a dict, names
    lower-cased."""
    out = {}
    items = record.get(key)
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and isinstance(item.get("Name"), str):
            value = item.get("Value")
            out[item["Name"].lower()] = value if isinstance(value, str) else ("" if value is None else str(value))
    return out


def params(record: dict) -> dict[str, str]:
    """An Exchange cmdlet's parameters."""
    return _pairs(record, "Parameters")


def properties(record: dict) -> dict[str, str]:
    return _pairs(record, "OperationProperties")


def _set(value: str) -> bool:
    return value.strip().lower() not in EMPTY


def logon_type(record: dict) -> int | None:
    value = record.get("LogonType")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if value.isdigit():
            return int(value)
        names = {v.lower().replace(" ", ""): k for k, v in LOGON_TYPES.items()}
        return names.get(value.lower().replace(" ", ""))
    return None


def actor(record: dict) -> str:
    return _text(record.get("UserId")) or _text(record.get("UserKey"))


def mailbox_owner(record: dict) -> str:
    return _text(record.get("MailboxOwnerUPN"))


def _system_account(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith("nt authority") or lowered.startswith("s-1-5-18") or lowered == "system"


def by_someone_else(record: dict) -> bool:
    """A mailbox was used by someone other than its owner: an admin or a delegate, never Exchange itself."""
    kind = logon_type(record)
    if kind in SOMEONE_ELSE:
        return True
    if kind in SYSTEM:
        return False
    who, owner = actor(record).lower(), mailbox_owner(record).lower()
    return bool(who and owner and who != owner and not _system_account(who))


def _forwarding(record: dict) -> bool:
    p = params(record)
    return any(k.lower() in p for k in FORWARDING)


def _audit_off(record: dict) -> bool:
    return "auditdisabled" in params(record)


@dataclass(frozen=True)
class Operation:
    name: str  # Microsoft's ``Operation``
    group: str  # a ``GROUPS`` key
    label: str  # what happened, in words for people
    when: Callable[[dict], bool] | None = None  # kept only when this says so
    counted: bool = False  # counted per person and day instead of kept one by one


OPERATIONS: dict[str, Operation] = {
    o.name.lower(): o
    for o in (
        # Files (SharePoint and OneDrive)
        Operation("FileDeleted", "files", "File deleted"),
        Operation("FileRecycled", "files", "File deleted"),
        Operation("FileDeletedFirstStageRecycleBin", "files", "File emptied from the recycle bin"),
        Operation("FileDeletedSecondStageRecycleBin", "files", "File emptied from the second-stage recycle bin"),
        Operation("FileVersionsAllDeleted", "files", "Every version of a file deleted"),
        Operation("FolderDeleted", "files", "Folder deleted"),
        Operation("FolderRecycled", "files", "Folder deleted"),
        Operation("FileRestored", "files", "File restored"),
        Operation("FileDownloaded", "files", "File downloaded"),
        Operation("FileMoved", "files", "File moved"),
        Operation("FileCopied", "files", "File copied"),
        Operation("FileMalwareDetected", "files", "Malware found in a file"),
        Operation("FileSyncDownloadedFull", "files", "Files downloaded by OneDrive sync", counted=True),
        Operation("FileSyncUploadedFull", "files", "Files uploaded by OneDrive sync", counted=True),
        # Sharing
        Operation("SharingSet", "sharing", "Shared with someone"),
        Operation("SharingRevoked", "sharing", "Sharing removed"),
        Operation("SharingInvitationCreated", "sharing", "Sharing invitation sent"),
        Operation("SharingInvitationAccepted", "sharing", "Sharing invitation accepted"),
        Operation("SharingInvitationRevoked", "sharing", "Sharing invitation withdrawn"),
        Operation("AnonymousLinkCreated", "sharing", "Anyone link created"),
        Operation("AnonymousLinkUsed", "sharing", "Anyone link used"),
        Operation("AnonymousLinkUpdated", "sharing", "Anyone link changed"),
        Operation("AnonymousLinkRemoved", "sharing", "Anyone link removed"),
        Operation("SecureLinkCreated", "sharing", "Specific people link created"),
        Operation("SecureLinkUsed", "sharing", "Specific people link used"),
        Operation("SecureLinkDeleted", "sharing", "Specific people link removed"),
        Operation("AddedToSecureLink", "sharing", "Someone added to a specific people link"),
        Operation("RemovedFromSecureLink", "sharing", "Someone removed from a specific people link"),
        Operation("CompanyLinkCreated", "sharing", "Organisation link created"),
        Operation("CompanyLinkUsed", "sharing", "Organisation link used"),
        Operation("CompanyLinkRemoved", "sharing", "Organisation link removed"),
        Operation("SharingInheritanceBroken", "sharing", "Item stopped inheriting its site's permissions"),
        Operation("SiteCollectionAdminAdded", "sharing", "Site admin added"),
        Operation("SiteCollectionAdminRemoved", "sharing", "Site admin removed"),
        Operation("PermissionLevelAdded", "sharing", "Permission level added"),
        Operation("PermissionLevelModified", "sharing", "Permission level changed"),
        Operation("PermissionLevelRemoved", "sharing", "Permission level removed"),
        Operation("SharingPolicyChanged", "sharing", "Sharing policy changed"),
        # Mailbox opened by someone else
        Operation("MailItemsAccessed", "mailbox", "Mail read by someone else", by_someone_else),
        Operation("MailboxLogin", "mailbox", "Signed in to someone else's mailbox", by_someone_else),
        Operation("FolderBind", "mailbox", "Folder opened by someone else", by_someone_else),
        Operation("SendAs", "mailbox", "Sent as someone else"),
        Operation("SendOnBehalf", "mailbox", "Sent on behalf of someone else"),
        Operation("Update", "mailbox", "Mail changed by someone else", by_someone_else),
        Operation("MoveToDeletedItems", "mailbox", "Mail deleted by someone else", by_someone_else),
        Operation("SoftDelete", "mailbox", "Mail deleted by someone else", by_someone_else),
        Operation("HardDelete", "mailbox", "Mail purged by someone else", by_someone_else),
        # Inbox rules, forwarding and mail flow
        Operation("New-InboxRule", "rules", "Inbox rule created"),
        Operation("Set-InboxRule", "rules", "Inbox rule changed"),
        Operation("Enable-InboxRule", "rules", "Inbox rule switched on"),
        Operation("Remove-InboxRule", "rules", "Inbox rule removed"),
        Operation("UpdateInboxRules", "rules", "Inbox rule changed in Outlook"),
        Operation("Set-Mailbox", "rules", "Mail forwarding changed", _forwarding),
        Operation("New-TransportRule", "rules", "Mail flow rule created"),
        Operation("Set-TransportRule", "rules", "Mail flow rule changed"),
        Operation("Remove-TransportRule", "rules", "Mail flow rule removed"),
        # Mailbox and folder permissions
        Operation("Add-MailboxPermission", "permissions", "Mailbox access granted"),
        Operation("Remove-MailboxPermission", "permissions", "Mailbox access removed"),
        Operation("Add-RecipientPermission", "permissions", "Send As granted"),
        Operation("Remove-RecipientPermission", "permissions", "Send As removed"),
        Operation("Add-MailboxFolderPermission", "permissions", "Folder shared"),
        Operation("Set-MailboxFolderPermission", "permissions", "Folder sharing changed"),
        Operation("Remove-MailboxFolderPermission", "permissions", "Folder sharing removed"),
        Operation("UpdateFolderPermissions", "permissions", "Folder sharing changed in Outlook"),
        Operation("New-MailboxExportRequest", "permissions", "Mailbox export requested"),
        # Audit settings
        Operation("Set-AdminAuditLogConfig", "audit", "Audit log settings changed"),
        Operation("Set-MailboxAuditBypassAssociation", "audit", "Mailbox auditing bypass changed"),
        Operation("Set-OrganizationConfig", "audit", "Mailbox auditing changed for the organisation", _audit_off),
    )
}


def operation(record: dict) -> Operation | None:
    """The kept operation this record is, or None for one the add-on drops."""
    found = OPERATIONS.get(_text(record.get("Operation")).lower())
    if found is None or (found.when is not None and not found.when(record)):
        return None
    return found


def label(name: str) -> str:
    found = OPERATIONS.get(name.lower())
    return found.label if found else name


def group(name: str) -> str:
    found = OPERATIONS.get(name.lower())
    return found.group if found else ""


# -- What each record is about, in a line ----------------------------------------------------------------


def _file(record: dict) -> str:
    """A file or folder's address: its full URL when the record has one, else its name."""
    url = _text(record.get("ObjectId"))
    if url.lower().startswith("http"):
        return url
    folder, name = _text(record.get("SourceRelativeUrl")), _text(record.get("SourceFileName"))
    return "/".join(p for p in (_text(record.get("SiteUrl")).rstrip("/"), folder, name) if p) or url


def _mailbox(record: dict) -> str:
    return (
        mailbox_owner(record)
        or _text(record.get("SendAsUserSmtp"))
        or _text(record.get("SendOnBehalfOfUserSmtp"))
        or _text(params(record).get("identity"))
        or _text(record.get("ObjectId"))
    )


def subject(record: dict, op: Operation) -> tuple[str, str]:
    """(whose mailbox or the person it's about, the file, link, rule or mailbox it's about)."""
    if op.group in ("files", "sharing"):
        target = _text(record.get("TargetUserOrGroupName"))
        return target, _file(record) or _text(record.get("SiteUrl"))
    p = params(record)
    if op.name in ("SendAs", "SendOnBehalf"):
        return _text(record.get("SendAsUserSmtp")) or _text(record.get("SendOnBehalfOfUserSmtp")), ""
    if op.group == "mailbox":
        folder = record.get("Folder") if isinstance(record.get("Folder"), dict) else {}
        folders = record.get("Folders") if isinstance(record.get("Folders"), list) else []
        paths = [_text(f.get("Path")) for f in folders if isinstance(f, dict)] or [_text(folder.get("Path"))]
        return mailbox_owner(record), ", ".join(p for p in paths if p)[:300]
    if op.name in ("New-TransportRule", "Set-TransportRule", "Remove-TransportRule"):
        return "", _text(p.get("name")) or _text(p.get("identity")) or _text(record.get("ObjectId"))
    if op.group == "rules":
        props = properties(record)
        rule = _text(p.get("name")) or _text(props.get("rulename")) or _text(p.get("identity"))
        return _mailbox(record), rule
    if op.group == "permissions":
        return _mailbox(record), _text(p.get("user")) or _text(p.get("trustee"))
    return "", _text(record.get("ObjectId")) or _text(p.get("identity"))


def _changes(record: dict, names: tuple[str, ...]) -> list[str]:
    p = params(record)
    return [f"{n} {p[n.lower()]}" for n in names if n.lower() in p and _set(p[n.lower()])]


def summary(record: dict, op: Operation) -> str:
    """The detail worth reading in a table cell: where a file went, who was given access, what a rule does."""
    if op.name in ("FileMoved", "FileCopied"):
        to = "/".join(
            p for p in (_text(record.get("DestinationRelativeUrl")), _text(record.get("DestinationFileName"))) if p
        )
        return f"To {to}" if to else ""
    if op.group == "sharing":
        kind = _text(record.get("TargetUserOrGroupType"))
        return f"{kind}" if kind else ""
    if op.group == "mailbox":
        kind = logon_type(record)
        client = _text(record.get("ClientInfoString"))[:80]
        who = LOGON_TYPES.get(kind, "") if kind is not None else ""
        return ", ".join(p for p in (who and f"As {who.lower()}", client) if p)
    if op.name == "Set-Mailbox":
        return "; ".join(_changes(record, FORWARDING)) or "Forwarding removed"
    if op.group == "rules":
        parts = _changes(record, RULE_FORWARDS + RULE_DELETES + ("MoveToFolder",))
        props = properties(record)
        if not parts and props.get("ruleactions"):
            parts = [props["ruleactions"][:200]]
        return "; ".join(parts)
    if op.group == "permissions":
        p = params(record)
        rights = p.get("accessrights") or p.get("permissions") or ""
        return rights[:200]
    if op.group == "audit":
        p = params(record)
        return "; ".join(f"{k} {v}" for k, v in sorted(p.items()) if k != "identity")[:300]
    return ""


def attention(record: dict, op: Operation) -> str:
    """Why this record goes in "Needs attention", or "" when it doesn't. (A mass delete is spotted across
    records, by the report.)"""
    p = params(record)
    if op.name == "AnonymousLinkCreated":
        return "Anyone with the link can open it, without signing in."
    if op.group == "mailbox" and logon_type(record) in (1, 6):
        return "An admin opened someone else's mailbox."
    if op.name == "Set-Mailbox" and _changes(record, FORWARDING[:2]):
        return FORWARDING_SET
    if op.group == "rules" and op.name not in ("Remove-InboxRule", "Remove-TransportRule"):
        actions = " ".join(_changes(record, RULE_FORWARDS + RULE_DELETES)).lower()
        actions += " " + properties(record).get("ruleactions", "").lower()
        if any(w in actions for w in ("forward", "redirect")):
            return "A rule forwards mail."
        if "delete" in actions:
            return "A rule deletes mail."
    if op.name == "Set-AdminAuditLogConfig":
        if any(p.get(k, "").lower() == "false" for k in ("unifiedauditlogingestionenabled", "adminauditlogenabled")):
            return "The audit log was turned off."
    if op.name == "Set-MailboxAuditBypassAssociation" and p.get("auditbypassenabled", "").lower() == "true":
        return "A mailbox or account was excluded from auditing."
    if op.name == "Set-OrganizationConfig" and p.get("auditdisabled", "").lower() == "true":
        return "Mailbox auditing was turned off for the organisation."
    if op.name == "New-MailboxExportRequest":
        return "A copy of a mailbox was requested."
    if op.name == "FileMalwareDetected":
        return "Microsoft found malware in a file."
    return ""


# -- The report's headline figures, also stored as metrics for their trends --------------------------------

DELETES = {
    "filedeleted",
    "filerecycled",
    "filedeletedfirststagerecyclebin",
    "filedeletedsecondstagerecyclebin",
    "fileversionsalldeleted",
    "folderdeleted",
    "folderrecycled",
}
RULE_CHANGES = {"new-inboxrule", "set-inboxrule", "enable-inboxrule", "updateinboxrules"}
FIGURES = {  # key: title
    "files_deleted": "Files deleted",
    "files_downloaded": "Files downloaded",
    "anyone_links": "Anyone links created",
    "mailboxes_opened": "Mailboxes opened by others",
    "rules_changed": "Inbox rules created or changed",
    "forwarding_set": "Forwarding set",
}
FORWARDING_SET = "Mail forwarding was set on a mailbox."


def figures(activities) -> dict[str, int]:
    """The headline figures over stored records (``events.Activity``): mailboxes are counted once each."""
    out = dict.fromkeys(FIGURES, 0)
    opened = set()
    for a in activities:
        name = a.operation.lower()
        if name in DELETES:
            out["files_deleted"] += 1
        elif name == "filedownloaded":
            out["files_downloaded"] += 1
        elif name == "anonymouslinkcreated":
            out["anyone_links"] += 1
        elif name in RULE_CHANGES:
            out["rules_changed"] += 1
        elif name == "set-mailbox" and a.attention == FORWARDING_SET:
            out["forwarding_set"] += 1
        if group(a.operation) == "mailbox" and a.operation not in ("SendAs", "SendOnBehalf"):
            opened.add((a.owner or a.object).lower())
    out["mailboxes_opened"] = len(opened)
    return out


def _without_subjects(value):
    """The record with every ``Subject`` taken out, at any depth."""
    if isinstance(value, dict):
        return {k: _without_subjects(v) for k, v in value.items() if k.lower() != "subject"}
    if isinstance(value, list):
        return [_without_subjects(v) for v in value]
    return value


def stored(record: dict) -> dict:
    """What's kept of a record: all of it, apart from email subjects."""
    return _without_subjects(record)


def stored_json(record: dict) -> str:
    return json.dumps(stored(record), separators=(",", ":"), sort_keys=True)
