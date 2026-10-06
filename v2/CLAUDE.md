# CLAUDE.md: Office Sentry v2

Read-only Microsoft 365 reporting for MSPs. See README.md for the user-facing
overview and DEPLOY.md for running it on a server.

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
  `ENCRYPTED_COLUMNS` in `__main__.py` so `rotate-keys` re-encrypts them
  (encrypted settings keys go in a module's `ENCRYPTED_KEYS`, gathered in
  `ENCRYPTED_SETTINGS`); name them `*_enc`, `test_key_rotation.py` checks.
- **Audit with `db.audit()`**; it records the visitor's address. Repeated
  anonymous events go through `db.audit_counted()` (one row per address and
  hour), and never store what a stranger typed. Sign-in noise is in
  `db.AUDIT_NOISE` and pruned after 90 days. Admins read it at `/admin/activity`.
- **Every PDF goes through `reports/export.to_pdf()`**, whose URL fetcher only
  loads files under `web/static/` and `data:` URIs. Never call WeasyPrint
  directly, and put fonts and images a PDF needs in `web/static/`.

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

The two SharePoint collectors list sites and OneDrives through
`collectors/sharepoint_lookup.Lookups`, so the second one each night reuses
the first one's answers (when they have the fields it asks for) and people
without a OneDrive aren't asked about for a week.

## Adding a check or report

- Checks go in `rules.py` with `@rule(...)`, or in a module imported at the
  bottom of `rules.py` (see `sharepoint_rules.py`). They read `TenantSnapshot` only,
  never Graph, and return `unavailable` (with the reason) when data or the
  tenant's licence is missing, rather than passing or failing.
- A check's title is its goal ("No inactive accounts"); its summary is what was
  found ("4 accounts inactive for 90+ days") and leads wherever a failing check
  is listed, with `RuleResult.action` (the recommendation's first instruction)
  under it. Count things with `text.plural(n, "account")` (the `plural` filter
  in templates), never "account(s)".
- Detail reports are builders in `reports/data_reports.py` returning
  `Section`s of rows; register them in `REPORTS` and they get a page, a tab
  and XLSX, CSV and PDF exports. Add the key to a menu group in
  `data_reports.GROUPS`; one left out shows under "More reports".
- Detail reports can also live in their own module (see `reports/sharepoint.py`)
  and register in `reports/__init__.py`. A builder can set `tiles` (headline
  figures), `chart`, `notice` (something the admin must fix first) and use
  `Link` cells; the page and XLSX render them.
- Headline figures open the rows behind them: give a `Tile` or `Highlight`
  `rows=Rows("<table title>", {"<column>": value or [values]}, age=(col, "gt90"),
  flagged=True)`. A figure named exactly like a table links to it without that.
  `test_report_library.py` opens every seeded headline link.
- Tables are drawn by `templates/_report_table.html` from `reports/views.py`:
  value filters, date presets, hidden columns and sort all live in the query
  string (`f=`, `age=`, `hide=`, `sort=`, keyed by table and column slugs), and
  the page, PDF, XLSX and CSV apply the same view (`views.apply`). Pass lists
  for list cells (they become `Many`, so a filter offers each item). Saved
  views are per person, per report key (`saved_views` table, `web/saved_views.py`).
- The Reports page (`/reports`) lists every registered report. Add a new key to
  the jobs in `reports/library.USES` and search words to `KEYWORDS` if its
  title doesn't contain what people would type.
- Every detail report automatically gets a cross-tenant rollup (`reports/rollups.py`,
  `/across/<key>`) and is compared in the month-over-month change report
  (`reports/month_changes.py`, `/changes`, `/tenants/<id>/changes`). Rollup
  columns are the report's `tiles`/`highlights`, else a row count per table;
  `Rollup.total_row()` sums counts and money only (int or `Money` headlines,
  not scores or percentages). "Every row" (`/across/<key>/rows/<table slug>`,
  `rollups.all_rows`) stacks each visible tenant's rows from the same builders
  under a Tenant column, as one `DataReport`, so `views` filters, sorts, saved
  views and the CSV/XLSX/PDF downloads work unchanged; it keeps
  `rollups.MAX_ROWS` per table and says when it cut. `/across/checks`
  (`rollups.check_matrix`) is every check for every tenant, evaluated from
  stored data with accepted risks from `findings`; it writes nothing. All
  `/across` pages are admin and analyst only, over `visible_tenants()`.
  The change report matches table rows on their first column; set
  `Section(compare=False)` for event logs and time series. Columns named
  `Last ...`, `... (UTC)` or containing "change" are ignored as time-driven.
- Downloads go through `reports/tables.py` (`Table`, `to_csv`, `to_xlsx`): CSV
  prefixes formula characters, XLSX never stores formulas (`safe_xlsx`).
- Report builders in `reports/` return plain data; templates and
  `reports/export.py` only format it. Charts are server-side SVG
  (`reports/charts.py`) so the PDF matches the page.
- Per-person Microsoft 365 usage lives in `usage.py`: `usage.person(snap, upn)`
  returns one account's last activity and use per service (keys in
  `usage.SERVICES`), Office apps by platform, installs and activity counts, for
  any page about one person. Usage reports may conceal names (32-character
  codes); show `usage.label(row)` and `usage.CONCEALED_NOTICE`.
- `tests/seed.py` runs every collector against `tests/graphdata.py`; use it
  for rule and report tests.
- The insurance evidence pack (`reports/evidence.py`) suggests answers to
  questionnaire questions in `reports/evidence_answers.QUESTIONS` (Yes, Partly,
  No, Not known, a one-line justification and a source link), built from the
  snapshot and check results. Enforcement comes first: MFA required by
  Conditional Access with only a named break-glass exclusion is "Yes" listing
  it. Admins and analysts can override an answer with a note
  (`evidence_answer_overrides`, audited, note text never logged); the page,
  PDF, XLSX and CSV show the override and who set it, and only staff on the
  page see the suggestion it replaced and the "data changed" hint.
- Client-facing reports (page + PDF) extend `templates/reports/_layout.html`
  (cover, MSP branding, running headers and page numbers) and build from the
  macros in `reports/_components.html` (verdict, KPI tile, severity bar,
  numbered actions). Plain-English summaries live in `reports/summary.py`.
  Styles are `report.css`; brand colours arrive as CSS variables (`--brand`,
  `--brand-ink`, ...), from `/brand.css` on the web and inline in PDFs, since
  the CSP blocks inline styles in the browser. Render the PDF and look at it.
- PDF routes return `web/printing.pdf_response(template, filename, ...)`: a
  WeasyPrint PDF, or without WeasyPrint (Windows without Docker) the same
  template as a print-ready page that opens the browser's print window. Table
  reports (detail, All tenants, What changed) use `reports/detail.html`,
  `rollup.html`, `changes.html` and `tenant_changes.html` with
  `document = true` (and `wide = true` for landscape).
- `tests/test_live_fixture.py` replays `tests/fixtures/live_tenant.json`, real
  responses from a Business Premium test tenant with names, emails, domains and
  IDs replaced. When a collector asks Microsoft for something new, re-record it
  with `python -m officesentry live-test --record tests/fixtures/live_tenant.json`
  (needs the `OFFICESENTRY_TEST_*` variables). Scrubbing happens in memory and
  refuses to write the file if a name, domain or phone number survives; still
  skim the diff. Phone fields are replaced by key (`recording.PHONE_KEYS`)
  whatever their format; `tests/test_recording.py` covers the scrubber.

## Robustness contract

`tests/test_robustness.py` fuzzes every check in `rules.RULES`, every report in
`data_reports.REPORTS` and every `changes.*_changes` feed against degraded
snapshots (a field removed or null, a list where text is expected and the other
way round, timestamps of `""`, `"0001-01-01T00:00:00Z"`, `"not-a-date"` or
without their `Z`, no objects at all). The registries are read when the tests
run, so new checks and reports are fuzzed as soon as they exist.

- Missing fields, nulls, odd timestamps and empty collections happen with real
  Microsoft data, so every check, report and change feed handles them itself.
- Wrong types can't come from Graph; for those it's enough that the guards
  contain the crash: `rules.evaluate()` shows the check as "couldn't run",
  `data_reports.build()` shows the report as unavailable and
  `changes.all_changes()` leaves out that feed. Nothing returns a malformed result.
- Use the shared helpers: `devices.parse_ts` / `devices.parse_day` for any
  timestamp or date, `coverage.policy_name` for a policy with no name.

## One page per account

`reports/user_profile.py` builds `/tenants/<id>/users/<object id>` (and the
Users tab, `directory()`) from stored snapshots. Each area is a function
decorated `@part(order)` returning a `Part` (facts, `Section` tables, a short
summary); call `person.flag(severity, text, part_key)` for "Needs attention".
Orders are spaced by 10, so a new area slots in without touching the others.
`history()` diffs the account across retained snapshots. Detail report cells
that are exactly an account's sign-in name or address become `UserLink`s in
`data_reports.build()` (a `str` with `page_href`), so builders don't need to do
anything to link people; PDFs and spreadsheets show the plain name.

Staff search every visible tenant's latest snapshot from the sidebar box
(`web/search.py`, `/search`, admin and analyst only); to make something
searchable, add a `Kind(collector, kind, values, show)` to `search.KINDS`, with
`show` linking the person's page or the detail report filtered to the row.

## Web UI conventions

- Styles live in `web/static/app.css` (tokens at the top, light and dark).
  Reports that also become PDFs use `report.css` instead.
- Downloads on a page sit in one `downloads([(label, href, title)])` control
  from `_macros.html`, PDF first.
- Every tenant page starts with `tenant_header(tenant, '<tab>')` from
  `_macros.html` (import it `with context`). There are seven tabs (Overview,
  Findings, Changes, Reports, Users, Emails, Settings); a page that belongs
  under one gets its own key, mapped to that tab in `TAB_OF`, and a link in
  the tab's second row (`.tab-pages`), rather than a new tab.
- Show times with the `ts()` macro or the `ago`/`when`/`day` filters, never raw
  ISO strings. Licence flags go through the `capability` filter.
- Tables that people read on phones get `class="stack-sm"` and a
  `data-label` on every cell after the first.
- Scripts need `nonce="{{ csp_nonce }}"`; there are no external assets.
- The root README's screenshots (`docs/screenshots/`) and `examples/` come from
  `python scripts/screenshots.py` (demo data from `scripts/demo_data.py`); re-run
  it when the UI or reports change, and look at the images before committing.

## Background work

`worker.py` is the only background process (run one): `cfg.worker_concurrency`
collector threads claim queued runs (`store.claim_next_run`: highest
`runs.priority` first, then oldest, and never two runs for one tenant at once).
`execute_run` syncs findings (and alerts) once the tenant has nothing else
queued or running, so a night's collection is evaluated once. Token providers
(`msauth.shared_certificate_token_provider`) and each thread's HTTP session are
reused across runs. Meanwhile the main thread, once a
minute, queues the collection slot (`schedule.py`, cron + timezone, skipping
collectors fresher than `max_age_hours`) and sends due report emails (which
first refresh stale collectors). Every thread opens its own SQLite connection;
never share one across threads. Manual runs and report refreshes get
`store.PRIORITY_WAITING`. Once a day `retention.apply` prunes under
the admin's *Data retention* settings (`retention.*` keys in `settings`;
unset means today's defaults): snapshots with `store.prune_snapshots` (latest
run per day for 30 days and per month for 12 by default; metrics and run rows
are never pruned), sign-in detail objects (`retention.SIGNIN_KINDS`) once
older than their period, resolved findings, run log info lines older than 90
days (`store.prune_run_logs`; warnings and errors stay), and, when set, the
activity log and issued reports (each never under 365 days). So code that
reads an older run must cope with its objects, or just its sign-in kinds,
being gone (see `changes._baseline_run`). A new store of personal data gets
a period there, with a dry-run count for the page, and deletes in batches. A side thread records the heartbeat that the portal banner and
`healthcheck worker` read, but only while the worker makes progress
(`worker.Liveness`: the main loop came round in the last 10 minutes and no run
has gone for 4 hours), so a wedged loop or thread shows as down. A failing
housekeeping pass (say "database is locked") is logged, rolled back and
retried the next minute; it never ends the worker. Don't add Celery or Redis.

The dashboard and the All tenants review read each tenant's row from
`rollup_cache` (`monthly_review.rollup_row(..., cached=True)`), rebuilt when
its `_fingerprint` changes (latest run, findings, prices, currency, app
connections, the code, the day). If the row comes to depend on something
else, add it to the fingerprint. The worker rebuilds a tenant's row after its
last queued run and every row once a day. Runs are read through the indexes in
`migrations/0016`; check a new query on `runs` with `EXPLAIN QUERY PLAN`, and
note that most rows are finished, so a filter on `status` alone needs
`INDEXED BY idx_runs_status`.

To add timed work (emails, digests), register a job in its own module with
`@schedule.periodic("name")`; it gets a `TickContext` every minute and uses
`schedule.due_slot(cron, tz, ctx.now, last_slot=..., not_before=...)` to
decide whether to act. Add the module to `worker.JOB_MODULES`. See
`delivery._email_job` and `schedule.refresh_for_report`.

## Email delivery

`delivery.tick()` (the `email_delivery` periodic job) sends scheduled emails
and retries failed alert emails. A report schedule's send time is a cron slot
(`delivery.schedule_cron`), and stale collectors are refreshed first. Alert emails are not a separate engine: `alerts.after_sync()`
(the Teams/Slack/webhook alert event after each findings sync) calls
`delivery.after_sync()`, so both go out on the same findings. Schedules store their own timezone and the last period they finished
("2026-10", "2026-W41", "2026-Q4"); new detail reports in `data_reports.REPORTS`
show up in the schedule form automatically. Email templates live in
`web/templates/emails/` and use inline styles and tables only (email clients
ignore stylesheets). Clients never see team contacts, hidden copies or errors.

Reports are built live, so what a client was sent is kept separately:
after a report email goes out, `delivery.send_schedule` stores each attached
PDF/XLSX in `issued_reports` (`issued.py`: bytes as a BLOB, period, version,
recipients, SHA-256). Snapshot pruning doesn't touch it; it's kept until the
admin's retention setting says otherwise (never under a year, and never a
tenant's latest file of each report), is backed up with the database and goes with the tenant on delete. The
tenant's Report history page (`/tenants/<id>/report-history`, linked from
Emailed reports and the client home) lists and downloads them; clients see
the files but not who they went to. Archiving never fails a send.

## Month-end reporting

`month_end.py` (pages in `web/reporting.py`, nav "Month-end"). A **report
plan** (`report_plans`) is one monthly recipe (reports, day, hour, timezone,
subject and message with `{client}`/`{month}`, `delivery.fill_placeholders`)
applied to many tenants at once: `apply_plan` creates or updates one ordinary
`email_schedules` row per tenant (`plan_id`, `subject`, `on_approval`), linked
in `report_plan_tenants`, so there is no second sender. Mode "send on
approval" (or delivery "portal only") sets `on_approval`, and
`delivery._tick_schedule`, Send now and Retry never send those. The Email
reports and Report plans pages show `month_end.coverage()` (who has a monthly
report, who has no contacts or recipients) with a fix link per gap.

`/reporting/<yyyy-mm>` is the queue: every visible tenant on a plan, with an
implicit Draft per month (`report_reviews` only gets a row once someone acts),
then In review, Approved, Sent. The period is the schedule period the report
goes out in. Flags: new critical findings since the last issued report, data
older than 24 h, checks that couldn't run. "Ready" means not yet issued and
nothing flagged. The review screen (`/reporting/<period>/<tenant>`) edits the
month's commentary, the tenant's "What we recommend next" items
(`report_actions`, kept across months, prefilled from
`getattr(result, "action_client", None) or result.action`) and notes on checks
(`finding_notes`, client-visible or staff-only). They reach the monthly review
template as `month_end` from `month_end.report_context()`, passed by the route
(`web/reporting.report_extras`), not the builder; staff-only notes only ever
reach staff on the web page, never a client or any PDF.

Approving (`freeze` then `deliver`) renders the plan's files once into
`issued_reports` (trigger `approval`, `approved_by`/`approved_at`), then emails
those exact bytes (`delivery.send_frozen`, which marks the schedule's period
done) or, for portal-only plans or no recipients, leaves them in the report
history. Approving an issued month again is a re-issue: it needs a reason and
stores the next version. Roles: admin and analyst review, comment, assign and
edit recommendations and notes; only admins edit plans and approve, send or
re-issue. Clients never see the queue, reviewers or staff-only notes. Every
change is audited; note text is never put in the audit log.

## Client tenant lifecycle

- `tenants.enabled = 0` is **paused** (no scheduled or manual collection, no
  report or alert emails, out of the digest; data stays); `archived_at` is
  **archived** (paused and hidden). `access.visible_tenants()` leaves archived
  tenants out and `tenant_or_404()` only lets admins open them, so lists,
  rollups and client pages need nothing extra; anything that selects tenants
  directly (worker, digest, schedules) must filter on both.
  `store.claim_next_run` never claims a paused tenant's run. Change state only
  through `lifecycle.py` (pause, resume, archive, restore, `delete_tenant`).
- A new table with a `tenant_id` gets `REFERENCES tenants(id) ON DELETE
  CASCADE`. `lifecycle.delete_tenant` deletes with foreign keys on and refuses
  (rolls back) if any table with a `tenant_id` still has rows for the tenant;
  only `audit_log` is kept on purpose (`lifecycle.KEPT`).
- Connection health (`tenant_health.assess`) is built from stored state only:
  the `connection` collector reads the app's own service principal, granted
  app roles and directory roles (GET only). Pages never call Graph for it;
  "Check connection now" queues that collector. Its fixes are plain English
  and say where in the Microsoft admin centres to click.
- Tenant admin routes are in `web/tenant_admin.py`: viewing health is admin
  and analyst, every change is admin-only and audited.

## Schema changes

Add `migrations/NNNN_description.sql`. Never edit an applied migration.
`db.migrate` re-reads `user_version` inside each migration's transaction (two
processes may start at once) and copies a database that already has a schema
to `backups/pre-upgrade-*.db` first (`backups.before_upgrade`); restoring that
copy is the only way back, since migrations only go forward.

## Commands

`pytest -n auto` (every core; plain `pytest` for debugging), `ruff check .`,
and `python -m officesentry <command>`:

| Command | What it does |
|---|---|
| `web` | the portal on Werkzeug's server, for development (Docker runs gunicorn) |
| `serve` | the portal on a production server without Docker: waitress on Windows, gunicorn elsewhere |
| `worker [--once]` | collections, the schedule, emailed reports and retention |
| `migrate` | apply database upgrades now (the portal and worker also do it on start) |
| `create-admin <name>` | create an admin from the shell (no two-step sign-in set up) |
| `setup-link` | print the one-time link for creating the first admin in the browser |
| `create-app` | create the read-only multi-tenant Entra app (device sign-in) |
| `rotate-keys [--new]` | re-encrypt stored secrets with the newest key, or new keys |
| `healthcheck web\|worker` | exit 0 if the portal answers or the worker checked in (Docker) |
| `backup [--dir] [--keep]` | a consistent copy of the database (keeps 7) |
| `restore FILE [--force]` | replace the database with a backup (refuses while the worker runs) |
| `delete-tenant <id>` | delete a tenant and everything stored for it |
| `live-test [--record FILE]` | collect from a test tenant (`OFFICESENTRY_TEST_*`), optionally re-record the fixture |

Docker: `docker compose up -d --build` (see DEPLOY.md).

Tests sign in with a password only (`require_mfa="off"` in `conftest.py`);
`tests/test_login_security.py` covers two-step sign-in. `make_user` stores a
cheap precomputed hash (scrypt costs ~0.1 s a time); the portal still uses
scrypt. A slow fixture that tests only read (`seeded`, `wrecked_once`, the
live replay) is module-scoped with its own database; keep tests that write
on the per-test `db`. Tests run in parallel in CI (`-n auto`), so never share
files outside `tmp_path`.
