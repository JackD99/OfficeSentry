# Unraid, Synology and TrueNAS

A NAS that runs containers can run Office Sentry: it uses the one-file stack,
[`v2/deploy/stack/compose.yaml`](../../deploy/stack/compose.yaml), with the data on the NAS's own disks.

> **Tested:** the stack file, with Docker Compose 5.6, with Docker volumes and with host folders owned by
> user ID 10001. Unraid, DSM and TrueNAS themselves: not yet. If a step doesn't match what you see, please
> [open an issue](https://github.com/JackD99/OfficeSentry/issues/new/choose).

## Before you start

- **Memory and CPU.** Office Sentry wants 2 CPUs and 4 GB of memory of its own, on top of what the NAS
  already uses. Intel, AMD and ARM (arm64) NAS systems all work.
- **HTTPS, and who can reach it.** The NAS's own web interface usually holds ports 80 and 443, so the stack's
  Caddy can't use them. The simplest way to reach the portal from outside the office, with HTTPS and no port
  opened on your router, is [Cloudflare Tunnel](cloudflare.md#cloudflare-tunnel), which the stack includes. On
  the office network only, the NAS's own reverse proxy works too (Synology, below). Nothing from Microsoft
  connects to the portal, so it doesn't need to be on the internet at all; but sign-in needs HTTPS.
- **The keys.** The key that decrypts the stored certificates is generated into its own folder or volume.
  Save a copy in your password manager ([Where the keys are](../../DEPLOY.md#where-the-keys-are)) and keep it
  out of your NAS backups where you can (each section says how).
- **Restoring a backup.** Stop both containers, then run `restore` in a one-off container given the same
  data and keys: [Portainer](portainer.md#keys-and-backups) has the `docker run` command. Use your own
  folders in place of the volume names (on Unraid, `-v /mnt/user/appdata/officesentry/data:/data -v
  /mnt/user/appdata/officesentry-keys:/keys`), and start the containers again afterwards.

## Unraid

Unraid 6.12 or later, with the **Docker Compose Manager** plugin (*Apps*, search "Compose Manager").

1. Make the folders, owned by the user Office Sentry runs as. Open the terminal (the `>_` icon at the top
   right) and run:

   ```bash
   mkdir -p /mnt/user/appdata/officesentry/data /mnt/user/appdata/officesentry-keys
   chown 10001 /mnt/user/appdata/officesentry/data /mnt/user/appdata/officesentry-keys
   ```

   Keep `appdata` on a pool (the cache), as Unraid does by default: the database wants a fast disk, not the
   array.
2. *Docker → Compose → Add New Stack*, named `officesentry`. Open its gear menu: **Edit Stack → Compose File**,
   paste the stack file and save; then **Edit Stack → ENV File**:

   ```text
   OFFICESENTRY_VERSION=2.4.1
   OFFICESENTRY_BASE_URL=https://sentry.yourmsp.com
   OFFICESENTRY_TIMEZONE=Europe/London
   OFFICESENTRY_DATA=/mnt/user/appdata/officesentry/data
   OFFICESENTRY_KEYS=/mnt/user/appdata/officesentry-keys
   COMPOSE_PROFILES=tunnel
   CLOUDFLARE_TUNNEL_TOKEN=<the tunnel's token>
   ```

   Leave out the last two lines if you put a proxy container in front instead (Nginx Proxy Manager or SWAG:
   see [Behind your own proxy](portainer.md#behind-your-own-proxy)).
3. **Compose Up**. The Docker tab shows `officesentry-web-1` and `officesentry-worker-1`.
4. The first admin's link is in `officesentry-web-1`'s log (its icon → **Logs**), on the line starting
   **No admin account yet**. Then carry on from step 6 of [First start](../../DEPLOY.md#first-start).
5. Nightly backup: with the **User Scripts** plugin, add a script scheduled daily:

   ```bash
   #!/bin/bash
   docker exec officesentry-web-1 python -m officesentry backup --keep 14
   ```

   From 2.4.0 this script is optional: the worker makes the same copy by itself every night at 03:30 and keeps
   the last 7 (`OFFICESENTRY_BACKUP_CRON` and `OFFICESENTRY_BACKUP_KEEP` change when and how many; see
   [Backups](../../DEPLOY.md#backups)).

   The copies land in `/mnt/user/appdata/officesentry/data/backups`, which an appdata backup (the
   **Appdata Backup** plugin) then copies off the pool. The keys are in their own folder,
   `/mnt/user/appdata/officesentry-keys`: exclude it from the appdata backup and keep its `secrets.json` in
   your password manager instead.

Don't run *Tools → New Permissions* (or "Docker Safe New Perms") over these folders: it gives them back to
`nobody`, and the portal then stops with `PermissionError: [Errno 1] Operation not permitted: '/data'`. If
it happens, run the `chown` line from step 1 again.

To update, read the [release notes](https://github.com/JackD99/OfficeSentry/releases), change
`OFFICESENTRY_VERSION` in **Edit Stack → ENV File**, then press **Compose Pull** and **Compose Up**. If the
release changed the stack file, paste the new one in **Edit Stack → Compose File** first.

## Synology

DSM 7.2 or later, with **Container Manager** (*Package Center*). Container Manager projects don't take
environment variables, so you write your settings into the stack itself; the file stays on the NAS.

1. In *File Station*, make a folder `officesentry` in the `docker` shared folder.
2. *Container Manager → Project → Create*: name `officesentry`, path `/docker/officesentry`, source
   **Create docker-compose.yml**. Paste the stack file, then change the lines under `x-settings` at the top,
   replacing each `${...}` with your value:

   ```yaml
   x-settings: &settings
     OFFICESENTRY_BASE_URL: https://sentry.yourmsp.com
     OFFICESENTRY_TIMEZONE: Europe/London
     OFFICESENTRY_COLLECT_CRON: 0 2 * * *
     OFFICESENTRY_WORKER_CONCURRENCY: 4
     OFFICESENTRY_TRUSTED_PROXIES: 1
     OFFICESENTRY_SECRET_KEY: ""
     OFFICESENTRY_ENCRYPTION_KEYS: ""
     OFFICESENTRY_SECRETS_FILE: /keys/secrets.json
   ```

   and on the `image:` line, put the release instead of `${OFFICESENTRY_VERSION:-2}`
   (`ghcr.io/jackd99/officesentry:2.4.1`). For Cloudflare Tunnel, also delete the `profiles: ["tunnel"]` line
   under `cloudflared` and put the token in place of `${CLOUDFLARE_TUNNEL_TOKEN:-}`. Leave the data and keys
   as Docker volumes: Container Manager makes them writable for Office Sentry by itself.
3. **Next**, **Done**. The project builds and starts; *Container* shows `officesentry-web-1` and
   `officesentry-worker-1`.
4. The first admin's link is in `officesentry-web-1`'s **Log** tab, on the line starting
   **No admin account yet**. Then carry on from step 6 of [First start](../../DEPLOY.md#first-start).
5. Save the keys: the container's **Terminal** tab, **Create**, then run `cat /keys/secrets.json` and put the
   line in your password manager.
6. Nightly backup: *Control Panel → Task Scheduler → Create → Scheduled Task → User-defined script*, user
   **root**, daily at 03:45, with:

   ```bash
   /usr/local/bin/docker exec officesentry-web-1 python -m officesentry backup --keep 14
   mkdir -p /volume1/docker/officesentry/backups
   /usr/local/bin/docker cp officesentry-web-1:/data/backups/. /volume1/docker/officesentry/backups/
   find /volume1/docker/officesentry/backups -maxdepth 1 -name 'officesentry-*.db.gz' -mtime +14 -delete
   ```

   The third line copies the backups out of the Docker volume into the shared folder, where
   **Hyper Backup** can take them off the NAS, and the last one deletes copies older than two weeks there,
   as the volume does. The keys never go there.

   Keep the task from 2.4.0 on, too. The worker's own nightly copy stays in the volume, and the worker skips
   it on nights the task has made one.

**HTTPS on the office network:** *Control Panel → Login Portal → Advanced → Reverse Proxy → Create*: source
HTTPS, your hostname, port 443; destination HTTP, `localhost`, port 8000. Give it a certificate under
*Control Panel → Security → Certificate*. Then check it as in [Checking it worked](#checking-it-worked).

To update, change the release on the `image:` line (*Project → officesentry → YAML configurations*), then
**Build** the project again.

## TrueNAS

TrueNAS Community Edition (SCALE) 24.10 or later, where apps run on Docker. Earlier SCALE releases ran
apps on Kubernetes and aren't covered.

1. *Apps → Discover Apps → Custom App* (the menu beside it) → **Install via YAML**. Name it `officesentry`.
2. Paste the stack file and set your values under `x-settings` and on the `image:` line exactly as in step 2
   of [Synology](#synology). For the tunnel, delete `cloudflared`'s `profiles:` line and put in its token.
   Leave the data and keys as Docker volumes.
3. **Save**. The app starts; its page shows the containers and their logs. The first admin's link is in the
   `web` container's log, on the line starting **No admin account yet**. Then carry on from step 6 of
   [First start](../../DEPLOY.md#first-start).
4. Nightly backup: *System → Advanced Settings → Cron Jobs → Add*, user `root`, daily at 03:45. TrueNAS names
   the containers after the app with `ix-` in front; check with `docker ps` in *System → Shell*. The command:

   ```bash
   docker exec ix-officesentry-web-1 python -m officesentry backup --keep 14 && mkdir -p /mnt/tank/backups/officesentry && docker cp ix-officesentry-web-1:/data/backups/. /mnt/tank/backups/officesentry/ && find /mnt/tank/backups/officesentry -maxdepth 1 -name 'officesentry-*.db.gz' -mtime +14 -delete
   ```

   with `/mnt/tank/backups` a dataset of yours that a replication or Cloud Sync task already takes off the
   NAS. The `find` at the end deletes copies older than two weeks there, as the volume does.

   Keep the job from 2.4.0 on, too. The worker's own nightly copy stays in the volume, and the worker skips
   it on nights the job has made one.
5. Save the keys: `docker exec ix-officesentry-web-1 cat /keys/secrets.json` in *System → Shell*, into your
   password manager.

To update, edit the app's YAML (*Apps → officesentry → Edit*), change the release on the `image:` line and
save.

## Checking it worked

Sign in through the HTTPS address, then open *Settings → Activity log*: your sign-in should show your own
address, not the proxy's or `127.0.0.1`. In the browser's developer tools the page should carry a
`Strict-Transport-Security` header; if it doesn't, the proxy isn't telling the portal it's on HTTPS
([Securing the portal](../securing-the-portal.md#behind-a-reverse-proxy)).
