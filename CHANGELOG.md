# Changelog

What changed in each Office Sentry release, written for the people who run it. Read the
**Action required**, **New Microsoft permissions** and **Database upgrade** parts before updating.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) (Added, Changed, Fixed,
Security, with the newest release first and a compare link per version) plus those three sections, and
versions follow [semantic versioning](https://semver.org). How to update: [v2/DEPLOY.md,
"Updating"](v2/DEPLOY.md#updating). How releases are made: [CONTRIBUTING.md,
"Releases"](CONTRIBUTING.md#releases).

## [2.4.0] - Unreleased

### Database upgrade

Yes, applied on start as usual, after a copy of the database is made: migration 45 adds a column that keeps
each approved month-end report's commentary, recommendations and notes as the client was given them. Months
approved before the update show the month as it is now, as before, until they're issued again.

### Added

- **The sign-in page says who to ask.** Under the sign-in card, "Trouble signing in? Contact Harbour IT at
  help@... or 0161 ...", from the Branding page's support details, else the contact line the reports carry.

### Changed

- **A client's home page leads with where they stand**: the month's verdict, the account manager's note once
  the month is approved, and buttons for the monthly review, its PDF and the two-page summary. Under it, the
  MSP's own "What we recommend next" with each one's status and target, else what needs attention, led by
  what was found ("1 admin without MFA") rather than the check's goal.
- **The report email carries what the attached review does**: the verdict, the account manager's note ("From
  Harbour IT") and their recommendations (else the top failing checks), in the HTML and the plain text.
- **The verdict names the most urgent item** under it ("Most urgent: 1 admin without MFA"), on the review's
  cover, report designs, the client's home page and the email.
- **The two-page summary is two pages.** The summary PDF's cover is a band on page one, without a contents
  list, so it's two pages with the account manager's note and recommendations, not three or four.
- **A quarterly review compares with the start of the quarter.** A design with the quarter's blocks says
  "Compared with 1 August", and its summary, key figures and What changed run from then, not from four weeks
  ago.
- **Check advice reads right to the client too.** "Review each app with the client", "the client's data" and
  "Waiting for the client to approve" no longer reach a client's report, and admin MFA's advice no longer says
  "each listed admin" where nothing is listed.
- Charts of findings found and fixed name the first month and each January in full ("May 2026"), admin
  activity in What changed gives dates in words ("6 Oct 2026"), and a quarterly review's Coming up shows
  "Now" for what needs attention now.

### Fixed

- [v2/DEPLOY.md](v2/DEPLOY.md), *Running on Windows without Docker*: updating no longer deletes the virtual
  environment. The steps said to replace the `v2` folder, which holds `.venv`; they now say to replace
  everything in it except `.venv`.
- [v2/DEPLOY.md](v2/DEPLOY.md), *First start*: it gives the commands that download the release's files, and
  turns HTTPS on with `COMPOSE_PROFILES=https` in `.env`, as the cloud start-up file already did, so later
  commands need no `--profile https`. It also ends with saving the key and setting up backups. *Updating* says
  how an install started with `--profile https` moves over. *Moving to a new server* copies `.env.backup`, the
  override and the backup jobs too. A new *Removing Office Sentry* section takes away the access Office
  Sentry was given, as well as the server.
- The deploy docs:
  - [Troubleshooting](v2/docs/troubleshooting.md) covers an address that doesn't load, or a certificate
    warning.
  - [Securing the portal](v2/docs/securing-the-portal.md) no longer says sign-in failures can go to a webhook.
  - The [deploy guides](v2/docs/deploy/README.md) say a restore needs both containers stopped.
  - The NAS guide's backup copies no longer pile up.
  - The Azure and AWS guides restart Caddy after an update.
- **Settings → Updates:** an Action required that starts "Nonetheless" still asks for the tick, and a
  Database upgrade that starts "Note" still shows its warning (only "None" and "No" as words count as
  nothing to do). Release notes holding a NUL character no longer break the page.
- **Update button:** when the data volume is full once the new version is running, the updater still goes
  back to the version before if the new one isn't healthy, and an update that stops part way never says
  "before anything was changed".
- **A client's home page showed checks the month-end reviewer had left out of their copy.** It's now built
  from the client's copy of the month, as their report, PDFs and email are.
- **The email and the home page counted affected accounts as checks** ("46 checks need attention (1 critical,
  30 high, 69 medium, 63 low findings)" beside a report saying 14 high). They count checks, as the report does.
- In PDFs, the found-and-fixed chart's "Fixed" key was cut off at the right edge.
- A full review whose month has commentary no longer runs its cover's contents list under the footer: the
  account manager's note opens the page after the cover.

### Security

- **Update button:** the updater only installs a release whose tag is on a commit in main's history, so a tag
  pushed on another branch's commit (which runs that commit's own release workflow) is refused even though
  its image is signed. The updater, which runs as root, never follows a link the portal could put in place of
  its shared `updates` folder, when it makes the folder, writes its status or takes its lock, and a named pipe
  or deeply nested file left as the request no longer stops it.
- **Files and mailbox access:** only a file in the client's own SharePoint or OneDrive becomes a link shown by
  its path. An inbox rule or other item named like an address on another host is shown whole and never as a
  link, on the page, in the PDF and spreadsheet and in a person's timeline, so it can't pass for the client's
  file.
- **Connections:** the steps that add Office Sentry's permissions to an app registration only work on the
  shared read-only app, never on the audit log add-on's app, which its clients approved for one permission.
- **Clients:** a new client never gets the id of one that was deleted. Before, the next client added after
  deleting the newest one took its id, and with it the deleted client's rows in the **Activity log**, its
  name and domain included, for anyone granted the new client. Rows from before a client was added no longer
  show as its own, so an id already reused this way stops showing the old client's history too.

### Security

- **After approval, the client keeps the approved month-end report until a new version is issued.** Commentary,
  recommendations and client-visible notes changed after a month was approved used to show on the client's live
  report straight away. The client now sees what was approved (the same as their PDF), the review screen says the
  month has changed since approval, and the month-end queue lists it under **Needs review** until someone issues
  a new version.
- **Only someone who can open every report a client's report plan emails can add a contact.** New contacts get
  the plan's emails, so someone without one of those report areas could add a contact, their own address
  included, and receive reports they can't open. The Contacts card and the schedule form now say who can add
  one. Choosing who gets a client's new-finding alerts now needs *Security findings and reviews*, since alerts
  name the findings.

## [2.3.1] - 2026-10-12

Fixes for 2.3. Nothing to do before updating: `docker-compose.yml`, the `Caddyfile` and `deploy/jobs.sh` are
the same as 2.3.0's, so the Update button can install it.

### New Microsoft permissions

None.

### Database upgrade

No.

### Fixed

- **Site access** no longer shows every site as open to everyone, guests included: SharePoint lists the
  "Everyone" claims on every site whether or not they're given anything, so whether everyone can open a site
  now comes from what it grants, directly or through its SharePoint groups. Nor does it say a site with no
  Microsoft 365 group has no owner (a high finding from **Every site has at least two owners**) when its
  owners are in the site's owners group: their members now count as owners once the client has approved the
  SharePoint permission, and until then the report and the check say they can't count them yet.
- **Teams** no longer lists every team older than 90 days as inactive where Microsoft hides names in usage
  reports, its default: Microsoft then hides team IDs in the activity report too, so the report says inactive
  teams can't be told until names are shown, and how to show them.
- **Data collection** no longer shows a green run that read nothing. Inbox rules and automatic replies with no
  mailboxes or users collected yet, and per-user MFA before the client's licences are known, now fail and say
  what has to be collected first, so their reports say "not collected" instead of "none found".
- **Month-end:** a paused client's month no longer sits in the queue as a draft; it's named under the queue
  until the client is resumed. An automatic plan whose client has no contacts now records a failed email on
  its day ("has no contacts to email it to") instead of sending nothing without a word, and sends that
  month's report once a contact is added.
- An app connection made by hand from the App connection page's steps no longer asks to **Update this app**
  as soon as its client ID is saved: those steps already list every permission the release asks for.
- **Activity history** pages no longer say "detailed from" a date older than the history kept, once retention
  has removed the months before it.
- `create-app` and `update-app` say in plain words when Microsoft can't be reached, refuses or doesn't know
  the tenant, instead of printing a traceback.
- `check-backup` of a plain `.db` file no longer leaves `-wal` and `-shm` files beside it, and `restore`
  removes the first-admin setup link left by a new server, since the restored database has its admins.
- Reloading two-step sign-in setup in the same browser within five minutes keeps the same QR code, so a code
  just scanned from it still works.
- The Kubernetes manifests and the deploy guides' examples name this release (they still said 2.1.0), and a
  test keeps them in step with each release.
- Tests no longer fail when run around midnight UTC, from the first of a month, or a few hours after the live
  tenant was recorded.
- **Wide tables fit a laptop.** The last tables that scrolled sideways at 1366 px with long names now fit: a long
  name before an address's "@" or a long host name in a DNS record may wrap at its dots, and **Every row** on All
  clients opens without a column or two its Client column has no room for (Size on shared mailboxes, Synced
  from on-premises on privileged role holders, Addresses on repeated sign-in failures, Department on deleted
  users, Account and Send on behalf on mailbox permissions by person). **Columns** offers them, and the client's
  own report and the downloads keep them. Tables are measured again once the page's font has loaded, so one that
  only just fits no longer slips past its card, and the All clients checks matrix now shows every category.
- **Settings → Updates** shows a release's **Action required** that says "None" as plain text, without a
  warning or a box to tick before **Update**.

### Security

- **What changed shows only the report areas someone can open.** Anyone with *What changed* saw every area's
  changes on a client, including areas their role leaves out (an Account manager saw identity and mailbox
  changes). Now each key change, figure and report's changes on a client's What changed page, its PDF and
  spreadsheet, and the All clients page show only to someone who opens that report there, and new and fixed
  findings only to someone who opens findings.
- **A role that sees or does more than you is an Administrator's to change.** Someone who manages users
  without being an Administrator could take capabilities out of such a role, or reset or delete it, and
  couldn't put them back. The role page now says only an Administrator can change it.
- **Only Administrators see an Administrator's signed-in sessions** on their user page.
- **The SMTP server is checked like a webhook.** Office Sentry looks it up once and connects to that address,
  refusing a link-local (cloud metadata), multicast or reserved one. This machine and the local network are
  still allowed, since mail relays live there.
- **The first-admin setup link stays out of `web.log`.** The log file (read by the support package and the
  Support page) now says where to find the link; only the portal's own output shows it.
- The public demo makes PDFs two at a time, so a stream of PDF requests can't slow its pages for everyone
  else, and the website has a `/.well-known/security.txt` saying where to report a vulnerability.

## [2.3.0] - 2026-10-12

### Action required

- **Updated to 2.2.0 without downloading its `docker-compose.yml` and `Caddyfile`, or without
  `--profile https`?** Download both from the release and run `docker compose --profile https up -d`. Until
  then the portal keeps 2.1.0's 30 seconds to start instead of 15 minutes, and Caddy keeps running without
  2.2.0's hardening (responses still carry a `Server` header). [v2/DEPLOY.md, "Updating"](v2/DEPLOY.md#updating)
  now says to download both every time, since releases change them without always saying so.
- **Off-site backups set up by hand** ([v2/DEPLOY.md, "Off-site backups"](v2/DEPLOY.md#off-site-backups),
  step 5): copy `deploy/jobs.sh` from this release's source over your copy, so a restore from the off-site
  copy brings back the activity history too.
- **Client sign-in with Microsoft** needs nothing on upgrade: it's off until an admin turns it on. Turning it
  on makes the staff sign-in app accept other organisations' accounts (one setting on the app in your own
  tenant, done by **Sign in to Microsoft** under *Settings → Microsoft sign-in*); who can sign in is still
  decided by Office Sentry ([v2/docs/client-sign-in.md](v2/docs/client-sign-in.md)).

### New Microsoft permissions

None. The Intune policies report, BitLocker recovery keys and PIM for groups use the permissions clients were
asked to approve with 2.2, and the mail rules and protection report uses read-only Exchange cmdlets under the
access clients already gave.

### Database upgrade

Yes, applied on start as usual, after a copy of the database is made: migration 41 adds a count of wrong
passwords to each signed-in browser, migration 42 records which Microsoft tenant each account signs in from,
when each client's people first signed in with Microsoft and when a session last confirmed a change with
Microsoft, and migration 43 adds a small table for what the month-end review leaves out of each client's
month. Nobody's access changes.

### Added

- **Client sign-in with Microsoft.** A client's people can sign in with the work account they already use,
  under their own organisation's MFA and Conditional Access, instead of a password from you. Each client's
  **Settings → Client sign-in** page lists who there can sign in and how, adds their primary contacts by work
  address (found in the client's directory as Office Sentry last collected it; report contacts without an
  account are a click away), gives each person a sign-in link with their address filled in, and has a
  message to send them and an approval link for their Microsoft 365 administrator, which asks for nothing but
  sign-in. Office Sentry only lets them in from their own company's tenant, into their own client account,
  matched on Microsoft's ID for that person and never on a name or address: never as staff, never into a new
  account, whatever roles their own tenant gives them, and only ever to clients in their own organisation.
  Nobody is invited into your tenant as a guest.
- **Settings → Microsoft sign-in** (was *Staff sign-in*) has a *Your clients' people* card: let the sign-in
  app accept clients' accounts with one Microsoft sign-in (or by hand), turn client sign-in on, and each
  client's people and approval at a glance.
- **Confirm with Microsoft.** An admin who signs in only with Microsoft has no password to type before a
  security change (setting someone's password, signing someone out, resetting their two-step sign-in), so
  those forms sign them in to Microsoft again instead; it counts for ten minutes.
- **Adding staff who sign in with Microsoft** on *Settings → Users*: no password, the username is their
  sign-in address, and the account is connected at their first sign-in. Staff can be granted clients before
  they first sign in.
- **Settings → Updates** lists every release newer than yours with its notes, what each asks of you first
  (Action required, New Microsoft permissions, Database upgrade, Security), and an **Update** button that
  installs it. The button checks the release is genuine, with build provenance signed by Office Sentry's
  release workflow on GitHub, downloads exactly that image, restarts the portal and the worker on it and, if
  the new version doesn't start healthy, goes back to the old one by itself, database included. The page
  follows the update through the portal's restart, and the activity log records who asked and how it ended.
  Nothing installs unless an admin presses the button. Office Sentry asks GitHub for the list of releases
  once a day (nothing about your server is sent, and the page can turn it off), and the foot of the menu
  says when one is out.
- The button needs the **updater**, a small container that is off until you turn it on with
  `COMPOSE_PROFILES=updater` in `.env`, using this release's `docker-compose.yml`. It is the only part of
  Office Sentry given the Docker socket; the portal never is. It only changes the image, so a release that
  changes `docker-compose.yml` or `Caddyfile` says so and is updated by hand. Updating by hand works as
  before, with or without it ([v2/DEPLOY.md, "The Update button"](v2/DEPLOY.md#the-update-button)).
- A capability for it, *Install updates*, held by Administrators and available to custom roles that see
  every client (like *Settings* and *System status*, it does nothing for a role limited to some clients).
- **Intune policies report** (Devices menu). Which platforms have devices enrolled but no compliance policy,
  what each compliance policy requires in plain words, configuration profiles and Windows update rings
  (deferral days, restart deadline, paused or not), endpoint security and settings catalog policies, and app
  protection for iPhones and Android phones (PIN, sharing, copy and paste, backups), each with who it's
  assigned to. Needs attention lists policies assigned to nobody, paused update rings, compliance policies
  that require nothing and app protection without a PIN. It reads the Intune permissions clients approved
  with 2.2; until a client has, the report says it's waiting.
- **BitLocker recovery keys** in the Devices report: every Windows computer joined to Entra ID, whether its
  recovery key is saved there, how many keys and when the newest was saved. Office Sentry reads only that a
  key exists, never the key. A computer Intune reports as encrypted with no saved key is flagged, since it
  can't be unlocked if it asks for its key.
- **Two device checks**: *Windows computers have their BitLocker recovery key saved* and *Every platform in
  use has a compliance policy*, both in the Device management with Intune project.
- **PIM for groups in the Admins report**: people who can make themselves a member of a group that holds an
  admin role are listed with that role as eligible, marked "(eligible member)" beside the group.
- **Mail rules and protection report** (Email menu). Every mail flow (transport) rule in the order it runs,
  with what it picks mail out by, what it does and its exceptions in plain words; inbound and outbound
  connectors with their servers, domains and encryption; and the anti-spam, anti-phishing and anti-malware
  policies, plus Safe Links and Safe Attachments where Defender for Office 365 is licensed, each with whom it
  applies to and whether a Standard or Strict preset covers them. Needs attention lists rules that let mail
  skip spam filtering or send copies outside the organisation, sender domains allowed past the spam filter,
  spoof protection or the attachment filter turned off, and a filtering service's connector without
  enhanced filtering.
- **Five email checks**, all in the Email security project: *No mail flow rule lets mail skip spam
  filtering*, *No mail flow rule sends mail outside the organisation*, *No sender domains are allowed past
  the spam filter*, *Spoof protection is on* and *Risky attachment types are blocked*.
- **Leave things out of one client's month-end report.** On the review screen, a failing check, a key
  figure, a recommendation, a part of the monthly review (Licences, What changed, Accepted risks and the
  rest) or a block of a report design can be left out of that client's copy for that month, and put back.
  The design, other clients and next month stay as they are. Left-out items stay on the review screen,
  greyed out with **Put back**, and the top of the page counts them with **Put everything back**; the sent
  PDFs, the spreadsheet, the email and the client's portal copy never have them.
- **A preview of the client's copy on the review screen**, beside the commentary and findings, switchable
  between the plan's reports, and drawn again as things are left out or put back.
- **Send failed months again from the queue.** After a mail outage, tick the clients whose email failed and
  press **Approve and send selected**: each gets the same frozen files again, without opening it.
- In the report designer: the preview is drawn at the printed page's width, so it lays out as the PDF does
  (four key figures to a row, half-width blocks side by side); clicking a block in the preview opens its
  settings; **Undo** and **Redo** (and Ctrl+Z); proper icons for move, half width, duplicate and remove; a
  search box over the blocks; and a **Band on page one** cover for short reports, which the Executive summary
  starter now uses.
- **The menu folds away.** "Fold the menu" at the bottom of the sidebar shrinks it to its icons, giving
  reports more room on a laptop, and it stays folded until you unfold it. Hover over an icon for its name.

### Changed

- The users page's two-step advice no longer counts client accounts that sign in with Microsoft, and their
  user page says their organisation's MFA applies instead.
- Setup notes written for your team, like "Microsoft is hiding people's names… sign in to
  admin.microsoft.com as a Global Admin", are left out of the client's copy of a report design. The portal
  and the previews still show them, marked "Only your team sees this". A design can include them again
  (**Include setup notes in the client's copy**), and the review can leave them out for one client and month.
- **Wide reports open on the columns that matter.** All users, Sign-in activity, Distribution lists, Teams
  channels, guests and apps, Licences and subscriptions, Automatic replies, Devices, Admin activity, Shared
  mailboxes, Mailbox size trend and Mobile devices leave a few secondary columns (dates created, older sizes,
  model) out of the page, so they fit a laptop. Columns shows how many are in view ("7/11") and brings the
  rest back; filtering by one shows it, and downloads still have every column.
- The Users lists (people, mailboxes, groups and sites) look and behave like report tables: long lists
  scroll inside their card with the headings in view, dates stay on one line and long addresses wrap. On a
  person's page, a long list of Conditional Access policies reads "7 policies" and opens in place.
- **Report tables fit the page.** A long list in a cell shows a short form you can open in place to see
  every item, one a line: who a Conditional Access policy covers reads "breakglass@contoso.com + 11 admin
  roles" rather than a paragraph of role names, and other lists show their first two and "+ N more".
  Downloads keep every item. Columns get room by how long their values are, long email addresses wrap after
  the "@", and headings no longer break at a hyphen. A table that still doesn't fit scrolls inside its card,
  with its scrollbar in view and its first column pinned, instead of pushing the page sideways.
- Every client page leads with the client's name, as the Overview does. Recommended projects, Report
  history and Offboard put their own title under the tabs, and Evidence and certifications and each
  evidence pack put theirs under the report picker with their downloads beside it, as detail reports do.
- **Recommended projects read like something you'd quote.** Directory sync that has stopped is now a quick fix,
  a repair rather than something to sell, and the old *Directory sync health* project is replaced by **Move to
  cloud-only accounts**: for a business of 50 users or fewer still syncing accounts from an office server, moving
  the accounts and domain-joined computers to the cloud so the server can go. *App and consent governance* is now
  *Third-party app review*, *Sharing, guests and Teams governance* is *File sharing and guest access*,
  *SharePoint storage and archiving* is *Storage and archiving* and *Licence and account clean-up* is *Licence
  savings and leaver clean-up*; statuses and notes you set on those are kept. A status set on *Directory sync
  health* doesn't carry over to the new project, since it was about the repair.
- **A proposal lists only the work the client needs.** "What the project covers", on the client's page and in the
  proposal PDF, now lists the parts their findings call for, so it no longer offers risky sign-in handling to a
  client with no risky sign-ins.
- **Site owners, guests with full control of a site and outside people who can edit shared files** are part of
  the File sharing and guest access project instead of separate quick fixes.
- The line under each finding on a client's Projects page now reads "Goal: every site has at least two owners",
  so it no longer looks like it contradicts "3 sites with no owner" above it. A failing directory sync check says
  "Not syncing: last sync 19 hours ago".
- A project picked in the month-end review lands on the client's report as "Project: Email security" rather than
  "Email security project".
- **All clients opens reports from the All reports menu**, the same one as a client's Reports tab: every
  report in columns with a search box, Monthly review, Insurance evidence and Checks included, instead of a
  drop-down list as tall as the screen. One row per client and Every row sit beside it; phones keep a list.

### Fixed

- An upgrade tried again within an hour of going back to the old version now copies the database again
  first. Before, it reused the first attempt's copy, so going back a second time lost what was written in
  between.
- The monthly review said a client's Microsoft 365 was "In good shape" when most checks couldn't run, for
  example while only the tenant profile had been collected. It now says **Partly checked**, and how many of
  the checks ran.
- **Inactive accounts** said "Needs Entra ID P1" whatever the reason it had no figure. It now gives the
  reason, such as "User data hasn't been collected yet".
- In a report design, the headings of two half-width blocks side by side line up, on the page and in the PDF.
- A report design previewed in dark mode drew its contact card dark on the light page; previews are light, as
  the PDF is.
- On phones, a table cell with more than one part keeps them together beside its label: a renewal's
  "in 20 days" no longer drops to the far side of the next line, and a project count no longer sits in the
  middle of the row.
- Long names and addresses no longer run off the screen: on phones, Delete buttons that name a client,
  group or person wrap, a headline figure such as "+3.8 GB a month" wraps inside its tile instead of over the
  next one, evidence pages show every download format and their whole answers, and long sign-in
  addresses in What changed and the Activity history break onto the next line. Settings > Users fits a
  laptop, with Add a user beside the two-step sign-in policy under the list.
- Evidence packs name the admin roles a Conditional Access policy leaves out of MFA ("Exchange Administrator
  (role)") instead of showing their Microsoft IDs.
- The Projects list fits a laptop screen: long client and project names wrap instead of pushing the Status
  column out of view.
- Restoring the off-site copy (`deploy/jobs.sh restore`) brings back the activity history as well as the
  database. Before, a new server got the database only, so every sign-in and audit event older than the 30
  days Microsoft keeps was gone, although the off-site copy held them.
- A backup restored without its activity history (a database file moved on its own) now says so, and the next
  collection reads back the 30 days Microsoft still keeps. Before, it said nothing, the Activity history page
  claimed months it no longer had, and those 30 days weren't read again. [v2/DEPLOY.md, "Moving to a new
  server"](v2/DEPLOY.md#moving-to-a-new-server) now moves the whole backups folder, history included.
- **Check connection now**, saving the client ID and the other connection checks say when Office Sentry can't
  reach Microsoft, or when Microsoft doesn't know the tenant (a mistyped domain or ID), instead of showing an
  error page. A collection that can't reach Microsoft fails with that reason, not as a bug.
- `restore` of a file the portal can't read (one put in with `docker compose cp`, which leaves it owned by
  root) says so instead of failing with a traceback, and restoring or checking a `.db.gz` no longer leaves
  `restore-*.db.part-shm` and `-wal` files behind. The activity history's backup manifest is readable by its
  owner only, like the copies.
- The Deploy workflow restarts Caddy after starting the portal, so a deploy that changes only a `Caddyfile`
  takes effect (Caddy reads it only when it starts, and compose doesn't recreate it for that). The project
  server's Caddy drops the portal's `Server` header too, as the bundled one does.
- [v2/DEPLOY.md](v2/DEPLOY.md): *Rolling back an update* copies the key back before 2.0.0 runs anything, and
  *Running your own build* keeps `docker-compose.override.yml` (the `/backups` folder) when `COMPOSE_FILE` is
  set, since Docker stops reading it by itself then.
- Spreadsheet downloads no longer fail when a tenant's text holds a control character (an inbox rule
  name with a vertical tab, say); evidence documents drop them the same way.
- Spreadsheets keep changes such as +4.6%, +2.2 and -1.5 GB, and dates written as "6 Feb 2026", as numbers
  and dates Excel can sort, and 29% no longer reads 29.0%. Person, mailbox, group and site spreadsheets
  open without Excel offering to repair them (they had a sheet called History, a name Excel keeps for
  itself), and the summary sheet's links reach sheets whose names have an apostrophe.
- CSV downloads show signed numbers such as +2, -1 and +4.6% without an apostrophe in front; anything
  that could be read as a formula still gets one. CSV and text downloads name their character set once.
- Change columns (30-day change, Growth a month) sort by size, so +21.1 GB comes above +7.7 GB, and names
  with accented letters sort in place (Åsa with the As, Émile with the Es) instead of after Z.
- A PDF of a report long enough to be cut at 2,000 rows counts every row in its figures and headings, as
  the page and spreadsheet do.
- The All clients review download marks its percentage columns with % and writes counts as whole numbers.
- Headline figures now open exactly the rows they count: Replying outside the organisation (replies on now,
  not scheduled ones), Admins with a gap, Global Administrators and Privileged accounts, No mail in 90
  days, Signed in with an old protocol (sign-ins that worked), and Not synced in 90 days and Password over
  a year old, which now count calendar days as their links do.
- Deleted users and groups gives separate figures for users and for groups about to go for good, each
  opening its own table, on the report and on All clients.
- Shared mailboxes over 50 GB are no longer flagged as disabled accounts wasting a licence. The check read
  mailbox sizes from the wrong place and never saw them.
- Licence right-sizing matches a mailbox to its account when the mailbox address differs from the sign-in
  name, so someone with a 48 GB mailbox and an archive isn't told Plan 1 will do. When Microsoft hides
  names in usage reports it no longer suggests removing licences, which Licence waste already counts, and
  a note says why.
- When Microsoft hides names in usage reports, OneDrive and SharePoint show hidden owners as "Hidden name
  (E6F395)", as the usage reports do, instead of a 32-character code.
- Mail flow shows "Not read" instead of green zeros when Microsoft refuses the message trace, says why at
  the top, and stays out of the All clients totals.
- Site permissions no longer waits for the client's approval forever because one locked site answered
  first while the permission is granted, and no longer passes "no guest has full control" when it couldn't
  read a single site.
- Teams no longer says it's waiting for the client's approval when the permissions are granted; a part
  Microsoft refuses is reported as failed. Site access only says the SharePoint permission is awaited while
  it is.
- A tenant with no outbound spam policy reads Unknown for automatic forwarding instead of Blocked, and the
  forwarding check can't run instead of passing.
- Sign-in retention now removes legacy sign-ins (accounts, addresses and countries) too; they were kept
  for good. A past day whose sign-in detail is gone says so instead of leaving its tables out.
- What changed no longer lists a report's data as new when one of its sources hadn't been collected at the
  start of the period, its sign-in detail was removed by retention, or the client approved a permission it
  needs in between (every Teams channel and guest read as added). It says why it didn't compare them, and
  no longer keeps saying a report is waiting for approval once the client has approved.
- What changed ignores what moves only with the clock (Not synced for 201 days, No mail in 90 days, Inactive
  for 6 to 12 months, Last 30 days), and a changed row shows as changed when another row has the same name
  (two groups called Marketing) instead of as added and removed.
- On a past day, What changed since then starts after the day shown. On a Changes page for chosen dates,
  Open report shows each report as it was at the end date, and the spreadsheet names that date instead of
  "Now".
- Automatic replies keep their First seen on date, and mailbox and site growth keep their figures, once
  older snapshots are pruned.
- **Quick fixes in a report design's Projects block** (the Quarterly review has one) no longer carry a green
  tick, which made a fix still to do look done. Each reads as it does on the Projects pages: what was found, with
  "Goal: ..." under it, and a quick fix that isn't a check, like "No Defender for Office 365", leads with its name
  instead of its explanation. In the PDF a quick fix no longer splits across two pages.
- On phones the Group picker fits its row when a group has a long name: it cuts the name it shows instead
  of widening Today, Clients, What changed, the All clients pages and the month-end queue.
- **A large month-end no longer loses emails to Microsoft's sending limit.** Exchange Online takes 30
  messages a minute from one mailbox; approving more clients than that at once had the rest refused ("429
  ApplicationThrottled") and left Approved with "Send failed", to be sent again one by one. Approvals and
  scheduled report emails now go out at that pace and wait out Microsoft's "too many messages" and "service
  unavailable" answers, for up to ten minutes, before they count as failed (in a longer outage the rest fail
  at once, as before). A test email or Send now from a page still sends at once.
- **The month-end queue flags a client a whole data source never came in for** ("Mailboxes not collected",
  or "2 data sources not collected") instead of marking it Ready, so a client whose Exchange access was
  never granted isn't approved and sent a report with that part missing. A client that removed the app,
  whose report could only say "Nothing has been collected, so no security checks could run", is flagged
  "No checks could run": it was marked Ready and sent in bulk before.
- On the public demo, picking a client group in the Group picker now shows that group, and the choice
  follows to All clients, What changed and the other group-aware pages. Before, the page went straight back
  to every client, because demo visitors have no sign-in to keep the choice on.
- Pages no longer scroll sideways past the edge of the window: the All clients Checks page and Projects
  (hidden text in their tables widened the page), and on phones the monthly review's buttons and the
  History list on person, mailbox, group and site pages, where a long address now wraps.

### Security

- Disconnecting someone's Microsoft account from their user page now needs the right to manage users, and
  only an Administrator can do it to an Administrator. It needed only the right to change settings.
- With staff passwords turned off, a staff account's right password now gets the same answer as a wrong one,
  and counts as a failed attempt.
- **Roles limited to some clients stay within them.** A custom role that sees only some clients, or a
  person given a capability for themselves, could reach other clients through what runs Office Sentry:
  client groups, editing a role held by someone with more access, the client import preview and team-wide
  settings. Now client groups can only be changed by someone who manages clients and sees every one; a role
  held by anyone who sees or does more than you can only be changed by an Administrator; the import preview
  leaves out other clients; and *Settings* and *System status* only work for someone who sees every client.
  The built-in roles were never affected.
- **A report area someone can't open stays closed everywhere.** A role without a report area (an Account
  manager has no identity reports, for example), or someone with an area taken away on one client, could
  still get that area's reports through a scheduled email, the report history or a report design, and the
  home pages counted findings they couldn't open. Now a schedule only sends reports its maker can open on
  that client, and only someone who can open everything a schedule sends can change it; the report history
  lists only reports the person can open and downloads need *Download files*; designs leave out tables the
  viewer can't open; and the Clients, Today and Accounts pages and a client's home page count and show only
  what the person may see. Every report email opens with the client's security summary, so setting one up,
  changing it or previewing it, and previewing alerts, now needs *Security findings and reviews*: First line
  no longer previews emails.
- **Draft commentary waits for approval in every email.** For a client whose month-end reports wait for
  approval, a monthly review attached to another scheduled email carried the month's commentary while it was
  still in review. It now carries it once the month is approved. Clients also no longer see which member of
  your team sent an email by hand, and staff-only notes follow *Review month-end reports* on each client.
- **Release images you can check by their version tag.** Each release's multi-platform image, the one its
  version tags point at and `docker pull` fetches, now has a signed build provenance of its own, so
  `gh attestation verify oci://ghcr.io/jackd99/officesentry:<version> --repo JackD99/OfficeSentry` passes
  ([SECURITY.md, "Checking a release"](SECURITY.md#checking-a-release)). Until now only each platform's build
  was signed. The release notes give the image's digest.
- The image no longer includes pip, which nothing uses once it's built and whose bundled copy trailed pip's
  own security fixes.
- The project's own GitHub workflows: the tools CI installs for itself come from a hash-pinned lock, and the
  jobs that publish images or deploy no longer leave their token in the checked-out code or build the website
  on the runner that holds the server's keys.
- The support package also sweeps the System status checks it includes, whose messages can quote a run's or a
  backup's error, and hides the portal's own address, the names a client or account had before it was renamed
  or deleted, the data folder's path, IPv6 addresses and ranges, and secrets written in quotes or as a header
  in printed data.
- Behind a reverse proxy the portal no longer takes a path prefix from the forwarded headers, and sends HSTS only
  when the visitor came over HTTPS, so a proxy that doesn't pass `X-Forwarded-Proto` shows by its absence
  ([Securing the portal](v2/docs/securing-the-portal.md#behind-a-reverse-proxy)). The brand colours and logo
  may be kept by the visitor's browser only, never a shared cache. The page that refuses a form sent with a
  stale token keeps its script, and email previews also refuse links to other base addresses and form targets.
- New tests attack client sign-in with Microsoft: a client's token can't reach another client, a staff
  account or a new account, and staff limited to some clients can't connect or disconnect anyone outside
  them. The isolation walk, the report area tests and the secrets crawl also run as a client signed in with
  Microsoft.
- Someone who manages users without being an Administrator, and has a capability taken away on one client,
  no longer counts as holding it everywhere: they can't give it to anyone, put it in a role, or change an
  account that holds it.
- The user pages no longer show a user manager who only sees some clients the client accounts of other
  clients, and the activity log shows a custom role that only sees some clients only the entries about
  those clients.
- The support package keeps clients as numbers for anyone who doesn't see every client: *Include client
  identifiers* is only offered to people who see them all.
- Webhook signing secrets must be at least 16 characters, since a short one can be worked out from a single
  signed request. A shorter secret already saved keeps working until it is changed; a long random one
  (`openssl rand -hex 32`) is best.
- Wrong passwords where a signed-in browser is asked for the password again (to change it, turn two-step
  sign-in off, or reset someone else's) no longer lock the account: the fifth signs that browser out. Someone
  holding a stolen session could otherwise keep its owner from signing in to end it.
- Recovery codes made before 2.2.0, still stored as a plain SHA-256, are put under the recovery code key when
  the portal starts, so a copy of the database can't be used to work them out. They keep working.
- New tests attack sign-in, sessions and forms on every route: a form sent without this browser's own token
  is refused, opening a page never changes stored data, sign-in answers the same whatever was wrong, and a
  copied cookie stops working whenever its session ends.

## [2.2.0] - 2026-10-11

Who can open what, and evidence for more than Cyber Essentials. The headlines: Site access, Site
permissions and File and folder permissions reports for SharePoint and OneDrive; seven more evidence packs
(IASME Cyber Assurance, ISO 27001, UK GDPR, CIS Microsoft 365, NIST CSF 2.0, SOC 2 and the Essential
Eight), with insurance and supplier questionnaires judged the same way; a page for every mailbox, group and
SharePoint site; roles, and staff sign-in with Microsoft; Teams, Mail flow, Administrative units and Licence
changes reports; and a monthly review that leads with the answer. Read **Action required** before updating:
uptime monitors and signed webhooks change, and nine new read-only Microsoft permissions are asked for once.

### Action required

- **Uptime monitors that put the monitoring token in the address** (`/health/ready?token=...`): the token
  now only works as a header, since addresses end up in proxy logs. Set the monitor to send
  `Authorization: Bearer <token>` and take the token out of its URL; until then it sees only whether Office
  Sentry is ready (200 or 503), not the checks ([v2/DEPLOY.md, "Is it working?"](v2/DEPLOY.md#is-it-working)).
- **Webhooks that check the signature** (a signing secret under *Settings → Alerts*): the signature now covers
  a timestamp, so a copied request can't be sent again. `X-OfficeSentry-Signature` is `v1=` and the HMAC-SHA256
  of `X-OfficeSentry-Timestamp`, a full stop and the body, instead of `sha256=` and the HMAC of the body, and
  `X-OfficeSentry-Key-Id` says which secret signed it. Change your receiver to check the new signature when you
  upgrade (it can accept either while you switch); until then it refuses every alert ([v2/DEPLOY.md, "Signed
  webhooks"](v2/DEPLOY.md#signed-webhooks)). Teams and Slack don't check signatures, so they need nothing.
- **Staff sign-in with Microsoft** needs nothing on upgrade: it's off until an admin sets it up.

### New Microsoft permissions

Nine read-only application permissions, released together so each client approves once. After updating,
press **Update the app** under *Settings → App connection* (or run `update-app`), then send each client the
approval link from its **What's new** page under *Onboarding* ([v2/DEPLOY.md, "When a release adds
permissions"](v2/DEPLOY.md#when-a-release-adds-permissions)). Until a client approves, everything else keeps
working and the reports that need them say they're waiting for approval.

| Permission | API | For |
|---|---|---|
| `Channel.ReadBasic.All` | Microsoft Graph | Teams channels, including private and shared ones |
| `ChannelMember.Read.All` | Microsoft Graph | Who is in each private and shared channel, guests and people from other organisations |
| `TeamsAppInstallation.ReadForTeam.All` | Microsoft Graph | Apps added to each team |
| `ReportSettings.Read.All` | Microsoft Graph | Whether usage reports hide people's names |
| `PrivilegedEligibilitySchedule.Read.AzureADGroup` | Microsoft Graph | People who can activate a privileged group's membership (PIM for groups) |
| `BitLockerKey.ReadBasic.All` | Microsoft Graph | Whether each device's BitLocker recovery key is saved (never the key) |
| `DeviceManagementConfiguration.Read.All` | Microsoft Graph | Intune configuration and compliance policies |
| `DeviceManagementApps.Read.All` | Microsoft Graph | Intune app protection policies and deployed apps |
| `Sites.Read.All` | Office 365 SharePoint Online | SharePoint group members and who has access to each site and list |

Setting up staff sign-in creates a separate app in your own tenant with no API permissions.

### Database upgrade

Yes, applied on start as usual: migration 37 adds a table for the search index, migration 38 adds
Microsoft account links on users and how each session was started, and migration 39 adds roles. Nobody's
access changes: today's admins become **Administrators**, analysts become **Second line** and client accounts
**Client viewers**, each holding exactly what the old role could do. As ever, a copy of the database is made
first. Migration 40, for the permission batch, adds a column on app connections and a table for each
client's approval.

### Added

- **A Get started checklist for new installs.** After creating the first admin, the home page lists the
  four steps to the first report (the app connection, the first client, its consent and its first
  collection), ticks each off as Office Sentry sees it done and goes away after the first collection.
- **Roles and capabilities.** What someone may do is now a role made of capabilities (which report areas
  open for them, downloads, refreshing data, accepting risks, setting up emailed reports, reviewing and
  approving month-end reports, projects, custom reports, managing clients, users or settings) and a scope
  (every client, or the clients and groups granted to them). Seven built-in roles: **Administrator**,
  **Team leader** (every client and every report, manages clients and approves month-end reports, not users
  or settings), **Second line** (what an analyst could do), **First line** (looks things up: people,
  identity, email and device reports, nothing to change), **Account manager** (licences, renewals,
  month-end reports and projects for their clients), **Auditor** (every report, read-only) and **Client
  viewer** (a client's own people). The user page picks a role and shows what the person can do, and an
  admin can give or take away single capabilities for one person, everywhere or on one client (taking away
  always wins; a client account only ever holds report capabilities). The menu, a client's tabs and the
  report picker show only what the person may open, every page and download checks on the server, and the
  cross-client pages leave out a client where a capability was taken away. There is always at least one
  Administrator: the last one can't be demoted, disabled or deleted.
- **Roles** (*Settings → Users → Roles*): change what a built-in role holds (not Administrator), put it back
  to its default, or add custom roles from scratch or as a copy of another, with a preview of the menu and
  client tabs the role opens. Changing a role signs out everyone holding it, so they pick it up at their next
  sign-in, and every change is in the activity log. On a person's page, **Customise** sets single
  capabilities for them: everywhere (as the role, allowed, or taken away) or on one client.
- Users and roles can be handed to someone who isn't an Administrator: they give out only what they hold
  themselves, only on the clients they see (other clients' names don't show), can't change their own role
  or access, and can't change an Administrator or anyone who can do or see more than they can.
- **Settings → Support** and a support package: one zip of the version, settings, health checks, recent
  failures and the portal's and worker's logs to attach to a bug report, with keys, certificates, tokens,
  addresses and client names taken out (clients appear as numbers unless you tick *Include client
  identifiers*). `python -m officesentry support-package` makes the same zip from the shell when the portal
  won't start, over a broken database or a configuration that won't load.
  [Getting help](v2/docs/getting-help.md) says what's in it.
- The portal and the worker keep a short rolling log in `<data dir>/logs/` (three files of 2 MB each), and
  every line says which request, user, client, run or task it belongs to. A page that fails shows a
  reference to quote; the same reference is on the error's line in the log and in the `X-Request-ID` header.
- **Site access** report (Files and Teams): who owns, administers and can reach each SharePoint site.
  Sites with no enabled owner or only one, each site's owners, administrators, members and guests, what
  the default library grants and to whom, every guest with the sites they can reach, and who administers
  many sites. A new check, "Every site has at least two owners", flags ownerless and single-owner sites.
  Read with the existing `Sites.Read.All` permission from each site's user list and default library;
  members of SharePoint groups on sites with no Microsoft 365 group are in Site permissions.
- **Site permissions** report (Files and Teams): each team site's SharePoint groups and their members, who
  holds which permission level, and the lists, libraries and subsites with their own permissions, with a check
  for guests with full control. Needs the SharePoint permission (Sites.Read.All on Office 365 SharePoint
  Online) from the next consent update; until a client approves it, the report says it's waiting.
- **Staff sign in with Microsoft.** Under *Settings → Staff sign-in*, an admin presses *Sign in to
  Microsoft* and Office Sentry creates a small sign-in app in the MSP's own tenant, with an app role for each
  built-in role (Admin for Administrator, TeamLeader, Analyst for Second line, FirstLine, AccountManager and
  Auditor) to assign people or groups to in Entra; a custom role gets an app role value of its own on the
  same page. The role follows Entra at every sign-in. Staff then press *Sign in with Microsoft* and use their
  work account, under the MSP's own Conditional Access and MFA; new staff get an account at their first
  sign-in, existing accounts link from their Account page. Passwords stay as the break-glass route until
  turned off, and `python -m officesentry staff-sign-in --passwords on` turns them back on from the
  server. Client accounts keep passwords. See [docs/staff-sign-in.md](v2/docs/staff-sign-in.md).
- **The monthly review leads with the answer.** The PDF and page now open with two summary pages: *At a
  glance* (what was checked, what changed, key figures, the severity bar) and *This month* (findings
  found and fixed each month as a chart, what was done, what to do next). Everything else (what changed
  in detail, the Secure Score trend, each failing check with the accounts affected, licences, passing
  checks, accepted risks, checks that couldn't run) follows as an appendix the summary links into.
  A **Summary PDF** button, and a "Monthly review: summary only" option in report plans and email
  schedules, give the two summary pages on their own.
- **Quarterly review.** A new starting point in the report designer (Month-end → Report designs): three
  months of Secure Score and findings, what was fixed in the quarter, the projects on the table and
  where each stands, licence spend a month and a year with the waste, and what's coming up in the next
  90 days. Its five blocks (Three-month trend, Fixed this quarter, Projects, Licence spend, Coming up)
  can go in any design, and text blocks take `{quarter}`. The demo data includes one under Custom
  reports.
- **What's new, for permissions.** When a release adds permissions, the Onboarding tracker lists the clients
  still to approve them ("New permissions to approve", then sent, then approved), and each client's **What's
  new** page says what every new permission reads and adds, with the email to send, the approval link and a
  PDF for the client. The App connection page updates your app registration with one sign-in (**Update the
  app**), and the new `update-app` command does the same. A client that hasn't approved yet stays *Healthy*:
  the missing permissions are a step to do, not a problem.
- **Teams channels, guests and apps** report (under *Files and Teams*): each team's private and shared channels
  with their members and owners, the guests and people from other organisations in them, the apps added to
  each team (custom uploads flagged), and teams nobody has used in 90 days. Usage reports also read your
  tenant's "conceal names" setting, so they say outright when Microsoft hides names.
- **Mail flow by domain** report (under *Email*): how much mail each domain received, sent and passed between
  colleagues, and how much was delivered, sent to Junk, quarantined or failed, per day for 30 days, with domains
  where more than 5% of mail fails flagged. It also lists Microsoft's top spam and phishing recipients for the
  last 30 days. Only counts are kept from the message trace, never subjects or addresses; it needs no new
  permission (Global Reader, as for distribution lists).
- **File and folder permissions** report (Files and Teams): who can open each shared file or folder in
  SharePoint and OneDrive and how (given access directly, sent a link, an anyone or organisation-wide link),
  who shared it and when, what was newly shared in the last 30 days, links past their expiry or expiring
  within 30 days, and access by person with guests and outside addresses first. The Shared files scan now
  records files and folders shared with colleagues too (so "who can access what" is complete), the sharer and
  date from Microsoft, and treats a link past its expiry as no access. A new check, "People outside can only
  view shared files", flags files and folders guests or outside addresses can edit.
- **The Shared files scan asks for changes only.** After a library's first walk, each night follows
  Microsoft's delta link for it and re-reads the permissions of changed items only; the rest keep last
  night's answer. Every library is walked whole again after 30 days (or when Microsoft drops the link), so a
  large client's nightly scan is a fraction of what it was.
- **Administrative units** report (Identity and access): every unit with its membership (assigned or dynamic,
  and whether the rule is paused), restricted management units, how many users, groups and devices are in
  each and which admins are scoped to it; every scoped admin, flagged when the account is blocked or a guest;
  and the members of each unit by name. A new Administrative units collector reads them with the permissions
  the app already has, and each person's page shows the units they are in and administer.
- One page per mailbox: Users > Mailboxes lists every mailbox with its size, forwarding, who else can
  open it and its inbox rules, and each opens a page with what needs attention first, then size and growth
  over the retained months, forwarding and inbox rules, who can open or send from it, protocols and
  settings, its automatic reply, the phones syncing it, email activity, open findings and what changed.
  Mailbox addresses in reports and search results open the page, and it downloads as PDF and XLSX.
- One page per group: Users > Groups and teams lists every team, Microsoft 365 group, security group and
  distribution list with its owners, members, guests and membership type, and each opens a page with what
  needs attention first, then its owners, members and the groups inside it, what membership gives
  (Conditional Access policies, admin roles, licences), its mail settings and traffic, the team's site with
  its headline sharing figures, how it's set up, admin changes, open findings and what changed. Group names
  and addresses in reports, the person page and search results open the page, and it downloads as PDF and
  XLSX.
- The **Empty, nested and dynamic groups** report now also lists teams and Microsoft 365 groups the
  expiration policy will delete within 90 days (with their owners, flagged when no owner can sign in to
  renew them), role-assignable groups (whose members hold admin roles, flagged when a guest is in one) and
  every group that still holds a blocked account, naming the accounts. Group nesting keeps the member IDs
  of security groups and distribution lists for that. **Coming up** and its weekly email list the teams and
  groups about to expire alongside renewals and app secrets.
- **Licence changes** report (Tenant and licences): every paid licence given to or taken from an account, with
  when it happened (to the day for the last month, to the month before that), whether it came directly or
  from a group, and, where the directory audit log has been read, who made the change. Licences bought and
  assigned month by month for the last year with what they cost at your prices, a chart of assigned licences
  against bought, and how bought licences and the monthly cost moved since a month ago, for checking a
  client's bill. Built from Office Sentry's own history; no new permission. The monthly review's "since last
  month" changes and What changed now list who was given or lost a licence.
- **Deploy guides by platform** ([v2/docs/deploy](v2/docs/deploy/README.md), and under *Deploy* on the
  website): Portainer; Dockge, Komodo, Coolify and Dokploy; Unraid, Synology and TrueNAS; Kubernetes (AKS,
  EKS, GKE, k3s and others); Azure and AWS virtual machines; DigitalOcean, Google Cloud, Linode, Vultr,
  Oracle Cloud and other VPS hosts; and Cloudflare (DNS only, a Tunnel with Access, or proxied). With them
  come a one-file stack for container managers and NAS systems (`v2/deploy/stack/compose.yaml`), Kubernetes
  manifests (`v2/deploy/kubernetes/`) and a start-up file for a new cloud server
  (`v2/deploy/cloud-init-server.yaml`). Each guide says which of its steps have been tested.
- One page per SharePoint site: Users > Sites lists every site with its storage, files, sharing links, last
  activity and open findings, and each opens a page with what needs attention first, then its storage over the
  retained months with the libraries and largest files that fill it, its sharing settings and the files shared
  from it, the team or Microsoft 365 group behind it (or its owner), its activity, open findings and what
  changed, with links to the Site access, Site permissions and File and folder permissions reports for who
  can reach it. Site addresses in
  reports, the group page and search results open the page, and it downloads as PDF and XLSX.
- Four checks in areas that were thin. **Accounts:** no account reports to a manager whose sign-in is
  blocked. **Guests:** no guest from another organisation owns a team or group. **Storage:** no mailbox and
  no OneDrive is at or over 90% of its quota (high once it's full). Each goes in the project an MSP would sell
  it under (account clean-up, sharing and guests, storage), with an effort estimate.
- Evidence by certification: a client's Evidence page now starts with the scheme they are going for.
  Cyber Essentials and Cyber Essentials Plus (IASME question set version 16, April 2026, on Requirements for
  IT Infrastructure v3.3) are judged from the Microsoft 365 data as an assessor would, control by control:
  Pass, Fail (any gap fails, auto-fail questions marked), Partly, or Your evidence where Microsoft 365 can't
  show it, with the figures, the fix and a text to paste. The page leads with what to fix before applying,
  compares with last month, three months or a year ago, lists what the CE Plus assessor tests, and carries
  the answers for the IASME portal with one-tap copy. Downloads: PDF, Word, plain text, XLSX and CSV.
- The cyber insurance proposal and the supplier security questionnaire are judged the same way, as two more
  packs on the Evidence page: each control Pass, Fail, Partly or Your evidence (a gap on a few accounts is
  Partly, since insurers price the risk rather than refuse it), with the figures, the fix and the form's
  answers to paste. The emailed
  "Cyber insurance evidence pack" is now this pack; the old controls view and its links open it.
- Three more packs on the Evidence page. **IASME Cyber Assurance** (Level 1 and Level 2), theme by theme from
  governance to incidents, judged strictly like Cyber Essentials. **ISO/IEC 27001:2022 Annex A**: the
  controls Microsoft 365 evidences (5.14 to 5.23, 6.5, 6.7, 7.9, 8.1 to 8.16 and more) with their ISO numbers,
  for the Statement of Applicability and the audit. **UK GDPR security and accountability**: the ICO's
  security outcomes (A to D) plus the records a client or the ICO asks for after a breach (record of
  processing, processor contracts, privacy notice, rights requests, DPIAs, retention, the ICO fee and
  72-hour breach reporting). Beyond the UK: the **CIS Microsoft 365 Foundations Benchmark** (the
  recommendations Office Sentry reads, all judged from the data), **NIST CSF 2.0** by category, **SOC 2**
  Trust Services Criteria (Security and Availability) and Australia's **Essential Eight** at Maturity Level
  One. Twenty-one new fill-in answers cover the governance, data protection and hardening questions
  Microsoft 365 can't see, each written once and reused by every pack.

### Changed

- [Running it on your own server](v2/DEPLOY.md) no longer has the Hetzner walkthrough, which was written for
  the project's own server. Off-site backups, the restore drill and restoring from the off-site copy are now
  written for any server, with `deploy/jobs.sh` run in the folder with `docker-compose.yml`.

- Pages that show every client at once stay quick as the number of clients grows. The insurance evidence
  rollup, every report's All clients rollup, the month-end queue, Recommended projects and the Accounts home
  page now read rows the worker keeps up to date after each collection, as What changed and the check
  matrix already did; a row is rebuilt the moment anything behind it changes, so nothing shown is older
  than the live page would be. At 200 clients the Accounts page went from 6 seconds to half a second, the
  month-end queue from 3.4 seconds to under a tenth, the insurance evidence rollup from 4 seconds to under
  a tenth, and a report's All clients rollup from about a second to under a tenth.
- The sidebar search reads an index the worker keeps, instead of scanning every client's data on each
  search. A client whose index is behind its latest collection is still scanned, so nothing is missed.
- PDFs of an All clients rollup, What changed and a client's change report keep the first 2,000 rows and
  say so, as table reports already did, so a very large client can't run a download into the server's
  timeout. The spreadsheet downloads still have every row.
- `DEPLOY.md` gives measured page, download and worker times at 200 clients ("How fast"), and
  `scripts/timings.py` measures them.
- **"Clients" for the companies you look after.** Pages, menus, emails, PDFs, spreadsheets and the docs now
  say client where they said tenant; "tenant" is kept only for the Microsoft 365 side (Tenant ID, consent in
  the client's tenant). In the menu, Tenants is now **Clients**, All tenants is **All clients**, and under
  Settings, Client tenants is **Client settings** and Tenant groups is **Client groups**. The account
  manager's home page (was "Clients") is now **Accounts**. Columns headed "Tenant" are now "Client";
  saved views and bookmarked links that filter or sort on that column keep working. The group digest is
  now the "Client digest", and its spreadsheet is named `client-digest-...`.
- **Shorter pages.** A page with several long tables shows the first 10 rows of each on a laptop (5 on a
  phone) with "Show all N rows" under it, so the next table is a scroll away; a page with one long table
  keeps every row on a laptop and 20 on a phone. A cell listing more than three accounts or items shows the
  first two and "and N more", which opens the rest. On a phone, short values in a stacked table sit beside
  their label instead of under it. The SharePoint storage and Sign-ins reports went from about 35,000 pixels
  tall on a phone to under 15,000, and Devices from 21,000 to 6,000.
- **A person's page, and the new mailbox and group pages,** open with a summary (what needs attention and
  open findings beside the account's facts), then each part at the page's full width, so no table is
  squeezed into a narrow column. Every part folds to its heading: open on a laptop, folded on a phone, and a
  "Details" link opens the part it points at.
- **Data collection** lists the sources that need a look first and folds away the ones that collected fine;
  the **month-end review** shows each failing check on one line with "Add a note" at its end.
- A user's settings show what their role opens beside the form on a wide screen.
- Report tables and a person's page show dates as "8 Oct 2026, 09:22" rather than `2026-10-08T09:22:00Z`.
- **Reports use the screen.** Pages can be 1,600 pixels wide instead of 1,240, and the list of reports is an
  "All reports" menu above the report (every report in columns, with a search box, and the report's area
  beside it) instead of a column beside it, so a report's table gets the page's width. "Show as it was on"
  sits on the same row. On a 1920 screen the All users table shows 9 of its 11 columns instead of 6.
- In report tables a plain flag is a small label beside the row's first value instead of a column that's
  empty on most rows (named ones, such as Severity on Findings, stay columns). In wide tables, columns that
  explain a row ("What it means", "What to check", "Why" ...) are a line under it, so the facts stay side by
  side; downloads keep them as columns. The links to a report's tables look like buttons.

### Fixed

- Nothing on a phone is wider than the screen any more: the person page, a second client's overview and the
  offboarding page scrolled sideways because a long address or a two-column grid didn't fit.
- In the monthly review on a phone, the account or item in each finding was squeezed into a third of the
  card, one word per line; it now uses the card's width, with its severity beside it.

- An Admin activity collection could be marked partial, with a warning that Microsoft timed out, when a
  second ticked over between two readings of the clock during the run; nothing was actually missing.
- DKIM is now judged for every domain Microsoft 365 sends for, not only domains whose MX records point at
  Microsoft 365. A domain behind a mail gateway (Mimecast, Proofpoint and the like) whose SPF record still
  includes `spf.protection.outlook.com` has its selector CNAMEs checked, and the finding says why; a domain
  Microsoft 365 doesn't send for stays "n/a". Before, those domains were skipped and the check could read "No
  domain receives mail through Microsoft 365" for a tenant that sends everything from Outlook.
- Admins who hold their role through a group (a role-assignable group, active or eligible through PIM) were
  invisible to the admin checks and the Admins report. The group's members are now read each night and
  checked like any other admin; the Admins report says which group a role comes through.
- A Conditional Access policy that accepts "MFA **or** a compliant device" no longer counts as requiring MFA,
  since a compliant device lets people in without it. The finding names the policy and the other choice.
- MFA policies that leave out roles or guests are now listed. Leaving out admin roles is only raised when no
  other policy requires MFA of the people holding them.
- The Global Administrator count leaves out disabled accounts and apps, and no active Global Administrator
  is its own finding instead of reading "Only one".
- Guests that can activate an admin role through PIM are flagged like guests that hold one, and a guest or
  app with several roles is one finding naming them all.
- MFA coverage treats only active privileged roles as admin roles, so a person with a read-only role
  (Directory Readers, say) gets the same severity there as on the Findings tab.
- The Admins report no longer hides Office Sentry's own app when it holds a privileged role: it only needs
  Global Reader.
- Shared mailboxes that need their licence (an archive, a hold or over 50 GB) aren't flagged as disabled
  accounts holding one.
- An inbox rule that moves mail mentioning payments, or marks it as read, into a folder Office Sentry
  couldn't name (without the Exchange role) is raised for review instead of passing.
- Inbox rules: an account with no Exchange Online mailbox no longer makes the collection partial every night
  on tenants that haven't given the app the Exchange role.
- SharePoint refusing the app while Sites.Read.All is granted is reported as SharePoint's own restriction,
  not as a missing permission to re-consent.
- When a tenant's Entra ID P1 licence isn't known, every collector now asks for the P1 data and lets
  Microsoft's answer decide, instead of some skipping it.
- A device that last checked in today, and dates written as "3 Sep 2026", are now dates to the table
  filters ("In the last 30 days") and sort as dates.
- A view-only anyone link with an expiry date is medium on the Shared files report, as on the finding.
- An accepted risk's review date must be a real date: one like 31 February would never come round.
- Outbound forwarding fails only when the default policy allows it. A scoped policy that allows it for named
  mailboxes, which is what the recommendation asks for, passes and is named in the summary.
- A mailbox forwarding to a recipient Office Sentry couldn't look up is flagged, instead of counting as
  internal.
- A disabled app with risky permissions is low, and says it's disabled. Entra Connect's own provisioning
  certificates, which it renews itself, are only flagged in their last 7 days.
- A custom user consent policy is flagged for review rather than passed as "low-risk permissions", and the
  Guests report names it.
- Guest invitations are "couldn't check" when Microsoft doesn't say who can invite, rather than a pass.
- Devices Intune couldn't evaluate (error, conflict, unknown) are low findings, since Conditional Access
  blocks them like non-compliant ones.
- Risky sign-ins is "couldn't check" when the sign-in log was cut short and nothing was found.
- Active trial licences are no longer costed, or counted as unused, in Licences and subscriptions, Licence
  waste, the unassigned licences check and the monthly review.
- What changed tells two subscriptions of one licence apart, so a seat change on one is one changed row
  rather than one added and one removed.
- What changed no longer lists every mailbox, OneDrive, site and library because its size or file count
  moved. Those columns are compared like day counts; a mailbox or site that crosses into a table (nearly
  full, say) still shows.

- Wide report tables take the width their columns need and scroll sideways, with the flag and name
  staying in view and a shaded edge where there is more, instead of squeezing their text column into a
  few words a line. The settings links wrap on laptops instead of running off the page, the rows of
  choices on Onboarding, Month-end and Questionnaires and evidence look like the rest, and counts on
  those rows and the warning lines of a run log are readable.
- A report email retried after midnight, sent again with Retry the next week, or approved late now names
  the month, week or quarter it was for, and carries that month's commentary, instead of the day it went out.
- A tenant nothing has been collected for yet is no longer emailed an empty report: the send fails with the
  reason and is retried.
- Send now and Retry wait while the timer is already sending that schedule's period, so a period can't go
  out twice. A failed Send now is logged as a failure.
- Approving a month-end report for a client with nobody to email no longer marks the month Sent: it stays
  Approved, flagged "Nobody to email" in the queue, with a Send again once a contact is added.
- A month-end email to a client with more than 20 contacts keeps the team's hidden copy, and Monthly report
  coverage names the contacts left off.
- The weekly digest's count of new critical and high findings leaves out accepted risks, as its list does.
- The plain-text part of a report email leads with the verdict and says when the data is old.
- Pausing and resuming an email schedule is saved as one change.
- The collection check and its alert no longer clear on a night every client failed just because one
  client's report refresh ran since.
- A system alert or certificate warning that no email or webhook took is tried again after 15 minutes,
  rather than being marked sent.
- Turning off off-site backups no longer leaves the Backups check warning for good: the nightly job and the
  restore drill record that they aren't set up.
- The server's uptime ping asks `/health/ready`, so the monitor hears when the worker stops or the disk
  fills, not only when the portal is down.
- `restore` now works over a damaged database: the damaged file is moved aside whole as
  `backups/pre-restore-<time>-damaged.db` and the backup goes in. A locked database, or no room for the
  safety copy, makes it refuse and say why, changing nothing. `deploy/jobs.sh restore` leaves the portal
  and worker stopped when the restore fails, rather than starting them over an empty database.
- A nightly backup whose activity history copy failed says the database was copied and what failed, rather
  than "No backup made", and still removes old copies.
- One huge client can't make the worker look down. The shared-files scan has a night's share of pages and
  time (about a million items or two hours); the libraries left over keep their last results and are
  scanned first the next night. The daily pass (retention, the dashboard rows) runs beside the worker's main
  loop rather than in it, and a run that finds the database locked as it's recorded is tried again for a
  few minutes rather than failed later as "worker stopped".
- A database upgrade on a large database no longer fails the deploy: the portal's health check allows 15
  minutes to start, the start script waits that long and then starts the worker, and Docker keeps the
  containers running while it is itself updated (`live-restore`, on new servers).
- The file pickers on Branding and the onboarding import look like the portal's other buttons and show
  the chosen file's name, the Activity log's filters sit level on one row with their button, the partial-run
  notice carries the same icon as other notices, each Data retention section says plainly what the next pass would
  delete, the onboarding "Send link" button has its full label, and a report plan's Save and Cancel stay in view
  on a long form like the scheduled-email form's.
- A deploy no longer copies the portal's settings files, among them the off-site backup password, into
  the public demo's folder on the server, and removes the copies earlier deploys left. The temporary SSH
  rule it opens in the Hetzner firewall is removed by a second job too, in case GitHub loses the deploy
  job's runner before it can close it.
- A long write to the database (a restore, a backup, a large delete) no longer makes every page fail:
  pages don't wait for it to note a sign-in was seen or to store a dashboard row, and a change that
  still finds the database locked after five seconds is answered with "Office Sentry is busy right
  now, try again in a moment" instead of an error page.
- On Windows without Docker, an account's page no longer fails once its sign-in history is kept: the
  dates on it are written the same way on every platform.
- A tenant whose consent is missing the AuditLog.Read.All permission still gets its users collected each
  night, with the run partial and the permission named, instead of the Users collection failing.
- An Exchange list longer than 200 pages (200,000 results) is cut there with a warning naming the
  cmdlet, instead of failing the whole collection.
- A session or proxy setting that can't work (`OFFICESENTRY_SESSION_HOURS=0`, which signed everyone out
  on their next request) stops the portal at start with a plain message, like the other settings.
- A form sent after the session had ended comes back, after signing in, to the page the form was on
  rather than to an address that only takes forms ("Method not allowed").
- The daily rebuild of the What changed and check matrix rows is no longer lost for a client whose
  rows were being refreshed at that moment.
- The advice for Cloudflare or another CDN in front of the proxy (`OFFICESENTRY_TRUSTED_PROXIES=2` alone)
  left every visitor looking like the proxy, with the bundled Caddy and the nginx example alike, so they
  all shared one address's sign-in limits. [Securing the
  portal](v2/docs/securing-the-portal.md#behind-a-reverse-proxy) now says what Caddy, nginx and Traefik each
  need to trust the CDN's addresses.

### Security

- Resetting someone else's password, two-step sign-in or sessions from their user page asks for the admin's
  own password, as the account page does before the same changes, so a stolen admin session isn't enough.
  Wrong answers count towards the admin's own sign-in lock.
- Recovery codes are stored as an HMAC under a random key kept encrypted with your encryption key, rather
  than a plain SHA-256, so a copy of the database alone can't be used to guess them. Codes made before this
  release keep working; making new ones on the account page stores them the new way.
- The two-step setup page makes a new authenticator secret each time it's opened, so someone who saw the
  page earlier (with a stolen session, say) doesn't know the secret the person goes on to add.
- Changing an app connection's client ID (a different app registration) no longer leaves its tenants marked
  as consented: each one needs the client to approve the new app, links already sent are marked not sent
  (they name the old app), and the page says how many clients must approve again.
- The public demo no longer keeps a visitor's address in the activity log when they open or download a
  report; what was opened is still recorded.
- Deleting a client no longer rebuilds the whole database inside the web request, which failed for want of
  room in Docker and left the client's data readable on disk while saying it was erased. The delete is
  quick, and the worker erases the last copies in the background, rebuilding the file only when there's
  room. If that can't be done, System status says so under **Deleted clients**, the team is alerted, and
  it's tried again every hour.
- Every response now carries `Cross-Origin-Opener-Policy` and `Cross-Origin-Resource-Policy: same-origin`
  and `X-Permitted-Cross-Domain-Policies: none`, and the bundled Caddy drops the `Server` header.
- The portal's access log (Docker and `serve`) records the path only, not the query string or the referer,
  which carried search terms, filter values and the one-time setup link.
- The containers get a process limit, and the bundled Caddy runs with every capability but binding its
  ports dropped, like the portal and worker.
- The Deploy workflow reads the server's address and the portal's name from the secrets `DEPLOY_HOST` and
  `OFFICESENTRY_DOMAIN` (and `OFFICESENTRY_SETTINGS` and `BACKUP_REPOSITORY` when they're secrets), since a
  public repository's run logs show every variable a step reads. Variables of those names still work; add
  each as a secret to hide it. The
  live canary takes the test tenant's ID from the secret `CANARY_TENANT_ID` for the same reason.
- The activity log now records pages people weren't allowed to open (another client's tenant, a staff-only
  page) and forms sent with a stale token, counted per person, address and hour with the latest page named,
  so a client account probing for other clients' data is visible under *Settings → Activity log*.
- Form-token, wrong-method, oversized-upload and server-error responses use the portal's own error page with
  plain wording, instead of the web server's.
- Passwords on lists of commonly used passwords (`password1234`, `qwerty123456` and 20,000 more of 12
  characters or longer), or containing the person's username or "officesentry", are refused wherever a
  password is chosen: first-run setup, the account page, the forced change after an admin's reset, and the
  admin's user forms. Existing passwords aren't checked until they're changed.
- A new docs page, [Securing the portal](v2/docs/securing-the-portal.md), lists what the portal does by
  itself, what the person running it has to do, and how to put it behind Caddy, nginx, Traefik or a CDN.
- The Deploy workflow gives the `HCLOUD_TOKEN` secret only to the two steps that open and close SSH, passes
  the firewall's ID between them as a checked number instead of through the environment of every later step,
  and writes the backup password and storage keys to disk only after the website is built, so the code it
  builds and the packages that build installs never see them. Nothing to change in your settings.
- Copy buttons put their own label back after "Copied" instead of re-reading it from the page as HTML.

## [2.1.0] - 2026-10-10

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
  variable that makes the portal take release tags only, as the published image pinned by digest.
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
- Tenants on Office 365 G3 (GCC), A3 education or F3 frontline plans are no longer treated as having no
  Exchange or SharePoint, which left their mailbox, inbox rule and SharePoint reports empty.
- Windows Server is no longer reported as Windows 10 past end of support (or Server 2025 as Windows 11),
  on the Devices report or in the supported Windows check.
- An evidence pack control whose only check fails for the whole organisation (mailbox auditing turned off)
  reads "Not in place", as the questionnaire answer does, instead of "Partly in place".
- Re-issuing a month sends only the files of the new approval: a report since dropped from the plan, or a
  file named after the tenant's old name, is no longer attached again.
- What changed no longer lists every inactive account, deleted item and renewal as changed because its day
  count moved with the calendar.

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

## [2.0.0] - 2026-10-09

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

[2.4.0]: https://github.com/JackD99/OfficeSentry/compare/v2.3.1...HEAD
[2.3.1]: https://github.com/JackD99/OfficeSentry/compare/v2.3.0...v2.3.1
[2.3.0]: https://github.com/JackD99/OfficeSentry/compare/v2.2.0...v2.3.0
[2.2.0]: https://github.com/JackD99/OfficeSentry/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/JackD99/OfficeSentry/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/JackD99/OfficeSentry/releases/tag/v2.0.0
