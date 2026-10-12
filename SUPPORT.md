# Getting help

Office Sentry is free software looked after by its maintainer and the people who use it. There is no paid
support and no support contract: help comes from the docs, from other MSPs in Discussions, and from issues
the maintainer works through as time allows. This page says where to go for what, and what to send so the
first answer can be the useful one.

**Found a security problem?** Don't post it anywhere public. Follow [SECURITY.md](SECURITY.md).

## Start here

| You want to... | Go to |
|---|---|
| Fix a setup or collection error (`AADSTS50011`, Global Reader, the worker not running, a report that stays empty) | [Troubleshooting](v2/docs/troubleshooting.md) |
| Install it, or move it to another server | [Quick start](README.md#quick-start), [Running it on your own server](v2/DEPLOY.md) and the [deploy guides](v2/docs/deploy/README.md) for Portainer, a NAS, Kubernetes or a cloud |
| Let your team sign in with their Microsoft accounts | [Staff sign-in with Microsoft](v2/docs/staff-sign-in.md) |
| Let your clients' people sign in with their own work accounts | [Client sign-in with Microsoft](v2/docs/client-sign-in.md) |
| Add a client and get consent | [Onboarding a client](v2/docs/onboarding-a-client.md) |
| Know what a report or check does, or what it reads | [Reports and features](v2/README.md), the [check catalogue](v2/docs/checks.md) and [What Office Sentry reads](v2/docs/what-office-sentry-reads.md) |
| Ask "how do I...?" or "is it possible to...?" | [Discussions, Q&A](https://github.com/JackD99/OfficeSentry/discussions/categories/q-a) |
| Report something that doesn't work | [Open a bug report](https://github.com/JackD99/OfficeSentry/issues/new?template=bug.yml) |
| Ask for a new report, check or feature | [The request forms](https://github.com/JackD99/OfficeSentry/issues/new/choose) |
| See what's coming | [ROADMAP.md](ROADMAP.md) |

The same docs are on the website with search: [officesentry.co.uk/docs](https://officesentry.co.uk/docs/).

## Before you report a bug

1. **Update first.** Fixes only go into the newest release ([CHANGELOG.md](CHANGELOG.md) lists them). The
   version is at the foot of the portal's menu and in `/health`, and *Settings → Updates* says whether a newer
   one is out.
2. **Check Troubleshooting** for the error text. Most setup problems are there with the fix.
3. **Get the support package.** In the portal, open **Settings → Support** and download the support
   package: one zip with the versions, health, settings, database and disk sizes and the recent log of your
   install. If the portal won't open, `docker compose run --rm -T web python -m officesentry support-package
   --out - > support.zip` (or `python -m officesentry support-package` without Docker) makes the same zip. It
   contains no secrets or client names unless you tick *Include client identifiers* (leave that off for a
   public issue); [Getting help](v2/docs/getting-help.md) lists exactly what's in it. If a page showed
   "Something went wrong on our side", note the reference it gave: it's also on the error's log line and in
   the `X-Request-ID` header, and the bug form has a field for it. (Older versions don't have the package; the
   bug form asks for the same facts by hand instead.)
4. **Open the bug form** and attach the package. Say what you did, what you saw and what you expected,
   and paste the error from the run's page or `docker compose logs web` / `docker compose logs worker`.

## What never goes in an issue

Issues and Discussions are public and stay public. Before you paste anything, replace tenant names and IDs,
domains, user names and email addresses, IP addresses, certificate thumbprints and client IDs with made-up
ones such as `contoso.example`. Keep Microsoft's error codes (`AADSTS50011`, `Authorization_RequestDenied`):
they help and identify nobody. Never attach a report, an export or a screenshot from a real client, and
never attach `secrets.json`, a `.pfx` or `.env`.

## What to expect

The maintainer reads every issue. Bugs that break a collection, a report a client receives or an install
come first; requests are weighed against the [roadmap](ROADMAP.md). An issue labelled **needs info** is
waiting for something from you, usually the support package; without an answer it's closed after a while
and can be reopened. There is no promised response time.

If you can fix something yourself, pull requests are welcome: [CONTRIBUTING.md](CONTRIBUTING.md) and the
[developer guide](DEVELOPING.md) say how, and issues labelled **good first issue** and **help wanted** are
a good place to start.
