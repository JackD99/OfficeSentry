# What Office Sentry reads in your Microsoft 365

This page is for the owner of a business, or their IT contact, who has been
asked by their IT provider (MSP) to approve access for **Office Sentry**. It
explains what you are approving, what it can and can't see, where the
information goes and how to take the access away again.

> **In short.** Your MSP asks a Global Administrator to approve one app,
> usually called *Office Sentry (read-only)*. It reads your Microsoft 365
> settings, accounts, licences and sign-in records each night and can't
> change anything. They may also ask you to give that app the Global Reader
> role, so mailbox settings can be read too. It never reads email, files or
> chats, and you can remove it yourself at any time
> ([How to remove access](#how-to-remove-access)).

Words used on this page:

- **Tenant**: your organisation's Microsoft 365 account, with all its users,
  mailboxes and settings.
- **Microsoft Graph**: the official Microsoft service that programs use to
  read information from Microsoft 365.
- **Permission**: a specific thing Microsoft lets a program do, such as "read
  the list of users". A program can only do what its permissions allow.
- **Consent**: your approval for a program to use its permissions in your
  tenant. Only a Global Administrator (or a Privileged Role Administrator)
  can give it for the whole organisation.

## What Office Sentry is

Office Sentry is a reporting tool your MSP runs. Every night it reads the
security and licence settings of your Microsoft 365 tenant and turns them into
reports: a monthly security and licensing review, lists of things to fix, and
detail reports on mailboxes, apps, sign-in policies, email domains, guests and
devices.

It connects as a program (an "app"), not as a person. It signs in with a
certificate that your MSP holds, not with anybody's password.

## Read-only by design

Office Sentry is built so that it can't change anything in your tenant.

- **It asks only for read permissions.** Every permission in the table below
  has `.Read` in its name (such as `User.Read.All`, `MailboxSettings.Read` or
  `Channel.ReadBasic.All`), except `Exchange.ManageAsApp`, which is explained
  under [Exchange Online](#exchange-online-and-the-global-reader-role).
- **The program can only read.** The part of Office Sentry that talks to
  Microsoft Graph has no way to send changes: it can only make "GET" (read)
  requests. The only other request it makes is to Microsoft's login service,
  to sign in.
- **Exchange commands are limited to a fixed list.** Exchange Online's admin
  service uses the same kind of request for reading and for changing things,
  so Office Sentry refuses to run any command that isn't on its list. Every
  command on the list starts with `Get-` (Exchange's word for "show me"):

  `Get-AcceptedDomain`, `Get-AdminAuditLogConfig`, `Get-AntiPhishPolicy`, `Get-AntiPhishRule`, `Get-ATPProtectionPolicyRule`,
  `Get-CASMailbox`, `Get-DistributionGroup`, `Get-DistributionGroupMember`,
  `Get-EOPProtectionPolicyRule`, `Get-HostedContentFilterPolicy`, `Get-HostedContentFilterRule`,
  `Get-HostedOutboundSpamFilterPolicy`, `Get-InboundConnector`, `Get-InboxRule`, `Get-Mailbox`,
  `Get-MailboxPermission`, `Get-MailboxStatistics`, `Get-MailTrafficSummaryReport`,
  `Get-MalwareFilterPolicy`, `Get-MalwareFilterRule`, `Get-MessageTraceV2`, `Get-MobileDevice`,
  `Get-MobileDeviceStatistics`, `Get-OrganizationConfig`, `Get-OutboundConnector`, `Get-Recipient`,
  `Get-RecipientPermission`, `Get-RemoteDomain`, `Get-SafeAttachmentPolicy`, `Get-SafeAttachmentRule`,
  `Get-SafeLinksPolicy`, `Get-SafeLinksRule`, `Get-TransportConfig`, `Get-TransportRule`.

  None of these return the contents of any email. `Get-MailboxStatistics`
  returns a mailbox's size and number of items, and `Get-CASMailbox` returns
  which ways of connecting (such as POP, IMAP and SMTP AUTH) a mailbox allows.
  `Get-MobileDevice` and `Get-MobileDeviceStatistics` return the phones and
  tablets that sync with each mailbox (model, operating system, access state
  and when each last synced). `Get-MessageTraceV2` is Exchange's message trace,
  which lists each message's sender, recipients, subject and time. Office
  Sentry keeps only counts from it: how many messages each distribution list
  received per day and when the last one arrived, and how many messages each
  of the tenant's domains received, sent and delivered, sent to Junk,
  quarantined or failed per day. It never keeps subjects, senders or
  recipients. `Get-MailTrafficSummaryReport` returns Microsoft's own summary
  of the 20 people who got the most spam and phishing in the last 30 days:
  their addresses and how many such messages each got. `Get-TransportRule`
  returns the organisation's mail flow rules (their conditions, actions and
  exceptions, and the addresses a rule sends copies to), and the connector,
  policy and rule cmdlets return how mail is routed and filtered: the anti-spam,
  anti-phishing, anti-malware, Safe Links and Safe Attachments settings and whom
  each policy applies to. Office Sentry keeps the settings it reports on, cuts
  free text such as a disclaimer short, and only counts the people an
  anti-phishing policy protects from impersonation. The Safe Links, Safe
  Attachments and `Get-ATPProtectionPolicyRule` commands are only run for
  clients with Defender for Office 365. `Get-AdminAuditLogConfig` returns
  whether your Microsoft 365 audit log is recording; Office Sentry keeps only
  that yes or no.

The only things Office Sentry ever creates or changes are its own apps, all
registered in your **MSP's** tenant:

- **Office Sentry (read-only)**, the one that reads your tenant. It's created
  once, and updated when a new release asks for more permissions.
- **Office Sentry sign-in**, if your MSP uses it, which people sign in to
  Office Sentry with. It can't read anything in your tenant: it only learns
  the name and sign-in address of whoever signs in with it.
- **Office Sentry audit log add-on**, if your MSP uses it and you approve it ([below](#the-audit-log-add-on-if-you-approve-it)). <!-- since 2.4.0 -->

Your MSP may also send report emails through an app of their own that can
send mail from their mailbox; it has no access to your tenant either.

Office Sentry never creates or changes anything in your tenant. Approving
consent makes Microsoft add the app to your *Enterprise applications* list,
and deleting it from there is how you remove access.

## The permissions you are approving

The Microsoft consent page shows these permissions, in Microsoft's wording,
before you approve. All of
them are *application* permissions: they apply to the whole tenant, not to one
person's account, which is why a Global Administrator has to approve them.

"Used by" names the Office Sentry reports that rely on the permission. The
*Monthly Security & Licensing Review* and the *insurance evidence pack* are
built from the checks, so they use all of the information below.

| Permission | What it lets Office Sentry read | Why it's needed | Used by |
|---|---|---|---|
| `Organization.Read.All` | Your organisation's name, domains and subscriptions (which licences you've bought and how many are in use). | To know which licences you have, so it only runs checks your licences support, and to work out unused licences. | Licence waste; Licences and subscriptions; Hybrid identity; email domain checks (to know your domains). |
| `User.Read.All` | Every user and guest account: name, email address, department, job title, office, company and manager, whether it's enabled, which licences it holds and whether each was assigned directly or through a group (with any assignment error), its usage location (country), when its password was last changed and whether it's set to never expire. Never the password itself. | Account and licence checks, and password age. | Licence checks; Licences and subscriptions; Licence waste; Licence changes; All users; inactive accounts; guests; shared mailboxes; Passwords and sign-in methods. |
| `AuditLog.Read.All` | Sign-in and audit records. Office Sentry reads the date each account last signed in, Microsoft's MFA registration summary and when each app connected to your tenant last signed in. It also reads the sign-in log and the directory audit log (changes made in Entra ID, such as new admins, password resets and changed policies) and keeps them beyond the 30 days Microsoft does: **every sign-in**, with the account, app, kind of client, IP address, country, city, device operating system, result, whether MFA was used and any risk Microsoft saw; a **daily summary** per account, app and country; and **every directory audit event** as Microsoft records it. See [how long it is kept](#how-long-it-is-kept). | To find accounts nobody uses, accounts without MFA, sign-ins that can't use MFA, password guessing, sign-ins from unusual places and admin changes, and to look back at what happened when something goes wrong. Sign-in records need Entra ID P1. | Inactive accounts; MFA coverage; Legacy sign-ins; Sign-in activity; Admin activity; Activity history; Unused apps. |
| `UserAuthenticationMethod.Read.All` | Which kinds of sign-in method each user has registered, such as the Authenticator app, a passkey or a phone number, and whether they've set up self-service password reset. Not the codes, secrets or phone numbers. | MFA registration, for tenants without Entra ID P1, and how strong each person's sign-in is. | MFA coverage; Passwords and sign-in methods. |
| `Directory.Read.All` | Directory information: groups and their members (and which licences a group hands out, and the rule of a dynamic group), devices, which apps users have agreed to, each subscription's status, trial flag and next renewal or end date, and your domains with their password expiry policy. It also reads the recycle bin: users deleted in the last 30 days (name, sign-in name, job title, department and the licences they held) and deleted Microsoft 365 groups. | Which accounts a sign-in policy covers, device records, app consents, subscription renewals, password expiry, and what can still be restored. | Conditional Access coverage; Devices; Apps and consents; Licences and subscriptions; Passwords and sign-in methods; Groups and Teams; Group membership; Empty, nested and dynamic groups; Hybrid identity; Deleted users and groups; Administrative units; Teams channels, guests and apps. |
| `RoleManagement.Read.Directory` | Who holds each admin role, including roles that can be activated on demand (PIM). | Admin account checks. | Admins and roles; Conditional Access coverage; Administrative units. |
| `Policy.Read.All` | Sign-in policies (Conditional Access, Security Defaults, which sign-in methods such as passkeys or text messages are allowed and for whom) and organisation-wide settings for guests and app consent. | MFA and legacy sign-in checks; sign-in method settings; guest and consent settings. | MFA checks; Conditional Access coverage; Passwords and sign-in methods; Guests and sharing; Tenant settings; the app consent check. |
| `Application.Read.All` | Apps connected to your tenant, what they've been allowed to do, and when app secrets or certificates expire. Not the secrets themselves. | To list third-party apps with access to your data and expiring app credentials. | Apps and consents. |
| `SecurityEvents.Read.All` | Your Microsoft Secure Score and its history. | The Secure Score trend in reports. | Secure Score trend. |
| `DeviceManagementManagedDevices.Read.All` | Devices managed by Intune: name, operating system, whether they're compliant, encrypted and checking in, and their main user. | Device checks. Needs an Intune licence. | Devices. |
| `SharePointTenantSettings.Read.All` | SharePoint's organisation-wide sharing settings (for example whether "anyone" links are allowed). Not sites or files. | The external sharing check. | Guests and sharing; Tenant settings. |
| `Sites.Read.All` | The list of SharePoint sites, libraries and OneDrives, and for each file and folder its name, location, size (including older versions), version number, when it was last changed and by whom, and who it's shared with. Recycle bin item names and sizes. **Not** what's inside the files. | To show which files are shared outside the organisation, and what fills SharePoint storage so it can be cleaned up, and who owns and can reach each site. | Shared files; File and folder permissions; OneDrive; SharePoint storage; Site access. |
| `MailboxSettings.Read` | Mailbox settings: inbox rules, and whether an automatic reply (out of office) is on, for when, whom it answers, and the first 300 characters of its message. **Not** emails. | Inbox rules that forward or hide mail are a common sign of a hacked mailbox; automatic replies show who is away and what the reply tells people outside. | Mail forwarding; Inbox rules; Automatic replies. |
| `Reports.Read.All` | Microsoft 365 usage reports: for each person, when they last used email, Teams, OneDrive, SharePoint and the Office apps, on which platforms, where Office is installed, and how many emails, Teams messages, calls and meetings they had. Storage and activity per SharePoint site and OneDrive, and when each team was last used. **Not** what's in any email, chat or file. | To show who actually uses what they're licensed for, suggest cheaper licences, and report SharePoint and OneDrive storage. If your tenant hides names in usage reports, Office Sentry shows codes instead. | Microsoft 365 usage; Licence right-sizing; SharePoint sites; OneDrive; Teams channels, guests and apps. |
| `IdentityRiskyUser.Read.All` | Accounts Entra ID Protection has flagged as risky: sign-in name, display name, risk level, risk state, why (for example a secure password reset) and when it last changed. Office Sentry keeps the accounts at risk now and those resolved in the last 90 days. Needs Entra ID P2. | To show accounts Microsoft thinks someone else may be using, so they can be checked with the person. | Risky users; the risky users check; the account page. |
| `Channel.ReadBasic.All` *(new in 2.2)* | The names, creation dates and kinds of the channels in each team (standard, private, shared). **Not** the messages in them. | A Teams report of every team's channels, including private and shared ones. | Teams channels, guests and apps. |
| `ChannelMember.Read.All` *(new in 2.2)* | Who is a member or owner of each private and shared channel, and whether they're a guest or from another organisation. | To show who can see private and shared channels, guests and people outside the organisation first. | Teams channels, guests and apps. |
| `TeamsAppInstallation.ReadForTeam.All` *(new in 2.2)* | Which apps are added to each team, and their versions. | So apps nobody expected stand out. | Teams channels, guests and apps. |
| `ReportSettings.Read.All` *(new in 2.2)* | One setting: whether your usage reports hide people's names. | So usage reports say names are hidden by your setting, instead of showing codes without explanation. | Microsoft 365 usage; Licence right-sizing. |
| `PrivilegedEligibilitySchedule.Read.AzureADGroup` *(new in 2.2)* | Who can activate membership of a group with Privileged Identity Management (PIM for groups), and until when. | Admin rights can come through such a group; the admin checks count those people too. | Admins and roles (people who can make themselves a member of a group holding an admin role, listed as eligible). Needs Entra ID P2. |
| `BitLockerKey.ReadBasic.All` *(new in 2.2)* | Whether each device has a BitLocker recovery key saved in Entra ID, and when. **Never** the key itself. | So a lost or locked computer can be recovered: devices without a saved key are listed. | Devices (BitLocker recovery keys); the BitLocker recovery key check. |
| `DeviceManagementConfiguration.Read.All` *(new in 2.2)* | Intune configuration and compliance policies, such as firewall, screen lock, encryption and update settings, and which devices they apply to. | To show what each compliance policy requires, which platforms have none, update rings and endpoint security policies, and who each applies to. | Intune policies; the compliance policy coverage check. Needs an Intune licence. |
| `DeviceManagementApps.Read.All` *(new in 2.2)* | Intune app protection policies and the apps Intune deploys, with their versions. | To show whether company data in apps on phones (including personal ones) is protected by a PIN and kept inside managed apps. | Intune policies (app protection). Needs an Intune licence. |
| `Sites.Read.All` on **Office 365 SharePoint Online** *(new in 2.2)* | SharePoint's own permission lists: who is in each SharePoint group and who has access to each site and list (it needs the app's certificate). Microsoft lists it under SharePoint, separately from Microsoft Graph's permission of the same name. **Not** what's inside the files. | Graph doesn't say who is in SharePoint's own groups, so without it "who has access to this site" is incomplete. | Site permissions; the guests with full control check. Until it's approved, Site permissions says it's waiting for it. |
| `DeviceLocalCredential.ReadBasic.All` *(new in 2.4)* | Whether each computer backs up its local administrator password in Entra ID with Windows LAPS, when it last did and when the password next changes. **Never** the password itself. | A local admin password that's the same on every computer, or never changes, lets one leak open them all: computers without a backup are listed. | Devices (Local admin passwords); the Windows LAPS check. | <!-- since 2.4.0 -->
| `SecurityAlert.Read.All` *(new in 2.4)* | Alerts Microsoft Defender raised: title, severity, status, who it's assigned to, which service raised it, when, what it turned out to be, and the accounts and devices it names. **Not** the emails, files or commands behind an alert. | So alerts nobody has looked at stand out, without signing in to the Defender portal. | Security alerts; the Defender alerts check. Not offered in Microsoft 365 operated by 21Vianet. | <!-- since 2.4.0 -->
| `SecurityIncident.Read.All` *(new in 2.4)* | Defender's incidents, which group related alerts: name, severity, status, who it's assigned to and when. | So one attack shows once, with whoever is dealing with it. | Security alerts. Not offered in Microsoft 365 operated by 21Vianet. | <!-- since 2.4.0 -->
| `ServiceHealth.Read.All` *(new in 2.4)* | Whether each Microsoft 365 service is working, and the outages and advisories Microsoft reports for your tenant: title, service, status and dates. | So a problem on Microsoft's side is known before anyone spends time troubleshooting it. | Service health. | <!-- since 2.4.0 -->
| `ServiceMessage.Read.All` *(new in 2.4)* | Microsoft's message centre posts for your tenant: title, services, category, whether it's a major change and any date to act by. **Not** the post's text. | So a change with a deadline, such as a feature retiring, is seen before the deadline passes. | Service health. | <!-- since 2.4.0 -->
| `Exchange.ManageAsApp` | Lets the app use Exchange Online's admin commands. Office Sentry only runs the `Get-` commands listed above. | Mailbox forwarding, who has access to which mailbox, shared mailboxes, mailbox sizes, holds and archive mailboxes, which protocols each mailbox allows, phones and tablets syncing mail, distribution lists and how much mail they get, how much mail each domain sends and receives, organisation mail settings, whether the Microsoft 365 audit log is on, mail flow rules and connectors, and the spam, phishing and malware filtering policies. Works only with the Global Reader role (below). | Mail forwarding; Mailbox permissions; Shared mailboxes; Mailbox size; Mailbox size trend; Mailbox holds and archiving; Mobile devices; Distribution lists; Mail flow by domain; Mail rules and protection; Legacy sign-ins; email checks. |

A few things Office Sentry checks come from the **public internet**, not from
your tenant: the SPF, DKIM, DMARC, MTA-STS and TLS-RPT records of your email
domains in public DNS, and, for a domain that publishes an MTA-STS record, its
policy file, fetched over HTTPS from `https://mta-sts.<your domain>/.well-known/mta-sts.txt`
the same way other mail servers fetch it. Anyone on the internet can look
these up; no permission is involved.

Adding a new permission later means a Global Administrator must approve again,
so the list is kept stable, and new permissions arrive together in one release.
Office Sentry 2.2 added the ones marked *new in 2.2*.
Office Sentry 2.4 added the ones marked *new in 2.4*. <!-- since 2.4.0 -->
Until you approve them,
everything else keeps working, and the reports that need them say they're
waiting for your approval. Your MSP sends you the same approval link as before,
with a short "What's new" note saying what each one reads and adds. Within the permissions above, newer versions of
Office Sentry may read more of what a permission allows (for example audit
records under `AuditLog.Read.All`), and keep it for longer, without asking
again. Your MSP's [check catalogue](checks.md) lists exactly which checks use
which data.

## Exchange Online and the Global Reader role

Microsoft Graph doesn't show mailbox forwarding, mailbox permissions or
organisation mail settings. Those come from Exchange Online's admin service,
which only answers a program that has an admin role in your tenant.

Your MSP will ask you to give the Office Sentry app the **Global Reader**
role. Global Reader is Microsoft's built-in role that can view settings
across Microsoft 365 but can't change any of them. It does not give access to
the contents of mailboxes. Office Sentry uses it only for the Exchange
commands listed above.

This step is optional. Without it, the mailbox reports and the email checks
that need Exchange stay empty, and everything else works. Inbox rules are read
through Microsoft Graph instead, so they work either way; only the names of
the folders they move mail to come from Exchange.

## What Office Sentry does not access

Office Sentry has no permission to read, and never reads:

- **Email contents**: messages, attachments, calendars or contacts. It doesn't
  have `Mail.Read` or any similar permission.
- **File contents**: it lists files in OneDrive, SharePoint and Teams (names,
  sizes, dates and sharing) but never opens or downloads them.
- **Teams**: no chats or channel messages.
- **Passwords or MFA secrets**: Microsoft never gives these to any program.
  Office Sentry sees only *which kinds* of sign-in method someone has
  registered.

What it does read about mailboxes is settings: forwarding addresses, who can
open or send from a mailbox, and inbox rules. An inbox rule includes its name,
its conditions (for example "subject contains *invoice*" or "from
*someone@example.com*") and what it does (for example "move to folder" or
"forward to"). For rules that move mail, it also reads the name of the
destination folder from the rule in Exchange (`Get-InboxRule`). It never opens
the folder. For a mailbox with an automatic reply (out of office) turned on or
scheduled, it keeps when the reply runs, whether it also answers people outside
the organisation, and the first 300 characters of its message, so your MSP can
see what it tells people.

<!-- since 2.4.0 -->
## The audit log add-on, if you approve it

Your MSP may also ask you to approve a second, separate app, **Office Sentry
audit log add-on**. It's optional: if you don't approve it, nothing else on
this page changes.

- **One permission**: *Read activity data for your organization*
  (`ActivityFeed.Read` on the Office 365 Management APIs). It has none of the
  permissions above.
- **What it reads**: your Microsoft 365 audit log for SharePoint, OneDrive and
  Exchange, of which it keeps a short list of activities: files deleted,
  downloaded, moved or shared; mail read, sent or deleted by someone other
  than the mailbox's owner (an admin or a delegate); inbox rules, forwarding
  and mailbox permissions set up; and changes to auditing itself. Files
  opened or edited, and people reading their own mail, are left out.
- **What it never reads**: email, attachments, file contents, chats or
  channel messages. The audit log says *that* something happened and who did
  it, not what was in it, and Office Sentry doesn't keep the email subjects
  some audit records carry.
- **Two requests that aren't reads.** Microsoft's audit log feed needs one to
  start and one to stop: Office Sentry sends *start* once you've approved the
  add-on, and *stop* when your MSP switches it off, or when part of the feed is
  turned off in your tenant (it then stops the rest). They tell Microsoft whether
  to make your new audit records available to the add-on; neither changes
  anything in your tenant.

It's removed the same way as the main app (see *How to remove access*,
step 2), by deleting "Office Sentry audit log add-on" instead.
<!-- /since -->

## Where the information is kept

Office Sentry runs on hosting your MSP chooses, so some of the details below
depend on your MSP. Ask them where their server is and who can access it.

- **Storage.** Everything Office Sentry collects is kept on the MSP's Office
  Sentry server: in its database, and the activity history in a file per
  month beside it. It isn't sent to anyone else. These files are not
  encrypted by Office Sentry; protecting the server and its disks is part of
  your MSP's hosting.
- **The certificate key.** The private key Office Sentry signs in with is
  stored encrypted (Fernet: AES with an integrity check) inside the database.
  The encryption key isn't in the database: the server is given it as a
  setting, or keeps it in a separate file readable only by Office Sentry. The key never
  leaves Office Sentry and can't be downloaded; only the public half of the
  certificate can be.
- **Email delivery.** If your MSP schedules monthly reports by email, the
  report is sent as PDF and spreadsheet attachments, through your MSP's own
  mail server or Microsoft 365 mailbox, to the addresses they enter.

### How long it is kept

These are Office Sentry's defaults. Your MSP can change each of them under
*Settings → Data retention*, shorter or longer, and some can be set to keep
data forever; ask them what they use.

- **Activity history** (the sign-in and audit logs described under
  `AuditLog.Read.All` above): every sign-in for **180 days**, the daily
  sign-in summaries for **25 months** and every directory audit event for
  **7 years**.
  If you approved the audit log add-on, the activities it keeps are kept for **2 years**. <!-- since 2.4.0 -->
- **Detailed snapshots** (lists of users, mailboxes, policies and so on) are
  kept for the last 30 days, plus one per month for the last 12 months, and
  older ones are deleted automatically. The most recent collection of each
  kind is always kept, however old. **Daily history**, a compact record of
  how each of those items changed so any earlier day can be shown again, is
  kept for **25 months**.
- **Numbers** used for trends (counts, scores and percentages) are kept
  indefinitely.
- **Collection logs** (what each collection did) are kept for 90 days; after
  that only their warnings and errors are kept.
- **Findings history** (each issue found, with the account or item it
  concerns, and when it was first seen and fixed), **reports sent to you**
  and the **activity log** (who in the MSP did what, including report
  downloads, with the address it came from) are kept until your MSP removes
  them; failed sign-ins in the activity log are removed after 90 days.
- **When your MSP stops working with you** and deletes your organisation in Office
  Sentry, everything collected from it is deleted and overwritten, including
  the activity history; activity log entries keep who did what and when,
  without your people's names or addresses. The MSP's backups keep a copy
  until they rotate out: by default the nightly copies on the server go after
  two weeks, and off-site copies, if your MSP keeps them, after a year.

### Who at your MSP can see it

Office Sentry hides every client from every user unless they are allowed to
see it. Anyone looking for a client they aren't allowed to see gets "not
found".

What each person can do is their **role**, a set of capabilities such as
which reports open for them, downloading files or approving reports, and
which clients they see:

| Office Sentry user | Can see |
|---|---|
| Administrator | Every client, plus Office Sentry's own settings and users. |
| Team leader | Every client and every report, but not Office Sentry's settings or users. |
| Second line, First line, Account manager, Auditor, and roles your MSP makes | Only the clients they've been given (one by one or through a group of clients), or every client if allowed. Within those, only what their role opens: a First line technician, for example, sees people, identity, email and device reports, but not licences. |
| Client viewer | Only the clients they've been given, and only reports and findings, never the collection logs. Your MSP may give you and your colleagues client accounts. |

Your MSP can also give or take away single capabilities for one person, on
every client or on one; taking away always wins.

Your MSP's staff may sign in with their own Microsoft work account, under
the MSP's own sign-in policies (Conditional Access and MFA), or with a
password and a code from an authenticator app. New installs require that code
for everyone who signs in with a password, client accounts included. Client
accounts can also sign in with their own Microsoft 365 work account, under
your sign-in policies, if your MSP turns that on and your administrator
approves the *Office Sentry sign-in* app once; that app only learns each
person's name and sign-in address.
Signed-in sessions end after 2 hours without use and 8 hours in total. Report
downloads are recorded with the user's name.

## How to remove access

You can take Office Sentry's access away at any time, without your MSP's
help. You need a Global Administrator (or another admin who can manage
enterprise apps and roles).

1. **Remove the Global Reader role**, if you gave it: in the Microsoft Entra
   admin centre, go to *Roles and admins* → *Global Reader*, select the
   Office Sentry app and choose *Remove assignments*.
2. **Delete the app from your tenant**: in the Microsoft Entra admin centre,
   go to *Enterprise applications*, open the Office Sentry app (usually called
   "Office Sentry (read-only)"; your MSP can confirm the name), choose
   *Properties* and then *Delete*. This removes the consent you gave. From
   then on Office Sentry can't sign in to your tenant.
3. **Delete the sign-in app too**, the same way, if you approved it so your
   people can sign in to their reports with their Microsoft account (usually
   called "Office Sentry sign-in"; your MSP can confirm the name). After that
   they can't sign in with it.
   Delete *Office Sentry audit log add-on* the same way, if you approved it. <!-- since 2.4.0 -->

To see what you approved without removing it, open the same enterprise app
and choose *Permissions*.

Removing access stops new collections. It doesn't delete what Office Sentry
has already collected; ask your MSP to delete your organisation's data.
