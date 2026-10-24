<!-- since 2.6.0 -->
# Autodesk licences

For MSP admins and account managers whose clients pay for Autodesk products: **which Autodesk seats each client
pays for, who holds them, who hasn't opened their product in 90 days, and which seats are still held by
people who have left**, beside the client's Microsoft 365 licences. Office Sentry reads them every night from
Autodesk's own reporting API, for the clients whose Autodesk team you pick.

It's off until you set it up, and nothing about Autodesk shows anywhere (clients' Settings tabs, the report
library, people's pages) until then. Like everything else in Office Sentry it only reads: Autodesk has no API
that assigns seats or changes users, and Office Sentry couldn't use one if it had.

## What it needs

- **The client's Autodesk team must be on the Business Success Plan (formerly Premium) or Enterprise plan.**
  Autodesk's [Business Success Plan Reporting API](https://aps.autodesk.com/en/docs/insights/v1/developers_guide/overview/)
  only answers for those teams. A Standard plan team has no API: Office Sentry lists it with *No API
  reporting* and can't read it.
- **An admin of the client's Autodesk team**, to make a personal access token. That's usually one of your own
  technicians, added to each client's team as a secondary admin in Autodesk Account. One token covers every
  team that person administers, so one technician on many clients' teams needs one token.
- **One app of your own**, registered once at aps.autodesk.com. It's yours (the MSP's), not the client's.

## What it reads, and the two calls that aren't reads

| Call | What it does |
|---|---|
| `POST /authentication/v2/token` | Signs in as your app (its client ID and secret). Not a read, but it changes nothing anywhere |
| `GET /insights/v1/contexts` | Lists the Autodesk teams a sign-in administers, and whether each has API reporting |
| `POST /insights/v1/exports` | Asks Autodesk to prepare one team's report files: subscriptions, users, and the last 90 days' use of each product. Every request names the team |
| `GET /insights/v1/exports/<id>` | Checks whether the files are ready |
| `GET` the file's link | Downloads each file from the short-lived link Autodesk gives. No token goes with it |

`POST /exports` is the only call besides the sign-in that isn't a GET, and it's in `tests/test_no_writes.py`'s
allow-list with the reason. It doesn't touch a user, a seat, a team or a setting in Autodesk Account; it asks
Autodesk to make a file. Your app's token and the sign-ins' tokens only ever go to Autodesk's API host, and
only to these paths.

From each file Office Sentry keeps: each subscription's product, seats bought and given out, status and dates;
each seat's holder (their email address and name), product, subscription and the date it was assigned; and
when they last used the product and on how many of the last 90 days. Nothing else about the team is read:
no designs, files, projects or anything inside Autodesk products.

## 1. Register your app

1. Sign in at [aps.autodesk.com](https://aps.autodesk.com/myapps) with your own (the MSP's) Autodesk account,
   open **My Apps** and create an application named "Office Sentry". Choose **Server-to-Server** as its type:
   it runs on your server with nobody signing in to it.
2. Under the app's **API Access**, tick **Business Success Plan Reporting API** and nothing else.
3. In Office Sentry, open **Settings → Autodesk** (or go to `/admin/autodesk`), paste the app's **Client ID**
   and **Client Secret**, and press **Save**. The secret is stored encrypted and never shown again; to change
   it, enter a new one. A new client ID needs its secret entered with it.

Saving adds an **Autodesk** page to each client's Settings tab. Nothing is read from Autodesk yet.

## 2. Add a sign-in

The person who is an admin of the clients' Autodesk teams makes a personal access token for your app:

1. In [Autodesk Account](https://profile.autodesk.com), **Profile → Security → Personal access tokens →
   Generate**.
2. Choose your app as the token's **Application scope**, name it (say "Office Sentry"), press **Generate** and
   copy the token. Autodesk shows it once.
3. In **Settings → Autodesk → Add a sign-in**, give it a name only you see (whose token it is) and paste the
   token. Press **Add and check**.

Office Sentry asks Autodesk which teams that token administers and lists them under the sign-in, each with
*API reporting* or *No API reporting*, and which client each is given to. **Check** asks again (after the
person is added to another client's team, say). The token is stored encrypted and never shown again. Autodesk
allows seven tokens per person.

## 3. Give each client its team

On a client's **Settings → Autodesk** page, choose the client's team from **Give *client* a team** and press
**Give team**. Office Sentry reads its licences straight away, then every night with the rest of the client's
data.

- **A team belongs to one client only.** Once given, it's not offered to any other client, and the collection
  only ever asks Autodesk for that client's own teams, by ID. Rows of any other team in a file are left out.
- **Only someone who sees every client picks teams**, since the list of teams comes from every client's
  Autodesk accounts. Others see the client's teams but not the picker.
- A client can have more than one team (an office each, say). The report's *Team* column then says which
  team each row is from.

## 4. What you get

- **Autodesk licences** (*Reports → Tenant and licences*, only for clients given a team): seats bought, seats
  given out, seats bought but not given to anyone, seats nobody has opened in 90 days, and seats still held by
  someone whose Microsoft 365 account is blocked or gone. Then every subscription with its seats and end date,
  and every seat with its holder, when they last used it and their Microsoft 365 account. Seats are matched to
  Microsoft 365 people by email address (sign-in name or primary address; an alias isn't matched).
- **People's pages**: an *Autodesk* part with the products a person holds and when they last used each. A
  blocked account that still holds Autodesk seats is listed under what needs attention.
- Like every report: All clients, What changed, saved views, PDF, XLSX and CSV downloads, and scheduled emails.
  Seats bought and given out are kept as figures each night, so the headline shows their trend.

Prices aren't read from Autodesk, so the report counts seats, not money.

### When Autodesk hides people's names

A team admin can choose, in Autodesk Account, to hash people's personal data in reports. Seats then come back
named by a code instead of an email address, so they can't be matched to Microsoft 365 accounts. The report
says so, and shows *Can't tell* for their Microsoft 365 account.

## 5. Switching it off

- **For one client**: on its Settings → Autodesk page, **Remove** its team. Nothing more is read for it, and
  once it has no team its Autodesk licences report goes.
- **A sign-in**: **Remove** it on Settings → Autodesk, then delete the token in Autodesk Account (Profile →
  Security → Personal access tokens). A team read with it can only be read again once another sign-in sees it.
- **Everything**: **Remove Autodesk** on Settings → Autodesk deletes your app's ID and secret and every sign-in.
  Which client has which team is kept, for if you set it up again. Delete the app at aps.autodesk.com too.

Every change is in Office Sentry's activity log (`autodesk_app_saved`, `autodesk_sign_in_added`,
`autodesk_team_added` and their removals).

## When something goes wrong

The client's Settings → Autodesk page and **Data collection** show the last read and what failed.

| Message | What to do |
|---|---|
| Autodesk refused the app's client ID or secret | The secret was regenerated or mistyped. Copy it again from aps.autodesk.com and save it on Settings → Autodesk |
| Autodesk refused the personal access token | The token was deleted, or was made for another app. Make a new one for your app and add it as a sign-in, then remove the old one |
| Autodesk says this sign-in isn't an admin of a team with API reporting | The token's person isn't an admin of that team any more, or the team isn't on the Business Success Plan or Enterprise plan |
| No Autodesk sign-in sees it any more | The sign-in that read the team was removed. Add one made by an admin of the team; **Check** links it |
| Use of each product couldn't be read | Autodesk didn't make the usage file. Seats are still shown, and *Not used in 90 days* says *Not read* until the next night |

Autodesk allows 10 export requests a minute per person; Office Sentry waits and tries again when it's busy.
Each team takes two exports a night.
<!-- /since -->
