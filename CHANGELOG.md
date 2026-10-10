# Changelog

What changed in each Office Sentry release, written for the people who run it. Read the
**Action required**, **New Microsoft permissions** and **Database upgrade** parts before updating.

How to update: [v2/DEPLOY.md, "Updating"](v2/DEPLOY.md#updating). How releases are made:
[CONTRIBUTING.md, "Releases"](CONTRIBUTING.md#releases).

## 2.1.0 (2026-10-10)

The first release from the public repository. The headlines: home pages for technicians and account
managers, onboarding many clients at once, answers for insurance and supplier questionnaires and Cyber
Essentials, client reports built from blocks, four Exchange reports, sign-in and audit history kept beyond
Microsoft's 30 days, and every report shown as the tenant was on any earlier day. Read **Action required**
before updating: the keys move to a volume of their own.

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
The new Exchange reports use more read-only `Get-` cmdlets under the Global Reader role clients already
assign, and automatic replies use `MailboxSettings.Read`, already granted, so no client approves again.

### Database upgrade

Yes: migration 0030 adds two small tables (`event_tenants`, `event_cursors`) for where each tenant's logs
have been read up to, migration 0031 adds `summary_cache` for the precomputed rows under Changed, and
migration 0032 adds `report_designs` (the client reports built from blocks under Month-end → Report designs),
migration 0033 adds `email_schedule_views`, the saved views a scheduled email sends, migration 0034 adds
`onboarding` (each new client's contact and how far their consent has got) and migration 0035 adds each
person's home page to `users`. The portal saves a `pre-upgrade-*` copy first, as always. The precomputed rows
fill in as the worker gets to each tenant; until then the pages build them as before.

Migration 0036 adds five empty tables for the daily history (`state_*`): the upgrade is instant and changes
no existing rows. The worker then copies the snapshots retention kept into the history in the background, a
few seconds a minute, oldest first, pausing while the disk is over 85% full. At 200 tenants that took about
12 minutes of worker time and 0.07 GB. To see the room it needs first, run `python -m officesentry history
--dry-run` (it writes nothing); `history` shows how far it has got, and `history --verify` checks that every
copied collection reads back exactly as its snapshot.

### Added

- Home pages by role, each opening with one sentence that says what needs attention. **Today**, for
  technicians: the clients that need someone today and why (new critical and high findings, failed
  collections, access not granted, anything due this week). **Clients**, for account managers: licence
  waste, renewals in the next 30 days, reports to approve and projects to propose, per client. Pick yours in
  Account settings; Home in the menu and the logo open it, and staff land on it when they sign in.

- Onboarding many clients (Settings → Client tenants → Onboarding): import tenants from a CSV (name, tenant ID
  or domain, group, contact; a preview first, nothing added until you confirm), then a tracker of every
  tenant's state (not sent, sent, approved, consented, Global Reader missing, connected) with a copyable
  consent link per tenant, a ready-to-send email, a "send this to your client" PDF pack (how to approve,
  what Office Sentry reads and never reads, how to remove it) and a mail-merge CSV of every link. Links
  sent to clients now work: their Global Administrator lands on a thank-you page instead of your sign-in
  page, and Office Sentry confirms the consent by signing in to the tenant (within minutes, then hourly for a
  month after a link is marked sent) and starts the first collection itself. No change to the app
  registration is needed: it uses the same redirect URI.
- Onboarding a client documents GDAP: a Partner Center technician whose GDAP role includes Privileged Role
  Administrator can approve the app and assign Global Reader for a client with their own partner account
  ([v2/docs/onboarding-a-client.md, "Through Partner Center (GDAP)"](v2/docs/onboarding-a-client.md#through-partner-center-gdap)).
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
- Questionnaires and evidence (a tenant's Reports → Questionnaires): answers for cyber insurance proposal
  forms, supplier security questionnaires and Cyber Essentials (IASME's Danzell set, by question number), in
  each form's wording and ready to copy and paste, with the Microsoft 365 data and date behind each one.
  Questions Microsoft 365 can't answer (backups, firewalls, plans) show a text to complete, with the
  missing parts in brackets; an answer written once fills every form that asks it. Downloads: a PDF answer
  sheet, XLSX, CSV and plain text per form.
- Charts on the busiest report tables (sign-ins by country and by app, licences, MFA methods, admin roles,
  security gaps, device compliance, mailbox and site storage): each bar opens the rows behind it, and the
  chart follows the table's filters, on the page and in the PDF.
- Small trend lines beside headline figures (the last 90 days) on the dashboard, the detail reports and
  the monthly review's key figures.
- The SharePoint storage report has a chart of storage over time with where it's heading at the last 30
  days' rate, up to the date it reaches the tenant's storage limit.
- The Admin activity report covers the last 12 months of kept history, each person's page shows their
  sign-ins by month and admin changes from it, and What changed lists admin changes with their own dates.
- Scheduled emails can send saved views: a report with your filters, columns and sort, as a PDF, XLSX or
  CSV. Tick them on a client's scheduled email, or choose **Email this view on a schedule** from a report's
  Views menu. Email reports → Saved views sends a view with every tenant's rows (or one group's) to your
  team, like the All tenants "every row" page. A scheduled view keeps working if its saved view is deleted.
- Alerts can post "The October 2026 report for Contoso is ready" to Teams, Slack or your webhook when a
  client's report email goes out or a month-end report is published (Settings → Alerts, "A client's report
  is ready"; off until you tick it). The post links to the client's report history and never carries the
  files, so only people signed in with access to that client can open them.
- Report designs (Month-end → Report designs): client reports built from blocks in the portal, without code.
  Drag in a status verdict, key figures, failing checks, what changed, licences, a table from any detail
  report, your own text and more; set each block up in its panel; see it on a real client as you go. Start
  from an executive summary, a full review, a licensing review or a blank page. A design works for every
  tenant (a block with nothing to show for a client is left out), opens from the tenant's Reports tab as a
  page and a PDF, and a report plan or email schedule can send it like the monthly review.
- Four Exchange reports (Reports → Email): **Mobile devices** (phones and tablets syncing mail, with which
  app, when each last synced, leavers' devices still connected, devices left over from mailboxes that
  no longer exist and ones waiting for approval),
  **Automatic replies** (who is sending an out-of-office reply, who outside hears it, and replies on so
  long they look forgotten), **Distribution lists** (members, owners, outside senders, and empty,
  ownerless or unused lists, from a count of the message trace kept per day) and **Mailbox size trend**
  (each mailbox's growth over one and three months from Office Sentry's own snapshots, and when it will
  fill up). If the message trace isn't available to the app, lists show without activity and say why.
- Unused apps report: every app from outside Microsoft with when it last signed in (from Entra ID's app
  sign-in activity, which needs Entra ID P1), flagging apps unused for 90 days that still hold permissions.
- All users report: every account with its department, job title, manager, licences and last sign-in, each
  manager with their direct reports, member counts by department, and accounts reporting to a manager whose
  sign-in is blocked.
- Empty, nested and dynamic groups report: groups with no members, groups inside other groups (and how many
  levels deep), and dynamic groups with their rules, flagging paused ones.
- A "Directory clean-up" job on the Reports page that groups these with Inactive accounts and the recycle bin.

- Daily history: each collected object is stored once per version, with the collections it was valid
  between, instead of a full copy per collection. It's written alongside the snapshots (which every page
  still reads) and checked against each one as it's written, so a tenant can be shown as it was on any day.
  Kept for 25 months by default (Settings → Data retention, "Daily history"); about 2.5 MB a night at 200
  tenants ([v2/DEPLOY.md, "How long data is kept"](v2/DEPLOY.md#how-long-data-is-kept)).
- Reports as of any date: every detail report can show the tenant as it was on an earlier day (its downloads
  too), read from the daily history when that day's snapshot has gone, and What changed compares any two
  dates instead of only "now against N days ago". Sign-in detail, which retention keeps for less time, says
  when it's no longer held.

### Changed

- Tenant pages and reports are shorter to reach: the Reports tab lists every report (summaries and detail
  reports) in one searchable list beside the report on laptops and in one menu on phones, instead of three
  rows of buttons, and the tab bar stays at the top of the window as you scroll. On a laptop screen a
  report's first table rows now show without scrolling.
- What changed across all tenants and the All tenants check matrix open in a fraction of a second at 200
  tenants (What changed took about 30 seconds). The worker works out each tenant's line after its
  collection and once a day; the pages read those lines, and rebuild any line whose data has changed since.
- The monthly review's "What changed" lists each new or returning finding it counts, next to the fixed
  ones, with the tenant's own changes (new accounts, devices, forwarding) under their own heading. Before,
  it gave the count and the list below showed different items.
- Sizes from 1 TB to 10 TB show two decimals ("1.28 TB of 1.45 TB"), so used, free and total add up on
  the SharePoint storage report.
- The sign-in log and shared files' sharing are read without the fields Microsoft has retired (it marked
  both calls with a sunset date because of them, the sign-in log's on 31 August 2026). Office Sentry now asks
  only for the fields it uses, so nothing it relies on is being retired; reports and stored history are
  unchanged.

### Fixed

- A domain with several problems in one area (an SPF record ending in `+all` that also uses `ptr`, say)
  was stored on the Findings tab, in alerts and in the month-end count at its least serious problem. It is
  now stored at its worst, with every problem listed.
- A Conditional Access policy for all apps that excludes some (every app except Exchange Online, say) no
  longer counts as MFA, or a legacy sign-in block, for everyone; the check names the apps it leaves out.
  Excluding Microsoft Intune Enrollment, as Windows Autopilot needs, still counts.
- A DNS lookup that fails (a timeout, or a resolver answering SERVFAIL or REFUSED) no longer reads as the
  domain having no records: the domain shows "Couldn't check", its findings stay as they were, and the
  mail domain count isn't recorded as zero that night.
- Send now or Retry on a monthly report no longer moves the schedule back from a month that was approved
  and sent early, which made the timer email that month again, unreviewed.
- Editing a paused email schedule keeps it paused.
- Collections no longer fail with "database is locked" when other work writes to the database at the same
  moment: every write transaction now waits its turn.
- The page a client approves access from (*What Office Sentry reads*) and the consent pack now say what is
  kept from the sign-in and audit logs, and for how long, list every Exchange command, and mention the
  MTA-STS policy file fetched over HTTPS.

### Security

- Client users no longer see the MSP team's email addresses on the tenant's Emails page: alert emails are
  left out, and report emails show only the client's own contacts.
- Encrypted off-site backups with restic to any S3-compatible storage, a weekly restore drill that proves
  the newest one opens, and the keys kept in a volume of their own, apart from the data and its backups.
- Wrong guesses at the current password on the change-password form now count towards the account's
  sign-in lock, as they already did when turning off two-step sign-in.
- `rotate-keys --new` saves the new keys beside the old ones before it re-encrypts anything, so an
  interrupted rotation can no longer leave stored secrets under a key that was never saved.
- The public demo no longer stores an authenticator secret on its shared visitor when the two-step setup
  page is opened.

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
