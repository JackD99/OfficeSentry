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
5. Open the portal in your browser and create the first admin account.
6. Follow the steps on the **App connection** page, then add your tenants.

## Is it working?

`docker compose ps` should show `web` and `worker` as **healthy**. If the
worker stops, admins see a yellow notice at the top of every page.

To see what the worker is doing: `docker compose logs -f worker`

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

To back up every night, add this line with `crontab -e` (change the folder):

```
30 3 * * * cd /opt/officesentry/v2 && docker compose exec -T web python -m officesentry backup
```

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
