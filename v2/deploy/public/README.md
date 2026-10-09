# The website and public demo

The project's own Hetzner server runs three things side by side:

| Address (example) | What | Who can open it |
|---|---|---|
| `reports.yourmsp.example` | the private portal, with real tenants | only the addresses in the `PORTAL_ALLOWED_IPS` secret |
| `officesentry.co.uk` | the website: the project page and the docs ([`website/`](../../../website/README.md)) | everyone |
| `demo.officesentry.co.uk` | the public demo: three fictional clients, read-only | everyone |

The Deploy workflow sets all three up (`.github/workflows/deploy.yml`). Nothing here is needed to run
Office Sentry yourself; it's how the project shows itself.

## How the demo is kept apart from the portal

- **Its own container, network and data.** The demo is a separate service (`demo` in `compose.yml`) on its
  own Docker network, which only Caddy shares with it, so it can't reach the portal. It doesn't read `.env`,
  has no volume and never sees the portal's database, `secrets.json` or keys.
- **Fictional data in memory.** At every start, `demo.sh` makes three fictional tenants with
  `scripts/demo_data.py --public` in a memory-only folder. At 04:00 UTC the container stops, Docker starts it
  again, and the demo is new: whatever visitors did is gone and the dates are current again.
- **Read-only, and never an admin.** With `OFFICESENTRY_PUBLIC_DEMO=1` and a database made by
  `--public`, every visitor is the `demo` analyst without signing in, and anything other than reading is
  refused with a plain message (`officesentry/web/public_demo.py`). Both switches are needed, so the setting
  alone does nothing to a real portal, and an admin account called `demo` signs nobody in.
- **Limits.** The demo gets at most one CPU and 1 GB of memory, so a busy demo can't starve the portal or
  its worker. It can't collect (its tenants aren't real) and sends no email.
- **Kept out of search engines.** Every page says `noindex`.

It is the same machine, though, so a flaw that let someone break out of the demo's container would reach
the server. Moving the demo to a server of its own later is a matter of running this compose file there.

## Turning it on

The steps for the project's server, in order. Each takes a minute or two.

1. **DNS.** Point each name at the server's IPv4 address with an **A** record (proxy off, "DNS only", if
   the domain is on Cloudflare): the portal's name (`reports` under `yourmsp.example`), the website's
   (`@` and `www` under `officesentry.co.uk`) and the demo's (`demo` under `officesentry.co.uk`).
2. **GitHub variables** (the repository's *Settings → Secrets and variables → Actions → Variables*):
   - `OFFICESENTRY_DOMAIN`: the portal's name, `reports.yourmsp.example`
   - `OFFICESENTRY_WEBSITE_DOMAIN`: `officesentry.co.uk`
   - `OFFICESENTRY_DEMO_DOMAIN`: `demo.officesentry.co.uk`, and `DEMO_ENABLED`: `true`
3. **GitHub secret** (same page, *Secrets* tab): `PORTAL_ALLOWED_IPS`, the addresses that may open the
   portal, separated by commas (`203.0.113.7, 198.51.100.0/24`). Without it anyone can reach the portal's
   sign-in page (two-step sign-in still protects it). The old `DEMO_PASSWORD` secret isn't used any more;
   delete it.
4. **Microsoft Entra**, only if the portal's name changed: in the Entra admin centre, *App registrations →*
   your Office Sentry app *→ Authentication*, add the redirect URI
   `https://reports.yourmsp.example/consent/callback` (Web). Without it, consent links for new clients
   fail with `AADSTS50011`.
5. **Deploy**: *Actions → Deploy → Run workflow*, action **deploy**.
6. **Hetzner firewall**: in the Hetzner console, *Firewalls →* the server's firewall *→ Inbound rules*, let
   **TCP 80** and **TCP 443** in from **Any IPv4** and **Any IPv6**. Leave SSH (22) closed; the workflow
   opens it for itself.
7. Run the Deploy workflow once more with action **restart**, so Caddy gets the new certificates straight
   away, then open the three addresses.

## Keeping them up to date

The website and the demo follow `main` by themselves; the portal only changes when you say so.

| What changes on `main` | Website | Demo | Portal |
|---|---|---|---|
| Docs only (Markdown, `docs/`, `examples/`, `website/`) | straight away | stays | stays |
| Code, once its tests pass | updated | rebuilt from that commit | only with `AUTO_DEPLOY` set to `true` |

To update the portal, run the Deploy workflow with action **deploy** (it updates the website and the demo
from the same commit too). With the `AUTO_DEPLOY` variable set to `true`, every merge whose tests pass
updates the portal as well. Action **website-and-demo** does the automatic update by hand.

The demo builds from its own copy of the code (`/opt/officesentry/demo-src`), so it can run newer code than
the portal; the Caddy setup and this compose file come from the portal's copy, so a change to them reaches
the server with the next portal deploy.

To turn the demo off, set `DEMO_ENABLED` to `false` and deploy. To take the website down, delete the
`OFFICESENTRY_WEBSITE_DOMAIN` variable and deploy.

## Files

| File | |
|---|---|
| `compose.yml` | added to the portal's compose files on this server: the demo service, and Caddy's site list and website folder |
| `Caddyfile` | Caddy's sites: the portal (allow-listed), the website and the demo |
| `Dockerfile`, `demo.sh` | the demo image: the portal's image (built from the same code) plus the fictional data maker, and its start script |
| `build-demo.sh` | builds that image on the server from its own copy of the code; `deploy/start.sh demo` then restarts the demo alone |

To try the demo on your own computer without Docker:

```bash
cd v2
python scripts/demo_data.py --data-dir /tmp/os-public --public
OFFICESENTRY_DATA_DIR=/tmp/os-public OFFICESENTRY_PUBLIC_DEMO=1 OFFICESENTRY_INSECURE_COOKIES=1 \
  OFFICESENTRY_REQUIRE_MFA=off python -m officesentry web
```
