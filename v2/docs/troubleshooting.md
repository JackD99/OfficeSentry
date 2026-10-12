# Troubleshooting

The errors people most often meet while setting up Office Sentry and adding
their first clients, and how to fix each one. Commands are for a Docker
install and run from the `v2` folder on the server; without Docker, run the
same `python -m officesentry ...` part in the virtual environment.

For a failed collection, the run's own error message is usually the quickest
guide: [Onboarding a client](onboarding-a-client.md#errors-on-failed-runs)
explains each one. A client's **Manage** page (under **Settings**) also says
in plain words what's wrong with its connection and where to fix it. For
staff signing in with Microsoft, [Staff sign-in](staff-sign-in.md#common-errors)
lists the errors and their fixes, and [Client sign-in](client-sign-in.md#common-errors)
does the same for your clients' people.

**Where to look first**

| What | Command |
|---|---|
| Are both containers up and healthy? | `docker compose ps` (both `web` and `worker` should say *healthy*) |
| What did the portal log? | `docker compose logs --tail 100 web` |
| What is the worker doing? | `docker compose logs -f worker` |
| Everything a bug report needs, in one zip | **Settings → Support**, or `docker compose run --rm -T web python -m officesentry support-package --out - > support.zip` ([Getting help](getting-help.md)) |

## Contents

- [The first-admin link doesn't work](#the-first-admin-link-doesnt-work)
- [Signing in fails with "The CSRF session token is missing"](#signing-in-fails-with-the-csrf-session-token-is-missing)
- [Creating the app fails](#creating-the-app-fails)
- [Two app connections, one marked "Needs client ID"](#two-app-connections-one-marked-needs-client-id)
- [AADSTS50011 on the consent page](#aadsts50011-on-the-consent-page)
- [Consent was granted but Office Sentry didn't record it](#consent-was-granted-but-office-sentry-didnt-record-it)
- [Mailboxes fail: Global Reader is missing](#mailboxes-fail-global-reader-is-missing)
- [The background worker isn't running](#the-background-worker-isnt-running)
- [Other sign-in errors (AADSTS codes)](#other-sign-in-errors-aadsts-codes)
- [PDF buttons open a print window instead of a file](#pdf-buttons-open-a-print-window-instead-of-a-file)
- [Asking for help](#asking-for-help)

## The first-admin link doesn't work

The link on the **No admin account yet** line in `docker compose logs web` is
built from `OFFICESENTRY_BASE_URL` in `.env`. If that isn't the address you
open Office Sentry on, the link points to the wrong place.

1. Set `OFFICESENTRY_BASE_URL` in `.env` to the exact address, for example
   `https://sentry.example.com` (https, no trailing slash).
2. Run `docker compose up -d` to restart with it.
3. Print a fresh link: `docker compose exec web python -m officesentry setup-link`.

The link stops working once the first admin exists. To add more people, sign
in as that admin and use **Settings → Users**.

## Signing in fails with "The CSRF session token is missing"

The sign-in cookie is marked *Secure*, so browsers only keep it over https.
Opened over plain `http://` on a server's address, the browser drops it and
every form says *Bad Request: The CSRF session token is missing*.

Open the portal at its https address (the `--profile https` option in the
Quick start, or your own reverse proxy). For a test on your own computer only,
`OFFICESENTRY_INSECURE_COOKIES=1` turns the *Secure* flag off; never set it on
a server other people reach.

## Creating the app fails

*Sign in to Microsoft* on the App connection page, and the `create-app`
command, both sign you in with a code (device sign-in) through Microsoft's
own *Microsoft Graph Command Line Tools* app, then register Office Sentry's
app in **your own** (MSP) tenant. The page says what went wrong in plain
words; these are the usual causes.

- **Sign in to your own tenant, not a client's.** If your account is a guest
  in your tenant, enter the tenant under *More options* (or `--tenant` for the
  command), as a domain (`yourmsp.onmicrosoft.com`) or tenant ID.
- **The account needs to be able to create app registrations and approve
  the sign-in tool's access**: Application Administrator, Cloud Application
  Administrator or Global Administrator. Without one of these, Microsoft
  shows *Need admin approval* during sign-in, or the command stops with a
  Graph `403` error.
- **Microsoft may ask to approve "Microsoft Graph Command Line Tools"** for
  the `Application.ReadWrite.All` permission. That's the sign-in tool, not
  Office Sentry. If your account can't approve it, ask a Global
  Administrator of your tenant to sign in instead.
- **The device code expires after about 15 minutes.** Press *Sign in to
  Microsoft* again (or run the command again) for a new one.
- **"Your Conditional Access policies block this kind of sign-in"** (AADSTS53003):
  many tenants block device code sign-in, as Microsoft recommends. Ask
  whoever manages Conditional Access to let your account use it for a few
  minutes, or create the app by hand.
- **"Setup was interrupted while the app was being created"**: the portal
  restarted in the middle. Look in the Entra admin centre under *App
  registrations* for an "Office Sentry (read-only)" app that no connection on
  the App connection page has the client ID of, delete it, and start again.

To create the app by hand (for example because device sign-in is blocked by
Conditional Access): **Settings → App connection → Other ways to create the
app**, generate a certificate, then follow the steps shown there.

## Two app connections, one marked "Needs client ID"

Older versions made a second connection when you pressed **Generate
certificate** and then also ran `create-app`. Now *Sign in to Microsoft* and
`create-app` both use a certificate you already generated for that cloud, so
this only happens if you generate two certificates by hand.

It does no harm. When you add a client and Office Sentry asks which app
connection to use, choose the one marked **Ready**. Picking the other one
makes consent fail.

## AADSTS50011 on the consent page

Microsoft says the reply address doesn't match. The consent link sends the
browser back to `<OFFICESENTRY_BASE_URL>/consent/callback`, and that must be
one of the app registration's redirect URIs, exactly.

1. Open **Settings → App connection** and note the redirect URI it shows.
2. In **your own** tenant's Entra admin centre, open *App registrations →
   (the Office Sentry app) → Authentication*. Under the **Web** platform, add
   that redirect URI exactly (https, same host name, no trailing slash) and
   save.
3. Open a fresh consent link (reload the client's page first).

It usually means `OFFICESENTRY_BASE_URL` was different when the app was
created (*Sign in to Microsoft* and `create-app` use it for the redirect URI;
the App connection page warns when you open it at a different address), or the app was created
by hand with a different address. If `OFFICESENTRY_BASE_URL` itself is wrong,
fix it in `.env`, run `docker compose up -d`, and then update the redirect URI.

## Consent was granted but Office Sentry didn't record it

Office Sentry only records consent when the browser comes back to it with a
fresh link, signed in as the same admin, in the same browser that opened it.
A link sent to the client, or opened over an hour later, is approved at
Microsoft but not recorded.

Press **Check connection now** on the client's checklist or Manage page. When
Office Sentry can sign in and read the tenant, it records the consent itself
and starts the first collection. [Onboarding a client](onboarding-a-client.md#2-get-admin-consent)
explains each message you can see on the way back.

## Mailboxes fail: Global Reader is missing

The **Mailboxes** data source fails with an `Exchange 401` or `Exchange 403`
error ending *Exchange refused access. In this tenant, assign the Global
Reader role to the Office Sentry app*, and the email checks that need it show
as *not checked*. Everything else, including inbox rules, still works.

Exchange Online only answers an app that holds an admin role in the tenant.
In the **client's** tenant:

1. *Microsoft Entra admin centre → Roles and admins → Global Reader → Add
   assignments*.
2. Select the Office Sentry app by name (the client's Manage page shows the
   name it has in that tenant).
3. Wait a few minutes: Microsoft takes a while to apply a new role
   assignment. Then press **Check again** on the checklist, and **Refresh**
   the Mailboxes data source once Global Reader shows as assigned.

If it still fails, check that consent included `Exchange.ManageAsApp`: the
Manage page lists any permission the tenant didn't grant. Don't give the app
Global Administrator or Exchange Administrator instead; they work, but give it
far more than reporting needs, and the Manage page warns about them.

## The background worker isn't running

Admins see this notice at the top of every page, collections stay *Queued*
and scheduled emails don't go out. The worker does all the background work,
so nothing new arrives until it's back.

1. Run `docker compose ps`. If `worker` isn't listed as running, start it with
   `docker compose up -d`.
2. If it is running but not healthy, look at `docker compose logs --tail 100
   worker` for an error, then restart it: `docker compose restart worker`.
   The worker only reports itself alive while it makes progress, so one
   collection stuck for more than four hours, or its main loop stalling for
   ten minutes, also shows as not running.
3. The worker waits for the portal to be healthy before it starts, and the
   portal upgrades the database first. Right after an update, give it a
   minute or two.

Run one worker only. Without Docker, run `python -m officesentry worker` as a
service (DEPLOY.md shows how on Windows), so it starts again after a reboot.

## Other sign-in errors (AADSTS codes)

Office Sentry explains the common sign-in errors in plain words on the run's
page and the Manage page. The ones met most during setup:

| Code | What's wrong | Fix |
|---|---|---|
| `AADSTS700016` | The tenant doesn't have the app yet. | Admin consent hasn't been granted there, or the client ID under *App connection* is wrong. |
| `AADSTS700027` | The app doesn't have Office Sentry's certificate. | Upload the `.cer` from *App connection* to the app registration (*Certificates & secrets → Certificates*). A client secret won't do. A new certificate can take 5 to 10 minutes to work. |
| `AADSTS50194` | The app only works in the tenant it was created in. | In the app registration, *Authentication → Supported account types*: *Accounts in any organizational directory*. |
| `AADSTS65001`, `AADSTS7000229` | Consent hasn't been granted in this tenant. | Send its Global Administrator the consent link. |
| `AADSTS90002`, `AADSTS900023` | Microsoft can't find the tenant. | Check the tenant ID or domain, and its Microsoft cloud. |
| `AADSTS53003` | A Conditional Access policy in the client's tenant blocks the app. | The client excludes Office Sentry's app from that policy, or allows your server's location. |
| `AADSTS7000112` | The client disabled the app. | *Enterprise applications → (the app) → Properties → Enabled for users to sign in: Yes*. |
| `AADSTS700024` | The server's clock is wrong. | Set the server's time to sync automatically. |

## PDF buttons open a print window instead of a file

That's how Office Sentry works on Windows without Docker: WeasyPrint's
libraries aren't available there, so PDF buttons open a print-ready page.
Choose **Save as PDF** in the print window. Scheduled emails go out without
PDF attachments. Run it with Docker to get real PDF files.

## Asking for help

Open an issue with the **Bug** form and attach a **support package**:
**Settings → Support** makes one zip of the version, settings, health
checks, recent failures and logs, with secrets and client names taken out
([Getting help](getting-help.md) says exactly what's in it). Quote the
reference shown on the error page if there was one. Issues are public, so
remove tenant names and IDs, domains, user names and email addresses, and IP
addresses from anything you paste yourself. Keep Microsoft's error codes
(`AADSTS...`, `Authorization_RequestDenied`): they help and identify nobody.
Security problems go through [private vulnerability reporting](../../SECURITY.md)
instead.
