# CLAUDE.md: Office Sentry

Office Sentry is a read-only, multi-tenant Microsoft 365 **reporting** tool
for MSPs. The whole product is in `v2/`. **Read `v2/CLAUDE.md` before
changing anything**; it has the rules, conventions and commands.

## Layout

```
v2/                 the product (Flask web app + worker, SQLite, Docker)
  CLAUDE.md         rules and conventions for working in v2
  README.md         features, setup, development
.github/workflows/  v2.yml: ruff + pytest on every push and PR
README.md           landing page pointing at v2/
SECURITY.md         vulnerability reporting and security model
CONTRIBUTING.md     development setup
```

## Scope

- Reporting first. Don't add write operations, remediation, incident
  response actions, on-prem agents, GDAP management or a Graph Explorer
  unless the owner asks for them.
- Never write to a tenant: read-only Graph permissions and allow-listed
  `Get-` Exchange cmdlets only (details in `v2/CLAUDE.md`).
- Tenant isolation is mandatory: users only see tenants they're granted,
  and secrets and private keys are never downloadable.

## History

The v1 portal (Flask MSP console, 365Inspect, PowerShell scripts, AD agent)
was removed. Its last state is the git tag `v1-final`; look there if you
need to see how v1 did something, but don't bring its code back wholesale.
