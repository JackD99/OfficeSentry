# Office Sentry v2

Read-only Microsoft 365 reporting for MSPs: nightly snapshots of every client
tenant, cross-tenant rollups, month-over-month changes and branded client
reports. This is the rebuild planned in the 2026-10 review; the v1 portal
remains in `../portal` until v2 replaces it.

**Status: Phase 1 (foundation).** Tenants can be connected and collected
(tenant profile + users). Reports arrive in Phase 2.

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
  worker.py      queue runner, nightly scheduler, orphan recovery
  web/           Flask app: access.py is the single authorization gate
```
