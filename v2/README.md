# Office Sentry v2

Read-only Microsoft 365 reporting for MSPs: nightly snapshots of every client
tenant, cross-tenant rollups, month-over-month changes and branded client
reports. This is the rebuild planned in the 2026-10 review; the v1 portal
remains in `../portal` until v2 replaces it.

**Status: Phase 2.** Tenants can be connected and collected nightly, and the
**Monthly Security & Licensing Review** is available per tenant (HTML, PDF,
XLSX) with a cross-tenant overview. Next: findings history and "changes since
last month" (Phase 3), then Exchange and SharePoint reports (Phase 4).

## Reports

- **Monthly Security & Licensing Review** (`/tenants/<id>/report`): KPIs with
  4-week change, Secure Score trend, failing checks with affected accounts and
  recommendations, licence waste in money, passing checks, checks that the
  tenant's licences can't support, and data freshness.
- **Overview** (`/reports`): one row per tenant with severity counts and KPIs.
- **Licence prices** (admin): enter monthly prices per SKU to cost waste.

Checks: MFA enforced (CA or Security Defaults, with exclusions), legacy auth
blocked, admins without MFA, Global Admin count, guest admins, apps with
admin roles, users without MFA, inactive accounts, licences on disabled
accounts, unassigned paid licences.

## What it does differently from v1

- **Read-only by construction.** The Entra app requests only `*.Read*`
  permissions, and the Graph client has no write methods.
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
  worker.py      queue runner, nightly scheduler, orphan recovery
  web/           Flask app: access.py is the single authorization gate
```
