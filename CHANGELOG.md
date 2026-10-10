# Changelog

What changed in each Office Sentry release, written for the people who run it. Read the
**Action required**, **New Microsoft permissions** and **Database upgrade** parts before updating.

How to update: [v2/DEPLOY.md, "Updating"](v2/DEPLOY.md#updating). How releases are made:
[CONTRIBUTING.md, "Releases"](CONTRIBUTING.md#releases).

## 2.1.0 (unreleased)

### Action required

- **Servers deployed with the Deploy workflow that ran the demo site** (`DEMO_ENABLED`): the password-protected
  demo is replaced by a public, read-only one that resets every night, meant for the project's own server
  ([v2/deploy/public/README.md](v2/deploy/public/README.md)). Delete the `DEMO_PASSWORD` secret. To keep a
  demo, follow that README (it opens port 443 to everyone, so the portal's allow-list moves to the
  `PORTAL_ALLOWED_IPS` secret); otherwise set `DEMO_ENABLED` to `false`. The old demo's data can go with
  `docker volume rm v2_officesentry-demo-data` in a shell.
- **Keys move out of the data volume.** `secrets.json` now lives in its own volume, `officesentry-keys`,
  so backups of the data never hold the key that decrypts them. Download the new `docker-compose.yml`
  with this release; the portal moves the file on its first start and logs **Moved the key file**. Then
  save a copy of `/keys/secrets.json` in your password manager ([v2/DEPLOY.md, "Where the keys
  are"](v2/DEPLOY.md#where-the-keys-are)). Going back to an older release afterwards: copy it back to
  `/data` first (v2/DEPLOY.md, "Rolling back an update").
- Activity history lives in a new folder beside the database, `/data/events` (monthly files of sign-ins and
  directory audit events). The `backup` command copies it into an `events` folder next to the database
  copies, and `restore` puts back any file that's missing. If you back up the data volume some other way,
  include `/data/events` ([v2/DEPLOY.md, "Backups"](v2/DEPLOY.md#backups)).

### New Microsoft permissions

None. The sign-in and directory audit logs were already read; they're now read incrementally and kept.

### Database upgrade

Yes: migration 0030 adds two small tables (`event_tenants`, `event_cursors`) for where each tenant's logs
have been read up to, and migration 0031 adds `summary_cache` for the precomputed rows under Changed. The
portal saves a `pre-upgrade-*` copy first, as always. The precomputed rows fill in as the worker gets to each
tenant; until then the pages build them as before.

### Added

- The project's website ([website/](website/README.md)): a page about Office Sentry and these docs as web
  pages with search, rebuilt from the repository whenever a doc changes, and a public read-only demo.
- Deploy workflow: a `restore-drill` action, backup status in `status`, and a `PRODUCTION_FROM_TAGS`
  variable that makes the portal take release tags only, as the published image pinned by digest
  ([v2/DEPLOY.md, "Production from releases"](v2/DEPLOY.md#production-from-releases)).
- Activity history: every sign-in and directory audit event is kept beyond the 30 days Microsoft keeps them.
  Each collection reads on from where the last one stopped, so a run that times out, hits its page limit or
  is interrupted carries on next time with nothing lost or counted twice, and a busy tenant's sign-in log is
  no longer cut off at 50,000 rows without saying so. Defaults: every sign-in for 180 days, daily sign-in
  summaries for 25 months and directory audit events for 7 years (Settings → Data retention, "Activity
  history"). Deleting a tenant deletes its history.
- A tenant's Activity history page (Changes → Activity history, staff only): how far back each log is
  detailed from, the last 30 days, sign-ins per day, each month with a CSV of every sign-in, and admin
  changes worth a second look.
- Charts on the busiest report tables (sign-ins by country and by app, licences, MFA methods, admin roles,
  security gaps, device compliance, mailbox and site storage): each bar opens the rows behind it, and the
  chart follows the table's filters, on the page and in the PDF.
- Small trend lines beside headline figures (the last 90 days) on the dashboard, the detail reports and
  the monthly review's key figures.
- The SharePoint storage report has a chart of storage over time with where it's heading at the last 30
  days' rate, up to the date it reaches the tenant's storage limit.
- The Admin activity report covers the last 12 months of kept history, each person's page shows their
  sign-ins by month and admin changes from it, and What changed lists admin changes with their own dates.

### Changed

- What changed across all tenants and the All tenants check matrix open in a fraction of a second at 200
  tenants (What changed took about 30 seconds). The worker works out each tenant's line after its
  collection and once a day; the pages read those lines, and rebuild any line whose data has changed since.
- The monthly review's "What changed" lists each new or returning finding it counts, next to the fixed
  ones, with the tenant's own changes (new accounts, devices, forwarding) under their own heading. Before,
  it gave the count and the list below showed different items.
- Sizes from 1 TB to 10 TB show two decimals ("1.28 TB of 1.45 TB"), so used, free and total add up on
  the SharePoint storage report.

### Security

- Encrypted off-site backups with restic to any S3-compatible storage, a weekly restore drill that proves
  the newest one opens, and the keys kept in a volume of their own, apart from the data and its backups.

## 2.0.0 (2026-10-09)

The first release of Office Sentry v2: read-only Microsoft 365 reporting for MSPs, rebuilt from the v1
portal. Everything below is new compared with v1.

### Action required

- **Installs that followed `main` with `git pull`** (before releases existed): the Docker setup now pulls
  the published image, `ghcr.io/jackd99/officesentry`, instead of building the folder. Add
  `OFFICESENTRY_VERSION=2.0.0` to `.env`, then `docker compose pull && docker compose up -d`. To keep
  building from the folder instead, add `COMPOSE_FILE=docker-compose.yml:docker-compose.build.yml` to `.env`.
- While the repository is private, the image is too: a server pulling it needs `docker login ghcr.io`
  with a GitHub token that can read packages.

### New Microsoft permissions

None beyond what each tenant already consented to. A new install asks each client tenant for the read-only
permissions listed in [What Office Sentry reads](v2/docs/what-office-sentry-reads.md).

### Database upgrade

Yes, for installs that ran a development build: the portal saves a `pre-upgrade-*` copy of the database
first, then upgrades it on start. New installs start with an empty database.

### Added

- Client-ready monthly reviews, insurance evidence packs and recommended projects, as pages and branded PDFs.
- Recommended projects are only work of half a day or more, with a rough estimate; smaller jobs are quick
  fixes on each client's page and in the month-end review. Admin account hardening is now part of Identity
  and Conditional Access, and Teams and groups governance part of Sharing, guests and Teams governance;
  their Proposed, Agreed or Declined status and notes move with them.
- Detail reports for identity, Exchange, SharePoint, OneDrive, Teams, devices and licences, each with
  filters, saved views and PDF, XLSX and CSV downloads.
- History for every tenant: what changed month over month, per tenant and across all tenants.
- One page per person, cross-tenant search, and an All tenants view of every report and check.
- Emailed reports, month-end review and approval, alerts to Teams, Slack, webhooks and email.
- Tenant pause, archive and delete; connection health with plain-English fixes; national clouds (GCC High,
  DoD and China).
- Tenant groups (Settings → Tenant groups) by tier, region, service level or anything else: a Group picker
  narrows the dashboard, All tenants, What changed, the month-end queue and Client tenants (and their
  downloads), a digest can cover one group, and an analyst can be granted every tenant in a group.
- Two-step sign-in, server-side sessions, an activity log, backups and a `restore` command.
- System status (Settings → System status) and system alerts: the team is emailed and the alert webhook posted
  when the worker stops, a backup fails, the disk is over 85% full or most tenants fail to collect.
  `/health/ready` answers 503 for uptime monitors when the portal can't do its job, with every check as JSON
  for a monitor holding the monitoring token ([v2/DEPLOY.md, "Is it working?"](v2/DEPLOY.md#is-it-working)).
- Releases: a versioned image on GHCR for amd64 servers with an SBOM, build provenance and a
  vulnerability scan; the version shows at the foot of the menu and on `/health`.

### Security

- Read-only toward client tenants: GET-only Graph calls and `Get-` Exchange cmdlets only.
- Certificate-based, app-only sign-in to client tenants; secrets are encrypted at rest and never shown or
  downloadable.
- Dependencies are installed from a hash-pinned lock file, and base images are pinned by digest.
