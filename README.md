# Office Sentry

Read-only Microsoft 365 reporting for MSPs and IT teams. Office Sentry
collects every client tenant nightly, keeps the history, and turns it into
the reports Microsoft doesn't give you in one place: a monthly security and
licensing review with month-over-month changes, mailbox forwarding and
access, app consents, Conditional Access coverage, email domain health, a
cyber-insurance evidence pack and a cross-tenant overview.

It connects with one multi-tenant Entra app that has read-only permissions
and signs in with a certificate. It never changes anything in a tenant.

**Everything lives in [`v2/`](v2/).** Setup, Docker quick start, the report
list and the development guide are in [v2/README.md](v2/README.md).

## Quick start

```bash
cd v2
cp .env.example .env          # set OFFICESENTRY_BASE_URL at least
docker compose up -d --build
```

Then open the portal, create the first admin, and follow the steps in
[v2/README.md](v2/README.md#quick-start-docker).

## The old v1 portal

The original Flask portal (MSP operations, 365Inspect, PowerShell scripts,
AD agent, GDAP management, Graph Explorer) has been removed. Its last state
is kept at the git tag `v1-final`:

```bash
git checkout v1-final
```

## More

- [SECURITY.md](SECURITY.md): reporting a vulnerability and the security model
- [CONTRIBUTING.md](CONTRIBUTING.md): development setup and conventions
