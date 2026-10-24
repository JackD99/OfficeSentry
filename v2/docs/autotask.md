<!-- since 2.6.0 -->
# Autotask PSA

For MSPs who run their service desk in Datto Autotask PSA: **a ticket in your Autotask for the Office Sentry
alerts you choose**, in the client's own company, so new findings, risky changes in a client's tenant and failing
collections land in the queue your technicians already work from.

It's off until you connect it under *Settings › Autotask PSA*. It works with or without the Teams, Slack or webhook
alert on *Settings › Alerts*: each alert goes to every place that's set up.

## What happens

- **One ticket per alert.** All of a client's new findings from one collection go in one ticket; so do the risky
  changes found in one collection. The ticket's title is the alert's ("Contoso: 3 new findings"), and its
  description lists each finding or change with a link back to the client in Office Sentry.
- **In the client's company.** You match each client to a company in your Autotask once. A client without one
  files its tickets in your own company, or gets none, as you choose. Problems with Office Sentry itself (the
  worker stopped, a backup failed, a certificate about to expire) go to your own company.
- **The priority follows the severity.** You pick the Autotask priority for critical, high, medium and low. A
  ticket for findings or changes takes the worst of them; a failing collection is medium; problems with Office
  Sentry are high (an expired certificate critical).
- **One ticket per problem.** A collection that keeps failing, or a problem with Office Sentry that lasts for days,
  makes one ticket, not one a day. When it clears (the collection works again, the problem is fixed) Office Sentry
  adds a note to the ticket saying so and leaves closing it to you. If it comes back after that, it's a new ticket.
- **Tried again if Autotask doesn't answer.** A ticket Autotask didn't take because it was down, slow or over its
  request limit is sent again after 5 minutes, 30 minutes, 2 hours and 8 hours. One Autotask refused (a field
  your ticket category requires, say) isn't: the error shows on the settings page, with *Try again* once you've
  fixed it.

Which alerts exist at all is still set on *Settings › Alerts* (the kinds of change in a client's tenant, whether a
failing collection or Office Sentry's own problems alert). The Autotask page picks which of them make a ticket,
and the least severe new finding that does.

## Setting it up

1. **Add an API user in Autotask.** In Autotask, go to *Admin › Account Settings & Users › Resources/Users*, and
   choose *New API User* (not a normal resource: an API user doesn't take a licence). Give it the security level
   **API User (system)**, and under *API Tracking Identifier* choose *Integration vendor: Custom (Internal
   Integration)*. Autotask shows a tracking identifier: copy it. Set a secret for the user and copy that too.
2. **Connect it.** In Office Sentry, open *Settings › Autotask PSA*, enter the API user's user name, its secret and
   the tracking identifier, and save. Office Sentry finds your Autotask's zone from the user name, then reads your
   active companies and the choices for a ticket's status, priority, queue, type and source.
3. **Choose the ticket defaults.** A status (Autotask's default status, usually *New*, is picked for you) and a
   priority for each severity (picked by name where yours are called Critical, High, Medium and Low). A queue, a
   ticket type (*Alert* suits) and a source (*Monitoring Alert*, if you have one) are optional; without them the
   ticket category decides. If your ticket category needs a due date, give one in hours.
4. **Match clients to companies.** Under *Clients and their companies*, type or pick each client's Autotask
   company. Clients whose name matches exactly one company's (leaving out "Ltd", "Inc" and punctuation) are filled
   in and marked *Suggested*: check them and press *Save companies*.
5. **Test it.** *Create a test ticket* makes a ticket in your own company with your choices, so you see straight
   away whether your Autotask accepts them. Then tick *Create tickets* and save.

*Check connection* reads the companies and the choices again: press it after adding a company or a queue in
Autotask. A client's company that's been made inactive in Autotask keeps its match, marked *Not active in
Autotask*, so its tickets don't quietly go elsewhere.

## What it sends to Autotask

Every request goes to Autotask's own servers (`webservices*.autotask.net`), as the API user, with its user name,
secret and tracking identifier in the request headers.

| Call | What it does |
|---|---|
| `GET /atservicesrest/v1.0/zoneInformation?user=…` | Finds which Autotask zone your database is in. Needs no sign-in |
| `GET /v1.0/Tickets/entityInformation/fields` | The choices for a ticket's status, priority, queue, type and source |
| `GET /v1.0/TicketNotes/entityInformation/fields` | The choices for a note's type and who sees it |
| `GET /v1.0/Companies/query` | Your active companies: their IDs, names and types, nothing else |
| `POST /v1.0/Tickets` | Creates a ticket for an alert, or the test ticket |
| `GET /v1.0/Tickets/{id}` | Reads the new ticket's number (T20261024.0001) to show beside it |
| `POST /v1.0/TicketNotes` | Adds a note to a ticket when its problem clears |

Creating tickets and notes are the only writes, and they're in your own Autotask. Nothing is ever changed, closed
or deleted there, and nothing about Autotask reaches a client's Microsoft 365 tenant. Autotask allows 10,000
requests an hour for your whole database; Office Sentry makes a handful per alert.

## What's kept, and who sees it

- The API user's secret and tracking identifier are encrypted with Office Sentry's keys (`rotate-keys`
  re-encrypts them), never shown again after saving, and never in the support package.
- Your company list (IDs and names), each client's matched company, and a record of each ticket: when, which
  client, its title, the ticket as sent (so it can be sent again) and its number or the error. Records are kept for
  400 days; deleting a client deletes its match and its records. The tickets themselves stay in Autotask.
- Every ticket created or failed is in the activity log (*alert sent* or *alert failed*, "via autotask"), with
  the settings changes and each change to a client's company.
- The settings page needs the *Manage settings* capability, which only takes effect for someone who sees every
  client.

## When it doesn't work

| What you see | What to do |
|---|---|
| *Autotask doesn't know that user name* | Enter the API user's user name exactly as Autotask shows it (it looks like an email address) |
| *Autotask refused the API user (HTTP 401)* | Check the secret and the tracking identifier, and that the user is active and an API user |
| *Autotask didn't let the API user do this (HTTP 403)* | Give the API user the API User (system) security level |
| *Missing Required Field: dueDateTime* (or another field) | Your ticket category needs it: set *Due in*, or the queue, then *Try again* |
| *Autotask's limit on requests was reached (HTTP 429)* | Another integration is using your database's hourly limit; the ticket is tried again later |
| No ticket for a client | Check *Which alerts make a ticket* and the severity, that the alert is on under *Settings › Alerts*, and whether clients without a company get tickets |
<!-- /since -->
