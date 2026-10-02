# Onboarding a client tenant

For MSP technicians adding a client's Microsoft 365 tenant to Office Sentry,
getting the first data in, and reading what the **Data collection** tab says.

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
  links and complete consent.
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

Within a minute, the worker also queues a scheduled collection for the new
tenant. Before consent these runs fail. That is expected; the runs after
consent replace them.

## 2. Get admin consent

The tenant's **Overview** tab shows *Grant admin consent* until consent is
recorded. The same link is on the **Data collection** tab.

The link opens Microsoft's consent page for the client's tenant. It lists
every permission (all read-only), and a Global Administrator approves them.
Microsoft then sends the browser back to Office Sentry, which records the
consent.

Two things decide whether Office Sentry records it:

- **The link works for one hour.** It is regenerated each time the page loads,
  so reload the page for a fresh one. An older link ends on an error page
  when it returns.
- **The return page needs a signed-in Office Sentry admin**, in the same
  browser.

So the reliable way is to open the link **yourself**, in the browser where you
are signed in to Office Sentry, and sign in to Microsoft with an account that
is a Global Administrator of the client's tenant.

If you send the link to the client's Global Administrator instead, their
approval still takes effect at Microsoft. But Microsoft then sends them to your
Office Sentry sign-in page, and Office Sentry doesn't record the consent: the
tenant keeps showing *Awaiting consent*. Collection works anyway (see step 4).
To clear the status, open a fresh link yourself later as described above.

What you see when you come back:

| Message | Meaning |
|---|---|
| *Consent recorded. A first collection has been queued.* | Done. |
| *Consent was not granted: …* | The admin cancelled, or Microsoft refused; Microsoft's reason follows. |
| *Consent came from a different tenant than this one.* | Someone approved from another tenant, for example signed in to your own MSP tenant. Sign in to the client's tenant and try again. |

## 3. Assign Global Reader (for mailbox data)

Mailboxes, forwarding addresses, mailbox permissions and organisation mail
settings come from Exchange Online, which only answers an app that holds an
admin role in the tenant. Office Sentry needs **Global Reader**, which can
view but not change settings.

In the client's tenant: *Microsoft Entra admin centre → Roles and admins →
Global Reader → Add assignments*, then select the Office Sentry app (the name
you gave the app registration, for example "Office Sentry (read-only)").

Without it, the **Mailboxes** data source fails and the checks that need it
show as not checked. Everything else works, including inbox rules, which are
read through Microsoft Graph. Microsoft can take a while to apply a new role
assignment; if Mailboxes still fails straight afterwards, refresh it again
later.

## 4. First collection

Recording consent queues only **Tenant profile and licences**. To collect
everything straight away, open the **Data collection** tab and press
**Refresh all data**. Otherwise the rest arrives with the nightly collection
(at `OFFICESENTRY_NIGHTLY_HOUR`, default 02:00, in `OFFICESENTRY_TIMEZONE`,
default UTC).

*Refresh all data* queues every data source, tenant profile first: it finds
out which licences the tenant has, and the others use that to skip what the
tenant can't provide. The worker runs one data source at a time, so a large
tenant can take a while.

If the client opened the consent link and Office Sentry still says *Awaiting
consent*, *Refresh all data* still works: collection doesn't depend on the
recorded consent, only on Microsoft's.

## 5. Read the Data collection tab

The **Data collection** tab (admins and analysts) has four parts:

- **Connection**: tenant ID, app, and whether consent is recorded. Admins also
  get the consent link here.
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
tenant does, data sources that need the new permission fail with `Graph 403
Authorization_RequestDenied` (or are partial).

1. **Add the permission to the app registration** in your own (MSP) tenant:
   *App registrations → (the app) → API permissions → Add a permission*,
   application permissions. *Settings → App connection → Permissions the app
   asks for* lists what this version of Office Sentry expects. Don't run
   `create-app` again: it creates a second app, and existing tenants stay on
   the first.
2. **Re-consent each tenant**: on the **Data collection** tab, open *Re-grant
   consent (after new permissions)*, the same way as step 2.
3. Press **Refresh all data**.

## Offboarding a client

1. **Stop emailed reports**: on the tenant's **Emailed reports** tab, delete
   its schedules.
2. **Remove client access**: under *Settings → Users*, delete the client's
   Office Sentry accounts, or untick the tenant on analysts who no longer need
   it.
3. **Remove Office Sentry from their tenant**, or ask them to: delete the
   Office Sentry enterprise app (*Enterprise applications → (the app) →
   Properties → Delete*) and remove its Global Reader assignment. The client
   can do this themselves; [What Office Sentry reads](what-office-sentry-reads.md#how-to-remove-access)
   has the steps.
4. **Delete the stored data.** The portal has no button to remove a tenant.
   Nightly collections continue for it (and fail once the app is deleted)
   until the tenant is removed from the database. Whoever runs the server can
   do that: back up the database, then run this against
   `<data directory>/officesentry.db`, with the tenant's number from its
   address (`/tenants/<number>`):

   ```sql
   PRAGMA foreign_keys = ON;
   DELETE FROM tenants WHERE id = <number>;
   ```

   That removes the tenant with its runs, collected data, figures, findings,
   schedules and access grants. Lines in the activity log that mention the
   tenant stay.
