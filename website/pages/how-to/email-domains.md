# Check SPF, DKIM and DMARC for every client domain

A client's domain without SPF, DKIM and an enforced DMARC policy is easy to spoof, which is how a lot of
invoice fraud starts, and the big mailbox providers increasingly turn away mail that lacks them. The records
are public, so they're easy to check for one domain. The trouble is every domain of every client, including
the ones nobody sends from, and noticing when one breaks.

## By hand, for one domain

From PowerShell on Windows (on a Mac or Linux, `dig +short TXT _dmarc.contoso.com` and so on):

```powershell
$domain = "contoso.com"
Resolve-DnsName $domain -Type MX
Resolve-DnsName $domain -Type TXT | Where-Object { $_.Strings -like "v=spf1*" }
Resolve-DnsName "_dmarc.$domain" -Type TXT
Resolve-DnsName "selector1._domainkey.$domain" -Type CNAME
Resolve-DnsName "selector2._domainkey.$domain" -Type CNAME
```

What to look for:

- **SPF**: exactly one `v=spf1` record (two count as none), ending in `-all`, or `~all` while you test.
  Microsoft 365 needs `include:spf.protection.outlook.com`. The whole record, includes and all, may need no
  more than 10 DNS lookups, and every include must exist, or SPF fails for all mail.
- **DKIM**: Microsoft 365 signs with two selectors, so both CNAMEs should exist, and signing must be on in the
  Defender portal (*Email authentication settings*, then *DKIM*). In Exchange Online PowerShell,
  `Get-DkimSigningConfig | Select-Object Domain, Enabled` shows it for every domain.
- **DMARC**: one record at `_dmarc.<domain>`, with a `rua=` address so someone receives the reports. `p=none`
  only reports; spoofed mail is still delivered until it's `p=quarantine` or `p=reject`, with no `pct=` below
  100.
- **A domain that doesn't send mail** (no MX, or a null MX) still needs `v=spf1 -all` and a DMARC record
  with `p=reject`, so nobody can send as it either.

For mail coming in, **MTA-STS** (a TXT record at `_mta-sts.<domain>` and a policy file at
`https://mta-sts.<domain>/.well-known/mta-sts.txt`) makes other servers deliver only over a checked,
encrypted connection, and **TLS-RPT** (a TXT record at `_smtp._tls.<domain>`) reports when they can't.

## Every client, every night

Office Sentry's **Email domains** report checks every verified domain of every client in public DNS each
night: SPF, DMARC, DKIM, MTA-STS and TLS-RPT, with what's wrong with each domain in words and what to publish.

- It catches the subtle failures as well as the missing records: two SPF records, too many lookups, an include
  that doesn't exist, `+all`, a `pct=` below 100, and parked domains without `p=reject`.
- Domains behind a mail gateway such as Mimecast or Proofpoint are told apart, and still expected to have
  DKIM where Microsoft 365 sends for them.
- Five checks (SPF, DKIM, DMARC, MTA-STS and TLS-RPT) fail where a domain falls short, so they reach the
  client's monthly review with the fix, and **Coming up** keeps every domain still at `p=none` in view until
  it's enforced.
- It needs no permission beyond the domain list: everything else is public DNS.

[Open Email domains in the demo](https://demo.officesentry.co.uk/across/domains) ·
[Every check](../../../v2/docs/checks.md) · [Install Office Sentry](../../../README.md#quick-start)
