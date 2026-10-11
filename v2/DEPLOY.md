# Running Office Sentry on your own server

Office Sentry runs as two containers from one image: the **portal** (the
website) and the **worker** (collects from tenants, sends scheduled emails,
tidies old data). Everything it stores lives in one Docker volume, and the
keys that decrypt its stored certificates and passwords in a second one
([Where the keys are](#where-the-keys-are)).

## What you need

- A Linux server or VM with [Docker](https://docs.docker.com/engine/install/)
  installed, with at least 2 CPUs and 4 GB of RAM. Disk use grows with the
  number of tenants and users (see [How much disk](#how-much-disk)).
- A domain name for the portal, such as `sentry.yourmsp.com`, with its DNS
  pointing at the server, if you want the built-in HTTPS.

### How much disk

Measured with `scripts/scale_data.py`: 200 client tenants of about 110
accounts each, after two years of nightly collection with the default *Data
retention* settings.

| Tenants | Database | One nightly backup | Disk to plan for |
|---|---|---|---|
| 50 | about 1 GB | about 0.3 GB | 20 GB |
| 200 | 4.0 GB | 1.2 GB (under 2 minutes) | 40 GB |
| 500 | about 10 GB | about 3 GB | 80 GB |

The 50 and 500 rows are scaled from the 200-tenant measurement. A tenant with
twice the accounts takes about twice the room. "Disk to plan for" covers the
operating system and Docker, the database, 7 nightly backups and the room a
backup needs while it's being made (about the database's size again). Of the
4.0 GB, collected data is 0.8 GB (stored compressed); the rest is run history
and trend figures, which grow by about 0.7 MB per tenant a month.

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

## First start

On Hetzner with only a phone? See [Hetzner, from a phone](#hetzner-from-a-phone).

1. Make a folder on the server (say `/opt/officesentry`) and open a terminal in it.
   From the newest [release](https://github.com/JackD99/OfficeSentry/releases),
   download `docker-compose.yml`, `Caddyfile` and `env.example` into it.
   They run that release: the image `ghcr.io/jackd99/officesentry`, built and
   tested by GitHub, for amd64 (Intel and AMD) servers; arm64 builds follow
   once the repository is public.
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

   Until the repository is public its image is private: pulling it needs
   `docker login ghcr.io` with a GitHub token that can read packages. The
   Hetzner deploy below doesn't pull, it builds.

   Already have a reverse proxy (nginx, Traefik)? Leave out `--profile https`
   and point the proxy at `http://127.0.0.1:8000`.
5. Create the first admin account. Run `docker compose logs web`, find the line
   starting **No admin account yet** and open the link on it (`docker compose exec
   web python -m officesentry setup-link` prints it again). Choose a username and
   password, scan the QR code with an authenticator app and save the recovery codes.
6. Open **Settings → App connection** and press **Sign in to Microsoft**. Enter the code it shows on
   Microsoft's page and sign in as an admin of your own tenant who can create app registrations. Office
   Sentry creates its read-only app and then offers to add your first tenant. No command needed.

## Is it working?

`docker compose ps` should show `web` and `worker` as **healthy**. If the
worker stops, admins see a yellow notice at the top of every page.

To see what the worker is doing: `docker compose logs -f worker`

**Settings → System status** shows the whole picture, problems first: the
worker, the last collection, the last backup, disk space, the database and the
app certificates, each with what to do about it.

### Hear about problems without signing in

Office Sentry tells your team itself when the worker stops, a backup fails,
the disk is over 85% full or most tenants fail to collect: an email to the
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
to give to an outside service; it never names a tenant.

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

Before 2.1.0 the signature was `sha256=` and the HMAC of the body alone, with
no timestamp, so a copied request could be sent again.

## Sign-in protection

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
keeps requiring it for admins and analysts only, so client users aren't
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

Nothing on the server changes: the portal and worker reach each tenant's
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
explains choosing the cloud for a tenant and what Microsoft doesn't offer in
each cloud.

## When things run

| Setting in `.env` | Default | What it does |
|---|---|---|
| `OFFICESENTRY_COLLECT_CRON` | `0 2 * * *` | When every tenant is collected, as cron (minute hour day month weekday). The default is 02:00 every night. |
| `OFFICESENTRY_TIMEZONE` | `UTC` | The timezone for the collection time and for emailed reports. |
| `OFFICESENTRY_MAX_AGE_HOURS` | `12` | A collection is skipped if it already ran this recently (say, by hand that evening). Before a scheduled email goes out, anything older than this is collected again first, so the report is fresh. |
| `OFFICESENTRY_WORKER_CONCURRENCY` | `4` | How many tenants are collected at once. Each tenant still runs one data source at a time. With many tenants, raise it (8 suits about 200) so the night's collection finishes before morning. |

"Run now", a tenant's first collection and the refresh before an emailed
report go ahead of the nightly queue, so they don't wait for every tenant to
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
tenant's first-scan findings (alert emails compare against them) and each
tenant's latest file of each issued report (month-end reporting compares
against it) are never deleted by retention. The activity log can't be set
under a year because it's the record of who changed what and signed in from
where, which security reviews and investigations look back over.

The **daily history** keeps every collection, not one a day or a month, at a
fraction of the size of snapshots: an object is stored again only when it
changes. Measured with `scripts/scale_data.py --history` at 200 tenants
(about 22,000 accounts): backfilling 40 days of snapshots took 12 minutes and
0.07 GB (the snapshots take 0.80 GB), and each night adds about 2.5 MB, so 25
months comes to about 2 GB. The command `python -m officesentry history
--dry-run` estimates it for your own data before you upgrade, and `history`
shows how far the backfill has got.

The **activity history** is each tenant's sign-in and directory audit logs,
kept beyond the 30 days Microsoft keeps them (7 days for audit events without
Entra ID P1). It lives in monthly files in `/data/events`, outside the
database, and a month older than its period is deleted as a whole file.
Deleting a tenant deletes its rows from every file.

The **activity log** (*Settings → Activity log*, admins only) records sign-ins
and every change, with the address it came from; filter it by user, action,
tenant and date, and download it as CSV. Failed and slowed-down sign-ins are
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
30 3 * * * cd /opt/officesentry/v2 && docker compose exec -T web python -m officesentry backup --dir /backups --keep 14
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
copy (see [How much disk](#how-much-disk)). With 200 tenants the copies kept
come to about 40 GB, well under a dollar a month at B2's published price.

**On the Hetzner deploy**, from your phone:

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
4. In GitHub: the repository's *Settings → Secrets and variables → Actions*.
   Add a variable and three secrets:

   | | Name | Value |
   |---|---|---|
   | Variable | `BACKUP_REPOSITORY` | `s3:https://` + the endpoint + `/` + the bucket name + `/officesentry`, e.g. `s3:https://s3.eu-central-003.backblazeb2.com/yourmsp-officesentry-backups/officesentry` |
   | Secret | `BACKUP_PASSWORD` | the password from step 3 |
   | Secret | `BACKUP_KEY_ID` | the keyID |
   | Secret | `BACKUP_SECRET_KEY` | the applicationKey |

5. *Actions → Deploy → Run workflow*, action **deploy**, to put the settings
   on the server. Then run it again with action **backup**: the first run
   sets up the storage, and the log ends with a line like
   `snapshot 1a2b3c4d saved`.
6. Run it once more with action **restore-drill** ([below](#restore-drill)).
   When it's green, the backup is proven to restore.

Then run **status** whenever you like: its last lines say when the database
copy, the off-site copy and the restore check last worked. *Settings → System
status* in the portal shows the same, and the team gets a system alert when
one fails or is overdue.

To hear about a failed backup even when the whole server is down, add a free
check at [Healthchecks.io](https://healthchecks.io) (period 1 day, grace 2
hours), and add its ping address as the secret `BACKUP_PING_URL`, then deploy.
Every nightly run pings it, or its `/fail` address when something failed.

**Elsewhere**, put the same four settings in a file named `.env.backup` next to
`docker-compose.yml`, readable only by you (`chmod 600 .env.backup`), each
value in single quotes:

```
RESTIC_REPOSITORY='s3:https://s3.eu-central-003.backblazeb2.com/yourmsp-officesentry-backups/officesentry'
RESTIC_PASSWORD='the password'
AWS_ACCESS_KEY_ID='the key ID'
AWS_SECRET_ACCESS_KEY='the secret key'
```

copy `deploy/jobs.sh` from the release's source next to it as
`deploy/jobs.sh`, then run `OFFICESENTRY_DIR=$PWD bash deploy/jobs.sh schedule`
in that folder (as a user who can run `docker`) to add the nightly and weekly
lines to your crontab. `bash deploy/jobs.sh now` backs up straight away.

The password and storage keys are only in `.env.backup`, which only restic's
own container reads; the portal never sees them.

### Restore drill

A backup is only proven once it has been restored. Every Sunday at 04:30
(server time) `deploy/jobs.sh drill` checks the off-site storage, restores the
newest database copy from it into a scratch volume, and has Office Sentry open
it read-only, run SQLite's integrity check and count the tenants. It never
touches the live database, and deletes the scratch volume afterwards. The
result is the **Restore check** line in `status` and on *System status*.

To run it now: *Actions → Deploy → Run workflow*, action **restore-drill**
(or `bash deploy/jobs.sh drill` in a shell on the server). A green run ends
with a line like:

```
OK: officesentry-20261009-034500.db.gz opens and passes SQLite's integrity check; database version 26, 12 tenants, newest collection 2026-10-09 02:14
```

Read the tenant count and the newest collection date: they should match what
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

**Deleted tenants stay in backups.** On the server itself, a deleted client's
data is erased from the database and activity history files by the worker
within minutes; *System status* shows **Deleted clients** as needing attention
(and the team is alerted) if that fails. Backups keep everything that was in the
database when they were made. After you delete a client tenant, its data stays
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

1. Make a new server and deploy as in [Hetzner, from a phone](#hetzner-from-a-phone),
   steps 2 to 6 but without creating an admin (with the same repository
   variables and secrets, the off-site settings included). Elsewhere: *First
   start* steps 1 to 4, plus `.env.backup` and `deploy/jobs.sh` as in
   *Off-site backups*.
2. Open a shell on it ([If you need a shell](#if-you-need-a-shell)) and
   `cd /opt/officesentry/v2`.
3. Put back `secrets.json`: `nano secrets.json`, paste the saved line, save
   (Ctrl+O, Enter, Ctrl+X), then:

   ```bash
   docker compose stop web worker
   docker compose run --rm --no-deps -T --entrypoint sh web -c 'umask 077 && cat > /keys/secrets.json' < secrets.json
   rm secrets.json
   ```

4. Restore the newest off-site copy and start again:

   ```bash
   bash deploy/jobs.sh restore
   ```

   It fetches the newest database copy from the storage, stops the portal and
   worker, restores it (keeping the empty database it replaces as
   `pre-restore-*`) and starts them. Sign in with your usual account. If the
   restore fails, it says why and leaves the portal and worker stopped, so
   nothing starts on a missing or half-restored database; fix the problem and
   run it again.
5. Point your domain at the new server (step 4 of the Hetzner steps), if
   you haven't yet.

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
allows it 15 minutes to come up before Docker calls it unhealthy, and the Deploy workflow waits as long
and then starts the worker if `up -d` gave up on it. The version now
running shows at the foot of the menu and at `/health`.

If the release notes change `docker-compose.yml` or `Caddyfile`, download
the new ones as well; they are attached to every release.

Running your own build of the `v2` folder instead (a branch, or changes of
your own)? Add `COMPOSE_FILE=docker-compose.yml:docker-compose.build.yml` to
`.env`, and update with `git pull` then `docker compose up -d --build`. The
Hetzner deploy below works this way.

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

## Hetzner, from a phone

Everything here works from a phone browser: the Hetzner Cloud console, GitHub
(use github.com in the browser for Settings; the GitHub app can run
workflows) and an SSH app such as Termius to make a key. You never type
commands on the server. The **Deploy** workflow (`.github/workflows/deploy.yml`)
copies the app over and starts it, and the server's Hetzner firewall decides
who can open the portal. It builds the chosen branch, tag or commit on the
server (`docker-compose.build.yml`) rather than pulling a release image, so
any commit can be deployed.

### 1. Make a deploy key

In Termius: *Keychain → + → Generate key*, type **ED25519**, name it
`officesentry-deploy`, no passphrase. Keep it open: you need the public key in
step 3 and the private key in step 5.

### 2. Create the firewall

In the [Hetzner Cloud console](https://console.hetzner.cloud), open (or create)
a project, then *Firewalls → Create firewall*, named `officesentry`, with these
inbound rules and **no rule for port 22**:

| Protocol | Port | Source | Why |
|---|---|---|---|
| TCP | 443 | your office and home addresses, e.g. `203.0.113.10/32` | the portal |
| TCP | 80 | Any IPv4, Any IPv6 | Let's Encrypt checks here before issuing the HTTPS certificate; Caddy only redirects it to 443 |
| ICMP | | Any | optional, lets you ping the server |

Port 443 is the allow-list. Anyone else can't even connect. Your address is
whatever a "what is my IP" search shows on that network. Mobile data changes
address often, so for a phone away from the office add its current address
while you need it and remove it after. Client users who sign in to the portal
need their addresses here too.

SSH stays closed: the Deploy workflow opens port 22 to its own address for the
length of each run (a rule named `github-actions-deploy (temporary)`) and
removes it at the end, whether the run passed, failed or was cancelled; a
second, separate job removes it again in case GitHub lost the first one's
runner part way through.

### 3. Create the server

*Servers → Add server*:

- **Image** Ubuntu 24.04. **Type** shared vCPU, x86, 2 vCPUs and 4 GB (the
  minimum in [What you need](#what-you-need)). Check its disk against
  [How much disk](#how-much-disk); a bigger type can be chosen later.
- **Networking** public IPv4 (and IPv6 if you like).
- **SSH keys** none needed.
- **Firewalls** tick `officesentry`.
- **Backups** worth ticking: Hetzner keeps a copy of the whole server, data
  and keys included, for 7 days, and the server saves a consistent database
  copy every night at 03:45 for those to hold. They're with the same
  provider, so add [Off-site backups](#off-site-backups) too.
- **Cloud config** paste [`deploy/cloud-init.yaml`](deploy/cloud-init.yaml),
  with its `ssh-ed25519 AAAA...` line replaced by your public key (Termius:
  the key → *Export to clipboard* or copy the public key).

Create it and note its IPv4 address. It takes a few minutes to install Docker.

### 4. Point your domain at it

At your DNS provider add an **A** record for, say, `sentry.yourmsp.com` with
the server's IPv4 address. Only add an AAAA (IPv6) record if your firewall
rules list your IPv6 ranges too, or browsers that prefer IPv6 won't get in.

**Domain on Cloudflare?** In the Cloudflare app or dash.cloudflare.com: the
domain → *DNS → Records → Add record*: type **A**, name `app` (for
`app.yourmsp.com`, or `@` for the bare domain), IPv4 address the server's,
and **Proxy status off (grey cloud, "DNS only")**. Keep the proxy off: with the orange
cloud, every visitor reaches the server from a Cloudflare address, so the
Hetzner firewall's allow-list would block everyone (or have to let all of
Cloudflare in), and Caddy can't get its certificate the usual way. Caddy
makes the HTTPS certificate itself, so nothing needs changing under
Cloudflare's SSL/TLS settings.

### 5. Tell GitHub where to deploy

In Hetzner: *Security → API tokens → Generate API token*, **Read & Write**
(the workflow edits the firewall with it).

In GitHub: the repository's *Settings → Secrets and variables → Actions*.

| Secrets | |
|---|---|
| `HCLOUD_TOKEN` | the Hetzner API token |
| `DEPLOY_SSH_KEY` | the deploy key's **private** key, all of it, including the BEGIN and END lines |
| `DEPLOY_HOST_KEY` | the server's own SSH key, one line starting `ssh-ed25519 AAAA` (below). Required: the workflow only talks to the server that has this key |
| `PORTAL_ALLOWED_IPS` | only when the server also runs a public site (below): the addresses that may open the portal, separated by commas |
| `SETUP_CODE` | only while there is no admin yet: a long random password (20 characters or more) to use as the first-admin code in step 6. Delete it afterwards |

| Variables | |
|---|---|
| `DEPLOY_HOST` | the server's IPv4 address |
| `OFFICESENTRY_DOMAIN` | the domain alone, e.g. `sentry.yourmsp.com` |
| `OFFICESENTRY_TIMEZONE` | e.g. `Europe/London` (default UTC) |
| `HCLOUD_FIREWALL` | only if the firewall isn't called `officesentry` |
| `OFFICESENTRY_WEBSITE_DOMAIN`, `OFFICESENTRY_WEBSITE_CONTACT`, `OFFICESENTRY_DEMO_DOMAIN`, `DEMO_ENABLED` | only for the project's own website and public demo (below) |
| `OFFICESENTRY_SETTINGS` | optional: more `.env` lines, one per line, e.g. `OFFICESENTRY_COLLECT_CRON=0 1 * * *`. Variables show in run logs, so never put a password or key here |
| `AUTO_DEPLOY` | `true` to deploy every merge to main once its tests pass (only main's newest commit; a re-run of an older test run doesn't redeploy it). Anything else, or no variable, means you deploy by hand ([below](#automatic-deploys-off-and-on)) |
| `PRODUCTION_FROM_TAGS` | `true` makes the portal take release tags only, and merges never deploy it ([below](#production-from-releases)). Unset or anything else: a deploy builds the chosen code, as today |
| `BACKUP_REPOSITORY` | optional: where the off-site copies go ([Off-site backups](#off-site-backups)), with the secrets `BACKUP_PASSWORD`, `BACKUP_KEY_ID`, `BACKUP_SECRET_KEY`, and optionally `BACKUP_PING_URL` and `UPTIME_PING_URL` |

The workflow writes the server's `.env` from these on every deploy (with
`OFFICESENTRY_TRUSTED_PROXIES=1` for Caddy), so change settings here, not on
the server.

**Getting `DEPLOY_HOST_KEY`.** Leave it out at first. Run the Deploy workflow
(step 6): it stops with a red message that shows the key the server answered
with, a line starting `ssh-ed25519 AAAA`. Add that line as the
`DEPLOY_HOST_KEY` secret and run the workflow again. Because you created the
server a few minutes earlier, the key it shows is its own. From then on every
run checks the server's key first, and if a different machine ever answers at
that address the run stops before sending it anything. If you rebuild or
replace the server, it gets a new key: the run stops with a message showing
the new one, and you update the secret the same way. If you haven't touched
the server and see that message, don't change the secret; check the
`DEPLOY_HOST` variable and the server in the Hetzner console first.

### 6. Deploy and create the admin

1. *Actions → Deploy → Run workflow*, action **deploy**. When it's green, it
   notes that there is no admin account yet.
2. Add a `SETUP_CODE` secret (step 5's page) holding a long random password,
   say one your password manager makes. Keep a copy for the next step.
3. Run the workflow again with action **setup-link**. Its summary says which
   page to open: `https://<your domain>/setup`.
4. Open that page, enter your `SETUP_CODE` as the setup code and create the
   admin account (with two-step sign-in), as in *First start*. The code only
   works until the first admin exists.
5. Delete the `SETUP_CODE` secret. Then carry on from step 6 of *First start*.

The run logs never show the setup link or code: they will be public once the
repository is, and whoever has the link can create the first admin.

### Everyday jobs

All from *Actions → Deploy → Run workflow*:

| Action | Does |
|---|---|
| `deploy` | copies the chosen branch, tag or commit and rebuilds; the portal is only down while the containers swap |
| `website-and-demo` | only with the website or public demo on (below): updates them from `main` (they also update by themselves after every merge), without touching the portal |
| `status` | container health, free disk, whether the worker checked in, how many errors the portal and worker logged in the last 24 hours, and when each backup job last worked |
| `backup` | a database copy now, keeping 14, then the off-site copy when it's set up |
| `restore-drill` | restores the newest off-site copy into a scratch volume and checks it opens ([Restore drill](#restore-drill)) |
| `restart` | restarts the portal, worker and Caddy |
| `setup-link` | makes your `SETUP_CODE` secret the first-admin code (step 6); does nothing once an admin exists |

To go back to an older version, deploy its commit or tag in the `ref` box; if
the database was upgraded since, restore the `pre-upgrade-*` copy as in
*Rolling back an update* (that needs a shell, below).

The runs never print the portal's own log, because it names clients, users
and addresses and the run logs will be public once the repository is. When
`status` counts errors, or a deploy says the portal didn't become healthy,
read the log in a shell (below) with `docker compose logs --tail 200 web`
(or `worker`).

### Automatic deploys: off and on

With the `AUTO_DEPLOY` variable set to `true`, every pull request merged
into main goes to your server as soon as its tests pass. To stop that and
choose when to deploy:

1. On github.com, open the repository, then **Settings** (the tab with the
   gear; on a phone, use the browser, not the GitHub app).
2. In the left menu, **Secrets and variables**, then **Actions**.
3. Open the **Variables** tab. Next to `AUTO_DEPLOY`, press the pencil.
4. Change the value to `false` and press **Update variable**.

The portal doesn't update by itself from then on (the project's website and
public demo, [below](#the-website-and-public-demo), still follow `main`). To turn it back on, do the same and
set the value to `true`. Merges keep running their tests either way.

### Deploying by hand

1. Open the repository's **Actions** tab (in the GitHub app: the repository,
   then *Actions*).
2. Pick **Deploy** in the list of workflows.
3. Press **Run workflow**. Leave *Use workflow from* on `main`, leave the
   action on **deploy**, and leave the `ref` box on `main` to deploy the
   newest code (or type a branch, tag or commit).
4. Press the green **Run workflow**. The run takes a few minutes; a green
   tick means the portal is up on the new version.

To see what a deploy will bring, look at the **Pull requests** tab, filter
*Closed*, and read what merged since your last deploy (the Deploy run list
shows when that was).

Every deploy also (re)installs the server's scheduled jobs in the `deploy`
user's crontab: the nightly backup at 03:45, the restore drill on Sundays at
04:30 and, when set up, the uptime ping. Their output goes to
`/opt/officesentry/jobs.log`.

### Production from releases

Off until you turn it on. Today every deploy builds the chosen code on the
server and runs it in production. With the `PRODUCTION_FROM_TAGS` variable set
to `true`:

- **The portal only takes releases.** Deploy with a release tag such as
  `v2.1.0` in the `ref` box. The server runs the image the release workflow
  built, scanned and published, pinned by its digest, so it runs exactly what
  was tested and nothing else, even if someone later moved the tag. Deploying
  anything else stops with a message saying so.
- **Merges never deploy the portal**, whatever `AUTO_DEPLOY` says. The
  [website and public demo](#the-website-and-public-demo) still follow `main`
  by themselves, so you can try what's coming on the demo before releasing it.

To turn it on:

1. Make a release first, if there isn't one yet (CONTRIBUTING.md, "Releases").
2. *Settings → Secrets and variables → Actions → Variables → New repository
   variable*: name `PRODUCTION_FROM_TAGS`, value `true`.
3. Deploy that release: *Actions → Deploy → Run workflow*, action **deploy**,
   `ref` the release tag (the *Releases* page lists them, e.g. `v2.1.0`).

**Deploying a release from your phone:** open the repository in the GitHub
app, *Actions → Deploy → Run workflow*, leave *Use workflow from* on `main`,
action **deploy**, type the tag (`v2.1.0`) in `ref`, then *Run workflow*. The
run's summary names the release and image it deployed. Going back to an older
release is the same with its tag (and the `pre-upgrade-*` copy if the database
was upgraded since, as in *Rolling back an update*).

To turn it off, delete the variable or set it to `false`; the next deploy
builds from the code again.

### Uptime monitor

The firewall only lets your own addresses reach the portal, so an outside
monitor can't open it. Instead the server asks the portal's `/health/ready`
every 5 minutes and tells a monitor the answer, and the monitor emails you
when the pings fail or stop (the server, Docker or the portal is down, or the
database, the worker or the disk has a problem):

1. Sign up at [Healthchecks.io](https://healthchecks.io) (free for 20 checks).
2. *Add Check*: name `Office Sentry portal`, period **5 minutes**, grace
   **10 minutes**. Copy its ping address (`https://hc-ping.com/...`).
3. In GitHub, add it as the repository secret `UPTIME_PING_URL`, then deploy.
4. Within 5 minutes the check turns green. Healthchecks.io emails you when it
   stops; its app can notify your phone too.

Use a second check, with period **1 day**, for `BACKUP_PING_URL` ([Off-site
backups](#off-site-backups)).

### When the repository goes public

Runs before this change printed the portal's log in `status` and `restart`
runs, and the first-admin link. GitHub keeps run logs for 90 days, and they
become public with the repository. Before making it public, open
*Actions → Deploy*, and for each run older than this change press **⋯** and
**Delete workflow run** (or wait until they are 90 days old).

Once it is public, GitHub Free also allows a **`production` environment that
needs your approval** before any deploy reaches the server, holding the deploy
secrets so nothing else in the repository can read them. Set it up straight
after making it public:

1. *Settings → Environments → New environment*, named `production`.
2. Tick **Required reviewers** and add yourself. Under *Deployment branches
   and tags*, choose **Selected branches and tags** and add the rule `main`
   and the tag rule `v*`.
3. Under **Environment secrets**, add `HCLOUD_TOKEN`, `DEPLOY_SSH_KEY`,
   `DEPLOY_HOST_KEY` and the `BACKUP_*` and `*_PING_URL` secrets again, with
   the same values as the repository secrets.
4. Ask for the Deploy workflow's `server` job to name `environment:
   production` (a one-line change in `.github/workflows/deploy.yml`). Once
   that has merged and a deploy has worked, delete those secrets from
   *Settings → Secrets and variables → Actions* so only the environment holds
   them.

From then on every deploy waits on the run's page for you to press
**Review deployments → Approve**.

### The website and public demo

The project's own server also runs the website (officesentry.co.uk) and a
public, read-only demo with fictional clients, next to its private portal.
That's how the project shows itself, and nothing an MSP running Office Sentry
needs; it moves the portal's allow-list from the Hetzner firewall to Caddy (the
`PORTAL_ALLOWED_IPS` secret), because the public sites need port 443 open to
everyone. How it's kept apart from the portal, and how to turn it on:
[`deploy/public/README.md`](deploy/public/README.md).

### If you need a shell

Add an inbound rule for TCP 22 from your current address in the firewall,
connect in Termius as `deploy@<server IP>` with the deploy key, run
`cd /opt/officesentry/v2`, and the commands elsewhere in this file work as
written (`.env` already picks the https and demo parts). Remove the rule afterwards.

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
