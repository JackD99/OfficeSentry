# Getting help

What to do when Office Sentry isn't working as it should: where to look
first, how to make a support package that tells a maintainer what happened
without telling them who your clients are, and how to report the problem.
Office Sentry is free and run by volunteers, so a report that arrives with
the package attached is the one that gets fixed fastest.

## Contents

- [Where to look first](#where-to-look-first)
- [The support package](#the-support-package)
- [What's in it, and what isn't](#whats-in-it-and-what-isnt)
- [The reference on an error page](#the-reference-on-an-error-page)
- [Where the logs are](#where-the-logs-are)
- [Reporting a problem](#reporting-a-problem)

## Where to look first

1. **Settings → System status.** Whether the background worker is running,
   when data was last collected, whether backups are recent and how full the
   disk is. Whatever isn't right is at the top, with the fix.
2. **The run's own page.** A failed collection says in plain words what
   Microsoft answered and what to do about it; the client's **Manage** page
   says what's wrong with its connection and where in the Microsoft admin
   centres to fix it.
3. **[Troubleshooting](troubleshooting.md).** The setup and sign-in errors
   people meet most, such as `AADSTS50011` on the consent page or mailboxes
   failing without Global Reader.
4. **Settings → Support** brings those together: the version, how many
   errors were logged in the last day, how many collections failed in the
   last two weeks, and the support package.

## The support package

One zip of everything a maintainer needs to see what went wrong, made in a
few seconds from what Office Sentry has already stored and logged. It never
asks Microsoft for anything, and it isn't kept on the server: it goes
straight to your browser (or to the file you name) and nowhere else.

**From the portal:** sign in as an admin, open **Settings → Support** and
press **Download support package**. The page lists what the zip will hold.
Making one is recorded in the activity log.

**From the server's shell**, for when the portal won't open:

```sh
# Docker: write it to the current folder on the server
docker compose run --rm -T web python -m officesentry support-package --out - > support.zip

# Without Docker, in the virtual environment
python -m officesentry support-package
```

The command works over a database that won't open and a configuration that
won't load: the zip then says so in its README and still carries the logs,
the environment and the installed versions, which is usually what's needed
to see why.

Open the zip before you send it if you like: every file is plain text or
JSON, and the README inside explains each one.

## What's in it, and what isn't

| File | Holds |
|---|---|
| `README.txt` | what's inside, what was taken out, and where to send it |
| `summary.json` | Office Sentry's version, Python, the operating system, the database's version and size, disk space, errors logged in the last 24 hours |
| `environment.json` | every `OFFICESENTRY_*` variable (secrets shown only as set or not set) and the configuration in force |
| `health.json` | the System status checks, their results and suggested fixes |
| `database.json` | tables with row counts, applied and pending migrations, settings by name (values only for plain ones such as retention periods) |
| `clients.json` | each client as a number: cloud, state, consent, capabilities, and the last run of each data source |
| `failures.json` | failed collection runs and background tasks of the last 14 days, with their error text and the run log's error lines |
| `worker.json` | the worker's last heartbeat, what's queued and running |
| `connections.json` | each app connection's cloud and certificate dates |
| `activity.json` | the last 150 actions in the activity log, without who did them or from where |
| `packages.txt` | every installed Python package and its version |
| `logs/web.log`, `logs/worker.log` | the last 512 KB of the portal's and the worker's own logs |

**Never in it:** the encryption keys and secret key, certificates and
private keys, passwords and two-step secrets, session tokens and cookies,
webhook URLs, SMTP settings beyond the port and security mode, e-mail
addresses, IP addresses, and anything a report shows (users, mailboxes,
devices, sign-ins, findings' detail).

Before any free text goes in (an error message, a traceback, a log line),
it is swept: client names and their Microsoft tenant references become
`[client 3]`, your team's user names `[user]`, addresses `[email]` and
`[ip]`, and anything that looks like a token, a key, a GUID or a long opaque
value is replaced. Microsoft's error codes (`AADSTS…`,
`Authorization_RequestDenied`) stay, because they help and identify nobody.

**Include client identifiers** on the Support page (or
`--include-identifiers` on the command) keeps client names, tenant IDs,
domains and user names, so a problem with one client can be followed by
name. The page only offers it to people who see every client, since the
package covers them all. The zip's README then says so in its first lines. Use it only for a
private channel, never a public issue.

## The reference on an error page

When a page fails, Office Sentry shows *Something went wrong on our side*
with a reference such as `3f9a1c2b4d5e`. The same reference is on the
error's line in the portal log (`req=3f9a1c2b4d5e`), with who was signed in
and which client the page was about, so quoting it in a report points
straight at the traceback. Every response also carries it as the
`X-Request-ID` header, which a reverse proxy can log.

## Where the logs are

The portal and the worker log to standard error, which Docker shows with
`docker compose logs web` and `docker compose logs worker`. Each also keeps
a short rolling copy in the data folder, `<data dir>/logs/web.log` and
`worker.log` (three files of 2 MB each per process, inside the data volume
in Docker), which is what the support package picks up: by the time someone
asks for help, the container's log has usually scrolled past the moment that
mattered.

Lines carry the context they happened in: the request's id and user for the
portal; the client, run and data source, or the task, for the worker. A
client is named by its number in the portal's address (`/tenants/<id>`).

## Reporting a problem

Open an issue with the
[Bug form](https://github.com/JackD99/OfficeSentry/issues/new?template=bug.yml)
and say:

- what you did, what you saw and what you expected instead;
- the version (Settings → Support shows it, as does the foot of the menu);
- the reference from the error page, if there was one;
- and attach the support package.

Issues are public. The package is written for that, but anything you paste
yourself (a screenshot, an error from a run's page) needs client names,
tenant IDs, domains, user names, e-mail addresses and IP addresses taken
out first.

Found a security problem? Don't post it: use
[private vulnerability reporting](../../SECURITY.md) instead.
