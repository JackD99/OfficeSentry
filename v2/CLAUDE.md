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
- **Every route needs `@login_required` or `@roles_required`.** Anything
  tenant-scoped loads the tenant with `access.tenant_or_404()`.
  `tests/test_access.py::test_every_page_requires_login` enforces the first.
- **Graph paths are relative to the version** (`"/users"`, `version="beta"`).
- **Credentials never touch disk as files** and are never sent to the browser.
- **Mock Graph with `responses`, never by replacing `GraphClient`.**

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

- Checks go in `rules.py` with `@rule(...)`. They read `TenantSnapshot` only,
  never Graph, and return `unavailable` (with the reason) when data or the
  tenant's licence is missing, rather than passing or failing.
- Detail reports are builders in `reports/data_reports.py` returning
  `Section`s of rows; register them in `REPORTS` and they get a page, a tab
  and an XLSX export.
- Report builders in `reports/` return plain data; templates and
  `reports/export.py` only format it. Charts are server-side SVG
  (`reports/charts.py`) so the PDF matches the page.
- `tests/seed.py` runs every collector against `tests/graphdata.py`; use it
  for rule and report tests.

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

## Schema changes

Add `migrations/NNNN_description.sql`. Never edit an applied migration.

## Commands

`pytest`, `ruff check .`, `python -m officesentry {web,worker,create-admin,create-app,rotate-keys}`
