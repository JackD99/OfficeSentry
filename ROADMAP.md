# SPO-Audit-Toolkit — Development Roadmap

## Purpose

Each task includes specific file paths, function signatures, code patterns, and acceptance criteria so changes can be implemented without ambiguity.

## Executive Summary

The SPO-Audit-Toolkit has grown from a SharePoint audit script collection into a substantial M365 management portal — **20 route blueprints, 95 templates, 45 PowerShell scripts, 108 security inspectors, and a 33-table SQLite database**. Phases 1-3 are complete — the platform now has a solid foundation with CI/CD, comprehensive test coverage, proper security hardening, Celery task queue, RBAC, REST API, webhooks, SSE real-time updates, an on-premises AD Agent system, PDF report generation, SMTP email delivery, report comparison, and a client-facing dashboard.

**Vision**: A self-hosted CIPP alternative — full MSP management platform built on Python + PowerShell.

**Deployment model**: Multi-instance self-hosted. Each MSP runs their own instance. Docker + PostgreSQL support are priorities.

**Strategy**: Core platform architecture first (task queue, RBAC, API, real-time), then expand features on a solid foundation. Phases 1-3 are complete.

---

## Current State Snapshot

### What's Working Well

- **Core audit pipeline**: 5 SPO audit types, 5 breach investigation types, 108-inspector security scanning, background job execution
- **MSP management**: Tenant CRUD with cert-based auth, 50+ tiered operations, BPA with 50+ checks, secure score tracking, alert rules, tenant groups, GDAP management, CA policies, Graph Explorer
- **Security & UI**: CSRF protection, rate limiting, security headers, Fernet encryption, modern dark UI with Chart.js dashboard
- **Newer modules**: Email security, identity protection, device management, service health, license management, incident response playbooks

### Critical Gaps (Updated — many resolved in Phase 1 & 2)

- ~~**Architecture**: jobs via threading.Thread, no API auth, no real-time updates~~ → ✅ Resolved: Celery task queue, REST API v1, SSE streaming
- **Architecture (remaining)**: SQLite only (no PostgreSQL), no Docker support
- ~~**Security**: CSP unsafe-inline, SHA256 key derivation, no SRI hashes, no HSTS, admin/non-admin only auth~~ → ✅ Resolved: Nonce-based CSP, PBKDF2, SRI, HSTS, 4-tier RBAC
- **Security (remaining)**: No SSO/SAML
- ~~**Testing**: 11 of 18 blueprints have zero test coverage, no CI/CD~~ → ✅ Resolved: 24 test files, 658 tests, GitHub Actions CI
- **Testing (remaining)**: No Docker, no Pester tests
- ~~**Features**: No PDF reports, no email notifications~~ → ✅ Resolved: WeasyPrint PDF generation, SMTP email delivery, report comparison, client dashboard
- **Features (remaining)**: No PSA integration, no auto-remediation, no approval workflows

---

## Phase 1: Foundation & Hardening ✅ COMPLETE

**Goal**: Fix security issues, establish CI/CD, expand test coverage, update docs. Make the app production-solid before adding features.

**Status**: All 7 tasks completed.

---

### 1.1 Security: Nonce-Based CSP (Replace unsafe-inline) ✅

**Problem**: `portal/app.py:282-288` sets CSP with `'unsafe-inline'` for both scripts and styles.

**Implementation**:

1. **Generate nonce per request** in `portal/app.py`:
```python
# In assign_request_id() (app.py ~line 245), add:
import secrets
g.csp_nonce = secrets.token_hex(16)
```

2. **Pass nonce to templates** — it's already available via `g.csp_nonce` in Jinja2 (Flask makes `g` available in templates).

3. **Update CSP header** in `set_security_headers()` (`app.py:280`):
```python
nonce = getattr(g, 'csp_nonce', '')
response.headers["Content-Security-Policy"] = (
    f"default-src 'self'; "
    f"script-src 'self' https://cdn.jsdelivr.net 'nonce-{nonce}'; "
    f"style-src 'self' https://cdn.jsdelivr.net 'nonce-{nonce}'; "
    f"font-src 'self' https://cdn.jsdelivr.net; "
    f"img-src 'self' data: blob:; "
    f"connect-src 'self' http://ip-api.com"
)
```

4. **Add nonce to all inline `<script>` and `<style>` tags** across all 84 templates:
   - Search all templates for `<script` and `<style` tags
   - Add `nonce="{{ g.csp_nonce }}"` attribute to each
   - The `base.html` template is the most important — it has the global inline scripts

5. **Update `test_security.py`** — the CSP header test needs to expect nonce format instead of unsafe-inline.

**Files to modify**: `portal/app.py`, `portal/templates/*.html` (all with inline scripts/styles), `tests/test_security.py`

**Acceptance criteria**: All pages load without CSP console errors. `test_security.py` passes. No `'unsafe-inline'` in CSP header.

**Implementation notes**: Nonce generated in `assign_request_id()` via `g.csp_nonce = secrets.token_hex(16)`. CSP header uses `'nonce-{nonce}'` for both `script-src` and `style-src`. All templates updated with `nonce="{{ g.csp_nonce }}"` on inline script/style tags.

---

### 1.2 Security: Upgrade Key Derivation (SHA256 → PBKDF2) ✅

**Problem**: `portal/database.py:24` uses `hashlib.sha256()` for key derivation — not a proper KDF.

**Implementation**:

1. **Replace `_derive_key`** in `database.py:24-25`:
```python
import hashlib, base64

def _derive_key(secret: str) -> bytes:
    """Derive a 32-byte Fernet key from the Flask secret using PBKDF2."""
    return hashlib.pbkdf2_hmac('sha256', secret.encode(), b'spo-audit-toolkit-v2', 100_000)
```

2. **Add re-encryption migration** in `database.py`. Add a new migration version (increment `SCHEMA_VERSION` to 6):
   - The migration should detect PFX passwords encrypted with old key derivation
   - Re-encrypt each with the new derivation
   - This requires the Flask secret_key, so it must run inside the app context
   - Add a `migrate_encryption()` function called from `app.py` after `init_db()`

3. **Migration strategy**: Try decrypting with old method; if it works, re-encrypt with new method. If old method fails, the value is already using new format (or is corrupt).

```python
def migrate_encryption(secret: str):
    """One-time migration: re-encrypt PFX passwords with PBKDF2 key derivation."""
    old_key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    new_key = base64.urlsafe_b64encode(_derive_key(secret))

    with get_db() as conn:
        rows = conn.execute("SELECT id, pfx_password_enc FROM tenants WHERE pfx_password_enc != ''").fetchall()
        for row in rows:
            try:
                from cryptography.fernet import Fernet
                plaintext = Fernet(old_key).decrypt(row["pfx_password_enc"].encode()).decode()
                new_enc = Fernet(new_key).encrypt(plaintext.encode()).decode()
                conn.execute("UPDATE tenants SET pfx_password_enc = ? WHERE id = ?", (new_enc, row["id"]))
            except Exception:
                pass  # Already migrated or corrupt
```

4. **Call from app.py** after `init_db()`:
```python
from database import migrate_encryption
migrate_encryption(app.secret_key)
```

**Files to modify**: `portal/database.py`, `portal/app.py`

**Acceptance criteria**: Existing encrypted passwords still decrypt correctly. New encryptions use PBKDF2. `test_database.py` passes (update encryption tests).

**Implementation notes**: `_derive_key()` uses `hashlib.pbkdf2_hmac('sha256', ..., b'spo-audit-toolkit-v2', 100_000)`. Legacy `_derive_key_legacy()` preserved. `migrate_encryption()` re-encrypts existing passwords on startup.

---

### 1.3 Security: SRI Hashes on CDN Resources ✅

**Problem**: `portal/templates/base.html` loads Bootstrap CSS/JS and Chart.js from CDN without Subresource Integrity hashes.

**Implementation**:

1. Find all `<link>` and `<script>` tags referencing `cdn.jsdelivr.net` in `base.html`
2. Generate SRI hashes: `curl -s <url> | openssl dgst -sha384 -binary | openssl base64 -A`
3. Add `integrity="sha384-<hash>" crossorigin="anonymous"` to each tag

**Files to modify**: `portal/templates/base.html`

**Acceptance criteria**: All CDN resources load with SRI attributes. No integrity errors in browser console.

**Implementation notes**: Bootstrap CSS and JS both have `integrity="sha384-..."` and `crossorigin="anonymous"` attributes in `base.html`.

---

### 1.4 Security: HSTS Header ✅

**Implementation**: Add to `set_security_headers()` in `portal/app.py:280`:
```python
if request.is_secure or os.environ.get("FLASK_ENV") == "production":
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
```

Only set when behind HTTPS or in production to avoid breaking local dev.

**Files to modify**: `portal/app.py`

**Implementation notes**: HSTS header set conditionally in `set_security_headers()` when `request.is_secure` or `FLASK_ENV=production`. Max-age: 31536000 (1 year) with `includeSubDomains`.

---

### 1.5 CI/CD Pipeline ✅

**Implementation**: Create `.github/workflows/ci.yml`:

```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt pytest pytest-cov ruff bandit
      - run: ruff check portal/ tests/
      - run: python -m pytest tests/ -v --tb=short
      - run: bandit -r portal/ -ll
```

Also create `requirements-dev.txt`:
```
pytest
pytest-cov
ruff
bandit
mypy
```

**Files to create**: `.github/workflows/ci.yml`, `requirements-dev.txt`

**Acceptance criteria**: CI runs on every push. Tests pass. Ruff has no errors. Bandit has no high/medium findings.

**Implementation notes**: `.github/workflows/ci.yml` created with Python 3.11, `requirements-dev.txt` with pytest, pytest-cov, ruff, bandit. Triggers on push and pull_request.

---

### 1.6 Test Coverage Expansion ✅

**Current gaps**: audits, jobs, reports, email_security, identity, devices, service_health, licenses, incidents — all have zero test coverage.

**Pattern for each new test file** (follow `tests/test_operations.py`):

```python
"""tests/test_<blueprint>.py"""

def test_page_requires_auth(client):
    """Unauthenticated access redirects to login."""
    resp = client.get("/<route>")
    assert resp.status_code in (302, 308)

def test_page_loads(authed_client):
    """Authenticated access returns 200."""
    resp = authed_client.get("/<route>")
    assert resp.status_code == 200

def test_post_endpoint(authed_client):
    """POST endpoint returns JSON with ok field."""
    resp = authed_client.post("/<route>", data={...})
    assert resp.status_code == 200
    data = resp.get_json()
    assert "ok" in data
```

**New test files needed** (in priority order):

| File | Routes to test | Key assertions |
|------|---------------|----------------|
| `tests/test_audits.py` | `/audits`, `/audits/spo`, `/audits/security`, `/audits/breach`, `/batch/audit` | Page loads, audit launch validates tenant, batch requires tenant selection |
| `tests/test_reports_routes.py` | `/reports`, `/report-viewer/*`, `/schedules/*`, `/audit-trail` | Page loads, download path validation, saved view CRUD |
| `tests/test_jobs_routes.py` | `/jobs`, `/jobs/<id>/status`, `/jobs/<id>/cancel` | Job listing, status returns JSON, cancel returns ok |
| `tests/test_email_security.py` | `/email-security/*` | All 5 sub-pages load, fetch endpoints validate tenant |
| `tests/test_identity.py` | `/identity/*` | Risky users/sign-ins pages load, MFA report validates input |
| `tests/test_devices.py` | `/devices/*` | Inventory/compliance/autopilot pages load, action validates tenant |
| `tests/test_incidents.py` | `/incidents/*` | Dashboard + all 5 playbook pages load, run endpoints validate |
| `tests/test_licenses.py` | `/licenses/*` | All 4 sub-pages load, fetch validates tenant |
| `tests/test_service_health.py` | `/service-health/*` | All 5 sub-pages load, scan validates tenant selection |

**Acceptance criteria**: All new tests pass. `python -m pytest tests/ -v` shows no failures.

**Implementation notes**: 23 test files now cover all blueprints. New test files: `test_audits.py`, `test_reports_routes.py`, `test_jobs_routes.py`, `test_email_security.py`, `test_identity.py`, `test_devices.py`, `test_incidents.py`, `test_licenses.py`, `test_service_health.py`, `test_ad_agent.py`, `test_api_v1.py`, `test_rbac.py`, `test_sse.py`, `test_celery_tasks.py`, `test_webhooks.py`.

---

### 1.7 Database Indexes ✅

**Problem**: Missing indexes on frequently-queried foreign key columns.

**Implementation**: Add to `_MIGRATIONS` dict as the next version in `database.py`:

```python
6: """
CREATE INDEX IF NOT EXISTS idx_jobs_tenant_id ON jobs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_audit_log_tenant_id ON audit_log(tenant_id);
CREATE INDEX IF NOT EXISTS idx_cached_tenant_data_lookup ON cached_tenant_data(tenant_id, data_type);
CREATE INDEX IF NOT EXISTS idx_bpa_results_tenant ON bpa_results(tenant_id);
CREATE INDEX IF NOT EXISTS idx_incident_cases_tenant ON incident_cases(tenant_id);
CREATE INDEX IF NOT EXISTS idx_service_health_cache_tenant ON service_health_cache(tenant_id);
CREATE INDEX IF NOT EXISTS idx_license_snapshots_tenant ON license_snapshots(tenant_id);
"""
```

Increment `SCHEMA_VERSION` accordingly.

**Files to modify**: `portal/database.py`

**Implementation notes**: 14 indexes added across migrations v6-v8 covering jobs, audit_log, cached_tenant_data, bpa_results, incident_cases, service_health_cache, license_snapshots, api_keys, webhook_deliveries, ad_agents, ad_agent_commands, ad_agent_templates.

---

## Phase 2: Core Platform Architecture ✅ COMPLETE

**Goal**: Replace single-process limitations. Unlock task scheduling, real-time updates, RBAC, and API access.

**Prerequisites**: Phase 1 complete.

**Status**: All 5 tasks completed. Additionally, an AD Agent system was implemented (beyond original scope).

---

### 2.1 Task Queue (Celery + Redis) ✅

**Problem**: Jobs run via `threading.Thread` in `helpers.py` — no retry, no timeout, no persistence across restarts. Report schedules and alert rules exist in DB but nothing executes them.

**Implementation plan**:

1. **Add dependencies** to `requirements.txt`:
```
celery[redis]==5.4.0
redis==5.2.0
```

2. **Create `portal/celery_app.py`**:
```python
from celery import Celery

def make_celery(app=None):
    celery = Celery(
        "portal",
        broker=os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0"),
        backend=os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/0"),
    )
    celery.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        task_track_started=True,
        worker_hijack_root_logger=False,
    )
    if app:
        celery.conf.update(app.config)
        TaskBase = celery.Task
        class ContextTask(TaskBase):
            def __call__(self, *args, **kwargs):
                with app.app_context():
                    return TaskBase.__call__(self, *args, **kwargs)
        celery.Task = ContextTask
    return celery
```

3. **Create `portal/tasks.py`** — migrate `_ps_worker` from `helpers.py`:
```python
from celery_app import make_celery
celery = make_celery()

@celery.task(bind=True, max_retries=2, soft_time_limit=3600, time_limit=3660)
def run_ps_job(self, job_id, cmd, env=None):
    """Run a PowerShell script as a Celery task."""
    # Same logic as helpers.py _ps_worker(), but with:
    # - Celery task state updates (self.update_state)
    # - Redis pub/sub for real-time output streaming
    # - Automatic retry on transient failures
    # - Timeout enforcement via soft_time_limit
```

4. **Update `helpers.py`** — `start_job()` dispatches to Celery when available, falls back to threading:
```python
def start_job(label, job_type, cmd, tenant_id=None, env=None):
    job_id = create_job(label, job_type, tenant_id=tenant_id, operator=current_user())
    try:
        from tasks import run_ps_job
        run_ps_job.delay(job_id, cmd, env)
    except Exception:
        # Fallback to thread-based execution
        t = threading.Thread(target=_ps_worker, args=(job_id, cmd, env), daemon=True)
        t.start()
    return job_id
```

5. **Update `cancel_job()`** in `helpers.py` — use `celery.control.revoke(job_id, terminate=True)` when Celery is active.

6. **Add Celery Beat periodic tasks** for:
   - Report schedule executor: check `report_schedules` table, launch matching audits
   - Alert rule evaluator: check `alert_rules` against recent data, create `alert_history` entries

7. **Wire into `app.py`**: Initialize Celery in `create_app()`, configure Redis session store.

8. **Update `.env.example`** with Redis/Celery config vars.

**Architecture**:
```
Browser → Flask → Redis (broker) → Celery Worker (runs PS scripts)
                                 → Celery Beat (schedules, alerts)
```

**Key concern**: `_running_processes` dict in `helpers.py:123` tracks subprocesses for cancellation. With Celery, use `task.revoke(terminate=True)` instead.

**Files to create**: `portal/celery_app.py`, `portal/tasks.py`
**Files to modify**: `portal/helpers.py`, `portal/app.py`, `requirements.txt`, `.env.example`

**Acceptance criteria**: Jobs run via Celery worker. `start_job()` falls back to threading if Redis unavailable. Report schedules execute on time.

**Implementation notes**: `portal/celery_app.py` creates Celery with Flask context. `portal/tasks.py` implements `run_ps_job`, `check_report_schedules`, `evaluate_alert_rules`, `deliver_webhook_task`. Beat schedule runs report checks hourly and alert evaluation every 5 minutes. `helpers.py` `start_job()` dispatches to Celery with threading fallback. Dependencies: `celery[redis]==5.4.0`, `redis==5.2.0`.

---

### 2.2 Real-Time Updates (SSE) ✅

**Problem**: Frontend polls `/jobs/<id>/status` every 60 seconds — no instant updates.

**Recommendation**: Use Server-Sent Events (SSE) — simpler than WebSocket, works through proxies, no extra dependency.

**Implementation**:

1. **Add SSE endpoint** in `portal/routes/jobs.py`:
```python
@bp.route("/jobs/<job_id>/stream")
@login_required
def job_stream(job_id):
    def generate():
        # Subscribe to Redis pub/sub channel f"job:{job_id}"
        # Yield SSE events: data: {"line": "...", "status": "running"}
        pass
    return current_app.response_class(generate(), mimetype='text/event-stream')
```

2. **Publish from Celery task** in `tasks.py`: Each output line published to Redis channel.

3. **Update frontend** JS in job-related templates: Use `EventSource` API instead of `setInterval` polling.

4. **Add dashboard auto-refresh**: SSE channel for dashboard stat updates.

**Files to modify**: `portal/routes/jobs.py`, `portal/tasks.py`, `portal/templates/jobs.html`, `portal/templates/dashboard.html`

**Implementation notes**: Two SSE endpoints in `routes/jobs.py`: `/jobs/<id>/stream` (real-time job output) and `/events/dashboard` (running job count). Uses Redis pub/sub when available, falls back to DB polling every 2 seconds. Proper SSE headers with keepalive comments every 15 seconds. Frontend uses `EventSource` API. Test coverage in `test_sse.py`.

---

### 2.3 Role-Based Access Control (RBAC) ✅

**Problem**: Binary auth — `is_admin` flag only. No granular permissions.

**Role matrix**:

| Role | View Tenants | Run Audits | Run Ops | Manage Users | Manage Settings |
|------|:---:|:---:|:---:|:---:|:---:|
| **super_admin** | All | All | All (Tier 1-3) | Yes | Yes |
| **admin** | Assigned groups | All | Tier 1-2 | No | No |
| **operator** | Assigned groups | All | Tier 1 only | No | No |
| **viewer** | Assigned groups | No | No | No | No |

**Implementation**:

1. **Extend users table** — add migration:
```sql
ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'operator';
ALTER TABLE users ADD COLUMN allowed_tenant_groups TEXT DEFAULT '[]';
```

2. **Create `portal/permissions.py`**:
```python
ROLE_HIERARCHY = {'super_admin': 4, 'admin': 3, 'operator': 2, 'viewer': 1}

def has_role(user, min_role):
    return ROLE_HIERARCHY.get(user.get('role', 'viewer'), 0) >= ROLE_HIERARCHY[min_role]

def can_access_tenant(user, tenant_id):
    if user.get('role') == 'super_admin':
        return True
    # Check if tenant belongs to user's allowed groups
    ...

def can_run_operation(user, tier):
    role = user.get('role', 'viewer')
    if role == 'super_admin': return True
    if role == 'admin': return tier <= 2
    if role == 'operator': return tier <= 1
    return False
```

3. **Add RBAC decorators** to `helpers.py`:
```python
def role_required(min_role):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = get_user(session["username"])
            if not has_role(user, min_role):
                abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorator
```

4. **Update `@admin_required`** to use `role_required('admin')`.

5. **Filter tenant listings** by user's `allowed_tenant_groups`.

6. **Add role management UI** — extend `/users` page in `routes/auth.py`.

**Files to create**: `portal/permissions.py`
**Files to modify**: `portal/helpers.py`, `portal/routes/auth.py`, `portal/database.py`, `portal/templates/users.html`

**Implementation notes**: `portal/permissions.py` implements 4-tier role hierarchy (super_admin=4, admin=3, operator=2, viewer=1). Functions: `has_role()`, `can_access_tenant()`, `can_run_operation()` (tier-based), `can_run_audit()`, `filter_tenants_for_user()`. Users table extended with `role TEXT DEFAULT 'operator'` and `allowed_tenant_groups TEXT DEFAULT '[]'`. `@role_required` decorator in helpers.py. Test coverage in `test_rbac.py`.

---

### 2.4 REST API Layer ✅

**Problem**: No programmatic access — no API keys, no bearer tokens.

**Implementation**:

1. **Add `api_keys` table**:
```sql
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    scopes TEXT DEFAULT '[]',
    expires_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    last_used_at TEXT
);
```

2. **Create `portal/routes/api_v1.py`** blueprint:
```python
bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")

@bp.before_request
def check_api_key():
    key = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not validate_api_key(key):
        return jsonify({"ok": False, "error": "Invalid API key"}), 401
```

3. **Core API endpoints**:
   - `GET /api/v1/tenants` — list tenants
   - `GET /api/v1/tenants/<id>` — tenant detail
   - `POST /api/v1/audits/run` — launch audit
   - `GET /api/v1/jobs/<id>` — job status
   - `GET /api/v1/reports` — list reports
   - `GET /api/v1/bpa/<tenant_id>` — BPA results

4. **API key management UI** in settings page.

5. **Exempt API routes from CSRF** (they use Bearer auth instead):
```python
csrf.exempt(api_v1_bp)
```

**Files to create**: `portal/routes/api_v1.py`
**Files to modify**: `portal/app.py`, `portal/database.py`, `portal/templates/settings.html`

**Implementation notes**: `portal/routes/api_v1.py` blueprint with `/api/v1` prefix. Bearer token auth via `Authorization` header. `api_keys` table with `key_prefix`, `key_hash`, `role`, `allowed_tenant_groups`. CSRF exempted. RBAC integration via `filter_tenants_for_user()`. Endpoints: `/api/v1/health`, `/api/v1/tenants`, `/api/v1/jobs/*`. Test coverage in `test_api_v1.py`.

---

### 2.5 Webhook System ✅

**Implementation**:

1. **Add `webhooks` table**:
```sql
CREATE TABLE IF NOT EXISTS webhooks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    secret TEXT NOT NULL,
    events TEXT DEFAULT '[]',
    enabled INTEGER DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
```

2. **Delivery via Celery task** in `tasks.py`:
```python
@celery.task(bind=True, max_retries=5)
def deliver_webhook(self, webhook_id, event_type, payload):
    # HMAC-sign payload with webhook secret
    # POST to webhook URL
    # Retry with exponential backoff on failure
```

3. **Event types**: `job.completed`, `job.failed`, `alert.triggered`, `bpa.drift_detected`, `report.generated`

4. **Webhook management UI** — new page linked from settings.

**Files to modify**: `portal/database.py`, `portal/tasks.py`
**Files to create**: `portal/routes/webhooks.py` (or add to settings), `portal/templates/webhooks.html`

**Implementation notes**: `portal/routes/webhooks.py` blueprint with CRUD routes. `webhooks` and `webhook_deliveries` tables in database. Events: `job.completed`, `job.failed`, `alert.triggered`, `bpa.drift_detected`, `report.generated`. HMAC-SHA256 signed payloads. Celery task `deliver_webhook_task()` with 5 retries and 30s backoff. Test coverage in `test_webhooks.py`.

### 2.6 AD Agent System (Bonus — beyond original roadmap) ✅

**Implementation**: Full on-premises Active Directory agent system for tenants with hybrid identity. Includes:
- Hybrid identity detection via Graph API (`Get-HybridStatus.ps1`)
- HMAC-SHA256 authenticated agent API (`portal/agent_auth.py`)
- Agent enrollment with one-time tokens (24h TTL)
- Command queue (lookup_user, update_user, add/remove_group_member, apply_template, ping)
- Per-tenant downloadable agent package (ZIP with `AD-Agent.ps1`, `Install-ADAgent.ps1`, `enrollment.json`)
- User onboarding templates
- 5 new database tables: `tenant_ad_config`, `ad_agents`, `ad_agent_enrollment_tokens`, `ad_agent_commands`, `ad_agent_templates`

**Files created**: `portal/agent_auth.py`, `portal/routes/ad_agent.py`, `portal/templates/ad_agent.html`, `scripts/AD-Agent.ps1`, `scripts/Install-ADAgent.ps1`, `scripts/Get-HybridStatus.ps1`, `tests/test_ad_agent.py`

---

## Phase 3: Reporting & Client-Facing Output ✅ COMPLETE

**Goal**: Transform raw audit data into polished, client-ready deliverables.

**Prerequisites**: Phase 2 (task queue for scheduled delivery).

**Status**: All 4 tasks completed. 50 new tests (658 total).

---

### 3.1 PDF Report Generation ✅

**Implementation**:

1. **Add dependency**: `weasyprint` to `requirements.txt` (HTML→PDF using same Jinja2 templates).

2. **Create `portal/report_engine.py`**:
```python
from weasyprint import HTML

def generate_pdf(template_name, context, output_path):
    """Render a Jinja2 template to PDF."""
    html_string = render_template(template_name, **context)
    HTML(string=html_string).write_pdf(output_path)
```

3. **Create PDF templates** in `portal/templates/reports/`:
   - `pdf_base.html` — branded layout with header, footer, page numbers
   - `pdf_executive_summary.html` — 1-page risk overview
   - `pdf_security_posture.html` — 365Inspect findings by severity
   - `pdf_bpa_compliance.html` — pass/fail table with guidance
   - `pdf_identity_risk.html` — MFA gaps, risky users
   - `pdf_license_optimization.html` — unused licenses, cost savings
   - `pdf_breach_investigation.html` — timeline, IOCs, affected accounts

4. **Add PDF download endpoints** in `routes/reports.py`:
```python
@bp.route("/reports/<tenant_id>/pdf/<report_type>")
@login_required
def download_pdf(tenant_id, report_type):
    ...
```

5. **Branding configuration** — add `branding` section to `config/portal.json`:
```json
{
    "branding": {
        "company_name": "Your MSP",
        "logo_path": "",
        "accent_color": "#0d6efd"
    }
}
```

**Files to create**: `portal/report_engine.py`, `portal/templates/reports/pdf_*.html`
**Files to modify**: `portal/routes/reports.py`, `requirements.txt`

**Implementation notes**: `portal/report_engine.py` created with 6 report types: `executive_summary`, `security_posture`, `bpa_compliance`, `identity_risk`, `license_optimization`, `breach_investigation`. Each has a dedicated Jinja2 template in `portal/templates/reports/pdf_*.html` extending `pdf_base.html`. `get_report_data()` fetches data from DB per report type. `generate_pdf()` returns `(pdf_bytes, filename)`. `WEASYPRINT_AVAILABLE` flag for graceful degradation when system libraries unavailable. Per-tenant branding stored in `tenant_branding` table with CRUD functions. Added `weasyprint==63.1` to `requirements.txt`. PDF download endpoint at `/reports/<tenant_id>/pdf/<report_type>`. PDF types listing at `/reports/pdf-types`.

---

### 3.2 Scheduled Report Delivery (Email) ✅

**Implementation**:

1. **SMTP configuration** — add to settings page and `portal.json`:
```json
{
    "smtp": {
        "host": "",
        "port": 587,
        "username": "",
        "password_enc": "",
        "sender": "",
        "use_tls": true
    }
}
```

2. **Email delivery Celery task** in `tasks.py`:
```python
@celery.task
def send_report_email(recipients, subject, body, attachments):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    ...
```

3. **Wire Celery Beat** to execute `report_schedules` table entries:
```python
@celery.task
def check_report_schedules():
    """Run every hour. Check for due schedules and launch audits."""
    with get_db() as conn:
        due = conn.execute(
            "SELECT * FROM report_schedules WHERE enabled=1 AND next_run <= datetime('now')"
        ).fetchall()
    for schedule in due:
        # Launch audit job, generate PDF, send email
        ...
```

4. **Recipient management** — add `recipients TEXT DEFAULT '[]'` column to `report_schedules` table.

**Files to modify**: `portal/tasks.py`, `portal/database.py`, `portal/routes/reports.py`, `portal/templates/settings.html`

**Implementation notes**: SMTP configuration stored in `portal.json` (host, port, username, encrypted password, sender, TLS). Routes: `GET/POST /reports/smtp-config` for configuration, `POST /reports/smtp-test` for testing. Celery task `send_report_email()` in `tasks.py` handles MIME multipart emails with PDF attachments, 3 retries with 60s delay. `check_report_schedules()` updated to call `send_report_email.delay()` when `email_on_completion` is set. Schema v9 migration adds `recipients TEXT DEFAULT '[]'` and `email_on_completion INTEGER DEFAULT 0` columns to `report_schedules` table.

---

### 3.3 Report Comparison ✅

**Implementation**: Add endpoint to diff two audit runs:

```python
@bp.route("/reports/compare")
@login_required
def compare_reports():
    """Compare two audit runs and highlight changes."""
    file_a = request.args.get("a")
    file_b = request.args.get("b")
    # Parse both CSVs, compute diff, render comparison template
```

**Files to modify**: `portal/routes/reports.py`
**Files to create**: `portal/templates/report_comparison.html`

**Implementation notes**: Report comparison endpoint at `GET /reports/compare`. Supports CSV and JSON files. CSV comparison uses set operations on normalized row tuples for added/removed detection, with first-column keying for change detection. JSON comparison shows structural diffs. Path traversal protection validates file paths against allowed base directories (returns 403 on traversal attempts). Template `report_comparison.html` shows file selection dropdowns and added/removed/changed/unchanged summary cards with full row tables.

---

### 3.4 Client-Facing Dashboard (Phase 3b) ✅

**Implementation**:

1. **Client user model** — add `role='client'` to RBAC system, scoped to one tenant.
2. **Read-only dashboard** — secure score trend, BPA status, recent reports, action items.
3. **Self-service downloads** — clients download their own reports without operator involvement.
4. **Per-tenant branding** — custom logo/colors in client view.

**Files to modify**: `portal/permissions.py`, `portal/routes/auth.py`
**Files to create**: `portal/templates/client_dashboard.html`

**Implementation notes**: `client` role added at level 0 (below viewer) in `permissions.py` ROLE_HIERARCHY. `is_client_user()` and `get_client_tenant_id()` helper functions added. Client dashboard at `GET /client-dashboard` shows read-only view: BPA summary, secure score, recent reports with self-service PDF downloads. Template `client_dashboard.html` with metrics cards, BPA compliance table, and report listing. Per-tenant branding via `tenant_branding` table: `GET/POST /reports/branding/<tenant_id>` for per-tenant, `POST /reports/branding/default` for global defaults. Branding data (company_name, logo_path, accent_color) applied to PDF reports and client dashboard. Schema v9 creates `tenant_branding` table with CRUD: `get_tenant_branding()`, `upsert_tenant_branding()`, `get_default_branding()`. Test coverage: 50 tests in `test_reporting.py` covering all Phase 3 features.

---

## Phase 4: MSP Operations Expansion & Automation

**Goal**: Automated remediation, approval workflows, drift detection, tenant onboarding wizard.

**Prerequisites**: Phase 2 (RBAC for approvals, task queue for remediation).

---

### 4.1 Operations Catalogue Expansion

**Current**: 50+ operations across User, Exchange, Teams, SharePoint categories with 3 tiers.

**New operations to add** (each needs: entry in `OPERATIONS` dict in `routes/operations.py`, PowerShell script in `scripts/Invoke-*.ps1`, parameter schema for form generation):

| Category | New Operations |
|----------|---------------|
| **User** | Bulk password reset, bulk license assignment, user provisioning from template, guest user lifecycle |
| **Exchange** | Shared mailbox CRUD, distribution list management, retention policies, journal rules |
| **Teams** | Team provisioning from template, channel management, guest access policies |
| **SharePoint** | Site provisioning from template, hub site management, sensitivity labels, storage quotas |
| **Entra** | Dynamic group management, administrative units, app registration lifecycle |
| **Intune** | App deployment, compliance policy CRUD, configuration profiles, Autopilot profile assignment |

**Files to modify**: `portal/routes/operations.py`
**Files to create**: Additional `scripts/Invoke-*.ps1` scripts as needed

---

### 4.2 Automated Remediation

**Implementation**:

1. **Create `scripts/Invoke-Remediation.ps1`** — takes check_key + tenant params, applies fix:
```powershell
param(
    [Parameter(Mandatory)][string]$CheckKey,
    [Parameter(Mandatory)][string]$TenantDomain,
    [Parameter(Mandatory)][string]$ClientId,
    [string]$CertificatePath,
    [switch]$DryRun
)
# Switch on CheckKey to apply specific remediation
```

2. **Add `remediation_script` field** to BPA check definitions in `routes/standards.py`.

3. **Add "Fix" button** on BPA results page that launches remediation job.

4. **Dry-run mode**: Preview changes before applying. Return what would change without making changes.

5. **Remediation log table**:
```sql
CREATE TABLE IF NOT EXISTS remediation_log (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    check_key TEXT NOT NULL,
    action TEXT NOT NULL,
    before_state TEXT DEFAULT '',
    after_state TEXT DEFAULT '',
    dry_run INTEGER DEFAULT 0,
    operator TEXT NOT NULL,
    executed_at TEXT DEFAULT (datetime('now'))
);
```

**Example remediations** (safe, low-risk):
- Enable unified audit log
- Disable legacy authentication
- Block external email forwarding
- Enable mailbox auditing
- Set password expiry policy

**Files to create**: `scripts/Invoke-Remediation.ps1`
**Files to modify**: `portal/routes/standards.py`, `portal/database.py`, `portal/templates/bpa.html`

---

### 4.3 Drift Detection

**Implementation**:

1. **Celery Beat task** runs BPA against tenants with assigned standards on configurable schedule.

2. **Compare results** against previous scan — flag changes:
```python
@celery.task
def check_bpa_drift():
    """Compare latest BPA results against previous. Create alerts on drift."""
    for tenant in tenants_with_standards:
        current = get_latest_bpa_results(tenant.id)
        previous = get_previous_bpa_results(tenant.id)
        drifted = [c for c in current if c.status != previous.get(c.check_key, {}).get('status')]
        for check in drifted:
            create_alert(...)
```

3. **Drift dashboard** — visual overview of compliance across tenants.

**Files to modify**: `portal/tasks.py`, `portal/routes/standards.py`
**Files to create**: `portal/templates/drift_dashboard.html`

---

### 4.4 Approval Workflows

**Implementation**:

1. **Add `change_requests` table**:
```sql
CREATE TABLE IF NOT EXISTS change_requests (
    id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    params TEXT DEFAULT '{}',
    tenant_id TEXT,
    requestor TEXT NOT NULL,
    approver TEXT,
    status TEXT DEFAULT 'pending',
    reason TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    resolved_at TEXT,
    expires_at TEXT
);
```

2. **Gate Tier 2+ operations** in `routes/operations.py`:
```python
if tier >= 2 and user_role != 'super_admin':
    # Create change request instead of executing
    create_change_request(operation, params, tenant_id, requestor=current_user())
    return jsonify({"ok": True, "pending_approval": True})
```

3. **Approval queue UI** — dashboard showing pending requests.

4. **Auto-execute on approval** via Celery task.

5. **Auto-expiration** — unapproved requests expire after 72 hours.

**Files to modify**: `portal/routes/operations.py`, `portal/database.py`
**Files to create**: `portal/routes/workflows.py`, `portal/templates/approval_queue.html`

---

### 4.5 Tenant Onboarding Wizard

**Implementation**: Multi-step guided flow:

1. Add tenant (name, domain, tenant_id)
2. Generate certificate (cert + PFX)
3. Create Entra app (upload cert, assign permissions)
4. Grant admin consent (instructions + link)
5. Run initial BPA + security scan + secure score fetch

Also support **bulk onboarding**: CSV import with columns `name,domain,tenant_id`.

**Files to modify**: `portal/routes/tenants.py`
**Files to create**: `portal/templates/tenant_onboarding_wizard.html`

---

## Phase 5: Integration Layer

**Goal**: Connect to the MSP ecosystem — PSA ticketing, notifications.

**Prerequisites**: Phase 2 (API layer, webhooks), Phase 3 (reports).

---

### 5.1 PSA Integration

**Implementation**:

1. **Create `portal/integrations/` directory** with:
   - `base.py` — abstract base class for PSA connectors
   - `connectwise.py` — ConnectWise Manage REST API client
   - `autotask.py` — Autotask/Datto REST API client
   - `halopsa.py` — HaloPSA REST API client

2. **Integration interface**:
```python
class PSAConnector:
    def create_ticket(self, alert, tenant): ...
    def sync_clients(self): ...
    def get_client_by_domain(self, domain): ...
```

3. **Ticket creation rules**: Map alert severity/type → ticket priority/board/status.

4. **Client sync**: Bi-directional sync between PSA client list and portal tenants.

5. **Integration config UI** in settings.

**Files to create**: `portal/integrations/__init__.py`, `portal/integrations/base.py`, `portal/integrations/connectwise.py`, `portal/integrations/autotask.py`, `portal/integrations/halopsa.py`
**Files to modify**: `portal/database.py`, `portal/templates/settings.html`

---

### 5.2 Notification Channels

**Implementation**:

1. **Create `portal/notifications.py`**:
```python
def send_notification(channel_type, config, message):
    if channel_type == "teams":
        requests.post(config["webhook_url"], json={"text": message})
    elif channel_type == "slack":
        requests.post(config["webhook_url"], json={"text": message})
    elif channel_type == "email":
        send_email(config["recipients"], "Alert", message)
```

2. **Per-user preferences** — `notification_preferences TEXT DEFAULT '{}'` column on users table.

3. **Integration with alerts**: When alert is triggered, deliver via user's preferred channel.

**Files to create**: `portal/notifications.py`
**Files to modify**: `portal/database.py`, `portal/templates/settings.html`

---

### 5.3 External Data Consumers

| Task | Detail |
|------|--------|
| Power BI connector | REST API endpoints optimized for Power BI data refresh |
| Scheduled data exports | CSV/JSON feeds to file shares or cloud storage |
| Grafana metrics | Prometheus-format metrics endpoint for operational dashboards |

---

## Phase 6: Compliance & Governance

**Goal**: Regulatory compliance framework mapping, evidence collection, governance reporting.

**Prerequisites**: Phase 4 (remediation, drift detection).

---

### 6.1 Compliance Frameworks

**Implementation**:

1. **Framework data model** — map BPA checks → framework controls (many-to-many):
```sql
CREATE TABLE IF NOT EXISTS compliance_frameworks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT DEFAULT '',
    controls TEXT DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS control_check_mapping (
    framework_id TEXT NOT NULL,
    control_id TEXT NOT NULL,
    check_key TEXT NOT NULL,
    PRIMARY KEY (framework_id, control_id, check_key)
);
```

2. **Built-in frameworks** as JSON files in `config/frameworks/`:
   - `cis-m365.json` — CIS Microsoft 365 Benchmark
   - `essential-eight.json` — Australian ASD Essential Eight
   - `cyber-essentials.json` — UK Cyber Essentials
   - Custom framework support (import/export)

3. **Compliance score**: `(passing controls / total controls) × 100` per framework per tenant.

4. **Compliance dashboard** — per-tenant scores with drill-down to individual controls.

5. **Evidence collection** — auto-attach BPA results, audit logs, config snapshots.

6. **Compliance PDF report** — client-facing report per framework (uses Phase 3 PDF engine).

**Files to create**: `portal/routes/compliance.py`, `portal/templates/compliance_*.html`, `config/frameworks/*.json`
**Files to modify**: `portal/database.py`, `portal/app.py`

---

### 6.2 Policy Governance

| Task | Detail |
|------|--------|
| Desired state config | Define target tenant configuration, detect deviations |
| Policy exceptions | Document and track approved exceptions to compliance requirements |
| Policy inheritance | Tenant groups inherit policies; per-tenant overrides allowed |
| Exception expiry | Exceptions auto-expire, trigger re-review alerts |

---

## Phase 7: Scale & Production Hardening

**Goal**: Containerization, database options, observability.

**Prerequisites**: Phase 2 (Redis, Celery).

---

### 7.1 Docker Support

**Implementation**:

1. **Create `Dockerfile`** (multi-stage):
```dockerfile
# Stage 1: Python deps
FROM python:3.11-slim AS python-deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Stage 2: PowerShell + modules
FROM mcr.microsoft.com/powershell:7.4-ubuntu-22.04 AS pwsh
COPY Build-Toolkit.ps1 .
RUN pwsh -File Build-Toolkit.ps1

# Stage 3: Runtime
FROM python:3.11-slim
COPY --from=python-deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=pwsh /opt/microsoft/powershell/7 /opt/microsoft/powershell/7
COPY --from=pwsh /root/.local/share/powershell/Modules /app/modules
COPY . /app
WORKDIR /app
RUN useradd -r portal && chown -R portal:portal /app
USER portal
HEALTHCHECK CMD curl -f http://localhost:5000/health || exit 1
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "--chdir", "portal", "app:create_app()"]
```

2. **Create `docker-compose.yml`**:
```yaml
services:
  app:
    build: .
    ports: ["5000:5000"]
    volumes: [./tenants:/app/tenants, ./data:/app/data, ./config:/app/config]
    depends_on: [redis]
    environment:
      - CELERY_BROKER_URL=redis://redis:6379/0
  worker:
    build: .
    command: celery -A portal.celery_app worker -l info
    volumes: [./tenants:/app/tenants, ./data:/app/data]
    depends_on: [redis]
  beat:
    build: .
    command: celery -A portal.celery_app beat -l info
    depends_on: [redis]
  redis:
    image: redis:7-alpine
    volumes: [redis-data:/data]
volumes:
  redis-data:
```

3. **Create `.dockerignore`**: Exclude `data/`, `tenants/`, `logs/`, `.git`, `*.pfx`, `*.cer`.

**Files to create**: `Dockerfile`, `docker-compose.yml`, `.dockerignore`

**Acceptance criteria**: `docker compose up` starts all services. `/health` returns 200. Jobs run via Celery worker.

---

### 7.2 PostgreSQL Support

**Implementation**: Add a thin abstraction layer rather than full SQLAlchemy refactor.

1. **Add dependency**: `psycopg2-binary` to `requirements.txt`.

2. **Make database backend configurable** via `DATABASE_URL` env var:
```python
# database.py
import os
DB_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = DB_URL.startswith("postgresql://")
```

3. **Abstract connection helper** — update `get_db()` to handle both backends.

4. **Handle SQL dialect differences**: `?` → `%s` for parameters. Create helper function.

5. **Keep SQLite as default** for single-instance deployments.

**Files to modify**: `portal/database.py`, `requirements.txt`, `.env.example`

---

### 7.3 Observability

**Implementation**:

1. **Structured JSON logging** — replace text format in `app.py`:
```python
class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", ""),
        })
```

2. **Prometheus metrics endpoint** at `/metrics` — request latency, job duration, error rates, queue depth.

3. **Expanded health check** — verify Redis, Celery worker, PowerShell availability in `/health`.

**Files to modify**: `portal/app.py`

---

### 7.4 Authentication Enhancements

1. **TOTP MFA**: Add `mfa_secret TEXT DEFAULT ''` to users table. Use `pyotp` library.

2. **OIDC/SAML**: Portal login via Entra ID using `authlib` library.

3. **Session management**: Active session list, remote revocation, concurrent session limits.

4. **Password policies**: Configurable minimum length, complexity, expiry.

**Files to modify**: `portal/routes/auth.py`, `portal/database.py`, `requirements.txt`

---

## PowerShell Improvements (Cross-Phase)

### Immediate (Phase 1)

| Task | File | Detail |
|------|------|--------|
| Graph API throttling | `scripts/Shared.psm1` `Invoke-GraphRequest` | Add 429/Retry-After detection with exponential backoff |
| Module version pinning | `Build-Toolkit.ps1` | Pin `RequiredVersion` to prevent breaking changes |
| Inspector error isolation | `inspectors/365Inspect.ps1` | Wrap each inspector in try/catch so one failure doesn't abort the scan |
| Token refresh | `scripts/Shared.psm1` `Get-AuditGraphToken` | Add token cache with refresh for long operations |

### Short-Term (Phase 3-4)

| Task | Detail |
|------|--------|
| Pester test framework | Add `tests/*.Tests.ps1` for `Shared.psm1` functions. Mock `Connect-PnPOnline`, `Invoke-RestMethod` |
| Structured progress output | Scripts emit JSON lines: `{"progress": 50, "message": "..."}` for SSE real-time UI updates |
| Inspector result caching | Cache Graph API responses within a scan session to avoid duplicate calls |

### Medium-Term (Phase 4-5)

| Task | Detail |
|------|--------|
| Remediation scripts | `scripts/Invoke-Remediation.ps1` for BPA auto-fix |
| Deprecated module migration | Replace `Microsoft.Online.SharePoint.PowerShell` with Graph equivalents |
| Parallel execution | `ForEach-Object -Parallel` for multi-site operations |

---

## Phase Dependencies

```
Phase 1 (Foundation) ✅
    │
    v
Phase 2 (Core Platform) ✅ ─────────────────────┐
    │                                             │
    ├──> Phase 3 (Reporting) ✅                   │
    │        │                                    │
    ├──> Phase 4 (Operations & Automation) ← READY│
    │        │                                    │
    │        └──> Phase 6 (Compliance)           │
    │                                             │
    ├──> Phase 5 (Integrations) ← READY          │
    │                                             │
    └──> Phase 7 (Scale) ← READY ────────────────┘
```

- **Phases 4, 5, 7 can now start** — Phase 2 and 3 prerequisites are met
- **Phase 5** is now unblocked (depends on Phase 3 for report delivery)
- **Phase 6** depends on Phase 4 (remediation and drift detection)
- **Phase 7** can start immediately (Celery + Redis already integrated)

---

## Priority Matrix

| Phase | Business Impact | Technical Risk | Effort | Status |
|-------|:---:|:---:|:---:|:---:|
| **1. Foundation** | High | Low | Medium | ✅ Complete |
| **2. Core Platform** | Critical | Medium | Large | ✅ Complete |
| **3. Reporting** | High (client-facing) | Low | Medium | ✅ Complete |
| **4. Operations** | High (MSP efficiency) | Medium | Large | Ready to start |
| **5. Integrations** | Medium (ecosystem) | Medium | Large | Ready to start |
| **6. Compliance** | Medium (regulated verticals) | Low | Medium | After Phase 4 |
| **7. Scale** | Medium (growth) | High | Large | Ready to start |

---

## Quick Wins (Can Start Today)

~~All original quick wins have been completed:~~

1. ~~**Add GitHub Actions CI** — `.github/workflows/ci.yml` running tests on every push~~ ✅
2. ~~**Fix CSP unsafe-inline** — add nonces in `app.py` + templates~~ ✅
3. ~~**Add SRI hashes** — to CDN links in `base.html`~~ ✅
4. ~~**Add database indexes** — migration for key FK columns~~ ✅
5. ~~**Add `requirements-dev.txt`** — pytest, pytest-cov, ruff, bandit~~ ✅
6. **Graph throttling** — add 429 retry to `Invoke-GraphRequest` in `Shared.psm1` (still pending)
7. ~~**Write missing tests** — start with `test_audits.py` and `test_jobs_routes.py`~~ ✅
8. ~~**HSTS header** — one line in `app.py`~~ ✅

---

## Verification Checklist (After Each Phase)

1. **Tests pass**: `python -m pytest tests/ -v`
2. **Lint clean**: `ruff check portal/` (once CI is set up)
3. **Security scan**: `bandit -r portal/` — no new high/medium findings
4. **Manual smoke test**: Login → create tenant → run audit → view report → check job status
5. **PowerShell integration**: Cert generation and at least one audit type work end-to-end
6. **Docker (Phase 7+)**: `docker compose up` starts cleanly, all services healthy
