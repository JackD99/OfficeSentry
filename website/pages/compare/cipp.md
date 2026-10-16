# Office Sentry and CIPP

CIPP, the CyberDrain Improved Partner Portal, is the open-source tool many MSPs use to manage their clients'
Microsoft 365: creating and offboarding users, applying standard settings across every tenant, and alerting.
Office Sentry does a different job: it reads every client's Microsoft 365 each night, keeps the history and
turns it into the reports you send. They work well side by side: CIPP to make changes, Office Sentry to
report on them.

Checked in October 2026 from CIPP's documentation and FAQ. If something here is out of date,
[tell us](https://github.com/JackD99/OfficeSentry/issues/new/choose) and we'll correct it.

## Side by side

| | CIPP | Office Sentry |
|---|---|---|
| **Main job** | Managing many tenants: users, mailboxes, licences, standards, alerts | Reporting on many tenants: monthly reviews, evidence, detail reports, history |
| **Changes tenants** | Yes, that's the point | Never: read-only permissions only |
| **Cost** | Free to self-host (CIPP's FAQ puts the Azure costs at about €25 a month); hosted by CyberDrain for €99 a month | Free; it runs on a server you already have, or a cloud server with 2 CPUs and 4 GB of memory |
| **Licence** | Open source (AGPL-3.0) | Open source (AGPL-3.0) |
| **Runs on** | Azure (a static web app, functions and a key vault) | Two Docker containers on any Linux server, with SQLite, or Windows without Docker |
| **Reaches clients through** | GDAP relationships in Partner Center | One read-only app that each client's admin approves from a link, or your technician approves through GDAP |
| **History** | Not its focus | Each day for 30 days and each month for a year by default: what changed since last month on every report, and sign-ins and admin changes beyond Microsoft's 30 days |
| **Reports for clients** | Reports you can brand | A branded monthly review you approve before it goes, Cyber Essentials, ISO 27001 and insurance evidence, quarterly reviews and project proposals, emailed on a schedule, and client users who sign in with their own Microsoft account |
| **Alerts** | Audit log alerts within about 15 minutes and scheduled checks, to email, a webhook or your PSA | New findings, failed collections and its own problems, to email, Teams, Slack or a webhook |
| **Your team** | Built-in roles and your own | Seven built-in roles and your own, each limited to the clients you grant |

## Why use both

- **Different permissions for different jobs.** CIPP needs admin rights to make changes. Office Sentry only
  ever asks for read permissions, so account managers, auditors and the clients themselves can use it without
  any risk of changing a tenant.
- **A record of what changed.** After CIPP applies a standard, Office Sentry's next collection shows the
  effect: findings fixed, MFA coverage up, and the before and after in the monthly review.
- **Clients outside Partner Center.** Office Sentry doesn't need GDAP. A client you don't resell licences to
  can approve its read-only app themselves.
- **They don't get in each other's way.** Office Sentry uses its own app with its own read-only permissions;
  nothing in CIPP needs to change.
- **Alerts and reports at different speeds.** CIPP's alerts tell you within minutes; Office Sentry's nightly
  collection tells the client what changed over the month.

## When one is enough

If you only need to manage tenants and the occasional report is fine, CIPP alone covers it. If you only need
reporting and evidence, or your clients want a tool that can't change anything, Office Sentry alone covers it.

[Try the Office Sentry demo](https://demo.officesentry.co.uk) ·
[All the tools compared](reporting-tools.md) · [Install Office Sentry](../../../README.md#quick-start)
