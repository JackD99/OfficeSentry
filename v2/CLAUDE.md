# CLAUDE.md: Office Sentry v2

Read-only Microsoft 365 reporting for MSPs. See README.md for the user-facing
overview and `/mnt/project-files/officesentry-review/00-recommendation.md`
(if present) for the plan this follows.

## Rules that must not be broken

- **Never write to a tenant.** No POST/PATCH/DELETE to Graph from collectors.
  `GraphClient` exposes GET only; keep it that way. New permissions must be
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

## Schema changes

Add `migrations/NNNN_description.sql`. Never edit an applied migration.

## Commands

`pytest`, `ruff check .`, `python -m officesentry {web,worker,create-admin,create-app,rotate-keys}`
