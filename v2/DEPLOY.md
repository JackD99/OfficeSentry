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

That writes to `/data/backups` inside the volume and keeps the last 7
(`--keep 14` keeps more). Each copy is checked before it's kept.

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

**Keep `secrets.json` too**, somewhere safe and separate from the backups:

```bash
docker compose cp web:/data/secrets.json ./secrets.json
```

Without it the stored certificate and mail passwords can't be decrypted; with
it, anyone holding a backup can. It only changes when you run `rotate-keys
--new`, so copy it again after that.

Before every upgrade that changes the database, the portal also saves a copy
as `/data/backups/pre-upgrade-v<old>-to-v<new>-<time>.db` (the last 3 are
kept). If it can't (a full disk, say), it doesn't upgrade and says why.

**Deleted tenants stay in backups.** Deleting a tenant removes it from the
database, not from backups taken before. Nightly backups rotate out after
`--keep` days, but `pre-upgrade-*` and `pre-restore-*` copies are kept until
3 newer ones replace them. If a client asks for their data to be erased, also
delete the older backups (or tell them when they rotate out).

## Restoring a backup

1. Stop the portal and the worker (the restore refuses while the worker is
   running):

   ```bash
   docker compose stop web worker
   ```

2. Restore it, naming a file in the volume (`docker compose run --rm --no-deps
   web ls /data/backups` lists them):

   ```bash
   docker compose run --rm --no-deps web python -m officesentry restore /data/backups/officesentry-20261005-033000.db
   ```

   A backup kept on the host goes in first with
   `docker compose cp ./officesentry-20261005-033000.db web:/data/restore.db`
   (then restore `/data/restore.db`), or from the `/backups` folder if you
   mounted one.

   It checks that the file is a sound Office Sentry database, saves the
   database it replaces as `/data/backups/pre-restore-<time>.db`, and copies
   the backup in with SQLite's backup API (no stale `-wal` or `-shm` files are
   left behind).
3. Put back the `secrets.json` from the same time as the backup if the keys
   changed since (`docker compose cp ./secrets.json web:/data/secrets.json`).
4. Start again: `docker compose up -d`. A backup from an older version is
   upgraded when the portal starts.

**Moving to a new server** is the same: install as in *First start* (skip
creating the admin), stop both containers, copy in `secrets.json` and the
backup, restore, start.

## Updating

```bash
git pull                     # or copy the new v2 folder over the old one
docker compose --profile https up -d --build
```

The portal upgrades the database on start, before the worker starts, after
saving a `pre-upgrade-*` copy (see Backups). Data in the volume is kept.

### Rolling back an update

Database upgrades only go forward, so going back to an older version means
going back to the database from before the upgrade. Anything collected since
the upgrade is lost.

1. `docker compose stop web worker`
2. Go back to the version you had: `git log --oneline` to find it, then
   `git checkout <commit>` (or copy the old v2 folder back).
3. `docker compose build`
4. Restore the copy taken before the upgrade:

   ```bash
   docker compose run --rm --no-deps web ls /data/backups
   docker compose run --rm --no-deps web python -m officesentry restore /data/backups/pre-upgrade-v8-to-v9-20261005-020000.db
   ```

   Versions from before the `restore` command: stop both containers, copy the
   backup over `/data/officesentry.db`, and delete `/data/officesentry.db-wal`
   and `/data/officesentry.db-shm` before starting.
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
   `OFFICESENTRY_DATA_DIR` (say `C:\OfficeSentry\data`; only the service
   account should be able to read it, since `secrets.json` lives there).
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
