# Office Sentry

**Branded Microsoft 365 security and licensing reports for every client, without gathering anything by hand.**
Office Sentry reads each client tenant every night, keeps the history, and turns it into the reports an MSP
actually sends: a monthly review, a cyber-insurance evidence pack, about 30 detail reports, a view across all
clients, and what changed since last month. It never changes anything in a client's tenant.

[![v2](https://github.com/JackD99/OfficeSentry/actions/workflows/v2.yml/badge.svg)](https://github.com/JackD99/OfficeSentry/actions/workflows/v2.yml)

[Website](https://officesentry.co.uk) · [Live demo](https://demo.officesentry.co.uk) · [Example reports (PDF)](examples/) · [Quick start](#quick-start) · [What it reads](v2/docs/what-office-sentry-reads.md) ·
[Security model](#security-model) · [Technical reference](v2/README.md)

![A client's overview: key figures, what needs attention and what changed since last month](docs/screenshots/tenant-overview.png)

<sub>Every screenshot and example report here comes from fictional demo data: made-up companies, people and
`.example` domains.</sub>

## Why

An MSP's technical lead spends days a month on the same chores for every client: exporting MFA status, checking
who forwards mail outside, counting licences nobody uses, filling in insurance questionnaires, and pasting it all
into a report the client will understand. Microsoft keeps the answers in a dozen admin centres, in its own words,
with no history.

Office Sentry does the gathering every night for every client and keeps the results. The report is ready when you
need it: branded with your logo, in plain English, with what changed since last time and what to fix first. You
review it and send it, or let it email itself.

## What you get

### Client reports and PDFs

The **Monthly Security & Licensing Review** opens with a verdict and a plain-English summary, then key figures with
their 4-week change, what to fix first, what changed, Secure Score, licence waste in money and the checks that
passed. It is a web page, a branded PDF and a spreadsheet, and reads well on a phone.

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/review.png" alt="Monthly review in the browser"></td>
    <td width="25%"><img src="docs/screenshots/pdf-review.png" alt="Monthly review PDF cover"></td>
    <td width="25%"><img src="docs/screenshots/phone-review.png" alt="Monthly review on a phone"></td>
  </tr>
  <tr>
    <td>The review in the portal</td>
    <td>The PDF, with your branding (<a href="examples/monthly-security-licensing-review.pdf">open it</a>)</td>
    <td>On a phone</td>
  </tr>
</table>

### SharePoint storage: why it's full and what to clean up

When a client's SharePoint fills up, the storage report shows how full the tenant is (its quota worked out from
its licences) and when it will be full at the current rate, where the space goes (current files, older versions,
recycle bins, Preservation Hold libraries), every site and library by size, and the largest folders and files,
files with the most version history, large files nobody has touched for two years and the biggest recycle bin
items, each with what to do about it. OneDrives that are nearly full get the same treatment.
See the full 19-page [example PDF](examples/sharepoint-storage.pdf) for a client at 88%.

![SharePoint storage: 88% used, full in about 3 months, and where the space goes](docs/screenshots/sharepoint-storage.png)

<table>
  <tr>
    <td width="60%"><img src="docs/screenshots/sharepoint-storage-files.png" alt="Largest files, with versions and last change"></td>
    <td width="40%"><img src="docs/screenshots/pdf-sharepoint-storage.png" alt="SharePoint storage PDF"></td>
  </tr>
  <tr>
    <td>The largest files, with their version history and who changed them last</td>
    <td>The same report as a PDF for the client (<a href="examples/sharepoint-storage.pdf">open it</a>)</td>
  </tr>
</table>

### Detail reports you can filter, save and export

About 30 detail reports across identity, email, files, devices and licences. Every table can be filtered by its
values (or by age, for dates), sorted and have columns hidden; the view can be saved and reused on any tenant, and
the PDF, XLSX and CSV downloads contain exactly the rows shown. Every headline figure opens the rows behind it.

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/mfa.png" alt="MFA coverage"><br>MFA coverage: who has set up MFA, who is required to, and where the two don't match</td>
    <td width="50%"><img src="docs/screenshots/mail-forwarding.png" alt="Mail forwarding"><br>Mail forwarding: every forward outside, by admin setting or inbox rule</td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/mailbox-permissions.png" alt="Mailbox permissions"><br>Mailbox permissions, with leavers and outside accounts flagged</td>
    <td><img src="docs/screenshots/licence-rightsizing.png" alt="Licence right-sizing"><br>Licence right-sizing: who could move to a cheaper licence, and the saving</td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/sign-ins.png" alt="Sign-in activity"><br>Sign-in activity: password guessing, unusual countries, sign-ins without MFA</td>
    <td><img src="docs/screenshots/subscriptions.png" alt="Licences and subscriptions"><br>Licences and subscriptions: cost, renewals, trials and assignment errors</td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/conditional-access.png" alt="Conditional Access coverage"><br>Conditional Access in plain words, and who each policy really covers</td>
    <td><img src="docs/screenshots/devices.png" alt="Devices"><br>Devices: Windows support dates, compliance, encryption, check-ins</td>
  </tr>
</table>

The full list (mailboxes, inbox rules, shared mailboxes, mailbox size, legacy sign-ins, passwords and sign-in
methods, admins and roles, apps and consents, guests and sharing, groups and Teams, email domains with SPF, DKIM,
DMARC and MTA-STS, hybrid identity, admin activity, tenant settings, Microsoft 365 usage, Secure Score and more) is
in the [technical reference](v2/README.md#reports). The **Reports** page finds any of them by name or by the job
you're doing ("Offboarding check", "Insurance renewal", "Storage cleanup").

![The Reports page: every report, searchable, grouped by job](docs/screenshots/reports-library.png)

### One page per person

Everything about one account on one page: sign-in, MFA and methods, admin roles, licences and what they're used
for, mailbox forwarding and delegates, devices, sharing, and a "Needs attention" list, with how the account has
changed over time. Useful for offboarding checks and "was this account compromised?" questions.

![An account page: needs attention, MFA, admin roles, licences and devices](docs/screenshots/person.png)

### Across all clients, and what changed

The **Tenants** page puts every client side by side, worst first. **All tenants** compares any report across every
client (one row each, every figure clickable), and **What changed** lists month-over-month changes: new and fixed
findings, admin, account, Conditional Access, forwarding, app, device and group changes, and a row-by-row comparison
of every detail report.

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/all-tenants.png" alt="All tenants rollup"></td>
    <td width="50%"><img src="docs/screenshots/what-changed.png" alt="What changed across all tenants"></td>
  </tr>
  <tr>
    <td>All tenants: the monthly review's figures for every client (<a href="examples/all-tenants-monthly-review.pdf">PDF</a>)</td>
    <td>What changed in the last 30 days, most important first (<a href="examples/what-changed-all-tenants.pdf">PDF</a>)</td>
  </tr>
</table>

### Emailed reports and alerts

Send any mix of reports weekly, monthly or quarterly at a time in the client's timezone: the client's people get
the email, your team a hidden copy. Stale data is collected again first. There's a cross-tenant digest for your
team, alert emails when a scan finds something new and critical, and Teams, Slack or webhook alerts. Every send is
logged, failures retry automatically, and you can preview at desktop and phone width.

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/emailed-reports.png" alt="A tenant's emailed reports"></td>
    <td width="50%"><img src="docs/screenshots/email-schedule-form.png" alt="Scheduling an email"></td>
  </tr>
  <tr>
    <td>Schedules, alerts, contacts and the delivery history</td>
    <td>Choosing what to send and when</td>
  </tr>
</table>

### Insurance evidence

The **insurance evidence pack** answers the controls cyber-insurance questionnaires ask about (MFA, admin accounts,
legacy sign-in, email authentication, forwarding, logging, leavers, apps, sharing, devices) with *In place*,
*Partly* or *Not in place*, from the data, with the gaps listed.

<table>
  <tr>
    <td width="60%"><img src="docs/screenshots/evidence.png" alt="Insurance evidence"></td>
    <td width="40%"><img src="docs/screenshots/pdf-evidence.png" alt="Insurance evidence PDF"></td>
  </tr>
  <tr>
    <td>Each control with the evidence and the gaps</td>
    <td>The pack as a PDF (<a href="examples/insurance-evidence-pack.pdf">open it</a>)</td>
  </tr>
</table>

### Recommended projects

**Projects** turns each client's findings into the projects an account manager can sell: no Conditional Access
becomes an *Identity and Conditional Access* project, unencrypted laptops a *Device management* one, missing
Business Premium features a *Security licence upgrade*, and so on. Every client's recommendations sit in one list,
worst first, with what was found, how big the job is and where it stands (suggested, proposed, agreed or declined,
with a note). Each client's page downloads a branded proposal PDF to send them. A project drops off by itself
once the tenant is fixed.

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/projects.png" alt="Recommended projects for every client"></td>
    <td width="50%"><img src="docs/screenshots/tenant-projects.png" alt="One client's recommended projects"></td>
  </tr>
  <tr>
    <td>Every client's recommended projects, worst first</td>
    <td>One client's projects with the findings behind them (<a href="examples/recommended-projects.pdf">proposal PDF</a>)</td>
  </tr>
</table>

### A view for clients

Client users can sign in too. They see only their own organisation's reports, key numbers and downloads, never
collection internals, run logs or other clients.

![What a client sees when they sign in](docs/screenshots/client-view.png)

### Managing client tenants

Adding a client shows a setup checklist that ticks itself off: admin consent, Global Reader, the first collection
and every permission granted. **Client tenants** lists every client's connection health, worst first, and each
tenant's **Manage** tab says in plain words what's wrong and where to click to fix it. Clients can be paused or
archived, and **Offboard** walks through a final export and the client's side, then deletes the tenant and
everything stored for it, with no SQL.

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/client-tenants.png" alt="Client tenants: connection health for every client"></td>
    <td width="50%"><img src="docs/screenshots/manage-tenant.png" alt="A tenant's Manage page"></td>
  </tr>
  <tr>
    <td>Connection health for every client</td>
    <td>One client's connection, permissions and settings</td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/onboarding.png" alt="The onboarding checklist for a new client"></td>
    <td><img src="docs/screenshots/dark-sharepoint-storage.png" alt="Dark mode"></td>
  </tr>
  <tr>
    <td>The checklist for a client being added</td>
    <td>Dark mode follows your system setting</td>
  </tr>
</table>

## Example reports

[`examples/`](examples/) has real exports from the demo data: the monthly review (PDF and XLSX), the insurance
evidence pack, SharePoint storage, MFA coverage, mailbox permissions, mail forwarding, licence right-sizing, sign-in
activity, sign-in methods, subscriptions, Conditional Access, an account page, What changed, All tenants and a
recommended projects proposal. All of it is fictional.

## Security model

Office Sentry holds read access to many clients, so it is built to do as little as possible with it.

- **Read-only by construction.** One multi-tenant Entra app asks only for read permissions (`*.Read*`, plus
  `Exchange.ManageAsApp`). The Graph client has no write methods, and the Exchange client refuses any cmdlet that
  isn't on a fixed allow-list of `Get-` cmdlets. It never creates or changes anything in a client's tenant.
- **Certificate sign-in, no credential files.** The app signs in with a certificate whose private key is encrypted
  in the database. Secrets and private keys are never downloadable; only the public `.cer` is, by admins.
- **Tenant isolation.** Deny by default: people see only the tenants they're granted, every tenant page goes
  through one access check, and anything else returns 404. Clients see only their own reports.
- **The portal itself:** two-step sign-in for staff, lockout after failed attempts, server-side sessions,
  CSRF protection, a strict Content Security Policy with no external assets, and encrypted secrets with key
  rotation.

Details are in [SECURITY.md](SECURITY.md) and the [technical reference](v2/README.md#what-it-does-differently-from-v1).

## What it reads

[What Office Sentry reads](v2/docs/what-office-sentry-reads.md) is written for a client's owner or IT contact who
has been asked to approve access: every permission, what is and isn't read (no email contents), where the data is
kept and how to remove access. Send it with the consent link.

## Quick start

Office Sentry runs as two containers (the portal and a worker) from one image, with everything stored in one
Docker volume.

Download `docker-compose.yml`, `Caddyfile` and `.env.example` from the newest
[release](https://github.com/JackD99/OfficeSentry/releases) into a folder on a Linux server with Docker, then:

```bash
cp .env.example .env          # set OFFICESENTRY_BASE_URL, OFFICESENTRY_DOMAIN and OFFICESENTRY_TIMEZONE
docker compose --profile https up -d
```

The image is `ghcr.io/jackd99/officesentry`, for amd64 servers (arm64 too once the repository is public); each release lists what changed in
[CHANGELOG.md](CHANGELOG.md). While this repository is private the image is too, so the server needs
`docker login ghcr.io` with a GitHub token that can read packages.

Leave out `--profile https` if you already have a reverse proxy, and point it at `http://127.0.0.1:8000`. Then:

1. Run `docker compose logs web` and open the link on the line starting **No admin account yet** to create the
   first admin (with two-step sign-in).
2. Create the multi-tenant, read-only app in your own Entra tenant:

   ```bash
   docker compose run --rm worker python -m officesentry create-app --tenant <your-tenant>.onmicrosoft.com
   ```

   It prints a code: open the link it shows, enter the code and sign in as an admin of **your own** (MSP) tenant
   who can create app registrations. It makes the certificate, registers the app and saves both, so **App
   connection** then shows one connection marked *Ready*. Don't press *Generate certificate* first; that is only
   for creating the app by hand, as the page describes.
3. **Add tenant** and follow its checklist: a Global Administrator of the client's tenant grants consent, you
   assign Global Reader, and the first collection runs.

Stuck? [Troubleshooting](v2/docs/troubleshooting.md) covers the common errors, such as `AADSTS50011` on the
consent page, mailboxes failing without Global Reader and the worker not running.

[v2/DEPLOY.md](v2/DEPLOY.md) covers HTTPS, the collection schedule, retention, backups and updating.

## Documentation

- [Technical reference](v2/README.md): every report and check, Exchange data, sign-in security, setup and layout
- [Running it on your own server](v2/DEPLOY.md)
- [Onboarding a client](v2/docs/onboarding-a-client.md): consent, Global Reader, the first collection, connection
  health and offboarding
- [What Office Sentry reads](v2/docs/what-office-sentry-reads.md): for clients approving access
- [Check catalogue](v2/docs/checks.md): every check, data source, detail report and permission
- [Troubleshooting](v2/docs/troubleshooting.md): the common setup and collection errors and their fixes
- [Example reports](examples/)

## Development

All code is in [`v2/`](v2/): Flask, SQLite and WeasyPrint, with one background worker and no Redis or Celery.

```bash
cd v2
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
ruff check . && pytest
OFFICESENTRY_INSECURE_COOKIES=1 python -m officesentry web   # http://127.0.0.1:8000
```

`python scripts/demo_data.py --data-dir /tmp/officesentry-demo` builds demo tenants to click around in.
The screenshots and example reports here are made by `python scripts/screenshots.py` from the same demo data;
re-run it when the UI or reports change. See [CONTRIBUTING.md](CONTRIBUTING.md) for conventions.

## Security issues

Please report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md). Don't open a public issue.

## Licence

Office Sentry is free software under the [GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0-only).
You can use, change and share it; if you run a modified version for others over a network, you must offer them
its source code under the same licence.

## The old v1 portal

The original v1 portal (MSP operations, 365Inspect, PowerShell scripts, AD agent, GDAP management, Graph Explorer)
has been removed. Its last state is on the `v1-final` branch.
