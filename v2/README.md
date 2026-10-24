# Office Sentry: technical reference

Read-only Microsoft 365 reporting for MSPs: nightly snapshots of every
client's Microsoft 365, cross-client rollups, month-over-month changes and
branded client reports. The [root README](../README.md) is the product page
with screenshots; this page lists every feature in detail. Running it on a
server is in [DEPLOY.md](DEPLOY.md), and on Portainer, a NAS, Kubernetes or a
cloud in the [deploy guides](docs/deploy/README.md). Changing the code is in
the [developer guide](../DEVELOPING.md).

**Read-only by design.** You register one multi-tenant Entra app, once, and
each client's admin opens a consent link to add their tenant: there is no app
or device-code step per client. The app asks only for read permissions, the
Graph client has no write methods, and the Exchange client only runs an
allow-listed set of `Get-` cmdlets. The certificate's private key is encrypted
in the database, never kept as a file; only the public `.cer` can be
downloaded, by people who manage settings. Report emails sent through
Microsoft 365 go from a mailbox in your own tenant, through a separate
send-only app.
The audit log add-on's own app also starts and stops the activity feed of each client who approves it: its only calls that aren't reads.

**Certificates and extra access are watched.** People who manage settings see
a banner, and the team gets an email and an alert, 60, 30 and 7 days before a
certificate Office Sentry signs in with (an app connection's, the mail app's or
the sign-in app's) expires. *Settings → App connection → Replace certificate*
adds a new one next to the old, tests it and switches without stopping
collection; [SECURITY.md](../SECURITY.md#if-a-certificate-or-key-leaks) has the
steps for a leaked key. A client whose tenant gave the app more than the
read-only permissions, or any admin role besides Global Reader, shows as
needing attention on its *Settings → Manage* page and in *Client settings*.

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
  for one client or across all of them, with your custom reports and saved
  views at the top. The search also finds clients and settings.
- **Clients** (`/`): every client you look after in one sortable, filterable
  table, worst first, with the monthly review's figures, each client's data
  health and its review as a PDF.
  Its *All reports* menu swaps the table to any other report for every client
  (`/across/<report>`): the insurance evidence pack and every detail report
  (its headline figures as the columns), limited to the clients you can see.
  New detail reports appear automatically, and `/across/review` opens the
  Clients page.
  **Every row** (`/across/<report>/rows/<table>`) stacks every client's rows of one table of
  a detail report under a Client column, with the same filters, sorting, saved
  views and PDF, XLSX and CSV downloads (up to 10,000 rows a table; it says when
  it stops there). **Checks** (`/across/checks`) is every check for every client
  in one grid, a square per check grouped by area, with a page per area that
  names each check and XLSX and CSV downloads; a failing or accepted square opens
  that client's findings.
- **What changed** (`/changes`, and each client's *Changes* tab): a standalone
  month-over-month report for the last 7, 30 or 90 days, or, on a client's
  *Changes* tab, between any two days the kept data reaches back to. It covers
  key figures then and now, new and fixed findings, admin, account, Conditional
  Access, forwarding, app, device and group changes, and a row-by-row
  comparison of every detail report. Each person sees the changes in the
  report areas they open on that client, and findings only with findings.
- **Downloads**: the reports above have CSV and XLSX. Values from clients can
  never become spreadsheet formulas.
- **Activity history** (`/tenants/<id>/activity`, a client's *Changes →
  Activity history*, staff only): every sign-in and directory audit event
  Office Sentry has read, kept beyond the 30 days Microsoft keeps them
  (sign-ins 180 days, daily summaries 25 months, audit events 7 years by
  default): how far back each is detailed, the last 30 days, sign-ins per day,
  each month with a CSV of every sign-in, and admin changes worth a second
  look. Each person's page shows their sign-ins by month from it.
- **Timeline** (`/tenants/<id>/timeline`, a client's *Changes → Timeline*,
  staff only): the client's year month by month, for a review meeting. Each
  month opens with a one-line summary (findings found and fixed, Secure Score
  and MFA then and now, admin changes, reports sent, failed nights) over its
  dated entries: findings found, fixed, come back or accepted, admin changes
  from the activity history, reports sent and approved, month-end notes and
  next steps, failed collections and the client's own milestones. Beside it,
  Secure Score, MFA, open critical and high findings and unassigned paid
  licences at each month's end over twelve months. Filter by kind, look back
  12 or 24 months or everything kept, and download it as PDF, XLSX or CSV.
  Each person sees only the parts whose report they open on that client.
- **Coming up** (`/upcoming`): what falls due in the next 90 days across your
  clients, sorted into now, 30, 60 and 90 days and no date: subscription
  renewals and trials (lapsing ones need attention now), client app secrets and
  certificates (and those expired in the last 30 days), teams and groups about
  to expire, mailboxes at 90% of their send limit or forecast to fill, DMARC
  still at p=none, and accepted risks whose review date comes up. Office
  Sentry's own certificates are added for admins. It filters and downloads
  (PDF, XLSX, CSV) like a report table, and comes as a weekly email too (below).
- **Search all clients** (the box at the top of the menu, `/search`, for
  staff): one search through the latest data of every client you can see, for
  clients by name or domain (listed first),
  people, groups, mailboxes, addresses mail is forwarded to (by an admin or an
  inbox rule), SharePoint sites, sign-in IP addresses, apps and app
  registrations, devices and domains. "Which clients forward to this address?"
  or "who signed in from this IP?" is one search. Each result opens the
  person's or object's page, or the detail report filtered to that row, and it
  only finds what you can open.
  Press `/` (or Ctrl+K, ⌘K on a Mac), or click that box, to open the **jump
  box** over any page: type a few letters to land on a client, a report for
  a client ("north mfa"), a report across every client, a custom report, one
  of your saved views, a page or a setting, or a person, mailbox, forwarding
  address, site, app, device or domain from the search; add `pdf` or `xlsx`
  ("north mfa pdf") for the report's file.
  Enter opens the highlighted row, and the last row searches everything for
  what you typed. While the box is empty it shows the places you opened last.
- **Home pages** (staff): **Today** (`/today`, for technicians) is a ledger of
  what happened across your clients, one line per thing, worst first, each with
  the time it happened: critical and high findings first seen or back,
  collections that failed or are out of date, access not granted yet, Coming up
  items due within a week, and the critical and high findings a collection no
  longer saw. What's new since you last looked (yesterday, on your first visit)
  sits above a line; what you'd seen before and is still open sits below it.
  Anyone who can annotate a client's findings hands any line to a colleague who
  sees it on that client, or snoozes it until tomorrow morning or for a week:
  snoozed lines leave the list and the counts until the snooze ends, and a hand-off
  on a failed collection or a renewal goes once that line clears. **Accounts** (`/clients`, for
  account managers) shows each client's licence waste, renewals in the next 30
  days, this month's month-end reports to approve and projects to propose. Each
  opens with one sentence saying what needs attention. Pick yours on the
  *Account* page or with *Make this my home page*; *Home* in the menu and the logo
  open it, and signing in lands there. Until the first collection has run,
  admins see the **Get started** checklist there instead.
- **Licence prices** (*Settings → Licence prices*): enter monthly prices per SKU
  to cost waste and right-sizing savings.
- **Findings** (`/tenants/<id>/findings`): every finding keeps first seen,
  resolved and reopened dates. Anyone who can accept risks (Second line, Team
  leader and Administrator by default) can accept one with a reason and an
  optional review date after today, when it comes back for review; accepted
  risks leave the totals and get their own section.
- **A finding's sheet** (`/tenants/<id>/findings/<finding id>`, or Open on any
  row of the Findings tab, in place beside the table): the evidence behind the
  finding (for an account, the facts from its page), its history, where else
  the same check fails across the clients the person sees, the fix as steps,
  *Copy for a ticket*, *Email the contact* (a `mailto:` link for the person's
  own mail app; Office Sentry sends nothing) and *Accept risk*. Anyone who can
  accept risks can also keep a note on a finding and hand it to a colleague
  who sees that client (`findings.note`, `hand` and `snooze`; migration 0048).
  Notes and hand-offs stay with the team: never on a client's pages, in a
  report or in an email. Nothing is ever written to the client's tenant.
- **Client grades** (`/tenants/<id>/grade`, staff with *Security findings and
  reviews*): every client's grade from A to E, the worst of five parts (admin
  accounts, sign-in and accounts, mail, devices, sharing and apps), each part
  its worst open finding after accepted risks are set aside and graded only
  when half its checks could run. The grade sits beside the client's name on
  every page about them and on the Clients page, and leads the
  Overview's figures; its page shows what
  moves each part, the grade on each of the last twelve month-ends (from the
  findings table, nothing extra stored) and the comparison with the other
  clients the person sees of about the same size, none of them named. The
  rules are in [docs/client-grades.md](docs/client-grades.md).
- **Since last month**: new and fixed findings, admin role changes, new,
  disabled and deleted accounts, licences people were given or lost, and
  Conditional Access policies added, switched or edited, compared with the
  snapshot from 4+ weeks earlier.
- **Emailed reports** (a client's *Emails* tab, and *Email reports* in the menu
  for every client at once):
  - Send any mix of reports (the monthly review, the evidence pack, any detail
    report) weekly, monthly or quarterly, at a time in the schedule's own timezone.
    Data collected longer ago than the freshness limit (12 hours by default) is
    collected again first.
  - **Saved views** too: a report with your filters, columns and sort, as a PDF,
    XLSX or CSV, for one client, or with every client's rows (or one group's)
    for your team.
  - Recipients are contacts: the client's people get the email, your team gets a
    hidden copy. Optional note at the top of the email.
    New contacts also get the client's report plan emails, so only someone who can open every report the plan sends adds one.
  - A cross-client **digest** for your team, worst clients first.
  - A weekly **Coming up** email for your team, with the same list as the
    Coming up page above.
  - **Alert emails** when a scan finds something new and critical (or high), for
    one client or all of them. They come from the same alert event as the
    Teams/Slack alerts below. A client's first scan never emails.
    They name the findings, so choosing who gets them needs security findings.
  - Preview (desktop and phone width), Send now, pause, and a delivery history
    with failures, automatic retries (5 min, 30 min, 2 h) and a Retry button.
  - Sent over SMTP or Microsoft 365 (Graph `sendMail` from a mailbox in your own
    tenant, through a separate send-only app), under Settings → Email delivery.
  - Every file a client was sent is kept as it was sent, in the client's
    **Report history** (under *Emails*), to download again later.
- **Alerts** (Settings → Alerts): a Microsoft Teams (Workflows), Slack or generic
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
- **People, mailboxes, groups and sites** (a client's *Users* tab): a page per
  account (`/tenants/<id>/users/<object id>`: sign-in, MFA and methods, admin
  roles, administrative units, licences and their use, forwarding and
  delegates, devices, sharing, its sign-ins by month and how the account
  changed), and the same for things that aren't people. *Mailboxes* lists every
  mailbox with its size, forwarding, who else can open it and its inbox rules;
  each page (`/tenants/<id>/mailboxes/<id>`) has size and growth over the kept
  months, forwarding and rules, who can open or send from it, protocols, its
  automatic reply, the phones syncing it and email activity. *Groups and teams*
  lists every team, Microsoft 365 group, security group and distribution list;
  each page (`/tenants/<id>/groups/<id>`) has its owners, members and nested
  groups, what membership gives (Conditional Access policies, admin roles,
  licences), mail settings and traffic, the team's site and admin changes.
  *Sites* lists every SharePoint site with its storage, files, sharing links and
  last activity; each page (`/tenants/<id>/sites/<id>`) has storage over the
  kept months with the libraries and largest files, sharing settings and shared
  files, the team or group behind it, activity and a link to *Site access*.
  Every page opens with what needs attention and its open findings, shows
  what changed, and downloads as PDF and XLSX. Names and addresses in reports
  and search results open these pages. A blocked account's page, or any
  account's after *Leaving? Show what to tidy up*, adds **Before you close this
  account**: what's still tied to it (licences with their monthly cost, the
  mailbox and whether it can become a shared one, who else can open it and
  which other mailboxes the person can open, the OneDrive, devices, teams and
  groups the person alone owns, admin roles), each with what to do, and a
  one-page PDF of it with a line for the client to sign.
- **Detail reports** (`/tenants/<id>/data/<report>`, each also as PDF, XLSX and CSV),
  grouped by area as in the report menu ([Detail reports by
  area](#detail-reports-by-area), below). A client's Reports tab (`/tenants/<id>/data`)
  opens on its report catalogue: the summaries, your custom reports and saved views, and
  every detail report by area, with a search box; the "All reports" menu above each
  report and the overview's Reports card list the same. Every table can be
  filtered by the values in it (or by age, for dates), sorted, and have columns
  hidden; headers stay visible while scrolling, and the view can be saved by
  name and reused on any client. Downloads contain the rows and columns shown.
  The busiest tables have a bar chart that follows the filters (click a bar for
  its rows), and headline figures carry a line of their last 90 days.
  Every headline number opens the rows behind it, here and on the Clients page.
  *Show as it was on* (`?on=`) shows a report, its PDF and its XLSX as the
  client was at the end of any earlier day the kept data reaches back to (the
  daily history, 25 months by default), with a link to what changed since then.
- **Evidence and certifications** (`/tenants/<id>/evidence`): the client's
  schemes, each judged from the data. **Cyber Essentials** and **CE Plus**
  (`/tenants/<id>/evidence/cyber-essentials`, PDF, Word, text, XLSX, CSV)
  show every control as Pass, Fail, Partly or Your evidence, with the
  figures, the fix, a text to paste, what changed since an earlier day and
  what the CE Plus assessor tests, plus the answers for the IASME portal
  (Danzell set, by question number). The **cyber insurance proposal** and
  **supplier security questionnaire** packs judge the controls those forms
  ask about the same way (a gap on a few accounts is Partly) and give the answers
  in each form's wording with text to copy and paste, and the Microsoft 365
  data and date behind each. Questions Microsoft 365 can't answer carry a
  text to complete. Staff can override any answer with a note, which fills
  every form and scheme. **IASME Cyber Assurance**, **ISO/IEC 27001 Annex
  A** (by control number) and **UK GDPR** (the ICO's security outcomes and
  the accountability records) are packs on the same page, with the
  governance and data protection answers to complete, as are the **CIS
  Microsoft 365 benchmark**, **NIST CSF 2.0**, **SOC 2** and the
  **Essential Eight**.
- **Recommended projects** (`/projects` and `/tenants/<id>/projects`, for roles
  with *Recommended projects*): each client's failing checks and missing
  licences grouped into the projects that fix them (Identity and Conditional
  Access, Device management, Email security, Windows upgrade, Security licence
  upgrade, Move to cloud-only accounts for small businesses still syncing from
  an office server, and more), worst first, with the findings behind each, the
  parts of the project those findings call for, the size of the job and a
  rough estimate. Only work of half a day or more is a project; smaller jobs
  (and urgent small ones, like one admin without MFA) and repairs (directory
  sync that has stopped) are listed as quick fixes on the client's page and
  offered separately in the month-end review. Account managers mark each
  project Proposed, Agreed or Declined with a note (XLSX and CSV of the list),
  and download a client-facing proposal PDF without the notes or estimates.
  Accepted risks don't count, and a project drops off once its findings are
  fixed.
- **Client view**: client users land on a page that leads with where they
  stand: the month's verdict and its most urgent item, the account manager's
  note once the month is approved, and buttons for the monthly review, its PDF
  and the two-page summary. Under it come the MSP's recommendations with their
  status and target (else what needs attention), key numbers, the other
  reports and who to contact at the MSP; they don't see collection internals
  or run logs.
- **Branding** (`/admin/branding`): the MSP's name, logo and colour on report
  covers, the portal, the sign-in page and every browser tab's icon and title ("Powered by
  Office Sentry" stays small in the footer), the support contact clients see
  in reports and on the sign-in page,
  and an optional note printed at the end of every report. The name is also
  the email sender's name unless Email delivery sets one.
  Without a colour of its own, reports and emails use the portal's ink blue.
- **Appearance** (each person's Account page): light, dark or the same as the device, and for staff a colour
  for the menu and buttons (ink by default, or bottle green, navy, claret, graphite or plum). Only that
  person's screen changes; client reports, PDFs and emails keep the MSP's branding.
- **Data retention** (*Settings → Data retention*, `/admin/retention`): every
  collection is kept as a snapshot, so reports can show history and changes.
  By default the last collection of each of the last 30 days and of each of
  the last 12 months is kept whole, the daily history keeps every collection
  for 25 months by storing each object once per version (`state_history.py`),
  and trend figures are kept for good. The page sets how long snapshots, the
  daily history, sign-in detail (IP addresses and locations), the activity
  history, resolved findings, run logs, the activity log and issued reports are
  kept, with a count of what the next daily pass would delete. Every default
  is in [DEPLOY.md](DEPLOY.md#how-long-data-is-kept).

### Detail reports by area

Each report is listed under its area in the report menu, in menu order.

#### Identity and access

- *All users*: every account with its department, job title, manager,
  licences, source (cloud or synced) and last sign-in; each manager with
  their direct reports; member counts by department. Accounts reporting to
  a manager whose sign-in is blocked are flagged.
- *MFA coverage*: who has set up MFA, who is required to use it (Security
  Defaults, Conditional Access or per-user MFA) and where the two don't match:
  how the tenant requires it, accounts with a gap (admins first), protected
  accounts by method, and whether guests are required to use it.
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
- *Admins and roles*: every admin role holder with MFA, PIM and sign-in
  status, privileged roles apart from the rest, and a break-glass check.
- *Administrative units*: every unit with its membership (assigned or
  dynamic, and whether the rule is paused), restricted management units, how
  many users, groups and devices are in each and which admins are scoped to
  it; every scoped admin, flagged when the account is blocked or a guest;
  and the members of each unit by name. Each person's page shows the units
  they are in and administer.
- *Conditional Access coverage*: every policy in plain words, and for each
  account which enforced policy requires MFA, whether legacy sign-in is
  blocked, and which policies exclude it (group membership expanded).
- *Trusted organisations and locations*: the cross-tenant access settings (what other organisations' people can do here, as guests and in Teams shared channels, and whether their MFA and devices are trusted), each organisation with settings of its own, and every named location with the Conditional Access policies that include or leave it out. Trusting every organisation's MFA and wide IP ranges marked trusted are flagged.
- *Inactive accounts*: members and guests with no sign-in for 90 days, or
  never signed in and created over 30 days ago (admins and licensed accounts
  first), guest invitations still unaccepted after 30 days, and disabled
  accounts still holding licences or admin roles. Sign-in dates need Entra ID
  P1.
- *Sign-in activity*: the last 7 days of interactive sign-ins from the
  Entra ID sign-in log (Entra ID P1), summarised rather than stored: accounts
  with repeated failures (wrong passwords, lockouts), addresses failing
  against several accounts (password spraying), successful sign-ins from
  countries the tenant rarely uses, successful sign-ins that needed only a
  password, failure reasons in plain English (MFA and other prompts counted
  apart from failures), and sign-ins by country, app and account. With
  Entra ID P2, risky sign-ins too. Each run reads on from where the last
  one stopped, into the activity history, and says which part is still to
  read if it stops. Each account's page shows its own summary.
- *Risky users* (Entra ID P2): accounts Entra ID Protection rates at risk
  or confirmed compromised, with the risk level and why, then the risk
  remediated, dismissed or confirmed safe in the last 90 days and how. A
  check fails while any account is at risk, and the account's page flags it.
- *Guests and sharing*: invitation, consent and SharePoint sharing settings,
  and every guest with invitation state and last sign-in.
- *Deleted users and groups*: the Entra ID recycle bin. Every deleted user
  (member or guest, the licences it held) and deleted team or Microsoft 365
  group, when it was deleted and the days left before Microsoft removes it
  for good after 30 days. Licensed members and teams with 7 days or less
  left are flagged. Restoring is done in the Entra or Microsoft 365 admin
  centre; Office Sentry only reads the recycle bin.
- *Legacy sign-ins*: SMTP AUTH, IMAP, POP and other basic-auth sign-ins from
  the last 30 days of the sign-in log (Entra ID P1), and which mailboxes
  still allow SMTP AUTH, POP and IMAP.
- *Hybrid identity*: whether accounts are synced from on-premises Active
  Directory, when directory sync and password hash sync last ran, which
  domains are federated, admin accounts synced from AD, and synced versus
  cloud-only account counts. Cloud-only tenants are labelled as such.

#### Email

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
- *Mail flow by domain*: for each of the tenant's domains, how much mail
  came in, went out and passed between colleagues, and how much was
  delivered, sent to Junk, quarantined or failed, counted per day from the
  message trace and kept for 30 days (only counts, never subjects or
  addresses). Domains where more than 5% of mail fails are flagged. Also
  Microsoft's top 20 spam and phishing recipients for the last 30 days
  (`Get-MailTrafficSummaryReport`).
- *Mailbox holds and archiving*: which mailboxes are on litigation hold or
  have an online archive, leavers' mailboxes (blocked or converted) that
  aren't on hold, and large mailboxes without an archive that are close to
  their quota.
- *Automatic replies*: mailboxes sending an out-of-office reply (Graph,
  `MailboxSettings.Read`), who outside hears it, and replies on for over 30
  days on an active account, dated from the kept snapshots.
- *Distribution lists*: members, owners and who outside can mail each list,
  with empty, ownerless and unused lists flagged. How much mail each gets is
  counted from the message trace (`Get-MessageTraceV2`, read 10 days at a
  time from where the last run stopped) and kept per day for 90 days.
- *Mobile devices*: phones and tablets syncing mail (`Get-MobileDevice` and
  `Get-MobileDeviceStatistics`), with which app and when each last synced;
  leavers' devices still connected, ones waiting for approval and ones
  unused for 90 days come first.
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
- *Mail rules and protection*: every mail flow (transport) rule in the order
  it runs, with what it picks mail out by, what it does and its exceptions
  in plain words; inbound and outbound connectors (servers, domains,
  encryption); and the anti-spam, anti-phishing and anti-malware policies,
  with Safe Links and Safe Attachments where Defender for Office 365 is
  licensed, each with whom it applies to and whether a Standard or Strict
  preset covers them (`Get-TransportRule`, `Get-InboundConnector`,
  `Get-OutboundConnector` and the policy and rule cmdlets of each kind).
  Rules that skip spam filtering or send mail outside, sender domains allowed
  past the spam filter and protection turned off come first.

#### Files and Teams

- *SharePoint sites*: every site's storage and growth, sites shared outside
  the organisation and inactive sites, from Microsoft's usage reports.
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
- *OneDrive*: the OneDrives of people who've left, what each person shares
  from their OneDrive with anyone, the whole organisation or people outside,
  and every OneDrive with its storage against its quota, files and last
  activity, with storage over time.
- *Shared files*: every file and folder shared with anyone, with people
  outside the organisation or with the whole organisation. Needs
  `Sites.Read.All`.
- *File and folder permissions*: who can open each shared file or folder
  in SharePoint and OneDrive and how (given access directly, sent a link,
  an anyone or organisation-wide link), who shared it and when, what was
  newly shared in the last 30 days, links past their expiry or expiring
  within 30 days, and access by person with guests and outside addresses
  first. After a library's first walk, each night asks Microsoft only for
  what changed in it and re-reads those items' permissions; every library
  is walked whole again after 30 days. Needs `Sites.Read.All`.
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
- *Group membership*: who is in which team and Microsoft 365 group: each
  person's teams and groups, each group's members, and every membership in
  one table. Security groups and distribution lists aren't included.
- *Empty, nested and dynamic groups*: groups with no members, security
  groups and distribution lists that contain other groups (and how many
  levels deep), dynamic groups with their rule, flagging paused ones, teams
  and groups about to expire (and whether an owner can renew them),
  role-assignable groups, and blocked accounts still in groups.
- *Files and mailbox access*, for clients with the [audit log add-on](docs/audit-log-addon.md): who deleted, downloaded and shared files, who opened someone else's mailbox, and the inbox rules, forwarding and mailbox permissions set up, from the client's Microsoft 365 audit log.

#### Devices and apps

- *Devices*: operating systems with Windows support dates, Intune devices
  that are non-compliant, unencrypted, out of support or not checking in,
  every managed device, which Windows computers joined to Entra ID have
  their BitLocker recovery key saved there (never the key itself), and
  every Entra ID device record by last activity. Tenants without Intune
  still get the Entra records and Windows versions.
  Also which computers back up their local admin password with Windows LAPS (never the password), with its own check.
- *Installed apps*: every app Intune has found on managed devices and how many devices have it, each app's versions newest first, and remote access tools (AnyDesk, ScreenConnect, TeamViewer and others) flagged to check they're expected.
- *Intune policies*: which platforms have enrolled devices but no
  compliance policy, what each compliance policy requires, configuration
  profiles and Windows update rings (deferrals, restart deadline, paused),
  endpoint security and settings catalog policies, and app protection for
  iPhones and Android phones (PIN, sharing, copy and paste, backups), each
  with who it's assigned to. Policies assigned to nobody and paused update
  rings are flagged.
- *Apps and consents*: third-party apps with what they were granted
  (application permissions, tenant-wide and per-user consent), and app
  registration secrets or certificates expiring.
- *Unused apps*: every app from outside Microsoft with when it last signed
  in (signing people in, on its own, or called as an API), from Entra ID's
  app sign-in activity (needs Entra ID P1). Apps unused for 90 days that
  still hold application permissions or consent are flagged.

#### Tenant and licences

- *Secure Score trend*: Secure Score over time and month by month, the
  controls whose points changed in the last 30 days, and where the points
  are: each control's points now against the most possible, with its user
  impact and effort.
- *Security alerts*: Microsoft Defender's open incidents and alerts, most severe first, with who is dealing with each and the accounts and devices each names, then the alerts resolved in the last 30 days and what they turned out to be. A check fails while a high or medium alert is open. Never the email, file or command behind an alert.
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
- *Licence waste*: paid licences nobody uses (unassigned, or on disabled or
  inactive accounts), by subscription and in money a month and a year at
  your prices; the disabled and inactive accounts holding them, most
  expensive first; and people holding a licence whose features another
  licence they have already includes. Free and trial licences are left out.
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
- *Microsoft 365 usage*: who used email, Teams, OneDrive, SharePoint and
  the Office apps in the last 30 days (share of licensed people per
  service, and a trend from Office Sentry's own history), when each person
  was last active in each service, Office apps by platform (Windows, Mac,
  mobile, web), Office installs per person, and email and Teams activity
  counts. From Microsoft's usage reports (`Reports.Read.All`).
<!-- since 2.6.0 -->
- *Microsoft 365 Copilot*: who uses the Copilot licences you pay for (share
  of people with Copilot who used it in the last 30 days, with a trend from
  Office Sentry's own history), in which apps (Copilot Chat, Teams, Outlook,
  Word, Excel, PowerPoint, OneNote, Loop), and a chart of how many used it
  each day over Microsoft's last 90 days. Unused Copilot licences (not used
  in 30 days, never used, or on a blocked account; licences given in the
  last 14 days aren't counted yet) with what each costs a month at your
  prices, licences bought but not assigned, and the busiest people without
  Copilot, by Teams meetings, messages and emails sent, to give an unused
  licence or a trial to. A Copilot licence is told by its service plan, so
  the add-on, Copilot Business and suites that include Copilot (E7) all
  count; a suite's Copilot has no price of its own, so it isn't costed. From
  Microsoft's Copilot usage reports (`Reports.Read.All`); not offered in GCC
  High, DoD or 21Vianet.
<!-- /since -->
- *Tenant settings*: what users and guests can do without an admin
  (create tenants, register apps, join by verifying an email address,
  sign up for trials, guest directory access) against the recommended
  value, and the admin consent request workflow (reviewers, emails,
  expiry).
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
- *Service health*: whether each Microsoft 365 service is working for the client, Microsoft's open outages and advisories and those resolved in the last 30 days, and the message centre's upcoming changes, those with a date to act by first (flagged when due within 30 days), then major changes.

### Checks

71 checks in 14 areas: Accounts, Admins, Apps, Devices, Email, Groups,
Guests, Hybrid identity, Identity, Licences, Sharing, Storage, Tenant
settings and Threats. Each check has a title that states its goal ("No inactive
accounts"), a summary of what was found ("4 accounts inactive for 90+ days"),
a recommendation that says what to do and where, and, when it fails, a
severity taken from its worst finding. A check whose data hasn't been
collected, or which the client's licences can't support (Intune checks
without an Intune licence, risky users without Entra ID P2), shows as not
checked with the reason, rather than passing or failing. [The check
catalogue](docs/checks.md) lists every check with the data it reads, what it
needs and its recommendation.

### Month-end reporting

**Month-end** in the menu (`month_end.py`, for roles that review month-end
reports) is where each client's monthly report is checked before it goes out.
A **report plan** (*Month-end → Report plans*, for people who manage
settings) is one monthly recipe (which reports, the day and hour in a
timezone, the subject and message with `{client}` and `{month}`) applied to
many clients at once. It gives each client an ordinary email schedule, so it
sends like any other. A plan either sends by itself or waits for approval, and
either emails the client's contacts or only publishes to the client portal.
The Email reports and Report plans pages show which clients have no monthly
report, or nobody to send it to, with a fix for each.

The **queue** (`/reporting/<yyyy-mm>`) lists every client on a plan for the
month as Draft, In review, Approved or Sent, with filters for your own, those
needing review and those ready to approve. It flags new critical findings
since the last report, data older than 24 hours, a data source not collected,
checks that couldn't run and nobody to email. A client's **review** screen has the month's verdict and commentary,
*What we recommend next* (kept from month to month), notes on failing checks
(for the client or for staff only), who is reviewing it, what's in the report
(see Report designs below) and a preview of the client's copy. **Approving**
one client, or many ticked at once, renders the files once, keeps them in the
client's report history and emails those exact files (or only publishes them,
for a portal-only plan). It runs in the background, so approving 200 clients
returns at once and the page shows progress; a month is still sent once if
the worker restarts, and emails go out at Exchange Online's 30 messages a
minute per mailbox, waiting out Microsoft's "too many messages" answers.
Approving a month again re-issues it, with a reason, as the next version.
Clients never see the queue, the reviewers or staff-only notes, and nothing
written for the month reaches them until it's approved or sent.

### Custom reports: your own client reports

**Reports → Custom reports** builds a client report from blocks, the way a website builder builds a
page. Drag a block in from the list (a status verdict, key figures, checks by severity, the Secure Score
trend, recommended next steps, failing checks, what changed, licences, a table from any detail report,
the insurance questionnaire answers, this month's commentary, headings, your own text, a contact card, a
page break), set it up in its panel (which severities, how many rows, the heading), put two blocks side
by side, and watch the preview on a real client update as you go. The preview is drawn at the printed
page's width, so it lays out as the PDF does; click a block in it to change that block, and Undo (Ctrl+Z)
takes back any change. The cover is a page of its own, or a band on page one for a short report. Start
from an executive summary, a full security review, a licensing review, a quarterly review (three months
of Secure Score and findings, what was fixed, the projects on the table and where each stands, licence
spend, and what's coming up in the next 90 days) or a blank page.

A custom report belongs to the MSP and works for every client: a block with nothing to show for a client
(no accepted risks, no commentary yet) is left out of that client's report. Each client's **Reports →
Custom reports** lists every one as a page and a PDF; the client's Reports tab, the "All reports" menu
above a client's reports and the Reports page list them too, and a report plan or a client's email schedule
can send one (as a PDF) alongside or instead of the monthly review: *Send on a schedule* beside a custom
report on a client's Custom reports page starts a schedule with it ticked, and that page says which of the
client's emails already send each one. Text blocks and the cover's title
and subtitle take `{client}`, `{month}`, `{quarter}`, `{msp}` and `{date}`, and a custom report holds up to 60 blocks.
Anyone who can *Design custom reports* makes and edits them; removing
one also needs *Settings*, since it comes out of any plan or schedule that sent it.

Setup notes written for your team (a detail report saying Microsoft is hiding people's names, and how to
sign in as a Global Admin to show them) stay out of the client's copy unless the report's **Include setup
notes in the client's copy** is ticked; the portal and the previews show them, marked "Only your team sees
this". On the month-end review screen, **What's in the report** leaves any block of a custom report, any part of
the monthly review or any key figure out of one client's copy for that month, and **Leave out** does the
same for one check or recommendation. The custom report and other clients stay as they are, the preview beside
it shows the client's copy, and the sent PDFs, spreadsheet, email and portal copy never have what was left
out.

### Exchange data

Mailbox forwarding, permissions and mail settings come from the Exchange
Online admin API (the same one the ExchangeOnlineManagement module uses),
limited in code to a fixed list of `Get-` cmdlets. It needs the app's
`Exchange.ManageAsApp` permission and, in each client tenant, the **Global
Reader** role assigned to the app (Entra admin center → Roles and admins →
Global Reader → Add assignments). Inbox rules are read through Graph, so they
work without it.

## Documentation

- [What Office Sentry reads](docs/what-office-sentry-reads.md): for clients
  asked to approve access. Every permission, what is and isn't read, where the
  data is kept and how to remove access.
- [Onboarding a client](docs/onboarding-a-client.md): for technicians. Adding
  a client, consent, Global Reader, the first collection, connection health,
  the errors on the Data collection page, new permissions to approve, pausing,
  archiving and offboarding.
- [Staff sign-in with Microsoft](docs/staff-sign-in.md): setting up the
  sign-in app, assigning roles in Entra, linking accounts, and turning
  passwords off and back on.
- [Client sign-in with Microsoft](docs/client-sign-in.md): letting your
  clients' people sign in with their own work account, adding them, and their
  administrator's approval.
- [Deploy guides](docs/deploy/README.md): Portainer and other container
  managers, Unraid, Synology and TrueNAS, Kubernetes, Azure, AWS, other clouds
  and Cloudflare.
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
- [The audit log add-on](docs/audit-log-addon.md): an optional second app that keeps who deleted, downloaded or shared files and who opened someone else's mailbox, for the clients who approve it.

## Sign-in security

- **Access is denied by default.** A person has a role (a set of capabilities:
  which report areas open for them, downloads, refreshing data, accepting
  risks, emailed reports, month-end review and approval, projects, custom
  reports, managing clients, users or settings) and a scope (every client, or
  the clients and groups granted to them). There are seven built-in roles
  (Administrator, Team leader, Second line, First line, Account manager,
  Auditor, Client viewer). *Settings → Users → Roles* changes what a built-in
  role holds (not Administrator) or puts it back to its default, and adds custom roles,
  from scratch or as a copy, with a preview of the menu and client tabs they
  open; changing a role signs out everyone holding it. On a person's page,
  **Customise** gives or takes away single capabilities for them, everywhere
  or on one client, and taking away always wins. Users and roles can be
  handed to someone who isn't an Administrator: they give out only what they
  hold, only on clients they see, and can't change their own access or anyone
  who holds more. Every page and download checks on the server; hidden
  clients return 404 and a capability taken away on one client answers 403
  there. There is always at least one Administrator.
  Pages only link to what the person can open, and only show buttons they can
  use (`access.can_open`): a name or report they can't open shows as text.
- **Sign in with Microsoft** (optional, off until an admin sets it up): staff sign
  in with their own Microsoft Entra ID account through OpenID Connect, under the
  MSP's own Conditional Access and MFA. *Settings → Microsoft sign-in* creates a
  small single-tenant app in the MSP's tenant with the same device-code wizard
  as the app connection (`sso.py`, `app_setup.py`); the read-only client app and
  its permissions are untouched. The app has an app role for each built-in role
  (Admin, TeamLeader, Analyst for Second line, FirstLine, AccountManager,
  Auditor), and a custom role can be given an app role value of its own; they're
  assigned to people or groups in Entra and read from the ID token at every
  sign-in ([docs/staff-sign-in.md](docs/staff-sign-in.md)). Which clients
  someone sees is still granted here, so tenant isolation doesn't change.
  Existing accounts link from the Account page or by a matching username; new
  staff get an account at their first sign-in. Passwords
  stay as the break-glass route until an admin signed in with Microsoft turns
  them off (`python -m officesentry staff-sign-in --passwords on` turns them
  back on from the server). An admin with no password confirms a security
  change (setting a password, signing someone out, resetting two-step) by
  signing in to Microsoft again (`prompt=login`), which counts for ten minutes.
  [docs/staff-sign-in.md](docs/staff-sign-in.md) has the steps.
- **Client sign-in with Microsoft** (optional, off until an admin turns it on):
  a client's people sign in with their own work account, from their own
  company's tenant. The same sign-in app is made multi-tenant and the button
  goes to `organizations`; the ID token's issuer must be its own `tid`, and
  the `tid` decides the path: the MSP's tenant is staff, any other tenant can
  only reach a client account (never created, never staff) matched on that
  `tid` and the person's `oid`, which staff took from the client's collected
  directory when they added them, granted a client whose Directory ID is that
  `tid`, in the sign-in app's cloud and not archived. App roles in a client's
  token are ignored, and such an account only ever sees clients in its own
  organisation. The client's
  administrator can approve the sign-in scopes for their organisation from a
  link on the client's *Settings → Client sign-in* page, which is also where
  staff add the client's people and copy the message to send them
  ([docs/client-sign-in.md](docs/client-sign-in.md)).
- **Two-step sign-in** (a 6-digit code from an authenticator app) is required
  for everyone on new installs, client accounts included; accounts that sign
  in with Microsoft and have no password rely on their organisation's MFA
  instead. Installs set up
  before October 2026 keep requiring it for staff only, so
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
  both from the user page. Five wrong passwords where a signed-in browser is
  asked for it again sign that browser out, without locking the account.
  Details in [DEPLOY.md](DEPLOY.md#sign-in-protection).
- **Sessions are stored on the server.** Signing out ends the session, so a
  copied cookie stops working. People can see where they're signed in and
  sign out other browsers on their **Account** page. Sessions end after 2 hours
  without activity or 8 hours in total, and when a password, role, access or
  two-step setting changes.
- **Activity log:** Administrators and Team leaders see sign-ins and every
  change, with the address, under *Settings → Activity log* (filters, paging,
  CSV). Failed sign-ins are kept for 90 days, the rest for good unless
  *Settings → Data retention* sets a period (never under a year). A custom
  role given the activity log that only sees some clients reads only the
  entries about those clients.
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

To try it from a copy of this repository, in `v2/`:

```bash
cp .env.example .env          # set OFFICESENTRY_BASE_URL, OFFICESENTRY_DOMAIN and OFFICESENTRY_TIMEZONE
echo COMPOSE_PROFILES=https >> .env    # Caddy, for HTTPS
docker compose up -d
```

That runs the published release image. To run this folder's code instead,
add `docker-compose.build.yml` (`-f docker-compose.yml -f
docker-compose.build.yml up -d --build`). For a real install, start from the
release's own files as in [DEPLOY.md, "First start"](DEPLOY.md#first-start),
which also creates the first admin and the app connection. The rest of
[DEPLOY.md](DEPLOY.md) covers HTTPS, schedule and retention settings, backups
and updating, by hand or with the optional Update button (*Settings →
Updates*, `updates.py`, and the `updater` container, `updater.py`).

Until the first collection, the home page shows a **Get started** checklist
(`first_run.py`): the app connection, the first client, its consent and its
first collection, each read from what Office Sentry has stored, so it ticks
itself off as you go. *Settings → App connection → Sign in to Microsoft*
creates the app: Office Sentry generates the certificate, registers the
multi-tenant read-only app in your tenant and saves the connection. The
sign-in's token is used once, in memory, and never stored; the device code is
kept encrypted only while the page waits (`app_setup.py`). `create-app` does
the same from the command line, and both reuse a certificate generated
earlier rather than making a second connection. Where Conditional Access
blocks device sign-in, *Other ways to create the app* on the same page walks
through creating it by hand ([Troubleshooting](docs/troubleshooting.md#creating-the-app-fails)).
Then [Onboarding a client](docs/onboarding-a-client.md) adds the first client
and follows it through consent, Global Reader and its first collection.

## Managing clients

- **Settings** in the menu opens *Client settings*. Every settings page opens with the Settings heading, its four areas as tabs and the open area's pages under them, then the page's own title: *Clients* (Client settings, Client groups, App connection), *Reports and email* (Branding, Licence prices, Email delivery, Alerts), *Team* (Users, Microsoft sign-in, Activity log) and *System* (System status, Data retention, Updates, Support). These docs name the page, as in *Settings → Email delivery*.
- **Switch client**, beside a client's name (staff who see more than one client): opens the page you're on for another client you see, within the client group you picked, with a box to find one by name. From an account, mailbox, group or site it opens that client's list, and a report the other client doesn't have, or that you can't open there, opens its Reports tab.
- **Client settings** (*Settings → Client settings*, or *Connection health* on
  the Clients page) lists every client with its status (*Onboarding*,
  *Healthy*, *Needs attention*, *Paused*, *Archived*), consent, missing
  permissions, Global Reader, last good collection and failing data sources,
  worst first.
- **Onboarding many clients** (*Settings → Client settings → Onboarding*, for
  people who can manage clients): **Import from CSV** adds clients from a
  spreadsheet (name, tenant ID or domain, and optionally groups, a contact and
  the cloud), showing every row and what's wrong with any of them before
  anything is added; clients already there are skipped. The tracker then
  follows each client through consent (Not sent, Sent, Approved, Consented,
  Global Reader missing, Connected), with its consent link, an email to send,
  a consent pack PDF and a CSV of every link for a mail merge
  ([Onboarding a client](docs/onboarding-a-client.md#onboarding-many-clients)).
- A client's **Settings → Manage** page (staff, not client accounts) shows its connection health:
  consent, sign-in, which of the app's permissions the tenant actually granted,
  whether the app holds Global Reader (or more than it needs), licences, last
  collection and failing data sources, each with the fix. It's read by the
  `connection` collector with every collection (GET only, from permissions the
  app already has); *Check connection now* queues it straight away.
- People who can manage clients can rename a client, change its tenant ID
  (until data has been collected) or app connection, keep notes, **pause** it
  (no collection, emailed reports or alert emails; data stays) and **archive**
  it (also hidden from lists, dashboards, rollups, digests and its client
  users; restorable).
- **Client groups** (*Settings → Client groups*) sort clients by tier, region,
  service level or anything else; a client can be in several. Pick a group at
  the top of the Clients page, What changed, Month-end or
  Client settings to see only those clients, with downloads to match, and give
  a digest one group instead of every client. Groups never show anyone more:
  the only way a group grants access is someone who manages users ticking it
  on a person's user page, which then covers every client in it, including
  ones added later. Client accounts are never given clients through a group.
- **What's new** (*Settings → Client settings → Onboarding*): when a release
  adds permissions, the tracker lists the clients still to approve them, and
  each client's *What's new* page has the email to send, the approval link and
  a PDF saying what each new permission reads and adds. *Settings → App
  connection → Update the app* (or `python -m officesentry update-app`) adds
  them to your app registration first. Until a client approves, it stays
  *Healthy* and the reports that need them say they're waiting.
- **Offboard** walks through a final export, the client's side (remove Global
  Reader, delete the enterprise app) and what will be deleted, then deletes the
  client and everything stored for it after you type its name, optionally with
  client accounts that had no other client. `python -m officesentry
  delete-tenant <id>` does the same from the server.
  The client's side also names the client sign-in app and the audit log add-on, deleted the same way, when the client may have approved them.

## National clouds (GCC High, DoD, China)

Each client has a **Microsoft cloud**, chosen when it's added (or later under
**Settings → Manage**):

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

Developing Office Sentry: [DEVELOPING.md](../DEVELOPING.md).
