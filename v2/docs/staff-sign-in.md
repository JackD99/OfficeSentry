# Staff sign-in with Microsoft

Your team can sign in to Office Sentry with the Microsoft account they already use at work, protected by
your own Conditional Access and MFA, instead of a separate username, password and authenticator code. An
admin sets it up once under **Settings → Staff sign-in**; it takes about two minutes.

What it is and isn't:

- It's about who signs in to the portal. Office Sentry's read-only connection to your clients' tenants
  doesn't change, and no new permissions are asked of any client.
- It uses a small, separate app registration in **your own** tenant ("Office Sentry sign-in") with no API
  permissions at all. Office Sentry only asks Microsoft who signed in.
- Roles come from Entra: you assign people (or a group) to the app's **Admin** or **Analyst** role in the
  Entra admin centre. Which clients an analyst sees is still granted in Office Sentry, so tenant isolation
  works exactly as before.
- Client accounts keep signing in with a password and two-step code.
- Passwords stay as the way in for staff if Microsoft is ever down, until you turn them off.

## Setting it up

1. Sign in to Office Sentry as an admin and open **Settings → Staff sign-in**.
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
4. *App roles → Create app role*, twice: display name and value `Admin`, then display name and value
   `Analyst`, both allowed for *Users/Groups*.
5. *Enterprise applications → Office Sentry sign-in → Properties*: set *Assignment required* to **Yes**.
   Under *Users and groups*, assign yourself the Admin role.
6. Copy the app's *Application (client) ID* and *Directory (tenant) ID* from its overview page into the
   form on the page, save, then tick **Let staff sign in with Microsoft** and save.

## Giving people access

In the Entra admin centre, open *Enterprise applications → Office Sentry sign-in → Users and groups* (the
**Assign people in Entra** button on the settings page takes you there) and add each person, or a group,
with a role:

| Role in Entra | In Office Sentry |
|---|---|
| Admin | Everything, including settings, client tenants and users. |
| Analyst | Works with the client tenants an Office Sentry admin grants. |

A role change applies at the person's next sign-in. Someone removed from the app can't sign in with
Microsoft any more.

New staff don't need an account made for them: their account appears on **Settings → Users** at their first
sign-in. Then grant them clients on their user page as usual (or tick *All tenants*, or a tenant group).

Existing accounts are linked to a Microsoft account in one of two ways:

- The person presses **Connect my Microsoft account** on their **Account** page (it asks for their password
  first, as every change to how an account signs in does).
- At their first Microsoft sign-in, if their Office Sentry username is the same as their Microsoft sign-in
  name (the user principal name, such as `jo@yourmsp.com`; never the mail address, which Microsoft doesn't
  verify), the two are linked automatically and their client grants are kept.

A linked account shows its role as *set in Entra* on its user page. An admin can **Disconnect** it there,
after which the person signs in with a password again; the person can do the same on their Account page.

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
| *Your Conditional Access policies blocked this sign-in* | Check the policy that applies to the sign-in app or the person. |

Every refused sign-in is in **Settings → Activity log** with the reason.
