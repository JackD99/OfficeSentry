# CLAUDE.md: Office Sentry v2

Read-only Microsoft 365 reporting for MSPs. See README.md for the user-facing
overview and `/mnt/project-files/officesentry-review/00-recommendation.md`
(if present) for the plan this follows.

## Rules that must not be broken

- **Never write to a tenant.** No POST/PATCH/DELETE to Graph from collectors.
  `GraphClient` exposes GET only; keep it that way. `ExchangeClient` POSTs to
  InvokeCommand, so it only runs cmdlets in `READ_ONLY_CMDLETS`; only ever add
  `Get-*` cmdlets there. New permissions must be
  read-only and go in `entra.GRAPH_PERMISSIONS` (adding one forces every
  tenant to re-consent, so batch them).
- **The one non-GET Graph call is `mailer._send_graph`** (`sendMail` from the
  MSP's own mailbox, via a separate single-tenant app with `Mail.Send`). Never
  add mail permissions to the multi-tenant read-only app.
- **Every route needs `@login_required` or `@roles_required`.** Anything
  tenant-scoped loads the tenant with `access.tenant_or_404()`.
  `tests/test_access.py::test_every_page_requires_login` enforces the first.
- **Graph paths are relative to the version** (`"/users"`, `version="beta"`).
- **Credentials never touch disk as files** and are never sent to the browser.
- **Mock Graph with `responses`, never by replacing `GraphClient`.**
- **Sessions are server-side** (`web/sessions.py`). To sign someone out, bump
  `users.session_version` or delete their `sessions` rows; never trust anything
  in the cookie except the session token. New encrypted columns go in
  `ENCRYPTED_COLUMNS` in `__main__.py` so `rotate-keys` re-encrypts them.

## Adding a collector

1. `officesentry/collectors/<name>.py`, subclass `Collector`, decorate `@register`.
2. Write objects with `ctx.objects(kind, [(id, dict)])` and numbers with
   `ctx.metric(key, value)`. Use `ctx.warn()` for a section the tenant's
   licence can't provide (run becomes `partial`), and `ctx.capabilities`
   (from `tenant_profile`) to skip what it can't have.
3. Import it in `collectors/__init__.py`.
4. Test it in `tests/test_collectors.py` with recorded Graph responses.

Exchange data: use `ctx.exchange.invoke("Get-...")` (created lazily). An
`ExchangeAccessDenied` fails the run with instructions to assign Global Reader.

## Adding a check or report

- Checks go in `rules.py` with `@rule(...)`, or in a module imported at the
  bottom of `rules.py` (see `sharepoint_rules.py`). They read `TenantSnapshot` only,
  never Graph, and return `unavailable` (with the reason) when data or the
  tenant's licence is missing, rather than passing or failing.
- Detail reports are builders in `reports/data_reports.py` returning
  `Section`s of rows; register them in `REPORTS` and they get a page, a tab
  and an XLSX export.
- Detail reports can also live in their own module (see `reports/sharepoint.py`)
  and register in `reports/__init__.py`. A builder can set `tiles` (headline
  figures), `chart`, `notice` (something the admin must fix first) and use
  `Link` cells; the page and XLSX render them.
- Report builders in `reports/` return plain data; templates and
  `reports/export.py` only format it. Charts are server-side SVG
  (`reports/charts.py`) so the PDF matches the page.
- `tests/seed.py` runs every collector against `tests/graphdata.py`; use it
  for rule and report tests.
- Client-facing reports (page + PDF) extend `templates/reports/_layout.html`
  (cover, MSP branding, running headers and page numbers) and build from the
  macros in `reports/_components.html` (verdict, KPI tile, severity bar,
  numbered actions). Plain-English summaries live in `reports/summary.py`.
  Styles are `report.css`; brand colours arrive as CSS variables (`--brand`,
  `--brand-ink`, ...), from `/brand.css` on the web and inline in PDFs, since
  the CSP blocks inline styles in the browser. Render the PDF and look at it.

## Web UI conventions

- Styles live in `web/static/app.css` (tokens at the top, light and dark).
  Reports that also become PDFs use `report.css` instead.
- Every tenant page starts with `tenant_header(tenant, '<tab>')` from
  `_macros.html` (import it `with context`). Add new tenant pages as a tab there.
- Show times with the `ts()` macro or the `ago`/`when`/`day` filters, never raw
  ISO strings. Licence flags go through the `capability` filter.
- Tables that people read on phones get `class="stack-sm"` and a
  `data-label` on every cell after the first.
- Scripts need `nonce="{{ csp_nonce }}"`; there are no external assets.

## Background work

`worker.py` is the only background process: one loop that claims queued runs
and, once a minute, queues the collection slot (`schedule.py`, cron +
timezone, skipping collectors fresher than `max_age_hours`) and sends due
report emails (which first refresh stale collectors). It prunes snapshots once
a day with `store.prune_snapshots` (latest run per day for 30 days and per
month for 12; metrics, findings and run rows are never pruned), so code that
reads an older run must cope with its objects being gone (see
`changes._baseline_run`). A side thread records the heartbeat that the
portal banner and `healthcheck worker` read. Don't add Celery or Redis.

To add timed work (emails, digests), register a job in its own module with
`@schedule.periodic("name")`; it gets a `TickContext` every minute and uses
`schedule.due_slot(cron, tz, ctx.now, last_slot=..., not_before=...)` to
decide whether to act. Add the module to `worker.JOB_MODULES`. See
`delivery._email_job` and `schedule.refresh_for_report`.

## Email delivery

`delivery.tick()` (the `email_delivery` periodic job) sends scheduled emails
and retries failed alert emails. A report schedule's send time is a cron slot
(`delivery.schedule_cron`), and stale collectors are refreshed first. Alert emails are not a separate engine: `alerts.after_sync()`
(the Teams/Slack/webhook alert event after each collection) calls
`delivery.after_sync()`, so both go out on the same findings. Schedules store their own timezone and the last period they finished
("2026-10", "2026-W41", "2026-Q4"); new detail reports in `data_reports.REPORTS`
show up in the schedule form automatically. Email templates live in
`web/templates/emails/` and use inline styles and tables only (email clients
ignore stylesheets). Clients never see team contacts, hidden copies or errors.

## Schema changes

Add `migrations/NNNN_description.sql`. Never edit an applied migration.

## Commands

`pytest`, `ruff check .`, `python -m officesentry {web,worker,create-admin,setup-link,create-app,rotate-keys,backup,healthcheck}`.
Docker: `docker compose up -d --build` (see DEPLOY.md).

Tests sign in with a password only (`require_mfa="off"` in `conftest.py`);
`tests/test_login_security.py` covers two-step sign-in.
