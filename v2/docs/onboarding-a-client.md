# Onboarding a client

For MSP technicians adding a client's Microsoft 365 tenant to Office Sentry,
getting the first data in, keeping its connection healthy, reading what the
**Manage** and **Data collection** pages (under **Settings**) say, and offboarding the client.

Office Sentry itself must already be installed and running: see the
[README](../README.md) for installation. For what the client is approving, send
them [What Office Sentry reads](what-office-sentry-reads.md). Every check, data
source and permission is listed in the [check catalogue](checks.md). If
something goes wrong, [Troubleshooting](troubleshooting.md) has the common
errors and their fixes.

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
  You set this up once for all clients: press *Sign in to Microsoft* there and
  Office Sentry creates the app, then offers to add your first client. On a
  fresh install, the *Get started* checklist on your home page walks
  through this, the first client, its consent and its first collection.
- **You can manage clients.** Adding clients, seeing consent links and
  changing or deleting clients need the *Manage clients* capability, which
  Administrators and Team leaders have. Everyone else on your team can see
  connection health for the clients they're given, and anyone who can
  *Refresh data* (Second line, for example) can press *Check connection now*.
- **The client has someone who can sign in as a Global Administrator** of
  their tenant, or you reach the client through Partner Center with a GDAP
  role that includes Privileged Role Administrator (see *Through Partner
  Center (GDAP)* below). Approving Microsoft Graph application permissions
  for the whole organisation needs one of those two roles.

## 1. Add the client

1. *Clients → Add client*.
2. **Client name**: how the client appears in Office Sentry and on reports.
3. **Tenant ID or domain**: the client's Directory (tenant) ID, or any domain
   the tenant uses, such as `contoso.onmicrosoft.com`. A domain is replaced by
   the tenant ID once consent is granted.
4. If you have more than one app connection, pick one. Most MSPs have one.

The client's **Overview** then shows a **setup checklist**. It ticks itself
off from what Office Sentry can see, and stays until every step is done:

1. Client added.
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
- **When the client was added by domain**, Office Sentry asks Microsoft's
  public sign-in metadata which tenant ID that domain belongs to, and only
  switches to the tenant ID that comes back with the consent if they match.
  If Microsoft can't be asked just then, the consent is recorded and the
  client keeps using the domain (which works); you can enter the tenant ID
  under *Manage → Settings* later. A client that already has its tenant ID
  never changes it on consent.

So the quickest way is to open the link **yourself**, in the browser where you
are signed in to Office Sentry, and sign in to Microsoft with an account that
is a Global Administrator of the client's tenant.

### Sending the link to the client

To have the client's Global Administrator approve it, send them the client
link instead: *Settings → Client settings → Onboarding*, then the client's
**Send…** (or **Copy link** in the list). That page has the link, a message to
paste into an email, a **Consent pack PDF** to attach (how to approve, what
Office Sentry reads and never reads, and how to remove it) and **Mark as
sent**. The client link is different from the one above:

- It never expires and is the same every time, so the printed pack, the
  email and the list all match.
- When the client's administrator approves, Microsoft sends them to a
  thank-you page from you (with the Global Reader step), not to your sign-in
  page.
- Coming back through it records nothing by itself. Office Sentry confirms
  the consent by **signing in to the tenant** as the app: within a few
  minutes of the client approving, then hourly for a month after you mark the
  link as sent, or straight away with **Check now**. Once that works it records
  the consent, swaps a domain for the tenant ID Microsoft publishes for it,
  and queues the first full collection. Until then the Onboarding list says
  *Approved, confirming*, with Microsoft's reason if signing in still fails.

**Check connection now** on the Manage page still works too, whichever way
the client approved.

### Through Partner Center (GDAP)

If you manage the client through Partner Center with granular delegated
admin privileges (GDAP), a technician can approve Office Sentry for the
client with their own partner account, without a Global Administrator
account in the client's tenant:

1. The technician's GDAP relationship with the client includes
   **Privileged Role Administrator** (or Global Administrator), and they've
   activated it if it's just-in-time.
2. They open the client's consent link (from the Onboarding page, or **Grant
   admin consent** on the client's checklist if they're also an Office
   Sentry admin) and sign in with their partner account. The link names the
   client's tenant, which is what makes Microsoft apply their GDAP roles
   there; a link to `/common` or `/organizations` would consent in the
   partner's own tenant instead.
3. They accept the permissions. Office Sentry confirms it by signing in, as
   above.
4. In the client's Microsoft Entra admin centre (from Partner Center's
   *Service management* links), they assign Global Reader to the app, as in
   step 3 below.

What Microsoft documents, and what it means here:

| Microsoft says | Source |
|---|---|
| An app with application-only permissions "must be manually consented to, either by the partner or customer ... using tenant-wide admin consent URL." | [GDAP FAQ](https://learn.microsoft.com/en-us/partner-center/customers/gdap-faq), "What action must a partner perform moving from DAP to GDAP regarding consent?" |
| "If you have the Global Administrator role in a customer's tenant, you can consent to the app yourself", with the URL `https://login.microsoftonline.com/{customertenant}.onmicrosoft.com/adminconsent?client_id={appid}`. | [Migrate your CPV/CSP applications to GDAP](https://learn.microsoft.com/en-us/partner-center/developer/gdap-and-secure-application-model), "Manually requesting app consent" |
| Privileged Role Administrator can grant consent "for apps requesting any permission, for any API"; Cloud Application Administrator and Application Administrator can't for "Microsoft Graph app roles (application permissions)". | [Grant tenant-wide admin consent](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/grant-admin-consent), "Prerequisites" |
| "All Microsoft Entra tasks are supported" through GDAP except a short list; the only application items on it are consenting "inline with sign-in" and enterprise app "User settings". | [Workloads supported by GDAP](https://learn.microsoft.com/en-us/partner-center/customers/gdap-supported-workloads), "Microsoft Entra ID tasks" |
| Adding someone to a directory role needs Privileged Role Administrator. | [Least-privileged GDAP roles by task](https://learn.microsoft.com/en-us/partner-center/customers/gdap-least-privileged-roles-by-task) |

So:

- **Cloud Application Administrator or Application Administrator isn't
  enough** for Office Sentry, although the GDAP FAQ names them: Office Sentry
  asks for Microsoft Graph application permissions, and Entra only lets
  Privileged Role Administrator or Global Administrator approve those.
- **Partner Center's consent API doesn't help**: it grants delegated (app +
  user) permissions, and Microsoft says explicit application-only consent
  through it "isn't supported for third-party application developers who use
  GDAP". Office Sentry never calls Partner Center.
- A technician holding only Global Reader or Security Reader through GDAP
  can get "No Access" on *Roles and admins* in a tenant with PIM turned on
  (a known GDAP issue), so they can't assign Global Reader either.
- The consent appears in the client's audit log under the technician's
  partner account, which is the traceability GDAP is meant to give the
  client. Tell the client before you approve on their behalf.

Office Sentry hasn't been tried against a live GDAP relationship yet (its
test tenant has none). It doesn't need to know how consent was given: the
sign-in check above records it either way. If you try it, the Onboarding page
shows *Consented* within minutes of pressing **Check now**.

What you see when you come back from the consent page:

| Message | Meaning |
|---|---|
| *Consent recorded. A first collection has been queued.* | Done. |
| *Consent was not granted: …* | The admin cancelled, or Microsoft refused; Microsoft's reason follows. |
| *Consent came from a different tenant than this one.* (or *than contoso.com*) | Someone approved from another tenant, for example signed in to your own MSP tenant, or the link was tampered with. Nothing is recorded. Sign in to the client's tenant and try again. |
| *That consent link has already been used or has expired …* | The link was used before, is over an hour old, or came back to a different browser or admin. Nothing is recorded. Open a fresh link from the page. |
| *Office Sentry couldn't confirm the tenant ID …* | Consent is recorded, but the client keeps its domain because Microsoft's sign-in metadata couldn't be read just then. |

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

## Onboarding many clients

*Settings → Client settings → Onboarding* (needs *Manage clients*) is for adding clients in
bulk and following each one through consent.

1. **Import from CSV.** One row per client with column names on the first
   row: `name` and `tenant` (the tenant ID or any domain the tenant uses), and
   optionally `group` (several separated by `;`; missing groups are created),
   `contact` (`jane@contoso.com` or `Jane Smith <jane@contoso.com>`, also added
   to the client's email contacts) and `cloud` (empty for Commercial, or GCC
   High, DoD, China). Excel's *Save as CSV* works, comma or semicolon, and so
   do Excel's *Unicode Text* and Windows PowerShell's `ConvertTo-Csv > clients.csv`
   (UTF-16). Up to 1,000 rows at a time; **Download a template** has the
   layout. You see every row first, with its line in the file and what's
   wrong with any of them, which app connection each
   cloud uses, any groups the import creates and who will see the new
   clients through a group. Nothing is added until you press **Import**.
   Clients already in Office Sentry are skipped, never changed.
2. **Send.** Each client's **Send…** page has its link, an email to paste and
   the consent pack PDF (see *Sending the link to the client* above). For a
   mail merge, **Consent links (CSV)** lists every client that hasn't
   consented with its contact and link. Tick clients and press **Mark as
   sent**.
3. **Follow.** The list shows each client's state:

| State | Meaning |
|---|---|
| Not sent | No link marked as sent yet. |
| Sent | Waiting for the client's Global Administrator. Checked hourly for a month. |
| Approved, confirming | Someone came back from the consent page through the client's link; Office Sentry is signing in to confirm (with Microsoft's reason while that fails). |
| Consented | Office Sentry signs in. Global Reader is read by the first collection. |
| Global Reader missing | Consented, but the app doesn't hold Global Reader, so mailbox reports can't be collected (step 3 below). |
| Connected | Consent and Global Reader in place. |

<!-- since 2.4.0 -->
The **Audit log** column says whether the client's Microsoft 365 audit log is recording, read from
Exchange Online with each collection (`Get-AdminAuditLogConfig`, under Global Reader). **Off** means
nobody can search who deleted or shared files, opened someone else's mailbox or created an inbox rule: the
client's admin turns it on in Microsoft Purview (*Audit*, then *Start recording user and admin activity*),
and the **The Microsoft 365 audit log is on** check stays failed until they do. **Not known yet** means
it hasn't been read: the client isn't connected, has no Exchange Online, or Exchange didn't answer.
<!-- /since -->

Imported clients are ordinary clients: people whose role covers every client
(Administrators and Team leaders) see them; everyone else only once they're
given access, one by one or through a group (someone given a group sees the
imported clients in it).

## 5. Connection health: the Manage page

Every client's **Manage** page (under **Settings**, for your team, not client accounts) has a **Connection
health** list, one line per thing that can go wrong, each with what to do:

| Line | What it checks | Typical fix |
|---|---|---|
| App connection | The client has an app connection with a client ID. | Choose it under *Settings*, or add the client ID under *Settings → App connection*. |
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

The client's overall status is one of:

| Status | Meaning |
|---|---|
| Onboarding | Consent isn't recorded yet, or the first full collection isn't done. The checklist says what's next. |
| Healthy | Every line is fine. |
| Needs attention | Something needs fixing: the first line marked *Fix* or *Check* says what. |
| Paused | Collection and emailed reports are paused (see *Pausing and archiving*). |
| Archived | Hidden from lists and reports until restored. |

**Every client at once**: *Settings → Client settings* (also *Connection
health* on the Clients page) lists every client with its status, consent,
missing permissions, Global Reader, last good collection and failing data
sources, the ones that need fixing first. Click a client to open its Manage
page.

People who can manage clients also edit it under *Manage → Settings*: its name, notes for the
team, its app connection (moving to an app with a different client ID needs
consent again), and its tenant ID or domain. The tenant ID can't change once
data has been collected, except from a domain to the tenant ID it stands for
(checked against Microsoft's public sign-in metadata for that domain):
to report on a different tenant, add it as a new client. *Test sign-in* there
signs in from the portal straight away without checking permissions.

## 6. Read the Data collection page

The **Data collection** page (under **Settings**, for anyone who can *Refresh data*) has four parts:

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
| `Checked mailbox permissions for 2000 of N mailboxes this run (2000 per run, so each mailbox is checked about every 2 runs)` / `Checked inbox rules for 2000 of N mailboxes this run …` | Mailbox permissions and inbox rules are read one mailbox at a time, up to 2,000 a run, taking the mailboxes checked longest ago first. The rest show their results from an earlier run, and the warning says when they were checked and how many haven't been checked yet. |
| `Could not read permissions on N mailboxes` / `Could not read inbox rules for N of M mailboxes` | Some mailboxes didn't answer, usually because they aren't set up yet or are on-premises. |
| `Intune managed devices need an Intune licence` | Entra ID device records were collected; Intune devices weren't. |
| `Intune managed devices weren't readable (…)` | `DeviceManagementManagedDevices.Read.All` isn't consented, or Intune isn't set up in the tenant. |
| `Result was truncated by the page limit` | Very large list; see *Truncated* above. |

### Errors on failed runs

Errors start with the service, the HTTP status and Microsoft's error code, for
example `Graph 403 Authorization_RequestDenied: …`. Where the text below comes
from Microsoft rather than Office Sentry, it is marked *typically*.

| Error | Cause and fix |
|---|---|
| `client has no app connection with a client ID` | The app connection is missing its client ID. Add it under *Settings → App connection*. |
| `Graph 401 …` with `AADSTS700016` (typically *Application … was not found in the directory*) | No consent in this tenant yet, or the tenant ID or domain is wrong. Get consent (step 2). |
| `Graph 401 …` with `AADSTS700027` (typically about the client assertion or certificate) | The certificate isn't on the app registration, or it has expired. Check *Settings → App connection* for the expiry date, and that the `.cer` is uploaded to the app. |
| `Graph 403 Authorization_RequestDenied: …` | A permission isn't consented in this tenant, usually after the app gained a permission. Re-consent (see below). |
| `Graph 403 Authorization_RequestDenied: Reading inbox rules was refused for every mailbox; check MailboxSettings.Read consent and any Exchange application access policy` | Inbox rules only. Re-consent, or, if the client restricts apps to certain mailboxes with an Exchange application access policy, ask them to include the mailboxes Office Sentry should read. |
| `Exchange 401 …` or `Exchange 403 …` ending *Exchange refused access. In this tenant, assign the Global Reader role to the Office Sentry app …* | Mailboxes only. Assign Global Reader (step 3), and make sure consent included `Exchange.ManageAsApp`. |
| `Graph 0 network_error: …` / `Exchange 0 network_error: …` | The Office Sentry server couldn't reach Microsoft after several retries. Check its internet access, proxy and DNS. |
| `Exchange 0 too_many_pages: … returned more than 200 pages` | An exceptionally large Exchange result. |
| `worker stopped before the run finished` | The worker was stopped or restarted mid-run. Refresh that data source. |
| `unknown client or collector` | The run refers to a data source that no longer exists, usually after an upgrade. Safe to ignore. |
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

A release that adds permissions adds them together (a batch, listed in the
release notes under **New Microsoft permissions**), so each client approves
once. Until a client does, its Manage page shows the new permissions as *To
do*, reports that need them say they're waiting for the client's approval, and
the client stays *Healthy*; everything else keeps working. *Settings → Client
settings* shows "N new to approve" next to each such client.

1. **Update the app registration** in your own (MSP) tenant, once: *Settings
   → App connection → Update the app*, and sign in as a Global Administrator,
   Application Administrator or Cloud Application Administrator. Office
   Sentry adds the new permissions and keeps everything else
   (`python -m officesentry update-app` does the same). Or add them by hand:
   *App registrations → (the app) → API permissions → Add a permission*,
   application permissions (*Permissions the app asks for* on the same page
   lists them, marked *New*), then press *I've added them*. Don't create
   the app again (with *Sign in to Microsoft* or `create-app`): that makes a
   second app, and existing clients stay on the first. Office Sentry refuses
   a second app for a cloud that already has one from the App connection page.
2. **Ask each client to approve**: *Settings → Client settings → Onboarding*
   lists them under *New permissions*. Each client's *What's new* page has the
   email to send, the approval link (the same one as before) and a PDF saying
   what each new permission reads and adds. Mark it sent; when the client
   comes back from Microsoft's page, Office Sentry re-reads its permissions.
3. Reports that were waiting fill in with the next collection, or press
   **Refresh all data** on the Data collection page.

A permission from before the batch that a client removed is still a problem,
not a *To do*: re-consent with *Re-grant consent (after new permissions)* on
the Manage page.

## Pausing and archiving

On the **Manage** page, people who can manage clients can:

- **Pause collection**, for example while a client's contract is on hold.
  Nothing is collected (scheduled or manual), no emailed reports or alert
  emails go out, and the client drops out of the team digest. Its data, reports
  and findings stay, and the client still shows (marked *Paused*) in lists and
  to its client users. Month-end names it under the queue instead, since
  nothing goes out for it. **Resume** picks up at the next nightly collection.
- **Archive** a client you've stopped working with but who may come back. An
  archived client is also paused, and is hidden from the Clients page, All
  clients, What changed, the Reports page, digests and its client users.
  It's under *Settings → Client settings → Archived*, where
  **Restore** brings it back (still paused, so resume it when ready). Grants
  for your team and client users are kept.

## Offboarding a client

On the client's **Manage** page, choose **Offboard…** (needs *Manage clients*). The page
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
   email history, the files in the client's **Report history** (the reports
   as they were sent or published, linked so you can download them first),
   and which Office Sentry users lose access, with their roles.
4. **Delete permanently**: tick any client accounts that had access to this
   client only, if they should go too (they'd see nothing afterwards), type the
   client's name and press *Delete … permanently*.

Everything is removed in one transaction, and Office Sentry checks that
nothing tenant-scoped is left before it commits; if anything would be, nothing
is deleted. Other users keep their accounts and just lose this client. The
activity log keeps an entry saying who deleted the client, when, and what was
removed.

Erasure, for a client's data-protection request:

- **Activity log**: entries about the client keep who did what and when, but
  details that name the client's people (accepted findings, which hold a
  user's sign-in name; contacts' email addresses; alert errors) are replaced
  with *(removed when the client was deleted)*. Client accounts deleted with
  the client are renamed *deleted client account #N* in the log and their
  addresses are cleared; the offboarding entry lists their usernames as the
  record of what was erased.
- **Database file**: the delete overwrites the removed rows
  (`secure_delete`), then compacts the file and empties the write-ahead log,
  so nothing can be read back from free space.
- **Backups**: copies made by the `backup` command before the
  delete still contain the client until they rotate out (the last 7 are
  kept), and so do any copies you took off the server. Delete those yourself
  if the client needs erasure sooner.

Whoever runs the server can do the same without the portal. With Docker, from
the `v2` folder on the server (back up first):

```bash
docker compose exec web python -m officesentry backup
docker compose exec web python -m officesentry delete-tenant <number>   # asks you to type the client's name
docker compose exec web python -m officesentry delete-tenant <number> --delete-client-users
```

Without Docker, run the same `python -m officesentry ...` commands in the
virtual environment. The number is the one in the client's address
(`/tenants/<number>`).
