# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately. **Don't open a public issue.**

Use GitHub's [private vulnerability reporting](https://github.com/JackD99/OfficeSentry/security/advisories/new) and include:

1. What the vulnerability is
2. Steps to reproduce it
3. The likely impact
4. A suggested fix, if you have one

If the private reporting form isn't available, email hello@officesentry.co.uk
with "Security" in the subject and no details. We'll reply with a private way
to send them.

We aim to acknowledge reports within 48 hours and give an initial assessment within 7 days.

## Supported versions

Security fixes go into the latest release only. Run the newest `2.x`
release (the version is at the foot of the portal's menu and in `/health`);
older releases and the removed v1 portal get no fixes.

## Security model

Office Sentry reads data from many client tenants, so it is built to do as
little as possible with that access. Details and code references are in
[v2/README.md](v2/README.md), [v2/DEPLOY.md](v2/DEPLOY.md) and
[v2/CLAUDE.md](v2/CLAUDE.md).

### Access to Microsoft 365

- **Read-only by construction.** One multi-tenant Entra app requests only
  read Graph permissions (`*.Read*`) plus `Exchange.ManageAsApp`. The Graph
  client has no write methods, and the Exchange client only runs an
  allow-list of `Get-` cmdlets.
- **Certificate sign-in, no stored user passwords.** The app signs in with a
  certificate. Its private key is encrypted in the database and is never
  written to disk as a file or sent to the browser. Only the public `.cer`
  can be downloaded, by admins. Certificates last two years; admins are
  warned on every page, and the MSP's team by email and the alert webhook,
  60, 30 and 7 days before one expires, and it can be replaced without a gap
  (see [If a certificate leaks](#if-a-certificate-or-key-leaks)).
- **Extra access is shown, not hidden.** Each tenant's connection check
  lists any permission the tenant granted beyond the read-only set, and any
  admin role besides Global Reader. Both are warnings on the tenant's Manage
  page and the Client tenants list.
- **Tested both ways.** `tests/test_no_writes.py` fails if an HTTP call
  other than GET appears outside a short list of named exceptions (the mail
  app, Exchange's `Get-` cmdlets, creating the app in the MSP's own tenant,
  and alert webhooks), and runs a full collection to check every request is
  a read. `tests/test_isolation_walk.py` opens every tenant-scoped route with
  another tenant's IDs, as an analyst and as a client, and expects a refusal.
- **The mail app is separate and can be fenced in.** Email through
  Microsoft 365 uses its own single-tenant app with `Mail.Send` in the MSP's
  tenant, signing in with a certificate (or a client secret). The settings
  page asks the admin to limit it to the one sending mailbox and shows a
  warning until they confirm it, because without that limit a leaked
  credential could send as anyone in the MSP's company.
- **Bound consent links.** The admin-consent link's signed state carries a
  one-time nonce, stored server-side with the admin and the signed-in
  browser that opened it, and expires after an hour. A tenant added by
  domain only takes the tenant ID that comes back with the consent if
  Microsoft's public sign-in metadata gives the same ID for that domain; a
  tenant that already has its ID never changes it on consent.

### The portal

| Control | Implementation |
|---|---|
| Tenant isolation | Deny by default. Users see only tenants granted to them; others return 404. One gate (`web/access.py`) loads every tenant. |
| Roles | Admin, analyst, client. Clients see only their own tenant's reports. |
| Two-step sign-in | Authenticator-app codes (TOTP) with ten one-time recovery codes. New installs require it for everyone; installs from before October 2026 keep requiring it for admins and analysts until `OFFICESENTRY_REQUIRE_MFA=all` is set (the Users page recommends it). A code or recovery code works once, even when sent twice at the same moment. Recovery codes are stored as an HMAC under a random key that is itself encrypted with the encryption key, so a copy of the database alone can't be used to guess them. Adding an authenticator from the account page, turning it off and replacing recovery codes ask for the password, and an admin resetting someone else's password, two-step sign-in or sessions gives their own password too. |
| Passwords | At least 12 characters, not one of the 20,000 most commonly used passwords of that length (from the public list of 10 million leaked passwords), and not containing the username or the product's name; scrypt hash (Werkzeug). A password an admin set (new user or reset) has to be replaced at the next sign-in. |
| Sign-in limits | Wrong passwords never lock an account. They slow down the address they come from: 5 per username or 20 in all per 15 minutes, counted before the password is checked so parallel requests can't get past them. A spread-out attack on one account slows only addresses that haven't signed in to it before. Five wrong two-step codes lock that step for 15 minutes. The client address comes from `X-Forwarded-For` only behind `OFFICESENTRY_TRUSTED_PROXIES`. |
| Sessions | Server-side: the cookie holds a random token, stored as a hash. 2 hours idle and 8 hours in total; ended on sign-out, disable, delete, role, password or two-step change. `__Host-` cookie, HttpOnly, SameSite=Lax, Secure over HTTPS. Same-site redirects only. |
| CSRF | Flask-WTF on every form. |
| Headers | Nonce-based CSP with no external assets, HSTS, X-Frame-Options DENY, nosniff, Referrer-Policy, Permissions-Policy, `Cache-Control: no-store`. |
| Activity log | Sign-ins and every change, with the client address; admins read, filter and export it under *Settings → Activity log*. Failed sign-ins with unknown usernames are counted per address and hour without storing what was typed, and sign-in noise is removed after 90 days. |
| Secrets | Session key and encryption key are separate. `python -m officesentry rotate-keys` re-encrypts every encrypted value (certificate keys, two-step secrets and the recovery code key, mail credentials, the alert webhook) and refuses if it finds one it doesn't know. Generated secrets are written with mode `0600`. |
| PDFs | Rendered by WeasyPrint with a URL fetcher that only loads the app's own static files and `data:` URIs: no other files, no network. |
| Outbound requests | Webhooks and MTA-STS fetches are HTTPS only, refuse internal addresses and connect to the address that was checked (no DNS rebinding). The SMTP password is never sent without TLS, except to a relay on the same machine. |

### Data retention

Admins choose how long personal data is kept under *Settings → Data
retention*: snapshots, sign-in detail (IP addresses and locations), resolved
findings, run logs, the activity log and issued reports. Defaults keep
everything as before (sign-in detail lasts as long as monthly snapshots,
about 13 months; findings, the activity log and issued reports are kept
forever), so set shorter periods to match your own policy. Neither the
activity log nor issued reports can be set below 365 days. The
worker deletes once a day in small batches with `secure_delete`; changes to
the settings are audited. Details in `v2/DEPLOY.md`.

### Offboarding a client

Deleting a tenant removes everything stored for it in one transaction and
checks nothing tenant-scoped is left. Activity log entries keep who did what
and when, but details naming the client's people are removed, and client
accounts deleted with it are anonymised. Deleted rows are overwritten in the
database file (`secure_delete`), which is then compacted and its write-ahead
log emptied. **Backups made before the delete keep the data until they rotate
out** (the last 7 by default), as do copies taken off the server.

### What should never be committed

- Certificates and keys: `*.pfx`, `*.cer`, `*.pem`, `*.key`, `*.p12`
- `.env` files and any credential, key or token
- The data directory (`data/`, or the `/data` Docker volume): it holds the
  database, encrypted keys and tenant snapshots
- Report exports: `*.csv`, `*.xlsx`

## If a certificate or key leaks

One certificate signs in to every client tenant of its cloud, so treat a
leak of its private key as urgent. It can leak with the database together
with `secrets.json` (a lost backup, say), or with access to the server.
Rotating the encryption key alone (`rotate-keys --new`) does **not** help:
whoever has the key can still sign in as the app until Entra stops trusting
the certificate.

1. **Make a new certificate.** *Settings → App connection*, open the
   connection and press *Replace certificate*. Download the new `.cer`.
2. **Add it to the app registration.** In your own tenant's Entra admin
   centre: *App registrations →* the Office Sentry app *→ Certificates &
   secrets → Certificates → Upload certificate*. Collection keeps working on
   the old certificate meanwhile.
3. **Test it and switch.** Back in Office Sentry press *Test new certificate*
   (it signs in and reads the tenant's name, nothing else), then *Switch to
   the new certificate*.
4. **Delete the old certificate from the app registration**, straight away.
   Its thumbprint is shown on the App connection page until you confirm with
   *I've removed it*. From this moment the leaked key can't sign in.
5. **Look for misuse.** In each client tenant's Entra sign-in logs, filter
   *Service principal sign-ins* by the app's ID for sign-ins from addresses
   other than your server's. The app is read-only, so the risk is data being
   read, not changed; tell affected clients if you find any.
6. **Then rotate the encryption key** if the database or a backup leaked:
   `rotate-keys --new` (see `v2/DEPLOY.md`), and treat the mail app's
   certificate or secret and the alert webhook URL as leaked too (make a new
   mail certificate under *Settings → Email delivery*, and a new webhook URL
   in Teams or Slack).

If you can't wait for the steps above, delete the certificate from the app
registration first: collection stops for every client until a new one is in
place, but nobody can sign in with the old one.

## Deployment

- Run it with Docker (`v2/docker-compose.yml`) behind a TLS-terminating
  reverse proxy such as Caddy, nginx or Traefik. The compose file only binds
  to `127.0.0.1`. Set `OFFICESENTRY_TRUSTED_PROXIES` to the number of proxies
  in front (1 for the bundled Caddy) so sign-in limits and the activity log
  see real client addresses.
- Only use `OFFICESENTRY_INSECURE_COOKIES=1` for local development over HTTP.
- Back up the database, ideally encrypted and off the server (restic to
  S3-compatible storage, `v2/DEPLOY.md`, "Off-site backups"), and run the
  restore drill. Keep a copy of `secrets.json` apart from the backups, in a
  password manager say: losing the encryption key means the stored
  certificate can't be decrypted and must be replaced. The Docker setup keeps
  `secrets.json` in its own volume (`/keys`), so the data volume and its
  backups never hold it.
- Review the Entra app's permissions and sign-in logs in your own tenant
  from time to time.

## Known limitations

- SQLite suits one server; it isn't built for several web servers sharing a database.
- Portal sign-in is username, password and two-step code. There's no SSO
  (Entra ID sign-in) for the portal yet.
- New users get a password from an admin, shared out of band. Whoever uses it
  first must replace it, so a leaked one is noticed (the real user can't sign
  in), but one-time invitation links would be better.
