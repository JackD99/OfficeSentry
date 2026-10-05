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

On Hetzner with only a phone? See [Hetzner, from a phone](#hetzner-from-a-phone).

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
| `OFFICESENTRY_WORKER_CONCURRENCY` | `4` | How many tenants are collected at once. Each tenant still runs one data source at a time. With many tenants, raise it (8 suits about 200) so the night's collection finishes before morning. |

"Run now", a tenant's first collection and the refresh before an emailed
report go ahead of the nightly queue, so they don't wait for every tenant to
be collected.

Some cron examples: `0 2 * * *` nightly at 02:00, `0 2 * * 1-5` weeknights
only, `30 1,13 * * *` twice a day at 01:30 and 13:30.

## How long data is kept

Trend numbers and findings history are kept forever. Full snapshots (every
user, policy and mailbox from a run) take the most space, so the worker keeps,
for each tenant and data source:

- the last run of each of the last **30 days** (`OFFICESENTRY_KEEP_DAILY`), and
- the last run of each of the last **12 months** (`OFFICESENTRY_KEEP_MONTHLY`).

Older snapshots are cleared once a day. Run pages for cleared snapshots still
show their numbers. Run logs older than 90 days keep only their warnings and
errors, so a page still says why an old run failed or was partial.

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

**Deleted tenants stay in backups.** Backups keep everything that was in the
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

## Hetzner, from a phone

Everything here works from a phone browser: the Hetzner Cloud console, GitHub
(use github.com in the browser for Settings; the GitHub app can run
workflows) and an SSH app such as Termius to make a key. You never type
commands on the server. The **Deploy** workflow (`.github/workflows/deploy.yml`)
copies the app over and starts it, and the server's Hetzner firewall decides
who can open the portal.

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
removes it at the end.

### 3. Create the server

*Servers → Add server*:

- **Image** Ubuntu 24.04. **Type** shared vCPU, x86, 2 vCPUs and 4 GB (the
  smallest is fine to start).
- **Networking** public IPv4 (and IPv6 if you like).
- **SSH keys** none needed.
- **Firewalls** tick `officesentry`.
- **Backups** worth ticking: Hetzner keeps a copy of the whole server, data
  and `secrets.json` included, for 7 days, and the server saves a consistent
  database copy every night at 03:30 for those to hold.
- **Cloud config** paste [`deploy/cloud-init.yaml`](deploy/cloud-init.yaml),
  with its `ssh-ed25519 AAAA...` line replaced by your public key (Termius:
  the key → *Export to clipboard* or copy the public key).

Create it and note its IPv4 address. It takes a few minutes to install Docker.

### 4. Point your domain at it

At your DNS provider add an **A** record for, say, `sentry.yourmsp.com` with
the server's IPv4 address. Only add an AAAA (IPv6) record if your firewall
rules list your IPv6 ranges too, or browsers that prefer IPv6 won't get in.

### 5. Tell GitHub where to deploy

In Hetzner: *Security → API tokens → Generate API token*, **Read & Write**
(the workflow edits the firewall with it).

In GitHub: the repository's *Settings → Secrets and variables → Actions*.

| Secrets | |
|---|---|
| `HCLOUD_TOKEN` | the Hetzner API token |
| `DEPLOY_SSH_KEY` | the deploy key's **private** key, all of it, including the BEGIN and END lines |

| Variables | |
|---|---|
| `DEPLOY_HOST` | the server's IPv4 address |
| `OFFICESENTRY_DOMAIN` | the domain alone, e.g. `sentry.yourmsp.com` |
| `OFFICESENTRY_TIMEZONE` | e.g. `Europe/London` (default UTC) |
| `HCLOUD_FIREWALL` | only if the firewall isn't called `officesentry` |
| `OFFICESENTRY_DEMO_DOMAIN` | optional: turns on the demo site (below), e.g. `demo.sentry.yourmsp.com` |
| `OFFICESENTRY_SETTINGS` | optional: more `.env` lines, one per line, e.g. `OFFICESENTRY_COLLECT_CRON=0 1 * * *` |
| `AUTO_DEPLOY` | `true` to deploy every merge to main once its tests pass |
| `DEPLOY_HOST_KEY` | optional: the server's SSH host key, which the first run prints, so later runs refuse a different server |

The workflow writes the server's `.env` from these on every deploy (with
`OFFICESENTRY_TRUSTED_PROXIES=1` for Caddy), so change settings here, not on
the server.

### 6. Deploy and create the admin

*Actions → Deploy → Run workflow*, action **deploy**. When it's green, open
the step **Show the first-admin link**, open that link on your phone and
create the admin account (with two-step sign-in), as in *First start*. The
link only works until the first admin exists. Then carry on from step 6 of
*First start*.

### Everyday jobs

All from *Actions → Deploy → Run workflow*:

| Action | Does |
|---|---|
| `demo-reset` | deploys, then fills the demo site with fresh demo data; the sign-in is in the run's summary |
| `deploy` | copies the chosen branch, tag or commit and rebuilds; the portal is only down while the containers swap |
| `status` | container health, free disk and the last log lines |
| `backup` | a database copy now, keeping 14 |
| `restart` | restarts the portal, worker and Caddy |
| `setup-link` | the first-admin link again |

To go back to an older version, deploy its commit or tag in the `ref` box; if
the database was upgraded since, restore the `pre-upgrade-*` copy as in
*Rolling back an update* (that needs a shell, below).

### A demo site to show people

The demo tenants from `scripts/demo_data.py` (three fictional clients with five
weeks of history, and a made-up MSP's branding) can run as a second site on
the same server, with its own data, so they never mix with real clients:

1. Add an A record for, say, `demo.sentry.yourmsp.com` pointing at the server.
2. Set the `OFFICESENTRY_DEMO_DOMAIN` variable to that name.
3. Run the Deploy workflow with action **demo-reset**. Its summary shows the
   sign-in: user `demo` and a new password.

The demo site uses the same firewall, so whoever can open the portal can open
the demo. Two-step sign-in is off there so colleagues can share the account,
and it never collects (the tenants aren't real), so dates in it age:
**demo-reset** again before a demo for fresh data and a new password. Don't
press "Run now" on a demo tenant; it fails, because there is nothing to
connect to. To remove the demo site, delete the variable and deploy; its data
stays in the `officesentry-demo-data` volume until you remove that.

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
