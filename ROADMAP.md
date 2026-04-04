# Office Sentry — Roadmap

## Vision

A self-hosted CIPP alternative — a full MSP management platform for M365 security auditing, administration, compliance enforcement, automated remediation, and client-facing reporting. Built on Python + Flask, moving away from PowerShell dependencies toward a pure Python stack.

## Design Principles

- **Python-first**: New features use Python + Microsoft Graph directly. No new PowerShell scripts. Migrate existing PS scripts incrementally.
- **Works everywhere**: macOS, Linux, Windows — no platform-specific dependencies (PowerShell, PKI modules, etc.).
- **Self-hosted simplicity**: SQLite by default, optional Redis/Celery for scale. Docker for easy deployment.
- **Server-rendered UI**: Jinja2 + Bootstrap 5 + vanilla JS. No SPA, no build step. HTMX where interactivity demands it.
- **Security by default**: Nonce-based CSP, Fernet encryption, RBAC, HMAC-signed agent comms.

---

## Current State

**Completed (Phases 1–3)**:
- Security hardening — nonce-based CSP, PBKDF2 key derivation, SRI hashes, HSTS, security headers
- CI/CD — GitHub Actions running tests, linting, security scanning
- Test suite — 24 files, 688+ tests covering all 21 blueprints
- Celery + Redis task queue with threading fallback
- SSE real-time job streaming
- 5-tier RBAC (super_admin, admin, operator, viewer, client)
- REST API v1 with Bearer token auth
- Webhook system with HMAC-signed delivery
- AD Agent system (HMAC-authenticated, on-prem hybrid identity)
- PDF report generation (WeasyPrint), SMTP email delivery, report comparison
- Client-facing dashboard with per-tenant branding

**In Progress**:
- PowerShell → Python migration (19 scripts migrated via `graph_client.py` + `graph_jobs.py`)
- CSP inline handler cleanup (all `onclick` attributes converted to `addEventListener`)

**Scale**: 21 blueprints, 95 templates, 33 DB tables, 45 PS scripts, 108 security inspectors, ~1,720-line CSS design system

---

## Phase 4: Python Migration & Platform Independence

**Goal**: Eliminate PowerShell as a runtime dependency. All Graph API operations run natively in Python.

**Why this is the priority**: PowerShell is the biggest barrier to development velocity and cross-platform deployment. Every PS script is a subprocess with no native error handling, no type safety, and requires `pwsh` + modules installed. Python replacements are faster, testable, and work everywhere.

### 4.1 Complete Graph API Migration

The `GraphClient` class (`portal/graph_client.py`) and `graph_jobs.py` already replace 13 PS scripts. Continue migrating the remaining `Get-*.ps1` scripts — they're all Graph API calls.

| Script | Replacement Function | Status |
|--------|---------------------|--------|
| `Get-TenantDomains.ps1` | `fetch_tenant_domains()` | Done |
| `Get-HybridStatus.ps1` | `detect_hybrid_status()` | Done |
| `Get-SecureScore.ps1` | `fetch_secure_score()` | Done |
| `Get-AuthMethods.ps1` | `fetch_auth_methods()` | Done |
| `Get-PartnerRelationships.ps1` | `fetch_partner_relationships()` | Done |
| `Get-AppConsentRequests.ps1` | `fetch_app_consent_requests()` | Done |
| `Get-AppRegistrations.ps1` | `fetch_app_registrations()` | Done |
| `Get-DirectoryAuditLogs.ps1` | `fetch_directory_audit_logs()` | Done |
| `Get-ConditionalAccessPolicies.ps1` | `fetch_conditional_access()` | Done |
| `Get-ServiceHealth.ps1` | `fetch_service_health()` | Done |
| `Setup-App.ps1` | `create_entra_app()` | Done |
| `Setup-RemoveApp.ps1` | `remove_entra_app()` | Done |
| `Get-SignInLogs.ps1` | `fetch_sign_in_logs()` | Done |
| `Get-UserReport.ps1` | `fetch_user_report()` | Done |
| `Get-LicenseReport.ps1` | `fetch_license_report()` | To do |
| `Get-IdentityProtection.ps1` | `fetch_identity_protection()` | To do |
| `Get-IntuneDevices.ps1` | `fetch_intune_devices()` | To do |
| `Get-EmailProtectionPolicies.ps1` | `fetch_email_protection()` | To do |
| `Get-EmailThreatReport.ps1` | `fetch_email_threats()` | To do |
| `Get-TenantBPA.ps1` | `fetch_tenant_bpa()` | Done |
| `Get-TransportRules.ps1` | `fetch_transport_rules()` | To do |
| `Get-QuarantineMessages.ps1` | `fetch_quarantine()` | To do |

### 4.2 Exchange Online Migration

Exchange scripts (`Get-MessageTrace.ps1`, `Search-UnifiedAuditLog.ps1`, `Audit-InboxRules.ps1`) use the Exchange Online PowerShell module. Replace with direct REST API calls to the Exchange Management API or Graph API equivalents where available.

This is the hardest migration — Exchange APIs have gaps compared to the PowerShell module. Prioritise what's available via Graph first.

### 4.3 PnP SharePoint Migration

SPO audit scripts (`Audit-SitePermissions.ps1`, `Audit-ExternalSharing.ps1`, `Audit-Groups.ps1`) use PnP.PowerShell. Replace with SharePoint REST API calls + Graph Sites API.

### 4.4 365Inspect Integration

The 108 security inspectors use Graph + Exchange PowerShell. Long-term, rewrite the most critical inspectors in Python. Short-term, keep the PS orchestrator but make it optional — the BPA system already covers many of the same checks.

### 4.5 Remove PowerShell Runtime Requirement

Once all critical paths are migrated, make `pwsh` optional:
- `helpers.py` — `start_job()` / PS subprocess code becomes legacy
- `setup.sh` / `start.cmd` — no longer need to check for pwsh
- Docker image — drops the PowerShell layer, halving image size

---

## Phase 5: Operations & Automation

**Goal**: Automated remediation, approval workflows, drift detection.

### 5.1 Automated Remediation

Add a "Fix" button to BPA results for safe, low-risk remediations:
- Enable unified audit log
- Disable legacy authentication
- Block external email forwarding
- Enable mailbox auditing
- Set password expiry policy

Implementation: Python functions in a `remediation.py` module using `GraphClient`. Each remediation has a dry-run mode. Remediation log table tracks before/after state.

### 5.2 Drift Detection

Celery Beat task runs BPA against tenants on schedule, compares against previous results, creates alerts on drift. Triggers webhooks (`bpa.drift_detected` event already defined).

### 5.3 Approval Workflows

Gate Tier 2+ operations behind approval for non-super_admin users:
- `change_requests` table with pending/approved/rejected status
- Approval queue UI
- Auto-execute on approval via Celery
- Auto-expire after 72 hours

### 5.4 Tenant Onboarding Wizard

Multi-step guided flow: create tenant → generate cert → create Entra app (device code flow) → grant consent → run initial BPA + security scan. Also support bulk onboarding via CSV import.

---

## Phase 6: Integrations & Notifications

**Goal**: Connect to the MSP ecosystem.

### 6.1 PSA Integration

Abstract connector interface for ConnectWise, Autotask/Datto, HaloPSA:
- Auto-create tickets from alerts
- Bi-directional client sync
- Map alert severity → ticket priority

### 6.2 Notification Channels

Teams, Slack, and email notifications triggered by alerts. Per-user notification preferences. Uses existing webhook infrastructure for delivery.

### 6.3 External Data Consumers

- Power BI connector (REST API endpoints optimized for data refresh)
- Scheduled CSV/JSON exports
- Prometheus metrics endpoint (`/metrics`) for Grafana

---

## Phase 7: Compliance & Governance

**Goal**: Regulatory compliance framework mapping and evidence collection.

### 7.1 Compliance Frameworks

Map BPA checks to framework controls (many-to-many):
- CIS Microsoft 365 Benchmark
- Australian ASD Essential Eight
- UK Cyber Essentials
- Custom framework import/export

Compliance score: `(passing controls / total controls) × 100` per framework per tenant.

### 7.2 Policy Governance

- Desired state configuration — define target tenant config, detect deviations
- Policy exceptions with expiry and re-review alerts
- Tenant groups inherit policies with per-tenant overrides

### 7.3 Compliance Reporting

Client-facing compliance PDF reports per framework, using the existing report engine.

---

## Phase 8: Scale & Production Hardening

**Goal**: Containerization, database options, observability, auth enhancements.

### 8.1 Docker Support

Multi-stage Dockerfile: Python runtime + optional PowerShell layer. `docker-compose.yml` with app, worker, beat, and Redis services.

### 8.2 PostgreSQL Support

Make database backend configurable via `DATABASE_URL` env var. Keep SQLite as default. Use SQLAlchemy incrementally — migrate table by table alongside raw SQL.

### 8.3 Observability

- Structured JSON logging
- Prometheus metrics endpoint — request latency, job duration, error rates, queue depth
- Expanded `/health` endpoint — verify Redis, Celery, database connectivity

### 8.4 Authentication Enhancements

- TOTP MFA (`pyotp`)
- OIDC/SAML login via Entra ID (`authlib`)
- Session management — active session list, remote revocation
- Configurable password policies

---

## Architecture Improvements (Ongoing)

These can be done incrementally alongside feature work:

| Area | Action | Effort | Impact |
|------|--------|--------|--------|
| database.py (3,100 lines) | Incremental SQLAlchemy migration | Medium | High — proper migrations, Postgres support |
| Large route files | Split `reports.py`, `operations.py`, `standards.py` into sub-packages | Low | Medium — maintainability |
| Redis caching | Cache Graph API responses, session storage, rate limiting | Low | Medium — performance |
| HTMX | Add to interactive pages (Graph Explorer, device actions) as needed | Low | Medium — UX without SPA complexity |

---

## Phase Dependencies

```
Phase 4 (Python Migration) ← IN PROGRESS
    │
    ├──► Phase 5 (Operations & Automation)
    │        │
    │        └──► Phase 7 (Compliance)
    │
    ├──► Phase 6 (Integrations)
    │
    └──► Phase 8 (Scale & Hardening) ← can start in parallel
```

---

## Priority Matrix

| Phase | Business Impact | Effort | Status |
|-------|:---:|:---:|:---:|
| ~~1. Foundation~~ | High | Medium | Done |
| ~~2. Core Platform~~ | Critical | Large | Done |
| ~~3. Reporting~~ | High | Medium | Done |
| **4. Python Migration** | Critical (removes platform barrier) | Large | In progress |
| **5. Operations & Automation** | High (MSP efficiency) | Large | Next |
| **6. Integrations** | Medium (ecosystem) | Large | After 5 |
| **7. Compliance** | Medium (regulated verticals) | Medium | After 5 |
| **8. Scale & Hardening** | Medium (growth) | Large | Parallel |
