# Running Office Sentry on your own server

Office Sentry runs as two containers from one image: the **portal** (the
website) and the **worker** (collects from tenants, sends scheduled emails,
tidies old data). Everything it stores lives in one Docker volume.

## What you need

- A Linux server or VM with [Docker](https://docs.docker.com/engine/install/)
  installed. A small VM (2 CPUs, 2 GB RAM) is a reasonable start; disk use
  grows with the number of tenants and users.
- A domain name for the portal, such as `sentry.yourmsp.com`, with its DNS
  pointing at the server, if you want the built-in HTTPS.

## First start

1. Copy the `v2` folder to the server and open a terminal in it.
2. Create your settings file: `cp .env.example .env`
3. Open `.env` in an editor (`nano .env`) and set at least:
   - `OFFICESENTRY_BASE_URL` to the address people will use, e.g. `https://sentry.yourmsp.com`
   - `OFFICESENTRY_DOMAIN` to the domain alone, e.g. `sentry.yourmsp.com`
   - `OFFICESENTRY_TIMEZONE` to your timezone, e.g. `Europe/London`
4. Start it, with automatic HTTPS:

   ```bash
   docker compose --profile https up -d --build
   ```

   Already have a reverse proxy (nginx, Traefik)? Leave out `--profile https`
   and point the proxy at `http://127.0.0.1:8000`.
5. Create the first admin account. Run `docker compose logs web`, find the line
   starting **No admin account yet** and open the link on it (`docker compose exec
   web python -m officesentry setup-link` prints it again). Choose a username and
   password, scan the QR code with an authenticator app and save the recovery codes.
6. Follow the steps on the **App connection** page, then add your tenants.

## Is it working?

`docker compose ps` should show `web` and `worker` as **healthy**. If the
worker stops, admins see a yellow notice at the top of every page.

To see what the worker is doing: `docker compose logs -f worker`

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

## When things run

| Setting in `.env` | Default | What it does |
|---|---|---|
| `OFFICESENTRY_COLLECT_CRON` | `0 2 * * *` | When every tenant is collected, as cron (minute hour day month weekday). The default is 02:00 every night. |
| `OFFICESENTRY_TIMEZONE` | `UTC` | The timezone for the collection time and for emailed reports. |
| `OFFICESENTRY_MAX_AGE_HOURS` | `12` | A collection is skipped if it already ran this recently (say, by hand that evening). Before a scheduled email goes out, anything older than this is collected again first, so the report is fresh. |

Some cron examples: `0 2 * * *` nightly at 02:00, `0 2 * * 1-5` weeknights
only, `30 1,13 * * *` twice a day at 01:30 and 13:30.

## How long data is kept

Trend numbers and findings history are kept forever. Full snapshots (every
user, policy and mailbox from a run) take the most space, so the worker keeps,
for each tenant and data source:

- the last run of each of the last **30 days** (`OFFICESENTRY_KEEP_DAILY`), and
- the last run of each of the last **12 months** (`OFFICESENTRY_KEEP_MONTHLY`).

Older snapshots are cleared once a day. Run pages for cleared snapshots still
show their log and numbers.

The **activity log** (*Settings → Activity log*, admins only) records sign-ins
and every change, with the address it came from; filter it by user, action,
tenant and date, and download it as CSV. Failed and slowed-down sign-ins are
removed after 90 days; everything else is kept. Failed sign-ins with
usernames that don't exist are counted per address and hour, without storing
what was typed.

## Backups

Everything is in the `officesentry-data` volume. To save a safe copy of the
database while it's running:

```bash
docker compose exec web python -m officesentry backup
```

That writes to `/data/backups` inside the volume and keeps the last 7. To get
it off the server, copy it out:

```bash
docker compose cp web:/data/backups ./backups
docker compose cp web:/data/secrets.json ./secrets.json
```

Keep `secrets.json` somewhere safe and separate: without it the stored
certificate can't be decrypted, and with it anyone holding the backup can.

Backups keep everything that was in the database when they were made. After
you delete a client tenant, its data stays in older backups until they rotate
out (the last 7 are kept) and in any copies you took off the server; delete
those too if the client asks for their data to be erased.

To back up every night, add this line with `crontab -e` (change the folder):

```
30 3 * * * cd /opt/officesentry/v2 && docker compose exec -T web python -m officesentry backup
```

## Rotating keys

If you think the encryption key may have leaked (a lost backup together with
`secrets.json`, say), replace it:

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

Ran `rotate-keys --new` before this release? It didn't re-encrypt the alert
webhook, so enter the webhook URL and secret again under *Settings → Alerts*.
Old backups are still encrypted with the old key.

## Updating

```bash
git pull                     # or copy the new v2 folder over the old one
docker compose --profile https up -d --build
```

The portal upgrades the database on start, before the worker starts. Data in
the volume is kept.

## Stopping

`docker compose down` stops everything and keeps your data. A collection in
progress is given up to two minutes to finish. Never use `down -v` unless you
mean to delete all data.
