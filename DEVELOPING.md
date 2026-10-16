# Developer guide

A short tour for people who want to change Office Sentry: how to run it, how
it fits together, where things live (every module in a line), how to test
against Microsoft with and without a tenant, and how pull requests are
reviewed. [CONTRIBUTING.md](CONTRIBUTING.md) has the rules every change
follows (sign-off, dependencies, releases).

## Run it locally

Everything is in `v2/`. You need Python 3.11 or newer.

```bash
cd v2
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
export OFFICESENTRY_DATA_DIR=/tmp/officesentry-demo OFFICESENTRY_INSECURE_COOKIES=1
python scripts/demo_data.py --data-dir "$OFFICESENTRY_DATA_DIR"   # prints the sign-in
python -m officesentry web
```

Open http://127.0.0.1:8000 and sign in with the name and password it printed. The demo data is three
fictional client tenants built from the test fixtures, collected twice so
trends and "What changed" have history. No Microsoft tenant is needed.

To run collections and scheduled emails, start the worker in a second
terminal with the same `OFFICESENTRY_DATA_DIR`: `python -m officesentry worker`.
PDF export needs the Pango libraries (CONTRIBUTING.md, "Development setup");
everything else works without them.

`web` is Werkzeug's development server; `python -m officesentry serve` runs the
portal on a production server (gunicorn, or waitress on Windows).

**Windows without Docker** works for development and for a small production
install ([v2/DEPLOY.md, "Running on Windows without Docker"](v2/DEPLOY.md#running-on-windows-without-docker)):
use `.venv\Scripts\activate` and `set OFFICESENTRY_INSECURE_COOKIES=1`, and
`serve` instead of gunicorn. What differs is PDF files: WeasyPrint needs the
Pango libraries, which Docker includes and Windows doesn't have. Without Pango,
every PDF button opens the same report as a print-ready page and the browser's
print window comes up: choose **Save as PDF** as the printer to get the same
layout. Scheduled emails can only attach PDFs when WeasyPrint works.

## How it fits together

Office Sentry is two processes from one Docker image, sharing one SQLite
database. There is no Redis, Celery or message broker.

- **The worker** (`officesentry/worker.py`) runs every collection, so a
  client's data is only ever read from Microsoft there, and collections only
  read. It also runs the schedule, sends report emails and alerts, and tidies
  old data.
  The audit log add-on's two calls that aren't reads (starting and stopping a client's activity feed) go from the worker too, as background tasks, never from a collection. <!-- since 2.4.0 -->
- **The portal** (`officesentry/web/`, Flask) shows people what is already
  stored; no page reads a client's data from Microsoft. It calls Microsoft only
  for sign-in, setup and checks someone asks for: signing staff and clients'
  people in with Microsoft (`sso.py`), the wizards that create or update an app
  (`app_setup.py`), testing a new certificate (`cert_lifecycle.py`), checking an
  app connection whose client ID was just saved (`connection_check.py`), and
  looking up which tenant a domain belongs to when consent comes back
  (`discovery.py`). An email someone sends from a page (*Send now*, a test
  email) goes from there too, through Microsoft 365 when that's how mail is
  sent.

**A nightly collection.** Once a minute the worker's main loop asks
`schedule.py` whether the collection time has passed. If it has, it queues a
run per tenant per collector in the `runs` table. Collector threads claim
runs one at a time (never two for one tenant at once). A run signs in with
the app's certificate, reads Microsoft Graph through `graph.py` (GET only)
and Exchange Online through `exchange.py` (`Get-` cmdlets only), paced by one
shared rate limiter (`ratelimit.py`, limits in `endpoints.py`). It stores
what it finds with `ctx.objects()` and `ctx.metric()`. When a tenant has
nothing left queued, the checks run against the new snapshot, findings are
synced, alerts go out and the cross-tenant pages' cached rows are rebuilt.

**A page request.** Each request opens its own database connection and loads
the person from the server-side session. Every route has `@needs(...)`, naming
the capabilities it needs (`permissions.py`), or `@login_required` for the few
pages anyone signed in may open. Anything about one tenant loads it through
`access.tenant_or_404()`, the single authorization gate. A report page calls
a builder in `reports/` that reads a `TenantSnapshot` (the latest successful
data per collector) and returns plain data. The same data becomes the web
page, a PDF (`reports/export.to_pdf()`), and XLSX or CSV (`reports/tables.py`).

**An emailed report.** `delivery.tick()`, a periodic job on the worker, sends
each schedule when its time comes, refreshing stale data first, and keeps a
copy of every file a client was sent in `issued_reports`.

## Where things live

Paths are in `v2/`, except the website.

| Path | What's there |
|---|---|
| `officesentry/collectors/` | one module per data source (`@register`); they read Microsoft and store snapshots |
| `officesentry/checks/` | the checks, one module per area; they read snapshots only, never Microsoft |
| `officesentry/reports/` | report builders (`@report`), charts, PDF/XLSX/CSV export, cross-tenant rollups, and the pages for a person, mailbox, group or site |
| `officesentry/web/` | the Flask app: routes, `templates/`, `static/` (`tokens.css` holds every colour and size); `access.py` is the single authorization gate |
| `officesentry/auditlog/` | the audit log add-on: each opted-in client's Microsoft 365 audit log, read through the Office 365 Management Activity feed | <!-- since 2.4.0 -->
| `officesentry/migrations/` | numbered SQL files, applied in order, never edited once applied |
| `officesentry/*.py` | everything else, one line each in [Every module](#every-module) below |
| `tests/` | the pytest suite; `graphdata.py` and friends are simulated Microsoft responses |
| `tests/auditdata.py` | the audit log add-on's Management Activity feed, simulated with `responses` | <!-- since 2.4.0 -->
| `tests/browser/` | screenshot and accessibility tests (their README says how to run them) |
| `tests/layout/` | every page at four widths, for layouts that break (likewise) |
| `scripts/` | demo data, screenshots, the check catalogue generator, scale data, the month-end dry run |
| `docs/` | guides for MSPs (onboarding, troubleshooting, `checks.md`) |
| `website/` (beside `v2/`) | officesentry.co.uk, built from these Markdown files |

### Every module

The modules at the top of `v2/officesentry/`, each in a line from its docstring.

| Module | What it does |
|---|---|
| `__main__.py` | the command line: `python -m officesentry <command>` (`--help` lists the commands) |
| `adminactivity.py` | which Entra ID directory audit events matter, and what they mean |
| `alerts.py` | alerts to Microsoft Teams, Slack or any webhook |
| `app_setup.py` | the setup wizard: creating Office Sentry's app from the portal, with a device-code sign-in |
| `authmethods.py` | how strong each person's sign-in methods are, and what the methods policy says |
| `backups.py` | the `backup` and `restore` commands, and the copy taken before a database upgrade |
| `canary.py` | the live canary: has Microsoft changed what it sends since the fixture was recorded? |
| `cert_lifecycle.py` | certificate expiry warnings, and replacing a certificate without a gap |
| `certs.py` | self-signed certificates for Entra app-only sign-in |
| `changes.py` | what changed between two snapshots of a tenant |
| `clouds.py` | the national clouds: every Microsoft host Office Sentry signs in to or reads from |
| `config.py` | configuration from the environment, with separate session and encryption keys |
| `connection_check.py` | checking a tenant's app connection, and sign-in failures in plain words |
| `coverage.py` | Conditional Access coverage: which enabled policies apply to which users |
| `crypto.py` | encryption of stored credentials (Fernet, rotatable) |
| `db.py` | SQLite access and schema migrations |
| `delivery.py` | scheduled emails: what gets sent, to whom and when |
| `devices.py` | device helpers for the device checks and the Devices report, and the shared timestamp parsers |
| `discovery.py` | which tenant a domain belongs to, from Microsoft's public sign-in metadata |
| `dnscheck.py` | email domain health from public DNS: MX, SPF, DMARC, DKIM, MTA-STS and TLS-RPT |
| `docker_api.py` | the updater's small client for the Docker Engine API on its Unix socket |
| `endpoints.py` | every call to Microsoft: path, version, permission, throttling limits and fallback |
| `entra.py` | the read-only permission list, creating the app registration, and consent links |
| `events.py` | the activity history: sign-ins and directory audit events kept for months or years |
| `exchange.py` | the Exchange Online admin API client: app-only and read-only |
| `findings.py` | the findings lifecycle: each evaluation turned into lasting history |
| `first_run.py` | the Get started checklist an admin sees until the first collection has run |
| `formatting.py` | numbers, money, dates and labels as people read them, for pages, reports and emails |
| `graph.py` | the Microsoft Graph client: app-only, read-only, paging and throttling-aware |
| `home.py` | the Today and Accounts home pages, each one sentence and then the detail |
| `hybrid.py` | hybrid identity helpers for the directory sync checks and the Hybrid identity report |
| `identity_protection.py` | Entra ID Protection's risky users: what the states and details mean |
| `intune.py` | Intune policies in plain words: the platform, what each requires and who it's assigned to |
| `issued.py` | the report archive: a copy of every report file a client was sent |
| `licences.py` | licence names, the prices the MSP enters, and licence-waste maths |
| `lifecycle.py` | a client's lifecycle: first collection, pause, archive and delete |
| `livetest.py` | `live-test`: every collector against a real test tenant, signing in with a client secret |
| `logs.py` | Office Sentry's own log: where it goes and what each line carries |
| `mailer.py` | sending an email over SMTP, or with Graph `sendMail` from the MSP's own mailbox |
| `mailflow.py` | mail-flow helpers for checks and reports: outside addresses and inbox rule risk |
| `mailprotection.py` | mail flow rules, connectors and protection policies in plain words |
| `month_end.py` | month-end reporting: report plans, the review queue, commentary and approval |
| `msauth.py` | app-only tokens from a connection's certificate, for Graph, Exchange and SharePoint |
| `onboarding.py` | onboarding many clients: CSV import, where each one has got, and confirming consent |
| `outbound.py` | connections to addresses people typed (webhooks, MTA-STS policy files, the SMTP server), never to an internal address |
| `packing.py` | packing snapshot rows stored before compression into compressed packs, a little at a time |
| `passwords.py` | passwords people may not choose: common ones, and ones built from their username |
| `permission_batch.py` | new read-only permissions released together, so each client approves once |
| `permissions.py` | the capabilities, and the built-in roles made of them |
| `projects.py` | recommended projects: the work a client's tenant shows it needs |
| `ratelimit.py` | Microsoft's limits shared by every collector thread, and each run's request counts |
| `recording.py` | recording a live run's Microsoft responses as a scrubbed test fixture |
| `retention.py` | how long personal data is kept, and the worker's daily prune |
| `roles.py` | roles in the database, and what one person can do once their role and overrides are added up |
| `rules.py` | old imports only: the checks moved to `checks/` |
| `schedule.py` | cron schedules in a timezone, collection freshness and periodic jobs |
| `search_index.py` | the index the sidebar search reads, kept by the worker |
| `serve.py` | the portal on a production web server, for running without Docker |
| `sharepoint_api.py` | SharePoint's own REST API: app-only and read-only |
| `signins.py` | the sign-in log summarised: what Microsoft's error codes mean |
| `snapshot.py` | read access to a tenant's latest collected data, for checks and reports |
| `sso.py` | sign-in with Microsoft (OpenID Connect), for staff and clients' own people |
| `state_history.py` | the daily history: every collected object stored once per version |
| `storage.py` | the tenant's SharePoint storage: how much it has and how much is used |
| `store.py` | data access for tenants, connections, runs and snapshots |
| `support.py` | the support package: one zip for a bug report, with secrets and client names taken out |
| `system_alerts.py` | telling the MSP's team when Office Sentry itself needs attention |
| `system_health.py` | Office Sentry's own health: `/health/ready` and System status |
| `tasks.py` | background tasks: long work handed to the worker, with leases |
| `tenant_groups.py` | client groups: the MSP's own slices of its clients (tier, region, service level) |
| `tenant_health.py` | whether a client is set up and collecting properly, in plain words with fixes |
| `text.py` | counted nouns for summaries: "1 admin", "3 admins" |
| `throttle.py` | sign-in throttling by address that can't be turned against the person signing in |
| `totp.py` | one-time codes for two-step sign-in, and recovery codes |
| `upcoming.py` | Coming up: what becomes a problem in the next 30, 60 or 90 days, across clients |
| `updater.py` | the updater container: checks a release's signature, installs it, goes back if it fails |
| `updates.py` | the daily release check, and asking the updater to install one |
| `usage.py` | Microsoft 365 usage per person, and licence right-sizing |
| `worker.py` | the background worker: collections, background tasks, the schedule, emailed reports and retention |

## Tests

```bash
cd v2
pytest -n auto        # the whole suite on every core; plain `pytest` is easier to debug
ruff format . && ruff check .
```

Each test gets a throwaway install from `tests/conftest.py`: a temporary data
folder, a migrated database, the app, a test client and fixtures such as
`two_tenants`. `tests/seed.py` runs every collector against the simulated
responses, which is what most check and report tests start from.

Some safety rules are tests, so breaking one fails CI rather than relying on
review: nothing writes to a tenant (`test_no_writes.py`), every page needs a
sign-in (`test_access.py`), every page refuses a role that lacks the
capabilities it needs (`test_roles.py`), no one sees another tenant's data
(`test_isolation_walk.py`), every Microsoft call is listed in `endpoints.py`
(`test_ratelimit.py`), and every check and report survives missing or odd
data (`test_robustness.py`).

Templates and CSS also have browser tests that compare screenshots with
baselines; see `tests/browser/README.md`. CI runs them on pull requests that
touch the UI, along with the layout checks in `tests/layout/`: every page at
1366, 1440 and 1920 px and on a phone, with long names and empty data, for
pages that scroll sideways, cut text off, overlap or push controls off the
screen.

## Simulating Microsoft at the HTTP level

Tests never replace `GraphClient` or `ExchangeClient` with a fake. They fake
HTTP with [`responses`](https://github.com/getsentry/responses), so URLs,
paging, retries and error handling are tested for real:

```python
@responses.activate
def test_unreadable_subscriptions_leave_the_rest_of_the_profile(db, two_tenants, factory):
    alpha, _ = two_tenants
    responses.get(f"{V1}/organization", json={"value": [{"id": "org1", "displayName": "Alpha"}]})
    responses.get(f"{V1}/subscribedSkus", json={"value": SKUS})
    responses.get(
        f"{V1}/directory/subscriptions",
        status=403,
        json={"error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges"}},
    )
    run_id, status = _run(db, alpha, "tenant_profile", factory)
    assert status == "partial"
```

Shared simulated responses live in `tests/graphdata.py` (and `exchangedata.py`,
`devicedata.py` and the other `*data.py` files). Copy the shape from
Microsoft's documentation, or better, from a real response.

## Testing against a real tenant

`python -m officesentry live-test [--out DIR]` runs every collector against a
test tenant using `OFFICESENTRY_TEST_TENANT_ID`, `OFFICESENTRY_TEST_CLIENT_ID`
and `OFFICESENTRY_TEST_CLIENT_SECRET`, stores the result as client "Live test"
and prints each collector's outcome and every check. The test tenant needs an
app registration with the read-only permissions in `entra.GRAPH_PERMISSIONS`
and the Global Reader role. The client secret is for this command only; the
product signs in with a certificate.

**The recorded tenant.** `tests/fixtures/live_tenant.json` holds real
responses from one Microsoft 365 Business Premium test tenant (Entra ID P1,
synced from on-premises AD, no Intune devices, one team), with names,
addresses, phone numbers, domains and IDs replaced. It was last recorded on
12 October 2026. `tests/test_live_fixture.py` replays it, so every collector,
check and report also runs on what Microsoft actually returns, in CI too.
When a collector starts asking Microsoft for something new, record the fixture
again against a test tenant of your own:

```bash
export OFFICESENTRY_TEST_TENANT_ID=... OFFICESENTRY_TEST_CLIENT_ID=... OFFICESENTRY_TEST_CLIENT_SECRET=...
python -m officesentry live-test --record tests/fixtures/live_tenant.json
```

Scrubbing happens in memory (`officesentry/recording.py`): nothing raw is
written, and it refuses to write the file if a name, domain or phone number
survives. Still read the diff before committing it, and say in the pull
request what you recorded. One person re-records at a time, since the file is
large and merges badly.

**What the recording doesn't cover.** These areas have no real recording, or
only an empty one, and are tested with simulated data only:

- **Entra ID P2 features**: risky users, sign-in risk, PIM-eligible roles and
  PIM for groups (the test tenant has no P2; Microsoft answers "not licensed",
  and no group in it holds a role).
- **Per-user MFA states**: Office Sentry only reads them on tenants without
  Entra ID P1.
- **Sign-in methods read one person at a time**: only used when Microsoft's
  registration report comes back empty, as it does on a newly licensed tenant.
- **Intune device details**: the tenant has no enrolled devices, so the
  device compliance and encryption checks have nothing to read.
- **Intune policies**: Microsoft's answers are real but empty, since the tenant
  has no compliance policies, configuration profiles or app protection
  policies, so the policies themselves are simulated.
- **BitLocker recovery keys**: the tenant hasn't approved
  BitLockerKey.ReadBasic.All yet, so the replay covers the waiting state.
- **Mail connectors and changed protection policies**: the tenant's mail flow
  rule and its default and preset protection policies are real, but it has no
  connectors, allowed or blocked senders, or policies of its own, so those are
  simulated.
- **Teams channels, guests, apps and activity**: the tenant's one team has
  only its General channel and Microsoft's own apps, and Microsoft hides names
  in its usage reports, so private and shared channels, guests and people from
  other organisations in them, custom apps and each team's activity are
  simulated.
- **SharePoint groups and permission levels** (Site permissions, and owners
  groups in Site access): `live-test` signs in with a client secret, which
  SharePoint's own API refuses, and the tenant hasn't approved its SharePoint
  permission, so the replay covers the waiting state.
- **National clouds** (GCC High, DoD, China): there's no test tenant in
  them; the endpoints come from Microsoft's documentation.
- **Risky sharing**: the tenant's shared file is shared with one person in the
  organisation, so anyone links and sharing outside the organisation are
  simulated.
- **Forwarding to someone in the directory**: the tenant forwards to an email
  address, so resolving a forwarding recipient (`Get-Recipient`) is simulated.
- **SharePoint recycle bin items**: Microsoft answered the recycle bin request
  with an empty list, though the site's storage figures count a deleted file.
- **The audit log add-on**: `live-test` runs add-on collectors only when asked for them, and the recording has none, so Microsoft's activity feed is simulated (`tests/auditdata.py`). <!-- since 2.4.0 -->

## Adding things

- **A collector**: a module in `collectors/` that subclasses `Collector` with
  `@register`, imported in `collectors/__init__.py`; every new Graph path or
  cmdlet listed in `endpoints.py`; a test in `tests/test_collectors.py`. A new
  Microsoft permission must be read-only and makes every client approve again,
  so raise it in an issue first.
- **A check**: a function decorated `@rule(...)` in its area's module in
  `checks/`. It reads the snapshot only and returns "not checked", with the
  reason, when data or a licence is missing. Then regenerate the catalogue:
  `PYTHONPATH=. python scripts/gen_check_catalogue.py > docs/checks.md`.
- **A report**: a builder declared with `@report(...)` in `reports/`. That one
  declaration gives it a page, its menu entry, search, downloads, a
  cross-tenant view and month-over-month changes.
- **A database change**: a new `migrations/NNNN_description.sql` with the next
  free number. Never edit one that has been released.

## How pull requests are reviewed

1. Open a pull request against `main` and fill in the template's checklist.
   Keep it to one change, finished end to end. For anything large (a new
   permission, a new area, a change to how data is stored), open an issue or
   a Discussion first.
2. CI runs the format and lint checks, the tests with a 90% coverage floor,
   a Docker build, a dependency audit and the sign-off check. **CI passed**
   sums them up; a pull request isn't merged until it's green.
3. The maintainer, [@JackD99](https://github.com/JackD99), reviews every pull
   request (`.github/CODEOWNERS`). Expect questions about what a person using
   Office Sentry sees, and screenshots for anything that looks different.

What is planned next is in [ROADMAP.md](ROADMAP.md).
