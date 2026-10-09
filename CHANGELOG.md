# Changelog

What changed in each Office Sentry release, written for the people who run it. Read the
**Action required**, **New Microsoft permissions** and **Database upgrade** parts before updating.

How to update: [v2/DEPLOY.md, "Updating"](v2/DEPLOY.md#updating). How releases are made:
[CONTRIBUTING.md, "Releases"](CONTRIBUTING.md#releases).

## 2.0.0 (2026-10-09)

The first release of Office Sentry v2: read-only Microsoft 365 reporting for MSPs, rebuilt from the v1
portal. Everything below is new compared with v1.

### Action required

- **Installs that followed `main` with `git pull`** (before releases existed): the Docker setup now pulls
  the published image, `ghcr.io/jackd99/officesentry`, instead of building the folder. Add
  `OFFICESENTRY_VERSION=2.0.0` to `.env`, then `docker compose pull && docker compose up -d`. To keep
  building from the folder instead, add `COMPOSE_FILE=docker-compose.yml:docker-compose.build.yml` to `.env`.
- **Servers deployed with the Deploy workflow that ran the demo site** (`DEMO_ENABLED`): the password-protected
  demo is replaced by a public, read-only one that resets every night, meant for the project's own server
  ([v2/deploy/public/README.md](v2/deploy/public/README.md)). Delete the `DEMO_PASSWORD` secret. To keep a
  demo, follow that README (it opens port 443 to everyone, so the portal's allow-list moves to the
  `PORTAL_ALLOWED_IPS` secret); otherwise set `DEMO_ENABLED` to `false`. The old demo's data can go with
  `docker volume rm v2_officesentry-demo-data` in a shell.
- While the repository is private, the image is too: a server pulling it needs `docker login ghcr.io`
  with a GitHub token that can read packages.
- **Keys move out of the data volume.** `secrets.json` now lives in its own volume, `officesentry-keys`,
  so backups of the data never hold the key that decrypts them. Download the new `docker-compose.yml`
  with this release; the portal moves the file on its first start and logs **Moved the key file**. Then
  save a copy of `/keys/secrets.json` in your password manager ([v2/DEPLOY.md, "Where the keys
  are"](v2/DEPLOY.md#where-the-keys-are)). Going back to an older release afterwards: copy it back to
  `/data` first (v2/DEPLOY.md, "Rolling back an update").

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
- The project's website ([website/](website/README.md)): a page about Office Sentry and these docs as web
  pages with search, rebuilt from the repository whenever a doc changes, and a public read-only demo.
- Releases: a versioned image on GHCR for amd64 servers with an SBOM, build provenance and a
  vulnerability scan; the version shows at the foot of the menu and on `/health`.

### Security

- Read-only toward client tenants: GET-only Graph calls and `Get-` Exchange cmdlets only.
- Certificate-based, app-only sign-in to client tenants; secrets are encrypted at rest and never shown or
  downloadable.
- Dependencies are installed from a hash-pinned lock file, and base images are pinned by digest.
- Encrypted off-site backups with restic to any S3-compatible storage, a weekly restore drill that proves
  the newest one opens, and the keys kept in a volume of their own, apart from the data and its backups.
