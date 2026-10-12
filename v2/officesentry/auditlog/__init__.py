"""The audit log add-on: who deleted, downloaded or shared files, who opened someone else's mailbox and who
created mail rules, from each opted-in client's Microsoft 365 unified audit log.

It's a second app registration, separate from the shared read-only one, holding exactly one permission
(ActivityFeed.Read on the Office 365 Management APIs), approved per client. Off until an admin creates the
add-on app (the ``audit_addon`` setting), and then only for the clients they ask for it.

* ``feed.py``: the Office 365 Management Activity feed client, GET to read plus the two POST calls that start
  and stop a client's feed. The only file here that sends anything but GET.
* ``addon.py``: the switch, the add-on app, and each client's opt-in (``audit_feeds``): asking, confirming
  the client's approval by signing in, starting and stopping the feed.
* ``jobs.py``: the periodic job that confirms approvals and retries starts.
* ``operations.py``: the audit operations kept, as data.

The records go into the activity history (``events.py``, store ``activity``) through the ``audit_activity``
collector, and show in the Files and mailbox access report and on each person's page.
"""
