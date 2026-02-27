# Office Sentry

**Microsoft 365 Security Audit, Management & Breach Investigation Platform**

A self-hosted, multi-tenant MSP management platform for Microsoft 365 — built on Python + PowerShell. Manage hundreds of client tenants from a single web portal with certificate-based authentication, real-time job streaming, role-based access control, PDF reporting, and a REST API.

---

## Overview

Office Sentry is a self-hosted alternative to CIPP, combining:

- **Web Portal** — Multi-tenant Flask dashboard with 20+ route blueprints, 95 templates, and a modern dark UI
- **PowerShell Audit Scripts** — 45 scripts for SharePoint Online, Exchange Online, and Microsoft Graph
- **365Inspect Security Scanner** — 108 individual security configuration checks
- **Certificate-Based Auth** — App-only authentication via Entra ID (Azure AD) with no user credentials stored
- **Task Queue** — Celery + Redis for background jobs with real-time SSE streaming (thread-based fallback)
- **REST API** — Bearer token authenticated API for automation and integration
- **PDF Reporting** — Branded PDF reports with WeasyPrint for client-ready deliverables

## Key Features

| Category | Features |
|---|---|
| **Tenant Management** | Onboard hundreds of M365 tenants, certificate generation, Entra app lifecycle, GDAP relationships |
| **Auditing** | SPO permissions, groups, external sharing, security scanning (108 checks), breach investigation (5 types) |
| **MSP Operations** | 50+ tiered operations across Users, Exchange, Teams, SharePoint with RBAC enforcement |
| **Compliance** | BPA templates (50+ checks), secure score tracking, standards enforcement, alert rules |
| **Identity & Security** | Risky users/sign-ins, MFA reporting, Conditional Access management, email security, incident response playbooks |
| **Device Management** | Intune inventory, compliance policies, Autopilot, remote device actions |
| **Reporting** | Interactive report viewer, PDF generation, report comparison, scheduled delivery (SMTP), client-facing dashboard |
| **Monitoring** | Service health, license management, webhook notifications, real-time job streaming (SSE) |
| **Hybrid Identity** | On-premises AD Agent with HMAC-SHA256 authentication, user provisioning templates |
| **Platform** | REST API v1, role-based access control (5 roles), background task queue (Celery), webhook system |

## Architecture

```
                    +-------------------+
                    |   Web Browser     |
                    | (Bootstrap 5 UI)  |
                    +--------+----------+
                             |
                    +--------v----------+      +------------------+
                    |   Flask Portal    |<---->|  Redis (optional) |
                    |   (20 Blueprints) |      |  Celery + SSE    |
                    +--+-----+------+--+      +------------------+
                       |     |      |
              +--------+  +--+--+  +--------+
              |           |     |           |
     +--------v---+ +----v----+ +----------v----+
     | SQLite DB   | |  PS 7   | | File System   |
     | (33 tables) | | Scripts | | (certs, output)|
     +-------------+ +----+----+ +-------+-------+
                          |               |
                 +--------v--------+  +---v---+
                 |  Microsoft 365  |  | AD    |
                 |  (Graph, SPO,   |  | Agent |
                 |   Exchange)     |  +-------+
                 +-----------------+
```

### Technology Stack

| Layer | Technology |
|---|---|
| **Backend** | Python 3.9+, Flask 3.1, SQLite (WAL mode), Celery 5.4 + Redis |
| **Frontend** | Bootstrap 5.3 (dark theme), Jinja2, Chart.js, Bootstrap Icons, SRI hashes |
| **Audit Engine** | PowerShell 7+, PnP.PowerShell, Microsoft.Graph, ExchangeOnlineManagement |
| **Security** | Nonce-based CSP, HSTS, PBKDF2 key derivation, Fernet encryption, CSRF, rate limiting |
| **Auth Model** | Certificate-based app-only (Entra ID), Bearer token API, HMAC-SHA256 agent auth |
| **Reporting** | WeasyPrint PDF generation, SMTP email delivery, report comparison |
| **RBAC** | 5-tier roles: super_admin, admin, operator, viewer, client |

### Current Scale

| Component | Count |
|---|---|
| Route blueprints | 20 + dashboard |
| Templates | 95 (Jinja2, Bootstrap 5 dark theme) |
| Database tables | 33 (schema v9) |
| PowerShell scripts | 45 (.ps1 + .psm1) |
| Security inspectors | 108 (365Inspect) |
| Test files | 24 (658 tests) |
| Python dependencies | 7 |

## Prerequisites

- **Python** 3.9 or later
- **PowerShell** 7+ (`pwsh`) for audit script execution
- **pip** for Python dependency management
- **Redis** (optional) for Celery task queue and SSE streaming
- A Microsoft 365 tenant with administrative access for Entra app registration

### Optional Dependencies

- **WeasyPrint** requires system libraries (cairo, pango) for PDF generation — the portal works without it
- **Redis** enables Celery background jobs and real-time streaming — falls back to threading without it

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

## Available Audits & Operations

### SharePoint Online Audits
- **Groups Audit** — Site groups and their members
- **Site Permissions** — Full permission snapshot for a site
- **External Sharing** — External users and sharing links
- **User Permissions** — A specific user's permissions across all sites

### Security Scanning (365Inspect)
- 108 individual security configuration checks
- Covers Exchange, SharePoint, Teams, Azure AD, and more
- Multiple output formats: HTML, CSV, JSON, XML

### Breach Investigation
- **Unified Audit Log** — Search the M365 audit log for suspicious activity
- **Inbox Rules** — Detect malicious forwarding rules (BEC indicator)
- **Sign-In Logs** — Analyze Azure AD sign-in activity
- **OAuth Consent** — Review OAuth consent grants for suspicious apps
- **Message Trace** — Trace email message flow

### MSP Operations (50+)
- **User Management** — Create, disable, reset password, offboard users
- **Exchange** — Mailbox conversion, delegation, forwarding, transport rules
- **Teams** — Team management operations
- **SharePoint** — Site admin operations
- All operations enforced by RBAC tier (Tier 1-3)

### Incident Response Playbooks
- BEC Response
- Account Compromise
- Data Exfiltration
- Malware Outbreak
- Privilege Escalation

### PDF Reports
- Executive Summary — One-page risk overview
- Security Posture — 365Inspect findings by severity
- BPA Compliance — Best Practice Analysis pass/fail with guidance
- Identity Risk — MFA gaps, risky users
- License Optimization — Unused licenses and savings
- Breach Investigation — Timeline, IOCs, affected accounts

## Project Structure

```
SPO-Audit-Toolkit/
├── portal/                  # Flask web application
│   ├── app.py               # App factory, dashboard, middleware
│   ├── database.py          # SQLite (33 tables, schema v9)
│   ├── helpers.py           # Auth decorators, PS runner, validation
│   ├── permissions.py       # RBAC engine (5-tier hierarchy)
│   ├── celery_app.py        # Celery + Redis task queue
│   ├── tasks.py             # Background tasks, webhooks, email
│   ├── report_engine.py     # PDF generation (WeasyPrint)
│   ├── agent_auth.py        # HMAC-SHA256 agent authentication
│   ├── routes/              # 20 Blueprint modules
│   ├── templates/           # 95 Jinja2 templates
│   └── static/css/          # Dark theme design system (~1,720 lines)
├── scripts/                 # 45 PowerShell scripts
│   ├── Shared.psm1          # Shared module (connections, export)
│   ├── Audit-*.ps1          # SPO audit scripts
│   ├── Get-*.ps1            # Data retrieval scripts
│   ├── Invoke-*.ps1         # Action/admin scripts
│   ├── AD-Agent.ps1         # On-premises AD agent
│   └── Install-ADAgent.ps1  # AD Agent installer
├── inspectors/              # 365Inspect security scanner
│   ├── 365Inspect.ps1       # Orchestrator
│   └── Inspectors/          # 108 inspector pairs
├── tests/                   # 24 test files (658 tests)
├── requirements.txt         # Python dependencies
├── Build-Toolkit.ps1        # PowerShell module downloader
├── setup.sh                 # Linux/macOS launcher
└── start.cmd                # Windows launcher
```

### Runtime Directories (gitignored)

| Directory | Purpose |
|---|---|
| `data/` | SQLite database (`portal.db`) and backups |
| `tenants/` | Per-tenant data, certificates, and audit output |
| `modules/` | PowerShell modules (downloaded by `Build-Toolkit.ps1`) |
| `logs/` | Application logs with rotation (`portal.log`) |
| `config/` | Portal configuration (`portal.json`) |
| `bin/` | Optional bundled PowerShell 7 runtime |

## Configuration

### Portal Configuration

The portal auto-generates its configuration on first run at `config/portal.json`:

```json
{
  "secret_key": "<auto-generated-64-char-hex>"
}
```

The secret key is used for Flask session management and Fernet encryption of PFX passwords (PBKDF2 key derivation with 100,000 iterations). **Back up this file** — losing it invalidates all encrypted passwords and active sessions.

### Environment Variables

| Variable | Description | Default |
|---|---|---|
| `FLASK_ENV` | Set to `production` for production deployments | `development` |
| `AUDIT_PFX_PASSWORD` | PFX password passed to PowerShell subprocesses | Set automatically by portal |
| `CELERY_BROKER_URL` | Redis URL for Celery broker | `redis://localhost:6379/0` |
| `CELERY_RESULT_BACKEND` | Redis URL for Celery results | `redis://localhost:6379/0` |

## Security

### Portal Security Controls

| Control | Implementation |
|---|---|
| **CSRF Protection** | Flask-WTF CSRFProtect on all POST routes |
| **Rate Limiting** | Login: 10/min, Initial setup: 5/min (Flask-Limiter) |
| **Session Timeout** | 8 hours with permanent session |
| **Password Hashing** | Werkzeug bcrypt-based hashing |
| **Security Headers** | Nonce-based CSP, HSTS, X-Frame-Options: DENY, X-Content-Type-Options, Referrer-Policy, Permissions-Policy |
| **SRI Hashes** | Subresource Integrity on all CDN resources |
| **Encrypted Secrets** | Fernet encryption with PBKDF2-derived keys (100k iterations) |
| **Path Traversal** | File downloads validated against allowed base directories |
| **Audit Trail** | All operator actions logged with tenant, timestamp, and details |
| **RBAC** | 5-tier role hierarchy with tenant group scoping |
| **API Auth** | Bearer token with scoped API keys |
| **Agent Auth** | HMAC-SHA256 with replay protection (5-min window + nonce dedup) |
| **Request Tracking** | X-Request-ID header on all responses |

### What Is Never Stored

- User credentials for M365 tenants
- Plaintext PFX passwords
- Interactive login tokens

### What to Protect

- `config/portal.json` — Contains the Flask secret key (encryption master key)
- `data/portal.db` — Contains tenant metadata and encrypted secrets
- `tenants/` — Contains certificates and audit output

See [SECURITY.md](SECURITY.md) for the full security policy and vulnerability reporting instructions.

## REST API

The portal exposes a REST API at `/api/v1` with Bearer token authentication:

```bash
# List tenants
curl -H "Authorization: Bearer <api-key>" http://localhost:5000/api/v1/tenants

# Check health
curl http://localhost:5000/api/v1/health
```

API keys are managed in Settings and scoped to RBAC roles and tenant groups.

## Testing

Run the test suite from the repository root:

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -v
```

**658 tests** across 24 test files covering all blueprints:

| Area | Coverage |
|---|---|
| Authentication & RBAC | Login, logout, setup, roles, tenant scoping |
| Database | CRUD, encryption, schema migrations, integrity |
| All 20 Blueprints | Route access, API responses, input validation |
| Security | CSRF, CSP headers, path traversal, rate limiting |
| Platform | Celery tasks, SSE streaming, webhooks, API keys |
| Reporting | PDF generation, comparison, client dashboard, SMTP, branding |

## Production Deployment

For production use, run behind a WSGI server with a reverse proxy:

```bash
pip install gunicorn
cd portal && gunicorn -w 4 -b 127.0.0.1:5000 "app:create_app()"
```

### With Celery (recommended)

```bash
# Start Redis
redis-server

# Start Celery worker
cd portal && celery -A celery_app.celery worker -l info

# Start Celery Beat (for schedules and alerts)
cd portal && celery -A celery_app.celery beat -l info
```

### Reverse Proxy

Place behind nginx or Caddy to provide:
- HTTPS termination with a valid TLS certificate
- Static file serving for `portal/static/`
- Proxy headers (`X-Forwarded-For`, `X-Forwarded-Proto`)
- SSE connection support (disable buffering for `/jobs/*/stream`)

Set `FLASK_ENV=production` and ensure `config/portal.json` is backed up.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines, coding conventions, and how to submit changes.

## License

This project is provided as-is for use by MSPs and IT administrators. See the repository for license details.
