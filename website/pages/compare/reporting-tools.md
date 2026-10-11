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
| **CIPP** | Managing many tenants | Free to self-host, or €99 a month hosted | Yes | Not its focus |
| **AdminDroid** | Reporting and auditing | Free edition; paid editions priced on request | Yes | Audit records for years |
| **Office Sentry** | Reporting for MSPs | Free | Never | Every night kept |

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

Open source (AGPL-3.0), read-only and built for MSPs: two Docker containers on a Linux server you run, with
one read-only app each client approves (or GDAP if you have it). Every report has a view across all clients,
worst first, and every night is kept, so each report shows what changed since last month and sign-ins and
admin changes go back beyond Microsoft's 30 days. For clients it writes a branded monthly review and Cyber
Essentials and insurance evidence, as PDF and Excel.

## Which fits

- **A couple of clients and the odd question:** the admin centres and a few scripts are enough.
- **Changing settings across many tenants** (new users, standard settings, alerts): CIPP is built for that.
- **The biggest catalogue of reports and a long audit trail** of file, mailbox and sharing activity, on a
  Windows server: AdminDroid.
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
- It has more than 40 detail reports with filters, saved views and exports, not thousands of report variants.
- You run it yourself, on a Linux server with Docker. There's no hosted version.

## Compare in detail

- [Office Sentry vs AdminDroid](admindroid.md)
- [Office Sentry and CIPP](cipp.md)

[Try the live demo](https://demo.officesentry.co.uk) · [Install Office Sentry](../../../README.md#quick-start)
