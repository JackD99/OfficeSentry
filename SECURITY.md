# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability in SPO-Audit-Toolkit, please report it responsibly. **Do not open a public issue.**

Instead, please contact the maintainers privately through GitHub's [security advisory feature](https://github.com/JackD99/SPO-Audit-Toolkit/security/advisories/new). Include:

1. A description of the vulnerability
2. Steps to reproduce
3. The potential impact
4. Any suggested fixes (optional)

We aim to acknowledge reports within 48 hours and provide an initial assessment within 7 days.

## Security Model

### Authentication

SPO-Audit-Toolkit uses **certificate-based app-only authentication** to connect to Microsoft 365 services. This means:

- No user passwords for M365 tenants are ever stored
- Authentication uses X.509 certificates (PFX + CER) registered with Entra ID
- PFX passwords are encrypted at rest using Fernet symmetric encryption
- The encryption key is derived from the Flask secret key via **PBKDF2** with 100,000 iterations
- The master key is stored in `config/portal.json`

### Portal Security Controls

| Control | Implementation |
|---|---|
| **CSRF Protection** | Flask-WTF CSRFProtect on all POST routes (exempted on API and Agent endpoints) |
| **Rate Limiting** | Login: 10/min, Initial setup: 5/min (Flask-Limiter, in-memory) |
| **Session Timeout** | Sessions expire after 8 hours |
| **Password Hashing** | Werkzeug bcrypt-based hashing |
| **Security Headers** | Nonce-based CSP, HSTS, X-Frame-Options: DENY, X-Content-Type-Options: nosniff, Referrer-Policy, Permissions-Policy |
| **SRI Hashes** | Subresource Integrity on all CDN resources (Bootstrap CSS/JS) |
| **Path Traversal Protection** | File download paths validated against allowed base directories |
| **Encrypted Secrets** | Fernet encryption with PBKDF2-derived keys (100k iterations) |
| **RBAC** | 5-tier role hierarchy (super_admin, admin, operator, viewer, client) with tenant group scoping |
| **API Authentication** | Bearer token with scoped API keys (SHA256 hashed, expiration enforced) |
| **Agent Authentication** | HMAC-SHA256 signed requests with 5-minute timestamp window and nonce deduplication |
| **Request Tracking** | X-Request-ID header on all responses |
| **Audit Trail** | All operator actions logged with tenant, timestamp, and details |

### Sensitive Files

The following files and directories contain sensitive data and are excluded from version control via `.gitignore`:

| Path | Contents |
|---|---|
| `config/portal.json` | Flask secret key (encryption master key), SMTP config |
| `data/portal.db` | Tenant metadata, encrypted PFX passwords, API key hashes, agent secrets, job history |
| `tenants/*/certs/` | X.509 certificates (PFX, CER) |
| `tenants/*/output/` | Audit output files (may contain tenant data) |
| `logs/` | Application logs |

### What Should Never Be Committed

- Certificates: `*.pfx`, `*.cer`, `*.pem`, `*.key`, `*.p12`
- Credentials, API keys, or tokens of any kind
- Database files (`*.db`)
- Audit output: `*.csv`, `*.xlsx`
- Configuration with secrets: `config/portal.json`
- Environment files: `.env`

## Deployment Considerations

### Development

The built-in Flask development server is suitable only for local development and testing. It should **not** be exposed to untrusted networks.

### Production

For production deployments:

1. Use a WSGI server (gunicorn) behind a reverse proxy (nginx, Caddy)
2. Enable HTTPS with a valid TLS certificate
3. Set `FLASK_ENV=production`
4. Back up `config/portal.json` securely — losing it invalidates all encrypted PFX passwords, SMTP passwords, and agent secrets
5. Restrict network access to trusted operators
6. Monitor `logs/portal.log` for suspicious activity
7. Use Redis for Celery task queue and Flask-Limiter in multi-process deployments
8. Configure SSE proxy support (disable response buffering for `/jobs/*/stream`)

### Entra ID App Permissions

The Entra app created for each tenant will have API permissions for SharePoint, Exchange, and Microsoft Graph. Follow the principle of least privilege:

- Only grant the permissions required for the audits you intend to run
- Review and audit app permissions regularly
- Remove unused Entra apps via the portal's "Remove App" function
- Monitor the Azure AD audit logs for any unauthorized use of the app credentials

### AD Agent Security

On-premises AD Agents communicate via HMAC-SHA256 signed HTTPS requests:

- Agent-initiated polling only (outbound from on-premises, no inbound connections required)
- Each agent is cryptographically bound to a single tenant
- Enrollment uses one-time tokens with 24-hour TTL
- Agent secrets are encrypted with Fernet at rest in the portal and DPAPI on Windows
- All requests include timestamp and nonce for replay protection

## Known Limitations

- SQLite is not suitable for high-concurrency production deployments (WAL mode helps but has limits)
- Login rate limiting uses in-memory storage and resets on application restart
- No multi-factor authentication for portal login (relies on network access controls)
- No SSO/SAML integration
- Session tokens are cookie-based; ensure HTTPS in production to prevent interception
