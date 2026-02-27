# CLAUDE.md — SPO-Audit-Toolkit

## Project Overview

SPO-Audit-Toolkit is a **Microsoft 365 security audit, management, and breach investigation platform** designed for MSPs and IT administrators. The core is a **web portal** (`portal/app.py`) — a multi-tenant Flask web app for managing hundreds of client tenants.

The portal uses **certificate-based app-only authentication** (Entra ID / Azure AD) to connect to SharePoint Online, Exchange Online, and Microsoft Graph without storing user credentials.

**Vision**: A self-hosted CIPP alternative — full MSP management platform with M365 administration, compliance enforcement, automated remediation, incident response, and client-facing reporting.

## Repository Structure

```
SPO-Audit-Toolkit/
├── CLAUDE.md                # This file — project context for Claude Code
├── ROADMAP.md               # 7-phase development roadmap with implementation details
├── README.md                # User-facing documentation and setup guide
├── CONTRIBUTING.md          # Development guidelines and conventions
├── SECURITY.md              # Security policy and vulnerability reporting
├── .env.example             # Environment variable documentation
├── setup.sh                 # Cross-platform launcher (Linux/macOS)
├── start.cmd                # Windows launcher for the Flask web portal
├── Build-Toolkit.ps1        # Downloads all required PowerShell modules to ./modules/
├── requirements.txt         # Python dependencies (pinned versions)
├── pyproject.toml           # pytest configuration
├── config/
│   └── portal.json          # Portal config (auto-generated Flask secret key, gitignored)
│
├── portal/                  # Flask web application (the main product)
│   ├── app.py               # App factory, config, middleware, dashboard route, security headers
│   ├── database.py          # SQLite persistence — 24 tables, schema migrations, CRUD functions
│   ├── helpers.py           # Auth decorators, PS runner, tenant filesystem, input validation
│   ├── routes/              # 17 Blueprint modules (see Blueprint Reference below)
│   │   ├── __init__.py
│   │   ├── auth.py          # Login, logout, initial setup, settings, password change
│   │   ├── tenants.py       # Tenant CRUD, cert generation, Entra app management
│   │   ├── audits.py        # SPO audit, security scan, breach investigation, batch ops
│   │   ├── jobs.py          # Job status polling, cancellation
│   │   ├── reports.py       # Report listing, interactive viewer, saved views, schedules
│   │   ├── operations.py    # 50+ MSP operations (User, Exchange, Teams, SharePoint)
│   │   ├── standards.py     # BPA, standards templates, secure scores, alerts, tenant groups
│   │   ├── tenant_admin.py  # Tenant lookup, domains, auth methods, partner relationships
│   │   ├── conditional.py   # Conditional Access policies, named locations, CA templates
│   │   ├── gdap.py          # GDAP relationships, role templates, onboarding/offboarding
│   │   ├── tools.py         # Graph Explorer, GeoIP lookup, Domain Health, config backup
│   │   ├── email_security.py # Quarantine, transport rules, anti-phishing, Defender policies
│   │   ├── identity.py      # Risky users/sign-ins, MFA report, auth strengths, password protection
│   │   ├── devices.py       # Intune inventory, compliance, Autopilot, device policies/actions
│   │   ├── service_health.py # Service status, incidents, message center, maintenance
│   │   ├── licenses.py      # License inventory, usage, optimization, user detail
│   │   └── incidents.py     # BEC, account compromise, data exfil, malware, privilege escalation
│   ├── static/css/portal.css # Custom dark theme design system (~1,700 lines)
│   └── templates/           # 84 Jinja2 templates (Bootstrap 5, dark theme)
│
├── scripts/                 # 42 PowerShell scripts (audit, setup, admin, incident response)
│   ├── Shared.psm1          # Shared module (connection helpers, export, password handling)
│   ├── Audit-*.ps1          # SPO audit scripts (5)
│   ├── Get-*.ps1            # Data retrieval scripts (21)
│   ├── Invoke-*.ps1         # Action/admin scripts (10)
│   ├── Search-*.ps1         # Search scripts (1)
│   ├── Setup-*.ps1          # Setup scripts (3)
│   └── Backup-*.ps1         # Backup scripts (1)
│
├── inspectors/              # 365Inspect security scanner (third-party, adapted)
│   ├── 365Inspect.ps1       # Main inspector orchestrator
│   ├── Inspectors/          # 108 inspector pairs (Inspect-*.ps1 + Inspect-*.json)
│   └── Utilities/
│       └── Write-ErrorLog.ps1
│
└── tests/                   # Python test suite (pytest) — 8 test files
    ├── conftest.py          # Shared fixtures, in-memory SQLite, test client
    ├── test_auth.py         # Login, logout, initial setup, session tests
    ├── test_database.py     # CRUD operations, encryption, schema integrity
    ├── test_routes.py       # Route access control, API response formats
    ├── test_security.py     # Path traversal, CSRF enforcement, CSP headers
    ├── test_operations.py   # MSP operations endpoint tests
    ├── test_standards.py    # BPA, standards, alerts, secure scores
    ├── test_tenant_admin.py # Tenant admin features
    └── test_new_features.py # Conditional access, GDAP, tools
```

### Directories created at runtime (gitignored)

- `bin/` — optional bundled PowerShell 7 runtime
- `modules/` — PowerShell modules downloaded by `Build-Toolkit.ps1`
- `tenants/` — per-tenant data, certs, and output (`tenants/<id>/certs/`, `tenants/<id>/output/`)
- `data/` — SQLite database (`portal.db`) and backups
- `logs/` — application logs (`portal.log`, with rotation)

## Current Scale

| Component | Count | Detail |
|-----------|-------|--------|
| Route blueprints | 17 + dashboard | ~9,800 lines across `portal/routes/` + `app.py` |
| Templates | 84 | Bootstrap 5 dark theme, Jinja2 |
| Database tables | 24 | + schema_version; ~2,100 lines in `database.py` |
| PowerShell scripts | 42 | 41 `.ps1` + 1 `.psm1` in `scripts/` |
| Security inspectors | 108 | `inspectors/Inspectors/` (365Inspect) |
| CSS design system | ~1,700 lines | Custom dark theme in `portal/static/css/portal.css` |
| Test files | 8 | + conftest.py |
| Python dependencies | 4 | Flask, cryptography, Flask-WTF, Flask-Limiter |

## Technology Stack

### PowerShell (audit scripts)
- **Requires**: PowerShell 7+ (pwsh)
- **Key modules**: PnP.PowerShell, MSAL.PS, Microsoft.Graph.*, ExchangeOnlineManagement, ImportExcel
- **Auth**: Certificate-based app-only (Entra ID registered app with X.509 cert)
- **Output**: XLSX (via ImportExcel) with CSV fallback

### Python (web portal)
- **Python**: 3.9+
- **Framework**: Flask 3.1
- **CSRF Protection**: Flask-WTF (CSRFProtect)
- **Rate Limiting**: Flask-Limiter (10/min on login, in-memory storage)
- **Database**: SQLite with WAL mode, version-tracked schema migrations (current version: 5)
- **Encryption**: cryptography (Fernet) for PFX passwords at rest
- **Password hashing**: Werkzeug (bcrypt-based)
- **Frontend**: Bootstrap 5.3 (dark theme), Bootstrap Icons 1.11.3, Chart.js, Jinja2 templates

## Key Architecture Decisions

### Authentication Model
All connections to M365 services use **certificate-based app-only auth**. No user passwords are stored. The flow is:
1. Generate self-signed cert (CER + PFX) via the portal's tenant setup
2. Register Entra app, upload CER, assign API permissions
3. Connect using ClientId + PFX + PFX password

The PFX password is encrypted with Fernet (key derived from Flask `secret_key` via SHA256) and stored in SQLite.

### Portal Architecture
The portal uses Flask Blueprints to organize routes by feature. The dashboard route lives in `app.py`; all 17 other blueprints are registered in `create_app()`.

Shared helpers (`helpers.py`) provide auth decorators (`@login_required`, `@admin_required`), PowerShell subprocess management, tenant filesystem operations, and input validation.

### Job Execution
The portal runs PowerShell scripts as background subprocesses via `threading.Thread` (in `helpers.py`). Output is streamed line-by-line into SQLite (`jobs` table). Job status is polled by the frontend via `/jobs/<id>/status`. Batch operations (`/batch/audit`) allow running scans across multiple tenants simultaneously. Running processes are tracked in `_running_processes` dict for cancellation support via `cancel_job()`.

### Shared Module Pattern
All audit scripts import `scripts/Shared.psm1` using `Join-Path $PSScriptRoot "Shared.psm1"` which provides:
- `Connect-AuditSite` — PnP connection with retry logic
- `Get-AuditGraphToken` — MSAL token acquisition
- `Invoke-GraphRequest` — paginated Graph API calls (max 100 pages)
- `Get-AuditCertificatePassword` — PFX password resolution (param > env var > prompt)
- `Export-AuditData` — XLSX/CSV export with consistent formatting
- `Get-PrincipalType` — classifies SharePoint principals (User, External, M365 Group, etc.)
- `Get-ExcludedSystemLists` — 43 SharePoint system lists to skip

### Inspector Pattern (365Inspect)
Each inspector consists of two files:
- `Inspect-*.ps1` — PowerShell function that returns finding messages
- `Inspect-*.json` — metadata (FindingName, Description, Remediation, Impact, Service, References)

The `365Inspect.ps1` orchestrator loads all 108 inspectors, runs them, and generates HTML/CSV/JSON/XML reports. Inspector metadata is cached in memory during the app lifetime.

### Database Schema Versioning
The SQLite database uses a `schema_version` table to track schema changes. On startup, `init_db()` checks the current version and applies incremental migrations from the `_MIGRATIONS` dict in `database.py`. Current version: 5.

## Blueprint Reference

### Core Blueprints

| Blueprint | File | Key Routes | Purpose |
|-----------|------|------------|---------|
| **auth** | `routes/auth.py` | `/login`, `/logout`, `/initial-setup`, `/settings`, `/users` | Authentication, user management |
| **tenants** | `routes/tenants.py` | `/tenants`, `/tenants/<id>`, `/tenants/<id>/setup-*`, `/tenants/<id>/delete` | Tenant CRUD, cert gen, Entra app setup/removal |
| **audits** | `routes/audits.py` | `/audits`, `/audits/spo`, `/audits/security`, `/audits/breach`, `/batch/audit` | SPO audit, 365Inspect security scan, breach investigation, batch ops |
| **jobs** | `routes/jobs.py` | `/jobs`, `/jobs/<id>/status`, `/jobs/<id>/cancel` | Job listing, status polling, cancellation |
| **reports** | `routes/reports.py` | `/reports`, `/report-viewer/<path>`, `/reports/download`, `/schedules/*`, `/audit-trail` | Report browser, interactive viewer, saved views, schedules, audit trail |

### MSP Management Blueprints

| Blueprint | File | Key Routes | Purpose |
|-----------|------|------------|---------|
| **operations** | `routes/operations.py` | `/operations`, `/operations/run`, `/operations/bulk` | 50+ tiered operations (User, Exchange, Teams, SharePoint) |
| **standards** | `routes/standards.py` | `/groups/*`, `/standards/*`, `/bpa/*`, `/secure-scores/*`, `/alerts/*` | Tenant groups, BPA templates, scanning, secure scores, alerts |
| **tenant_admin** | `routes/tenant_admin.py` | `/tenant-lookup/*`, `/tenants/<id>/domains`, `/tenants/<id>/auth-methods`, `/tenants/<id>/partner-relationships`, `/tenants/<id>/app-consent`, `/tenants/<id>/applications`, `/tenants/<id>/directory-audits`, `/tenants/<id>/users` | Tenant admin data queries |
| **conditional** | `routes/conditional.py` | `/conditional-access`, `/ca-templates/*`, `/tenants/<id>/ca-policies`, `/tenants/<id>/named-locations`, `/tenants/<id>/vacation-mode` | Conditional Access management |
| **gdap** | `routes/gdap.py` | `/gdap`, `/gdap/role-templates/*`, `/gdap/onboarding/*`, `/gdap/offboarding/*`, `/tenants/<id>/gdap-relationships` | GDAP relationship lifecycle |
| **tools** | `routes/tools.py` | `/graph-explorer/*`, `/geoip-lookup/*`, `/domain-health/*`, `/backup/*` | Utility tools and config backup/restore |

### Newer Feature Blueprints

| Blueprint | File | Key Routes | Purpose |
|-----------|------|------------|---------|
| **email_security** | `routes/email_security.py` | `/email-security/quarantine`, `/email-security/transport-rules`, `/email-security/anti-phishing`, `/email-security/defender-policies`, `/email-security/threat-report` | Email protection management |
| **identity** | `routes/identity.py` | `/identity/risky-users`, `/identity/risky-signins`, `/identity/mfa-report`, `/tenants/<id>/auth-strengths`, `/tenants/<id>/password-protection` | Identity protection & MFA |
| **devices** | `routes/devices.py` | `/devices/inventory`, `/devices/compliance`, `/devices/autopilot`, `/tenants/<id>/device-policies`, `/tenants/<id>/devices/action` | Intune device management |
| **service_health** | `routes/service_health.py` | `/service-health`, `/service-health/incidents`, `/service-health/message-center`, `/service-health/maintenance`, `/service-health/overview` | M365 service monitoring |
| **licenses** | `routes/licenses.py` | `/licenses`, `/licenses/usage`, `/licenses/optimization`, `/licenses/overview`, `/tenants/<id>/licenses/user-detail` | License lifecycle management |
| **incidents** | `routes/incidents.py` | `/incidents`, `/incidents/bec-response`, `/incidents/account-compromise`, `/incidents/data-exfiltration`, `/incidents/malware-outbreak`, `/incidents/privilege-escalation` | IR playbooks |

### Dashboard (in app.py)
- `GET /` — main dashboard with stats, Chart.js analytics, compliance overview, onboarding wizard
- `GET /api/stats` — dashboard stats JSON
- `GET /api/chart-data` — chart data for AJAX refresh
- `GET /health` — unauthenticated health check endpoint

## Database Schema (24 tables)

### Core
| Table | Purpose | Key Columns |
|-------|---------|-------------|
| **users** | Portal operator accounts | username, password_hash, is_admin |
| **tenants** | M365 tenant records | domain, tenant_id, client_id, pfx_password_enc, app_display_name |
| **jobs** | Background job tracking | tenant_id, type, status, operator, output (JSON array) |
| **audit_log** | Operator action audit trail | tenant_id, operator, action, details |

### Reports & Views
| Table | Purpose | Key Columns |
|-------|---------|-------------|
| **saved_views** | Report viewer configs | columns, filters, sort, chart settings |
| **report_schedules** | Scheduled report defs | frequency, day_of_week, hour, audit_type — *not yet executed* |

### Compliance & Standards
| Table | Purpose | Key Columns |
|-------|---------|-------------|
| **tenant_groups** | Logical tenant grouping | name, color |
| **tenant_group_members** | Group ↔ tenant membership | group_id, tenant_id |
| **standards_templates** | BPA check collections | checks (JSON), created_by |
| **tenant_standards** | Template ↔ tenant assignment | action (report/enforce) |
| **bpa_results** | BPA scan results | check_key, status, expected, actual, impact |
| **alert_rules** | Alert rule definitions | conditions, actions — *no evaluation engine yet* |
| **alert_history** | Triggered alert records | severity, status, delivered_via |
| **secure_scores** | Secure Score snapshots | current_score, max_score, details |

### Tenant Administration
| Table | Purpose | Key Columns |
|-------|---------|-------------|
| **tenant_custom_variables** | Per-tenant key-value metadata | var_key, var_value |
| **cached_tenant_data** | Cached Graph API responses | data_type, data (JSON) |
| **gdap_role_templates** | Reusable GDAP role sets | roles (JSON) |
| **tenant_backups** | Config backup records | backup_type, components, data |
| **ca_policy_templates** | CA policy templates | policy_json, category |

### Security & Incident Response
| Table | Purpose | Key Columns |
|-------|---------|-------------|
| **incident_cases** | IR case records | playbook, severity, findings, status |
| **incident_notes** | Notes on incident cases | case_id, author, content |

### Monitoring
| Table | Purpose | Key Columns |
|-------|---------|-------------|
| **service_health_cache** | M365 service health status | service, status, details |
| **license_snapshots** | License count snapshots | sku_data, total/assigned |
| **device_inventory_cache** | Intune device summaries | device_count, compliant/noncompliant |

## PowerShell Scripts Reference

### Audit Scripts (run as background jobs via portal)
| Script | Purpose | API |
|--------|---------|-----|
| `Audit-Groups.ps1` | Site groups + members audit | PnP |
| `Audit-SitePermissions.ps1` | Full site permission snapshot | PnP |
| `Audit-ExternalSharing.ps1` | External users + sharing links | PnP |
| `Audit-InboxRules.ps1` | Inbox rules (BEC detection) | Exchange |
| `Audit-OAuthConsent.ps1` | OAuth consent grant review | Graph |

### Data Retrieval Scripts (Get-*.ps1)
| Script | Purpose | API |
|--------|---------|-----|
| `Get-SharePointTenantPermissions.ps1` | User permissions across all sites | PnP |
| `Get-SignInLogs.ps1` | Azure AD sign-in log analysis | Graph |
| `Get-MessageTrace.ps1` | Exchange message trace | Exchange |
| `Search-UnifiedAuditLog.ps1` | Unified Audit Log search | Exchange |
| `Get-TenantBPA.ps1` | Best Practice Analysis scan | Graph + Exchange |
| `Get-SecureScore.ps1` | Microsoft Secure Score fetch | Graph |
| `Get-ConditionalAccessPolicies.ps1` | CA policy retrieval | Graph |
| `Get-EmailProtectionPolicies.ps1` | Email protection policy data | Exchange |
| `Get-EmailThreatReport.ps1` | Email threat intelligence | Graph |
| `Get-IdentityProtection.ps1` | Risky users/sign-ins | Graph |
| `Get-IntuneDevices.ps1` | Intune device inventory | Graph |
| `Get-LicenseReport.ps1` | License SKU/assignment data | Graph |
| `Get-QuarantineMessages.ps1` | Quarantined email messages | Exchange |
| `Get-ServiceHealth.ps1` | M365 service health status | Graph |
| `Get-TransportRules.ps1` | Exchange transport rules | Exchange |
| `Get-UserReport.ps1` | User listing with details | Graph |
| `Get-TenantDomains.ps1` | Tenant domain listing | Graph |
| `Get-AuthMethods.ps1` | Auth methods per user | Graph |
| `Get-PartnerRelationships.ps1` | Partner/GDAP relationships | Graph |
| `Get-AppRegistrations.ps1` | Entra app registrations | Graph |
| `Get-AppConsentRequests.ps1` | App consent requests | Graph |
| `Get-DirectoryAuditLogs.ps1` | Directory audit log entries | Graph |

### Action/Admin Scripts (Invoke-*.ps1)
| Script | Purpose |
|--------|---------|
| `Invoke-UserAction.ps1` | User management (enable, disable, reset password, etc.) |
| `Invoke-UserOffboard.ps1` | User offboarding workflow |
| `Invoke-MailboxAction.ps1` | Mailbox operations (convert, delegate, forwarding) |
| `Invoke-ExchangeAdmin.ps1` | Exchange admin operations |
| `Invoke-TeamAction.ps1` | Teams management operations |
| `Invoke-SPOAdmin.ps1` | SharePoint admin operations |
| `Invoke-GraphExplorer.ps1` | Arbitrary Graph API queries |
| `Invoke-DeviceAction.ps1` | Intune device remote actions |
| `Invoke-IncidentResponse.ps1` | Incident response playbook execution |
| `Invoke-QuarantineAction.ps1` | Release/delete quarantined emails |

### Setup & Maintenance Scripts
| Script | Purpose |
|--------|---------|
| `Setup-Cert.ps1` | Non-interactive cert generation (CER + PFX) |
| `Setup-App.ps1` | Non-interactive Entra app registration |
| `Setup-RemoveApp.ps1` | Non-interactive Entra app removal |
| `Backup-TenantConfig.ps1` | Tenant configuration backup to JSON |

## Development Conventions

### PowerShell Scripts
- Use `Set-StrictMode -Version Latest` and `$ErrorActionPreference = "Stop"`
- Use `#requires` directives for module dependencies
- Use `[CmdletBinding()]` with typed parameters
- Use `[System.Collections.Generic.List[PSCustomObject]]` instead of `@()` array concatenation
- Import Shared.psm1 using `Join-Path $PSScriptRoot "Shared.psm1"`
- Log with pattern: `Write-Host "$(Get-Date) INFO: message"` (also `WARN:` for warnings)
- Use `Write-Progress` for long operations with counter/total tracking
- Always disconnect sessions after use (`Disconnect-PnPOnline`, `Disconnect-ExchangeOnline`)
- Export data via `Export-AuditData` from Shared.psm1 (consistent XLSX/CSV output)
- File naming: `{Prefix}_{SiteName}_{yyyyMMdd-HHmm}.xlsx`
- Setup scripts output JSON on last stdout line for portal parsing

### Python / Flask
- App factory pattern in `app.py` (`create_app()`)
- Routes organized into Blueprints under `portal/routes/`
- All routes behind `@login_required` decorator (except `/login`, `/initial-setup`, `/health`)
- Admin-only routes use `@admin_required` decorator
- API endpoints return `jsonify({"ok": True/False, ...})`
- CSRF protection via Flask-WTF on all POST routes; CSRF token exposed as `window.CSRF_TOKEN` in base template for fetch() calls
- Path traversal protection on file downloads (validates against allowed base directories)
- Database operations use context manager (`with get_db() as conn:`)
- Subprocess environment set up via `build_ps_env()` (injects local module path + PFX password)
- Input validation helpers in `helpers.py`: `validate_domain()`, `validate_tenant_guid()`, `validate_site_url()`, `validate_email()`, `sanitize_text()`

### Templates
- Extend `base.html` with `{% block content %}`
- Bootstrap 5 dark theme (`data-bs-theme="dark"`)
- Sidebar navigation with collapsible sections and Bootstrap Icons
- Flash messages rendered in base template
- All forms include CSRF token (hidden field for traditional forms, `window.CSRF_TOKEN` for fetch)
- `url_for` uses blueprint-qualified names (e.g., `url_for('tenants.tenant_detail', tenant_id=...)`)
- Dashboard uses Chart.js for analytics visualizations

### Adding a New Blueprint
1. Create `portal/routes/new_feature.py` with `bp = Blueprint("new_feature", __name__)`
2. Import and register in `portal/app.py` in `create_app()`
3. Add `@login_required` to all routes (or `@admin_required` for admin-only)
4. Create templates in `portal/templates/`
5. Add sidebar nav entry in `portal/templates/base.html`
6. Add test file in `tests/test_new_feature.py`

### Adding a New Database Table
1. Add `CREATE TABLE` to the `SCHEMA` string in `database.py`
2. Increment `SCHEMA_VERSION` constant
3. Add the same `CREATE TABLE` as a new entry in `_MIGRATIONS` dict
4. Add CRUD functions following existing patterns (`create_*`, `get_*`, `list_*`, `update_*`, `delete_*`)
5. All functions must use `with get_db() as conn:` context manager

### Adding a New Audit Script
1. Create `scripts/New-AuditScript.ps1` following existing parameter patterns
2. Add `#requires -Modules <module>` directive at the top
3. Import Shared.psm1: `$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"`
4. Use `Get-AuditCertificatePassword` for PFX password resolution
5. Use `Connect-AuditSite` or `Get-AuditGraphToken` for connections
6. Use `Export-AuditData` for output
7. Wire it into the appropriate Blueprint in `portal/routes/` and add a template

### Adding a New Inspector
1. Create `inspectors/Inspectors/Inspect-NewCheck.ps1` with a function returning messages
2. Create matching `inspectors/Inspectors/Inspect-NewCheck.json` with finding metadata
3. The 365Inspect orchestrator auto-discovers new inspectors

## Testing

### Running Tests
```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -v
```

### Test Infrastructure
- **conftest.py** — Flask test client fixture, temporary SQLite database (reset per test)
- Fixtures: `client` (unauthenticated), `authed_client` (authenticated), `app_csrf`/`client_csrf` (CSRF enabled)
- Database automatically reset before each test via `_clean_db` autouse fixture

### Coverage by Blueprint

| Blueprint | Test File | Status |
|-----------|-----------|--------|
| auth | `test_auth.py` | Covered |
| database | `test_database.py` | Covered |
| tenants | `test_routes.py` (partial) | Partial |
| audits | — | **Missing** |
| jobs | — | **Missing** |
| reports | — | **Missing** |
| operations | `test_operations.py` | Covered |
| standards | `test_standards.py` | Covered |
| tenant_admin | `test_tenant_admin.py` | Covered |
| conditional | `test_new_features.py` (partial) | Partial |
| gdap | `test_new_features.py` (partial) | Partial |
| tools | `test_new_features.py` (partial) | Partial |
| email_security | — | **Missing** |
| identity | — | **Missing** |
| devices | — | **Missing** |
| service_health | — | **Missing** |
| licenses | — | **Missing** |
| incidents | — | **Missing** |
| security headers | `test_security.py` | Covered |

### Writing Tests for a Blueprint
Follow `tests/test_operations.py` as a model:
1. Create `tests/test_<blueprint>.py`
2. Use `authed_client` fixture for authenticated route tests
3. Test: GET pages return 200, POST endpoints validate input, API responses match `{"ok": True/False}` format
4. Mock PowerShell subprocess calls — don't test actual PS execution
5. Test access control (unauthenticated requests redirect to login)

## Security Considerations

- **Never commit**: certificates (`*.pfx`, `*.cer`, `*.pem`), credentials, audit output, database (`data/`), tenant data (`tenants/`)
- PFX passwords encrypted at rest using Fernet (key derived from Flask secret_key)
- CSRF protection: Flask-WTF CSRFProtect applied globally
- Rate limiting: Login (10/min), Initial setup (5/min) — in-memory storage
- Security headers: CSP, X-Frame-Options: DENY, X-Content-Type-Options: nosniff, Referrer-Policy, Permissions-Policy
- Request tracking: X-Request-ID header on all responses
- Session timeout: 8 hours (`permanent_session_lifetime`)
- Custom error pages: 404/403/500 with JSON for API endpoints
- File download routes validate paths against allowed base directories
- `AUDIT_PFX_PASSWORD` env var passes passwords to subprocesses (avoids CLI exposure)

## Known Gaps & Technical Debt

### Architecture
- **SQLite only** — no concurrent writer support, limits multi-process deployments
- **Jobs via threading.Thread** — no retry, no timeout, no persistence across restarts
- **Report schedules** exist in DB but nothing executes them
- **Alert rules** exist in DB but no evaluation engine triggers them
- **In-memory rate limiter** — resets on restart; needs Redis for multi-process
- **No API authentication** — no bearer tokens, no API keys
- **Frontend polls** for job updates — no real-time push

### Security
- CSP allows `'unsafe-inline'` for scripts and styles (`app.py:282-288`)
- Key derivation uses SHA256 (`database.py:24`) instead of PBKDF2/scrypt
- No SRI hashes on CDN resources in `base.html`
- No HSTS header / HTTPS enforcement
- Binary auth model (admin/non-admin) — no granular RBAC
- No SSO/SAML

### Testing & DevOps
- No CI/CD pipeline
- No Docker support
- 11 of 18 blueprints lack dedicated test coverage
- No PowerShell Pester tests

### Missing Features
- No PDF report generation
- No email/webhook notifications
- No PSA/RMM integration
- No automated BPA remediation
- No approval workflows for destructive operations

## Deployment

### Development
```bash
python portal/app.py
# Runs on http://127.0.0.1:5000
```

### Production
```bash
pip install gunicorn
cd portal && gunicorn -w 4 -b 127.0.0.1:5000 "app:create_app()"
```

Place behind a reverse proxy (nginx, Caddy) for HTTPS, static files, proxy headers. Set `FLASK_ENV=production`. Back up `config/portal.json` — losing the secret key invalidates all encrypted PFX passwords.

## Dependencies

### Python Packages (`requirements.txt`)
- Flask==3.1.3 — web framework
- cryptography==44.0.0 — Fernet encryption for PFX passwords
- Flask-WTF==1.2.2 — CSRF protection
- Flask-Limiter==3.8.0 — rate limiting

### PowerShell Modules (via `Build-Toolkit.ps1`)
PnP.PowerShell, MSAL.PS, Microsoft.Graph.Authentication, Microsoft.Graph.Applications, Microsoft.Graph.Users, Microsoft.Graph.Groups, Microsoft.Graph.DirectoryObjects, Microsoft.Online.SharePoint.PowerShell, ExchangeOnlineManagement, ImportExcel
