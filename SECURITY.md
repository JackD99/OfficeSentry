# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately. **Don't open a public issue.**

Use GitHub's [private vulnerability reporting](https://github.com/JackD99/OfficeSentry/security/advisories/new) and include:

1. What the vulnerability is
2. Steps to reproduce it
3. The likely impact
4. A suggested fix, if you have one

We aim to acknowledge reports within 48 hours and give an initial assessment within 7 days.

## Security model

Office Sentry reads data from many client tenants, so it is built to do as
little as possible with that access. Details and code references are in
[v2/README.md](v2/README.md) and [v2/CLAUDE.md](v2/CLAUDE.md).

### Access to Microsoft 365

- **Read-only by construction.** One multi-tenant Entra app requests only
  read Graph permissions (`*.Read*`) plus `Exchange.ManageAsApp`. The Graph
  client has no write methods, and the Exchange client only runs an
  allow-list of `Get-` cmdlets.
- **Certificate sign-in, no stored user passwords.** The app signs in with a
  certificate. Its private key is encrypted in the database and is never
  written to disk as a file or sent to the browser. Only the public `.cer`
  can be downloaded, by admins.
- **Signed consent.** The admin-consent callback is signed, expires, and
  checks that the returning tenant is the one that was invited.

### The portal

| Control | Implementation |
|---|---|
| Tenant isolation | Deny by default. Users see only tenants granted to them; others return 404. One gate (`web/access.py`) loads every tenant. |
| Roles | Admin, analyst, client. Clients see only their own reports. |
| Sessions | 8-hour lifetime, HttpOnly, SameSite=Lax, Secure over HTTPS. Ended on disable, delete, role change or password change. |
| Login | 5 failed attempts lock the account for 15 minutes. Same-site redirects only. |
| CSRF | Flask-WTF on every form. |
| Headers | Nonce-based CSP with no external assets, HSTS, X-Frame-Options DENY, nosniff, Referrer-Policy, Permissions-Policy, `Cache-Control: no-store`. |
| Secrets | Session key and encryption key are separate. Encryption keys can be rotated (`python -m officesentry rotate-keys`). Generated secrets are written with mode `0600`. |

### What should never be committed

- Certificates and keys: `*.pfx`, `*.cer`, `*.pem`, `*.key`, `*.p12`
- `.env` files and any credential, key or token
- The data directory (`data/`, or the `/data` Docker volume): it holds the
  database, encrypted keys and tenant snapshots
- Report exports: `*.csv`, `*.xlsx`

## Deployment

- Run it with Docker (`v2/docker-compose.yml`) behind a TLS-terminating
  reverse proxy such as Caddy, nginx or Traefik. The compose file only binds
  to `127.0.0.1`.
- Only use `OFFICESENTRY_INSECURE_COOKIES=1` for local development over HTTP.
- Back up the data volume, including `secrets.json`. Losing the encryption
  key means the stored certificate can't be decrypted and must be replaced.
- Review the Entra app's permissions and sign-in logs in your own tenant
  from time to time.

## Known limitations

- SQLite suits one server; it isn't built for several web servers sharing a database.
- Portal sign-in is username and password. There's no MFA or SSO for the portal yet, so keep it behind network controls.
