# Running Office Sentry on your own server

Office Sentry runs as two containers from one image: the **portal** (the
website) and the **worker** (collects from clients, sends scheduled emails,
tidies old data). Everything it stores lives in one Docker volume, and the
keys that decrypt its stored certificates and passwords in a second one
([Where the keys are](#where-the-keys-are)).

## What you need

- A Linux server or VM with [Docker](https://docs.docker.com/engine/install/)
  installed, with at least 2 CPUs and 4 GB of RAM. Disk use grows with the
  number of clients and users (see [How much disk](#how-much-disk)); page
  times hardly do ([How fast](#how-fast)).
- A domain name for the portal, such as `sentry.yourmsp.com`, with its DNS
  pointing at the server, if you want the built-in HTTPS.

### How much disk

Measured with `scripts/scale_data.py`: 200 clients of about 110
accounts each, after two years of nightly collection with the default *Data
retention* settings.

| Clients | Database | One nightly backup | Disk to plan for |
|---|---|---|---|
| 50 | about 1 GB | about 0.3 GB | 20 GB |
| 200 | 4.0 GB | 1.2 GB (under 2 minutes) | 40 GB |
| 500 | about 10 GB | about 3 GB | 80 GB |

The 50 and 500 rows are scaled from the 200-client measurement. A client with
twice the accounts takes about twice the room. "Disk to plan for" covers the
operating system and Docker, the database, 7 nightly backups and the room a
backup needs while it's being made (about the database's size again). Of the
4.0 GB, collected data is 0.8 GB (stored compressed); the rest is run history
and trend figures, which grow by about 0.7 MB per client a month.

Before collected data was compressed the same database was 13.8 GB and 7
backups took about 97 GB. An existing install compresses its older data a few
seconds a minute in the background after updating; the file then stops
growing for a long while but doesn't shrink by itself. To get the space back
at once, run this at a quiet time (it pauses collections while it runs; on a
large database expect several minutes):

```bash
docker compose stop worker
docker compose run --rm --no-deps worker python -m officesentry compact
docker compose start worker
```

### How fast

Measured with `scripts/timings.py` on the same 200-client database (90 nights of history, 1.5 GB,
every client with open findings and on a month-end plan), on 4 cores with nothing else running.
"Normal" is what people get on an ordinary day, after the worker's nightly work; "first" is a page
nobody has opened since an upgrade or a restore, when the portal builds what the worker hasn't yet.

| Page, at 200 clients | Normal | First |
|---|---|---|
| Dashboard | 0.6 s | 3.9 s |
| Today | 0.5 s | 0.4 s |
| Accounts | 0.5 s | 6 s |
| All clients: monthly review | 0.1 s | 0.1 s |
| All clients: checks | 0.3 s | 2 s |
| All clients: insurance evidence | 0.05 s | 4 s |
| All clients: a report's rollup | 0.05 s | 0.6 to 1 s |
| All clients: every row of a report | 0.9 s | 0.7 s |
| What changed, every client | 0.2 s | 25 s |
| Projects | 0.1 s | 0.2 s |
| Coming up | 0.3 s | 0.5 s |
| Month-end queue | 0.05 s | 0.1 s |
| Search for a name | 0.15 s | 0.9 s |
| Search for an IP address | 0.35 s | 0.8 s |
| A client's own pages (overview, findings, changes, reports, a person) | 0.01 to 0.2 s | 0.03 to 0.3 s |
| PDF downloads (a client's review, every client's changes, a rollup) | 1.7 to 4.6 s | |
| XLSX and CSV downloads | 0.05 to 0.9 s | |

| Worker job, at 200 clients | Time |
|---|---|
| Rebuild of every precomputed row (after each night's collection, and once a day) | 91 s in all, about half a second a client |
| Findings sync, every client | 3 s |
| Retention pass | 2 s |
| Compressed backup (1.5 GB database, 0.77 GB on disk) | 47 s |

Pages that show every client read rows the worker keeps up to date, so their time hardly grows with
the number of clients; a PDF's time is the rendering, which is why PDFs keep the first 2,000 rows of
a table. How long the night's collection takes depends on Microsoft's rate limits and each tenant's
size, not on the server: see `OFFICESENTRY_WORKER_CONCURRENCY` below.

## First start

Running Portainer, a NAS, Kubernetes, Azure, AWS or another cloud? The [deploy guides](docs/deploy/README.md)
start from there.

1. Make a folder on the server (say `/opt/officesentry`) and open a terminal in it.
   From the newest [release](https://github.com/JackD99/OfficeSentry/releases),
   download `docker-compose.yml`, `Caddyfile` and `env.example` into it.
   They run that release: the image `ghcr.io/jackd99/officesentry`, built,
   tested and scanned by GitHub Actions, for amd64 (Intel and AMD) servers and, from 2.1.0,
   arm64 (such as Ampere and Raspberry Pi 4 or 5).
2. Create your settings file: `cp env.example .env`
3. Open `.env` in an editor (`nano .env`) and set at least:
   - `OFFICESENTRY_BASE_URL` to the address people will use, e.g. `https://sentry.yourmsp.com`
   - `OFFICESENTRY_DOMAIN` to the domain alone, e.g. `sentry.yourmsp.com`
   - `OFFICESENTRY_TIMEZONE` to your timezone, e.g. `Europe/London`

   `OFFICESENTRY_VERSION` is already set to the release you downloaded.
4. Start it, with automatic HTTPS:

   ```bash
   docker compose --profile https up -d
   ```

   Already have a reverse proxy (nginx, Traefik)? Leave out `--profile https`
   and point the proxy at `http://127.0.0.1:8000`.
5. Create the first admin account. Run `docker compose logs web`, find the line
   starting **No admin account yet** and open the link on it (`docker compose exec
   web python -m officesentry setup-link` prints it again). Choose a username and
   password, scan the QR code with an authenticator app and save the recovery codes.
6. Your home page now shows a **Get started** checklist: the app connection, your first client, its
   consent and its first collection, each ticking itself off as it happens. Press **Set up the app
   connection** (or open **Settings → App connection**) and press **Sign in to Microsoft**. Enter the code it
   shows on Microsoft's page and sign in as an admin of your own tenant who can create app registrations.
   Office Sentry creates its read-only app and then offers to add your first client. No command needed.

## Is it working?

`docker compose ps` should show `web` and `worker` as **healthy**. If the
worker stops, admins see a yellow notice at the top of every page.

To see what the worker is doing: `docker compose logs -f worker`

**Settings → System status** shows the whole picture, problems first: the
worker, the last collection, the last backup, disk space, the database and the
app certificates, each with what to do about it.

### Hear about problems without signing in

Office Sentry tells your team itself when the worker stops, a backup fails,
the disk is over 85% full or most clients fail to collect: an email to the
people under *Email reports → Your team* (once *Email delivery* is set up) and
a post to the Teams, Slack or webhook alert (once *Alerts* is set up). Each
problem is sent once when it starts, again every day it lasts, and once when
it's fixed. Turn them off under *Settings → Alerts*, "Problems with Office
Sentry".

Nothing on the server can tell you the server itself is down, so add a free
uptime monitor too. It checks this address every few minutes:

```
https://<your domain>/health/ready
```

It answers **200** while Office Sentry works and **503** when the database,
the worker or the disk has a problem. Anyone can read that much, so it's safe
to give to an outside service; it never names a client.

**Uptime Kuma** (self-hosted, on a different machine) or **UptimeRobot** /
**Better Stack** (free accounts):

1. Add a new monitor of type **HTTP(s)**.
2. URL: `https://<your domain>/health/ready`. Interval: 5 minutes.
3. Leave "accepted status codes" at 200-299, so a 503 alerts you.
4. Choose how it tells you (email, phone app) and save.

To let the monitor see *why* it failed, make a monitoring token on
*Settings → System status* ("Create monitoring token"). The page shows the
token once; set the monitor to send it as a header,
`Authorization: Bearer <token>` (in Uptime Kuma, under **Headers**). The token
only works as that header, never in the address, since addresses end up in
proxy and monitor logs. To try it:

```
curl -H "Authorization: Bearer <token>" https://<your domain>/health/ready
```

With the token the address returns every check as JSON:

```json
{"ok": true, "status": "warn", "version": "2.0.0", "checked_at": "2026-10-09T17:40:00Z",
 "checks": {"worker": {"status": "ok", "summary": "Checked in 20 seconds ago", "age_seconds": 20, ...},
            "backup": {"status": "warn", "summary": "No backup for 40 hours", ...}, ...}}
```

`status` is `ok`, `warn` (needs attention; still 200) or `fail` (503). In
Uptime Kuma, the **HTTP(s) - Json Query** type with the query `status` and
expected value `ok` alerts on warnings too. A monitor that can't send headers
still gets the 200 or 503. Replace or remove the token on the same page.

**Healthchecks.io** works the other way round: something on the server pings
it, and it alerts when the pings stop. Add a check with a 5-minute period,
then add this line with `crontab -e` on the server (your check's ping URL):

```
*/5 * * * * curl -fsS -m 10 http://127.0.0.1:8000/health/ready >/dev/null && curl -fsS -m 10 https://hc-ping.com/<your-check-id> >/dev/null
```

It pings only while Office Sentry is ready, so you hear when the server, the
portal or the worker stops.

### Signed webhooks

With a signing secret saved under *Settings → Alerts*, every webhook request
carries three headers, so your own receiver can tell it came from Office
Sentry and isn't a copy of an earlier one sent again:

- `X-OfficeSentry-Timestamp`: when it was sent, in seconds since 1970 (UTC).
- `X-OfficeSentry-Signature`: `v1=` and the hex HMAC-SHA256, with the secret
  as the key, of the timestamp, a full stop (`.`) and the request body exactly
  as received.
- `X-OfficeSentry-Key-Id`: the first 8 hex characters of the SHA-256 of the
  secret. To change the secret, let the receiver accept both secrets, picking
  the one whose key id matches, then save the new one in Office Sentry and
  remove the old one from the receiver.

Use a long random secret: Office Sentry refuses one shorter than 16
characters, since anyone who sees one signed request could find a short secret
by trying them all, then sign requests of their own. `openssl rand -hex 32`
makes a good one.

Refuse a request whose signature doesn't match or whose timestamp is more than
five minutes from your clock. In Python:

```python
import hashlib, hmac, time


def genuine(headers, body: bytes, secret: str) -> bool:
    stamp = headers.get("X-OfficeSentry-Timestamp", "")
    if not stamp.isdigit() or abs(time.time() - int(stamp)) > 300:
        return False
    mac = hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(headers.get("X-OfficeSentry-Signature", ""), "v1=" + mac)
```

Before 2.2.0 the signature was `sha256=` and the HMAC of the body alone, with
no timestamp, so a copied request could be sent again.

## Sign-in protection

**Staff can sign in with their Microsoft account** instead of a password and authenticator code: an admin
sets it up under *Settings → Staff sign-in* in about two minutes, and
[docs/staff-sign-in.md](docs/staff-sign-in.md) walks through it, including turning passwords off for staff
and turning them back on from the server (`python -m officesentry staff-sign-in --passwords on`) if
Microsoft sign-in ever breaks. The rest of this section is about password sign-ins.

Wrong passwords never lock an account, so nobody can keep you out just by
knowing your username. Instead, Office Sentry slows down **where** the
wrong passwords come from (15-minute sliding window):

- 5 wrong passwords for one username from one address: that address waits
  until the oldest of them is 15 minutes old. The same person signing in from
  anywhere else isn't affected.
- 20 wrong passwords from one address, whatever the usernames: that address
  waits too.
- 10 wrong passwords for one username from anywhere (a spread-out attack):
  addresses that haven't signed in to that account in the last 90 days get one
  attempt at a time, 5 seconds apart, doubling for each further 10 failures up
  to 5 minutes. Addresses the person has signed in from before are never slowed.
- 5 wrong two-step codes lock the two-step step of that account for 15
  minutes. Reaching it needs the right password, so if this happens, change
  the password.

**Two-step sign-in** is required for everyone on a new install, client
accounts included. An install that was already in use before this release
keeps requiring it for staff only (everyone but client accounts), so client users aren't
suddenly asked to enrol; *Settings → Users* shows the current policy. To
require it for clients too, set `OFFICESENTRY_REQUIRE_MFA=all` in `.env` and
restart (tell your clients first: they set it up at their next sign-in).
`staff` and `off` are the other values; the variable always wins over the
install's default.

Unknown usernames are limited exactly like real ones. An admin can clear an
account's limits under *Settings → Users → (user) → Unlock now*.
Attempts are kept for two days (successful ones for 90, to recognise addresses).

These limits need the visitor's real address. Behind the bundled Caddy or your
own reverse proxy, set `OFFICESENTRY_TRUSTED_PROXIES=1` (one proxy) in `.env`;
with it at `0` the `X-Forwarded-For` header is ignored, everyone appears to come
from the proxy and shares its limits, and the portal logs a warning. Don't set
it higher than the number of proxies you really have, or visitors could choose
their own address.

## Clients in GCC High, DoD or China

Nothing on the server changes: the portal and worker reach each client's
cloud from the same install. The server needs outbound HTTPS to that cloud's
hosts (login.microsoftonline.us, graph.microsoft.us, outlook.office365.us for
GCC High; login.microsoftonline.us, dod-graph.microsoft.us, webmail.apps.mil
for DoD; login.chinacloudapi.cn, microsoftgraph.chinacloudapi.cn,
partner.outlook.cn for China), so allow them if outbound traffic is filtered.

Each of those clouds needs its own app registration, made in that cloud, and
its own app connection in Office Sentry with the matching cloud: on **App connection**, open
*Create an app for another Microsoft cloud*, choose the cloud and sign in, or run
`docker compose run --rm worker python -m officesentry create-app --cloud gcc_high --tenant <your GCC High tenant>`
(or `dod`, `china`). The [README](README.md#national-clouds-gcc-high-dod-china)
explains choosing the cloud for a client and what Microsoft doesn't offer in
each cloud.

## When things run

| Setting in `.env` | Default | What it does |
|---|---|---|
| `OFFICESENTRY_COLLECT_CRON` | `0 2 * * *` | When every client is collected, as cron (minute hour day month weekday). The default is 02:00 every night. |
| `OFFICESENTRY_TIMEZONE` | `UTC` | The timezone for the collection time and for emailed reports. |
| `OFFICESENTRY_MAX_AGE_HOURS` | `12` | A collection is skipped if it already ran this recently (say, by hand that evening). Before a scheduled email goes out, anything older than this is collected again first, so the report is fresh. |
| `OFFICESENTRY_WORKER_CONCURRENCY` | `4` | How many clients are collected at once. Each client still runs one data source at a time. With many clients, raise it (8 suits about 200) so the night's collection finishes before morning. |

"Run now", a client's first collection and the refresh before an emailed
report go ahead of the nightly queue, so they don't wait for every client to
be collected.

Some cron examples: `0 2 * * *` nightly at 02:00, `0 2 * * 1-5` weeknights
only, `30 1,13 * * *` twice a day at 01:30 and 13:30.

## How long data is kept

Admins set this under *Settings → Data retention* (`/admin/retention`). The
page says what each period covers and roughly how much the next pass would
delete, and every change goes in the activity log. The worker applies it once
a day, deleting in small batches so the portal stays responsive; deleted rows
are overwritten in the database file where that costs no extra I/O
(`secure_delete = FAST`). Backups keep deleted data until they rotate out.

| Data | Default (today's behaviour) | Can be set to |
|---|---|---|
| Snapshots (every user, mailbox, device and policy from a run) | the last run of each of the last **30 days** (`OFFICESENTRY_KEEP_DAILY`) and of the last **12 months** (`OFFICESENTRY_KEEP_MONTHLY`) | any number of days and months, or every month forever |
| Daily history (each collected object, stored once per version, so any day can be rebuilt) | **25 months** (each data source's latest collection is always kept) | 1 to 1,200 months, or forever |
| Sign-in detail (accounts, IP addresses, countries and cities from the sign-in logs) | as long as its snapshot: about 13 months | 30 days or more |
| Activity history: every sign-in | **180 days** | 30 days to 10 years |
| Activity history: daily sign-in summaries | **25 months** | 1 to 120 months, or forever |
| Activity history: directory audit events | **7 years** | 1 year or more, or forever |
| Resolved findings | forever | 30 days or more after they were resolved |
| Run log info lines (warnings and errors always stay) | 90 days | 7 days or more, or forever |
| Activity log | forever | 365 days or more |
| Issued reports (files sent or approved for clients) | forever | 365 days or more |

The settings page overrides the environment variables once saved. Trend
numbers are counts only, with no names or addresses, and are kept forever, so
run pages for cleared snapshots still show their numbers. The latest snapshot,
anything collected in the last day, open findings and accepted risks, each
client's first-scan findings (alert emails compare against them) and each
client's latest file of each issued report (month-end reporting compares
against it) are never deleted by retention. The activity log can't be set
under a year because it's the record of who changed what and signed in from
where, which security reviews and investigations look back over.

The **daily history** keeps every collection, not one a day or a month, at a
fraction of the size of snapshots: an object is stored again only when it
changes. Measured with `scripts/scale_data.py --history` at 200 clients
(about 22,000 accounts): backfilling 40 days of snapshots took 12 minutes and
0.07 GB (the snapshots take 0.80 GB), and each night adds about 2.5 MB, so 25
months comes to about 2 GB. The command `python -m officesentry history
--dry-run` estimates it for your own data before you upgrade, and `history`
shows how far the backfill has got.

The **activity history** is each client's sign-in and directory audit logs,
kept beyond the 30 days Microsoft keeps them (7 days for audit events without
Entra ID P1). It lives in monthly files in `/data/events`, outside the
database, and a month older than its period is deleted as a whole file.
Deleting a client deletes its rows from every file.

The **activity log** (*Settings → Activity log*, admins only) records sign-ins
and every change, with the address it came from; filter it by user, action,
client and date, and download it as CSV. Failed and slowed-down sign-ins are
removed after 90 days; everything else is kept unless *Data retention* sets
a period. Failed sign-ins with
usernames that don't exist are counted per address and hour, without storing
what was typed.

## Backups

Everything is in the `officesentry-data` volume. To save a safe copy of the
database while it's running:

```bash
docker compose exec web python -m officesentry backup
```

That writes to `/data/backups` inside the volume and keeps the last 7
(`--keep 14` keeps more). Each copy is checked, then compressed
(`officesentry-<time>.db.gz`, about a third of the database's size). While
it's being made, the uncompressed copy needs room next to it: the backup
stops with a plain message if the disk is short of about one and a half
times the database's size.

Each run also brings an `events` folder next to the copies up to date: a
compressed copy of each activity history file (`/data/events`), copied again
only when it changed, so a month that's over is copied once. `restore` puts
back any history file the data folder is missing from the `events` folder next
to the backup it restores. If you back up the volume some other way, include
`/data/events`.

**Keep backups off the data volume.** A copy in the same volume is lost with
the volume. Give the portal a second folder on the host (or a mounted network
share) and back up into it. Create the folder for the container's user
(uid 10001):

```bash
sudo mkdir -p /srv/officesentry-backups && sudo chown 10001 /srv/officesentry-backups
```

then add a `docker-compose.override.yml` next to `docker-compose.yml`:

```yaml
services:
  web:
    volumes: ["/srv/officesentry-backups:/backups"]
```

and run `docker compose up -d`. To back up every night at 03:30, add this
line with `crontab -e` (change the folder):

```
30 3 * * * cd /opt/officesentry && docker compose exec -T web python -m officesentry backup --dir /backups --keep 14
```

Each run writes how it went to `/data/backup-status.json`, wherever the copy
goes. *Settings → System status* shows it, and the team gets a system alert
when a backup fails or is overdue. The [off-site copy](#off-site-backups) and
the [restore drill](#restore-drill) record themselves in the same file:

```json
{"local":         {"ok": true,  "last_attempt": "2026-10-09T03:45:02Z", "last_success": "2026-10-09T03:45:02Z",
                   "detail": "officesentry-20261009-034500.db.gz, 412 MB", "error": ""},
 "offsite":       {"ok": false, "last_attempt": "2026-10-09T03:46:10Z", "last_success": "2026-10-08T03:46:01Z",
                   "detail": "", "error": "restic couldn't reach the bucket"},
 "restore_check": {"ok": true,  "last_attempt": "2026-10-05T04:30:00Z", "last_success": "2026-10-05T04:30:00Z",
                   "detail": "the latest off-site copy restores and opens", "error": ""}}
```

A nightly copy (`local`, `offsite`) counts as overdue after 26 hours and the
weekly restore check after 8 days. A job that has never run isn't listed and
isn't alerted on.

### Where the keys are

`secrets.json` holds the keys that decrypt the stored certificates, two-step
secrets and mail passwords. Docker keeps it in a volume of its own,
`officesentry-keys` (`/keys/secrets.json`), so the data volume and every
backup made from it hold no key: someone who gets a backup can't read the
certificates in it. An install from before October 2026 kept it in the data
volume; the portal moves it to `/keys` the first time it starts on this
version and logs **Moved the key file**.

**Keep a copy somewhere safe and separate from the backups**, such as your
password manager. Print it with:

```bash
docker compose exec web cat /keys/secrets.json
```

and save the line it prints. Without it the stored certificates and mail
passwords can't be decrypted after losing the server; with it, anyone holding
a backup can. It only changes when you run `rotate-keys --new`, so save it
again after that. Running without Docker, it stays in the data folder unless
`OFFICESENTRY_SECRETS_FILE` names another place.

### Off-site backups

The nightly copies are on the same server as the data. To keep encrypted
copies somewhere else, Office Sentry uses [restic](https://restic.net) with
any S3-compatible storage: Backblaze B2, Wasabi, Cloudflare R2, Hetzner Object
Storage, AWS S3 or MinIO. Pick a provider other than the one that runs your
server, so one account problem can't take both. restic encrypts everything
before it leaves the server with a password you choose; the storage provider
only ever sees scrambled data.

Every night at 03:45 (server time) `deploy/jobs.sh` makes the database copy
(or takes the one made in the last hour), then sends the backups folder to the
storage. It keeps one copy a day for two weeks, one a week for two months and
one a month for a year. Only the backups folder goes: never the live
database, and never `secrets.json`, which lives in its own volume.

Costs are small: each night sends only what's new, the night's compressed
copy (see [How much disk](#how-much-disk)). With 200 clients the copies kept
come to about 40 GB, well under a dollar a month at B2's published price.

To set it up:

1. Make the storage. On Backblaze B2 (backblaze.com, free to sign up): *B2
   Cloud Storage → Buckets → Create a Bucket*, a name such as
   `yourmsp-officesentry-backups`, **Private**, encryption off (restic
   encrypts already). Note the **Endpoint** shown on the bucket, such as
   `s3.eu-central-003.backblazeb2.com`.
2. Make a key for it: *Application Keys → Add a New Application Key*, name
   `officesentry`, **allow access to** that bucket only, **Read and Write**.
   Copy the **keyID** and **applicationKey**; B2 shows the second only once.
3. Make the backup password: a long random one from your password manager (no
   `'` in it). **Save it in the password manager, next to the copy of
   `secrets.json`.** Without it the off-site copies can't be read by anyone,
   you included.
4. Put the settings in a file named `.env.backup` next to
   `docker-compose.yml`, readable only by you (`chmod 600 .env.backup`), each
   value in single quotes. The repository is `s3:https://` + the endpoint +
   `/` + the bucket name + `/officesentry`:

   ```
   RESTIC_REPOSITORY='s3:https://s3.eu-central-003.backblazeb2.com/yourmsp-officesentry-backups/officesentry'
   RESTIC_PASSWORD='the password from step 3'
   AWS_ACCESS_KEY_ID='the keyID'
   AWS_SECRET_ACCESS_KEY='the applicationKey'
   ```

5. Copy `deploy/jobs.sh` from the release's source next to it as
   `deploy/jobs.sh`, then run `OFFICESENTRY_DIR=$PWD bash deploy/jobs.sh schedule`
   in that folder (as a user who can run `docker`) to add the nightly and
   weekly lines to your crontab.
6. Run `OFFICESENTRY_DIR=$PWD bash deploy/jobs.sh now`: the first run sets up
   the storage and ends with a line like `snapshot 1a2b3c4d saved`. Then run
   it with `drill` instead of `now` ([below](#restore-drill)). When that
   passes, the backup is proven to restore.

`docker compose exec web python -m officesentry backup-status` says when the
database copy, the off-site copy and the restore check last worked. *Settings →
System status* in the portal shows the same, and the team gets a system alert
when one fails or is overdue.

To hear about a failed backup even when the whole server is down, add a free
check at [Healthchecks.io](https://healthchecks.io) (period 1 day, grace 2
hours), and add its ping address to `.env.backup` as
`BACKUP_PING_URL='https://hc-ping.com/...'`. Every nightly run pings it, or its
`/fail` address when something failed.

The password and storage keys are only in `.env.backup`, which only restic's
own container reads; the portal never sees them.

### Restore drill

A backup is only proven once it has been restored. Every Sunday at 04:30
(server time) `deploy/jobs.sh drill` checks the off-site storage, restores the
newest database copy from it into a scratch volume, and has Office Sentry open
it read-only, run SQLite's integrity check and count the clients. It never
touches the live database, and deletes the scratch volume afterwards. The
result is the **Restore check** line in `status` and on *System status*.

To run it now: `OFFICESENTRY_DIR=$PWD bash deploy/jobs.sh drill` in the
folder with `docker-compose.yml`. A run that passes ends with a line like:

```
OK: officesentry-20261009-034500.db.gz opens and passes SQLite's integrity check; database version 26, 12 clients, newest collection 2026-10-09 02:14
```

Read the client count and the newest collection date: they should match what
the portal shows.

Once a year, rehearse the whole recovery below on a scratch server, so the
steps are known before they're needed.

Before every upgrade that changes the database, the portal also saves a copy
as `/data/backups/pre-upgrade-v<old>-to-v<new>-<time>-from-<release>.db`, where
`<release>` is the Office Sentry version that copy goes with (the last 3 are
kept). It's made before the upgrade starts, while the portal is down (see
[Updating](#updating)). It isn't compressed, so the older version you'd
go back to can always restore it. If it can't (a full disk, say), it doesn't
upgrade and says why.

**Deleted clients stay in backups.** On the server itself, a deleted client's
data is erased from the database and activity history files by the worker
within minutes; *System status* shows **Deleted clients** as needing attention
(and the team is alerted) if that fails. Backups keep everything that was in the
database when they were made. After you delete a client, its data stays
in older backups until they rotate out (nightly ones after `--keep` runs;
`pre-upgrade-*` and `pre-restore-*` copies only when 3 newer ones replace
them) and in any copies you took off the server; delete those too if the
client asks for their data to be erased.

## Restoring a backup

1. Stop the portal and the worker (the restore refuses while the worker is
   running):

   ```bash
   docker compose stop web worker
   ```

2. Restore it, naming a file in the volume (`docker compose run --rm --no-deps
   web ls /data/backups` lists them):

   ```bash
   docker compose run --rm --no-deps web python -m officesentry restore /data/backups/officesentry-20261005-033000.db.gz
   ```

   A backup kept on the host goes in first with
   `docker compose cp ./officesentry-20261005-033000.db.gz web:/data/restore.db.gz`
   (then restore `/data/restore.db.gz`), or from the `/backups` folder if you
   mounted one. Older, uncompressed `.db` backups restore the same way.
   Versions of Office Sentry from before October 2026 can't read `.db.gz`
   files: unpack one first with `gunzip -k <file>` and restore the `.db`.

   It checks that the file is a sound Office Sentry database, saves the
   database it replaces as `/data/backups/pre-restore-<time>.db.gz`, and copies
   the backup in with SQLite's backup API (no stale `-wal` or `-shm` files are
   left behind). When the database it replaces is too damaged to copy (the
   usual reason for a restore), it's moved aside whole instead, as
   `/data/backups/pre-restore-<time>-damaged.db` with its `-wal` file, and the
   restore goes ahead. If anything goes wrong it says so and names the copy
   of the old database.
3. Put back the `secrets.json` from the same time as the backup if the keys
   changed since: with `secrets.json` in the current folder,

   ```bash
   docker compose run --rm --no-deps -T --entrypoint sh web -c 'umask 077 && cat > /keys/secrets.json' < secrets.json
   ```

4. Start again: `docker compose up -d`. A backup from an older version is
   upgraded when the portal starts.

**Moving to a new server** is the same: install as in *First start* (skip
creating the admin), stop both containers, put back `secrets.json` as in step
3, copy in the backup, restore, start.

### Restoring from the off-site copy

When the server is lost. You need the backup password and the saved
`secrets.json` from your password manager. Tested on a fresh server with
Office Sentry's own scripts.

1. Make a new server and install as in *First start* steps 1 to 4 (without
   creating an admin), plus `.env.backup` and `deploy/jobs.sh` as in
   *Off-site backups*.
2. Open a terminal on it in that folder (such as `/opt/officesentry`).
3. Put back `secrets.json`: `nano secrets.json`, paste the saved line, save
   (Ctrl+O, Enter, Ctrl+X), then:

   ```bash
   docker compose stop web worker
   docker compose run --rm --no-deps -T --entrypoint sh web -c 'umask 077 && cat > /keys/secrets.json' < secrets.json
   rm secrets.json
   ```

4. Restore the newest off-site copy and start again:

   ```bash
   OFFICESENTRY_DIR=$PWD bash deploy/jobs.sh restore
   ```

   It fetches the newest database copy from the storage, stops the portal and
   worker, restores it (keeping the empty database it replaces as
   `pre-restore-*`) and starts them. Sign in with your usual account. If the
   restore fails, it says why and leaves the portal and worker stopped, so
   nothing starts on a missing or half-restored database; fix the problem and
   run it again.
5. Point your domain at the new server, if you haven't yet.

The off-site copies hold every database copy of the last nights too. To
restore an older one, ask restic for a list in step 4 instead:
`docker compose --profile offsite run --rm offsite snapshots`.

## Rotating keys

If you think the encryption key may have leaked (a lost backup together with
`secrets.json`, say), replace it, then save the new `secrets.json` as in
[Where the keys are](#where-the-keys-are):

```bash
docker compose exec web python -m officesentry rotate-keys --new
docker compose restart
```

That makes a new session key and a new encryption key, re-encrypts every
stored secret with it (certificate private keys, two-step secrets, the SMTP
password, the Graph mail secret, and the alert webhook URL and signing
secret), then drops the old key. Everyone signs in again. If the database
holds an encrypted value the command doesn't recognise, it stops and changes
nothing. With keys in environment variables instead, put the new key first in
`OFFICESENTRY_ENCRYPTION_KEYS`, run `rotate-keys`, then remove the old key.

Rotating keys doesn't stop a leaked *certificate* signing in: anyone holding
its private key can until it's deleted from the app registration in Entra.
Replace it too, as described under *If a certificate or key leaks* in
`SECURITY.md` (*Settings → App connection → Replace certificate*).

Ran `rotate-keys --new` before this release? It didn't re-encrypt the alert
webhook, so enter the webhook URL and secret again under *Settings → Alerts*.
Old backups are still encrypted with the old key.

## Updating

Read the release's notes first ([releases](https://github.com/JackD99/OfficeSentry/releases),
or `CHANGELOG.md`), above all **Action required**, **New Microsoft
permissions** and **Database upgrade**. Then set the new version in `.env`
(`OFFICESENTRY_VERSION=2.1.0`) and run:

```bash
docker compose pull
docker compose up -d
```

The portal upgrades the database on start, before the worker starts, after
saving a `pre-upgrade-*` copy (see Backups). Data in the volume is kept. The
portal is down from `up -d` until the copy and the upgrade finish: seconds
for a small database, minutes for a large one, so update at a quiet time. The portal's health check
allows it 15 minutes to come up before Docker calls it unhealthy; if `up -d` gives up on it before the
worker starts, run `docker compose up -d` again once the portal answers. The version now
running shows at the foot of the menu and at `/health`.

If the release notes change `docker-compose.yml` or `Caddyfile`, download
the new ones as well; they are attached to every release.

### When a release adds permissions

Some releases ask Microsoft for new read-only permissions (the notes list
them under **New Microsoft permissions**), released together so each client
approves once. After updating:

1. **Update your app, once.** *Settings → App connection* shows "Update this
   app" on each app connection that doesn't ask for them yet. Press *Update the
   app* and sign in to your own (the MSP's) tenant as a Global Administrator,
   Application Administrator or Cloud Application Administrator: Office Sentry
   adds the new permissions to its app registration and keeps everything else.
   From the command line, `docker compose run --rm worker python -m officesentry
   update-app` does the same. Or add them by hand in the Entra admin centre and
   press *I've added them*.
2. **Ask each client to approve.** *Settings → Client settings → Onboarding*
   lists the clients with new permissions to approve. Each one's *What's new*
   page has the email to send, the same approval link as before and a PDF
   saying what each new permission reads and adds. Office Sentry re-reads a
   client's permissions when they come back from Microsoft's page.

Nothing breaks in the meantime: reports that need a new permission say they're
waiting for the client's approval, and clients stay *Healthy*.

Running your own build of the `v2` folder instead (a branch, or changes of
your own)? Add `COMPOSE_FILE=docker-compose.yml:docker-compose.build.yml` to
`.env`, and update with `git pull` then `docker compose up -d --build`.

### Rolling back an update

Database upgrades only go forward, so going back to an older version means
going back to the database from before the upgrade. Anything collected since
the upgrade is lost. An older version started on the upgraded database
refuses to start, and its log (`docker compose logs web`) names the copy to
restore.

1. `docker compose stop web worker`
2. Go back to the version you had: set it in `.env` (`OFFICESENTRY_VERSION=2.0.0`)
   and run `docker compose pull`. Building your own? `git checkout <commit>`
   and `docker compose build`.
3. Restore the copy taken before the upgrade. Its name ends with the version
   that made it (`-from-2.0.0`), when it was made by 2.0.0 or later:

   ```bash
   docker compose run --rm --no-deps web ls /data/backups
   docker compose run --rm --no-deps web python -m officesentry restore /data/backups/pre-upgrade-v8-to-v9-20261005-020000-from-2.0.0.db
   ```

   Versions from before the `restore` command: stop both containers, copy the
   backup over `/data/officesentry.db`, and delete `/data/officesentry.db-wal`
   and `/data/officesentry.db-shm` before starting.
4. Going back to a version from before the keys moved to their own volume
   (October 2026)? It looks for `secrets.json` in the data volume, so copy it
   back first:
   `docker compose run --rm --no-deps -T --entrypoint sh web -c 'cp -p /keys/secrets.json /data/secrets.json'`.
5. `docker compose up -d`

## Stopping

`docker compose down` stops everything and keeps your data. A collection in
progress is given up to two minutes to finish. Never use `down -v` unless you
mean to delete all data.

## Running on Windows without Docker

Docker is the supported way to run Office Sentry. A Windows server without
Docker works too, with one difference: PDF buttons open a print-ready page
(choose **Save as PDF** in the print window) and scheduled emails go out
without PDF attachments, because WeasyPrint's Pango libraries aren't
available on Windows.

1. Install Python 3.12 from python.org, and copy the `v2` folder to the
   server (say `C:\OfficeSentry\v2`).
2. In PowerShell, in that folder:

   ```powershell
   python -m venv .venv
   .venv\Scripts\pip install .
   ```

3. Set the settings from `.env.example` as system environment variables
   (System Properties → Environment Variables), at least
   `OFFICESENTRY_BASE_URL`, `OFFICESENTRY_TIMEZONE` and
   `OFFICESENTRY_DATA_DIR` (say `C:\OfficeSentry\data`), and
   `OFFICESENTRY_SECRETS_FILE` (say `C:\OfficeSentry\keys\secrets.json`) so
   the keys stay out of the data folder and its backups. Only the service
   account should be able to read either folder.
4. Run two services that start at boot, for example with
   [NSSM](https://nssm.cc/) or Task Scheduler ("At startup", "Run whether
   user is logged on or not"):
   - the portal: `C:\OfficeSentry\v2\.venv\Scripts\python.exe -m officesentry serve`
     (waitress, on 127.0.0.1:8000)
   - the worker: `C:\OfficeSentry\v2\.venv\Scripts\python.exe -m officesentry worker`
5. Put HTTPS in front: IIS with URL Rewrite and Application Request Routing,
   or Caddy for Windows, forwarding to `http://127.0.0.1:8000`, and set
   `OFFICESENTRY_TRUSTED_PROXIES=1`.
6. Create the first admin from the link that
   `.venv\Scripts\python.exe -m officesentry setup-link` prints.
7. Back up with a scheduled task running
   `python.exe -m officesentry backup --dir D:\OfficeSentryBackups` (a
   different disk from the data), and keep a copy of `secrets.json`
   elsewhere. Restoring works as above: stop both services, run
   `python -m officesentry restore <file>`, start them. To update, stop both
   services, replace the `v2` folder, run `.venv\Scripts\pip install .` again
   and start them.
