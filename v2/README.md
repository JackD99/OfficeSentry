# Office Sentry v2

Read-only Microsoft 365 reporting for MSPs: nightly snapshots of every client
tenant, cross-tenant rollups, month-over-month changes and branded client
reports. The [root README](../README.md) is the product page with screenshots;
this page lists every feature in detail and covers setup and development.
Running it on a server is in [DEPLOY.md](DEPLOY.md).

## Reports

- **Monthly Security & Licensing Review** (`/tenants/<id>/report`): KPIs with
  4-week change, Secure Score trend, failing checks with affected accounts and
  recommendations, licence waste in money, passing checks, checks that the
  tenant's licences can't support, and data freshness.
- **Reports** (`/reports`): every report in one searchable page, by area and by
  job ("Offboarding check", "Insurance renewal", "Storage cleanup"...), opened
  for one tenant or across all of them. The search also finds tenants and
  settings, and lists your saved views.
- **All tenants** (`/across/<report>`): a sortable, filterable table with one row
  per tenant for the monthly review, the insurance evidence pack and every
  detail report, limited to the tenants you can see. New detail reports appear
  automatically.
- **What changed** (`/changes`, and each tenant's *Changes* tab): a standalone
  month-over-month report for the last 7, 30 or 90 days. It covers key
  figures then and now, new and fixed findings, admin, account, Conditional
  Access, forwarding, app, device and group changes, and a row-by-row
  comparison of every detail report.
- **Downloads**: every page above has CSV and XLSX. Values from tenants can
  never become spreadsheet formulas.
- **Licence prices** (admin): enter monthly prices per SKU to cost waste and
  right-sizing savings.
- **Findings** (`/tenants/<id>/findings`): every finding keeps first seen,
  resolved and reopened dates. Analysts can accept a risk with a reason and an
  optional expiry; accepted risks leave the totals and get their own section.
- **Since last month**: new and fixed findings, admin role changes, new,
  disabled and deleted accounts, and Conditional Access policies added,
  switched or edited, compared with the snapshot from 4+ weeks earlier.
- **Emailed reports** (a tenant's *Emailed reports* tab, and *Email reports*
  for everything at once):
  - Send any mix of reports (the monthly review, the evidence pack, any detail
    report) weekly, monthly or quarterly, at a time in the schedule's own timezone.
  - Recipients are contacts: the client's people get the email, your team gets a
    hidden copy. Optional note at the top of the email.
  - A cross-tenant **digest** for your team, worst tenants first.
  - **Alert emails** when a scan finds something new and critical (or high), for
    one tenant or all of them. They come from the same alert event as the
    Teams/Slack alerts below. A tenant's first scan never emails.
  - Preview (desktop and phone width), Send now, pause, and a delivery history
    with failures, automatic retries (5 min, 30 min, 2 h) and a Retry button.
  - Sent over SMTP or Microsoft 365 (Graph `sendMail` from a mailbox in your own
    tenant, through a separate send-only app), under Settings → Email delivery.
- **Alerts** (Admin → Alerts): a Microsoft Teams (Workflows), Slack or generic
  JSON webhook message when a tenant gets new findings at or above a chosen
  severity (one message per tenant per collection), when a collection fails
  after working last time, and optionally when it works again. The webhook URL
  and optional HMAC signing secret are stored encrypted; only https URLs that
  resolve to public addresses are called (`OFFICESENTRY_ALLOW_PRIVATE_WEBHOOKS=1`
  allows internal ones).
- **Detail reports** (`/tenants/<id>/data/<report>`, each also as PDF, XLSX and CSV),
  grouped by area (identity, email, files, devices, tenant). Every table can be
  filtered by the values in it (or by age, for dates), sorted, and have columns
  hidden; headers stay visible while scrolling, and the view can be saved by
  name and reused on any tenant. Downloads contain the rows and columns shown.
  Every headline number opens the rows behind it, here and on All tenants:
  - *Mail forwarding*: every forward (admin-set or inbox rule), and the
    outbound spam policies and remote domains that allow or block it.
  - *Inbox rules*: rules that forward outside, delete or hide mail, every
    rule that sends mail elsewhere, and every rule in every mailbox.
  - *Mailbox permissions*: full access, send as and send on behalf, by person
    and by mailbox, with blocked, guest and outside accounts flagged.
  - *Shared mailboxes*: shared, room and equipment mailboxes whose account can
    be signed in to, their last sign-in, who has access, and their size.
  - *Mailbox size*: each mailbox's size against its send quota
    (`Get-MailboxStatistics`), with mailboxes over 90% flagged.
  - *Legacy sign-ins*: SMTP AUTH, IMAP, POP and other basic-auth sign-ins from
    the last 30 days of the sign-in log (Entra ID P1), and which mailboxes
    still allow SMTP AUTH, POP and IMAP.
  - *Sign-in activity*: the last 7 days of interactive sign-ins from the
    Entra ID sign-in log (Entra ID P1), summarised rather than stored: accounts
    with repeated failures (wrong passwords, lockouts), addresses failing
    against several accounts (password spraying), successful sign-ins from
    countries the tenant rarely uses, successful sign-ins that needed only a
    password, failure reasons in plain English (MFA and other prompts counted
    apart from failures), and sign-ins by country, app and account. With
    Entra ID P2, risky sign-ins too. Reads at most 50,000 sign-ins per run
    (the most recent) and says so if it stops. Each account's page shows its
    own summary.
  - *Admin activity*: security-relevant changes from the Entra ID directory
    audit log over the last 30 days (7 without Entra ID P1, which is all the
    tenant keeps): admin roles granted or removed (including PIM), Conditional
    Access policies added, changed or deleted, consents and app permissions,
    secrets and certificates added to apps, domain and federation changes,
    admin password resets, deleted users and security info changed by admins.
    Federation changes, new app credentials, role grants and Conditional Access
    policies deleted or switched off are listed first, then every change and
    who made them. Reads at most 10,000 events per run and says so if it stops.
  - *Apps and consents*: third-party apps with what they were granted
    (application permissions, tenant-wide and per-user consent), and app
    registration secrets or certificates expiring.
  - *Conditional Access coverage*: every policy in plain words, and for each
    account which enforced policy requires MFA, whether legacy sign-in is
    blocked, and which policies exclude it (group membership expanded).
  - *Passwords and sign-in methods*: how each member signs in and how strong
    that is (phishing-resistant: passkey, security key, Windows Hello or
    certificate; Authenticator app or token; phone only; none), admins
    without a phishing-resistant method, people whose only second factor is a
    text or call, who can sign in without a password or reset their own, and
    the method each person is asked for first. The tenant's authentication
    methods policy (which methods are on and for whom, SMS, voice and email
    one-time passcodes, the registration campaign, system-preferred MFA)
    against what's recommended, and the authentication strengths Conditional
    Access requires. Password hygiene: password age, passwords set to never
    expire, accounts that never signed in with a password set long ago, and
    each domain's password expiry policy. Who hasn't set up MFA at all stays
    in *MFA coverage*.
  - *Tenant settings*: what users and guests can do without an admin
    (create tenants, register apps, join by verifying an email address,
    sign up for trials, guest directory access) against the recommended
    value, and the admin consent request workflow (reviewers, emails,
    expiry).
  - *Guests and sharing*: invitation, consent and SharePoint sharing settings,
    and every guest with invitation state and last sign-in.
  - *Deleted users and groups*: the Entra ID recycle bin. Every deleted user
    (member or guest, the licences it held) and deleted team or Microsoft 365
    group, when it was deleted and the days left before Microsoft removes it
    for good after 30 days. Licensed members and teams with 7 days or less
    left are flagged. Restoring is done in the Entra or Microsoft 365 admin
    centre; Office Sentry only reads the recycle bin.
  - *Groups and Teams*: who can create teams and groups, whether guests are
    allowed and unused groups expire, teams and Microsoft 365 groups with no
    enabled owner or a single one, every team and group with its owners,
    members and guests, every guest in a team or group, and counts of security
    groups and distribution lists.
  - *Email domains*: SPF (with DNS lookup count), DMARC and DKIM for every
    verified domain, from public DNS. SPF checks also catch multiple records,
    +all/?all, the ptr mechanism and includes that publish no SPF record;
    DMARC checks catch multiple records, a missing p= policy, pct below 100,
    sp=none and no rua= address. Domains that receive mail are also checked
    for MTA-STS (the `_mta-sts` record and the policy file at
    `https://mta-sts.<domain>/.well-known/mta-sts.txt`: mode, mx hosts
    covering the domain's MX, max_age; fetched like sending servers do, with
    no redirects) and TLS-RPT (`_smtp._tls`). MTA-STS and TLS-RPT have their own
    checks and don't change prospect grades.
  - *Devices*: operating systems with Windows support dates, Intune devices
    that are non-compliant, unencrypted, out of support or not checking in,
    every managed device, and every Entra ID device record by last activity.
    Tenants without Intune still get the Entra records and Windows versions.
  - *Hybrid identity*: whether accounts are synced from on-premises Active
    Directory, when directory sync and password hash sync last ran, which
    domains are federated, admin accounts synced from AD, and synced versus
    cloud-only account counts. Cloud-only tenants are labelled as such.
  - *SharePoint storage*: for a client whose SharePoint is filling up. How
    full the tenant is (its quota estimated from licences: 1 TB + 10 GB per
    licence + extra storage) and when it will be full at the current rate;
    where the space goes (current files, older versions, recycle bins,
    Preservation Hold libraries, other); every site and library by size;
    the largest folders and files, the files with the most version history,
    the largest files untouched for two years and the largest recycle bin
    items, each with what to do about it; and OneDrives over 80% of their own
    quota with their largest files. Sizes come from SharePoint's own storage
    figures (`SMTotalSize`), which include every version. Needs
    `Sites.Read.All`.
  - *Licences and subscriptions*: for renewals and fixing licence problems.
    Every product with licences purchased, in a grace period, suspended,
    assigned and available, and its monthly cost at your prices; every
    subscription behind them (`/directory/subscriptions`) with its status and
    the date it next renews, lapses or is deleted, with trials and
    subscriptions in their grace period, suspended or locked listed first and
    bucketed into 30, 60 and 90 days; who holds which licence, directly or by
    group (filter by licence); group licensing errors (not enough licences,
    conflicting or missing base licences, usage location) in plain words with
    what to do; licensed accounts without a usage location; licences assigned
    both directly and by a group; and the groups that hand licences out.
  - *Microsoft 365 usage*: who used email, Teams, OneDrive, SharePoint and
    the Office apps in the last 30 days (share of licensed people per
    service, and a trend from Office Sentry's own history), when each person
    was last active in each service, Office apps by platform (Windows, Mac,
    mobile, web), Office installs per person, and email and Teams activity
    counts. From Microsoft's usage reports (`Reports.Read.All`).
  - *Licence right-sizing*: people who could move to a cheaper licence, with
    the saving at your prices: nothing used in 30 days (remove the licence,
    or convert to a shared mailbox when it still receives mail), Copilot
    unused, only email used (Exchange Online Plan 1, or Plan 2 for big,
    archived or held mailboxes), no desktop Office apps used (Business
    Standard to Basic, Office 365 E3 to E1, apps-only plans removed), and
    Exchange Online Plan 2 on small mailboxes. Also lists services people are
    licensed for but don't use. Disabled and inactive accounts stay in
    Licence waste so nothing is counted twice. When the tenant hides names in
    usage reports, people show as short codes, licences are read from
    Microsoft's product names, and the page explains how to show names.
- **Insurance evidence** (`/tenants/<id>/evidence`, PDF, XLSX): the controls
  cyber-insurance questionnaires ask about (MFA, admin accounts, legacy auth,
  email authentication, forwarding, logging, leavers, apps, sharing, devices), each
  answered In place / Partly / Not in place from the checks, with the gaps
  listed. Backups and training are marked "Not assessed".
- **Prospect assessments** (`/prospects`, admins and analysts): enter any
  domain to grade its email security from public DNS and see whether it uses
  Microsoft 365 and how it signs in. No access to the tenant needed; PDF for
  sending to the prospect.
- **Client view**: client users land on a page with their reports, key
  numbers and downloads, and don't see collection internals or run logs.

Checks: MFA enforced (CA or Security Defaults, with exclusions), legacy auth
blocked, admins without MFA, admins not covered by an MFA policy, Global Admin
count, guest admins, apps with admin roles, users without MFA, admins without a
phishing-resistant sign-in method, people whose only second factor is a text or
call, inactive accounts, licences on disabled accounts, unassigned paid
licences, external forwarding, inbox rules that hide mail, automatic forwarding
policy, shared mailbox sign-in, mailbox auditing, SMTP AUTH, risky third-party
app access, user consent, app credential expiry, users creating tenants, users
registering apps, guest directory access, joining by email verification, admin
consent requests, SharePoint anonymous links, guest invitations, stale guest
invitations, teams and groups without an owner, who can create teams and
groups, group expiration, device compliance, disk encryption, Windows releases
out of support, joined computers not in Intune, devices not checking in, stale
device records, SharePoint storage nearly full, licence assignment errors,
domain federation changes, risky sign-ins that succeeded (Entra ID P2), and
subscriptions about to lapse (trials in use ending within 30 days,
subscriptions in their grace period, suspended ones still assigned). For
tenants synced from on-premises AD: directory sync running, password hash sync
on, and admin accounts that are cloud-only. The [check catalogue](docs/checks.md)
lists every check with the data it reads and what it needs.

### Exchange data

Mailbox forwarding, permissions and mail settings come from the Exchange
Online admin API (the same one the ExchangeOnlineManagement module uses),
limited in code to a fixed list of `Get-` cmdlets. It needs the app's
`Exchange.ManageAsApp` permission and, in each client tenant, the **Global
Reader** role assigned to the app (Entra admin center → Roles and admins →
Global Reader → Add assignments). Inbox rules are read through Graph, so they
work without it.

### Testing against a real tenant

`python -m officesentry live-test [--out DIR]` runs every collector against a
test tenant using `OFFICESENTRY_TEST_TENANT_ID`, `OFFICESENTRY_TEST_CLIENT_ID`
and `OFFICESENTRY_TEST_CLIENT_SECRET`, stores the result as tenant "Live test"
and prints each collector's outcome and every check. The secret is only for
this command; the product signs in with a certificate.

## Documentation

- [What Office Sentry reads](docs/what-office-sentry-reads.md): for clients
  asked to approve access. Every permission, what is and isn't read, where the
  data is kept and how to remove access.
- [Onboarding a client](docs/onboarding-a-client.md): for technicians. Adding
  a tenant, consent, Global Reader, the first collection, connection health,
  the errors on the Data collection tab, pausing, archiving and offboarding.
- [Check catalogue](docs/checks.md): every check, data source, detail report
  and permission, generated from the code by `scripts/gen_check_catalogue.py`.

## What it does differently from v1

- **Read-only by construction.** The Entra app requests only `*.Read*`
  Graph permissions, the Graph client has no write methods, and the Exchange
  client only runs allow-listed `Get-` cmdlets.
- **One multi-tenant app.** You register it once; each client admin opens a
  consent link to add their tenant. No per-tenant app or device-code step.
- **No credential files.** The certificate's private key is encrypted in the
  database. Only the public `.cer` can be downloaded, by admins.
- **Deny-by-default access.** Users see only tenants granted to them (admins
  and "all tenants" analysts excepted). Hidden tenants return 404.
- **Every run is a snapshot** (`runs` → `objects` + `metrics`), so reports
  can show history and changes. Metrics and findings are kept forever; full
  snapshots for the last 30 days plus one per month for 12 months.
- **One worker process, no Redis/Celery.** It collects on a cron schedule in
  your timezone, skips data that is already fresh, refreshes stale data before
  an emailed report, prunes old snapshots, and recovers runs left behind by a
  crash.

## Sign-in security

- **Two-step sign-in** (a 6-digit code from an authenticator app) is required
  for everyone on new installs, client accounts included. Installs set up
  before October 2026 keep requiring it for admins and analysts only, so
  nobody is surprised after an upgrade; *Settings → Users* shows the policy
  and recommends extending it to clients. Set it with
  `OFFICESENTRY_REQUIRE_MFA` (`all`, `staff` or `off`). Adding an
  authenticator from the account page asks for the password, and a password
  an admin set (new user or reset) has to be replaced at the next sign-in,
  before two-step setup (or right after the code, for accounts that have it). Each user gets ten
  one-time recovery codes. An admin can reset someone's two-step sign-in from
  their user page if they lose their phone.
- **Sign-in limits by address, not account lockout:** wrong passwords slow
  down the address they come from (5 per username or 20 in all per 15
  minutes), and a spread-out attack on one account slows new addresses only,
  so nobody can lock a person out from elsewhere. Five wrong two-step codes
  (which need the password) lock that step for 15 minutes. Admins can clear
  both from the user page. Details in [DEPLOY.md](DEPLOY.md#sign-in-protection).
- **Sessions are stored on the server.** Signing out ends the session, so a
  copied cookie stops working. People can see where they're signed in and
  sign out other browsers on their **Account** page. Sessions end after 2 hours
  without activity or 8 hours in total, and when a password, role, access or
  two-step setting changes.
- **Activity log:** admins see sign-ins and every change, with the address,
  under *Settings → Activity log* (filters, paging, CSV). Failed sign-ins are
  kept for 90 days, the rest for good.
- **Cookies** are `Secure`, `HttpOnly`, `SameSite=Lax` and use the `__Host-`
  prefix. Behind a reverse proxy, set `OFFICESENTRY_TRUSTED_PROXIES=1` so the
  real client IP and scheme are used.
- **Keys:** the session key and the encryption key are separate and kept in
  `secrets.json` (mode 0600, tightened if it was looser) unless set as
  environment variables. `python -m officesentry rotate-keys --new` replaces
  both and re-encrypts every stored secret.

## Quick start (Docker)

```bash
cp .env.example .env          # set OFFICESENTRY_BASE_URL at least
docker compose --profile https up -d --build
```

[DEPLOY.md](DEPLOY.md) has the full steps: HTTPS, schedule and retention
settings, backups and updating.

Create the first admin:

1. Run `docker compose logs web` and find the line starting **No admin account
   yet**. Open the link on it. (Lost it? `docker compose exec web python -m
   officesentry setup-link` prints it again.) The link only works until the
   first admin exists, so nobody else can claim a fresh install.
2. Choose a username and password, then scan the QR code with an authenticator
   app (Microsoft Authenticator works) and save the recovery codes.

Then:

1. **App connections → Generate certificate.**
2. Create the app: `docker compose run --rm worker python -m officesentry create-app --tenant <your-tenant>`
   (device sign-in as an admin of *your* tenant), or create it by hand as the
   App connections page describes and paste its client ID. By hand, make the
   app multi-tenant, upload the certificate (a client secret won't work) and
   add the redirect URI `https://<your portal>/consent/callback` as a Web
   platform. Enter your Directory (tenant) ID with the client ID to check the
   certificate straight away.
3. **Add tenant**. The tenant's page shows a setup checklist that ticks itself
   off: grant admin consent (a Global Administrator of the tenant opens the
   link; a full collection is queued on return), assign Global Reader, first
   full collection, all permissions granted. The tenant's **Manage** tab says
   in plain words what's wrong and how to fix it if Office Sentry can't sign
   in or is missing a permission or role.

## Managing client tenants

- **Client tenants** (*Settings*, or *Connection health* on the Tenants page)
  lists every tenant with its status (*Onboarding*, *Healthy*, *Needs
  attention*, *Paused*, *Archived*), consent, missing permissions, Global
  Reader, last good collection and failing data sources, worst first.
- A tenant's **Manage** tab (admins and analysts) shows its connection health:
  consent, sign-in, which of the app's permissions the tenant actually granted,
  whether the app holds Global Reader (or more than it needs), licences, last
  collection and failing data sources, each with the fix. It's read by the
  `connection` collector with every collection (GET only, from permissions the
  app already has); *Check connection now* queues it straight away.
- Admins can rename a tenant, change its tenant ID (until data has been
  collected) or app connection, keep notes, **pause** it (no collection,
  emailed reports or alert emails; data stays) and **archive** it (also hidden
  from lists, dashboards, rollups, digests and its client users; restorable).
- **Offboard** walks through a final export, the client's side (remove Global
  Reader, delete the enterprise app) and what will be deleted, then deletes the
  tenant and everything stored for it after you type its name, optionally with
  client accounts that had no other tenant. `python -m officesentry
  delete-tenant <id>` does the same from the server.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest && ruff check .
OFFICESENTRY_INSECURE_COOKIES=1 python -m officesentry web   # http://127.0.0.1:8000
python -m officesentry worker                                 # second terminal
```

`pytest -n auto` runs the tests on every core. `web` is Werkzeug's
development server; `python -m officesentry serve` runs the portal on a
production server (gunicorn, or waitress on Windows).

**Windows without Docker** works for development and for a small
production install (see "Running on Windows without Docker" in
[DEPLOY.md](DEPLOY.md)): use `.venv\Scripts\activate` and
`set OFFICESENTRY_INSECURE_COOKIES=1`, and `serve` instead of gunicorn. What
differs is PDF files: WeasyPrint needs the Pango libraries, which Docker
includes and Windows doesn't have. Without Pango, every PDF button opens the
same report as a print-ready page and the browser's print window comes up:
choose **Save as PDF** as the printer to get the same layout. Scheduled
emails can only attach PDFs when WeasyPrint works.

Tests mock Microsoft Graph at the HTTP layer (`responses`), not the client
class, so URL and paging bugs are caught.

## Layout

```
officesentry/
  config.py      env config; separate session and encryption keys
  db.py          SQLite (WAL) + transactional migrations (migrations/*.sql)
  backups.py     backup, restore, and the copy taken before each upgrade
  crypto.py      Fernet vault with key rotation
  certs.py       certificate generation, Entra keyCredential
  entra.py       read-only permission list, app creation, consent URL
  graph.py       Graph client: retries, Retry-After, paging, typed errors
  store.py       tenants, connections, runs, objects, metrics, pruning
  collectors/    one module per data source; @register adds it
  snapshot.py    latest successful data per collector, for rules/reports
  rules.py       checks: snapshot -> pass / fail with findings / unavailable
  licences.py    SKU names, prices, licence-waste maths
  usage.py       per-person Microsoft 365 usage and licence right-sizing rules
  reports/       report builders (data only), SVG charts, XLSX/PDF export
  findings.py    findings lifecycle (new, resolved, reopened, accepted)
  changes.py     snapshot diffs for "since last month"
  mailer.py      sends a finished email over SMTP or Graph sendMail
  delivery.py    schedules, digests, alert emails, retries and email rendering
  schedule.py    cron slots in a timezone, freshness (max age), periodic jobs
  worker.py      queue runner, schedule, retention, heartbeat
  tenant_health.py  connection health and the onboarding checklist, from stored state
  lifecycle.py   first collection, pause, archive and deleting a tenant
  web/           Flask app: access.py is the single authorization gate
```
