# CLAUDE.md — SPO-Audit-Toolkit

## Project Overview

SPO-Audit-Toolkit is a **Microsoft 365 security audit and breach investigation toolkit** designed for MSPs and IT administrators. It provides two interfaces:

1. **CLI toolkit** (`toolkit.ps1`) — interactive PowerShell menu for single-tenant auditing
2. **Web portal** (`portal/app.py`) — multi-tenant Flask web app for managing hundreds of client tenants

Both interfaces use **certificate-based app-only authentication** (Entra ID / Azure AD) to connect to SharePoint Online, Exchange Online, and Microsoft Graph without storing user credentials.

## Repository Structure

```
SPO-Audit-Toolkit/
├── toolkit.ps1              # Main CLI entry point (PowerShell menu)
├── start.cmd                # Windows launcher for toolkit.ps1 (finds pwsh)
├── run.cmd                  # Windows launcher for the Flask web portal
├── Build-Toolkit.ps1        # Downloads all required PowerShell modules to ./modules/
├── requirements.txt         # Python dependencies (Flask, cryptography)
├── config/
│   └── settings.json        # CLI toolkit config (tenant, cert paths, defaults)
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
│   ├── app.py               # Flask app (routes, job runner, PS subprocess management)
│   ├── database.py          # SQLite persistence (tenants, jobs, audit log, users)
│   ├── static/css/portal.css
│   └── templates/           # Jinja2 templates (Bootstrap 5, dark theme)
│       ├── base.html         # Layout with sidebar navigation
│       ├── dashboard.html
│       ├── tenants.html
│       ├── tenant_detail.html
│       ├── audit_spo.html    # SPO audit launch page
│       ├── audit_security.html  # Security scan (365Inspect) page
│       ├── audit_breach.html    # Breach investigation page
│       ├── jobs.html
│       ├── reports.html
│       ├── audit_trail.html
│       ├── login.html
│       ├── initial_setup.html
│       └── settings.html
├── inspectors/              # 365Inspect security scanner (third-party, adapted)
│   ├── 365Inspect.ps1       # Main inspector orchestrator
│   ├── Inspectors/          # Individual security checks (~90 inspector pairs)
│   │   ├── Inspect-*.ps1    # PowerShell inspection logic
│   │   └── Inspect-*.json   # Finding metadata (description, remediation, impact)
│   └── Utilities/
│       └── Write-ErrorLog.ps1
└── .gitignore
```

### Directories created at runtime (gitignored)

- `bin/` — optional bundled PowerShell 7 runtime
- `modules/` — PowerShell modules downloaded by `Build-Toolkit.ps1`
- `certs/` — generated certificates (CLI mode)
- `output/` — audit report output (CLI mode)
- `tenants/` — per-tenant data, certs, and output (portal mode)
- `data/` — SQLite database (`portal.db`)
- `logs/` — application logs (`portal.log`)

## Technology Stack

### PowerShell (audit scripts)
- **Requires**: PowerShell 7+ (pwsh)
- **Key modules**: PnP.PowerShell, MSAL.PS, Microsoft.Graph.*, ExchangeOnlineManagement, ImportExcel
- **Auth**: Certificate-based app-only (Entra ID registered app with X.509 cert)
- **Output**: XLSX (via ImportExcel) with CSV fallback

### Python (web portal)
- **Python**: 3.8+
- **Framework**: Flask 3.0+
- **Database**: SQLite with WAL mode
- **Encryption**: cryptography (Fernet) for PFX passwords at rest
- **Password hashing**: Werkzeug (bcrypt-based)
- **Frontend**: Bootstrap 5.3 (dark theme), Bootstrap Icons, Jinja2 templates

## Key Architecture Decisions

### Authentication Model
All connections to M365 services use **certificate-based app-only auth**. No user passwords are stored. The flow is:
1. Generate self-signed cert (CER + PFX)
2. Register Entra app, upload CER, assign API permissions
3. Connect using ClientId + PFX + PFX password

The PFX password is the only secret:
- **CLI mode**: prompted once per session, cached in `$script:SessionPfxPassword`
- **Portal mode**: encrypted with Fernet (derived from Flask `secret_key`), stored in SQLite

### Portal Job Execution
The portal runs PowerShell scripts as background subprocesses via `threading.Thread`. Output is streamed line-by-line into SQLite (`jobs` table). Job status is polled by the frontend via `/jobs/<id>/status`.

### Shared Module Pattern
All audit scripts import `scripts/Shared.psm1` which provides:
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

The `365Inspect.ps1` orchestrator loads all inspectors, runs them, and generates HTML/CSV/JSON/XML reports.

## Development Conventions

### PowerShell Scripts
- Use `Set-StrictMode -Version Latest` and `$ErrorActionPreference = "Stop"`
- Use `#requires` directives for module dependencies
- Use `[CmdletBinding()]` with typed parameters
- Use `[System.Collections.Generic.List[PSCustomObject]]` instead of `@()` array concatenation
- Log with pattern: `Write-Host "$(Get-Date) INFO: message"` (also `WARN:` for warnings)
- Use `Write-Progress` for long operations with counter/total tracking
- Always `Disconnect-PnPOnline` or `Disconnect-ExchangeOnline` after use
- Export data via `Export-AuditData` from Shared.psm1 (consistent XLSX/CSV output)
- File naming: `{Prefix}_{SiteName}_{yyyyMMdd-HHmm}.xlsx`
- Setup scripts output JSON on last stdout line for portal parsing

### Python / Flask
- Routes grouped by feature with comment dividers
- All routes behind `@login_required` decorator (except `/login`, `/initial-setup`)
- API endpoints return `jsonify({"ok": True/False, ...})`
- Path traversal protection on file downloads (validates against allowed base directories)
- Database operations use context manager (`with get_db() as conn:`)
- Subprocess environment set up via `build_ps_env()` (injects local module path + PFX password)

### Templates
- Extend `base.html` with `{% block content %}`
- Bootstrap 5 dark theme (`data-bs-theme="dark"`)
- Sidebar navigation with Bootstrap Icons
- Flash messages rendered in base template

### Configuration
- `config/settings.json` — CLI toolkit settings (tenant, app, cert paths)
- `config/portal.json` — portal config (auto-generated Flask secret key)
- Portal tenant data stored in SQLite, not JSON files

## Common Tasks

### First-time setup (CLI)
```powershell
# 1. Download PowerShell modules
.\Build-Toolkit.ps1

# 2. Launch toolkit
.\start.cmd
# Then use menu options 1 (cert), 2 (Entra app), 3 (configure tenant)
```

### First-time setup (Portal)
```cmd
# 1. Download PowerShell modules
powershell -File Build-Toolkit.ps1

# 2. Launch portal
run.cmd
# Visit http://127.0.0.1:5000, create admin account
```

### Adding a new audit script
1. Create `scripts/New-AuditScript.ps1` following the existing parameter pattern
2. Import `Shared.psm1` at the top
3. Use `Get-AuditCertificatePassword` for PFX password resolution
4. Use `Connect-AuditSite` or `Get-AuditGraphToken` for connections
5. Use `Export-AuditData` for output
6. Wire it into `toolkit.ps1` menu (CLI) and/or `portal/app.py` routes (portal)

### Adding a new inspector
1. Create `inspectors/Inspectors/Inspect-NewCheck.ps1` with a function returning messages
2. Create matching `inspectors/Inspectors/Inspect-NewCheck.json` with finding metadata
3. The 365Inspect orchestrator auto-discovers new inspectors

## Security Considerations

- **Never commit**: certificates (`*.pfx`, `*.cer`, `*.pem`), credentials, audit output (`*.csv`, `*.xlsx`), database (`data/`), tenant data (`tenants/`)
- PFX passwords are encrypted at rest in the portal database using Fernet
- The Flask secret key is auto-generated and stored in `config/portal.json`
- File download routes validate paths against allowed base directories
- The portal uses session-based auth with werkzeug password hashing
- All PowerShell scripts use `$ErrorActionPreference = "Stop"` for fail-fast behavior
- Environment variable `AUDIT_PFX_PASSWORD` is used to pass passwords to subprocesses (avoids command-line exposure)

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
- Flask >= 3.0.0
- cryptography >= 42.0.0
