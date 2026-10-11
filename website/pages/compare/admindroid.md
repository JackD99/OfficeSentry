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
| **Runs on** | A Windows server, with PostgreSQL | Two Docker containers on a Linux server, with SQLite |
| **Reports** | Over 3,500 in the paid editions, plus dashboards | More than 40 detail reports with filters, saved views and exports, plus client reports and evidence packs |
| **Audit trail** | The unified audit log: file, sharing, mailbox and Teams activity, kept for up to 10 years, with alerts on events | Entra sign-ins and admin changes kept beyond Microsoft's 30 days; not the unified audit log yet |
| **Changes tenants** | Yes: management actions, bulk changes and workflows | Never: read-only permissions only |
| **Several clients** | Switch between tenants, group them, connect through GDAP | Every report across every client, worst first, and what changed this month across all of them |
| **Month-over-month** | Some reports over time | Every report compared with last month, and findings with when they were first seen and fixed |
| **Reports for clients** | Exports (PDF, Excel, CSV, HTML), scheduled emails, rebranding in paid editions | A branded monthly review, Cyber Essentials, CE Plus and insurance evidence, quarterly reviews and project proposals, emailed on a schedule |

## Where AdminDroid is stronger

- **Breadth.** If a report exists for Microsoft 365, AdminDroid probably has a version of it, across more
  services (Power BI, Viva Engage, Stream) and on-premises Active Directory.
- **Activity auditing.** It reads the unified audit log, so it can answer "who opened, shared or deleted this
  file" and "who read this mailbox", and alert as it happens.
- **Making changes.** Its management actions, bulk operations and workflows let you fix what a report finds
  from the same screen.

## Where Office Sentry is stronger

- **Built around several clients.** Every report has an All clients view, one row per client, and every figure
  opens the rows behind it. Clients can be grouped by tier, sector or region.
- **History is the product.** Every night's results are kept, so every report says what changed since last
  month and every finding records when it appeared and when it was fixed.
- **Reports a client will read.** The Monthly Security & Licensing Review leads with a verdict and plain
  English, carries your branding, and can email itself. Cyber Essentials and insurance questionnaires are
  answered from the data, control by control.
- **Read-only by construction.** It asks clients' tenants for read permissions only and has no code path that
  writes to them, which makes it an easy yes for clients and insurers.
- **Free and open.** No editions, no per-user pricing, no telemetry, and the source code is public.

## Which to choose

Choose AdminDroid if you need the widest report catalogue, a file and mailbox audit trail, or the ability to
change things from your reporting tool, and you run Windows servers.

Choose Office Sentry if your job is monthly reporting to many clients, with history, evidence and a view
across all of them, from a tool that can only read.

[Try the Office Sentry demo](https://demo.officesentry.co.uk) ·
[All the tools compared](reporting-tools.md) · [Install Office Sentry](../../../README.md#quick-start)
