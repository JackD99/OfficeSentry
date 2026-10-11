# Office Sentry v2

Read-only Microsoft 365 reporting for MSPs: nightly snapshots of every
client's Microsoft 365, cross-client rollups, month-over-month changes and
branded client reports. The [root README](../README.md) is the product page
with screenshots; this page lists every feature in detail and covers setup
and development. Running it on a server is in [DEPLOY.md](DEPLOY.md), and on
Portainer, a NAS, Kubernetes or a cloud in the [deploy guides](docs/deploy/README.md).

## Reports

- **Monthly Security & Licensing Review** (`/tenants/<id>/report`): two summary
  pages that lead with the answer (what was checked and what changed, key figures
  with their 4-week change, findings found and fixed each month, what was done
  this month and what to do next), then an appendix with the detail: what
  changed, the Secure Score trend, failing checks with affected accounts and
  recommendations, licence waste in money, passing checks, checks that the
  client's licences can't support, and data freshness. The *Summary PDF* (and the
  "Monthly review: summary only" option in plans and schedules) is the two
  summary pages on their own.
- **Reports** (`/reports`): every report in one searchable page, by area and by
  job ("Offboarding check", "Insurance renewal", "Storage cleanup"...), opened
  for one client or across all of them. The search also finds clients and
  settings, and lists your saved views.
- **All clients** (`/across/<report>`): a sortable, filterable table with one row
  per client for the monthly review, the insurance evidence pack and every
  detail report, limited to the clients you can see. New detail reports appear
  automatically.
- **What changed** (`/changes`, and each client's *Changes* tab): a standalone
  month-over-month report for the last 7, 30 or 90 days. It covers key
  figures then and now, new and fixed findings, admin, account, Conditional
  Access, forwarding, app, device and group changes, and a row-by-row
  comparison of every detail report.
- **Downloads**: every page above has CSV and XLSX. Values from clients can
  never become spreadsheet formulas.
- **Licence prices** (admin): enter monthly prices per SKU to cost waste and
  right-sizing savings.
- **Findings** (`/tenants/<id>/findings`): every finding keeps first seen,
  resolved and reopened dates. Analysts can accept a risk with a reason and an
  optional expiry; accepted risks leave the totals and get their own section.
- **Since last month**: new and fixed findings, admin role changes, new,
  disabled and deleted accounts, licences people were given or lost, and
  Conditional Access policies added, switched or edited, compared with the
  snapshot from 4+ weeks earlier.
- **Emailed reports** (a client's *Emailed reports* tab, and *Email reports*
  for everything at once):
  - Send any mix of reports (the monthly review, the evidence pack, any detail
    report) weekly, monthly or quarterly, at a time in the schedule's own timezone.
  - **Saved views** too: a report with your filters, columns and sort, as a PDF,
    XLSX or CSV, for one client, or with every client's rows (or one group's)
    for your team.
  - Recipients are contacts: the client's people get the email, your team gets a
    hidden copy. Optional note at the top of the email.
  - A cross-client **digest** for your team, worst clients first.
  - A weekly **Coming up** email (and page) for your team: renewals and trials, expiring app secrets and
    certificates, teams and groups about to expire, mailboxes filling up, DMARC still at p=none and accepted
    risks due in the next 90 days.
  - **Alert emails** when a scan finds something new and critical (or high), for
    one client or all of them. They come from the same alert event as the
    Teams/Slack alerts below. A client's first scan never emails.
  - Preview (desktop and phone width), Send now, pause, and a delivery history
    with failures, automatic retries (5 min, 30 min, 2 h) and a Retry button.
  - Sent over SMTP or Microsoft 365 (Graph `sendMail` from a mailbox in your own
    tenant, through a separate send-only app), under Settings → Email delivery.
- **Alerts** (Admin → Alerts): a Microsoft Teams (Workflows), Slack or generic
  JSON webhook message when a client gets new findings at or above a chosen
  severity (one message per client per collection), when a collection fails
  after working last time, optionally when it works again, and optionally when a
  client's report is ready ("The October 2026 report for Contoso is ready", a
  link to the report history, never the files). The webhook URL
  and optional HMAC signing secret are stored encrypted; signed requests carry
  a timestamp and key id, so a receiver can refuse a replayed one (DEPLOY.md,
  "Signed webhooks"); only https URLs that
  resolve to public addresses are called (`OFFICESENTRY_ALLOW_PRIVATE_WEBHOOKS=1`
  allows internal ones).
- **System status and system alerts** (Settings → System status): whether the
  worker runs, data is being collected, backups are recent and there's room on
  the disk, with what to do about anything that isn't. The team is emailed and
  the alert webhook posted when the worker stops, a backup fails, the disk is
  over 85% full or most clients fail to collect; once when it starts, daily
  while it lasts and when it's fixed. `/health/ready` answers 503 for uptime
  monitors when the portal can't do its job (DEPLOY.md, "Is it working?").
- **Detail reports** (`/tenants/<id>/data/<report>`, each also as PDF, XLSX and CSV),
  grouped by area (identity, email, files, devices, tenant). Every table can be
  filtered by the values in it (or by age, for dates), sorted, and have columns
  hidden; headers stay visible while scrolling, and the view can be saved by
  name and reused on any client. Downloads contain the rows and columns shown.
  The busiest tables have a bar chart that follows the filters (click a bar for
  its rows), and headline figures carry a line of their last 90 days.
  Every headline number opens the rows behind it, here and on All clients:
  - *Mail forwarding*: every forward (admin-set or inbox rule), and the
    outbound spam policies and remote domains that allow or block it.
  - *Inbox rules*: rules that forward outside, delete or hide mail, every
    rule that sends mail elsewhere, and every rule in every mailbox.
  - *Mailbox permissions*: full access, send as and send on behalf, by person
    and by mailbox, with blocked, guest and outside accounts flagged.
  - *Shared mailboxes*: shared, room and equipment mailboxes whose account can
    be signed in to, their last sign-in, who has access, and their size.
  - *Mailbox size*: each mailbox's size against its send quota
    (`Get-MailboxStatistics`), with mailboxes over 90% flagged.
  - *Mailbox size trend*: each mailbox's growth over one and three months
    from Office Sentry's own kept snapshots, the date each will fill at that
    rate, and the client's total month by month.
  - *Automatic replies*: mailboxes sending an out-of-office reply (Graph,
    `MailboxSettings.Read`), who outside hears it, and replies on for over 30
    days on an active account, dated from the kept snapshots.
  - *Mobile devices*: phones and tablets syncing mail (`Get-MobileDevice` and
    `Get-MobileDeviceStatistics`), with which app and when each last synced;
    leavers' devices still connected, ones waiting for approval and ones
    unused for 90 days come first.
  - *Distribution lists*: members, owners and who outside can mail each list,
    with empty, ownerless and unused lists flagged. How much mail each gets is
    counted from the message trace (`Get-MessageTraceV2`, read 10 days at a
    time from where the last run stopped) and kept per day for 90 days.
  - *Mail flow by domain*: for each of the tenant's domains, how much mail
    came in, went out and passed between colleagues, and how much was
    delivered, sent to Junk, quarantined or failed, counted per day from the
    message trace and kept for 30 days (only counts, never subjects or
    addresses). Domains where more than 5% of mail fails are flagged. Also
    Microsoft's top 20 spam and phishing recipients for the last 30 days
    (`Get-MailTrafficSummaryReport`).
  - *Legacy sign-ins*: SMTP AUTH, IMAP, POP and other basic-auth sign-ins from
    the last 30 days of the sign-in log (Entra ID P1), and which mailboxes
    still allow SMTP AUTH, POP and IMAP.
  - *Sign-in activity*: the last 7 days of interactive sign-ins from the
    Entra ID sign-in log (Entra ID P1), summarised rather than stored: accounts
    with repeated failures (wrong passwords, lockouts), addresses failing
    against several accounts (password spraying), successful sign-ins from
    countries the tenant rarely uses, successful sign-ins that needed only a
    password, failure reasons in plain English (MFA and other prompts counted
    apart from failures), and sign-ins by country, app and account. With
    Entra ID P2, risky sign-ins too. Each run reads on from where the last
    one stopped, into the activity history below, and says which part is
    still to read if it stops. Each account's page shows its own summary.
  - *Risky users* (Entra ID P2): accounts Entra ID Protection rates at risk
    or confirmed compromised, with the risk level and why, then the risk
    remediated, dismissed or confirmed safe in the last 90 days and how. A
    check fails while any account is at risk, and the account's page flags it.
  - *Admin activity*: security-relevant changes from the Entra ID directory
    audit log over the last 12 months of the activity history (Microsoft keeps
    30 days, or 7 without Entra ID P1, so the history starts when Office Sentry
    first read the tenant): admin roles granted or removed (including PIM), Conditional
    Access policies added, changed or deleted, consents and app permissions,
    secrets and certificates added to apps, domain and federation changes,
    admin password resets, deleted users and security info changed by admins.
    Federation changes, new app credentials, role grants and Conditional Access
    policies deleted or switched off are listed first, then every change and
    who made them.
  - *Activity history* (Changes → Activity history, staff only): every
    sign-in and directory audit event Office Sentry has read, kept beyond the
    30 days Microsoft keeps them (sign-ins 180 days, daily summaries 25
    months, audit events 7 years by default): how far back each is detailed,
    the last 30 days, sign-ins per day, each month with a CSV of every
    sign-in, and admin changes worth a second look. Each person's page shows
    their sign-ins by month from it.
  - *Apps and consents*: third-party apps with what they were granted
    (application permissions, tenant-wide and per-user consent), and app
    registration secrets or certificates expiring.
  - *Unused apps*: every app from outside Microsoft with when it last signed
    in (signing people in, on its own, or called as an API), from Entra ID's
    app sign-in activity (needs Entra ID P1). Apps unused for 90 days that
    still hold application permissions or consent are flagged.
  - *All users*: every account with its department, job title, manager,
    licences, source (cloud or synced) and last sign-in; each manager with
    their direct reports; member counts by department. Accounts reporting to
    a manager whose sign-in is blocked are flagged.
  - *Administrative units*: every unit with its membership (assigned or
    dynamic, and whether the rule is paused), restricted management units, how
    many users, groups and devices are in each and which admins are scoped to
    it; every scoped admin, flagged when the account is blocked or a guest;
    and the members of each unit by name. Each person's page shows the units
    they are in and administer.
  - *Conditional Access coverage*: every policy in plain words, and for each
    account which enforced policy requires MFA, whether legacy sign-in is
    blocked, and which policies exclude it (group membership expanded).
  - *Passwords and sign-in methods*: how each member signs in and how strong
    that is (phishing-resistant: passkey, security key, Windows Hello or
    certificate; Authenticator app or token; phone only; none), admins
    without a phishing-resistant method, people whose only second factor is a
    text or call, who can sign in without a password or reset their own, and
    the method each person is asked for first. The tenant's authentication
    methods policy (which methods are on and for whom, SMS, voice and email
    one-time passcodes, the registration campaign, system-preferred MFA)
    against what's recommended, and the authentication strengths Conditional
    Access requires. Password hygiene: password age, passwords set to never
    expire, accounts that never signed in with a password set long ago, and
    each domain's password expiry policy. Who hasn't set up MFA at all stays
    in *MFA coverage*.
  - *Tenant settings*: what users and guests can do without an admin
    (create tenants, register apps, join by verifying an email address,
    sign up for trials, guest directory access) against the recommended
    value, and the admin consent request workflow (reviewers, emails,
    expiry).
  - *Guests and sharing*: invitation, consent and SharePoint sharing settings,
    and every guest with invitation state and last sign-in.
  - *Deleted users and groups*: the Entra ID recycle bin. Every deleted user
    (member or guest, the licences it held) and deleted team or Microsoft 365
    group, when it was deleted and the days left before Microsoft removes it
    for good after 30 days. Licensed members and teams with 7 days or less
    left are flagged. Restoring is done in the Entra or Microsoft 365 admin
    centre; Office Sentry only reads the recycle bin.
  - *Groups and Teams*: who can create teams and groups, whether guests are
    allowed and unused groups expire, teams and Microsoft 365 groups with no
    enabled owner or a single one, every team and group with its owners,
    members and guests, every guest in a team or group, and counts of security
    groups and distribution lists.
  - *Teams channels, guests and apps*: each team's private and shared
    channels with their members, owners, guests and people from other
    organisations, the apps added to each team (custom uploads flagged), and
    teams with no activity in 90 days. Channels and apps need the 2.2
    permissions; until a client approves them the report says it's waiting.
  - *Empty, nested and dynamic groups*: groups with no members, security
    groups and distribution lists that contain other groups (and how many
    levels deep), dynamic groups with their rule, flagging paused ones, teams
    and groups about to expire (and whether an owner can renew them),
    role-assignable groups, and blocked accounts still in groups.
  - *Email domains*: SPF (with DNS lookup count), DMARC and DKIM for every
    verified domain, from public DNS. SPF checks also catch multiple records,
    +all/?all, the ptr mechanism and includes that publish no SPF record;
    DMARC checks catch multiple records, a missing p= policy, pct below 100,
    sp=none and no rua= address. Domains that receive mail are also checked
    for MTA-STS (the `_mta-sts` record and the policy file at
    `https://mta-sts.<domain>/.well-known/mta-sts.txt`: mode, mx hosts
    covering the domain's MX, max_age; fetched like sending servers do, with
    no redirects) and TLS-RPT (`_smtp._tls`). MTA-STS and TLS-RPT have their own
    checks.
  - *Devices*: operating systems with Windows support dates, Intune devices
    that are non-compliant, unencrypted, out of support or not checking in,
    every managed device, and every Entra ID device record by last activity.
    Tenants without Intune still get the Entra records and Windows versions.
  - *Hybrid identity*: whether accounts are synced from on-premises Active
    Directory, when directory sync and password hash sync last ran, which
    domains are federated, admin accounts synced from AD, and synced versus
    cloud-only account counts. Cloud-only tenants are labelled as such.
  - *SharePoint storage*: for a client whose SharePoint is filling up. How
    full the tenant is (its quota estimated from licences: 1 TB + 10 GB per
    licence + extra storage) and when it will be full at the current rate;
    where the space goes (current files, older versions, recycle bins,
    Preservation Hold libraries, other); every site and library by size;
    the largest folders and files, the files with the most version history,
    the largest files untouched for two years and the largest recycle bin
    items, each with what to do about it; and OneDrives over 80% of their own
    quota with their largest files. Sizes come from SharePoint's own storage
    figures (`SMTotalSize`), which include every version. Needs
    `Sites.Read.All`.
  - *Site access*: who can reach each SharePoint site and how. Sites with
    no enabled owner or a single one (the Microsoft 365 group's owners, or
    the site administrators of a site with no group), each site's owners,
    administrators, members and guests, what the default library grants
    and to whom (site groups, groups, people, "Everyone"), every guest and
    the sites they can reach, and the people who administer many sites.
    Read from each site's hidden user list and default library with
    `Sites.Read.All`; SharePoint group members on sites with no group are
    in Site permissions.
  - *Site permissions*: each team site's SharePoint groups and who is in
    them, which permission level each person and group holds, the lists,
    libraries and subsites that stopped inheriting their site's
    permissions, and everyone with access by person, guests first. Guests
    with full control fail a check. Read through SharePoint's own API with
    `Sites.Read.All` on Office 365 SharePoint Online and the app's
    certificate; until the client approves that permission the report says
    it's waiting.
  - *File and folder permissions*: who can open each shared file or folder
    in SharePoint and OneDrive and how (given access directly, sent a link,
    an anyone or organisation-wide link), who shared it and when, what was
    newly shared in the last 30 days, links past their expiry or expiring
    within 30 days, and access by person with guests and outside addresses
    first. After a library's first walk, each night asks Microsoft only for
    what changed in it and re-reads those items' permissions; every library
    is walked whole again after 30 days. Needs `Sites.Read.All`.
  - *Licences and subscriptions*: for renewals and fixing licence problems.
    Every product with licences purchased, in a grace period, suspended,
    assigned and available, and its monthly cost at your prices; every
    subscription behind them (`/directory/subscriptions`) with its status and
    the date it next renews, lapses or is deleted, with trials and
    subscriptions in their grace period, suspended or locked listed first and
    bucketed into 30, 60 and 90 days; who holds which licence, directly or by
    group (filter by licence); group licensing errors (not enough licences,
    conflicting or missing base licences, usage location) in plain words with
    what to do; licensed accounts without a usage location; licences assigned
    both directly and by a group; and the groups that hand licences out.
  - *Licence changes*: every paid licence given to or taken from an account,
    from Office Sentry's own kept collections (to the day for the last month,
    to the month before that), whether it came directly or from a group, and
    who made the change and when where the directory audit log has been read
    (Admin activity). Licences bought and assigned at the end of each month
    for the last year, with the cost at your prices and a chart of assigned
    against bought; headline figures say how bought licences and the monthly
    cost moved since a month ago. Free licences are left out.
  - *Microsoft 365 usage*: who used email, Teams, OneDrive, SharePoint and
    the Office apps in the last 30 days (share of licensed people per
    service, and a trend from Office Sentry's own history), when each person
    was last active in each service, Office apps by platform (Windows, Mac,
    mobile, web), Office installs per person, and email and Teams activity
    counts. From Microsoft's usage reports (`Reports.Read.All`).
  - *Licence right-sizing*: people who could move to a cheaper licence, with
    the saving at your prices: nothing used in 30 days (remove the licence,
    or convert to a shared mailbox when it still receives mail), Copilot
    unused, only email used (Exchange Online Plan 1, or Plan 2 for big,
    archived or held mailboxes), no desktop Office apps used (Business
    Standard to Basic, Office 365 E3 to E1, apps-only plans removed), and
    Exchange Online Plan 2 on small mailboxes. Also lists services people are
    licensed for but don't use. Disabled and inactive accounts stay in
    Licence waste so nothing is counted twice. When the tenant hides names in
    usage reports, people show as short codes, licences are read from
    Microsoft's product names, and the page explains how to show names.
- **Evidence and certifications** (`/tenants/<id>/evidence`): the client's
  schemes, each judged from the data. **Cyber Essentials** and **CE Plus**
  (`/tenants/<id>/evidence/cyber-essentials`, PDF, Word, text, XLSX, CSV)
  show every control as Pass, Fail, Partly or Your evidence, with the
  figures, the fix, a text to paste, what changed since an earlier day and
  what the CE Plus assessor tests, plus the answers for the IASME portal
  (Danzell set, by question number). **Questionnaires** (`?set=`) give the
  answers for cyber insurance proposal forms and supplier security
  questionnaires in each form's wording with text to copy and paste, and the
  Microsoft 365 data and date behind each. Questions Microsoft 365 can't
  answer carry a text to complete. Staff can override any answer with a
  note, which fills every form and scheme.
- **Recommended projects** (`/projects` and `/tenants/<id>/projects`, admins
  and analysts): each client's failing checks and missing licences grouped
  into the projects that fix them (Identity and Conditional Access, Device
  management, Email security, Windows upgrade, Security licence upgrade and
  more), worst first, with the findings behind each, the size of the job and
  a rough estimate. Only work of half a day or more is a project; smaller jobs
  (and urgent small ones, like one admin without MFA) are listed as quick
  fixes on the client's page and offered separately in the month-end review.
  Account managers mark each project Proposed, Agreed or Declined with a note
  (XLSX and CSV of the list), and download a client-facing proposal PDF
  without the notes or estimates. Accepted risks don't count, and a project
  drops off once the tenant is fixed.
- **Client view**: client users land on a page that leads with their latest
  monthly review (page and PDF), then open recommendations with their status,
  key numbers, the other reports and who to contact at the MSP; they don't see
  collection internals or run logs.
- **Branding** (`/admin/branding`): the MSP's name, logo and colour on report
  covers, the portal, the sign-in page and the browser tab icon ("Powered by
  Office Sentry" stays small in the footer), the support contact clients see,
  and an optional note printed at the end of every report. The name is also
  the email sender's name unless Email delivery sets one.
- **Data retention** (`/admin/retention`): how long snapshots, the daily history, sign-in detail
  (IP addresses and locations), the activity history, resolved findings, run
  logs, the activity log and issued reports are kept, with a count of what the next daily pass would
  delete. Defaults keep today's behaviour; see DEPLOY.md.

Checks: MFA enforced (CA or Security Defaults, with exclusions), legacy auth
blocked, admins without MFA, admins not covered by an MFA policy, Global Admin
count, guest admins, apps with admin roles, users without MFA, admins without a
phishing-resistant sign-in method, people whose only second factor is a text or
call, inactive accounts, licences on disabled accounts, unassigned paid
licences, external forwarding, inbox rules that hide mail, automatic forwarding
policy, shared mailbox sign-in, mailbox auditing, SMTP AUTH, risky third-party
app access, user consent, app credential expiry, users creating tenants, users
registering apps, guest directory access, joining by email verification, admin
consent requests, SharePoint anonymous links, sites without two owners, guest invitations, stale guest
invitations, teams and groups without an owner, who can create teams and
groups, group expiration, device compliance, disk encryption, Windows releases
out of support, joined computers not in Intune, devices not checking in, stale
device records, SharePoint storage nearly full, licence assignment errors,
domain federation changes, risky sign-ins that succeeded (Entra ID P2), and
subscriptions about to lapse (trials in use ending within 30 days,
subscriptions in their grace period, suspended ones still assigned). For
tenants synced from on-premises AD: directory sync running, password hash sync
on, and admin accounts that are cloud-only. The [check catalogue](docs/checks.md)
lists every check with the data it reads and what it needs.

### Report designs: your own client reports

**Month-end → Report designs** builds a client report from blocks, the way a website builder builds a
page. Drag a block in from the list (a status verdict, key figures, checks by severity, the Secure Score
trend, recommended next steps, failing checks, what changed, licences, a table from any detail report,
the insurance questionnaire answers, this month's commentary, headings, your own text, a contact card, a
page break), set it up in its panel (which severities, how many rows, the heading), put two blocks side
by side, and watch the preview on a real client update as you go. Start from an executive summary, a
full security review, a licensing review, a quarterly review (three months of Secure Score and findings,
what was fixed, the projects on the table and where each stands, licence spend, and what's coming up in
the next 90 days) or a blank page.

A design belongs to the MSP and works for every client: a block with nothing to show for a client (no
accepted risks, no commentary yet) is left out of that client's report. Each client's **Reports →
Custom reports** lists every design as a page and a PDF, and a report plan or a client's email schedule
can send a design (as a PDF) alongside or instead of the monthly review. Text blocks take `{client}`,
`{month}`, `{quarter}`, `{msp}` and `{date}`. Admins and analysts make and edit designs; removing one is an admin's
call, and it comes out of any plan or schedule that sent it.

### Exchange data

Mailbox forwarding, permissions and mail settings come from the Exchange
Online admin API (the same one the ExchangeOnlineManagement module uses),
limited in code to a fixed list of `Get-` cmdlets. It needs the app's
`Exchange.ManageAsApp` permission and, in each client tenant, the **Global
Reader** role assigned to the app (Entra admin center → Roles and admins →
Global Reader → Add assignments). Inbox rules are read through Graph, so they
work without it.

### Testing against a real tenant

`python -m officesentry live-test [--out DIR]` runs every collector against a
test tenant using `OFFICESENTRY_TEST_TENANT_ID`, `OFFICESENTRY_TEST_CLIENT_ID`
and `OFFICESENTRY_TEST_CLIENT_SECRET`, stores the result as client "Live test"
and prints each collector's outcome and every check. The secret is only for
this command; the product signs in with a certificate.

With `--record tests/fixtures/live_tenant.json` it also saves Microsoft's
responses, with names, addresses, phone numbers, domains and IDs replaced, and
the test suite replays them, so every collector, check and report runs on real
Microsoft responses in CI. The recording comes from one Microsoft 365 Business
Premium tenant (Entra ID P1, synced from on-premises AD, no Intune devices).
These areas have no real recording and are tested with simulated data only:

- **Entra ID P2 features**: risky users and sign-in risk (the test tenant
  has no P2; Microsoft answers "not licensed").
- **Per-user MFA states**: Office Sentry only reads them on tenants without
  Entra ID P1.
- **Sign-in methods read one person at a time**: only used when Microsoft's
  registration report comes back empty, as it does on a newly licensed tenant.
- **Intune device details**: the tenant has no enrolled devices, so the
  device compliance and encryption checks have nothing to read.
- **National clouds** (GCC High, DoD, China): there's no test tenant in
  them; the endpoints come from Microsoft's documentation.
- **Risky sharing**: the tenant's shared file is shared with one person in the
  organisation, so anyone links and sharing outside the organisation are
  simulated.
- **Forwarding to someone in the directory**: the tenant forwards to an email
  address, so resolving a forwarding recipient (`Get-Recipient`) is simulated.
- **SharePoint recycle bin items**: Microsoft answered the recycle bin request
  with an empty list, though the site's storage figures count a deleted file.

## Documentation

- [What Office Sentry reads](docs/what-office-sentry-reads.md): for clients
  asked to approve access. Every permission, what is and isn't read, where the
  data is kept and how to remove access.
- [Onboarding a client](docs/onboarding-a-client.md): for technicians. Adding
  a client, consent, Global Reader, the first collection, connection health,
  the errors on the Data collection page, pausing, archiving and offboarding.
- [Check catalogue](docs/checks.md): every check, data source, detail report
  and permission, generated from the code by `scripts/gen_check_catalogue.py`.
- [Troubleshooting](docs/troubleshooting.md): the common setup errors
  (`AADSTS50011`, `create-app`, Global Reader, the worker not running) and
  their fixes.
- [Getting help](docs/getting-help.md): where to look first, the support
  package (one zip of versions, settings, health, failures and logs with
  secrets and client names taken out) and how to report a problem.
- [Securing the portal](docs/securing-the-portal.md): what the portal does
  by itself, what the person running it has to do, and reverse proxy setups
  for Caddy, nginx, Traefik and a CDN.

## What it does differently from v1

- **Read-only by construction.** The Entra app requests only `*.Read*`
  Graph permissions, the Graph client has no write methods, and the Exchange
  client only runs allow-listed `Get-` cmdlets.
- **One multi-tenant app.** You register it once; each client admin opens a
  consent link to add their tenant. No per-tenant app or device-code step.
- **No credential files.** The certificate's private key is encrypted in the
  database. Only the public `.cer` can be downloaded, by admins.
- **Certificates don't expire by surprise.** Admins see a banner, and the
  team gets an email and an alert, 60, 30 and 7 days before an app
  certificate (or the mail app's) expires. *App connection → Replace
  certificate* adds a new one next to the old, tests it and switches without
  stopping collection. `SECURITY.md` has the steps for a leaked key.
- **Extra access is flagged.** A client whose tenant gave the app more than the
  read-only permissions, or any admin role besides Global Reader, shows as
  needing attention on its Manage page and on Client settings.
- **Deny-by-default access.** A person has a role (a set of capabilities:
  which report areas open for them, downloads, refreshing data, accepting
  risks, emailed reports, month-end review and approval, projects, custom
  reports, managing clients, users or settings) and a scope (every client, or
  the clients and groups granted to them). Seven built-in roles (Administrator,
  Team leader, Second line, First line, Account manager, Auditor, Client
  viewer); an admin can also give or take away single capabilities for one
  person, everywhere or on one client, and taking away always wins. Every
  page and download checks on the server; hidden clients return 404 and a
  capability taken away on one client answers 403 there.
- **Every run is a snapshot** (`runs` → `objects` + `metrics`), so reports
  can show history and changes. Metrics and findings are kept forever; full
  snapshots for the last 30 days plus one per month for 12 months. The daily
  history also keeps every run for 25 months, storing each object once per
  version (`state_history.py`), written alongside the snapshots and checked
  against them.
- **One worker process, no Redis/Celery.** It collects on a cron schedule in
  your timezone, skips data that is already fresh, refreshes stale data before
  an emailed report, and prunes old snapshots. Month-end approvals and report
  emails run as background tasks in the same worker, so approving 200 clients
  returns at once and the page shows progress; work a crashed worker left is
  picked up again, and a client's month is still sent exactly once.

## Sign-in security

- **Sign in with Microsoft** (optional, off until an admin sets it up): staff sign
  in with their own Microsoft Entra ID account through OpenID Connect, under the
  MSP's own Conditional Access and MFA. *Settings → Staff sign-in* creates a
  small single-tenant app in the MSP's tenant with the same device-code wizard
  as the app connection (`sso.py`, `app_setup.py`); the read-only client app and
  its permissions are untouched. The app has an app role for each built-in role
  (Admin, TeamLeader, Analyst for Second line, FirstLine, AccountManager,
  Auditor), and a custom role can be given an app role value of its own; they're
  assigned to people or groups in Entra and read from the ID token at every
  sign-in ([docs/staff-sign-in.md](docs/staff-sign-in.md)). Which clients
  someone sees is still granted here, so tenant isolation doesn't change. Existing accounts link from the Account page or by a
  matching username; new staff get an account at their first sign-in. Passwords
  stay as the break-glass route until an admin signed in with Microsoft turns
  them off (`python -m officesentry staff-sign-in --passwords on` turns them
  back on from the server). Client accounts always use passwords.
  [docs/staff-sign-in.md](docs/staff-sign-in.md) has the steps.
- **Two-step sign-in** (a 6-digit code from an authenticator app) is required
  for everyone on new installs, client accounts included. Installs set up
  before October 2026 keep requiring it for admins and analysts only, so
  nobody is surprised after an upgrade; *Settings → Users* shows the policy
  and recommends extending it to clients. Set it with
  `OFFICESENTRY_REQUIRE_MFA` (`all`, `staff` or `off`). Adding an
  authenticator from the account page asks for the password, and a password
  an admin set (new user or reset) has to be replaced at the next sign-in,
  before two-step setup (or right after the code, for accounts that have it). Each user gets ten
  one-time recovery codes. An admin can reset someone's two-step sign-in from
  their user page if they lose their phone, giving their own password, as
  for resetting a password or signing someone out.
- **Sign-in limits by address, not account lockout:** wrong passwords slow
  down the address they come from (5 per username or 20 in all per 15
  minutes), and a spread-out attack on one account slows new addresses only,
  so nobody can lock a person out from elsewhere. Five wrong two-step codes
  (which need the password) lock that step for 15 minutes. Admins can clear
  both from the user page. Details in [DEPLOY.md](DEPLOY.md#sign-in-protection).
- **Sessions are stored on the server.** Signing out ends the session, so a
  copied cookie stops working. People can see where they're signed in and
  sign out other browsers on their **Account** page. Sessions end after 2 hours
  without activity or 8 hours in total, and when a password, role, access or
  two-step setting changes.
- **Activity log:** admins see sign-ins and every change, with the address,
  under *Settings → Activity log* (filters, paging, CSV). Failed sign-ins are
  kept for 90 days, the rest for good unless *Settings → Data retention* sets
  a period (never under a year).
- **Cookies** are `Secure`, `HttpOnly`, `SameSite=Lax` and use the `__Host-`
  prefix. Behind a reverse proxy, set `OFFICESENTRY_TRUSTED_PROXIES=1` so the
  real client IP and scheme are used.
- **Keys:** the session key and the encryption key are separate and kept in
  `secrets.json` (mode 0600, tightened if it was looser) unless set as
  environment variables. In Docker it lives in a volume of its own, apart
  from the data and its backups. `python -m officesentry rotate-keys --new`
  replaces both and re-encrypts every stored secret.
- **Backups:** nightly checked copies, encrypted off-site copies with restic
  to any S3-compatible storage, and a weekly drill that restores the newest
  one and checks it opens (`DEPLOY.md`, "Off-site backups").

## Quick start (Docker)

```bash
cp .env.example .env          # set OFFICESENTRY_BASE_URL at least
docker compose --profile https up -d
```

That runs the published release image. To run this folder's code instead,
add `docker-compose.build.yml` (`-f docker-compose.yml -f
docker-compose.build.yml up -d --build`). [DEPLOY.md](DEPLOY.md) has the full
steps: HTTPS, schedule and retention settings, backups and updating.

Create the first admin:

1. Run `docker compose logs web` and find the line starting **No admin account
   yet**. Open the link on it. (Lost it? `docker compose exec web python -m
   officesentry setup-link` prints it again.) The link only works until the
   first admin exists, so nobody else can claim a fresh install.
2. Choose a username and password, then scan the QR code with an authenticator
   app (Microsoft Authenticator works) and save the recovery codes.

Your home page then shows a **Get started** checklist (`first_run.py`): the app
connection, your first client, its consent and its first collection, each read
from what Office Sentry has stored, so it ticks itself off as you go and
disappears after the first collection. Then:

1. Create the app: **Settings → App connection → Sign in to Microsoft**. The
   page shows a code and Microsoft's link; sign in there as an admin of *your*
   tenant who can create app registrations (Global Administrator, Application
   Administrator or Cloud Application Administrator). Office Sentry generates
   the certificate, registers the multi-tenant read-only app and saves the
   connection with its client ID, then offers to add your first client. The
   sign-in's token is used once, in memory, and never stored; the device code
   is kept encrypted only while the page waits (`app_setup.py`).
   `docker compose run --rm worker python -m officesentry create-app` does the
   same from the command line. Both reuse a certificate you generated earlier
   rather than making a second connection.
2. Or create it by hand (say, if Conditional Access blocks device sign-in):
   *Other ways to create the app* on the same page. Generate a certificate,
   make the app multi-tenant, upload the certificate (a client secret won't
   work), add the redirect URI `https://<your portal>/consent/callback` as a
   Web platform and paste its client ID. Enter your Directory (tenant) ID with
   the client ID to check the certificate straight away.
3. **Add client** (or the form at the end of the wizard). The client's page shows a setup checklist that ticks itself
   off: grant admin consent (a Global Administrator of the tenant opens the
   link; a full collection is queued on return), assign Global Reader, first
   full collection, all permissions granted. The client's **Manage** tab says
   in plain words what's wrong and how to fix it if Office Sentry can't sign
   in or is missing a permission or role.

## Managing clients

- **Client settings** (*Settings*, or *Connection health* on the Clients page)
  lists every client with its status (*Onboarding*, *Healthy*, *Needs
  attention*, *Paused*, *Archived*), consent, missing permissions, Global
  Reader, last good collection and failing data sources, worst first.
- A client's **Manage** tab (admins and analysts) shows its connection health:
  consent, sign-in, which of the app's permissions the tenant actually granted,
  whether the app holds Global Reader (or more than it needs), licences, last
  collection and failing data sources, each with the fix. It's read by the
  `connection` collector with every collection (GET only, from permissions the
  app already has); *Check connection now* queues it straight away.
- Admins can rename a client, change its tenant ID (until data has been
  collected) or app connection, keep notes, **pause** it (no collection,
  emailed reports or alert emails; data stays) and **archive** it (also hidden
  from lists, dashboards, rollups, digests and its client users; restorable).
- **Client groups** (*Settings*, *Client groups*) sort clients by tier, region,
  service level or anything else; a client can be in several. Pick a group at
  the top of the Clients dashboard, All clients, What changed, Month-end or
  Client settings to see only those clients, with downloads to match, and give
  a digest one group instead of every client. Groups never show anyone more:
  the only way a group grants access is an admin ticking it for an analyst on
  their user page, which then covers every client in it, including ones added
  later. Client accounts are never given clients through a group.
- **Offboard** walks through a final export, the client's side (remove Global
  Reader, delete the enterprise app) and what will be deleted, then deletes the
  client and everything stored for it after you type its name, optionally with
  client accounts that had no other client. `python -m officesentry
  delete-tenant <id>` does the same from the server.

## National clouds (GCC High, DoD, China)

Each client has a **Microsoft cloud**, chosen when it's added (or later under
**Manage**):

| Cloud | Sign-in | Microsoft Graph | Exchange admin API |
|---|---|---|---|
| Commercial (includes GCC) | login.microsoftonline.com | graph.microsoft.com | outlook.office365.com |
| US Government GCC High | login.microsoftonline.us | graph.microsoft.us | outlook.office365.us |
| US Government DoD | login.microsoftonline.us | dod-graph.microsoft.us | webmail.apps.mil |
| China (operated by 21Vianet) | login.chinacloudapi.cn | microsoftgraph.chinacloudapi.cn | partner.outlook.cn |

GCC (moderate) tenants use the commercial endpoints, so leave them on
Commercial.

**Each national cloud needs its own app registration.** GCC High, DoD and
China are separate Entra IDs: an app registered in commercial Entra can't sign
in to them, and the other way round. For clients in one of those clouds:

1. **App connection → Create an app for another Microsoft cloud**, choose
   that cloud and **Sign in to Microsoft** with an account in your tenant *in
   that cloud*. Office Sentry creates that cloud's app and connection, with
   its own certificate. (`docker compose run --rm worker python -m
   officesentry create-app --cloud gcc_high`, or `dod`, `china`, does the
   same from the command line.)
2. Or register the app by hand in that cloud's own admin centre
   (entra.microsoft.us for GCC High and DoD, the 21Vianet Azure portal for
   China), exactly as for the commercial app, after generating a certificate
   for that cloud under *Other ways to create the app*, and save its client ID.
3. Add the client with the same cloud and that app connection. Its consent
   link opens on that cloud's sign-in page, and collection signs in to that
   cloud's Graph and Exchange.

A client in a cloud with no app connection for it is added anyway; its page
says to set up an app for that cloud, and collection doesn't start until there
is one. An app connection can only be chosen for clients in its own cloud.

Microsoft doesn't offer everything in every cloud. Where it doesn't (Secure
Score and Identity Protection's risky users in China; the Microsoft 365 Apps
and Copilot usage reports in GCC High, DoD and China), the collection is
marked *partial* with the reason, and the report or check shows it as
unavailable instead of failing. The list is `missing` in `officesentry/clouds.py`.

Report emails are sent from your own mailbox through the commercial cloud,
whatever cloud the clients are in.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest && ruff format . && ruff check .
OFFICESENTRY_INSECURE_COOKIES=1 python -m officesentry web   # http://127.0.0.1:8000
python -m officesentry worker                                 # second terminal
```

`pytest -n auto` runs the tests on every core. `web` is Werkzeug's
development server; `python -m officesentry serve` runs the portal on a
production server (gunicorn, or waitress on Windows).

**Windows without Docker** works for development and for a small
production install (see "Running on Windows without Docker" in
[DEPLOY.md](DEPLOY.md)): use `.venv\Scripts\activate` and
`set OFFICESENTRY_INSECURE_COOKIES=1`, and `serve` instead of gunicorn. What
differs is PDF files: WeasyPrint needs the Pango libraries, which Docker
includes and Windows doesn't have. Without Pango, every PDF button opens the
same report as a print-ready page and the browser's print window comes up:
choose **Save as PDF** as the printer to get the same layout. Scheduled
emails can only attach PDFs when WeasyPrint works.

Tests mock Microsoft Graph at the HTTP layer (`responses`), not the client
class, so URL and paging bugs are caught.

## Layout

```
officesentry/
  config.py      env config; separate session and encryption keys
  db.py          SQLite (WAL) + transactional migrations (migrations/*.sql)
  backups.py     backup, restore, and the copy taken before each upgrade
  crypto.py      Fernet vault with key rotation
  certs.py       certificate generation, Entra keyCredential
  cert_lifecycle.py  certificate expiry warnings and replacing one without a gap
  entra.py       read-only permission list, app creation, consent URL
  graph.py       Graph client: retries, Retry-After, paging, typed errors
  store.py       tenants, connections, runs, objects, metrics, pruning
  collectors/    one module per data source; @register adds it
  snapshot.py    latest successful data per collector, for rules/reports
  checks/        checks, one module per area: snapshot -> pass / fail with findings / unavailable
  licences.py    SKU names, prices, licence-waste maths
  usage.py       per-person Microsoft 365 usage and licence right-sizing rules
  formatting.py  money, dates and labels as people read them, for pages, reports and emails
  reports/       report builders (data only, one @report declaration each), SVG charts, XLSX/PDF export
  findings.py    findings lifecycle (new, resolved, reopened, accepted)
  changes.py     snapshot diffs for "since last month"
  mailer.py      sends a finished email over SMTP or Graph sendMail
  delivery.py    schedules, digests, alert emails, retries and email rendering
  schedule.py    cron slots in a timezone, freshness (max age), periodic jobs
  worker.py      queue runner, task threads, schedule, retention, heartbeat
  tasks.py       background tasks (approvals, report emails) with leases
  tenant_health.py  connection health and the onboarding checklist, from stored state
  system_health.py  Office Sentry's own health: /health/ready and System status
  system_alerts.py  emails and webhook posts when Office Sentry itself needs attention
  lifecycle.py   first collection, pause, archive and deleting a client
  web/           Flask app: access.py is the single authorization gate
```
