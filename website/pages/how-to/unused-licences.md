# Find unused and wasted Microsoft 365 licences

Licence waste usually sits in four places, and the first three are quick to find:

1. Licences still assigned to blocked accounts, often leavers.
2. Paid licences that were bought but never assigned.
3. Licences on accounts nobody has signed in to for months.
4. People on a bigger plan than they use: someone on Business Standard who never opens the desktop Office
   apps, or someone on Office 365 E3 who only uses email.

Put a monthly price on each and the total is usually the easiest part of a client review to act on.

## By hand, for one tenant

With the Microsoft Graph PowerShell module:

```powershell
Connect-MgGraph -Scopes "User.Read.All", "Organization.Read.All", "AuditLog.Read.All"

# 1. Licences on accounts that can't sign in
Get-MgUser -All -Filter "accountEnabled eq false" -Property UserPrincipalName, AssignedLicenses |
    Where-Object { $_.AssignedLicenses.Count -gt 0 } |
    Select-Object UserPrincipalName, @{ Name = "Licences"; Expression = { $_.AssignedLicenses.Count } }

# 2. Bought but not assigned
Get-MgSubscribedSku | Select-Object SkuPartNumber,
    @{ Name = "Bought"; Expression = { $_.PrepaidUnits.Enabled } }, ConsumedUnits,
    @{ Name = "Spare"; Expression = { $_.PrepaidUnits.Enabled - $_.ConsumedUnits } }

# 3. Last sign-in of every licensed account (needs Entra ID P1)
Get-MgUser -All -Property UserPrincipalName, AssignedLicenses, SignInActivity |
    Where-Object { $_.AssignedLicenses.Count -gt 0 } |
    Select-Object UserPrincipalName,
        @{ Name = "LastSignIn"; Expression = { $_.SignInActivity.LastSuccessfulSignInDateTime } }
```

Use the last successful sign-in. `LastSignInDateTime` also moves when someone fails to sign in, so a
forgotten account under a password spray looks as if it's in use.

Licences come back as part numbers rather than names (`SPB` is Microsoft 365 Business Premium,
`ENTERPRISEPACK` is Office 365 E3), so keep Microsoft's product names list to hand.

For the fourth kind, the Microsoft 365 admin centre's **Reports → Usage** pages show who used each service
and the desktop Office apps in the last 30, 90 or 180 days. If people show up as long codes instead of names,
untick **Display concealed user, group, and site names in all reports** under **Settings → Org settings →
Services → Reports**.

## Every client, every night

Office Sentry costs the waste at the monthly prices you enter for each licence, so the figure is in money,
for one client or all of them.

![Licence right-sizing: who could move to a cheaper licence, and the saving](../../../docs/screenshots/licence-rightsizing.png)

- **Licence waste** lists licences on disabled and inactive accounts and paid licences nobody is assigned,
  with the monthly cost. The monthly review leads with that figure.
- **Licence right-sizing** lists people who could move to a cheaper licence and the saving: nothing used in
  30 days, only email used, no desktop Office apps used, Copilot unused.
<!-- since 2.6.0 -->
- **Microsoft 365 Copilot** lists the Copilot licences nobody used in 30 days, with what each costs a month,
  the ones bought but not assigned, and the busiest people without Copilot, so an unused licence goes to
  someone who will use it.
<!-- /since -->
- **Licences and subscriptions** shows every subscription with its renewal date, trials about to end and
  group licensing errors in plain words, and **Licence changes** shows who was given or lost a licence each
  month, and by whom.
- The **Clients** page puts the waste for every client side by side, largest first, when you pick this report from its menu.
- **Autodesk licences** does the same for Autodesk seats, for clients on Autodesk's Business Success Plan or Enterprise plan: seats bought but not given out, seats nobody has opened in 90 days, and seats held by people whose Microsoft 365 account is blocked or gone ([setting it up](../../../v2/docs/autodesk.md)). <!-- since 2.6.0 -->

[Open Licence waste in the demo](https://demo.officesentry.co.uk/across/licences) ·
[Example PDF](../../../examples/licence-right-sizing.pdf) ·
[Install Office Sentry](../../../README.md#quick-start)
