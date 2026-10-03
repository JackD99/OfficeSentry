# Office Sentry v2

Read-only Microsoft 365 reporting for MSPs: nightly snapshots of every client
tenant, cross-tenant rollups, month-over-month changes and branded client
reports. This is the rebuild planned in the 2026-10 review; the v1 portal
remains in `../portal` until v2 replaces it.

**Status: Phase 5.** Tenants are collected nightly; the **Monthly Security &
Licensing Review** is available per tenant (HTML, PDF, XLSX) with a
cross-tenant overview, findings history, accepted risks, "since last month"
changes and monthly emailed reports, plus detail reports on mailboxes, apps,
Conditional Access coverage, email domains and guests, a cyber-insurance
evidence pack, prospect assessments and a client view. Next: a UX and design
pass, and a first run against a real tenant.

## Reports

- **Monthly Security & Licensing Review** (`/tenants/<id>/report`): KPIs with
  4-week change, Secure Score trend, failing checks with affected accounts and
  recommendations, licence waste in money, passing checks, checks that the
  tenant's licences can't support, and data freshness.
- **Overview** (`/reports`): one row per tenant with severity counts and KPIs.
- **Licence prices** (admin): enter monthly prices per SKU to cost waste.
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
- **Detail reports** (`/tenants/<id>/data/<report>`, each also as XLSX):
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
  - *Tenant settings*: what users and guests can do without an admin
    (create tenants, register apps, join by verifying an email address,
    sign up for trials, guest directory access) against the recommended
    value, and the admin consent request workflow (reviewers, emails,
    expiry).
  - *Guests and sharing*: invitation, consent and SharePoint sharing settings,
    and every guest with invitation state and last sign-in.
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
count, guest admins, apps with admin roles, users without MFA, inactive
accounts, licences on disabled accounts, unassigned paid licences, external
forwarding, inbox rules that hide mail, automatic forwarding policy, shared
mailbox sign-in, mailbox auditing, SMTP AUTH, risky third-party app access,
user consent, app credential expiry, SharePoint anonymous links, guest
invitations, stale guest invitations, teams and groups without an owner, who
can create teams and groups, group expiration, device compliance, disk encryption,
user consent, app credential expiry, users creating tenants, users
registering apps, guest directory access, joining by email verification,
admin consent requests, SharePoint anonymous links, guest
invitations, stale guest invitations, device compliance, disk encryption,
Windows releases out of support, joined computers not in Intune, devices not
checking in, stale device records.
For tenants synced from on-premises AD: directory sync running, password
hash sync on, and admin accounts that are cloud-only.
checking in, stale device records, domain federation changes.

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
  a tenant, consent, Global Reader, the first collection, and the errors on the
  Data collection tab.
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
  for admins and analysts and optional for client users. Change it with
  `OFFICESENTRY_REQUIRE_MFA` (`staff`, `all` or `off`). Each user gets ten
  one-time recovery codes. An admin can reset someone's two-step sign-in from
  their user page if they lose their phone.
- **Lockout:** five wrong passwords or codes lock the account for 15 minutes.
  Admins can unlock it straight away from the user page.
- **Sessions are stored on the server.** Signing out ends the session, so a
  copied cookie stops working. People can see where they're signed in and
  sign out other browsers on their **Account** page. Sessions end after 2 hours
  without activity or 8 hours in total, and when a password, role, access or
  two-step setting changes.
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
   App connections page describes and paste its client ID.
3. **Add tenant**, then have a Global Administrator of that tenant open the
   consent link on the tenant page. A first collection is queued on return.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest && ruff check .
OFFICESENTRY_INSECURE_COOKIES=1 python -m officesentry web   # http://127.0.0.1:8000
python -m officesentry worker                                 # second terminal
```

On Windows, use `.venv\Scripts\activate` and `set OFFICESENTRY_INSECURE_COOKIES=1`.
Everything except PDF export works natively. PDFs need the Pango libraries,
which WeasyPrint can't install on Windows by itself, so Docker (Docker
Desktop) is the recommended way to run Office Sentry on Windows. Without
Pango, the PDF buttons are simply hidden and HTML and XLSX still work.

Tests mock Microsoft Graph at the HTTP layer (`responses`), not the client
class, so URL and paging bugs are caught.

## Layout

```
officesentry/
  config.py      env config; separate session and encryption keys
  db.py          SQLite (WAL) + transactional migrations (migrations/*.sql)
  crypto.py      Fernet vault with key rotation
  certs.py       certificate generation, Entra keyCredential
  entra.py       read-only permission list, app creation, consent URL
  graph.py       Graph client: retries, Retry-After, paging, typed errors
  store.py       tenants, connections, runs, objects, metrics, pruning
  collectors/    one module per data source; @register adds it
  snapshot.py    latest successful data per collector, for rules/reports
  rules.py       checks: snapshot -> pass / fail with findings / unavailable
  licences.py    SKU names, prices, licence-waste maths
  reports/       report builders (data only), SVG charts, XLSX/PDF export
  findings.py    findings lifecycle (new, resolved, reopened, accepted)
  changes.py     snapshot diffs for "since last month"
  mailer.py      sends a finished email over SMTP or Graph sendMail
  delivery.py    schedules, digests, alert emails, retries and email rendering
  schedule.py    cron slots in a timezone, freshness (max age), periodic jobs
  worker.py      queue runner, schedule, retention, heartbeat
  web/           Flask app: access.py is the single authorization gate
```
