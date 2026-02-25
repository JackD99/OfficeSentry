# CLAUDE.md — SPO-Audit-Toolkit

## Project Overview

SPO-Audit-Toolkit is a **Microsoft 365 security audit and breach investigation toolkit** designed for MSPs and IT administrators. It provides a **web portal** (`portal/app.py`) — a multi-tenant Flask web app for managing hundreds of client tenants.

The portal uses **certificate-based app-only authentication** (Entra ID / Azure AD) to connect to SharePoint Online, Exchange Online, and Microsoft Graph without storing user credentials.

## Repository Structure

```
SPO-Audit-Toolkit/
├── README.md                # Project documentation and setup guide
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
├── scripts/                 # PowerShell audit & setup scripts
│   ├── Shared.psm1          # Shared module (connection helpers, export, password handling)
│   ├── Audit-Groups.ps1     # Site groups + members audit
│   ├── Audit-SitePermissions.ps1  # Full permission snapshot
│   ├── Audit-ExternalSharing.ps1  # External users + sharing links
│   ├── Audit-InboxRules.ps1       # Inbox rules (BEC detection)
│   ├── Audit-OAuthConsent.ps1     # OAuth consent grant review
│   ├── Get-SharePointTenantPermissions.ps1  # User permissions across all sites
│   ├── Get-SignInLogs.ps1         # Azure AD sign-in log analysis
│   ├── Get-MessageTrace.ps1       # Exchange message trace
│   ├── Search-UnifiedAuditLog.ps1 # Unified Audit Log search
│   ├── Setup-Cert.ps1       # Non-interactive cert generation (for portal)
│   ├── Setup-App.ps1        # Non-interactive Entra app creation (for portal)
│   └── Setup-RemoveApp.ps1  # Non-interactive Entra app removal (for portal)
├── portal/                  # Flask web application
│   ├── app.py               # App factory, config, middleware (CSP, logging)
│   ├── database.py          # SQLite persistence (tenants, jobs, audit log, users)
│   ├── helpers.py           # Shared helpers (auth decorator, PS runner, env setup)
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── auth.py          # login, logout, initial_setup, settings
│   │   ├── tenants.py       # tenant CRUD, setup (cert, app)
│   │   ├── audits.py        # SPO audit, security scan, breach investigation, batch
│   │   ├── jobs.py          # job status, polling
│   │   └── reports.py       # report listing, downloads, audit trail
│   ├── static/css/portal.css
│   └── templates/           # Jinja2 templates (Bootstrap 5, dark theme)
│       ├── base.html         # Layout with sidebar navigation
│       ├── dashboard.html
│       ├── tenants.html
│       ├── tenant_detail.html
│       ├── audit_spo.html    # SPO audit launch page
│       ├── audit_security.html  # Security scan (365Inspect) page
│       ├── audit_breach.html    # Breach investigation page
│       ├── batch.html        # Batch operations page
│       ├── jobs.html
│       ├── reports.html
│       ├── audit_trail.html
│       ├── login.html
│       ├── initial_setup.html
│       └── settings.html
├── inspectors/              # 365Inspect security scanner (third-party, adapted)
│   ├── 365Inspect.ps1       # Main inspector orchestrator
│   ├── Inspectors/          # Individual security checks (108 inspector pairs)
│   │   ├── Inspect-*.ps1    # PowerShell inspection logic
│   │   └── Inspect-*.json   # Finding metadata (description, remediation, impact)
│   └── Utilities/
│       └── Write-ErrorLog.ps1
├── tests/                   # Python test suite (pytest)
│   ├── conftest.py          # Shared fixtures, in-memory SQLite, test client
│   ├── test_auth.py         # Login, logout, initial setup, session tests
│   ├── test_database.py     # CRUD operations, encryption, schema integrity
│   ├── test_routes.py       # Route access control, API response formats
│   └── test_security.py     # Path traversal, CSRF enforcement, CSP headers
└── .gitignore
```

### Directories created at runtime (gitignored)

- `bin/` — optional bundled PowerShell 7 runtime
- `modules/` — PowerShell modules downloaded by `Build-Toolkit.ps1`
- `tenants/` — per-tenant data, certs, and output
- `data/` — SQLite database (`portal.db`)
- `logs/` — application logs (`portal.log`, with rotation)

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
- **Rate Limiting**: Flask-Limiter (10/min on login)
- **Database**: SQLite with WAL mode, version-tracked schema migrations
- **Encryption**: cryptography (Fernet) for PFX passwords at rest
- **Password hashing**: Werkzeug (bcrypt-based)
- **Frontend**: Bootstrap 5.3 (dark theme), Bootstrap Icons, Jinja2 templates

## Key Architecture Decisions

### Authentication Model
All connections to M365 services use **certificate-based app-only auth**. No user passwords are stored. The flow is:
1. Generate self-signed cert (CER + PFX) via the portal's tenant setup
2. Register Entra app, upload CER, assign API permissions
3. Connect using ClientId + PFX + PFX password

The PFX password is encrypted with Fernet (derived from Flask `secret_key`) and stored in SQLite.

### Portal Architecture
The portal uses Flask Blueprints to organize routes by feature:
- `routes/auth.py` — login, logout, initial setup, password change
- `routes/tenants.py` — tenant CRUD, cert generation, Entra app management
- `routes/audits.py` — SPO audit, security scan, breach investigation, batch operations
- `routes/jobs.py` — job status polling
- `routes/reports.py` — report listing, file downloads, audit trail

Shared helpers (`helpers.py`) provide auth decorators, PowerShell subprocess management, and tenant filesystem operations.

### Job Execution
The portal runs PowerShell scripts as background subprocesses via `threading.Thread`. Output is streamed line-by-line into SQLite (`jobs` table). Job status is polled by the frontend via `/jobs/<id>/status`. Batch operations (`/batch/audit`) allow running security scans or SPO audits across multiple tenants simultaneously.

### Shared Module Pattern
All audit scripts import `scripts/Shared.psm1` using `Join-Path $PSScriptRoot "Shared.psm1"` which provides:
- `Connect-AuditSite` — PnP connection with retry logic
- `Get-AuditGraphToken` — MSAL token acquisition
- `Invoke-GraphRequest` — paginated Graph API calls
- `Get-AuditCertificatePassword` — PFX password resolution (param > env var > prompt)
- `Export-AuditData` — XLSX/CSV export with consistent formatting
- `Get-PrincipalType` — classifies SharePoint principals (User, External, M365 Group, etc.)
- `Get-ExcludedSystemLists` — list of SharePoint system lists to skip

### Inspector Pattern (365Inspect)
Each inspector consists of two files:
- `Inspect-*.ps1` — PowerShell function that returns finding messages
- `Inspect-*.json` — metadata (FindingName, Description, Remediation, Impact, Service, References)

The `365Inspect.ps1` orchestrator loads all 108 inspectors, runs them, and generates HTML/CSV/JSON/XML reports.

### Database Schema Versioning
The SQLite database uses a simple `schema_version` table to track schema changes. On startup, `init_db()` checks the current version and applies any incremental migrations defined in the `_MIGRATIONS` dict in `database.py`. This is a lightweight approach — no Alembic dependency.

## Development Conventions

### PowerShell Scripts
- Use `Set-StrictMode -Version Latest` and `$ErrorActionPreference = "Stop"`
- Use `#requires` directives for module dependencies (e.g., `#requires -Modules ExchangeOnlineManagement`)
- Use `[CmdletBinding()]` with typed parameters
- Use `[System.Collections.Generic.List[PSCustomObject]]` instead of `@()` array concatenation
- Import Shared.psm1 using `Join-Path $PSScriptRoot "Shared.psm1"` (scripts live in `scripts/`)
- Log with pattern: `Write-Host "$(Get-Date) INFO: message"` (also `WARN:` for warnings)
- Use `Write-Progress` for long operations with counter/total tracking
- Always `Disconnect-PnPOnline` or `Disconnect-ExchangeOnline` after use
- Export data via `Export-AuditData` from Shared.psm1 (consistent XLSX/CSV output)
- File naming: `{Prefix}_{SiteName}_{yyyyMMdd-HHmm}.xlsx`
- Setup scripts output JSON on last stdout line for portal parsing

### Python / Flask
- App factory pattern in `app.py` (`create_app()`)
- Routes organized into Blueprints under `portal/routes/`
- All routes behind `@login_required` decorator (except `/login`, `/initial-setup`)
- API endpoints return `jsonify({"ok": True/False, ...})`
- CSRF protection via Flask-WTF on all POST routes; CSRF token exposed as `window.CSRF_TOKEN` in base template for fetch() calls
- Path traversal protection on file downloads (validates against allowed base directories)
- Database operations use context manager (`with get_db() as conn:`)
- Subprocess environment set up via `build_ps_env()` (injects local module path + PFX password)

### Templates
- Extend `base.html` with `{% block content %}`
- Bootstrap 5 dark theme (`data-bs-theme="dark"`)
- Sidebar navigation with Bootstrap Icons
- Flash messages rendered in base template
- All forms include CSRF token (hidden field for traditional forms, `window.CSRF_TOKEN` for fetch)
- url_for uses blueprint-qualified names (e.g., `url_for('tenants.tenant_detail', tenant_id=...)`)

### Configuration
- `config/portal.json` — portal config (auto-generated Flask secret key)
- Tenant data stored in SQLite, not JSON files

## Testing

### Python (pytest)
Run tests from the repository root:
```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -v
```

Test infrastructure:
- **conftest.py** — Flask test client fixture, temporary SQLite database (reset per test)
- **test_auth.py** — Login, logout, initial setup, password change, session protection
- **test_database.py** — User/tenant/job CRUD, encryption roundtrip, schema versioning
- **test_routes.py** — Route access control, API response formats, tenant operations
- **test_security.py** — CSRF enforcement, path traversal protection, CSP/security headers

Patterns:
- Use `client` fixture for unauthenticated requests, `authed_client` for authenticated
- Use `app_csrf` / `client_csrf` fixtures to test with CSRF enforcement enabled
- Database is automatically reset before each test via the `_clean_db` autouse fixture

### PowerShell (Pester)
No Pester tests currently exist. When adding them:
- Place test files in `tests/` as `*.Tests.ps1`
- Mock `Connect-PnPOnline`, `Connect-ExchangeOnline`, and Graph API calls
- Test parameter validation and output formatting

## Common Tasks

### First-time setup
```bash
# 1. Download PowerShell modules
pwsh -File Build-Toolkit.ps1

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Launch portal
# Windows:
start.cmd
# Linux/macOS:
./setup.sh --run

# Visit http://127.0.0.1:5000, create admin account
```

### Adding a new audit script
1. Create `scripts/New-AuditScript.ps1` following the existing parameter pattern
2. Add `#requires -Modules <module>` directive at the top
3. Import Shared.psm1: `$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"`
4. Use `Get-AuditCertificatePassword` for PFX password resolution
5. Use `Connect-AuditSite` or `Get-AuditGraphToken` for connections
6. Use `Export-AuditData` for output
7. Wire it into the appropriate Blueprint in `portal/routes/` and add a template

### Adding a new inspector
1. Create `inspectors/Inspectors/Inspect-NewCheck.ps1` with a function returning messages
2. Create matching `inspectors/Inspectors/Inspect-NewCheck.json` with finding metadata
3. The 365Inspect orchestrator auto-discovers new inspectors

## Security Considerations

- **Never commit**: certificates (`*.pfx`, `*.cer`, `*.pem`), credentials, audit output (`*.csv`, `*.xlsx`), database (`data/`), tenant data (`tenants/`)
- PFX passwords are encrypted at rest in the portal database using Fernet
- The Flask secret key is auto-generated and stored in `config/portal.json`
- **CSRF protection**: Flask-WTF CSRFProtect applied globally; all forms include CSRF tokens
- **Session timeout**: Sessions expire after 8 hours (`permanent_session_lifetime`)
- **Rate limiting**: Login route limited to 10 requests/minute via Flask-Limiter
- **CSP headers**: Content-Security-Policy set on all responses (allows self + Bootstrap CDN)
- **Security headers**: X-Content-Type-Options: nosniff, X-Frame-Options: DENY, Referrer-Policy, Permissions-Policy
- **Request tracking**: X-Request-ID header on all responses for request correlation
- **Input validation**: Domain, GUID, email, and URL validation on tenant and audit forms
- **Custom error pages**: Styled 404/403/500 error pages with JSON responses for API endpoints
- **Health endpoint**: `/health` endpoint for load balancer and monitoring integration
- **Database maintenance**: Backup utility, integrity checks, and old job purging
- **Log rotation**: RotatingFileHandler (10 MB, 5 backups) for `logs/portal.log`
- File download routes validate paths against allowed base directories
- The portal uses session-based auth with werkzeug password hashing
- All PowerShell scripts use `$ErrorActionPreference = "Stop"` for fail-fast behavior
- Environment variable `AUDIT_PFX_PASSWORD` is used to pass passwords to subprocesses (avoids command-line exposure)

## Deployment

### Development
```bash
python portal/app.py
# Runs on http://127.0.0.1:5000 with Flask's built-in server
```

### Production
For production deployments, use a WSGI server behind a reverse proxy:

```bash
# Install gunicorn
pip install gunicorn

# Run with gunicorn (from repository root)
cd portal && gunicorn -w 4 -b 127.0.0.1:5000 "app:create_app()"
```

Place behind a reverse proxy (nginx, Caddy) that provides:
- HTTPS termination with a valid TLS certificate
- Static file serving for `portal/static/`
- Proxy headers (X-Forwarded-For, X-Forwarded-Proto)

Set the `FLASK_ENV=production` environment variable. Ensure the Flask secret key in `config/portal.json` is backed up — losing it invalidates all encrypted PFX passwords and active sessions.

## Dependencies

### PowerShell Modules (downloaded via `Build-Toolkit.ps1`)
- PnP.PowerShell — SharePoint Online operations
- MSAL.PS — token acquisition for Graph API
- Microsoft.Graph.Authentication — Graph connection
- Microsoft.Graph.Applications — Entra app management
- Microsoft.Graph.Users — user operations
- Microsoft.Graph.Groups — group operations
- Microsoft.Graph.DirectoryObjects — directory queries
- Microsoft.Online.SharePoint.PowerShell — SPO admin
- ExchangeOnlineManagement — Exchange operations
- ImportExcel — XLSX export (optional, CSV fallback)

### Python Packages (`requirements.txt`)
- Flask==3.1.3 — web framework
- cryptography==44.0.0 — Fernet encryption for PFX passwords
- Flask-WTF==1.2.2 — CSRF protection
- Flask-Limiter==3.8.0 — rate limiting

## Known Gaps

- No database migration tool (Alembic) — uses a lightweight version-tracked approach in `database.py`
- No automated PowerShell tests (Pester) — only Python tests exist
- SQLite is not suitable for high-concurrency production use (WAL mode helps but has limits)
- No email/notification system for completed audits
- Login rate limiting uses in-memory storage (resets on restart); for multi-process deployments, configure a Redis backend for Flask-Limiter
