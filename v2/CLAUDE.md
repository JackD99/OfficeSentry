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
  MSP's own mailbox, via a separate single-tenant app with `Mail.Send`, in the
  mail app's own cloud, `graph_cloud`). Never add mail permissions to the
  multi-tenant read-only app. `tests/test_no_writes.py` fails on any
  `.post/.put/.patch/.delete/.request(` outside its `ALLOWED` files, and
  checks a full collection only sends GETs (plus `Get-` cmdlets).
- **Every route needs `@login_required` or `@roles_required`.** Anything
  tenant-scoped loads the tenant with `access.tenant_or_404()`.
  `tests/test_access.py::test_every_page_requires_login` enforces the first;
  `tests/test_isolation_walk.py` opens every route that takes a tenant (or
  one of its objects) with another tenant's IDs and expects a refusal. A new
  URL argument fails there until it's listed in `TENANT_ARGS` (seed an object
  for it in the `world` fixture) or `OTHER_ARGS`.
- **Graph paths are relative to the version** (`"/users"`, `version="beta"`).
- **Microsoft hosts come from `clouds.py`** (commercial, GCC High, DoD,
  China). A tenant (`tenants.cloud`) only signs in with an app connection of
  the same cloud (`app_connections.cloud`, `worker.connection_cloud`); pass
  the cloud to `GraphClient`, `ExchangeClient`, `msauth` and
  `admin_consent_url`, and never spell out a Microsoft host elsewhere. Something
  Microsoft doesn't offer in a cloud goes in that cloud's `missing` (a
  collector key or `"<collector>.<part>"`): the worker warns instead of
  running it, and reports and checks show it as unavailable with that reason.
- **Credentials never touch disk as files** and are never sent to the browser.
- **Mock Graph with `responses`, never by replacing `GraphClient`.**
- **Sessions are server-side** (`web/sessions.py`). To sign someone out, bump
  `users.session_version` or delete their `sessions` rows; never trust anything
  in the cookie except the session token. New encrypted columns go in
  `ENCRYPTED_COLUMNS` in `__main__.py` so `rotate-keys` re-encrypts them
  (encrypted settings keys go in a module's `ENCRYPTED_KEYS`, gathered in
  `ENCRYPTED_SETTINGS`); name them `*_enc`, `test_key_rotation.py` checks.
- **The public demo** (`web/public_demo.py`, `deploy/public/`) signs every
  visitor in as the `demo` analyst and refuses anything but GET, HEAD and
  OPTIONS. It needs both `OFFICESENTRY_PUBLIC_DEMO=1` and the `public_demo`
  setting that only `scripts/demo_data.py --public` writes, and the visitor is
  never an admin; keep it that way. It runs on fictional data in its
  own container, network and memory-only folder, never with the portal's
  `.env`, data or secrets. A new POST route is refused there automatically;
  `tests/test_public_demo.py` opens every page as the visitor.
- **Audit with `db.audit()`**; it records the visitor's address (never on
  the public demo, where every visitor is a stranger). Repeated anonymous
  events go through `db.audit_counted()` (one row per address and hour), and
  never store what a stranger typed. Sign-in noise is in
  `db.AUDIT_NOISE` and pruned after 90 days. Admins read it at `/admin/activity`.
- **No import cycles.** `tests/test_imports.py` fails on a module-level cycle;
  put what two modules share in a third (as `checks/base.py` and
  `reports/registry.py` do) rather than importing late or at the bottom.
- **Every PDF goes through `reports/export.to_pdf()`**, which makes tagged
  PDF/UA files (`export.PDF_VARIANT`) and whose URL fetcher only
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
5. List every new Graph path or Exchange cmdlet in `endpoints.py` (version,
   permission, Microsoft's throttling limits and resource-unit cost,
   fallback). The shared limiter paces calls by it and run telemetry names
   them by it; `tests/test_ratelimit.py` fails on a call it doesn't list.
   Prefer v1.0; a beta entry needs `beta=` saying what v1.0 lacks
   (`tests/test_endpoints.py` checks).
6. Ask for the fields you read with `$select`. Without it Microsoft also
   returns fields it has retired and marks the whole call with `Deprecation`
   and `Sunset` headers (it did on the beta sign-in log and on drive item
   permissions until they were narrowed to `events.SIGNIN_FIELDS` and
   `sharepoint.PERMISSION_FIELDS`).

Exchange data: use `ctx.exchange.invoke("Get-...")` (created lazily). An
`ExchangeAccessDenied` fails the run with instructions to assign Global Reader.

The two SharePoint collectors list sites and OneDrives through
`collectors/sharepoint_lookup.Lookups`, both asking for the same fields
(`SITE_FIELDS`, `DRIVE_FIELDS`: everything either needs), so the second one
each night reuses the first one's site list and OneDrives, and people without
a OneDrive aren't asked about for a week. A field either collector starts
needing goes in those lists.

## Adding a check or report

- Checks live in the `checks/` package: `checks/base.py` is the framework
  (`@rule(...)`, `RULES`, `evaluate()`, `Finding`, `RuleResult`) and each other
  module is one area's checks, decorated `@rule(...)`. A new check goes in its
  area's module; a new area is a new module imported at the end of
  `checks/__init__.py` (checks are listed in import order). `rules.py` only keeps
  old imports working; import from `checks`. Checks read `TenantSnapshot` only,
  never Graph, and return `unavailable` (with the reason) when data or the
  tenant's licence is missing, rather than passing or failing. A check that
  can judge some objects but not others (a domain whose DNS lookup failed)
  returns their object ids as a fifth element (`RuleResult.keep`): the
  findings sync leaves those as they are. Several findings with one object id
  are stored as one, at the worst severity (`findings.by_key`).
- A check's title is its goal ("No inactive accounts"); its summary is what was
  found ("4 accounts inactive for 90+ days") and leads wherever a failing check
  is listed, with `RuleResult.action` (the recommendation's first instruction)
  under it. Count things with `text.plural(n, "account")` (the `plural` filter
  in templates), never "account(s)".
- A check that calls for work an MSP would sell goes in one project's
  `checks` in `projects.CATALOG` with a rough `Effort` in hours (each check
  in at most one; `test_projects.py` checks). A project is half a day or more
  (`projects.MIN_HOURS`, counting `OVERHEAD`); `projects.assess` turns the rest
  into quick fixes, and a small critical fix is always a quick fix so it isn't
  held for a sale. Failing checks in no project are quick fixes too.
  Recommended projects (`/projects`, per tenant `/tenants/<id>/projects` with
  its quick fixes, and the proposal PDF, which never shows estimates) and the
  month-end review's suggestions (`month_end.suggestions`: projects, then
  quick fixes) are worked out from the checks on every view, leaving out
  accepted risks; only the account manager's status and note are stored
  (`project_status`). Conditions no check covers (a missing licence) are
  `signals` on the project. Merging a project away needs a migration for its
  statuses and an entry in `projects.MERGED`.
- A detail report is a builder filling a `DataReport` with `Section`s of rows,
  declared once with `@report(key, title, description, collectors, group=,
  order=, keywords=, uses=)` (`reports/registry.py`). That one declaration gives
  it a page, a place in its menu group (`group` from `registry.GROUP_TITLES`,
  `order` spaced by 10; none shows under "More reports"), the report library
  and its search (`keywords`: words people type that aren't in the title or
  description), jobs in `library.USES` (`uses`), XLSX, CSV and PDF exports,
  a rollup, What changed and the schedule form. Builders live in a module per
  area (`data_reports.py`, `reports/sharepoint.py`, ...); a new module is
  imported in `reports/__init__.py`. Everything reads `data_reports.REPORTS`,
  `GROUPS` and `grouped()`, which the declarations build. A builder can set
  `tiles` (headline figures), `chart`, `notice` (something the admin must fix
  first) and use `Link` cells; the page and XLSX render them.
- Headline figures open the rows behind them: give a `Tile` or `Highlight`
  `rows=Rows("<table title>", {"<column>": value or [values]}, age=(col, "gt90"),
  flagged=True)`. A figure named exactly like a table links to it without that.
  `test_report_library.py` opens every seeded headline link.
- Tables are drawn by `templates/_report_table.html` from `reports/views.py`:
  value filters, date presets, hidden columns and sort all live in the query
  string (`f=`, `age=`, `hide=`, `sort=`, keyed by table and column slugs), and
  the page, PDF, XLSX and CSV apply the same view (`views.apply`). Pass lists
  for list cells (they become `Many`, so a filter offers each item). Saved
  views are per person, per report key (`saved_views` table, `web/saved_views.py`);
  save them with `saved_views.store`, which also updates the copies scheduled
  emails keep (see "Email delivery").
  A `Section` can also name its flag column (`flag_name`), group rows under
  collapsible headings by a column (`group`, anchors in `group_ids`), always
  offer a value filter on some columns (`filter_columns`), carry a key per row
  for row actions (`keys`, `{% call(key) report_table(tv, csv, action=...) %}`)
  and drop the "to review" count (`review=False`).
- The Findings tab (`/tenants/<id>/findings`, and `/findings/resolved`) is built
  by `reports/findings_report.py` from the `findings` table on that same table:
  open findings grouped by check, then accepted risks, with saved views (report
  keys `findings` and `findings-resolved`) and PDF, XLSX and CSV downloads. It
  isn't in `REPORTS`, since it isn't built from a snapshot.
- The Reports page (`/reports`) lists every declared report, with its
  `keywords` searchable. A job in `reports/library.USES` lists its reports most
  useful first; a report joining one later says so with `uses=`.
- Every detail report automatically gets a cross-tenant rollup (`reports/rollups.py`,
  `/across/<key>`) and is compared in the month-over-month change report
  (`reports/month_changes.py`, `/changes`, `/tenants/<id>/changes`). Rollup
  columns are the report's `tiles`/`highlights`, else a row count per table;
  `Rollup.total_row()` sums counts and money only (int or `Money` headlines,
  not scores or percentages). "Every row" (`/across/<key>/rows/<table slug>`,
  `rollups.all_rows`) stacks each visible tenant's rows from the same builders
  under a Client column, as one `DataReport`, so `views` filters, sorts, saved
  views and the CSV/XLSX/PDF downloads work unchanged; it keeps
  `rollups.MAX_ROWS` per table and says when it cut. `/across/checks`
  (`rollups.check_matrix`) is every check for every tenant, evaluated from
  stored data with accepted risks from `findings`. All
  `/across` pages are admin and analyst only, over `visible_tenants()`.
  `/changes` and `/across/checks` (and their downloads) read each tenant's
  stored row from `summary_cache` (`reports/precomputed.py`, kinds
  `changes:<days>` and `checks`) while its fingerprint matches, and build and
  store it otherwise. The worker builds them in a `precompute` task after the
  tenant's collection and once a day (every row again when retention removed
  something). If a row comes to depend on something else, add it to
  `Rows.fingerprint`; `tests/test_precomputed.py` checks stored rows equal the
  live build (`across(..., now=...)` and `check_matrix(..., now=...)` never
  use them). Rows are per tenant: only ever read them for `scoped_tenants()`.
  The change report matches table rows on their first column; set
  `Section(compare=False)` for event logs and time series. Columns named
  `Last ...`, `... (UTC)` or containing "change", and the day counts in
  `month_changes.DAY_COUNT_COLUMNS` ("Days", "Days left", ...), are ignored as
  time-driven; "No check-in for N days" in a cell is compared without its number.
  A section names its columns that drift every night with use (sizes, file and
  item counts) in `Section(drifts=(...))`, and those are ignored too.
- Downloads go through `reports/tables.py` (`Table`, `to_csv`, `to_xlsx`): CSV
  prefixes formula characters, XLSX never stores formulas (`safe_xlsx`).
- Report builders in `reports/` return plain data; templates and
  `reports/export.py` only format it. Charts are server-side SVG
  (`reports/charts.py`) so the PDF matches the page.
- A table's chart is declared on its `Section`: `chart=Chart("<column>",
  ("<series column>", ...), total=..., title=...)` (`data_reports.Chart`).
  `views.chart` draws it from the rows the view leaves, each bar linking to
  the table filtered to its value; downloads get it as `Section.figure`
  (`views.apply`/`figures`). Series colours come in a fixed order (tokens
  `--chart-series`, `--chart-2`, `--chart-3`, `charts.TONE_FILL`), three at
  most, with a legend. A `Tile` or `Highlight` takes
  `trend=metric_trend(snap, "<metric key>", now)` for a 90-day sparkline.
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
  Question sets (`reports/questionnaires.py`: cyber insurance, supplier
  questionnaire, Cyber Essentials by Danzell number) word those topics the
  way each form does, with text to paste; topics Microsoft 365 can't answer
  (`evidence_topics.py`, `gap()`; shared parts in `evidence_base.py`) carry a
  fill-in text with `[brackets]`.
  An override is per topic, so one written answer fills every set. A new set
  (or a framework pack) is a `QuestionSet` passed to `questionnaires.register()`,
  which refuses unknown topics; the page (`?set=`), PDF answer sheet, XLSX,
  CSV and text download pick it up.
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
  reports (detail, All clients, What changed) use `reports/detail.html`,
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
- The live canary (`.github/workflows/canary.yml`, `canary.py`) runs `live-test
  --canary` against the test tenant on a schedule, signing in with GitHub's
  OIDC token through a federated credential (no secret), and fails when a field
  the fixture has stops coming back or changes type, a call starts failing, or
  Microsoft sends a `Deprecation`/`Sunset` header the fixture lacks. It compares
  shapes only and never prints errors or check results (CI logs go public).
  Recordings keep those headers, so re-recording acknowledges them.
- `tests/nightly` (`pytest tests/nightly --nightly`, `.github/workflows/nightly.yml`,
  weekly while private): every release tag's demo database upgraded and crawled,
  the example PDFs against page baselines (`--update` refreshes them), and
  page time budgets at 200 tenants. Both scheduled workflows keep one GitHub
  issue open while failing (`.github/scripts/scheduled_issue.sh`); see its README.

## Robustness contract

`tests/test_robustness.py` fuzzes every check in `checks.RULES`, every report in
`data_reports.REPORTS` and every `changes.*_changes` feed against degraded
snapshots (a field removed or null, a list where text is expected and the other
way round, timestamps of `""`, `"0001-01-01T00:00:00Z"`, `"not-a-date"` or
without their `Z`, no objects at all). The registries are read when the tests
run, so new checks and reports are fuzzed as soon as they exist.

- Missing fields, nulls, odd timestamps and empty collections happen with real
  Microsoft data, so every check, report and change feed handles them itself.
- Wrong types can't come from Graph; for those it's enough that the guards
  contain the crash: `checks.evaluate()` shows the check as "couldn't run",
  `data_reports.build()` shows the report as unavailable and
  `changes.all_changes()` leaves out that feed. Nothing returns a malformed result.
- Use the shared helpers: `devices.parse_ts` / `devices.parse_day` for any
  timestamp or date, `coverage.policy_name` for a policy with no name.

## One page per account

The `reports/profile/` package builds `/tenants/<id>/users/<object id>`
(`page.build()`, and the Users tab, `directory()`) from stored snapshots. Each
area is its own module with a function decorated `@part(order)` (`base.py`)
returning a `Part` (facts, `Section` tables, a short summary); call
`person.flag(severity, text, part_key)` for "Needs attention". Orders are
spaced by 10, so a new area slots in without touching the others; import its
module in `profile/__init__.py`. `reports/user_profile.py` only keeps old
imports working.
`history()` diffs the account across retained snapshots. Detail report cells
that are exactly an account's sign-in name or address become `UserLink`s in
`data_reports.build()` (a `str` with `page_href`), so builders don't need to do
anything to link people; PDFs and spreadsheets show the plain name.

Staff search every visible tenant's latest snapshot from the sidebar box
(`web/search.py`, `/search`, admin and analyst only); to make something
searchable, add a `Kind(collector, kind, values, show)` to `search.KINDS`, with
`show` linking the person's page or the detail report filtered to the row.

## One page per mailbox, group or site

`reports/object_page.py` is the same pattern for things that aren't people: an `ObjectPage` drawn by
`templates/object_page.html` for every kind, a `Subject` (the snapshot, the object, `flag()`), areas as
`@part(kind, order)` functions returning a `Part`, `findings_for()`, `sources_for()` and `object_path()`.
Each kind is one module (`reports/mailbox_page.py`: `/tenants/<id>/mailboxes/<object id>` and the
Mailboxes list, routes in `web/mailboxes.py`), imported in `reports/__init__.py`, with its list in the Users
tab's second row. A module registers how its objects are named in reports in `data_reports.PAGE_INDEXES`
(lower-cased name -> page path); `data_reports.build()` turns matching plain cells into `PageLink`s after
people are linked, so an address that is an account's still opens the person's page. Pages read stored
snapshots only (`store.object_copies` for history) and link back to the reports the facts came from,
filtered to the object (`Subject.report()`).

## Home pages by role

`home.py` (pages in `web/home.py`, staff only, `@group_aware` over `scoped_tenants()`): **Today**
(`/today`, for technicians: critical and high findings new in the last day, failed or out-of-date
collections, access not granted yet, and Coming up items due within a week) and **Accounts** (`/clients`, for
account managers: licence waste from the cached rollup rows, renewals in 30 days, this month's month-end
reports to approve and projects to propose). Each opens with one sentence (`Today.sentence()`,
`Clients.sentence()`), then the detail, from stored state only. A person's choice is `users.home` (Account
settings, or "Make this my home page" on either page); `/home` (the sidebar's Home and the logo) opens it,
and signing in without a page to go back to lands there. Client users keep the dashboard. A new reason on Today
is a `Reason` with a `kind` in `home.KINDS` and a `target` that `web/home._link` turns into a URL.

## Web UI conventions

- Colours, type sizes (`--fs-*`), spacing (`--space-*`, a 4px grid), radii and
  shadows are tokens in `web/static/tokens.css`, light and dark, shared by
  `app.css` (the portal) and `report.css` (reports that also become PDFs, which
  set `<html data-theme="light">`). Neither file defines a colour of its own;
  add a token instead (`test_styleguide.py` checks). Text colours
  (`--text`, `--text-2`, `--muted`, `--faint`, `--sev-*`) meet WCAG AA (4.5:1)
  on every surface, and `test_ux_fixes.py` measures them; use `--decor` for
  lines and icons only, never for text. Don't tell things apart by colour alone
  (the check matrix squares carry a letter).
- `/styleguide` (staff) shows every token and shared component, and
  `/styleguide/report` (and `.pdf`) the client report components
  (`web/styleguide.py`). A new shared component or state goes there too.
- `tests/browser` (`pytest tests/browser --ui`, in the pinned Playwright image;
  see its README) screenshots about 34 pages at laptop and phone widths, light
  and dark, against baselines, and runs axe-core. CI runs it
  (`.github/workflows/ui.yml`) on PRs that touch templates or CSS; refresh the
  baselines with `--update` when a change is meant, and look at them.
- Admin settings are one sidebar entry. A new settings page goes in `SETTINGS`
  at the top of `base.html` (endpoint, label, other endpoints of the page),
  which gives it the row of settings links at the top of the page.
- Every page loads `static/app.js` (PDF links show "Preparing your PDF…" until
  the file arrives) and `static/tables.js` (sorting, row search, in-place
  views, and stacked tables capped at 20 rows with "Show all" on phones). A
  page that also loads `tables.js` itself still gets one copy.
- Downloads on a page sit in one `downloads([(label, href, title)])` control
  from `_macros.html`, PDF first.
- Every tenant page starts with `tenant_header(tenant, '<tab>')` from
  `_macros.html` (import it `with context`). There are seven tabs (Overview,
  Findings, Changes, Reports, Users, Emails, Settings); a page that belongs
  under one gets its own key, mapped to that tab in `TAB_OF`, and a link in
  the tab's second row (`.tab-pages`), rather than a new tab. On laptops its tab bar stays pinned to the
  top as the page scrolls (`data-pin`, `app.js`: CSS `position: sticky` with a negative top, so nothing
  changes height); pass `pin=false` for a sample header. The Reports tab has no second row: its pages
  (Monthly review, Questionnaires and evidence, every detail report from `library.catalogue()`, and
  Custom reports for staff) are in `report_picker()`, a searchable list beside the report on wide screens
  and a select on narrower ones;
  a page that uses it wraps itself in `.report-layout` with the report in `.report-main`.
- Show times with the `ts()` macro or the `ago`/`when`/`day` filters, never raw
  ISO strings. Licence flags go through the `capability` filter. Python code
  outside `web/` (reports, emails) uses the same functions from
  `formatting.py` (`money`, `when`, `day`, `capability`); reports and checks
  never import `web` (`tests/test_imports.py`).
- Tables that people read on phones get `class="stack-sm"` and a
  `data-label` on every cell after the first.
- Scripts need `nonce="{{ csp_nonce }}"`; there are no external assets.
- The root README's screenshots (`docs/screenshots/`) and `examples/` come from
  `python scripts/screenshots.py` (demo data from `scripts/demo_data.py`); re-run
  it when the UI or reports change, and look at the images before committing.
  The website's copies are retaken on every deploy (website/README.md); a new
  page shown there needs a `Shot` in `SHOTS`. `tests/test_demo_samples.py`
  checks the demo's reports add up.

## Background work

`worker.py` is the only background process (run one): `cfg.worker_concurrency`
collector threads claim queued runs (`store.claim_next_run`: highest
`runs.priority` first, then oldest, and never two runs for one tenant at once).
`execute_run` syncs findings (and alerts) once the tenant has nothing else
queued or running, so a night's collection is evaluated once. Token providers
(`msauth.shared_certificate_token_provider`) and each thread's HTTP session are
reused across runs. Meanwhile the main thread, once a
minute, queues the collection slot (`schedule.py`, cron + timezone, skipping
collectors fresher than `max_age_hours`) and queues due report emails (which
first refresh stale collectors) as tasks. Every thread opens its own SQLite connection;
never share one across threads. Manual runs and report refreshes get
`store.PRIORITY_WAITING`. Once a day `retention.apply` prunes under
the admin's *Data retention* settings (`retention.*` keys in `settings`;
unset means today's defaults): snapshots with `store.prune_snapshots` (latest
run per day for 30 days and per month for 12 by default; metrics and run rows
are never pruned), sign-in detail objects (`retention.SIGNIN_KINDS`) once
older than their period, resolved findings, run log info lines older than 90
days (`store.prune_run_logs`; warnings and errors stay), and, when set, the
activity log and issued reports (each never under 365 days); the activity history
too (`retention.prune_history`, see "Activity history"). So code that
reads an older run must cope with its objects, or just its sign-in kinds,
being gone (see `changes._baseline_run`). A new store of personal data gets
a period there, with a dry-run count for the page, and deletes in batches. A side thread records the heartbeat that the portal banner and
`healthcheck worker` read, but only while the worker makes progress
(`worker.Liveness`: the main loop came round in the last 10 minutes, no run
or the daily pass has gone for 4 hours and no task for 1), so a wedged loop or thread shows as down. The
daily pass (`worker._daily`: retention, every dashboard row, the precompute tasks) runs in its own thread
(`_daily_thread`), since on a large install it takes longer than the main loop may be away. A failing
housekeeping pass (say "database is locked") is logged, rolled back and
retried the next minute; it never ends the worker. A collector thread that finds the database locked when
it records a finished run tries again for a few minutes (`_while_locked`) rather than leave the run to be
failed as "worker stopped" once its lease runs out. `db.transaction` (and
`store.atomic`) begin with `BEGIN IMMEDIATE`, so a write that reads first waits
for other writers instead of failing at once; pass `immediate=False` only for
a read. The portal's connections wait `web.WEB_BUSY_TIMEOUT_MS` (5 seconds) for
the lock rather than the worker's 30, and a write that still finds it held gets
the "busy right now" page (503 with `Retry-After`), never a 500. Bookkeeping a
page can do without once (noting a session was seen, storing a dashboard row it
just built) goes through `db.try_write`, which waits a quarter of a second and
skips. Don't add Celery or Redis.

Office Sentry's own health is `system_health.assess()` (database, schema
version, worker heartbeat, last collection and whether most tenants failed
the last scheduled slot, backups from `<data dir>/backup-status.json`
(`backups.record_status`; `local` from the `backup` command, `offsite` and
`restore_check` from the deployment's restic jobs, in one shared format;
nightly copies overdue after 26 hours, the restore check after 8 days), disk
and certificates, from stored state only. Each check is `ok`, `warn` or
`fail`; `/health/ready` answers 503 on any `fail` and shows
only `{"ok": ...}` unless the caller has the monitoring token (hash in
`settings`, made on `/admin/status`) or is a signed-in admin, and never names
a tenant or carries Microsoft's error text. `system_alerts.py` turns the same
checks into emails to the team contacts and webhook posts (`alerts.system`):
the worker checks backup, disk and collection as the `system_alerts`
periodic job, and since a stopped worker can't say so, each web process runs
a watchdog thread (started on its first request, not under `cfg.testing`) for
the worker. Alert state is a `system_alert.<key>` settings row changed in a
write transaction before sending, so two processes never both send. Tests
see a 40%-full disk (`conftest._roomy_disk`).

Microsoft's limits are per app, not per thread, so every Graph and Exchange
call in the worker goes through one process-wide limiter
(`ratelimit.LIMITER`, passed by `worker.default_graph_factory` and
`default_exchange_factory`): token buckets per limit in `endpoints.LIMITS`
(app-wide, per tenant, or per report API, some counted in resource units),
and a 429 pauses that limit's bucket for every thread until `Retry-After`.
Clients made elsewhere (tests, connection check, live test) aren't paced.
Each run's requests are counted (`ratelimit.Telemetry`: calls, 429s,
failures, retry and limiter waits, `x-ms-throttle-limit-percentage`): totals
on `runs` (`requests`, `throttled`, `wait_seconds`; NULL before this existed)
and per endpoint in `run_requests`, pruned with run log info lines after 90
days. The run page shows both. Requests carry the `User-Agent`
`ISV|OfficeSentry|OfficeSentry/<version>` Microsoft prioritises.

Long work goes through **background tasks** (`tasks.py`, the `tasks` table),
never a web request or the main loop: `worker.TASK_THREADS` task threads
claim tasks (highest priority first) and run the function registered for
their kind with `@tasks.handler(kind)` (add its module to
`worker.TASK_MODULES`). Kinds today: `approve` (`month_end.py`),
`send_schedule` (`delivery.py`), `consent_check` (`onboarding.py`), `erase` (`lifecycle.py`) and
`precompute` (`reports/precomputed.py`, below other work). Queue with `tasks.enqueue(conn, kind,
key=..., payload=...)`: at most one task per `key` is queued or running, so
use a key that names the thing done once ("approve:<tenant>:<period>").
Claimed runs and tasks carry a lease (`store.LEASE`) that the heartbeat
thread renews; when a worker dies, `expire_leases` fails its runs and queues
its tasks again (up to `tasks.MAX_ATTEMPTS`). So a handler may run twice:
save `ctx.checkpoint(...)` in the same transaction as any step that mustn't
repeat and skip it when `ctx.done` says so. Raise `tasks.TaskFailed` with
words for people; a handler's own failure isn't retried. Pages show
progress from the rows (`tasks.batch_progress`, a JSON route polled by
`_progress.html`). Finished tasks are pruned after 30 days. Table report
PDFs keep the first `printing.PDF_MAX_ROWS` (2,000) rows per table and say
so (`printing.cap_rows`); the XLSX and CSV have every row.

The dashboard and the All clients review read each tenant's row from
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
show up in the schedule form automatically. A schedule can also send saved views
(`email_schedule_views`, `delivery.ScheduledView`): a copy of the view's name and
query with a format (PDF, XLSX, or CSV with one file per table the view shapes,
`delivery.view_plan`), kept in step through `saved_views.store` and kept when the
saved view is deleted. The form only offers the signed-in person's own views (or
copies the schedule already has). Report schedules attach them after the reports
and archive them as kind `view:<report key>`; a digest whose `reports` is
`["views"]` (`delivery.is_views`, `build_views_email`, `/emails/views/new`, admin)
stacks every active tenant's rows, or one group's, with `rollups.all_rows` and goes
to the team only. `delivery.digest_builder` picks a digest's builder. When a
tenant's report email goes out (`send_schedule`, after its commit) or a month-end
month is sent or published (`month_end.deliver`), `delivery.report_ready` posts
`alerts.report_ready` if the admin ticked `alerts.on_reports`: a link to the
report history and the file names, never the files. Email templates live in
`web/templates/emails/` and use inline styles and tables only (email clients
ignore stylesheets). Clients never see team contacts, hidden copies or errors.
An email names the period of its schedule's latest send time
(`delivery.email_period`, `period_label`), never the clock's, so a retry after
midnight or a Retry next week still says, and carries the commentary of, the
month it was for; a month-end send passes its `period`. A report schedule
never emails a tenant nothing has been collected for (`nothing_collected`: the
send fails with the reason and retries), and Send now and Retry refuse while
the timer's `send_schedule` task for the period is queued or running
(`being_sent`), so nobody sends a period twice.

**Coming up** (`upcoming.py`, page `/upcoming` in `web/upcoming.py`, admin and analyst) lists what falls
due in the next 90 days across the visible, active tenants: subscription renewals and trials (lapsing ones
need attention now), client app secrets and certificates (and expired in the last 30 days), mailboxes at
90% of their send limit or forecast to fill from a collection two to eight weeks old, DMARC still at
`p=none` (no date) and accepted risks whose review date comes up. Office Sentry's own certificates are
added for admins and the email only. `upcoming.collect()` returns `Item`s sorted into windows (Now, 30,
60, 90, No date); `upcoming.report()` puts them on the shared report table, so filters and downloads
work. A part that fails on one tenant's data is left out for that tenant and named on the page. The
weekly email is a digest schedule whose `reports` is `["upcoming"]` (`delivery.is_upcoming`,
`build_upcoming_email`, `emails/upcoming.html`), so it uses the digest's sender, retries and history and
goes to the team only. A new kind of item is a function in `upcoming.PARTS` with a test in
`tests/test_upcoming.py`.

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
reach staff on the web page, never a client or any PDF. A client's live report shows none
of it until the month is approved or sent (`report_context(..., approved_only=True)`).

Approving queues an `approve` task per client and month
(`month_end.queue_approval`; bulk approval queues one per ticked client under
one `batch`, and the queue page shows its progress). The task renders the
plan's files outside the write lock (`render_files`), then in one
transaction re-checks the state, stores them in `issued_reports` (`freeze`:
trigger `approval`, `approved_by`/`approved_at`) and checkpoints "frozen";
then emails those exact bytes (`deliver` and `delivery.send_frozen`, which
record the email, mark the schedule's period done and the month Sent in one
transaction) or, for portal-only plans, leaves them in the report history. An
email plan with nobody to send to keeps them in the report history too, but
the month stays Approved with the reason (`report_reviews.error`), flagged
"Nobody to email" in the queue, until Send again reaches someone. A plan
email goes to the client's first `mailer.MAX_RECIPIENTS` contacts plus the
team's hidden copy (`_plan_recipients`); `coverage()` names anyone left off. A worker that dies between freeze and send sends once on
retry, without freezing again (`tests/test_tasks.py`). Approving an issued month again is a re-issue: it needs a reason and
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
  only `audit_log` is kept on purpose (`lifecycle.KEPT`). The request only
  deletes (with `secure_delete` on); the `erase` task (`lifecycle.erase`) then
  removes the tenant's activity history, writes the WAL back and VACUUMs when
  there's room. `erase.state` in `settings` tracks a delete still to erase; a
  failure is the `erase` health check and system alert, and is retried hourly.
- Connection health (`tenant_health.assess`) is built from stored state only:
  the `connection` collector reads the app's own service principal, granted
  app roles and directory roles (GET only). Pages never call Graph for it;
  "Check connection now" queues that collector. Its fixes are plain English
  and say where in the Microsoft admin centres to click.
- Anything the tenant granted the app beyond `entra.GRAPH_PERMISSIONS` and
  `EXCHANGE_PERMISSIONS` (the collector's `other`), and any directory role
  besides Global Reader, is a warning (`tenant_health.extra_permissions`,
  `extra_roles`) on Manage and the Client settings list.
- App certificates (`cert_lifecycle.py`): `expiring()` grades every
  certificate in use, each app connection's and the mail app's, at 60, 30
  and 7 days and expired, for the App connection page, the admin banner in
  `base.html` and the `certificate_expiry` periodic job (one email to the
  team contacts and one webhook alert per threshold). Replacing one keeps
  two on the connection: `start` (the `next_*` columns), the admin uploads
  the new .cer next to the old, `test` signs in with it, `switch` swaps it
  in and remembers `old_thumbprint` until `forget_old`. `switch` refuses
  until a test worked. The runbook is in the root `SECURITY.md`.
- Creating the app (`app_setup.py`): the App connection page's wizard starts
  Microsoft's device-code sign-in (`entra.start_device_sign_in`) and the page
  checks back every few seconds (`app_setup.check`, one token request per
  Microsoft interval, claimed with a conditional `UPDATE` because the next
  check may reach another portal process). The access token lives only in
  the request that receives it and creates the app; only the device code is
  stored, encrypted (`setup_sign_ins.device_code_enc`), until it's used,
  fails or is cancelled. The wizard and `create-app` both finish with
  `register_app`, which asks for exactly `entra.setup_scope` and reuses a
  connection still waiting for its client ID in that cloud.
- Tenant admin routes are in `web/tenant_admin.py`: viewing health is admin
  and analyst, every change is admin-only and audited.
- Onboarding many clients (`onboarding.py`, admin pages `/admin/onboarding*`
  in `web/onboarding.py`, table `onboarding` from `migrations/0034`): CSV
  import (`parse` previews, `create` adds in one transaction, groups created
  as needed), each tenant's state (`onboarding.state`, from stored data only),
  the client link, email text and consent pack PDF. A **client link**
  (`web/onboarding.client_link`, state signed with salt `client-consent`,
  naming the tenant's id and `created_at`) differs from an admin's own consent
  link: it never expires and comes back to `/consent/callback` without a
  sign-in. That route is the one exception to "every route needs a
  decorator": it hands a client state to the public `client_returned` page and
  everything else to `_consent_return` (`@roles_required("admin")`). The
  public page never records consent and never shows Microsoft's text; it
  notes `approved_at` and queues a `consent_check` task. Only
  `onboarding.confirm` (signing in as the app, `connection_check.check`)
  sets `consented_at` for a client link; the `consent_checks` periodic job
  re-checks tenants still waiting (`onboarding.due`). No Partner Center calls.

## Report designs

`reports/designs.py` holds the widgets (`WIDGETS`: key, title, group, `Field`s for
the editor's panel, a `build(ctx, settings)` returning plain data or `{"empty":
why}`) and the document rules (`clean`: unknown kinds dropped, every setting
coerced to its field, half width only where `halfable`). A design row
(`report_designs`, migration 0032) is `settings` (cover) and `blocks` JSON;
`document(row)` always returns a cleaned document. `Context` builds the monthly
review, its summary, the evidence pack and detail reports once each for a
tenant; `render` builds every block and catches a widget's exception, so a
broken block says so and never takes the report down (`test_report_designs.py`
checks). `templates/reports/_widgets.html` has one macro per kind and draws the
page, the preview and the PDF from the same blocks. Routes are in
`web/designs.py` (admin and analyst; delete is admin): the editor autosaves the
JSON with fetch, `/reporting/designs/<id>/preview?tenant=` is the preview pane,
and `/tenants/<id>/designs/<id>` (`.pdf`) is a tenant's copy. A design is a
report key `design:<id>` in `delivery.catalog(conn)` (PDF only), so plans and
schedules send it through `_attachments`; `designs.remove` strips the key from
them. Adding a widget: a `Widget` in `WIDGETS` and a `w_<kind>` macro in
`DRAW`; the editor reads the palette from `designs.palette()`.

## Tenant groups

`tenant_groups.py` (schema in `migrations/0029`, admin page `/admin/groups` in
`web/groups.py`). A group is a name, an optional category (Tier, Region, ...)
and its tenants (`tenant_group_members`); a tenant is in any number. Groups
only ever narrow lists of tenants someone already sees (`tenant_groups.only`
over `visible_tenants()`). The one grant is an admin ticking a group for an
analyst (`user_tenant_groups`): `access._GRANTED` adds its tenants, now and
later, and ignores it for every other role. Never read group grants anywhere
else.

Staff pages that list tenants are `@group_aware` (under `@roles_required`)
and read `scoped_tenants()` instead of `visible_tenants()`: the dashboard,
`/across/*` (and their downloads), `/changes*`, the month-end queue and
Client settings. The choice is kept on the sign-in (`sessions.group_id`); a GET
with `?group=<id>` or `?group=all` picks and redirects to the plain URL. The
picker (`_groups.html`, `group_picker()`) only offers groups holding a
tenant the person sees, and downloads say which group they cover
(`scope().label`). A digest covers one group when `email_schedules.group_id`
is set (`delivery.digest_data(..., group_id)`); deleting the group sets it
back to every tenant.

## Snapshot storage

Collected objects are stored compressed (`store.py`, "Snapshot data"): an
`objects` row whose `data` is a BLOB is a pack of up to `store.PACK_BYTES` of
one run and kind's objects in id order, keyed by its first object's id, with
`items` saying how many it holds. Rows from before packing hold one object as
JSON text (`items` 1); `packing.py` converts them a few seconds a minute from
the worker (the `compact` command does it all and shrinks the file). So:

- Read objects through `store` (`run_objects`, `find_object`,
  `object_copies`, `matching_objects`) or `TenantSnapshot`, never
  `objects.data` or `json_extract` in SQL.
- Count objects with `SUM(items)`, not `COUNT(*)`. Deleting or checking rows
  by run and kind (`EXISTS`, retention) works as before.
- Tests that edit stored rows with SQL call `tests/stored.unpack(db)` first;
  `test_compressed_storage.py` checks every report, check, change feed and
  account page is identical from packed and unpacked rows.
- `scripts/scale_data.py` builds N tenants with two years of history for
  sizing and timing (`--plain --compact` measures before and after,
  `--history` the daily history below).

## Daily history (state history)

`state_history.py` (schema in `migrations/0036`) stores each collected object
once per version instead of a copy per run: `state_versions` rows (per
`state_series`: tenant, collector, kind) say from which recorded run an
object's content held and until which (`to_run`, NULL while current), and the
content is a line in a `state_packs` pack of what changed in that run. Fields
that change nearly every night (`VOLATILE`: last sign-ins, mailbox sizes,
usage dates) stay out of the versions, in `state_volatile` rows of only what
moved, with a full row every `KEYFRAME_ROWS`. Sign-in detail
(`retention.SIGNIN_KINDS`) isn't recorded; reading falls back to the snapshot
for it and for runs not recorded yet.

It runs in **shadow mode**: collections still write `objects` and every page
reads that. `worker._run_collector` records each successful run as it
finishes (`record_finished`, never failing the run) once its data source's
earlier runs are, and the `state_history` periodic job records the rest
oldest first a few seconds a minute (`catch_up`, resumable from a watermark),
which backfills the snapshots kept from before the upgrade. Both pause while
the disk is over `system_health.DISK_WARN` full. Every recorded run is read
back and compared with its snapshot (`verify_run`); a difference is logged and
counted in `status()`. So:

- A data source's runs are recorded in run ID order; `record_run` refuses an
  older one. Rebuild a run with `state_history.run_objects`, `find_object`,
  `object_copies` and `matching_objects`, which answer like `store`'s.
- A new kind with a field that changes every night: add it to `VOLATILE`.
- `test_state_history.py` swaps `store`'s readers for these and checks every
  report, check, change feed, account page and cross-tenant page is identical.
- Retention: `retention.state_months` (Daily history, 25 months by default)
  via `state_history.prune`, which always keeps each data source's latest run
  and rewrites the first kept run's fast-changing fields as a full row.
- `python -m officesentry history` shows progress; `--dry-run` estimates the
  room and time the backfill needs without writing, `--backfill` does it now
  and `--verify` checks every run that still has its snapshot.

## Activity history

`events.py` (schema in `migrations/0030`) keeps every sign-in and directory audit event beyond the 30 days
Microsoft keeps them, in monthly SQLite files beside the database: `<data dir>/events/<store>/<YYYY-MM>.db`,
stores `signins`, `daily` (one row per person, day, app and country) and `audits` (Graph's JSON,
compressed). Rows carry the tenant's key from `event_tenants` (never reused), not `tenants.id`, and are
deduplicated on (tenant, time, id), so reading an event twice stores it once.

- Collect with `events.ingest(ctx, events.SIGNIN_LOG | AUDIT_LOG, keep_days, budget)`. Each tenant's
  cursor (`event_cursors`, `events.Coverage`) says what's been read: each run adds `[read_to - 6 hours,
  now]` and reads the gaps newest first, saving the cursor after every page, so a crash, a timeout (one-day
  slices, then the 10-minute give-up) or the page budget loses nothing and the next run carries on. Ranges
  Microsoft dropped before they were read go in `lost`. Warn with what `coverage.missing()` returns rather
  than truncating quietly. `tests/test_events.py` replays overlapping and crashing runs.
- Read through `events` (`signins_between`, `audits_between`, `daily_between`, `daily_for`, `totals`),
  never the files. Pages that read it say where it starts ("detailed from <date>",
  `Coverage.detailed_from`). Readers today: the Sign-ins, legacy sign-in and Admin activity collectors, the
  Admin activity report (12 months), `changes.audit_changes`, each person's page and the tenant's Activity
  history page (`/tenants/<id>/activity`, staff only, under the Changes tab).
- Retention: `retention.Policy.history_*` (D18 defaults: sign-ins 180 days, daily summaries 25 months,
  audits 7 years); whole months go as files (`events.prune`). `lifecycle.delete_tenant` deletes the
  tenant's rows from every file after the database delete, and the daily pass sweeps rows whose key is
  gone (`events.sweep`). `backup` mirrors the folder (`backups.mirror_events`) and `restore` puts back
  missing files.
- Tests that need a fixed "now" patch `events.clock` (the live replay sets it to the recording's time).

## Schema changes

Add `migrations/NNNN_description.sql`. Never edit an applied migration.
`db.migrate` re-reads `user_version` inside each migration's transaction (two
processes may start at once) and, before taking the write lock, copies a
database that already has a schema to `backups/pre-upgrade-*-from-<release>.db`
(`backups.before_upgrade`); restoring that copy is the only way back, since
migrations only go forward. Copies check free disk first. Backups and
pre-restore copies are gzipped after SQLite's backup API (`restore` takes
`.db.gz` and plain `.db`); pre-upgrade copies stay plain, since an older
release (whose `restore` may not read gzip) restores them. Code
older than the database refuses to start (`db.DatabaseTooNew`, naming the copy
to restore), and two files with one number stop the upgrade, since the second
would otherwise be skipped for good. Several branches add migrations at once:
take the next free number above `main`'s highest when you merge `main` in
(`tests/test_migrations.py` fails on a clash).

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
| `create-app` | create the read-only multi-tenant Entra app (device sign-in); the App connection page's wizard does the same |
| `rotate-keys [--new]` | re-encrypt stored secrets with the newest key, or new keys |
| `healthcheck web\|worker` | exit 0 if the portal answers or the worker checked in (Docker) |
| `backup [--dir] [--keep]` | a consistent, compressed copy of the database (keeps 7) |
| `backup-status [--json]` | what each backup job last did (`backup-status.json`); `--record` is for `deploy/jobs.sh` |
| `check-backup FILE [--record]` | check a backup opens and passes SQLite's integrity check, without restoring it (the restore drill) |
| `restore FILE [--force]` | replace the database with a backup (refuses while the worker runs) |
| `compact` | compress snapshots stored before compression now and shrink the database file |
| `history [--dry-run\|--backfill\|--verify]` | the daily history: progress, an estimate of the backfill without writing, the backfill now, or a check that it reads back as the snapshots |
| `delete-tenant <id>` | delete a tenant and everything stored for it |
| `live-test [--record FILE] [--canary REPORT]` | collect from a test tenant (`OFFICESENTRY_TEST_*`), optionally re-record the fixture or compare the answers' shape with it (the live canary) |

Keys: `secrets.json` lives at `OFFICESENTRY_SECRETS_FILE` when set (Docker: `/keys/secrets.json`, its own
volume), else in the data folder; `config.move_secrets` moves one left in the data folder on start. Never
put it, or anything that decrypts, in the data volume or a backup. The server's own jobs (nightly backup,
restic off-site copy, Sunday restore drill, uptime ping) are `deploy/jobs.sh`, scheduled by every deploy;
restic's settings are in `.env.backup`, read only by the `offsite` service, never by the portal.

Docker: `docker-compose.yml` runs the published image
(`ghcr.io/jackd99/officesentry:${OFFICESENTRY_VERSION}`); add
`docker-compose.build.yml` to build this folder instead (`-f docker-compose.yml
-f docker-compose.build.yml up -d --build`, or `COMPOSE_FILE` in `.env`, which
is how the Hetzner deploy builds). See DEPLOY.md.

Tests sign in with a password only (`require_mfa="off"` in `conftest.py`);
`tests/test_login_security.py` covers two-step sign-in. `make_user` stores a
cheap precomputed hash (scrypt costs ~0.1 s a time); the portal still uses
scrypt. A slow fixture that tests only read (`seeded`, `wrecked_once`, the
live replay) is module-scoped with its own database; keep tests that write
on the per-test `db`. Tests run in parallel in CI (`-n auto`), so never share
files outside `tmp_path`.

## Releases and dependencies

- The version is `officesentry.__version__` (pyproject reads it). It shows at
  the foot of the menu for signed-in people and in `/health`, and `db.migrate`
  records it in `settings` (`app.version`) so pre-upgrade copies are named
  `...-from-<release>.db` and `DatabaseTooNew` names the release that upgraded.
- The image and CI install from hash-pinned lock files made with pip-compile
  (`requirements.txt` for the image and pip-audit, `requirements-dev.txt` for
  CI). A new dependency goes in `pyproject.toml` and both locks
  (CONTRIBUTING.md, "Dependencies"); `tests/test_release.py` and `pip check`
  fail otherwise. Base images are pinned by digest.
- `.github/workflows/release.yml` publishes a release (Run workflow with a
  version, or a `v*` tag): the version must match `__version__` and have a
  `CHANGELOG.md` section (`scripts/release_notes.py`) and the commit needs a
  passing v2 run on `main` (reused, not repeated, to save Actions minutes);
  then an amd64 image with SBOM and provenance (arm64 and signed attestations
  switch on by themselves once the repository is public),
  a start-up check, a vulnerability scan, version tags, the GitHub release,
  and `scripts/prune_images.py` keeps the last three releases' images. Changes people running it must act on go in
  the changelog's next section under **Action required**, **New Microsoft
  permissions** or **Database upgrade**.
