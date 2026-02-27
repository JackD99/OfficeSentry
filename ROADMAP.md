# SPO-Audit-Toolkit — Comprehensive Improvement Roadmap

## Executive Summary

The SPO-Audit-Toolkit has grown from a SharePoint audit script collection into a substantial M365 management portal — **18 route blueprints, 84 templates, 42 PowerShell scripts, 108 security inspectors, and a 24-table SQLite database**. However, the architecture, testing, and infrastructure haven't kept pace with feature expansion.

**Vision**: Transform this into a **full MSP management platform** — M365 administration, compliance enforcement, automated remediation, incident response, client-facing reporting, and PSA/RMM integration. A self-hosted CIPP alternative built on Python + PowerShell.

**Deployment model**: Multi-instance self-hosted. Each MSP runs their own instance. Docker + PostgreSQL support are priorities; multi-org SaaS is not.

**Strategy**: Core platform architecture first (task queue, RBAC, API, real-time), then expand features on a solid foundation.

---

## Current State Audit

### Metrics

| Component | Count | Detail |
|-----------|-------|--------|
| Route blueprints | 18 | 7,085 lines across `portal/routes/` |
| Templates | 84 | Bootstrap 5 dark theme, Jinja2 |
| Database tables | 24 | 2,129 lines in `portal/database.py` |
| PowerShell scripts | 42 | `scripts/` directory |
| Security inspectors | 108 | `inspectors/Inspectors/` (365Inspect) |
| CSS design system | 1,721 lines | Custom dark theme in `portal/static/css/portal.css` |
| Test lines | 2,666 | 6 test files, ~152 test cases |
| Python dependencies | 4 | Flask, cryptography, Flask-WTF, Flask-Limiter |

### What's Working Well

**Core Audit Pipeline**
- 5 SPO audit types (groups, permissions, external sharing, OAuth consent, tenant-wide permissions)
- 5 breach investigation types (unified audit log, inbox rules, sign-in logs, OAuth consent, message trace)
- 108-inspector security scanning via 365Inspect framework
- Background job execution with PS subprocess output streaming
- Interactive report viewer (CSV/JSON) with saved views, column customization, export

**MSP Management**
- Tenant CRUD with certificate-based auth (cert generation + Entra app creation via PowerShell)
- 50+ tiered MSP operations across 4 categories (User, Exchange, Teams, SharePoint)
- BPA with 50+ built-in checks, standards templates, tenant assignment
- Secure Score tracking across tenants
- Alert rules and history system
- Tenant groups, custom variables, configuration backup/restore
- GDAP relationship management (onboarding/offboarding with role templates)
- Conditional Access policy viewing and template management
- Graph Explorer, GeoIP lookup, Domain Health tools
- Batch operations across multiple tenants simultaneously

**Security & UI**
- CSRF protection (Flask-WTF) on all POST routes
- Rate limiting on login (Flask-Limiter)
- Security headers (CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy)
- Fernet encryption for PFX passwords at rest
- Modern dark UI with custom design system and Chart.js dashboard analytics
- Onboarding wizard, responsive sidebar with collapsible sections

**Newer Modules (partially complete)**
- Email Security: quarantine, transport rules, anti-phishing, Defender policies, threat reports
- Identity Protection: risky users, risky sign-ins, MFA gap analysis, risk overview
- Device Management: Intune inventory, compliance, Autopilot, device actions
- Service Health: status, incidents, message center, planned maintenance
- License Management: inventory, usage analysis, optimization recommendations
- Incident Response: BEC, account compromise, data exfiltration, malware, privilege escalation playbooks

### Critical Gaps

**Architecture**
- SQLite only — no concurrent writer support, no connection pooling, limits multi-process deployments
- Jobs run via `threading.Thread` — no retry, no timeout enforcement, no persistence across restarts
- Report schedules exist in DB (`report_schedules` table) but nothing ever executes them
- Alert rules exist in DB (`alert_rules` table) but no evaluation engine triggers them
- In-memory rate limiter state resets on restart
- No API authentication — no bearer tokens, no API keys, no programmatic access
- Frontend polls every 60 seconds — no real-time updates
- Only 4 Python dependencies — missing task queue, ORM, email, PDF generation

**Security**
- CSP allows `'unsafe-inline'` for scripts and styles
- Key derivation uses plain SHA256 (`database.py:24`) instead of PBKDF2/scrypt
- No HTTPS enforcement or HSTS header
- No SRI hashes on CDN resources (Bootstrap, Chart.js)
- Binary auth model — only admin/non-admin, no granular RBAC
- No SSO/SAML for portal authentication
- PFX passwords passed via environment variables

**Testing & DevOps**
- No CI/CD pipeline
- No Docker support
- Tests cover auth, database, routes, security, operations, standards, tenant_admin — but 11 of 18 blueprints have zero test coverage
- No PowerShell Pester tests
- CLAUDE.md documents 5 blueprints when 18 exist — severely outdated

**MSP Platform**
- No PDF report generation or client-facing output
- No email/webhook notifications
- No PSA/RMM/ticketing integration
- No automated remediation (BPA reports but doesn't fix)
- No approval workflows for destructive operations
- No tenant onboarding wizard (guided multi-step flow)

---

## Roadmap

### Phase 1: Foundation & Hardening
**Goal**: Fix critical security issues, establish CI/CD, expand test coverage, update documentation. Make the existing app production-solid before adding new features.

**Estimated scope**: 2-3 weeks focused effort

#### 1.1 Security Hardening

| Task | Files | Priority |
|------|-------|----------|
| Replace `'unsafe-inline'` with nonce-based CSP | `portal/app.py` (after_request handler), all templates with inline `<script>` | Critical |
| Upgrade key derivation from SHA256 to PBKDF2 | `portal/database.py:24-37` (`_derive_key`, `encrypt_value`, `decrypt_value`) + migration to re-encrypt existing data | Critical |
| Add SRI hashes to all CDN resources | `portal/templates/base.html` (Bootstrap CSS/JS, Chart.js) | High |
| Add HSTS header + HTTPS redirect option | `portal/app.py` (after_request) | High |
| Session hardening — IP binding + ID regeneration on login | `portal/routes/auth.py`, `portal/helpers.py` | Medium |
| Replace env var password passing with stdin pipe | `portal/helpers.py:_ps_worker()`, `portal/helpers.py:build_ps_env()` | Medium |

**Key consideration for key derivation migration**: Existing encrypted PFX passwords must be re-encrypted. Add a one-time migration in `init_db()` that detects old encryption format and re-encrypts with new key derivation.

#### 1.2 CI/CD Pipeline

| Task | Files | Priority |
|------|-------|----------|
| GitHub Actions workflow: lint + type check + test + security scan | `.github/workflows/ci.yml` (new) | High |
| Add dev dependencies file | `requirements-dev.txt` (new): pytest, pytest-cov, ruff, mypy, bandit | High |
| Pre-commit hooks config | `.pre-commit-config.yaml` (new) | Medium |
| Dependabot config for Python + GitHub Actions | `.github/dependabot.yml` (new) | Medium |

#### 1.3 Test Coverage Expansion

Current coverage by blueprint:

| Blueprint | Test File | Status |
|-----------|-----------|--------|
| auth | `test_auth.py` (150 lines) | Covered |
| tenants | `test_routes.py` (partial) | Partial |
| audits | — | **Missing** |
| jobs | — | **Missing** |
| reports | — | **Missing** |
| operations | `test_operations.py` (568 lines) | Covered |
| standards | `test_standards.py` (398 lines) | Covered |
| tenant_admin | `test_tenant_admin.py` (472 lines) | Covered |
| conditional | `test_new_features.py` (partial) | Partial |
| gdap | `test_new_features.py` (partial) | Partial |
| tools | `test_new_features.py` (partial) | Partial |
| email_security | — | **Missing** |
| identity | — | **Missing** |
| devices | — | **Missing** |
| service_health | — | **Missing** |
| licenses | — | **Missing** |
| incidents | — | **Missing** |

**Target**: 70%+ line coverage. New test files needed:

| Test File | Covers |
|-----------|--------|
| `tests/test_audits.py` | Audit catalogue, SPO audit launch, security scan, breach investigation, batch ops |
| `tests/test_reports_routes.py` | Report listing, viewer, saved views, schedules, download path security |
| `tests/test_jobs_routes.py` | Job status, cancellation, listing, edge cases |
| `tests/test_email_security.py` | Quarantine, transport rules, anti-phishing, Defender policies |
| `tests/test_identity.py` | Risky users/sign-ins, MFA analysis, risk overview |
| `tests/test_devices.py` | Device inventory, compliance, Autopilot, actions |
| `tests/test_incidents.py` | All incident playbook endpoints |
| `tests/test_licenses.py` | License inventory, usage, optimization |
| `tests/test_service_health.py` | Service health, incidents, message center |

#### 1.4 Documentation

| Task | Files |
|------|-------|
| Update CLAUDE.md to reflect all 18 blueprints, 24 tables, 42 scripts | `CLAUDE.md` |
| Document all API/POST endpoints with request/response formats | `CLAUDE.md` or `docs/api.md` |
| Add deployment guide with reverse proxy examples | `README.md` |
| Update `.env.example` with all environment variables | `.env.example` |

#### 1.5 Quick Database Wins

| Task | Files |
|------|-------|
| Add indexes on `jobs.tenant_id`, `jobs.status`, `audit_log.tenant_id`, `cached_tenant_data.tenant_id` | `portal/database.py` (schema + migration) |
| Add `VACUUM` to database maintenance | `portal/database.py` |

---

### Phase 2: Core Platform Architecture
**Goal**: Replace single-process limitations with proper infrastructure. This is the highest-impact phase — it unlocks task scheduling, real-time updates, RBAC, and API access.

**Estimated scope**: 4-6 weeks focused effort | **Prerequisites**: Phase 1

#### 2.1 Task Queue (Celery + Redis)

Replace `threading.Thread` in `portal/helpers.py` with Celery tasks backed by Redis.

| Task | Detail |
|------|--------|
| Add Celery + Redis | `requirements.txt`: celery, redis. New `portal/celery_app.py` for Celery config |
| Migrate job execution | Convert `_ps_worker()` → Celery task in `portal/tasks.py`. Keep `start_job()` API surface but backend becomes Celery |
| Job progress tracking | Use Celery task state + Redis pub/sub for real-time progress (replace SQLite output polling) |
| Report schedule executor | Celery Beat periodic task that checks `report_schedules` table and launches matching audits |
| Alert rule evaluator | Celery Beat periodic task that evaluates `alert_rules` against recent audit data and creates alerts |
| Job timeout enforcement | Celery `time_limit` and `soft_time_limit` per job type |
| Job retry on failure | Configurable retry with exponential backoff for transient failures |
| Redis session store | Move Flask sessions from cookie-based to Redis (for multi-process) |
| Redis rate limiter | Configure Flask-Limiter to use Redis backend (persists across restarts) |

**Architecture**:
```
Browser → Flask (web) → Redis (broker) → Celery Worker (runs PS scripts)
                                       → Celery Beat (schedules, alert checks)
```

**Key migration concern**: The current `_running_processes` dict in `helpers.py:123` tracks running subprocesses for cancellation. With Celery, cancellation uses `task.revoke(terminate=True)`. The `cancel_job()` function in `helpers.py:148` needs to be updated accordingly.

#### 2.2 Real-Time Updates

| Task | Detail |
|------|--------|
| Add Flask-SocketIO or SSE | `requirements.txt`: flask-socketio (if WebSocket) or use native SSE with streaming responses |
| Job output streaming | Celery task publishes output lines to Redis pub/sub → Flask streams to browser |
| Live alert notifications | Push new alerts to connected clients |
| Dashboard auto-refresh | Real-time stat updates without full page reload |
| Job status updates | Instant status changes (running→done/failed) without 60s poll interval |

**Recommendation**: Start with SSE (Server-Sent Events) — simpler than WebSocket, no additional dependency beyond Redis pub/sub, works through reverse proxies without special config. Fall back to polling for browsers that don't support SSE (rare).

#### 2.3 Role-Based Access Control (RBAC)

| Role | View Tenants | Run Audits | Run Operations | Manage Users | View Reports | Manage Settings |
|------|:---:|:---:|:---:|:---:|:---:|:---:|
| **Super Admin** | All | All | All (Tier 1-3) | Yes | All | Yes |
| **Admin** | Assigned groups | All | Tier 1-2 | No | Assigned | No |
| **Operator** | Assigned groups | All | Tier 1 only | No | Assigned | No |
| **Viewer / Account Manager** | Assigned groups | No | No | No | Assigned (read-only) | No |

| Task | Detail |
|------|--------|
| Extend users table | Add `role TEXT DEFAULT 'operator'`, `allowed_tenant_groups TEXT DEFAULT '[]'` |
| Permission system | New `portal/permissions.py` with capability checks |
| RBAC decorators | `@role_required('admin')`, `@tenant_access_required` in `portal/helpers.py` |
| Tenant-scoped access | Filter `list_tenants()` by user's allowed tenant groups |
| Role management UI | Admin page for user role assignment and tenant group access |
| Audit trail enrichment | Log role context with every action |

#### 2.4 REST API Layer

| Task | Detail |
|------|--------|
| API key model | New `api_keys` table: key hash, scopes, expiration, created_by |
| API authentication middleware | Check `Authorization: Bearer <key>` header, validate against DB |
| API key management | CRUD endpoints + UI page for managing API keys |
| Versioned API routes | `portal/routes/api_v1.py` — new blueprint for `/api/v1/` prefix |
| Core API endpoints | `GET /api/v1/tenants`, `GET /api/v1/jobs/{id}`, `POST /api/v1/audits/run`, `GET /api/v1/reports` |
| Rate limiting per key | Separate rate limits for API keys vs web sessions |
| OpenAPI documentation | Auto-generate or hand-write OpenAPI 3.0 spec |

#### 2.5 Webhook System

| Task | Detail |
|------|--------|
| Webhook model | New `webhooks` table: URL, secret, event types, enabled |
| Webhook delivery | Background Celery task with HMAC signing and exponential retry |
| Event types | `job.completed`, `job.failed`, `alert.triggered`, `bpa.drift_detected`, `report.generated` |
| Webhook management UI | CRUD page for webhook configuration |
| Delivery log | Track delivery attempts, response codes, failures |

**New dependencies**: `celery`, `redis`, `flask-socketio` (optional)

**Key files**:
- `portal/celery_app.py` — New Celery configuration
- `portal/tasks.py` — New Celery task definitions
- `portal/permissions.py` — New RBAC module
- `portal/routes/api_v1.py` — New API blueprint
- `portal/helpers.py` — Updated job execution, new decorators
- `portal/database.py` — Roles, API keys, webhooks tables + migrations
- `portal/app.py` — Redis integration, SSE/SocketIO setup

---

### Phase 3: Reporting & Client-Facing Output
**Goal**: Transform raw audit data into polished, client-ready deliverables that account managers can present to clients.

**Estimated scope**: 3-4 weeks | **Prerequisites**: Phase 2 (task queue for scheduled delivery)

#### 3.1 PDF Report Generation

| Task | Detail |
|------|--------|
| Add PDF engine | `requirements.txt`: weasyprint (HTML→PDF, uses same Jinja2 templates) |
| Report template base | `portal/templates/reports/pdf_base.html` — branded PDF layout with header, footer, page numbers |
| Executive Summary report | 1-page: risk score, key findings count by severity, top 5 action items, secure score gauge |
| Security Posture report | 365Inspect findings grouped by severity, each with description + remediation steps |
| SPO Permission report | Per-site permission breakdown with external sharing highlights |
| BPA Compliance report | Pass/fail table with expected vs actual, improvement guidance |
| Identity Risk report | MFA gaps, risky users, sign-in anomalies with recommendations |
| License Optimization report | Unused licenses, cost savings opportunities, SKU breakdown |
| Breach Investigation report | Timeline, indicators of compromise, affected accounts, remediation steps |
| Branding configuration | Configurable logo, company name, accent color (stored in `config/portal.json`) |
| Batch PDF generation | Generate reports across multiple tenants in one PDF with table of contents |

#### 3.2 Scheduled Report Delivery

| Task | Detail |
|------|--------|
| SMTP configuration | Settings page for SMTP server, credentials, sender address |
| Email delivery Celery task | Send PDF/XLSX reports as attachments or download links |
| Recipient management | Per-schedule recipients (portal users + external email addresses) |
| Delivery log | Track sent reports, bounces, retry status |
| Schedule activation | Wire Celery Beat to actually execute the existing `report_schedules` table entries |

#### 3.3 Report Enhancements

| Task | Detail |
|------|--------|
| Report comparison | Diff two audit runs to highlight changes (new permissions, removed users, score changes) |
| Trend charts | Historical graphs for secure score, BPA compliance %, permission count |
| Multi-format export | Add PDF export alongside existing CSV/JSON in report viewer |
| Print stylesheet | CSS `@media print` styles for browser-based printing |

#### 3.4 Client-Facing Dashboard (Future — Phase 3b)

| Task | Detail |
|------|--------|
| Client user model | Separate login tier with `role='client'`, scoped to one tenant |
| Read-only dashboard | Secure score trend, BPA status, recent reports, open action items |
| Self-service downloads | Clients download their own reports without operator involvement |
| Per-tenant branding | Custom logo/colors in client view |

**New dependencies**: `weasyprint`

**Key files**:
- `portal/report_engine.py` — New PDF generation module
- `portal/templates/reports/` — PDF-specific templates
- `portal/routes/reports.py` — PDF endpoints, email delivery endpoints
- `portal/database.py` — SMTP config, delivery log tables

---

### Phase 4: MSP Operations Expansion & Automation
**Goal**: Expand operations, add automated remediation, approval workflows, and streamline tenant onboarding.

**Estimated scope**: 4-6 weeks | **Prerequisites**: Phase 2 (RBAC for approvals, task queue for remediation)

#### 4.1 Operations Catalogue Expansion

**Current**: 50+ operations across User, Exchange, Teams, SharePoint categories with 3 tiers.

| Category | New Operations |
|----------|---------------|
| **User** | Bulk password reset, bulk license assignment, user provisioning from template, guest user lifecycle |
| **Exchange** | Shared mailbox CRUD, distribution list management, retention policies, journal rules |
| **Teams** | Team provisioning from template, channel management, guest access policies, app permission policies |
| **SharePoint** | Site provisioning from template, hub site management, sensitivity labels, storage quota management |
| **Entra** | Dynamic group management, administrative units, app registration lifecycle, named location CRUD |
| **Intune** | App deployment, compliance policy CRUD, configuration profile management, Autopilot profile assignment |

Each new operation needs:
1. Entry in `OPERATIONS` dict in `portal/routes/operations.py`
2. PowerShell implementation in `scripts/Invoke-*.ps1`
3. Parameter schema for dynamic form generation

#### 4.2 Automated Remediation

| Task | Detail |
|------|--------|
| Remediation script framework | `scripts/Invoke-Remediation.ps1` — takes check_key + tenant params, applies fix |
| Link BPA checks to remediation | Add `remediation_script` field to BPA check definitions in `portal/routes/standards.py` |
| One-click remediate | "Fix" button on BPA results that launches remediation job |
| Remediation log | Track all changes with before/after state snapshots |
| Dry-run mode | Preview what remediation would change before applying |
| Safe defaults | Only auto-remediate low-risk items (enable audit log, etc.); high-risk items require approval |

**Example remediations**:
- Enable unified audit log
- Disable legacy authentication
- Enable security defaults
- Block external forwarding
- Enable mailbox auditing
- Set password expiry policy

#### 4.3 Drift Detection

| Task | Detail |
|------|--------|
| Scheduled BPA runs | Celery Beat runs BPA against tenants with assigned standards on configurable schedule |
| Drift detection | Compare new BPA results against previous — flag changes in status |
| Drift alerts | Auto-create alerts when tenant drifts out of compliance |
| Drift dashboard | Visual overview of standards compliance across all tenants |

#### 4.4 Approval Workflows

| Task | Detail |
|------|--------|
| Change request model | New `change_requests` table: operation, params, requestor, approver, status |
| Tier-gated operations | Tier 2+ operations create a change request instead of executing immediately |
| Approval queue UI | Dashboard showing pending approvals with operation details |
| Approval notification | Email/webhook to approvers when request submitted |
| Auto-expiration | Unapproved requests expire after configurable timeout (default: 72 hours) |
| Execution on approval | Approved requests auto-execute as Celery tasks |

#### 4.5 Tenant Onboarding Wizard

| Task | Detail |
|------|--------|
| Multi-step wizard | Guided flow: Add tenant → Generate cert → Create Entra app → Grant admin consent → Run initial BPA |
| Bulk onboarding | CSV import (columns: name, domain, tenant_id) with batch cert/app creation |
| Onboarding checklist | Per-tenant progress tracker showing setup completion |
| Baseline audit | Auto-launch BPA + security scan + secure score fetch on newly onboarded tenants |

**Key files**:
- `portal/routes/operations.py` — Expanded OPERATIONS dict
- `portal/routes/standards.py` — Drift detection, remediation hooks
- `portal/routes/workflows.py` — New approval workflow blueprint
- `portal/database.py` — Change requests, remediation log tables
- `scripts/Invoke-Remediation.ps1` — New remediation script
- Various `scripts/Invoke-*.ps1` — Expanded operations

---

### Phase 5: Integration Layer
**Goal**: Connect to the MSP ecosystem — PSA ticketing, notifications, and external API consumers.

**Estimated scope**: 4-5 weeks | **Prerequisites**: Phase 2 (API layer, webhooks), Phase 3 (reports)

#### 5.1 PSA Integration

| PSA | Integration Points |
|-----|-------------------|
| **ConnectWise Manage** | Create tickets from alerts/findings, sync client list ↔ tenants, map agreements |
| **Autotask/Datto** | Create tickets from alerts, sync client list |
| **HaloPSA** | Create tickets from alerts, sync client list |

| Task | Detail |
|------|--------|
| PSA adapter interface | `portal/integrations/base.py` — abstract base class for PSA connectors |
| ConnectWise connector | `portal/integrations/connectwise.py` — REST API client |
| Autotask connector | `portal/integrations/autotask.py` — REST API client |
| HaloPSA connector | `portal/integrations/halopsa.py` — REST API client |
| Ticket creation rules | Map alert severity/type → ticket priority/board/status |
| Client sync | Bi-directional: PSA client list ↔ portal tenant list |
| Integration config UI | Settings page for PSA connection details, field mapping |

#### 5.2 Notification Channels

| Task | Detail |
|------|--------|
| Microsoft Teams | Incoming webhook connector for alert/job notifications |
| Slack | Incoming webhook connector |
| Email notifications | SMTP-based notifications for alerts, job completions, approval requests |
| Notification preferences | Per-user notification channel preferences |
| Notification templates | Configurable message templates per event type |

#### 5.3 External Data Consumers

| Task | Detail |
|------|--------|
| Power BI connector | REST API endpoints optimized for Power BI data refresh |
| Scheduled data exports | CSV/JSON feeds to file shares or cloud storage |
| Grafana metrics | Prometheus-format metrics endpoint for operational dashboards |

**Key files**:
- `portal/integrations/` — New directory for integration modules
- `portal/routes/integrations.py` — New integration management blueprint
- `portal/database.py` — Integration config tables, notification preferences

---

### Phase 6: Compliance & Governance
**Goal**: Add regulatory compliance framework mapping, evidence collection, and governance reporting.

**Estimated scope**: 3-4 weeks | **Prerequisites**: Phase 4 (remediation, drift detection)

#### 6.1 Compliance Frameworks

| Framework | Target Audience |
|-----------|----------------|
| **CIS Microsoft 365 Benchmark** | General enterprise security |
| **NIST 800-171** | US DoD contractors |
| **Essential Eight (ASD)** | Australian organizations |
| **Cyber Essentials (UK)** | UK organizations |
| **Custom frameworks** | MSP-defined compliance templates |

| Task | Detail |
|------|--------|
| Framework data model | Map BPA checks → framework controls (many-to-many) |
| Framework definitions | JSON/YAML files in `config/frameworks/` defining control-to-check mappings |
| Framework import/export | Upload custom frameworks, export for sharing |
| Compliance score | Per-framework percentage: (passing controls / total controls) |

#### 6.2 Compliance Reporting

| Task | Detail |
|------|--------|
| Compliance dashboard | Per-tenant framework compliance scores with drill-down |
| Gap analysis view | Missing controls with remediation guidance and effort estimate |
| Compliance trend | Historical score tracking with charts |
| Multi-tenant heatmap | Cross-tenant compliance overview for MSP leadership |
| Evidence collection | Auto-attach BPA results, audit logs, config snapshots as compliance evidence |
| Compliance PDF report | Client-facing compliance report per framework |

#### 6.3 Policy Governance

| Task | Detail |
|------|--------|
| Desired state config | Define target tenant configuration, detect and flag deviations |
| Policy exceptions | Document and track approved exceptions to compliance requirements |
| Policy inheritance | Tenant groups inherit policies; per-tenant overrides allowed |
| Exception expiry | Exceptions auto-expire, trigger re-review alerts |

**Key files**:
- `portal/routes/compliance.py` — New compliance blueprint
- `portal/templates/compliance_*.html` — Compliance dashboards and reports
- `portal/database.py` — Framework tables, compliance score history, exceptions
- `config/frameworks/` — Framework definition files

---

### Phase 7: Scale & Production Hardening
**Goal**: Production-harden for multi-instance MSP deployment with containerization, database options, and observability.

**Estimated scope**: 4-6 weeks | **Prerequisites**: Phase 2 (Redis, Celery)

#### 7.1 Containerization

| Task | Detail |
|------|--------|
| Multi-stage Dockerfile | Stage 1: Python deps. Stage 2: PowerShell 7 + modules. Stage 3: Runtime with non-root user |
| docker-compose.yml | Services: app (Flask+gunicorn), worker (Celery), beat (Celery Beat), redis, postgres (optional) |
| .dockerignore | Exclude certs, database, output, tenants, logs, .git |
| Volume mounts | `tenants/`, `data/`, `logs/`, `config/` as named volumes |
| Health checks | Docker HEALTHCHECK using `/health` endpoint |
| Environment config | All config via environment variables (12-factor app) |
| Production compose | Separate `docker-compose.prod.yml` with resource limits, restart policies |

#### 7.2 Database Options

| Task | Detail |
|------|--------|
| PostgreSQL support | Add psycopg2 adapter, make database backend configurable via `DATABASE_URL` env var |
| Connection pooling | Connection pool for concurrent request handling |
| Migration tool | Add Alembic for proper schema migrations (replace custom `_MIGRATIONS` dict) |
| SQLite preservation | Keep SQLite as default for single-instance deployments |
| Backup improvements | pg_dump support for PostgreSQL, scheduled backup Celery task |

**Approach**: Rather than full SQLAlchemy ORM (massive refactor), add a thin abstraction layer that handles PostgreSQL parameter syntax differences (`?` → `%s`) and connection management. This preserves all existing query logic while enabling PostgreSQL.

#### 7.3 Observability

| Task | Detail |
|------|--------|
| Structured JSON logging | Replace text logging with JSON format including request_id, user, tenant context |
| Prometheus metrics endpoint | `/metrics` — request latency, job duration, error rates, queue depth, active connections |
| Health check expansion | Check Redis connectivity, Celery worker availability, database, PowerShell availability |
| Log aggregation support | Structured logs compatible with ELK, Loki, CloudWatch |

#### 7.4 Performance

| Task | Detail |
|------|--------|
| Response caching | Cache frequently-accessed data (tenant list, BPA checks, SKU maps) in Redis |
| Database query optimization | Add missing indexes, optimize N+1 queries in listing endpoints |
| Static file serving | Configure for CDN or nginx static serving in production |
| Gzip compression | Add compression middleware for API responses |

#### 7.5 Authentication Enhancements

| Task | Detail |
|------|--------|
| OIDC/SAML support | Portal login via Entra ID, Okta, Google Workspace (useful for MSPs with their own IdP) |
| TOTP MFA | Optional TOTP-based MFA for portal accounts |
| Session management | Active session list, remote revocation, concurrent session limits |
| Password policies | Configurable minimum length, complexity, expiry |

**Key files**:
- `Dockerfile` — New multi-stage build
- `docker-compose.yml` — Full stack orchestration
- `docker-compose.prod.yml` — Production configuration
- `portal/database.py` — PostgreSQL support, connection pooling
- `portal/app.py` — Structured logging, metrics endpoint
- `alembic/` — Migration directory (if Alembic adopted)
- `portal/routes/auth.py` — SSO/MFA integration

---

## PowerShell Improvements (Cross-Phase)

These improvements apply across all phases and should be addressed incrementally alongside the Python work.

### Immediate (During Phase 1)

| Task | File | Detail |
|------|------|--------|
| Graph API throttling | `scripts/Shared.psm1` `Invoke-GraphRequest` | Add 429/Retry-After detection with exponential backoff |
| Module version pinning | `Build-Toolkit.ps1` | Pin `RequiredVersion` to prevent breaking changes from upstream module updates |
| Error continuation in inspector loop | `inspectors/365Inspect.ps1` | Wrap each inspector in try/catch so one failure doesn't abort entire scan |
| Token refresh handling | `scripts/Shared.psm1` `Get-AuditGraphToken` | Add token cache with refresh for operations lasting >60 minutes |

### Short-Term (During Phase 2-3)

| Task | Detail |
|------|--------|
| Pester test framework | Add `tests/*.Tests.ps1` for `Shared.psm1` functions. Mock `Connect-PnPOnline`, `Invoke-RestMethod` |
| Structured progress output | All scripts emit JSON progress lines (`{"progress": 50, "message": "..."}`) for real-time UI updates |
| Inspector result caching | Cache Graph API responses within a scan session to avoid duplicate calls across inspectors |
| Script input validation | Validate all URLs, GUIDs, emails before making API calls |

### Medium-Term (During Phase 4-5)

| Task | Detail |
|------|--------|
| Remediation scripts | New `scripts/Invoke-Remediation.ps1` for BPA auto-fix operations |
| Replace deprecated modules | Migrate `Microsoft.Online.SharePoint.PowerShell` usage to Microsoft Graph equivalents |
| Configurable timeouts | Per-script timeout parameters for long-running operations |
| Parallel execution | Use PowerShell `ForEach-Object -Parallel` for multi-site operations |

---

## Phase Dependencies & Parallelism

```
Phase 1 (Foundation)
    |
    v
Phase 2 (Core Platform) ────────────────────────────────┐
    |                                                     |
    ├──> Phase 3 (Reporting)                             |
    |        |                                            |
    ├──> Phase 4 (Operations & Automation)               |
    |        |                                            |
    |        └──> Phase 6 (Compliance)                   |
    |                                                     |
    ├──> Phase 5 (Integrations) ←── needs Phase 3 too    |
    |                                                     |
    └──> Phase 7 (Scale) ←───────────────────────────────┘
```

- **Phases 3, 4, 5 can run in parallel** once Phase 2 is complete
- **Phase 6** depends on Phase 4 (remediation and drift detection)
- **Phase 7** can start after Phase 2 but should incorporate learnings from other phases

---

## Priority Matrix

| Phase | Business Impact | Technical Risk | Effort | Start |
|-------|:---:|:---:|:---:|:---:|
| **1. Foundation** | High | Low | Medium | Immediately |
| **2. Core Platform** | Critical | Medium | Large | After Phase 1 |
| **3. Reporting** | High (client-facing) | Low | Medium | After Phase 2 |
| **4. Operations** | High (MSP efficiency) | Medium | Large | After Phase 2 |
| **5. Integrations** | Medium (ecosystem) | Medium | Large | After Phase 2+3 |
| **6. Compliance** | Medium (regulated verticals) | Low | Medium | After Phase 4 |
| **7. Scale** | Medium (growth) | High | Large | After Phase 2 |

---

## Verification Criteria

After each phase, verify:

1. **All existing tests pass**: `python -m pytest tests/ -v`
2. **New tests added**: Coverage target met for modified/new code
3. **Security scan**: `bandit -r portal/` — no new high/medium findings
4. **Lint clean**: `ruff check portal/` — no errors
5. **Manual smoke test**: Login → create tenant → run audit → view report → check job
6. **PowerShell integration**: Cert generation, Graph Explorer, at least one audit type work end-to-end
7. **Docker (Phase 7+)**: `docker compose up` starts cleanly, all services healthy

---

## Quick Wins (Can Start Today)

These provide immediate value with minimal risk and no architectural changes:

1. **Update CLAUDE.md** — document all 18 blueprints, 24 tables, 42 scripts (currently documents 5 blueprints)
2. **Add GitHub Actions CI** — run existing tests on every push
3. **Fix CSP unsafe-inline** — add nonces to `app.py` after_request handler
4. **Add SRI hashes** — to Bootstrap and Chart.js CDN links in `base.html`
5. **Add database indexes** — on `jobs.tenant_id`, `jobs.status`, `audit_log.tenant_id`, `cached_tenant_data(tenant_id, data_type)`
6. **Add `requirements-dev.txt`** — pytest, pytest-cov, ruff, bandit
7. **Graph throttling** — add 429 retry handling to `Invoke-GraphRequest` in `Shared.psm1`
