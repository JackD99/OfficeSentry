# Cloudflare

Three ways to use Cloudflare with Office Sentry. Pick one; they differ in what's open on your server and
where the allow-list of who may reach the portal lives.

| | DNS only | Cloudflare Tunnel | Proxied |
|---|---|---|---|
| What it is | Cloudflare only answers DNS (grey cloud) | `cloudflared` beside the portal connects out to Cloudflare | Cloudflare's proxy in front of your server (orange cloud) |
| Ports open on the server | 80 and 443 | none | 443, to Cloudflare's addresses only |
| HTTPS certificate | Caddy's, from Let's Encrypt | Cloudflare's | Cloudflare's, and an Origin certificate on Caddy |
| Who may reach the portal | your server's firewall | a Cloudflare Access policy | Cloudflare WAF or Access rules |
| `OFFICESENTRY_TRUSTED_PROXIES` | 1 | 1 | 2 |
| Good for | a cloud server (the default) | a NAS or an office server, nothing opened on the router | a cloud server that should only be reached through Cloudflare |
| Tested | Yes | Not yet | Caddy and the portal's side, yes; Cloudflare's side, not yet |

Tunnel and Proxied both send every page through Cloudflare, so set up [What Cloudflare must leave
alone](#what-cloudflare-must-leave-alone) with either.

## DNS only

The way [Hetzner, from a phone](../../DEPLOY.md#4-point-your-domain-at-it) describes: an **A** record with the
proxy off (grey cloud, "DNS only"), and Caddy makes the certificate itself. Nothing else changes.

## Cloudflare Tunnel

`cloudflared`, running next to the portal, keeps an outgoing connection to Cloudflare, and Cloudflare sends
your domain's visitors down it. No port is opened on the server or router, and the server needs no
certificate. The portal still sees each visitor's address.

1. In the Cloudflare dashboard, **Zero Trust → Networks → Tunnels → Create a tunnel**, type **Cloudflared**,
   named `officesentry`. On the next page, copy the token: the long text after `--token` in any of the
   install commands. Treat it as a password: anyone who has it can run your tunnel. Don't run the install
   command; Docker runs `cloudflared` below.
2. Add a **public hostname**: subdomain `sentry`, your domain, service type **HTTP**, URL `web:8000`.
3. Run `cloudflared`:
   - **The one-file stack** ([Portainer](portainer.md), [other managers](container-managers.md),
     [NAS](nas.md)): set `COMPOSE_PROFILES=tunnel` and `CLOUDFLARE_TUNNEL_TOKEN=<the token>` in the stack's
     environment variables.
   - **The server setup** ([Running it on your own server](../../DEPLOY.md)): add `CLOUDFLARE_TUNNEL_TOKEN=<the
     token>` to `.env` and save this as `docker-compose.override.yml` next to `docker-compose.yml`:

     ```yaml
     services:
       cloudflared:
         image: cloudflare/cloudflared:2026.9.3@sha256:072c067d25ccbe61d46e18f0d0723255f2bb5304f7317caa95b27031520ff92c
         command: ["tunnel", "run"]
         environment:
           TUNNEL_TOKEN: ${CLOUDFLARE_TUNNEL_TOKEN:?Set CLOUDFLARE_TUNNEL_TOKEN in .env}
         depends_on: [web]
         restart: unless-stopped
         read_only: true
         cap_drop: [ALL]
         security_opt: ["no-new-privileges:true"]
     ```

     then `docker compose up -d` without `--profile https`: Cloudflare provides HTTPS, so Caddy isn't needed.
     Remove the firewall's rules for ports 80 and 443.
   - **Kubernetes:** run `cloudflared` as its own Deployment, as Cloudflare's documentation describes, with
     the service URL `http://officesentry.officesentry.svc.cluster.local:8000`.
4. In Office Sentry's settings, `OFFICESENTRY_BASE_URL` is `https://` and the tunnel's hostname, and
   `OFFICESENTRY_TRUSTED_PROXIES` is 1.
5. The tunnel's dashboard shows it as **Healthy**. Open the hostname, sign in, and check *Settings → Activity
   log* shows your own address.

### Who may reach it: Cloudflare Access

Without a firewall in front, anyone can reach the sign-in page through the tunnel. To keep the allow-list,
put Cloudflare Access in front: **Zero Trust → Access → Applications → Add an application → Self-hosted**,
for the tunnel's hostname, with a policy allowing your team (emails ending in your domain, your office
addresses, or both).

Some people reach the portal without being on that list, so add a second self-hosted application with a
**Bypass** policy for **Everyone**, covering these paths on the same hostname:

| Path | Who needs it |
|---|---|
| `/consent/callback` | a client's administrator, back from approving access on Microsoft's consent page ([Onboarding a client](../onboarding-a-client.md)) |
| `/static/`, `/brand.css`, `/brand/logo` | that page's styles and your logo (the same files as the sign-in page; nothing private) |
| `/health/ready` | an uptime monitor outside your network ([Hear about problems without signing in](../../DEPLOY.md#hear-about-problems-without-signing-in)) |

Client users who sign in to the portal to see their own reports must pass the Access policy too: add their
addresses to it (Access's one-time PIN by email works for any address), or leave Access off and rely on the
portal's own sign-in protection ([Sign-in protection](../../DEPLOY.md#sign-in-protection)).

## Proxied

Cloudflare's proxy (orange cloud) in front of the server's Caddy, for its firewall and DDoS protection. It
needs three changes to the [server setup](../../DEPLOY.md), because by default Caddy ignores the visitor's
address that Cloudflare passes on (and every visitor would then look like Caddy itself). Your own nginx or
Traefik instead of Caddy? [Behind a reverse proxy](../securing-the-portal.md#behind-a-reverse-proxy) says what
each needs; the firewall and the rest below are the same.

1. **A certificate for the connection from Cloudflare.** In Cloudflare: **SSL/TLS → Overview**, mode
   **Full (strict)**; then **SSL/TLS → Origin Server → Create Certificate** for your hostname. Save the two
   parts on the server, in the folder with `docker-compose.yml`, as `origin.pem` and `origin.key`, owned by
   root and readable by root only:

   ```bash
   sudo chown root:root origin.pem origin.key && sudo chmod 600 origin.key
   ```

2. **A Caddyfile that trusts Cloudflare's addresses.** Save this as `Caddyfile.cloudflare` next to the other
   files. The addresses are Cloudflare's published ranges ([cloudflare.com/ips](https://www.cloudflare.com/ips/));
   if they ever change, update the line and restart Caddy.

   ```text
   {
   	servers {
   		trusted_proxies static 173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18 190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13 104.24.0.0/14 172.64.0.0/13 131.0.72.0/22 2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32 2405:8100::/32 2a06:98c0::/29 2c0f:f248::/32
   	}
   }

   {$SITE_ADDRESS} {
   	tls /etc/caddy/origin.pem /etc/caddy/origin.key
   	encode gzip
   	header -Server
   	reverse_proxy web:8000
   }
   ```

   and use it, with the certificate, through a `docker-compose.override.yml`:

   ```yaml
   services:
     caddy:
       volumes:
         - ./Caddyfile.cloudflare:/etc/caddy/Caddyfile:ro
         - ./origin.pem:/etc/caddy/origin.pem:ro
         - ./origin.key:/etc/caddy/origin.key:ro
   ```

3. **Two proxies.** Set `OFFICESENTRY_TRUSTED_PROXIES=2` in `.env` (Cloudflare, then Caddy), and run
   `docker compose --profile https up -d`.

Then, in Cloudflare, turn the DNS record's proxy on (orange cloud). On the server's firewall, allow 443 from
Cloudflare's addresses only (the same list), and close 80: otherwise anyone who finds the server's address can
skip Cloudflare. Your own allow-list moves to Cloudflare: a **WAF custom rule** that blocks requests to the
hostname from addresses outside your list, except the paths in the [Access](#who-may-reach-it-cloudflare-access)
table above, or Cloudflare Access itself.

Sign in and check *Settings → Activity log*: your own address means it works. Caddy's address (or a
Cloudflare one) means `OFFICESENTRY_TRUSTED_PROXIES` or the Caddyfile isn't in place.

## What Cloudflare must leave alone

With the Tunnel or Proxied, add a **Configuration Rule** (**Rules → Configuration Rules**) for the portal's
hostname that turns off **Email Obfuscation** and **Rocket Loader**. Both rewrite pages and add a script of
Cloudflare's; the portal's Content-Security-Policy blocks scripts that aren't its own, so with Email
Obfuscation on, the addresses in reports would show as `[email protected]`. Also leave caching off for the
hostname (Cloudflare doesn't cache the portal's pages by default, and the portal marks every page
`no-store`), and don't add rules that rewrite the portal's headers.

Cloudflare waits 100 seconds for an answer; the slowest PDFs take about a minute, so that's enough.
