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
Please give us time to release a fix before you talk about the problem in public; we'll agree a
date with you. The fix's GitHub security advisory says what was wrong and, if you'd like, credits you.

**In scope:** the code in this repository, the published images at `ghcr.io/jackd99/officesentry`, and the
public demo. For example: seeing a client you weren't given, reaching a secret or private key, changing
anything in a Microsoft 365 tenant, getting round sign-in or two-step sign-in, or changing anything on
the demo.

**Not in scope:** how one installation is set up (its server, its proxy, or a setting the docs warn
against), problems in Microsoft 365 itself (report those to Microsoft), and the demo's data being readable
by anyone: it's made up, and meant to be seen.

## Supported versions

Security fixes go into the latest release only. Run the newest `2.x`
release (the version is at the foot of the portal's menu and in `/health`);
older releases and the removed v1 portal get no fixes.

## Security model

Office Sentry reads data from many clients, so it is built to do as
little as possible with that access. Details and code references are in
[v2/README.md](v2/README.md), [v2/DEPLOY.md](v2/DEPLOY.md) and
[v2/CLAUDE.md](v2/CLAUDE.md).

### How data flows

```text
 Each client's Microsoft 365 tenant
 Microsoft Graph, Exchange Online's admin API and
 SharePoint's REST API (and the Management Activity
 feed, for clients on the audit log add-on)
      │
      │ HTTPS, signed in as your app with its
      │ certificate. Reads only: GET requests and
      │ Exchange Get- commands.
      ▼
 ┌─ Your server ─────────────────────────────────┐
 │ worker: collects every night, sends report    │
 │ emails and alerts, applies Data retention     │
 │   │ writes                                    │
 │   ▼                                           │
 │ data volume (/data): the database (snapshots, │
 │ findings, users, settings, and certificate    │
 │ keys, encrypted), activity history, logs,     │
 │ nightly backups                               │
 │   ▲ reads                                     │
 │   │                                           │
 │ portal: pages, PDFs and spreadsheets, from    │
 │ stored data only                              │
 │                                               │
 │ keys volume (/keys): secrets.json, the keys   │
 │ that decrypt those certificate keys; never in │
 │ /data or a backup                             │
 └──┬───────────────────────────┬────────────────┘
    ▲                           │
    │ HTTPS through Caddy       │ Out: report emails,
    │ or your own proxy         │ alert webhooks,
    │                           ▼ off-site backups
 Your staff and your              (encrypted by restic)
 clients' people
```

Only the worker reads a client's data from Microsoft; no page does. The portal talks to Microsoft to
sign people in, for setup steps an admin starts (creating or updating the app, testing a new
certificate) and to send an email someone sends from a page. Every permission the app asks for, why, and which reports use
it, is in [What Office Sentry reads](v2/docs/what-office-sentry-reads.md).

| What | Where it's kept | How it's protected |
|---|---|---|
| Collected data: snapshots, findings, reports sent to clients, users and settings | The database, `/data/officesentry.db` | Compressed, not encrypted by Office Sentry. Protect the server and its disk, and encrypt the disk if your policy asks for encryption at rest. |
| Sign-in and directory audit history | A file per month beside the database, `/data/events` | As the database. |
| The app certificates' private keys, the sign-in app's key, two-step secrets, mail passwords and secrets, the alert webhook and its signing secret | In the database | Encrypted with the encryption key (Fernet: AES with an integrity check). Never written to disk as a file, never sent to a browser; only an app's public `.cer` can be downloaded. |
| Passwords, recovery codes, session tokens | In the database | Only as hashes: scrypt for passwords, an HMAC under an encrypted key for recovery codes. |
| Session key and encryption key | `secrets.json`, in its own volume (`/keys`), readable only by Office Sentry | Never in the data volume or a backup. Keep a copy apart, in a password manager say. |
| Nightly backups | `/data/backups`, or a folder you give it | Compressed. They hold the encrypted keys but not the key that decrypts them. |
| Off-site backups (optional) | S3-compatible storage you choose | Encrypted by restic with your password before they leave the server. |

[v2/DEPLOY.md](v2/DEPLOY.md#backups) covers backups, restoring, moving to a new server and rotating
keys.

### Access to Microsoft 365

- **Read-only by construction.** One multi-tenant Entra app requests only
  read permissions: Graph's (`*.Read*`), `Exchange.ManageAsApp`, and
  `Sites.Read.All` on SharePoint Online's own API. The Graph client has no
  write methods, and the Exchange client only runs an allow-list of `Get-`
  cmdlets.
- **Certificate sign-in, no stored user passwords.** The app signs in with a
  certificate. Its private key is encrypted in the database and is never
  written to disk as a file or sent to the browser. Only the public `.cer`
  can be downloaded, by admins. Certificates last two years; admins are
  warned on every page, and the MSP's team by email and the alert webhook,
  60, 30 and 7 days before one expires, and it can be replaced without a gap
  (see [If a certificate leaks](#if-a-certificate-or-key-leaks)).
- **Extra access is shown, not hidden.** Each client's connection check
  lists any permission the tenant granted beyond the read-only set, and any
  admin role besides Global Reader. Both are warnings on the client's Manage
  page and the Client settings list.
- **Tested both ways.** `tests/test_no_writes.py` fails if an HTTP call
  other than GET appears outside a short list of named exceptions, and runs
  a full collection to check every request is a read. The exceptions are the
  mail app, Exchange's `Get-` cmdlets, alert webhooks, the live test's
  recording (which passes on a GET), the updater's calls to the server's own
  Docker engine, and three that only touch the MSP's own tenant: creating the
  app, adding a release's new permissions to it, and sign-in with Microsoft
  (its token request, creating its sign-in app, and letting that app accept
  other organisations' accounts for client sign-in).
  The audit log add-on adds one more: starting and stopping a client's audit feed (below).
  `tests/test_isolation_walk.py` opens every tenant-scoped route with
  another client's IDs, as staff and as a client, and expects a refusal.
  `tests/test_roles.py` opens every route as every built-in role and expects
  a refusal exactly where the role lacks what the page needs.
- **Client text stays text.** `tests/test_injection_walk.py` ends every name, title and file name a client's tenant sends with markup, opens every page, report and PDF, and fails if any of them shows it unescaped.
- **The mail app is separate and can be fenced in.** Email through
  Microsoft 365 uses its own single-tenant app with `Mail.Send` in the MSP's
  tenant, signing in with a certificate (or a client secret). The settings
  page asks the admin to limit it to the one sending mailbox and shows a
  warning until they confirm it, because without that limit a leaked
  credential could send as anyone in the MSP's company.
- **The audit log add-on is its own app, with one permission.** It's off until an admin turns it on, and each client approves it separately. Its app has only `ActivityFeed.Read` on the Office 365 Management APIs and its own certificate, and Office Sentry keeps only a fixed list of file, sharing and mailbox events from it, without email subjects. Its only requests other than GET start and stop a client's audit feed: they tell Microsoft whether to make that client's audit records available to the add-on, and change nothing in the tenant.
- **Bound consent links.** The admin-consent link's signed state carries a
  one-time nonce, stored server-side with the admin and the signed-in
  browser that opened it, and expires after an hour. A client added by
  domain only takes the tenant ID that comes back with the consent if
  Microsoft's public sign-in metadata gives the same ID for that domain; a
  client that already has its tenant ID never changes it on consent.

### The portal

| Control | Implementation |
|---|---|
| Tenant isolation | Deny by default. Users see only clients granted to them; others return 404. One gate (`web/access.py`) loads every client. |
| Roles | A role is a set of capabilities and a scope (every client, or those granted). Seven built-in roles (Administrator, Team leader, Second line, First line, Account manager, Auditor, Client viewer), custom roles, and per-person capabilities added or taken away, everywhere or on one client; taking away wins. Every page and download checks on the server. Someone who manages users without being an Administrator can only give out what they hold on every client they see, can't change an account, or a role held by one, that sees or does more, and isn't shown client accounts of other clients. A role limited to some clients manages clients, users and the activity log within those clients only; client groups, Settings, System status and installing updates need every client. A report area someone can't open on a client stays closed wherever that client's data goes: scheduled emails and their previews, the report history, custom reports, the home pages and What changed. Clients see only their own organisation's reports. |
| Client contacts and alerts | New contacts get a client's report plan emails, so only someone who can open everything the plan sends adds a contact. New-finding alerts name the findings, so choosing who gets them needs security findings. |
| Approved reports | Once a month's report is approved, its client sees that approved copy. Commentary, recommendations and notes changed afterwards reach them only with a new version. |
| Sign in with Microsoft | Optional, off until an admin sets it up. OpenID Connect against a single-tenant app in the MSP's own tenant with no API permissions, authorization code with PKCE, a certificate-signed client assertion (`private_key_jwt`; the private key is encrypted in the database) and the ID token verified against the tenant's published keys: signature, audience, issuer, tenant, lifetime and nonce. Roles are app roles assigned in Entra (one per built-in role, and values an admin gives custom roles) and read at every sign-in; client access is still granted in Office Sentry. Passwords stay as the break-glass route until an admin signed in with Microsoft turns them off; `python -m officesentry staff-sign-in --passwords on` turns them back on from the server. An account with no password confirms a security change by signing in to Microsoft again (`prompt=login`, the same Microsoft account), which counts for ten minutes on that session. |
| Client sign-in with Microsoft | Optional, off until an admin turns it on. The same sign-in app accepts other organisations' accounts (`AzureADMultipleOrgs`; changed in the MSP's tenant only, by device sign-in with `Application.ReadWrite.All` or by hand), and the button signs in through `organizations`. The ID token's issuer must be the tenant in its own `tid`. A token from the MSP's tenant takes the staff path above; any other tenant can only reach an existing client account, matched on the token's `tid` and `oid` together and never on `preferred_username` or `email`, and only when the account is granted (directly, never through a group) a client whose Directory ID is that `tid`, in the sign-in app's cloud and not archived. Staff connect a client's person to their object ID when they add them, from the client's directory as Office Sentry last collected it (members only, not guests). Accounts are never created this way, a client's account never becomes staff (nor a staff account a client's), the same object ID from another tenant is refused, and app roles in a client's token are ignored. A client account connected this way only ever sees clients whose Directory ID is its own tenant's, whatever it is granted; disconnecting it removes its grants of clients in any other organisation, and staff limited to some clients can't change an account that holds a client outside them, even one it can't see now. Microsoft sign-in, staff or client, doesn't ask for Office Sentry's own two-step code, whatever `OFFICESENTRY_REQUIRE_MFA` says; it relies on Entra's MFA and Conditional Access. Refusals don't say whether an account exists, and strangers' sign-ins are counted per address without their names. A client's administrator can approve the sign-in scopes (`openid profile email`, no API permissions) from a signed link; its public return page records nothing for the client (the approval is noted at the first sign-in from that client's tenant) and never shows Microsoft's text. |
| Two-step sign-in | Authenticator-app codes (TOTP) with ten one-time recovery codes. New installs require it for everyone; installs made before the first release (2.0.0) keep requiring it for staff (everyone but client accounts) until `OFFICESENTRY_REQUIRE_MFA=all` is set (the Users page recommends it). A code or recovery code works once, even when sent twice at the same moment. Recovery codes are stored as an HMAC under a random key that is itself encrypted with the encryption key, so a copy of the database alone can't be used to guess them; codes made before 2.2.0 (a plain SHA-256) are put under the key when the portal starts. Adding an authenticator from the account page, turning it off and replacing recovery codes ask for the password, and an admin resetting someone else's password, two-step sign-in or sessions gives their own password too. |
| Passwords | At least 12 characters, not one of the 20,000 most commonly used passwords of that length (from the public list of 10 million leaked passwords), and not containing the username or the product's name; scrypt hash (Werkzeug). A password an admin set (new user or reset) has to be replaced at the next sign-in. |
| Sign-in limits | Wrong passwords never lock an account. They slow down the address they come from: 5 per username or 20 in all per 15 minutes, counted before the password is checked so parallel requests can't get past them. A spread-out attack on one account slows only addresses that haven't signed in to it before. Five wrong two-step codes lock that step for 15 minutes. Five wrong passwords where a signed-in browser is asked for it again sign that browser out, and never lock the account, so a stolen session can't keep its owner from signing in to end it. The client address comes from `X-Forwarded-For` only behind `OFFICESENTRY_TRUSTED_PROXIES`. |
| Sessions | Server-side: the cookie holds a random token, stored as a hash. 2 hours idle and 8 hours in total by default (`OFFICESENTRY_SESSION_IDLE_MINUTES`, `OFFICESENTRY_SESSION_HOURS`); ended on sign-out, disable, delete, role, password or two-step change. `__Host-` cookie, HttpOnly, SameSite=Lax, Secure over HTTPS. Same-site redirects only. |
| CSRF | Flask-WTF on every form. |
| Headers | Nonce-based CSP with no external assets, HSTS over HTTPS, X-Frame-Options DENY (the portal's own email and design previews may be framed by the portal, under a stricter CSP), nosniff, Referrer-Policy, Permissions-Policy, Cross-Origin-Opener-Policy and Cross-Origin-Resource-Policy `same-origin`, `X-Permitted-Cross-Domain-Policies: none`. `Cache-Control: no-store` on everything but the static files and the brand colours and logo, which only the visitor's own browser may keep. Behind a proxy, the visitor's address, scheme and host come from the forwarded headers of `OFFICESENTRY_TRUSTED_PROXIES` hops; a path prefix never does. The bundled Caddy drops the `Server` header. |
| Activity log | Sign-ins, every change, and every refusal (a page or tenant someone wasn't allowed, a form sent with a stale token), with the client address; admins read, filter and export it under *Settings → Activity log*. Failed sign-ins with unknown usernames and refusals are counted per address and hour, without storing what was typed, and sign-in noise is removed after 90 days. Someone given the activity log who doesn't see every client reads only the entries about their own clients. |
| Secrets | Session key and encryption key are separate. `python -m officesentry rotate-keys` re-encrypts every encrypted value (the app's certificate keys, the staff sign-in certificate key, two-step secrets and the recovery code key, mail credentials, the alert webhook and its signing secret, and the setup wizard's sign-in code while it waits) and refuses if it finds one it doesn't know. Generated secrets are written with mode `0600`. |
| PDFs | Rendered by WeasyPrint with a URL fetcher that only loads the app's own static files and `data:` URIs: no other files, no network. |
| Outbound requests | Webhooks and MTA-STS fetches are HTTPS only, refuse internal addresses and connect to the address that was checked (no DNS rebinding), through a proxy (`HTTPS_PROXY`) too. Signed webhooks cover a timestamp (a copied request can't be sent again) and need a signing secret of at least 16 characters. The SMTP server is looked up once and reached at that address; a link-local (cloud metadata), multicast or reserved address is refused, while this machine and the local network are allowed, since mail relays live there. The SMTP password is never sent without TLS, except to a relay on the same machine. |
| Updates | The portal reads the list of releases from GitHub's API once a day (an admin can turn it off) and never installs anything. The optional **updater** container (`updater.py`, off until turned on in `.env`) installs a release when someone with *Install updates* presses Update. It is the only container given the Docker socket and the folder with `docker-compose.yml`, has no ports, and takes one thing from the portal: the release number asked for, in a file on the data volume, ignored once ten minutes old. Its own record of an update in progress is in a volume the portal can't reach. It installs only a published release of this repository (not a draft or pre-release) that is newer than the one running with the same major version and whose tag is on a commit in `main`'s history, only once cosign has verified the image's build provenance was signed by this repository's `release.yml` (GitHub's OIDC issuer, run from `main` or a `v*` tag) for the commit the release's tag points at, and it pulls and runs exactly that digest. So the most a compromised portal can do is ask for a newer, genuine release. Its helpers, Docker's command line (with no network) and cosign (read-only, every capability dropped), are pinned by digest. |

### Data retention

Admins choose how long personal data is kept under *Settings → Data
retention*. By default:

- snapshots: one a day for 30 days, then one a month for 12 months, with the
  sign-in detail in them (IP addresses and locations);
- the daily history of how each item changed: 25 months;
- the activity history: every sign-in for 180 days, daily sign-in summaries
  for 25 months and directory audit events for 7 years;
- the audit log add-on's file and mailbox events: 2 years;
- run logs: 90 days, after which only their warnings and errors stay;
- resolved findings, the activity log and issued reports: until an admin
  sets a period.

Set shorter periods to match your own policy. Neither the activity log nor
issued reports can be set below 365 days. The worker deletes once a day in
small batches with `secure_delete`; changes to the settings are audited.
Details in `v2/DEPLOY.md`.

### Offboarding a client

Deleting a client removes everything stored for it in one transaction and
checks nothing tenant-scoped is left. Activity log entries keep who did what
and when, but details naming the client's people are removed, and client
accounts deleted with it are anonymised. Deleted rows are overwritten in the
database file (`secure_delete`). The worker then erases what's left on disk
in the background: it removes the client's activity history, compacts the
database file when there's enough free space for the copy that takes, and
empties its write-ahead log. If that fails, *Settings → System status* shows
it under *Deleted clients* (and system alerts tell the team, when they're
on), and the worker tries again every hour.
If the client had the audit log add-on, its audit feed is stopped too.
**Backups made before the delete keep the data until they rotate out**: with
the Docker setup's nightly jobs, after two weeks on the server, and after a
year in off-site copies if you keep them.

### What should never be committed

- Certificates and keys: `*.pfx`, `*.cer`, `*.pem`, `*.key`, `*.p12`
- `.env` files and any credential, key or token
- The data directory (`data/`, or the `/data` Docker volume): it holds the
  database, encrypted keys and client snapshots
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
  to `127.0.0.1`, and runs the portal and worker read-only, as a non-root
  user, with every capability dropped and a process limit. Set `OFFICESENTRY_TRUSTED_PROXIES` to the number of proxies
  in front (1 for the bundled Caddy) so sign-in limits and the activity log
  see real client addresses.
- The Update button's updater container is given the Docker socket, which is as good as root on the
  server. It's off unless you turn it on (`COMPOSE_PROFILES=updater`); leave it off, and update by hand,
  if your policy rules out a container with the socket (`v2/DEPLOY.md`, "The Update button").
- Only use `OFFICESENTRY_INSECURE_COOKIES=1` for local development over HTTP.
  [Securing the portal](v2/docs/securing-the-portal.md) has the full list of
  what the portal does by itself, what the person running it has to do, and
  proxy configurations for Caddy, nginx, Traefik and a CDN.
- Back up the database, ideally encrypted and off the server (restic to
  S3-compatible storage, `v2/DEPLOY.md`, "Off-site backups"), and run the
  restore drill. Keep a copy of `secrets.json` apart from the backups, in a
  password manager say: losing the encryption key means the stored
  certificate can't be decrypted and must be replaced. The Docker setup keeps
  `secrets.json` in its own volume (`/keys`), so the data volume and its
  backups never hold it.
- Review the Entra app's permissions and sign-in logs in your own tenant
  from time to time.

## How security is checked

Every change goes through the same checks, whoever wrote it, and isn't merged until they pass
([CONTRIBUTING.md](CONTRIBUTING.md#how-changes-are-checked)). The ones that guard security (the tests are
in `v2/tests/`):

- **Access control.** `test_roles.py` opens every page as every built-in role and expects a
  refusal exactly where the role lacks what the page needs (`test_limited_roles.py` does the same for
  roles limited to some clients). `test_isolation_walk.py` opens every page and download that
  names a client with another client's IDs, and posts every form with them, as staff and as a client,
  and fails if anything of the other client shows. `test_access.py` checks every page needs a sign-in,
  and `test_report_areas_everywhere.py` that a report someone can't open stays closed inside every
  other page that shows it.
- **Sign-in.** `test_login_security.py` covers two-step codes, sign-in limits, sessions and cookies;
  `test_client_sign_in_attacks.py` tries to reach another client, a staff account or a new account
  through client sign-in with Microsoft, with tokens a client's own administrator could make.
- **Read-only.** `test_no_writes.py` fails on any request other than GET outside its named exceptions,
  and runs a whole collection to check every request is a read.
- **Secrets.** `test_no_secrets_downloadable.py` plants one of every kind of secret and searches every
  page and download for it, as an admin, an analyst and a client; `test_support.py` does the same for
  every byte of the support package; `test_key_rotation.py` fails on an encrypted value `rotate-keys`
  wouldn't re-encrypt.
- **Injection.** `test_injection_walk.py` puts markup in every name a client's tenant sends and fails
  if any page, report or PDF shows it unescaped.
- **Code scanning.** GitHub's CodeQL scans the code on every pull request, on every change to `main`
  and every week.
- **Dependencies.** Python packages come from hash-pinned lock files, which `pip-audit` checks for known
  vulnerabilities on every pull request and every Monday. Dependabot proposes updates weekly, and
  security fixes as soon as GitHub publishes them. GitHub Actions are pinned to commit hashes and base
  images by digest.
- **Releases.** A release is built from a commit on `main` that passed the full CI run, scanned for
  known vulnerabilities, and signed (below). The Update button installs only a release whose signature
  checks out.
- **The public demo.** `test_public_demo.py` opens every page as the demo's visitor, who can't change
  anything: the demo refuses every request but GET, HEAD and OPTIONS.

## Checking a release

Every image at [`ghcr.io/jackd99/officesentry`](https://github.com/JackD99/OfficeSentry/pkgs/container/officesentry)
is built by this repository's [release workflow](.github/workflows/release.yml) from a tagged commit on
`main` that passed the full CI run, and scanned before it gets a version tag (a fixable vulnerability rated
high or critical stops the release). Each one carries an SBOM and a build provenance saying which workflow,
commit and tag made it, and the provenance is signed through GitHub's artifact attestations (Sigstore). To
check an image before running it, with the [GitHub CLI](https://cli.github.com) and the version you're
installing in place of `<version>`:

```bash
gh attestation verify oci://ghcr.io/jackd99/officesentry:<version> --repo JackD99/OfficeSentry
```

It passes only for an image this repository's workflow built; anything else, or a copy changed since, fails.
Releases up to 2.2.0 signed each platform's build but not the multi-platform image the version tags point at,
so for those, check the digest the release notes list instead (`oci://ghcr.io/jackd99/officesentry@sha256:…`).

What goes into the image is pinned too: Python packages from hash-pinned lock files that CI audits for known
vulnerabilities on every change and every week, and the base image by digest. The image runs as an
unprivileged user and leaves out pip, which nothing needs once it's built. The GitHub Actions the workflows
use are pinned to full commit hashes, and the jobs that can publish or deploy don't keep their token where
the code they build could read it.

## Known limitations

- SQLite suits one server; it isn't built for several web servers sharing a database.
- Staff can sign in with Microsoft once an admin sets it up
  ([Staff sign-in with Microsoft](v2/docs/staff-sign-in.md)), and so can a
  client's people, from their own company's tenant, once an admin turns client
  sign-in on ([Client sign-in with Microsoft](v2/docs/client-sign-in.md)).
  Clients in another Microsoft cloud than the sign-in app, or whose tenant ID
  isn't known yet, keep using a password and two-step code.
- An account that signs in with a password gets it from an admin, shared out
  of band. Whoever uses it first must replace it, so a leaked one is noticed
  (the real user can't sign in), but one-time invitation links would be
  better. Accounts that sign in with Microsoft don't need one: staff accounts
  are made at their first sign-in, and a client's people are found in their
  own directory and added with no password.
