# CLAUDE.md: Office Sentry

Office Sentry is a read-only, multi-tenant Microsoft 365 **reporting** tool
for MSPs. The whole product is in `v2/`. **Read `v2/CLAUDE.md` before
changing anything**; it has the rules, conventions and commands.

## Layout

```
v2/                 the product (Flask web app + worker, SQLite, Docker)
  CLAUDE.md         rules and conventions for working in v2
  README.md         features, setup, development
.github/workflows/  v2.yml: ruff + pytest on every push and PR; deploy.yml: Hetzner deploys
.github/ISSUE_TEMPLATE/  issue forms (bug, report request, check request)
.github/CODEOWNERS  every pull request asks @JackD99 for review
README.md           product page: features with screenshots, quick start
docs/screenshots/   README images, made by v2/scripts/screenshots.py
examples/           example PDF/XLSX exports from fictional demo data (same script)
SECURITY.md         vulnerability reporting and security model
CONTRIBUTING.md     development setup and how changes are built and checked
CODE_OF_CONDUCT.md  Contributor Covenant 2.1
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
was removed. Its last state is the `v1-final` branch; look there if you
need to see how v1 did something, but don't bring its code back wholesale.
