# Other container managers

Dockge, Komodo, Coolify and Dokploy all deploy a compose file with environment variables set in their own
screens, so they run the same one-file stack as [Portainer](portainer.md):
[`v2/deploy/stack/compose.yaml`](../../deploy/stack/compose.yaml). This page says what's different in each.

> **Tested:** the stack file, with Docker Compose 5.6 (both containers healthy, `COMPOSE_PROFILES` read from
> an env file). The managers' own screens: not yet. If a step doesn't match what you see, please
> [open an issue](https://github.com/JackD99/OfficeSentry/issues/new/choose).

## What every manager needs

- **The stack file as it is**, pasted into the manager's compose editor. Settings go in the manager's
  environment variables, never in the file.
- **At least these variables:**

  ```text
  OFFICESENTRY_VERSION=2.3.1
  OFFICESENTRY_BASE_URL=https://sentry.yourmsp.com
  OFFICESENTRY_TIMEZONE=Europe/London
  ```

  plus, for HTTPS without a proxy of the manager's own, `COMPOSE_PROFILES=https` and
  `OFFICESENTRY_DOMAIN=sentry.yourmsp.com` (the stack's Caddy), or `COMPOSE_PROFILES=tunnel` and
  `CLOUDFLARE_TUNNEL_TOKEN=...` ([Cloudflare Tunnel](cloudflare.md#cloudflare-tunnel)).
- **A standalone Docker host** with 2 CPUs and 4 GB of memory for Office Sentry, not a Swarm.
- **The first admin's link**, from the line starting **No admin account yet** in the `web` container's log,
  or by running `python -m officesentry setup-link` in it. Then carry on from step 6 of
  [First start](../../DEPLOY.md#first-start).
- **A nightly backup.** Run `python -m officesentry backup --keep 14` in the `web` container every night,
  with the manager's scheduler if it has one for commands in a container, otherwise the host's
  (`45 3 * * * docker exec <web container> python -m officesentry backup --keep 14` in `crontab -e`). Save
  `/keys/secrets.json` (`cat` it in the `web` container) in your password manager.
  <!-- since 2.4.0 -->
  From 2.4.0 the scheduled command is optional: the worker makes the same copy by itself every night and
  keeps the last 7 ([Backups](../../DEPLOY.md#backups)).
  <!-- /since -->
- **Updating:** read the [release notes](https://github.com/JackD99/OfficeSentry/releases), change
  `OFFICESENTRY_VERSION`, and redeploy with a fresh pull of the image.

## Dockge

1. **+ Compose**, stack name `officesentry`.
2. Paste the stack file into `compose.yaml`, and the variables into the `.env` box under it.
3. **Deploy**. Dockge runs Docker Compose itself, so `COMPOSE_PROFILES` in `.env` switches Caddy or the tunnel
   on.
4. The stack page shows both containers' logs; its terminal can run commands in `officesentry-web-1`.

The stack lives in `/opt/stacks/officesentry` on the host; the data and keys are Docker volumes
(`officesentry_officesentry-data`, `officesentry_officesentry-keys`), not files in that folder.

## Komodo

1. **Stacks → New Stack**, name `officesentry`, on the server that should run it.
2. Choose a UI-defined compose file and paste the stack file. (Or point it at the Git repository
   `JackD99/OfficeSentry`, branch `main`, file path `v2/deploy/stack/compose.yaml`.)
3. Put the variables in the stack's **Environment**. Komodo writes them to an env file it passes to Docker
   Compose, so `COMPOSE_PROFILES` works. Keep the tunnel token and any keys in Komodo's secret
   **Variables** and refer to them as `[[NAME]]`, so they're hidden in the interface and logs.
4. **Deploy**. The stack's page shows the logs; run commands with the container terminal or `docker exec` on
   the server.

## Coolify

Coolify puts its own proxy (Traefik or Caddy) in front, with certificates, so leave `COMPOSE_PROFILES` out.

1. In a project, **+ New → Docker Compose Empty**, and paste the stack file.
2. Coolify lists the stack's variables under **Environment Variables**: fill in `OFFICESENTRY_BASE_URL`,
   `OFFICESENTRY_TIMEZONE` and `OFFICESENTRY_VERSION`. Leave `OFFICESENTRY_TRUSTED_PROXIES` at 1 (Coolify's
   proxy).
3. Give the **web** service the domain, with the portal's port after it: `https://sentry.yourmsp.com:8000`.
   Give the worker none.
4. If another app on the server already uses port 8000 on `127.0.0.1`, set `OFFICESENTRY_PORT` to a free one
   such as `127.0.0.1:8010` (the proxy reaches the portal on Coolify's network, not through that port).
5. **Deploy**. The first admin's link is in the `web` container's logs; Coolify's **Terminal** runs commands
   in it, and **Scheduled Tasks** can run the nightly backup in the `web` container.

## Dokploy

Dokploy puts Traefik in front, with certificates, so leave `COMPOSE_PROFILES` out.

1. In a project, **Create Service → Compose**, then **Raw** as the provider and paste the stack file.
2. Put the variables in the **Environment** tab. Leave `OFFICESENTRY_TRUSTED_PROXIES` at 1 (Traefik).
3. In **Domains**, add `sentry.yourmsp.com` for the service `web`, container port `8000`, with HTTPS and a
   Let's Encrypt certificate. Give the worker none.
4. **Deploy**. If the domain answers *Bad Gateway* while both containers are healthy, Traefik can't reach
   the `web` container: follow Dokploy's documentation for putting a compose service on `dokploy-network`.

## Checking it worked

Whatever the manager, sign in, then open *Settings → Activity log*: your sign-in should show your own
address, not the proxy's. If it shows the proxy's, `OFFICESENTRY_TRUSTED_PROXIES` doesn't match the number
of proxies in front ([Securing the portal](../securing-the-portal.md#behind-a-reverse-proxy)).
