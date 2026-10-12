# Staff sign-in with Microsoft

Your team can sign in to Office Sentry with the Microsoft account they already use at work, protected by
your own Conditional Access and MFA, instead of a separate username, password and authenticator code. An
admin sets it up once under **Settings → Microsoft sign-in**; it takes about two minutes.

What it is and isn't:

- It's about who signs in to the portal. Office Sentry's read-only connection to your clients' tenants
  doesn't change, and no new permissions are asked of any client.
- It uses a small, separate app registration in **your own** tenant ("Office Sentry sign-in") with no API
  permissions at all. Office Sentry only asks Microsoft who signed in.
- Roles come from Entra: the app has an app role for each Office Sentry role, and you assign people (or a
  group) to one in the Entra admin centre. Which clients someone sees is still granted in Office Sentry, so
  tenant isolation works exactly as before.
- Client accounts sign in with a password and two-step code, unless you also turn on
  [client sign-in](client-sign-in.md): then your clients' people can use their own work account too.
- Passwords stay as the way in for staff if Microsoft is ever down, until you turn them off.

## Setting it up

1. Sign in to Office Sentry as an admin and open **Settings → Microsoft sign-in**.
2. Press **Sign in to Microsoft**. The page shows a code and a link to Microsoft's sign-in page.
3. Open the link, enter the code and sign in as a Global Administrator of your own (the MSP's) tenant.
   Microsoft may ask you to let *Microsoft Graph Command Line Tools* (Microsoft's own app) manage apps for
   you; accept. Office Sentry uses this sign-in once and keeps nothing from it.
4. Come back to Office Sentry. Within a few seconds it has created the sign-in app, assigned you its Admin
   role and turned Microsoft sign-in on.
5. Press **Connect my Microsoft account** on the same page and sign in, so your existing admin account is
   linked to your Microsoft account.
6. Sign out and press **Sign in with Microsoft** on the sign-in page to try it.

Passwords still work after this, so nothing breaks if a step is missed.

### If the wizard can't do everything

The account you sign in with needs to create app registrations, assign people to them and grant consent.
A Global Administrator can do all three. With a lesser role, the page says which step it couldn't do and
how to do it in the Entra admin centre:

- **Nobody has a role yet**: *Enterprise applications → Office Sentry sign-in → Users and groups → Add
  user/group*, pick yourself and the Admin role.
- **Consent wasn't granted**: *Enterprise applications → Office Sentry sign-in → Permissions → Grant admin
  consent*. Without it, each person may see a consent prompt (or "needs admin approval") the first time.

### Setting it up by hand

If Conditional Access blocks device-code sign-in, or you'd rather make the app yourself, open *Set it up by
hand instead* at the bottom of the setup card:

1. Press **Make the certificate** and download its `.cer` file.
2. In the Entra admin centre: *App registrations → New registration*. Name: `Office Sentry sign-in`.
   Supported account types: *Accounts in this organizational directory only*. Redirect URI (Web): the
   address shown on the page (`https://<your portal>/login/microsoft/callback`).
3. *Certificates & secrets → Certificates → Upload certificate*: the `.cer` file.
4. *App roles → Create app role*, allowed for *Users/Groups*, once for each role in the table under
   [Giving people access](#giving-people-access) you'll use (the value as shown, any display name). `Admin`
   is the one you need now.
5. *Enterprise applications → Office Sentry sign-in → Properties*: set *Assignment required* to **Yes**.
   Under *Users and groups*, assign yourself the Admin role.
6. Copy the app's *Application (client) ID* and *Directory (tenant) ID* from its overview page into the
   form on the page, save, then tick **Let staff sign in with Microsoft** and save.

## Giving people access

In the Entra admin centre, open *Enterprise applications → Office Sentry sign-in → Users and groups* (the
**Assign people in Entra** button on the settings page takes you there) and add each person, or a group,
with a role:

| App role in Entra | Office Sentry role |
|---|---|
| `Admin` | **Administrator**: everything, including settings, clients and users. |
| `TeamLeader` | **Team leader** |
| `Analyst` | **Second line** |
| `FirstLine` | **First line** |
| `AccountManager` | **Account manager** |
| `Auditor` | **Auditor** |

What each role can do is under **Settings → Users → Roles**. Someone assigned more than one gets the first in
this table. A role change applies at the person's next sign-in, and someone removed from the app can't sign
in with Microsoft any more. The role on their user page follows Entra, so change it there.

**Custom roles** (made under *Settings → Users → Roles*) are reached the same way: create an app role on the
sign-in app (*App registrations → Office Sentry sign-in → App roles*) with a value such as `ServiceDesk`, then
type that value next to the role under *Who can sign in → Custom roles from Entra* on the Microsoft sign-in page.
They come after the built-in roles in the order above. Deleting a custom role removes its value too.

New staff don't need an account made for them: their account appears on **Settings → Users** at their first
sign-in. Then grant them clients on their user page as usual (or tick *Every client, including ones added later*, or a client group).
To grant clients before their first sign-in, add them on **Settings → Users** with *Microsoft* under *How
they sign in* and their sign-in name as the username: the account has no password, is connected at their
first sign-in, and takes its role from Entra then.

Existing accounts are linked to a Microsoft account in one of two ways:

- The person presses **Connect my Microsoft account** on their **Account** page (it asks for their password
  first, as every change to how an account signs in does).
- At their first Microsoft sign-in, if their Office Sentry username is the same as their Microsoft sign-in
  name (the user principal name, such as `jo@yourmsp.com`; never the mail address, which Microsoft doesn't
  verify), the two are linked automatically and their client grants are kept.

A linked account shows its role as *set in Entra* on its user page; capabilities added or taken away for the
person there still apply on top. Someone who manages users can **Disconnect** it there (an Administrator's
account only by an Administrator), after which the person signs in with a password again; the person can do
the same on their Account page.

Signing in with Microsoft never asks for Office Sentry's own two-step sign-in, whatever
`OFFICESENTRY_REQUIRE_MFA` says: it relies on Entra's MFA and Conditional Access, so require MFA for the
sign-in app there.

## Confirming a change without a password

Some changes ask for your password again first, so a stolen session can't make them: setting someone's
password, signing someone out everywhere and resetting their two-step sign-in. An account that signs in only
with Microsoft has no password to type, so those forms show **Confirm with Microsoft** instead. It signs you in
to Microsoft again (Microsoft asks for your password and MFA, whatever your session), and for the next ten
minutes the change goes through on that browser. Confirming with a different Microsoft account than the one
connected to yours confirms nothing.

## Turning passwords off for staff

Once you've signed in with Microsoft yourself, you can set **Passwords for staff** to *Off* on the settings
page. Staff accounts can then only sign in with Microsoft; client accounts aren't affected. Office Sentry
refuses to turn passwords off from a password session, so you can't lock yourself out.

If Microsoft sign-in ever stops working (the certificate expired, the app was deleted), turn passwords back
on from the server:

```bash
docker compose exec web python -m officesentry staff-sign-in --passwords on
```

`--off` turns Microsoft sign-in off altogether (passwords come back on). Without Docker, run
`python -m officesentry staff-sign-in ...` in the Office Sentry folder.

## The certificate

The sign-in app signs in with a certificate Office Sentry made; the private key is encrypted in the database
and never leaves. It lasts two years. Sixty days before it expires, admins see a notice and the team contacts
get an email. To replace it without a gap: **Make a new certificate** on the settings page, upload the new
`.cer` to the app registration next to the old one, press **Switch to the new certificate**, then delete the
old one in Entra.

## Common errors

| What Microsoft says | What to do |
|---|---|
| *isn't assigned to Office Sentry yet* (AADSTS50105) | Assign the person a role under *Users and groups* on the enterprise app. |
| *The reply address doesn't match* (AADSTS50011) | Add the redirect URI shown on the settings page to the app registration (*Authentication → Web*). It must match `OFFICESENTRY_BASE_URL`. |
| *doesn't recognise Office Sentry's certificate* (AADSTS700027) | Upload the current `.cer` from the settings page to the app registration, or make a new certificate and upload that. |
| *hasn't been given consent* (AADSTS65001) | Grant admin consent under *Permissions* on the enterprise app. |
| *belongs to another tenant* (AADSTS50020) | The person signed in with an account outside your tenant (a client's, or a personal account). Guests aren't supported. |
| *only accepts accounts from the MSP's own tenant* (AADSTS50194) | Client sign-in is on but the sign-in app wasn't changed to accept other organisations ([client sign-in](client-sign-in.md#setting-it-up), step 1). Staff can use the *Sign in with Microsoft as staff* link meanwhile. |
| *Your Conditional Access policies blocked this sign-in* | Check the policy that applies to the sign-in app or the person. |

Every refused sign-in is in **Settings → Activity log** with the reason.
