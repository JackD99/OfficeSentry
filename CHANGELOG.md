# Changelog

What changed in each Office Sentry release, written for the people who run it. Read the
**Action required**, **New Microsoft permissions** and **Database upgrade** parts before updating.

How to update: [v2/DEPLOY.md, "Updating"](v2/DEPLOY.md#updating). How releases are made:
[CONTRIBUTING.md, "Releases"](CONTRIBUTING.md#releases).

## 2.0.0 (unreleased)

The first release of Office Sentry v2: read-only Microsoft 365 reporting for MSPs, rebuilt from the v1
portal. Everything below is new compared with v1.

### Action required

- **Installs that followed `main` with `git pull`** (before releases existed): the Docker setup now pulls
  the published image, `ghcr.io/jackd99/officesentry`, instead of building the folder. Add
  `OFFICESENTRY_VERSION=2.0.0` to `.env`, then `docker compose pull && docker compose up -d`. To keep
  building from the folder instead, add `COMPOSE_FILE=docker-compose.yml:docker-compose.build.yml` to `.env`.
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
- Detail reports for identity, Exchange, SharePoint, OneDrive, Teams, devices and licences, each with
  filters, saved views and PDF, XLSX and CSV downloads.
- History for every tenant: what changed month over month, per tenant and across all tenants.
- One page per person, cross-tenant search, and an All tenants view of every report and check.
- Emailed reports, month-end review and approval, alerts to Teams, Slack, webhooks and email.
- Tenant pause, archive and delete; connection health with plain-English fixes; national clouds (GCC High,
  DoD and China).
- Two-step sign-in, server-side sessions, an activity log, backups and a `restore` command.
- Releases: a versioned image on GHCR for amd64 servers with an SBOM, build provenance and a
  vulnerability scan; the version shows at the foot of the menu and on `/health`.

### Security

- Read-only toward client tenants: GET-only Graph calls and `Get-` Exchange cmdlets only.
- Certificate-based, app-only sign-in to client tenants; secrets are encrypted at rest and never shown or
  downloadable.
- Dependencies are installed from a hash-pinned lock file, and base images are pinned by digest.
- Encrypted off-site backups with restic to any S3-compatible storage, a weekly restore drill that proves
  the newest one opens, and the keys kept in a volume of their own, apart from the data and its backups.
