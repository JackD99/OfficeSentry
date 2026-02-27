# Contributing to SPO-Audit-Toolkit

Thank you for your interest in contributing! This document covers the development setup, coding conventions, and how to submit changes.

## Development Setup

### Prerequisites

- Python 3.9+
- PowerShell 7+ (`pwsh`)
- pip
- Redis (optional, for Celery task queue)

### Getting started

```bash
# Clone the repository
git clone https://github.com/JackD99/SPO-Audit-Toolkit.git
cd SPO-Audit-Toolkit

# Install Python dependencies
pip install -r requirements.txt

# Install dev/test dependencies
pip install -r requirements-dev.txt

# Download PowerShell modules (optional, needed for audit execution)
pwsh -File Build-Toolkit.ps1

# Run the portal in development mode
python portal/app.py
```

### Running tests

```bash
python -m pytest tests/ -v
```

The test suite has **658 tests across 24 files**. All tests must pass before submitting changes. Tests use an isolated temporary SQLite database that resets between each test.

## Project Structure

| Directory | Purpose |
|---|---|
| `portal/` | Flask web application — app factory, 20 blueprints, 95 templates, 33-table database |
| `portal/routes/` | Blueprint modules (auth, tenants, audits, reports, operations, etc.) |
| `portal/templates/` | Jinja2 templates (Bootstrap 5 dark theme) |
| `scripts/` | 45 PowerShell audit, setup, and admin scripts |
| `inspectors/` | 365Inspect security scanner (108 inspectors) |
| `tests/` | pytest test suite |

### Key Files

| File | Role |
|---|---|
| `portal/app.py` | App factory, dashboard route, middleware, security headers |
| `portal/database.py` | SQLite persistence — 33 tables, schema migrations, all CRUD functions |
| `portal/helpers.py` | Auth decorators, PowerShell runner, input validation |
| `portal/permissions.py` | RBAC engine — 5-tier role hierarchy, tenant group scoping |
| `portal/celery_app.py` | Celery + Redis task queue factory |
| `portal/tasks.py` | Celery tasks — job runner, webhooks, email delivery, schedules |
| `portal/report_engine.py` | PDF report generation (WeasyPrint) |
| `portal/agent_auth.py` | HMAC-SHA256 authentication for AD Agent API |

## Coding Conventions

### Python / Flask

- **App factory pattern** in `app.py` via `create_app()`
- **Blueprints** for route organization under `portal/routes/`
- All routes behind `@login_required` (except `/login`, `/initial-setup`, `/health`)
- Admin-only routes use `@admin_required`; role-gated routes use `@role_required(min_role)`
- API endpoints return `jsonify({"ok": True/False, ...})`
- CSRF protection via Flask-WTF on all POST routes (exempted on API and Agent endpoints)
- Database operations use `with get_db() as conn:` context manager
- Use `logging.getLogger("portal")` for logging
- Input validation via `helpers.py`: `validate_domain()`, `validate_tenant_guid()`, `validate_site_url()`, `validate_email()`, `sanitize_text()`

### PowerShell Scripts

- Use `Set-StrictMode -Version Latest` and `$ErrorActionPreference = "Stop"`
- Use `[CmdletBinding()]` with typed parameters
- Add `#requires -Modules <module>` directives
- Import Shared.psm1: `$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"`
- Log with: `Write-Host "$(Get-Date) INFO: message"` (also `WARN:` for warnings)
- Use `Write-Progress` for long operations
- Export data via `Export-AuditData` from Shared.psm1
- Always disconnect sessions (`Disconnect-PnPOnline`, `Disconnect-ExchangeOnline`)

### Templates

- Extend `base.html` with `{% block content %}`
- Bootstrap 5 dark theme (`data-bs-theme="dark"`)
- Use `url_for()` with blueprint-qualified names (e.g., `url_for('tenants.tenant_detail', tenant_id=...)`)
- Include CSRF tokens in all forms (`window.CSRF_TOKEN` for fetch calls)
- Add `nonce="{{ g.csp_nonce }}"` to all inline script/style tags

### Testing

- Use `client` fixture for unauthenticated requests
- Use `authed_client` for authenticated requests (logged in as admin)
- Use `app_csrf` / `client_csrf` for CSRF enforcement tests
- Database resets automatically before each test
- Mock PowerShell subprocess calls — don't test actual PS execution
- Follow `tests/test_operations.py` as a model for new test files

## Adding New Features

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
4. Add CRUD functions following existing patterns
5. All functions must use `with get_db() as conn:` context manager

### Adding a New Audit Script

1. Create `scripts/New-AuditScript.ps1` following existing parameter patterns
2. Add `#requires -Modules <module>` directive
3. Import Shared.psm1 and use its connection/export helpers
4. Wire it into the appropriate Blueprint in `portal/routes/`
5. Add a template or update an existing one
6. Add tests for the new route

### Adding a New Inspector

1. Create `inspectors/Inspectors/Inspect-NewCheck.ps1` with a function returning finding messages
2. Create matching `inspectors/Inspectors/Inspect-NewCheck.json` with finding metadata
3. The 365Inspect orchestrator auto-discovers new inspectors

## Submitting Changes

1. Fork the repository
2. Create a feature branch from `main`
3. Make your changes with clear, descriptive commits
4. Run the full test suite (`python -m pytest tests/ -v`) — all 658 tests must pass
5. Open a pull request with a description of what changed and why

### Commit Messages

- Use imperative mood ("Add feature" not "Added feature")
- Keep the first line under 72 characters
- Reference issue numbers where applicable

## Reporting Issues

Use the GitHub Issues tab to report bugs or request features. Include:
- Steps to reproduce (for bugs)
- Expected vs actual behavior
- Python/PowerShell version
- Operating system

## Security Vulnerabilities

For security vulnerabilities, **do not open a public issue**. See [SECURITY.md](SECURITY.md) for responsible disclosure instructions.
