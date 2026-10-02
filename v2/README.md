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
- **Emailed reports** (`/tenants/<id>/schedules`, SMTP under Admin → Email):
  monthly on a chosen day and hour, with PDF and XLSX attached.
- **Detail reports** (`/tenants/<id>/data/<report>`, each also as XLSX):
  - *Mailbox access and forwarding*: every forward (admin-set or inbox rule),
    inbox rules that hide mail, full access / send as / send on behalf, and
    shared mailboxes that can sign in.
  - *Apps and consents*: third-party apps with what they were granted
    (application permissions, tenant-wide and per-user consent), and app
    registration secrets or certificates expiring.
  - *Conditional Access coverage*: every policy in plain words, and for each
    account which enforced policy requires MFA, whether legacy sign-in is
    blocked, and which policies exclude it (group membership expanded).
  - *Guests and sharing*: invitation, consent and SharePoint sharing settings,
    and every guest with invitation state and last sign-in.
  - *Groups and Teams*: who can create teams and groups, whether guests are
    allowed and unused groups expire, teams and Microsoft 365 groups with no
    enabled owner or a single one, every team and group with its owners,
    members and guests, every guest in a team or group, and counts of security
    groups and distribution lists.
  - *Email domains*: SPF (with DNS lookup count), DMARC and DKIM for every
    verified domain, from public DNS.
  - *Devices*: operating systems with Windows support dates, Intune devices
    that are non-compliant, unencrypted, out of support or not checking in,
    every managed device, and every Entra ID device record by last activity.
    Tenants without Intune still get the Entra records and Windows versions.
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
Windows releases out of support, joined computers not in Intune, devices not
checking in, stale device records.

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
  can show history and changes. Metrics are kept forever; full snapshots for
  `OFFICESENTRY_RETENTION_DAYS`.
- **One worker process, no Redis/Celery.** It recovers runs left behind by a
  crash and queues each nightly slot once.

## Quick start (Docker)

```bash
cp .env.example .env          # set OFFICESENTRY_BASE_URL at least
docker compose up -d --build
```

Open the portal, create the first admin, then:

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
  mailer.py      SMTP delivery and the monthly schedule
  worker.py      queue runner, nightly scheduler, orphan recovery
  web/           Flask app: access.py is the single authorization gate
```
