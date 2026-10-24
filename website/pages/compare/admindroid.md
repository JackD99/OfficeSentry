# Office Sentry vs AdminDroid

AdminDroid and Office Sentry both report on Microsoft 365 and both run on your own server, so your clients'
data stays with you. They're built for different jobs. AdminDroid is a broad reporting, auditing and
management tool for one organisation's Microsoft 365, with MSP support added on. Office Sentry is a
read-only reporting tool built for MSPs from the start: every client side by side, history kept every night,
and reports written for clients.

Checked in October 2026 from AdminDroid's website, licensing FAQ and release notes. If something here is out
of date, [tell us](https://github.com/JackD99/OfficeSentry/issues/new/choose) and we'll correct it.

## Side by side

| | AdminDroid | Office Sentry |
|---|---|---|
| **Cost** | Free edition (around 140 Microsoft 365 reports and 10 dashboards); paid editions priced on request; a 15-day trial of everything | Free, every feature |
| **Licence** | Commercial | Open source (AGPL-3.0) |
| **Runs on** | A Windows server, with PostgreSQL | Two Docker containers on a Linux server, with SQLite, or Windows without Docker |
| **Reports** | Over 3,500 in the paid editions, plus dashboards | 57 detail reports with filters, saved views and exports, 71 checks, and client reports and evidence for 11 schemes |
| **Audit trail** | The unified audit log: file, sharing, mailbox and Teams activity, kept for up to 10 years, with alerts on events | Entra sign-ins and admin changes kept beyond Microsoft's 30 days; file, sharing and mailbox activity through the optional audit log add-on, kept 2 years by default |
| **Changes tenants** | Yes: management actions, bulk changes and workflows | Never: read-only permissions only |
| **Several clients** | Switch between tenants, group them, connect through GDAP | Every report across every client, worst first, and what changed this month across all of them |
| **Month-over-month** | Some reports over time | Every report compared with last month, and findings with when they were first seen and fixed |
| **Reports for clients** | Exports (PDF, Excel, CSV, HTML), scheduled emails, rebranding in paid editions | A branded monthly review you approve before it goes, Cyber Essentials, CE Plus, ISO 27001 and insurance evidence, quarterly reviews and project proposals, emailed on a schedule |

## Where AdminDroid is stronger

- **Breadth.** If a report exists for Microsoft 365, AdminDroid probably has a version of it, across more
  services (Power BI, Viva Engage, Stream) and on-premises Active Directory.
- **Activity auditing.** It reads the unified audit log for every service, Teams included, keeps it for up
  to 10 years and alerts as it happens. Office Sentry's add-on reads SharePoint, OneDrive and Exchange
  activity each night.
- **Making changes.** Its management actions, bulk operations and workflows let you fix what a report finds
  from the same screen.

## Where Office Sentry is stronger

- **Built around several clients.** The Clients page shows any report for every client, one row each, and every figure
  opens the rows behind it. Clients can be grouped by tier, sector or region.
- **History is the product.** Each day's results are kept for 30 days and each month's for a year by default
  (longer if you choose), so every report says what changed since last month and every finding records when
  it appeared and when it was fixed.
- **Reports a client will read.** The Monthly Security & Licensing Review leads with a verdict and plain
  English, carries your branding, and can email itself once you've approved it. Cyber Essentials and insurance
  questionnaires are answered from the data, control by control. A client's own people can sign in to see
  their reports.
- **Read-only by construction.** It asks clients' tenants for read permissions only and has no code path that
  writes to them, which makes it an easy yes for clients and insurers.
- **Free and open.** No editions, no per-user pricing and no telemetry: the only thing it asks the outside
  world is GitHub's list of releases, once a day, and an admin can turn that off. The source code is public.

[See an example monthly review (PDF)](../../../examples/monthly-security-licensing-review.pdf) ·
[Every example report](../../../examples/README.md)

## Which to choose

Choose AdminDroid if you need the widest report catalogue, a file and mailbox audit trail with alerts as it
happens, or the ability to change things from your reporting tool.

Choose Office Sentry if your job is monthly reporting to many clients, with history, evidence and a view
across all of them, from a tool that can only read.

[Try the Office Sentry demo](https://demo.officesentry.co.uk) ·
[All the tools compared](reporting-tools.md) · [Install Office Sentry](../../../README.md#quick-start)
