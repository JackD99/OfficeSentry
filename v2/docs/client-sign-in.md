# Client sign-in with Microsoft

The people at your clients can sign in to their reports with the work Microsoft account they already use,
under their own organisation's MFA and Conditional Access, instead of a username, password and authenticator
code from you. You add their primary contacts once; they press **Sign in with Microsoft** like your staff do.

What it is and isn't:

- It uses the same small sign-in app as [staff sign-in](staff-sign-in.md), in **your own** tenant, with no
  API permissions. Office Sentry only asks Microsoft who signed in. The read-only connection to your clients'
  tenants doesn't change, and no client is asked for any new permission.
- Each person still needs a client account here, granted their own company only, so tenant isolation works
  exactly as before: they see that client's reports and nothing else.
- Office Sentry only lets them in from **their own company's tenant**. A Microsoft account from anywhere else,
  even one with the same address, gets nowhere, and never gets an account made for it.
- Roles a client's own admin assigns in their tenant count for nothing: a client's people are always client
  accounts, with the role you gave them here.
- Client accounts with a password keep working. You can mix the two.

## Why their own tenant, not guests in yours

The other way to do this is to invite each client's contacts into your own tenant as guests (B2B) and let them
sign in through it. Office Sentry doesn't do that, on purpose:

- **No guest accounts in your tenant.** Guests from every client would sit in your directory, need
  Conditional Access exceptions and reviews, and could be given access to your own Microsoft 365 by mistake.
- **No invitation admin.** There's nothing to invite, redeem, resend or clean up when someone leaves. When
  the client disables someone in their own tenant, they can't sign in here any more.
- **Their MFA, not yours.** They sign in with the account and security their own organisation already
  manages.

## Setting it up

You need [staff sign-in](staff-sign-in.md) set up first (the sign-in app). Then, as an admin, under
**Settings → Microsoft sign-in**, in *Your clients' people*:

1. **Let the sign-in app accept your clients' accounts.** Press **Sign in to Microsoft**, enter the code on
   Microsoft's page and sign in as a Global Administrator (or Application Administrator) of your own tenant.
   Office Sentry changes one setting on the sign-in app, its supported account types, to accounts in any
   organisation's directory, and nothing else. It asks Microsoft for `Application.ReadWrite.All` (delegated,
   used once) to do it, and keeps nothing from the sign-in.

   By hand instead: in the Entra admin centre, *App registrations → Office Sentry sign-in → Authentication →
   Supported account types*, choose *Accounts in any organizational directory (Any Microsoft Entra ID tenant -
   Multitenant)*, save, then press **I've changed it** on the page.
2. **Turn on client sign-in.**
3. **Add each client's people**, on the client's **Settings → Client sign-in** page (see below).

Your staff's sign-in doesn't change. *Sign in with Microsoft* now accepts any organisation's account, and the
tenant the account belongs to decides who it is: your own tenant is staff, as before; a client's tenant is
that client's people. If a staff member is ever turned away
as if they were a client, the sign-in page then offers **Sign in with Microsoft as staff**, which signs in
through your own tenant only, as before.

## Adding a client's people

Open the client, then **Settings → Client sign-in**. It shows who at the client can sign in and how, whether
their administrator has approved the sign-in app, and:

- **Add someone**: their work email address, the one they sign in to Microsoft with (their user principal
  name, or failing that their email address). Office Sentry finds them in the client's directory as its last
  collection read it, so someone new shows up after the next collection; guests in the client's directory
  can't be added. It makes a Client viewer account granted this client only, with no password, connected to
  that person's Microsoft account. The client's report contacts who don't have an account yet are listed for
  one-click adding. Adding someone who already has a password account for this client connects that account
  instead, and they can then sign in either way.
- **Tell them how to sign in**: a message to copy into an email, with the sign-in page's address. Each person
  also has a **Sign-in link** that opens Microsoft's sign-in with their address filled in.

When they sign in, Office Sentry matches the account on Microsoft's ID for that person in that client's
tenant only, never on a name or email address, which their organisation's administrators can change. A later
rename doesn't matter, and someone given their old address doesn't become them.

A client's person signed in this way only ever sees clients in their own organisation, even if they're given
another one by mistake, and the Users page won't grant them one. Their account can't be given a staff role.
Microsoft sign-in doesn't ask for Office Sentry's own two-step sign-in, whatever `OFFICESENTRY_REQUIRE_MFA`
says: their organisation's MFA and Conditional Access apply instead.

## Their administrator's approval

Microsoft asks each organisation to approve an app before its people use it. Many organisations only let an
administrator do that, so the first person to sign in may see *Need admin approval*.

The client's **Client sign-in** page has a **Copy approval link** for their Microsoft 365 administrator (a
Global Administrator, Cloud Application Administrator or Application Administrator). It asks for the sign-in
scopes only (`openid`, `profile`, `email`): their people can sign in here, and Office Sentry learns each
person's name and sign-in address, nothing else. They approve once for everyone, and land on a thank-you
page. The message to send their people includes the link until the approval is noted.

Office Sentry notes the approval when Microsoft says it was given for that client's own tenant, and again when
someone from it first signs in. It's shown on the page; it doesn't change who can sign in.

## When a client's tenant ID isn't known yet

Office Sentry tells the client's people apart by their tenant ID. A client added by its tenant ID has it from
the start; one added by domain gets it from its first collection, and until then the page says so and nobody
there can sign in with Microsoft. Clients in another Microsoft cloud than your sign-in app (GCC High, DoD,
China) keep using passwords. Your own tenant, if you report on it, is staff: people there sign in as staff.

## Common errors

| What they see | What to do |
|---|---|
| *Your Microsoft account isn't set up to sign in here* | Add them on the client's Client sign-in page with the address they sign in to Microsoft with, and check the account is granted that client, isn't disabled, and the client isn't archived. Someone whose account in the client's tenant was deleted and made again is a new person to Microsoft: remove the old account here and add them again. |
| *Your organisation hasn't approved signing in to these reports yet* (AADSTS65001, 90094, 90095) | Send their Microsoft 365 administrator the approval link from the Client sign-in page. |
| *You didn't approve the sign-in on Microsoft's page* (AADSTS65004) | They declined Microsoft's consent prompt. They can sign in again and accept. |
| *Your organisation only lets people it has assigned use this sign-in* (AADSTS50105) | Their tenant requires assignment to the app: their administrator assigns them under *Enterprise applications → Office Sentry sign-in → Users and groups*. |
| *Your organisation's Conditional Access policies blocked this sign-in* (AADSTS53003) | A policy in the client's tenant applies to the sign-in app; their IT team can check it. |
| *The sign-in app only accepts accounts from the MSP's own tenant* (AADSTS50194) | Step 1 wasn't done in Entra. Run it from **Settings → Microsoft sign-in**. Meanwhile staff get a *Sign in with Microsoft as staff* link on the sign-in page. |

Every refused sign-in is in **Settings → Activity log**. Sign-ins from organisations with no account here are
counted per address, without their names.

## Turning it off

**Turn off client sign-in** under *Settings → Microsoft sign-in* sends client accounts back to passwords;
anyone without one can't sign in until you set one on their user page. The sign-in app keeps accepting other
organisations' accounts until you change it back in Entra, but Office Sentry lets none of them in.
