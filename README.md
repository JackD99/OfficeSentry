# Office Sentry

**Branded Microsoft 365 security and licensing reports for every client, without gathering anything by hand.**
Office Sentry reads each client's Microsoft 365 every night, keeps the history, and turns it into the reports an MSP
actually sends: a monthly review, evidence for Cyber Essentials and insurance forms, 46 detail reports, a page for
every person, mailbox, group and site, a view across all clients, and what changed since last month. It never changes
anything in a client's tenant.

[![CI](https://github.com/JackD99/OfficeSentry/actions/workflows/v2.yml/badge.svg)](https://github.com/JackD99/OfficeSentry/actions/workflows/v2.yml)
[![Release](https://img.shields.io/github/v/release/JackD99/OfficeSentry?label=release)](https://github.com/JackD99/OfficeSentry/releases/latest)
[![Image](https://img.shields.io/badge/image-ghcr.io%2Fjackd99%2Fofficesentry-2563eb)](https://github.com/JackD99/OfficeSentry/pkgs/container/officesentry)
[![Licence](https://img.shields.io/badge/licence-AGPL--3.0-blue)](LICENSE)

[Website](https://officesentry.co.uk) · [Live demo](https://demo.officesentry.co.uk) · [Example reports (PDF)](examples/) · [Quick start](#quick-start) · [What it reads](v2/docs/what-office-sentry-reads.md) ·
[Security model](#security-model) · [Getting help](SUPPORT.md) · [Technical reference](v2/README.md)

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

The **Monthly Security & Licensing Review** leads with the answer on two summary pages. *At a glance* has what was
checked, what changed, the key figures with their 4-week change and the findings by severity. *This month* has the
findings found and fixed each month, what was done and what to do next. The detail follows as an appendix: what
changed, the Secure Score trend, each failing check with the accounts affected, licence waste in money and the checks
that passed. It is a web page, a branded PDF and a spreadsheet, and reads well on a phone. The **Summary PDF** is the
two summary pages on their own ([open it](examples/monthly-review-summary.pdf)), and report plans and email schedules
can send just that.

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

![This month: findings found and fixed each month, what was done and what to do next](docs/screenshots/review-attention.png)

**Report designs** let an account manager build their own client report without code: drag in blocks (a
verdict, key figures, failing checks, what changed, licences, a table from any detail report, your own text),
set each one up, see it on a real client as you go, and send it with a report plan. Start from an executive
summary, a full review, a licensing review or a **Quarterly review**: three months of Secure Score and findings,
what was fixed, the projects on the table and where each stands, licence spend and what's coming up in the next
90 days ([example PDF](examples/quarterly-review.pdf)).

### SharePoint storage: why it's full and what to clean up

When a client's SharePoint fills up, the storage report shows how full it is (its quota worked out from
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

### Who can reach each site and team

**Site access** shows who owns, administers and can reach each SharePoint site: sites with no enabled owner or
only one, each site's owners, administrators, members and guests, what the default library grants and to whom,
every guest with the sites they can reach, and who administers many sites. **Site permissions** goes a level
deeper with SharePoint's own permissions: who is in each SharePoint group, the permission level each person and
group holds, the lists, libraries and subsites with permissions of their own, and any guest with full control. **Teams channels, guests and apps** does
the same for Teams: each team's private and shared channels with their members, the guests and people from other
organisations in them, the apps added to each team (custom uploads flagged) and teams nobody has used in 90 days.

![Site access: sites with no owner or one, guests, and sites open to everyone in the organisation](docs/screenshots/site-access.png)

### Detail reports you can filter, save and export

46 detail reports across identity, email, files and Teams, devices and licences. Every table can be filtered by its
values (or by age, for dates), sorted and have columns hidden; the view can be saved and reused on any client, and
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

The full list (mailboxes, inbox rules, shared mailboxes, mailbox size and its trend, automatic replies, mobile
devices, distribution lists, mail flow per domain, legacy sign-ins, passwords and sign-in methods, admins and roles,
administrative units, apps and consents, guests and sharing, groups and Teams, Teams channels with their guests and
apps, empty, nested and expiring groups, who can reach each SharePoint site and its permission levels, email domains with SPF, DKIM, DMARC and
MTA-STS, hybrid identity, admin activity, licence changes month by month, tenant settings, Microsoft 365 usage,
Secure Score and more) is in the [technical reference](v2/README.md#reports). The **Reports** page finds any of them
by name or by the job you're doing ("Offboarding check", "Insurance renewal", "Storage cleanup").

![The Reports page: every report, searchable, grouped by job](docs/screenshots/reports-library.png)

### One page per person

Everything about one account on one page: sign-in, MFA and methods, admin roles, licences and what they're used for,
mailbox forwarding and delegates, devices, sharing, the administrative units it's in, and a "Needs attention" list,
with how the account has changed over time. Useful for offboarding checks and "was this account compromised?"
questions.

![An account page: needs attention, MFA, admin roles, licences and devices](docs/screenshots/person.png)

Mailboxes get the same treatment: **Users > Mailboxes** lists every mailbox with its size, forwarding and who
else can open it, and each has a page with its size and growth over time, inbox rules, access, protocols, automatic
reply, the phones syncing it and what changed. Mailbox addresses in reports and search results open it.
So do groups: **Users > Groups and teams** lists every team, Microsoft 365 group, security group and distribution
list, and each has a page with its owners and members, what membership gives (Conditional Access, admin roles,
licences), its mail and team site, how it's set up and what changed.
And SharePoint sites: **Users > Sites** lists every site with its storage, sharing links and last activity, and
each has a page with its storage and what fills it, how it's shared, the team behind it, its activity and what
changed. Site addresses in reports and on group pages open it.

### Across all clients, and what changed

The **Clients** page puts every client side by side, worst first. **All clients** compares any report across every
client (one row each, every figure clickable), and **What changed** lists month-over-month changes: new and fixed
findings, admin, account, Conditional Access, forwarding, app, device and group changes, and a row-by-row comparison
of every detail report.

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/all-tenants.png" alt="All clients rollup"></td>
    <td width="50%"><img src="docs/screenshots/what-changed.png" alt="What changed across all clients"></td>
  </tr>
  <tr>
    <td>All clients: the monthly review's figures for every client (<a href="examples/all-clients-monthly-review.pdf">PDF</a>)</td>
    <td>What changed in the last 30 days, most important first (<a href="examples/what-changed-all-clients.pdf">PDF</a>)</td>
  </tr>
</table>

### Emailed reports and alerts

Send any mix of reports weekly, monthly or quarterly at a time in the client's timezone: the client's people get
the email, your team a hidden copy. Saved views go too, with your filters, columns and sort, for one client or
across every client. Stale data is collected again first. There's a cross-client digest for your team, alert
emails when a scan finds something new and critical, and Teams, Slack or webhook alerts, including "the October
report for Contoso is ready" with a link into the portal. Every send is
logged, failures retry automatically, and you can preview at desktop and phone width.

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/emailed-reports.png" alt="A client's emailed reports"></td>
    <td width="50%"><img src="docs/screenshots/email-schedule-form.png" alt="Scheduling an email"></td>
  </tr>
  <tr>
    <td>Schedules, alerts, contacts and the delivery history</td>
    <td>Choosing what to send and when</td>
  </tr>
</table>

### Evidence and certifications

The **Evidence page** starts with the scheme the client is going for. **Cyber Essentials** and **Cyber
Essentials Plus** are judged from the data as an assessor would, control by control: *Pass*, *Fail* (any gap
fails; the auto-fail questions are marked), *Partly*, or *Your evidence* where Microsoft 365 can't show it
(firewalls, patching, anti-malware), with the figures, the fix and a text to paste. The page leads with what to
fix before applying, compares with last month or last year, says what the CE Plus assessor will test, and
carries the answers for the IASME portal with one-tap copy; downloads come as PDF, Word, text, XLSX and CSV.
**IASME Cyber Assurance**, **ISO/IEC 27001 Annex A** and **UK GDPR** (the ICO's security outcomes and the
accountability records) are judged the same way, and so are the **CIS Microsoft 365 benchmark**, **NIST CSF
2.0**, **SOC 2** and Australia's **Essential Eight** for clients asked to map to them. The **cyber insurance proposal** and **supplier security
questionnaire** packs turn the same data into answers in each form's wording, with a text to complete where
Microsoft 365 can't answer; one written answer fills every pack.

<table>
  <tr>
    <td width="60%"><img src="docs/screenshots/cyber-essentials.png" alt="Cyber Essentials evidence"></td>
    <td width="40%"><img src="docs/screenshots/pdf-evidence.png" alt="Cyber Essentials evidence PDF"></td>
  </tr>
  <tr>
    <td>Cyber Essentials: what to fix, each control's verdict and the text to paste</td>
    <td>The pack as a PDF (<a href="examples/cyber-essentials-evidence.pdf">open it</a>; the <a href="examples/insurance-evidence-pack.pdf">insurance pack</a>)</td>
  </tr>
</table>

### Recommended projects

**Projects** turns each client's findings into the projects an account manager can sell: no Conditional Access
becomes an *Identity and Conditional Access* project, unencrypted laptops a *Device management* one, missing
Business Premium features a *Security licence upgrade*, and so on. A project is half a day's work or more; anything
smaller, like registering MFA for one admin, is a quick fix the team just does, listed on the client's page. Every
client's recommendations sit in one list, worst first, with what was found, how big the job is, a rough estimate
and where it stands (suggested, proposed, agreed or declined, with a note). Each client's page downloads a branded
proposal PDF to send them. A project drops off by itself once its findings are fixed.

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

### Your team, each with the access they need

Everyone at your MSP has a role made of capabilities, and sees only the clients they're given. Seven roles are built
in: Administrator, Team leader, Second line, First line (looks things up, changes nothing), Account manager
(licences, renewals, month-end reports and projects), Auditor (every report, read-only) and Client viewer. **Settings
→ Users → Roles** changes what a built-in role holds or adds your own, and **Customise** on a person's page gives or
takes away single capabilities, everywhere or on one client. Staff can sign in with their Microsoft work account,
under your own Conditional Access and MFA, with their role assigned in Entra ([Staff
sign-in](v2/docs/staff-sign-in.md)).

### Managing clients

Adding a client shows a setup checklist that ticks itself off: admin consent, Global Reader, the first collection
and every permission granted. **Client settings** lists every client's connection health, worst first, and each
client's **Manage** tab says in plain words what's wrong and where to click to fix it. When a release asks for new
permissions, **Update the app** adds them to your app registration with one sign-in, and the Onboarding tracker
lists the clients still to approve them, each with a **What's new** page: the email to send, the approval link and a
PDF saying what each permission reads. Clients can be paused or archived, and **Offboard** walks through a final
export and the client's side, then deletes the client and everything stored for it, with no SQL.

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/client-tenants.png" alt="Client settings: connection health for every client"></td>
    <td width="50%"><img src="docs/screenshots/manage-tenant.png" alt="A client's Manage page"></td>
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

[`examples/`](examples/) has real exports from the demo data: the monthly review (PDF and XLSX) and its summary PDF,
a quarterly review, Cyber Essentials evidence, the insurance evidence pack, SharePoint storage, MFA coverage, mailbox
permissions, mail forwarding, licence right-sizing, sign-in activity, sign-in methods, subscriptions, Conditional
Access, an account page, What changed, All clients and a recommended projects proposal. All of it is fictional.

## Security model

Office Sentry holds read access to many clients, so it is built to do as little as possible with it.

- **Read-only by construction.** One multi-tenant Entra app asks only for read permissions (`*.Read*`, plus
  `Exchange.ManageAsApp`). The Graph client has no write methods, and the Exchange client refuses any cmdlet that
  isn't on a fixed allow-list of `Get-` cmdlets. It never creates or changes anything in a client's tenant.
- **Certificate sign-in, no credential files.** The app signs in with a certificate whose private key is encrypted
  in the database. Secrets and private keys are never downloadable; only the public `.cer` is, by admins.
- **Tenant isolation and roles.** Deny by default: people see only the clients they're granted, every client
  page goes through one access check, and anything else returns 404. What they may do there is a role made of
  capabilities (built-in: Administrator, Team leader, Second line, First line, Account manager, Auditor and
  Client viewer), narrowed or widened per person and per client by an admin. Clients see only their own reports.
- **The portal itself:** staff can sign in with their own Microsoft account (Entra ID, under your Conditional
  Access and MFA; roles assigned in Entra), or with two-step sign-in, which is required for everyone on new
  installs, client accounts included; wrong passwords
  slow down the address they come from rather than locking the account, so nobody can lock a colleague out;
  server-side sessions, CSRF protection, a strict Content Security Policy with no external assets, and encrypted
  secrets with key rotation.

Details are in [SECURITY.md](SECURITY.md) and the [technical reference](v2/README.md#what-it-does-differently-from-v1).

## What it reads

[What Office Sentry reads](v2/docs/what-office-sentry-reads.md) is written for a client's owner or IT contact who
has been asked to approve access: every permission, what is and isn't read (no email contents), where the data is
kept and how to remove access. Send it with the consent link.

## Quick start

Office Sentry runs as two containers (the portal and a worker) from one image, with everything stored in one
Docker volume.

Download `docker-compose.yml`, `Caddyfile` and `env.example` from the newest
[release](https://github.com/JackD99/OfficeSentry/releases) into a folder on a Linux server with Docker, then:

```bash
cp env.example .env           # set OFFICESENTRY_BASE_URL, OFFICESENTRY_DOMAIN and OFFICESENTRY_TIMEZONE
docker compose --profile https up -d
```

The image is [`ghcr.io/jackd99/officesentry`](https://github.com/JackD99/OfficeSentry/pkgs/container/officesentry),
public, for amd64 (Intel and AMD) servers and, from 2.1.0, arm64 ones. It is built and
scanned by GitHub Actions from a tagged release, with a signed build provenance. Each release lists what changed,
and anything to do before updating, in [CHANGELOG.md](CHANGELOG.md).

Leave out `--profile https` if you already have a reverse proxy, and point it at `http://127.0.0.1:8000`. Then:

1. Run `docker compose logs web` and open the link on the line starting **No admin account yet** to create the
   first admin (with two-step sign-in). Your home page then shows a **Get started** checklist (the app connection,
   your first client, its consent and its first collection), which ticks itself off as you go.
2. Open **Settings → App connection** and press **Sign in to Microsoft**. The page shows a code and Microsoft's
   link: enter the code there and sign in as an admin of **your own** (MSP) tenant who can create app
   registrations. Office Sentry then creates its multi-tenant, read-only app with a certificate and saves it, with
   nothing to type on the server. (`docker compose run --rm worker python -m officesentry create-app` does the
   same from the command line.)
3. Add your first client on the same page and follow its checklist: a Global Administrator of the client's
   tenant grants consent, you assign Global Reader, and the first collection runs.

Stuck? [Troubleshooting](v2/docs/troubleshooting.md) covers the common errors, such as `AADSTS50011` on the
consent page, mailboxes failing without Global Reader and the worker not running. For anything else,
**Settings → Support** makes a support package to attach to a bug report, with secrets and client names taken
out ([Getting help](v2/docs/getting-help.md)).

[v2/DEPLOY.md](v2/DEPLOY.md) covers HTTPS, the collection schedule, retention, backups and updating. Running
Portainer, a NAS, Kubernetes, Azure, AWS or another cloud? The [deploy guides](v2/docs/deploy/README.md) start
from there.

## Documentation

- [Technical reference](v2/README.md): every report and check, Exchange data, sign-in security, setup and layout
- [Running it on your own server](v2/DEPLOY.md)
- [Deploy guides](v2/docs/deploy/README.md): Portainer and other container managers, Unraid, Synology and
  TrueNAS, Kubernetes, Azure, AWS, other clouds and Cloudflare
- [Staff sign-in with Microsoft](v2/docs/staff-sign-in.md): let your team sign in with their Entra ID account
- [Onboarding a client](v2/docs/onboarding-a-client.md): consent, Global Reader, the first collection, connection
  health and offboarding
- [What Office Sentry reads](v2/docs/what-office-sentry-reads.md): for clients approving access
- [Check catalogue](v2/docs/checks.md): every check, data source, detail report and permission
- [Troubleshooting](v2/docs/troubleshooting.md): the common setup and collection errors and their fixes
- [Getting help](v2/docs/getting-help.md): where to look first, the support package and what it leaves out
- [Securing the portal](v2/docs/securing-the-portal.md): hardening defaults, what you have to do, reverse proxies
- [Example reports](examples/)
- [Roadmap](ROADMAP.md): what's planned next, and what never will be
- [Changelog](CHANGELOG.md): what changed in each release

## Development

All code is in [`v2/`](v2/): Flask, SQLite and WeasyPrint, with one background worker and no Redis or Celery.

```bash
cd v2
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
ruff format . && ruff check . && pytest
OFFICESENTRY_INSECURE_COOKIES=1 python -m officesentry web   # http://127.0.0.1:8000
```

`python scripts/demo_data.py --data-dir /tmp/officesentry-demo` builds demo clients to click around in.
The screenshots and example reports here are made by `python scripts/screenshots.py` from the same demo data;
re-run it when the UI or reports change. [DEVELOPING.md](DEVELOPING.md) is a short tour of the code and tests, and
[CONTRIBUTING.md](CONTRIBUTING.md) has the conventions.

## Getting help and contributing

[SUPPORT.md](SUPPORT.md) says where to go for what: [Troubleshooting](v2/docs/troubleshooting.md) for setup
errors, [Discussions](https://github.com/JackD99/OfficeSentry/discussions) for questions, and the
[issue forms](https://github.com/JackD99/OfficeSentry/issues/new/choose) for bugs (attach the support package
from the portal's Settings) and for report, check and feature requests. Issues are public: never paste a
client's data.

Pull requests are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) has the conventions and how every change is
checked, and [DEVELOPING.md](DEVELOPING.md) is the tour of the code. Everyone taking part agrees to the
[code of conduct](CODE_OF_CONDUCT.md).

## Security issues

Please report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md). Don't open a public issue.

## Licence

Office Sentry is free software under the [GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0-only).
You can use, change and share it; if you run a modified version for others over a network, you must offer them
its source code under the same licence.

## The old v1 portal

The original v1 portal (MSP operations, 365Inspect, PowerShell scripts, AD agent, GDAP management, Graph Explorer)
has been removed. Its last state is on the `v1-final` branch.
