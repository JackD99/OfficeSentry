# Microsoft 365 reporting tools for MSPs compared

If you look after Microsoft 365 for several clients, reporting comes from one of four places: Microsoft's own
admin centres, PowerShell scripts, management platforms that also report (such as CIPP), and dedicated
reporting tools (such as AdminDroid and Office Sentry). Each is good at something different, and plenty of
MSPs use two of them together. This page sets out which is good at what.

Checked in October 2026 from each product's own site and documentation. Products change, so check the
details that matter to you with the vendor.

## At a glance

| | Main job | Cost | Changes tenants | History |
|---|---|---|---|---|
| **Microsoft admin centres** | Running one tenant | Included | Yes | 30 days of logs |
| **PowerShell scripts** | Whatever you write | Free, plus your time | If you write it to | What you keep |
| **CIPP** | Managing many tenants | Free to self-host (about €25 a month of Azure), or €99 a month hosted | Yes | Not its focus |
| **AdminDroid** | Reporting and auditing | Free edition; paid editions priced on request | Yes | Audit records for years |
| **Office Sentry** | Reporting for MSPs | Free, on a small server of your own | Never | Each day for 30 days, then each month for a year, by default |

### Microsoft admin centres

Included with every tenant and the place to change anything. They show one tenant at a time; Microsoft 365
Lighthouse adds some views across clients for partners. Sign-in and audit logs go back 30 days (7 without
Entra ID P1 or P2) and usage reports up to 180 days, and there's nothing ready to hand to a client.

### PowerShell scripts

Free and as flexible as you make them, running on your own machine or in Azure Automation, with an app or a
sign-in per tenant. History is whatever you save, and every report for a client is one you build and keep
working as Microsoft's modules change.

### CIPP

Open source (AGPL-3.0) and built for managing many tenants: users, mailboxes, licences, standard settings and
alerts. It runs in your own Azure subscription or on CyberDrain's hosting and reaches clients through GDAP in
Partner Center. It has some reports, but keeping history isn't its job.
[Office Sentry and CIPP compared](cipp.md).

### AdminDroid

A commercial reporting and auditing tool on a Windows server you run. The paid editions have thousands of
reports, keep the unified audit log (file, sharing and mailbox activity) for years and can make changes from
the same screen. It reaches clients through an app in each tenant or GDAP, switches between tenants or groups
them, and exports reports or emails them on a schedule.
[Office Sentry vs AdminDroid](admindroid.md).

### Office Sentry

Open source (AGPL-3.0), read-only and built for MSPs: two Docker containers on a server you run (Linux, or
Windows without Docker), with one read-only app that each client's admin approves from a link, or your
technician approves through GDAP. It has 49 detail reports and 69 checks, and every report has a view across
all clients, worst first. History is kept (each day for 30 days and each month for a year by default, longer
if you choose), so each report shows what changed since last month. Sign-ins and admin changes go back
beyond Microsoft's 30 days. For clients it writes a branded monthly review, evidence for Cyber Essentials,
ISO 27001, CIS and other schemes, and insurance questionnaires, as PDF and Excel, which you approve before
they go.

## Which fits

- **A couple of clients and the odd question:** the admin centres and a few scripts are enough.
- **Changing settings across many tenants** (new users, standard settings, alerts): CIPP is built for that.
- **The biggest catalogue of reports and a long audit trail** of file, mailbox and sharing activity, with
  alerts as it happens: AdminDroid.
- **Monthly reporting to clients**, with history, evidence for Cyber Essentials and insurers, and every client
  side by side, from something that can't change a tenant: Office Sentry.

Many MSPs combine a management tool with a reporting one: CIPP to make the changes, Office Sentry to report
on them and keep the record.

## What Office Sentry doesn't do

Being plain about it saves everyone time:

- It never changes anything in a tenant. That's deliberate: clients and insurers can approve it knowing it can
  only read.
- It doesn't read the unified audit log (file, sharing and mailbox activity) today, or on-premises Active
  Directory.
- It has 49 detail reports with filters, saved views and exports, not thousands of report variants.
- It reports once a night (or when you ask), not in real time, and alerts by email, Teams, Slack or a
  webhook rather than through a PSA.
- You run it yourself, on a Linux server with Docker (or Windows without it). There's no hosted version, and
  support is the docs and GitHub Discussions.

## Compare in detail

- [Office Sentry vs AdminDroid](admindroid.md)
- [Office Sentry and CIPP](cipp.md)

[Try the live demo](https://demo.officesentry.co.uk) · [Install Office Sentry](../../../README.md#quick-start)
