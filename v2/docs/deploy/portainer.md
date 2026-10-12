# Portainer

Run Office Sentry as a Portainer stack on a Docker host Portainer manages. You paste one file, set a few
environment variables in Portainer, and deploy; Portainer then shows the portal and worker, their logs and a
console.

> **Tested:** the stack file, with Docker Compose 5.6: both containers healthy, named volumes and host folders,
> keys from variables, and the HTTPS profile. Portainer's own screens: not yet. The steps follow Portainer CE
> 2.2x; if a screen differs, please [open an issue](https://github.com/JackD99/OfficeSentry/issues/new/choose).

## What you need

- Portainer CE or Business managing a **standalone Docker** environment (local socket or the Portainer
  Agent), on a Linux machine with at least 2 CPUs and 4 GB of memory free for Office Sentry. Not a Swarm
  environment ([why](README.md#not-supported-and-why)); for a Kubernetes environment see
  [Kubernetes](kubernetes.md).
- A domain name for the portal, such as `sentry.yourmsp.com`, and a way to put HTTPS in front: the stack's
  own Caddy (when nothing else on the machine uses ports 80 and 443), a proxy you already run (Nginx Proxy
  Manager, Traefik), or [Cloudflare Tunnel](cloudflare.md#cloudflare-tunnel).

## 1. Create the stack

1. In Portainer, pick the environment, then **Stacks → Add stack**.
2. **Name:** `officesentry`.
3. **Build method: Web editor.** Paste the whole of
   [`v2/deploy/stack/compose.yaml`](../../deploy/stack/compose.yaml) (on GitHub, open it and use **Copy raw
   file**). Don't change it: everything you set goes in the next step.

   Prefer Portainer to fetch it? Choose **Repository** instead, with URL
   `https://github.com/JackD99/OfficeSentry`, reference `refs/heads/main` and compose path
   `v2/deploy/stack/compose.yaml`. The release you run is still chosen by `OFFICESENTRY_VERSION`.

## 2. Set its environment variables

Under **Environment variables**, switch to **Advanced mode** and paste, with your own values:

```text
OFFICESENTRY_VERSION=2.3.0
OFFICESENTRY_BASE_URL=https://sentry.yourmsp.com
OFFICESENTRY_TIMEZONE=Europe/London
```

`OFFICESENTRY_VERSION` is the [release](https://github.com/JackD99/OfficeSentry/releases) to run (an exact
one is safest). Then add the lines for how HTTPS reaches it:

| HTTPS from | Add |
|---|---|
| The stack's own Caddy, which gets a Let's Encrypt certificate (ports 80 and 443 free, DNS pointing here) | `COMPOSE_PROFILES=https` and `OFFICESENTRY_DOMAIN=sentry.yourmsp.com` |
| [Cloudflare Tunnel](cloudflare.md#cloudflare-tunnel) (no ports opened) | `COMPOSE_PROFILES=tunnel` and `CLOUDFLARE_TUNNEL_TOKEN=<the tunnel's token>` |
| A proxy you already run | nothing here; see [Behind your own proxy](#behind-your-own-proxy) |

Every other setting in [`.env.example`](../../.env.example) can be set the same way once it has a line under
`x-settings` at the top of the stack (copy the pattern of the lines there). Portainer keeps these variables
in its own database, so anyone who can edit the stack in Portainer can read them: limit who has that access.

## 3. Deploy and create the first admin

1. Press **Deploy the stack**. Portainer pulls the image and starts `web`, then `worker` once the portal is
   healthy (a minute or two). **Containers** shows both as *healthy*.
2. If you set `COMPOSE_PROFILES` and there's no `caddy` (or `cloudflared`) container, your Portainer doesn't
   pass it on: open the stack's **Editor**, delete the `profiles:` line under that service, and press
   **Update the stack**.
3. Open **Containers → officesentry-web-1 → Logs** and find the line starting **No admin account yet**. Open
   its link, choose a username and password, scan the QR code with an authenticator app and save the
   recovery codes. Lost the line? **Console** on the same container, **Connect** with `/bin/sh`, then run
   `python -m officesentry setup-link`.
4. Carry on from step 6 of [First start](../../DEPLOY.md#first-start): the **Get started** checklist on your
   home page walks through the app connection and your first client.

## Behind your own proxy

The portal listens on `127.0.0.1:8000` on the Docker host. A proxy installed on the host itself (nginx,
Caddy) can use that address as it is ([Securing the portal](../securing-the-portal.md#behind-a-reverse-proxy)).

A proxy running in another container (Nginx Proxy Manager, Traefik, SWAG) reaches the portal by name on a
shared Docker network instead. In the stack's **Editor**, add the network your proxy uses (its name is
under **Networks**) and attach `web` to it:

```yaml
services:
  web:
    # ...everything already there, plus:
    networks: [default, proxy]

networks:
  proxy:
    external: true
    name: npm_default        # your proxy's network
```

then point the proxy at `http://officesentry-web-1:8000`. In Nginx Proxy Manager: **Add Proxy Host**,
scheme `http`, forward hostname `officesentry-web-1`, port `8000`, and on the **SSL** tab a Let's Encrypt
certificate with **Force SSL**. Leave `OFFICESENTRY_TRUSTED_PROXIES` at 1. Sign in, then check
*Settings → Activity log* shows your own address, not the proxy's.

## Keys and backups

The keys are generated into the `officesentry_officesentry-keys` volume on first start. Save a copy in your
password manager: **Console** on `officesentry-web-1`, then `cat /keys/secrets.json`
([Where the keys are](../../DEPLOY.md#where-the-keys-are)). To keep them in Portainer instead, set
`OFFICESENTRY_SECRET_KEY` and `OFFICESENTRY_ENCRYPTION_KEYS` before the first deploy
([Keys from a secret store](README.md#keys-from-a-secret-store)).

Portainer doesn't schedule commands, so make the nightly backup with the Docker host's own scheduler
(`crontab -e` as a user who can run Docker):

```text
45 3 * * * docker exec officesentry-web-1 python -m officesentry backup --keep 14
```

Copies go to the `backups` folder in the data volume (on the host,
`/var/lib/docker/volumes/officesentry_officesentry-data/_data/backups`). Include that folder in whatever
backs up the host, or set `OFFICESENTRY_DATA` to a host folder owned by user ID 10001 that's already backed up.
*Settings → System status* shows when the last backup ran and alerts when it fails or is overdue.

To restore one: **Stop** both containers, then on the Docker host

```bash
docker run --rm -v officesentry_officesentry-data:/data -v officesentry_officesentry-keys:/keys \
  -e OFFICESENTRY_SECRETS_FILE=/keys/secrets.json ghcr.io/jackd99/officesentry:2.3.0 \
  python -m officesentry restore /data/backups/<file>.db.gz
```

and **Start** them again ([Restoring a backup](../../DEPLOY.md#restoring-a-backup) explains what it does).

## Updating

Read the release notes first ([releases](https://github.com/JackD99/OfficeSentry/releases)), above all
**Action required**, **New Microsoft permissions** and **Database upgrade**. Then open the stack, change
`OFFICESENTRY_VERSION`, and press **Update the stack** with **Re-pull image and redeploy** on. The portal
upgrades the database before the worker starts again. Going back needs the database from before the upgrade
([Rolling back an update](../../DEPLOY.md#rolling-back-an-update)).

## If it doesn't start

| You see | Fix |
|---|---|
| `required variable OFFICESENTRY_BASE_URL is missing a value` | Add `OFFICESENTRY_BASE_URL` under the stack's environment variables. |
| `PermissionError: [Errno 1] Operation not permitted: '/data'` in the portal's log | `OFFICESENTRY_DATA` (or `OFFICESENTRY_KEYS`) names a host folder user ID 10001 doesn't own: `sudo chown 10001 <folder>` on the host. |
| `Bind for 127.0.0.1:8000 failed: port is already allocated` | Another container uses port 8000: set `OFFICESENTRY_PORT=127.0.0.1:8001`. |
| The worker stays *unhealthy* | Its log (**Containers → officesentry-worker-1 → Logs**) says why; [Troubleshooting](../troubleshooting.md) covers the common causes. |
| Signing in doesn't stick | Sign-in only works over HTTPS, by design: open the `https://` address, not `http://<host>:8000`. |
