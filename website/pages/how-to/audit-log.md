<!-- since 2.4.0 -->
# Find who deleted or shared a file, or opened someone's mailbox, in Microsoft 365

"Who deleted the client folder?", "Did anyone download the price list before they left?", "Has someone been
reading the director's mail?" Microsoft 365 records the answers in its unified audit log, as long as the log
is on. The catches: Audit (Standard), which comes with most business plans, keeps records for 180 days; a
search covers one tenant at a time; and it returns raw records you have to read through.

## First, is the audit log on?

New tenants have it on, but it can be switched off, and some older tenants never had it. Connect with the
Exchange Online PowerShell module (`Install-Module ExchangeOnlineManagement`):

```powershell
Connect-ExchangeOnline
Get-AdminAuditLogConfig | Select-Object UnifiedAuditLogIngestionEnabled
```

**True** means it's recording. If it's **False**, turn it on in the Microsoft Purview portal (*Audit*, then
*Start recording user and admin activity*). It can take a few hours to start, and nothing from before then
can be found.

## By hand, for one tenant

Search in the Purview portal (*Audit*, then a new search), or with `Search-UnifiedAuditLog` in the same
PowerShell session, as an account with the *Audit Logs* or *View-Only Audit Logs* role.

### Files deleted, downloaded or shared

```powershell
Search-UnifiedAuditLog -StartDate (Get-Date).AddDays(-30) -EndDate (Get-Date) `
    -Operations FileDeleted, FileRecycled, FileDownloaded, SharingSet, AnonymousLinkCreated `
    -ResultSize 5000 |
    ForEach-Object { $_.AuditData | ConvertFrom-Json } |
    Select-Object CreationTime, UserId, Operation, ObjectId
```

`ObjectId` is the file's address. One search returns at most 5,000 records; for more, add
`-SessionId <any text> -SessionCommand ReturnLargeSet` and run it again until it returns nothing.

### Mail opened by someone other than its owner

Exchange records a mailbox being read as `MailItemsAccessed`, and its `LogonType` says by whom: 0 is the
owner, 1 an admin, 2 a delegate.

```powershell
Search-UnifiedAuditLog -StartDate (Get-Date).AddDays(-3) -EndDate (Get-Date) `
    -Operations MailItemsAccessed, SendAs, SendOnBehalf -ResultSize 5000 |
    ForEach-Object { $_.AuditData | ConvertFrom-Json } |
    Where-Object { $_.LogonType -ne 0 } |
    Select-Object CreationTime, UserId, MailboxOwnerUPN, LogonType, Operation
```

Most of those records are people reading their own mail, so search a few days at a time.

### Inbox rules and forwarding set up

```powershell
Search-UnifiedAuditLog -StartDate (Get-Date).AddDays(-30) -EndDate (Get-Date) `
    -Operations New-InboxRule, Set-InboxRule, UpdateInboxRules, Set-Mailbox -ResultSize 5000
```

Each record's `AuditData` holds the rule's settings, so read it for forwarding or deleting actions. A rule made
in Outlook on the desktop shows up as `UpdateInboxRules`.

## Every client, every night

Office Sentry's optional **audit log add-on** reads each approving client's audit log every night through
Microsoft's activity feed, and keeps what MSPs get asked about for two years by default (you can choose six
months to seven years, or forever):

- **Files and mailbox access** lists files deleted, downloaded, moved or shared, anyone links, mail read, sent
  or deleted by an admin or a delegate, inbox rules and forwarding set up, mailbox permissions given, and
  changes to auditing itself. Files opened or edited, and people reading their own mail, are left out.
- **Needs attention** at the top picks out an admin opening someone else's mailbox, a rule that forwards or
  deletes mail, a new anyone link, auditing switched off, and anyone deleting 100 files or more within an
  hour.
- Each person's page shows what they did beside their sign-ins.
- The check *The Microsoft 365 audit log is on* runs for every client, with or without the add-on, so a
  client whose log is off is found before anyone needs it.

The add-on is a separate app with one permission (`ActivityFeed.Read`), which each client approves on its
own, and it never reads what's in an email or a file. Microsoft's feed doesn't give the past: the report fills
from the day the add-on starts, so switch it on before you need it.

[Set up the audit log add-on](../../../v2/docs/audit-log-addon.md) ·
[What the client approves](../../../v2/docs/what-office-sentry-reads.md#the-audit-log-add-on-if-you-approve-it) ·
[Every check](../../../v2/docs/checks.md) · [Install Office Sentry](../../../README.md#quick-start)
<!-- /since -->
