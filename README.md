# SPO-Audit-Toolkit

**Microsoft 365 Security Audit & Breach Investigation Toolkit**

A multi-tenant, self-hosted toolkit for MSPs and IT administrators to run repeatable Microsoft 365 audits and investigation workflows across hundreds of client tenants from a single web portal.

---

## Overview

SPO-Audit-Toolkit combines:

- **Web Portal** -- A Flask-based multi-tenant dashboard for tenant management, job execution, reporting, and audit trail
- **PowerShell Audit Scripts** -- A library of scripts for SharePoint Online, Exchange Online, and Microsoft Graph auditing
- **365Inspect Security Scanner** -- An integrated security configuration scanner with 108 individual checks
- **Certificate-Based Auth** -- App-only authentication via Entra ID (Azure AD) with no user credentials stored

## Key Features

| Feature | Description |
|---|---|
| **Multi-Tenant Management** | Onboard and manage hundreds of M365 tenants from one portal |
| **Certificate-Based Auth** | App-only auth via Entra ID -- no user passwords stored |
| **SPO Auditing** | Site permissions, groups, external sharing, user permissions |
| **Security Scanning** | 108 configuration checks via 365Inspect integration |
| **Breach Investigation** | Unified Audit Log, inbox rules, sign-in logs, OAuth consent, message trace |
| **Batch Operations** | Run audits across multiple tenants simultaneously |
| **Job Management** | Background execution with live progress streaming |
| **Audit Trail** | Full logging of who ran what, when, against which tenant |
| **Report Management** | Browse, download, and manage audit outputs (XLSX, CSV, HTML) |
| **User Management** | Admin and operator roles with password management |

## Architecture

```
                    +-------------------+
                    |   Web Browser     |
                    | (Bootstrap 5 UI)  |
                    +--------+----------+
                             |
                    +--------v----------+
                    |   Flask Portal    |
                    |  (app.py)         |
                    +--+-----+------+--+
                       |     |      |
              +--------+  +--+--+  +--------+
              |           |     |           |
     +--------v---+ +----v----+ +----------v----+
     | SQLite DB   | |  PS 7   | | File System   |
     | (database)  | | Scripts | | (certs, output)|
     +-------------+ +----+----+ +---------------+
                          |
                 +--------v--------+
                 |  Microsoft 365  |
                 |  (Graph, SPO,   |
                 |   Exchange)     |
                 +-----------------+
```

### Technology Stack

| Layer | Technology |
|---|---|
| **Backend** | Python 3.9+, Flask 3.1, SQLite (WAL mode) |
| **Frontend** | Bootstrap 5.3 (dark theme), Jinja2, Bootstrap Icons |
| **Audit Engine** | PowerShell 7+, PnP.PowerShell, Microsoft.Graph, ExchangeOnlineManagement |
| **Security** | Fernet encryption (PFX passwords), CSRF protection, rate limiting, CSP headers |
| **Auth Model** | Certificate-based app-only (Entra ID / Azure AD) |

## Prerequisites

- **Python** 3.9 or later
- **PowerShell** 7+ (`pwsh`) for audit script execution
- **pip** for Python dependency management
- A Microsoft 365 tenant with administrative access for Entra app registration

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/JackD99/SPO-Audit-Toolkit.git
cd SPO-Audit-Toolkit
```

### 2. Download PowerShell modules

```bash
pwsh -File Build-Toolkit.ps1
```

This downloads required PowerShell modules (PnP.PowerShell, Microsoft.Graph, ExchangeOnlineManagement, etc.) into the `modules/` directory.

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 4. Launch the portal

**Windows:**
```cmd
start.cmd
```

**Linux / macOS:**
```bash
./setup.sh --run
```

**Manual:**
```bash
python portal/app.py
```

### 5. Initial setup

1. Open http://127.0.0.1:5000 in your browser
2. Create your admin account on the initial setup page
3. Add your first tenant (name + domain)
4. Generate a certificate and create an Entra app through the tenant setup wizard
5. Run your first audit

## Tenant Onboarding Flow

```
1. Add Tenant          Enter tenant name, domain, and M365 tenant ID
        |
        v
2. Generate Cert       Creates a self-signed X.509 certificate (PFX + CER)
        |
        v
3. Create Entra App    Registers an app in Azure AD, uploads the certificate,
        |              and assigns API permissions
        v
4. Configure Client    Copy the Client ID from job output into tenant settings
        |
        v
5. Run Audits          SPO audit, security scan, or breach investigation
```

## Available Audits

### SharePoint Online Audits
- **Groups Audit** -- Site groups and their members
- **Site Permissions** -- Full permission snapshot for a site
- **External Sharing** -- External users and sharing links
- **User Permissions** -- A specific user's permissions across all sites

### Security Scanning (365Inspect)
- 108 individual security configuration checks
- Covers Exchange, SharePoint, Teams, Azure AD, and more
- Multiple output formats: HTML, CSV, JSON, XML

### Breach Investigation
- **Unified Audit Log** -- Search the M365 audit log for suspicious activity
- **Inbox Rules** -- Detect malicious forwarding rules (BEC indicator)
- **Sign-In Logs** -- Analyze Azure AD sign-in activity
- **OAuth Consent** -- Review OAuth consent grants for suspicious apps
- **Message Trace** -- Trace email message flow

## Project Structure

```
SPO-Audit-Toolkit/
+-- portal/                  # Flask web application
|   +-- app.py               # App factory, config, middleware
|   +-- database.py          # SQLite persistence layer
|   +-- helpers.py            # Auth decorators, PS runner, utilities
|   +-- routes/              # Blueprint route modules
|   |   +-- auth.py          # Login, logout, user management
|   |   +-- tenants.py       # Tenant CRUD, cert/app setup
|   |   +-- audits.py        # Audit execution routes
|   |   +-- jobs.py          # Job status and cancellation
|   |   +-- reports.py       # Report browser, downloads, audit trail
|   +-- templates/           # Jinja2 templates (Bootstrap 5 dark theme)
|   +-- static/css/          # Custom stylesheets
+-- scripts/                 # PowerShell audit & setup scripts
|   +-- Shared.psm1          # Shared module (connections, export, helpers)
|   +-- Audit-*.ps1          # Audit scripts
|   +-- Get-*.ps1            # Data retrieval scripts
|   +-- Search-*.ps1         # Search scripts
|   +-- Setup-*.ps1          # Setup/teardown scripts
+-- inspectors/              # 365Inspect security scanner
|   +-- 365Inspect.ps1       # Orchestrator
|   +-- Inspectors/          # 108 inspector pairs (.ps1 + .json)
+-- tests/                   # pytest test suite
+-- config/                  # Portal configuration (gitignored)
+-- requirements.txt         # Python dependencies
+-- Build-Toolkit.ps1        # PowerShell module downloader
+-- setup.sh                 # Linux/macOS launcher
+-- start.cmd                # Windows launcher
```

### Runtime Directories (gitignored)

| Directory | Purpose |
|---|---|
| `data/` | SQLite database (`portal.db`) |
| `tenants/` | Per-tenant data, certificates, and audit output |
| `modules/` | PowerShell modules (downloaded by `Build-Toolkit.ps1`) |
| `logs/` | Application logs with rotation (`portal.log`) |
| `bin/` | Optional bundled PowerShell 7 runtime |

## Configuration

### Portal Configuration

The portal auto-generates its configuration on first run at `config/portal.json`:

```json
{
  "secret_key": "<auto-generated-64-char-hex>"
}
```

The secret key is used for Flask session management and Fernet encryption of PFX passwords. **Back up this file** -- losing it invalidates all encrypted passwords and active sessions.

### Environment Variables

| Variable | Description | Default |
|---|---|---|
| `FLASK_ENV` | Set to `production` for production deployments | `development` |
| `AUDIT_PFX_PASSWORD` | PFX password passed to PowerShell subprocesses | Set automatically by portal |

## Security

### Portal Security Controls

- **CSRF Protection** -- Flask-WTF CSRFProtect on all POST routes
- **Rate Limiting** -- Login limited to 10 requests/minute
- **Session Timeout** -- Sessions expire after 8 hours
- **Security Headers** -- CSP, X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy
- **Encrypted Secrets** -- PFX passwords encrypted at rest with Fernet
- **Path Traversal Protection** -- File downloads validated against allowed directories
- **Password Hashing** -- Werkzeug bcrypt-based hashing

### What Is Never Stored

- User credentials for M365 tenants
- Plaintext PFX passwords
- Interactive login tokens

### What to Protect

- `config/portal.json` -- Contains the Flask secret key
- `data/portal.db` -- Contains tenant metadata and encrypted secrets
- `tenants/` -- Contains certificates and audit output

See [SECURITY.md](SECURITY.md) for the full security policy and vulnerability reporting instructions.

## Testing

Run the test suite from the repository root:

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -v
```

The test suite covers:
- **Authentication** -- Login, logout, initial setup, password management, session protection
- **Database** -- CRUD operations, encryption roundtrip, schema versioning
- **Routes** -- Access control, API response formats, tenant operations
- **Security** -- CSRF enforcement, path traversal protection, security headers

## Production Deployment

For production use, run behind a WSGI server with a reverse proxy:

```bash
pip install gunicorn
cd portal && gunicorn -w 4 -b 127.0.0.1:5000 "app:create_app()"
```

Place behind nginx or Caddy to provide:
- HTTPS termination with a valid TLS certificate
- Static file serving for `portal/static/`
- Proxy headers (`X-Forwarded-For`, `X-Forwarded-Proto`)

Set `FLASK_ENV=production` and ensure the secret key in `config/portal.json` is backed up.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines, coding conventions, and how to submit changes.

## License

This project is provided as-is for use by MSPs and IT administrators. See the repository for license details.
