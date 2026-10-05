# Onboarding a client tenant

For MSP technicians adding a client's Microsoft 365 tenant to Office Sentry,
getting the first data in, keeping its connection healthy, reading what the
**Manage** and **Data collection** pages (under **Settings**) say, and offboarding the client.

Office Sentry itself must already be installed and running: see the
[README](../README.md) for installation. For what the client is approving, send
them [What Office Sentry reads](what-office-sentry-reads.md). Every check, data
source and permission is listed in the [check catalogue](checks.md).

## Before you start

- **Office Sentry is running**, both the web portal and the **worker**. The
  worker does all collection; without it, runs stay *Queued*.
- **`OFFICESENTRY_BASE_URL` is the address you open Office Sentry on.** The
  consent link sends people back to `<base URL>/consent/callback`, and that
  must match the redirect URI on the app registration exactly. *Settings →
  App connection* shows the redirect URI Office Sentry expects. If they
  differ, Microsoft shows error `AADSTS50011` on the consent page.
- **The app connection is ready.** *Settings → App connection* shows the
  connection as *Ready* once it has a certificate and the app's client ID.
  You set this up once for all clients.
- **You are an Office Sentry admin.** Only admins can add tenants, see consent
  links and change or delete tenants. Analysts can see connection health and
  press *Check connection now*.
- **The client has someone who can sign in as a Global Administrator** of
  their tenant. Only a Global Administrator can approve application
  permissions for the whole organisation.

## 1. Add the tenant

1. *Tenants → Add tenant*.
2. **Client name**: how the tenant appears in Office Sentry and on reports.
3. **Tenant ID or domain**: the client's Directory (tenant) ID, or any domain
   the tenant uses, such as `contoso.onmicrosoft.com`. A domain is replaced by
   the tenant ID once consent is granted.
4. If you have more than one app connection, pick one. Most MSPs have one.

The tenant's **Overview** then shows a **setup checklist**. It ticks itself
off from what Office Sentry can see, and stays until every step is done:

1. Tenant added.
2. Admin consent granted (step 2 below).
3. Global Reader assigned (step 3).
4. First full collection done (step 4).
5. All permissions granted: the permissions the client's tenant actually
   granted match what Office Sentry asks for.

The **Manage** page shows the same checklist above the connection health.

## 2. Get admin consent

Press **Grant admin consent** on the checklist (also under *Manage →
Settings*). The link opens Microsoft's consent page for the client's tenant.
It lists every permission (all read-only), and a Global Administrator
approves them. Microsoft then sends the browser back to Office Sentry, which
records the consent and **queues a full collection**: nothing else to press.

Two things decide whether Office Sentry records it:

- **The link works once, for one hour.** It is regenerated each time the page
  loads, so reload the page for a fresh one. An older or already used link
  records nothing when it returns.
- **The return page needs the same signed-in Office Sentry admin, in the same
  browser** that opened the link. A link opened from another browser or by
  another admin records nothing.
- **When the tenant was added by domain**, Office Sentry asks Microsoft's
  public sign-in metadata which tenant ID that domain belongs to, and only
  switches to the tenant ID that comes back with the consent if they match.
  If Microsoft can't be asked just then, the consent is recorded and the
  tenant keeps using the domain (which works); you can enter the tenant ID
  under *Manage → Settings* later. A tenant that already has its tenant ID
  never changes it on consent.

So the quickest way is to open the link **yourself**, in the browser where you
are signed in to Office Sentry, and sign in to Microsoft with an account that
is a Global Administrator of the client's tenant.

If you send the link to the client's Global Administrator instead, their
approval still takes effect at Microsoft, but Microsoft sends them to your
Office Sentry sign-in page and the consent isn't recorded there. Once they've
approved, press **Check connection now** on the checklist or the Manage page:
when Office Sentry can sign in and read the tenant, it records the consent
itself and queues the first full collection.

What you see when you come back from the consent page:

| Message | Meaning |
|---|---|
| *Consent recorded. A first collection has been queued.* | Done. |
| *Consent was not granted: …* | The admin cancelled, or Microsoft refused; Microsoft's reason follows. |
| *Consent came from a different tenant than this one.* (or *than contoso.com*) | Someone approved from another tenant, for example signed in to your own MSP tenant, or the link was tampered with. Nothing is recorded. Sign in to the client's tenant and try again. |
| *That consent link has already been used or has expired …* | The link was used before, is over an hour old, or came back to a different browser or admin. Nothing is recorded. Open a fresh link from the page. |
| *Office Sentry couldn't confirm the tenant ID …* | Consent is recorded, but the tenant keeps its domain because Microsoft's sign-in metadata couldn't be read just then. |

## 3. Assign Global Reader (for mailbox data)

Mailboxes, forwarding addresses, mailbox permissions and organisation mail
settings come from Exchange Online, which only answers an app that holds an
admin role in the tenant. Office Sentry needs **Global Reader**, which can
view but not change settings.

In the client's tenant: *Microsoft Entra admin centre → Roles and admins →
Global Reader → Add assignments*, then select the Office Sentry app (the name
you gave the app registration, for example "Office Sentry (read-only)"). The
Manage page names the app as it appears in the client's tenant.

Without it, the **Mailboxes** data source fails and the checks that need it
show as not checked. Everything else works, including inbox rules, which are
read through Microsoft Graph. Microsoft can take a while to apply a new role
assignment; press **Check again** on the checklist after a few minutes, and
refresh Mailboxes once it shows as assigned.

If the app holds **Global Administrator** or **Exchange Administrator**, the
Manage page warns: those work, but give the app far more than reporting needs.
Ask the client to remove them and keep Global Reader.

## 4. First collection

The full collection starts by itself when consent is recorded (or when *Check
connection now* first succeeds after the client consented on their own). The
checklist shows *Collecting now* until every data source has run, then ticks
the step. Otherwise the rest arrives with the nightly collection
(`OFFICESENTRY_COLLECT_CRON`, default 02:00 every night, in
`OFFICESENTRY_TIMEZONE`, default UTC).

The worker runs one data source at a time, tenant profile first: it finds out
which licences the tenant has, and the others use that to skip what the tenant
can't provide. A large tenant can take a while. To collect again later, use
**Refresh all data** on the **Data collection** page.

## 5. Connection health: the Manage page

Every tenant's **Manage** page (under **Settings**, admins and analysts) has a **Connection
health** list, one line per thing that can go wrong, each with what to do:

| Line | What it checks | Typical fix |
|---|---|---|
| App connection | The tenant has an app connection with a client ID. | Choose it under *Settings*, or add the client ID under *Settings → App connection*. |
| Admin consent | Consent is recorded. | Grant admin consent (step 2). |
| Sign-in | Office Sentry can sign in to the tenant. Failures are explained in plain words (see *Errors on failed runs* below). | Usually the certificate or consent. |
| Permissions | Which of the app's permissions the tenant actually granted, read from the app's own enterprise app in the tenant. Missing ones are listed by name. | A Global Administrator opens the consent link again; it grants every permission at once. |
| Global Reader role | Whether the app holds Global Reader (and nothing more powerful). | Step 3. |
| App enabled | Shown only when the client disabled the app. | *Enterprise applications → (the app) → Properties → Enabled for users to sign in: Yes*. |
| Licences found | The licence features the tenant profile found. | Nothing to fix; unlicensed checks show as not checked. |
| Collection | Every data source has been collected, and none is older than 36 hours. | Check the worker is running. |
| Data sources | Data sources whose latest run failed, with the first error explained. | See *Data collection* for each one. |

Permissions and Global Reader are read by the **Connection and permissions**
data source (`connection`), which runs with every collection using only GET
requests and the permissions the app already has (`Application.Read.All`,
`RoleManagement.Read.Directory`). **Check connection now** queues it on its
own; the result appears within a minute or so (reload the page).

The tenant's overall status is one of:

| Status | Meaning |
|---|---|
| Onboarding | Consent isn't recorded yet, or the first full collection isn't done. The checklist says what's next. |
| Healthy | Every line is fine. |
| Needs attention | Something needs fixing: the first line marked *Fix* or *Check* says what. |
| Paused | Collection and emailed reports are paused (see *Pausing and archiving*). |
| Archived | Hidden from lists and reports until restored. |

**Every client at once**: *Settings → Client tenants* (also *Connection
health* on the Tenants page) lists every tenant with its status, consent,
missing permissions, Global Reader, last good collection and failing data
sources, the ones that need fixing first. Click a tenant to open its Manage
page.

Admins also edit the tenant under *Manage → Settings*: its name, notes for the
team, its app connection (moving to an app with a different client ID needs
consent again), and its tenant ID or domain. The tenant ID can't change once
data has been collected, except from a domain to the tenant ID it stands for
(checked against Microsoft's public sign-in metadata for that domain):
to report on a different tenant, add it as a new one. *Test sign-in* there
signs in from the portal straight away without checking permissions.

## 6. Read the Data collection page

The **Data collection** page (under **Settings**, admins and analysts) has four parts:

- **Connection**: tenant ID, app, whether consent is recorded, the status
  and the top problems from the Manage page, with a link to it.
- **Licences found**: the licence features the tenant profile found (Entra ID
  P1 and P2, Intune, Defender for Office 365 P1 and P2, Exchange Online,
  SharePoint Online). Unlicensed features are greyed out.
- **Data sources**: every collector with its latest status, when it last
  finished, the error or warnings, and a *Refresh* button for that source.
- **Recent runs**: the last 50 runs. Click one to see its log, the numbers it
  recorded and the raw items it collected.

Run statuses:

| Status | Meaning |
|---|---|
| Queued / Running | Waiting for, or being processed by, the worker. |
| Succeeded | Everything was collected. |
| Partial | Collected, but part was skipped or limited. The warnings say what. Reports use the data. |
| Failed | The run didn't finish. Reports ignore it and keep using the last successful run, if there is one. |
| Truncated (on the run page) | A list was longer than the paging limit, so not every item was read. |

### Warnings on partial runs

| Warning | What it means |
|---|---|
| `Sign-in activity needs Entra ID P1; collected users without it` | Users were collected without last sign-in dates. The inactive accounts check shows as not checked. |
| `Conditional Access needs Entra ID P1` | Microsoft refused Conditional Access policies for licence reasons. |
| `Expanded the first 200 of N groups used by Conditional Access` | Only the first 200 groups in Conditional Access policies were expanded to their members. |
| `Checked the first 2000 of N enabled members` | MFA registration without Entra ID P1 is checked user by user, up to 2,000. |
| `Microsoft returned no Secure Score for this tenant yet` | Microsoft hasn't calculated a Secure Score for the tenant yet, which is common for new tenants. |
| `Read application permissions for the first 1000 of N apps` | Very many third-party apps; only the first 1,000 were read. |
| `SharePoint sharing settings unavailable: …` | Microsoft's reason follows. The sharing check shows as not checked. |
| `Checked the first 100 of N domains` / `DNS lookups failed for <domain>: …` | Domain checks are limited to 100 domains; a DNS lookup failed from the Office Sentry server. |
| `Checked mailbox permissions on the first 2000 of N mailboxes` / `Could not read permissions on N mailbox(es)` | Mailbox permissions are read one mailbox at a time, up to 2,000. |
| `Read inbox rules for the first 2000 of N mailboxes` / `Could not read inbox rules for N of M mailbox(es)` | Some mailboxes didn't answer, usually because they aren't set up yet or are on-premises. |
| `Intune managed devices need an Intune licence` | Entra ID device records were collected; Intune devices weren't. |
| `Intune managed devices weren't readable (…)` | `DeviceManagementManagedDevices.Read.All` isn't consented, or Intune isn't set up in the tenant. |
| `Result was truncated by the page limit` | Very large list; see *Truncated* above. |

### Errors on failed runs

Errors start with the service, the HTTP status and Microsoft's error code, for
example `Graph 403 Authorization_RequestDenied: …`. Where the text below comes
from Microsoft rather than Office Sentry, it is marked *typically*.

| Error | Cause and fix |
|---|---|
| `tenant has no app connection with a client ID` | The app connection is missing its client ID. Add it under *Settings → App connection*. |
| `Graph 401 …` with `AADSTS700016` (typically *Application … was not found in the directory*) | No consent in this tenant yet, or the tenant ID or domain is wrong. Get consent (step 2). |
| `Graph 401 …` with `AADSTS700027` (typically about the client assertion or certificate) | The certificate isn't on the app registration, or it has expired. Check *Settings → App connection* for the expiry date, and that the `.cer` is uploaded to the app. |
| `Graph 403 Authorization_RequestDenied: …` | A permission isn't consented in this tenant, usually after the app gained a permission. Re-consent (see below). |
| `Graph 403 Authorization_RequestDenied: Reading inbox rules was refused for every mailbox; check MailboxSettings.Read consent and any Exchange application access policy` | Inbox rules only. Re-consent, or, if the client restricts apps to certain mailboxes with an Exchange application access policy, ask them to include the mailboxes Office Sentry should read. |
| `Exchange 401 …` or `Exchange 403 …` ending *Exchange refused access. In this tenant, assign the Global Reader role to the Office Sentry app …* | Mailboxes only. Assign Global Reader (step 3), and make sure consent included `Exchange.ManageAsApp`. |
| `Graph 0 network_error: …` / `Exchange 0 network_error: …` | The Office Sentry server couldn't reach Microsoft after several retries. Check its internet access, proxy and DNS. |
| `Exchange 0 too_many_pages: … returned more than 200 pages` | An exceptionally large Exchange result. |
| `worker stopped before the run finished` | The worker was stopped or restarted mid-run. Refresh that data source. |
| `unknown tenant or collector` | The run refers to a data source that no longer exists, usually after an upgrade. Safe to ignore. |
| Any other `SomeError: …` | An unexpected error in Office Sentry. The run's log has the full details; report it with that log. |

## Checks that show "not checked"

A check that can't be evaluated isn't passed or failed: it appears under *Not
checked* in the monthly review, with the reason. Two kinds of reason:

- **The data hasn't been collected yet**, for example *Conditional Access data
  hasn't been collected yet*. Refresh the data source, or wait for the nightly
  run.
- **The tenant's licence doesn't include the feature.** Office Sentry reads
  the licences first and doesn't ask Microsoft for what the tenant can't have:

| Missing licence | Effect |
|---|---|
| Entra ID P1 | No last sign-in dates, so *No inactive accounts* is not checked and the inactive accounts figure says *Needs Entra ID P1*. No Conditional Access policies, so MFA enforcement and legacy sign-in blocking are judged by Security Defaults alone. MFA registration is read user by user instead of from Microsoft's report. |
| Entra ID P2 | Admin roles show active assignments only, not PIM-eligible ones. |
| Intune | Device compliance, encryption, check-in and enrolment checks are not checked. Entra ID device records and Windows versions still are. |
| Exchange Online | Mailboxes and inbox rules aren't collected, and the email checks that need them are not checked. |
| SharePoint Online | SharePoint sharing settings aren't read, so the anonymous links check is not checked. |

The [check catalogue](checks.md) lists the licence note for each check.

## Re-consent when permissions change

Adding a permission to the app means every tenant must approve again. Until a
tenant does, the Manage page lists the new permission as missing, and data
sources that need it fail with `Graph 403 Authorization_RequestDenied` (or
are partial). *Settings → Client tenants* shows which tenants still miss it.

1. **Add the permission to the app registration** in your own (MSP) tenant:
   *App registrations → (the app) → API permissions → Add a permission*,
   application permissions. *Settings → App connection → Permissions the app
   asks for* lists what this version of Office Sentry expects. Don't run
   `create-app` again: it creates a second app, and existing tenants stay on
   the first.
2. **Re-consent each tenant**: on the **Manage** page, open *Re-grant consent
   (after new permissions)* under Settings, or *Open consent link* next to
   *Permissions*, the same way as step 2.
3. Press **Check connection now**, then **Refresh all data** on the Data
   collection page.

## Pausing and archiving

On the **Manage** page, admins can:

- **Pause collection**, for example while a client's contract is on hold.
  Nothing is collected (scheduled or manual), no emailed reports or alert
  emails go out, and the tenant drops out of the team digest. Its data, reports
  and findings stay, and the tenant still shows (marked *Paused*) in lists and
  to its client users. **Resume** picks up at the next nightly collection.
- **Archive** a client you've stopped working with but who may come back. An
  archived tenant is also paused, and is hidden from the Tenants page, All
  tenants, What changed, the Reports page, digests and its client users.
  Admins find it under *Settings → Client tenants → Archived*, where
  **Restore** brings it back (still paused, so resume it when ready). Grants
  for analysts and client users are kept.

## Offboarding a client

On the tenant's **Manage** page, choose **Offboard…** (admins only). The page
walks through it:

1. **Download a final copy**: the monthly review and insurance evidence pack
   as PDF and XLSX. Detail reports download from their own pages.
2. **Remove Office Sentry from the client's Microsoft 365.** Office Sentry
   can't do this itself; it only reads the tenant. A Global Administrator of
   the client's tenant removes the Global Reader assignment and deletes the
   Office Sentry enterprise app (*Enterprise applications → (the app) →
   Properties → Delete*). The page has the steps, with the app's name in their
   tenant, as text to send the client; they're also in
   [What Office Sentry reads](what-office-sentry-reads.md#how-to-remove-access).
3. **Check what will be deleted**: collection runs and stored data, findings
   (including accepted risks), emailed reports, alert emails, client contacts,
   email history, and which Office Sentry users lose access.
4. **Delete permanently**: tick any client accounts that had access to this
   tenant only, if they should go too (they'd see nothing afterwards), type the
   tenant's name and press *Delete … permanently*.

Everything is removed in one transaction, and Office Sentry checks that
nothing tenant-scoped is left before it commits; if anything would be, nothing
is deleted. Other users keep their accounts and just lose this tenant. The
activity log keeps an entry saying who deleted the tenant, when, and what was
removed.

Erasure, for a client's data-protection request:

- **Activity log**: entries about the tenant keep who did what and when, but
  details that name the client's people (accepted findings, which hold a
  user's sign-in name; contacts' email addresses; alert errors) are replaced
  with *(removed when the tenant was deleted)*. Client accounts deleted with
  the tenant are renamed *deleted client account #N* in the log and their
  addresses are cleared; the offboarding entry lists their usernames as the
  record of what was erased.
- **Database file**: the delete overwrites the removed rows
  (`secure_delete`), then compacts the file and empties the write-ahead log,
  so nothing can be read back from free space.
- **Backups**: copies made by `python -m officesentry backup` before the
  delete still contain the tenant until they rotate out (the last 7 are
  kept), and so do any copies you took off the server. Delete those yourself
  if the client needs erasure sooner.

Whoever runs the server can do the same without the portal (back up first
with `python -m officesentry backup`):

```bash
python -m officesentry delete-tenant <number>            # asks you to type the tenant's name
python -m officesentry delete-tenant <number> --delete-client-users
```

The number is the one in the tenant's address (`/tenants/<number>`).
