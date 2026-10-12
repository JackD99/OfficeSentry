# Securing the portal

What Office Sentry does by itself to protect the portal, what the person
running it has to do, and how to put it behind a reverse proxy. The security
model and how to report a vulnerability are in
[SECURITY.md](../../SECURITY.md); the full install guide is
[Running it on your own server](../DEPLOY.md).

Office Sentry holds the certificate that reads every client tenant, so the
portal is the thing to protect. Treat its server like a domain controller:
only the people who run it get a shell, and nothing else runs on it.

## What it does by itself

The defaults are the secure ones. Nothing below needs switching on.

| | What |
|---|---|
| Sign-in | Passwords hashed with scrypt; 12 characters at least, and the 20,000 most common long passwords, the username and the product name are refused. Two-step codes from an authenticator app, required for everyone on a new install. Wrong passwords slow down the address they come from, never lock the account ([Sign-in protection](../DEPLOY.md#sign-in-protection)). Once an admin sets it up, staff can sign in with Microsoft instead, under your own Conditional Access and MFA ([Staff sign-in with Microsoft](staff-sign-in.md)). |
| Sessions | Kept on the server: the cookie holds a random token. Two hours idle and eight in total, ended on sign-out and on any change to the account. `__Host-` cookie, HttpOnly, SameSite, Secure over HTTPS. |
| Who sees what | Every page and download checks on the server what the signed-in person's role and capabilities allow, and on which clients. Clients see their own organisation only. Certificates, private keys and the encryption key can't be downloaded by anyone. |
| Forms | Every form and autosave carries an anti-forgery token. A missing or stale one is refused, explained, and counted in the activity log. |
| Browser headers | A nonce-based Content-Security-Policy with no external scripts, styles or fonts, HSTS over HTTPS, `X-Frame-Options: DENY`, nosniff, `Referrer-Policy: same-origin`, a Permissions-Policy, cross-origin opener and resource policies of `same-origin`, `X-Permitted-Cross-Domain-Policies: none`, and `Cache-Control: no-store` on every page and download. |
| Uploads | Requests over 1 MB are refused, which covers the only uploads: a logo and a tenant list to import. |
| Activity log | Every sign-in, refused page, change and download, with the address it came from. Admins read and export it under *Settings → Activity log*. |
| Secrets | The session key and the encryption key for stored certificates, two-step secrets and mail credentials are generated into `secrets.json` (mode `0600`) in a volume of its own, apart from the data and its backups. |
| Error pages | The portal's own pages, with no stack traces, versions or server names. |
| Containers | The portal and worker run read-only, as a non-root user, with every Linux capability dropped, `no-new-privileges` and a process limit. The compose file binds the portal to `127.0.0.1` only. |
| Logs | The access log records the path, status and timing of each request, never the query string or the referer. |
| PDFs | Rendered on the server from the app's own templates, with a fetcher that loads only the app's static files: no other files, no network. |
| Microsoft 365 | Read-only Graph permissions and `Get-` Exchange cmdlets only; the certificate's private key never leaves the server. [What Office Sentry reads](what-office-sentry-reads.md) lists every permission. |

## What you have to do

- **HTTPS only.** Run it behind a TLS-terminating reverse proxy (the next
  section). Cookies are marked `Secure`, so signing in over plain HTTP doesn't
  work, by design. `OFFICESENTRY_INSECURE_COOKIES=1` is for development on a
  laptop and nothing else.
- **Tell it how many proxies are in front** with `OFFICESENTRY_TRUSTED_PROXIES`
  (1 for the bundled Caddy or one proxy of your own). With it at `0`, every
  visitor appears to come from the proxy and shares its sign-in limits. Never
  set it higher than the number of proxies you really have, or visitors could
  pick their own address.
- **Keep the firewall to 80 and 443** (and SSH from your own addresses). The
  portal's own port 8000 is bound to the loopback interface, so a proxy on the
  same host reaches it and nothing else does. Don't change the bind address to
  put a proxy on another host without a private network between them.
- **Back up, and keep the keys apart.** Nightly backups go in the data volume;
  copy them off the server encrypted (`DEPLOY.md`, "Off-site backups") and run
  the restore drill. Keep a copy of `secrets.json` somewhere safe that isn't
  with the backups: without the encryption key the stored certificate can't be
  read and must be replaced.
- **Update.** Each release's notes say what changed; a line under *Security* in
  the changelog means update soon. The image is rebuilt with current base
  packages on every release.
- **Watch the activity log** now and then, or send its sign-in failures to a
  webhook ([Hear about problems without signing in](../DEPLOY.md#hear-about-problems-without-signing-in)).
  Refused pages (someone opening a tenant they haven't been granted) and
  stale form tokens are counted per person and hour.
- **Give out the least.** A role is a set of capabilities and a scope.
  Administrator and Team leader see every client. Second line, First line,
  Account manager and Auditor see only the clients and client groups granted
  to them, unless *Every client* is ticked on their user page. A Client viewer
  sees their own organisation only. Pick the smallest role that does the job
  (they're described under *Settings → Users → Roles*). For one person, add or
  take away single capabilities with *Customise* on their user page rather than
  giving them a bigger role; taking away always wins. Grant a client group to
  someone who looks after several clients rather than all of them. Remove
  accounts the day someone leaves: disabling one ends their sessions at once.
- **Limited roles stay limited.** A custom role that sees only some clients
  can still manage clients, users or the activity log, but only within those
  clients: its holders don't see other clients' names, accounts or activity,
  and can't change client groups or a role held by someone who sees or does
  more. *Settings* and *System status* cover every client, so they only work
  for someone who sees every client.
- **A report area taken away stays away.** Someone who can't open a report
  area on a client (a role without it, or taken away with *Customise*) can't
  schedule it by email, download it from the report history or see it in a
  report design, and the home pages leave it out of their counts. Every report
  email opens with the client's security summary, so setting up, changing or
  previewing one needs *Security findings and reviews* on that client.

## Behind a reverse proxy

The portal expects a proxy in front of it that ends TLS and passes two headers:
`X-Forwarded-For` with the visitor's address and `X-Forwarded-Proto` with
`https`. With `OFFICESENTRY_TRUSTED_PROXIES=1` the portal trusts the last hop
of those headers and ignores anything the visitor put there themselves.
The portal always runs at the root of its address, so it never takes a path
prefix from `X-Forwarded-Prefix`.

**Caddy** (the bundled `Caddyfile`, started with `--profile https`) does all
of this by itself, including the certificate. Nothing to configure beyond
`OFFICESENTRY_DOMAIN` in `.env`.

**nginx**:

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name sentry.yourmsp.com;
    ssl_certificate     /etc/letsencrypt/live/sentry.yourmsp.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/sentry.yourmsp.com/privkey.pem;
    server_tokens off;
    client_max_body_size 2m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;   # a long PDF can take a minute
    }
}
server {
    listen 80;
    server_name sentry.yourmsp.com;
    return 301 https://$host$request_uri;
}
```

Use `$remote_addr`, not `$proxy_add_x_forwarded_for`: the portal only wants the
address the proxy itself saw, and appending to a header the visitor sent lets
them choose what's logged.

**Traefik** passes both headers by default. Set `forwardedHeaders.insecure`
off (the default) and `trustedIPs` only to proxies you run, and give the
router a `websecure` entry point with a certificate resolver. No middleware is
needed for the headers.

**Cloudflare or another CDN in front of your proxy.** Each of these proxies
replaces the address the CDN passes on with the CDN's own, so every visitor
would look like the CDN until the proxy is told to trust the CDN's published
address ranges:

- **Caddy:** a `trusted_proxies static <ranges>` line in the Caddyfile's global
  `servers` block, and `OFFICESENTRY_TRUSTED_PROXIES=2` (the CDN, then Caddy).
  [Cloudflare](deploy/cloudflare.md#proxied) has the whole Caddyfile.
- **nginx:** the real IP module, so `$remote_addr` above becomes the visitor's
  address: `set_real_ip_from <range>;` once per range, `real_ip_header
  X-Forwarded-For;` and `real_ip_recursive on;`. `OFFICESENTRY_TRUSTED_PROXIES`
  stays 1.
- **Traefik:** the ranges in the entry point's `forwardedHeaders.trustedIPs`,
  and `OFFICESENTRY_TRUSTED_PROXIES=2`.

Then make the firewall accept 443 from the CDN's addresses only, or anyone
who finds the server's address can skip the CDN. The portal sets its own
security headers and `Cache-Control: no-store`, so leave the CDN's caching
and header rewriting off for this host; on Cloudflare, also Email Obfuscation
and Rocket Loader ([What Cloudflare must leave
alone](deploy/cloudflare.md#what-cloudflare-must-leave-alone)). A Cloudflare
Tunnel connects to the portal directly and needs none of this: 1 is right
there ([Cloudflare Tunnel](deploy/cloudflare.md#cloudflare-tunnel)).

**Checking it worked.** Sign in, then look at *Settings → Activity log*: the
sign-in's address should be yours, not `127.0.0.1` or the proxy's. The portal
also logs a warning the first time a forwarded header arrives while
`OFFICESENTRY_TRUSTED_PROXIES` is `0`. In the browser's developer tools, the response
should carry `Strict-Transport-Security`; if it doesn't, the proxy isn't
passing `X-Forwarded-Proto: https` and the portal thinks it's on plain HTTP.

## Without Docker

[Running on Windows without Docker](../DEPLOY.md#running-on-windows-without-docker)
covers the service. The same rules apply: a reverse proxy with TLS in front,
`OFFICESENTRY_TRUSTED_PROXIES` set, the data folder and `secrets.json`
readable by the service account only, and the port the portal listens on
reachable from the proxy alone.
