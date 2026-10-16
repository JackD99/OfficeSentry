<!-- since 2.5.0 -->
# Client grades

Every client gets a grade from **A** to **E**: one letter to put in front of a client, worked out the same
way for everyone, with the rules published so you can explain exactly why a client is a C and what would
make it a B. The grade sits beside the client's name on every page about them, first among the figures on
their Overview, and on its own page (**Grade**, from that figure) with how it was worked out, twelve months
of history and how the client compares with others you look after.

## How a grade is worked out

**Five parts.** The checks are grouped into five parts. Checks about cost, capacity and housekeeping
(unused licences, storage quotas, directory sync, a blocked manager) say nothing about how exposed a client
is, so they don't count towards a grade; they stay on the Findings tab and in the monthly review.

| Part | What it covers |
|---|---|
| Admin accounts | Who holds admin roles: MFA registered and required for every admin, phishing-resistant methods, how many Global Administrators, guests and apps with admin roles, cloud-only admins, and changes to how domains sign in |
| Sign-in and accounts | Everyone else's sign-in: MFA enforced and registered, legacy sign-in methods, text or call as the only second factor, risky sign-ins and at-risk accounts, inactive accounts, password hash sync, and open Defender alerts |
| Mail | Forwarding outside the organisation and inbox rules that hide mail, mail flow rules, spam, spoof and malware filtering, the audit log and mailbox auditing, SMTP AUTH, shared mailbox sign-in, and each domain's SPF, DKIM, DMARC, MTA-STS and TLS-RPT |
| Devices | Intune enrolment, compliance policies and whether devices meet them, encryption and saved BitLocker keys, Windows versions still getting updates, devices checking in, old device records, and LAPS |
| Sharing and apps | Anyone links and sharing outside the organisation, site owners and guests with full control, who can invite guests and create groups, ownerless groups and group expiry, what users may do with apps and registrations, and third-party apps' permissions and credentials |

The Grade page lists every check in each part under *What each part checks*.

**A part's grade is its worst open finding.** A critical finding makes the part an **E**, a high finding a
**D**, a medium one a **C**, only low findings a **B**, and a part whose every check passed is an **A**.
Accepted risks are set aside first, so a risk the client has signed off doesn't count against them, and an
*info* finding is a note, not a problem, so it doesn't count either. Each finding counts at its check's
severity, the one shown on the Findings tab.

**A part needs half its checks.** A part is only graded when at least half its checks could run. A client
without Intune has no Devices grade, a client without Entra ID P1 may have no Sign-in grade, and nothing is
graded before the first collection. The part says why, and the grade comes from the parts that were graded;
if fewer than half of all the counted checks could run, the page says so beside the letter.

**The grade is the worst part.** One critical gap keeps a client out of A and B, whatever else is in good
shape. Each part says what moves it: fix the checks at its worst level and it takes its next grade. The
Grade page's *Next step* is the grade the client gets once the worst part's checks are fixed, and *How the
grade is worked out* lists those checks.

Only `admin_mfa` (an admin without MFA) is ever critical, so an E always means that: every other check is at
most high. The [check catalogue](checks.md) has every check's severity.

## Twelve months of grades

The Grade page shows the grade on each of the last twelve month-ends, with this month so far at the end,
and says how it moved ("Up from D in July"). Nothing extra is stored for it: each month's grade is worked
out from the findings that were open at that month-end (first seen by then, not yet fixed, and not an
accepted risk at the time), using each finding's current severity and the parts that are graded today.
Months before the client's first collection aren't graded. Because resolved findings are kept
(*Settings → Data retention*), the history reaches back as far as they do.

## Compared with your other clients

The Grade page sets the client beside the other clients you can see, so you can say "better than 7 of the
11 clients of this size we look after" without naming any of them:

- Clients are compared with others of about the same size by licensed users (up to 24, 25 to 99, 100 to
  499, 500 or more). When fewer than two other clients are that size, every other client is used instead,
  and with fewer than two other clients in all, nothing is said.
- The figures are the median grade, Secure Score, users with MFA, open critical and high findings, and
  licence waste per licensed user a month, each marked better, worse or the same as the median. You only see
  the figures of the report areas you can open on the client.
- The others' figures are what was last worked out for the Clients list (they're refreshed after each
  collection and once a day); paused and archived clients are left out.
- Nothing about any client leaves your install, and no other client is ever named.

## Who sees it

The grade shows to staff roles that can see **Security findings and reviews** on the client, on the pages
they already open; a first-line role without findings sees no grade. Clients don't see the Grade page.
<!-- /since -->
