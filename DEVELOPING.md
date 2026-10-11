# Developer guide

A short tour for people who want to change Office Sentry: how to run it, how
it fits together, where things live, how to test against Microsoft without a
tenant, and how pull requests are reviewed. [CONTRIBUTING.md](CONTRIBUTING.md)
has the rules every change follows (sign-off, dependencies, releases).

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

## How it fits together

Office Sentry is two processes from one Docker image, sharing one SQLite
database. There is no Redis, Celery or message broker.

- **The portal** (`officesentry/web/`, Flask) shows people what is already
  stored. It never calls Microsoft for a page.
- **The worker** (`officesentry/worker.py`) is the only thing that talks to
  Microsoft, and it only reads. It also runs the schedule, sends report
  emails and alerts, and tidies old data.

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

```
v2/
  officesentry/
    collectors/   one module per data source (@register); they read Microsoft and store snapshots
    checks/       the checks, one module per area; they read snapshots only, never Microsoft
    reports/      report builders (@report), charts, PDF/XLSX/CSV export, cross-tenant rollups
    web/          the Flask app: routes, templates/, static/ (tokens.css holds every colour and size)
    graph.py      Graph client (GET only, paging, retries)    exchange.py   Exchange client (Get- only)
    endpoints.py  every Microsoft call, its permission and limits
    store.py      runs, snapshots and metrics                 db.py         SQLite and migrations
    worker.py     the background process                      delivery.py   scheduled emails
    logs.py       the rolling log files and line context       support.py    the support package (redacted diagnostics)
    migrations/   numbered SQL files, applied in order, never edited once applied
  tests/          pytest suite; graphdata.py and friends are simulated Microsoft responses
  tests/browser/  screenshot and accessibility tests (their README says how to run them)
  scripts/        demo data, screenshots, the check catalogue generator, scale data, the month-end dry run
  docs/           guides for MSPs (onboarding, troubleshooting, checks.md)
website/          officesentry.co.uk, built from these Markdown files
```

`v2/README.md`, "Layout", describes every top-level module in a line.

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
touch the UI.

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

**The recorded tenant.** `tests/fixtures/live_tenant.json` holds real
responses from a Microsoft 365 Business Premium test tenant, with names,
addresses, phone numbers, domains and IDs replaced. `tests/test_live_fixture.py`
replays it, so every collector, check and report also runs on what Microsoft
actually returns. When a collector starts asking Microsoft for something new,
record the fixture again against a test tenant of your own:

```bash
export OFFICESENTRY_TEST_TENANT_ID=... OFFICESENTRY_TEST_CLIENT_ID=... OFFICESENTRY_TEST_CLIENT_SECRET=...
python -m officesentry live-test --record tests/fixtures/live_tenant.json
```

The test tenant needs an app registration with the read-only permissions in
`entra.GRAPH_PERMISSIONS` and the Global Reader role. The client secret is for
this command only. Scrubbing happens in memory (`officesentry/recording.py`):
nothing raw is written, and it refuses to write the file if a name, domain or
phone number survives. Still read the diff before committing it, and say in
the pull request what you recorded. One person re-records at a time, since
the file is large and merges badly.

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
