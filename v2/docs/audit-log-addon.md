<!-- since 2.4.0 -->
# The audit log add-on

For MSP admins who want to answer the questions clients ask after something goes wrong: **who deleted,
downloaded or shared that file, who opened someone else's mailbox, and who created that inbox rule.**
Those answers are in the client's Microsoft 365 unified audit log, which the main Office Sentry app can't
read. The add-on reads it, for the clients you choose and who approve it.

It's off until you set it up, and nothing about it shows anywhere (clients' Settings tabs, the onboarding
tracker, the reports) until then.

## What it is

- **A second app registration**, "Office Sentry audit log add-on", separate from the shared read-only app,
  with its own certificate and exactly one permission: `ActivityFeed.Read` on the Office 365 Management
  APIs (*Read activity data for your organization*). It holds none of the main app's permissions.
- **Approved per client.** A client's Global Administrator approves it the same way they approved the main
  app: a link, Microsoft's consent page, a thank-you page. A client who doesn't approve it loses nothing
  they have today, and the main app's permissions don't change.
- **About 70 kinds of audit record are kept** out of the thousand or so Microsoft logs for SharePoint,
  OneDrive and Exchange: files deleted, restored, downloaded, moved and copied; sharing links and
  invitations; mail read, sent or deleted by someone other than the mailbox's owner; inbox rules, forwarding
  and mail flow rules; mailbox and folder permissions and mailbox exports; and changes to auditing itself.
  Files opened or edited, and mail read by its owner, are left out. OneDrive sync downloads and uploads are
  counted per person and day rather than kept one by one. Email subjects are never stored.

It never changes anything in a client's tenant, and never reads email, attachments, file contents, chats or
channel messages: the audit log says *that* a file was downloaded and by whom, not what was in it.

## The two calls that aren't reads

Every request the add-on makes reads, except two that Microsoft's feed needs. They're the only exception
to Office Sentry's "GET only" rule, and both are in `tests/test_no_writes.py`'s allow-list with the reason.

| Call | When Office Sentry sends it | What it does |
|---|---|---|
| `POST .../activity/feed/subscriptions/start` | Once per kind of record (SharePoint and OneDrive, Exchange) for a client you asked for, after Office Sentry has confirmed the client approved the add-on | Asks Microsoft to make the client's new audit records available to the add-on's app |
| `POST .../activity/feed/subscriptions/stop` | For each kind of record that may still be on (a start that worked for one kind and not the other counts), when you switch the add-on off for a client or delete the client, or when Microsoft says the client's feed is off for one kind (the other is stopped too) | Asks Microsoft to stop |

Neither touches a user, mailbox, file, group, policy or setting, and the audit log itself carries on as
before. A nightly collection never sends either: when it finds part of a feed off, a task sends the stop
for the rest afterwards. Both are in Office Sentry's activity log
(`audit_feed_started`, `audit_feed_stopped`), and in the client's own logs at Microsoft.

## 1. Create the add-on's app

You need someone who can create app registrations in your own (the MSP's) tenant: a Global Administrator,
Application Administrator or Cloud Application Administrator.

1. Open **Settings → App connection → Audit log add-on** (or go to `/admin/audit-addon`).
2. Press **Sign in to Microsoft**, enter the code on Microsoft's page and sign in. Office Sentry uses that
   sign-in once, to create the app, and doesn't keep it.
3. Office Sentry creates the app with its one permission and its own certificate, and switches the add-on
   on. That only adds an **Audit log add-on** page to each client's Settings tab; nothing is read from any
   client yet.

If your Conditional Access blocks device sign-in, open **Create the app by hand instead** on the same page:
generate the add-on's certificate there, create the app in the Entra admin centre as the page describes
(multi-tenant, the redirect URI it shows, the .cer file, `ActivityFeed.Read` under *Office 365 Management
APIs → Application permissions* and nothing else), then paste its client ID into its card on **App
connection**.

The add-on's certificate appears on **App connection** beside the shared app's, with the same expiry
warnings, and is replaced the same way. Its test only signs in; it reads nothing. Changing the add-on
app's client ID means every client has to approve the new app: they're all shown as Off, with the reason,
until you send them the link again.

Microsoft doesn't offer the feed in China (21Vianet). For GCC High or DoD clients, create the app in that
cloud too (**More options** before signing in).

## 2. Ask a client

1. Open the client, then **Settings → Audit log add-on**.
2. Send their Global Administrator the add-on link, with the email text and the one-page pack from the same
   page (the pack says what the add-on reads and never reads, how long it's kept, and how they remove it).
   Press **Mark as sent**.
3. The client approves on Microsoft's consent page and lands on the thank-you page.
4. Office Sentry confirms the approval by signing in as the add-on app and reading the client's feed
   subscriptions (one GET), checking every hour until it can, or now with **Check now**. Once it's
   confirmed, it starts the feed and queues the first collection. A paused client isn't signed in to until
   it's resumed.

The onboarding tracker's **Audit log** column says whether each client's audit log is recording at all,
and under it where the client has got with the add-on (Not asked, Sent, Approved, On, Off; *Approved,
confirming* while Office Sentry hasn't signed in yet). If the audit log is off, the add-on has nothing to
read, so have the client turn it on in Microsoft Purview first.

Microsoft can take up to 12 hours after the feed starts to make the first records available, and doesn't
give the past through the feed: the report fills from the day the feed started.

## 3. What you get

- **Files and mailbox access**, a detail report under *Files and Teams*, only for clients the add-on was asked
  for (its link marked as sent, or switched on; a client whose admin approved unasked gets it once you switch
  it on). It leads with figures for the last 30 days (files deleted and downloaded, anyone links created,
  mailboxes opened by someone else, inbox rules changed, forwarding set) with their trends, then **Needs
  attention** (an admin opening someone else's mailbox, forwarding, inbox and mail flow rules that forward,
  redirect, copy or delete mail,
  anyone links, auditing turned off, mailbox exports, malware, and mass deletes of 100 files or more by
  one person in an hour; a file counts once when it's deleted, not again when a recycle bin is emptied),
  then a table per kind of activity, who did most, and OneDrive sync per person
  and day. Like every detail report it has filters, saved views, PDF, XLSX and CSV, the All clients rollup
  and schedules.
- **Each person's Timeline** lists what they did to files, sharing and mailboxes, and what someone else did
  to their mailbox.
- **The client's Activity history page** says how many records are kept and where the add-on is up to.

The records are read every night, with the rest of the client's collection. Microsoft keeps them in the
feed for 7 days; if a client isn't collected for longer than that, or its first collection comes more than 7
days after the feed started, the report says which hours couldn't be read.

## 4. How long it's kept

**Files and mailbox events**, under **Settings → Data retention**: 2 years by default, from 6 months to 7
years or forever. They're stored with the rest of the activity history, in monthly files beside the
database, and backed up and restored with it.

## 5. Switching it off

- **For one client:** **Settings → Audit log add-on → Switch off** on the client. Nothing more is
  collected; Office Sentry sends the stop calls, for whatever of the feed may be on, even if starting it
  only half worked. What was collected stays until retention removes it, unless you tick **Also delete the
  events collected**. **Switch on again** starts it again without asking the client, as long as they haven't
  removed the app; while the stop calls are still being sent, the page says so and switching on waits. If
  Microsoft refuses a stop at your end (the add-on app's certificate, an outage), the page says why and
  Office Sentry sends it again every hour for a week.
- **By the client:** removing "Office Sentry audit log add-on" from *Enterprise applications* in their
  tenant. Office Sentry notices at the next collection and shows the client as Off with the reason. Their
  approval went with the app, so switching it on again needs the link sent and approved again.
- **At Microsoft:** if someone in the client's tenant turns the feed off for one kind of record, the next
  collection shows the client as Off with the reason, and Office Sentry stops the other kind too, so nothing
  is left on that nobody reads.
- **Deleting a client** stops whatever of its feed may be on, and deletes what was collected with everything
  else.
- **Everywhere:** the switch on **Settings → App connection → Audit log add-on** hides the add-on
  everywhere: its report, its pages, people's Timelines, report designs (a design that has its table leaves
  it out), and scheduled emails and month-end plans (a schedule that sends its report, or a saved view of
  it, sends the rest without them). It can't be switched off while any client's
  feed is on or being started, archived clients included (the page names them and lists them); switch those
  off first.

## When something goes wrong

The client's **Audit log add-on** page says what Microsoft answered, in words. The usual ones:

| What it says | What to do |
|---|---|
| The client hasn't approved the add-on yet, or has removed it from their tenant | Send the link again, or wait for the hourly check if they've only just approved |
| Microsoft's audit log feed refused, and Office Sentry tries again in 20 minutes | Usually nothing. If it keeps happening, check the tracker's **Audit log** column: Microsoft won't start a feed for a client whose audit log is off, so have them turn it on in Microsoft Purview (*Audit → Start recording user and admin activity*), which can take a few hours to take effect |
| Office Sentry couldn't reach Microsoft's audit log feed | Nothing: it tries again shortly |
| A problem with the add-on app's certificate | Replace the certificate on **App connection**, as for the shared app |
<!-- /since -->
