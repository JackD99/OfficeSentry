# Contributing to SPO-Audit-Toolkit

Thank you for your interest in contributing. This document covers the development setup, coding conventions, and how to submit changes.

## Development Setup

### Prerequisites

- Python 3.9+
- PowerShell 7+ (`pwsh`)
- pip

### Getting started

```bash
# Clone the repository
git clone https://github.com/JackD99/SPO-Audit-Toolkit.git
cd SPO-Audit-Toolkit

# Install Python dependencies
pip install -r requirements.txt

# Install test dependencies
pip install pytest

# Download PowerShell modules (optional, needed for audit execution)
pwsh -File Build-Toolkit.ps1

# Run the portal in development mode
python portal/app.py
```

### Running tests

```bash
python -m pytest tests/ -v
```

All tests must pass before submitting changes. The test suite uses an isolated temporary SQLite database and resets state between tests.

## Project Structure

- `portal/` -- Flask web application (Python)
- `scripts/` -- PowerShell audit and setup scripts
- `inspectors/` -- 365Inspect security scanner
- `tests/` -- pytest test suite

## Coding Conventions

### Python / Flask

- **App factory pattern** in `app.py` via `create_app()`
- **Blueprints** for route organization under `portal/routes/`
- All routes behind `@login_required` (except `/login`, `/initial-setup`)
- API endpoints return `jsonify({"ok": True/False, ...})`
- CSRF protection via Flask-WTF on all POST routes
- Database operations use the `with get_db() as conn:` context manager
- Use `logging.getLogger("portal")` for logging

### PowerShell Scripts

- Use `Set-StrictMode -Version Latest` and `$ErrorActionPreference = "Stop"`
- Use `[CmdletBinding()]` with typed parameters
- Import Shared.psm1: `$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"`
- Log with: `Write-Host "$(Get-Date) INFO: message"`
- Export data via `Export-AuditData` from Shared.psm1
- Always disconnect sessions (`Disconnect-PnPOnline`, etc.)

### Templates

- Extend `base.html` with `{% block content %}`
- Bootstrap 5 dark theme (`data-bs-theme="dark"`)
- Use `url_for()` with blueprint-qualified names
- Include CSRF tokens in all forms

### Testing

- Use `client` fixture for unauthenticated requests
- Use `authed_client` for authenticated requests
- Use `app_csrf` / `client_csrf` for CSRF enforcement tests
- Database resets automatically before each test

## Adding a New Audit Script

1. Create `scripts/New-AuditScript.ps1` following existing parameter patterns
2. Add `#requires -Modules <module>` directive
3. Import Shared.psm1 and use its connection/export helpers
4. Wire it into the appropriate Blueprint in `portal/routes/`
5. Add a template or update an existing one
6. Add tests for the new route

## Adding a New Inspector

1. Create `inspectors/Inspectors/Inspect-NewCheck.ps1` with a function returning finding messages
2. Create matching `inspectors/Inspectors/Inspect-NewCheck.json` with finding metadata
3. The 365Inspect orchestrator auto-discovers new inspectors

## Submitting Changes

1. Fork the repository
2. Create a feature branch from `main`
3. Make your changes with clear, descriptive commits
4. Run the full test suite (`python -m pytest tests/ -v`)
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
