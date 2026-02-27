"""
portal/routes/ad_agent.py — AD Agent management blueprint.

Operator-facing routes (login/admin required):
  - Hybrid identity detection
  - Agent enrollment, download, revoke, rotate
  - Command dispatch and monitoring
  - User template CRUD

Agent-facing API routes (HMAC auth, CSRF-exempt):
  - POST /api/agent/enroll     — exchange enrollment token for credentials
  - POST /api/agent/heartbeat  — agent check-in
  - POST /api/agent/poll       — fetch pending commands
  - POST /api/agent/result     — report command results
"""

import hashlib
import io
import json
import logging
import os
import secrets
import zipfile
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint, render_template, request, jsonify, abort,
    current_app, send_file, g,
)

from database import (
    get_tenant, log_action, encrypt_value, decrypt_value,
    get_tenant_ad_config, upsert_tenant_ad_config,
    create_ad_agent, get_ad_agent, get_ad_agent_for_tenant,
    list_ad_agents, update_ad_agent, revoke_ad_agent,
    create_enrollment_token, validate_enrollment_token,
    consume_enrollment_token, list_enrollment_tokens,
    create_agent_command, get_agent_command, poll_pending_commands,
    complete_agent_command, list_agent_commands, VALID_COMMAND_TYPES,
    create_agent_template, get_agent_template, list_agent_templates,
    update_agent_template, delete_agent_template,
)
from helpers import (
    login_required, admin_required, current_user,
    build_ps_env, start_job, sanitize_text, validate_tenant_guid,
    SCRIPTS_DIR, tenant_dir,
)
from agent_auth import agent_auth_required

log = logging.getLogger("portal")

bp = Blueprint("ad_agent", __name__)

# Separate blueprint for agent API (will be CSRF-exempted)
agent_api_bp = Blueprint("agent_api", __name__)


# ===================================================================
# Operator-facing routes
# ===================================================================

@bp.route("/ad-agent")
@login_required
def ad_agent_overview():
    """Main AD agent management page — lists all tenants with agent status."""
    from database import list_tenants
    tenants = list_tenants()
    agents = list_ad_agents()
    # Build lookup: tenant_id -> agent
    agent_map = {}
    for a in agents:
        if a["tenant_id"] not in agent_map or a["status"] == "active":
            agent_map[a["tenant_id"]] = a
    return render_template("ad_agent.html", tenants=tenants, agent_map=agent_map)


# --- Hybrid detection ---

@bp.route("/tenants/<tenant_id>/ad-agent/check-hybrid", methods=["POST"])
@admin_required
def check_hybrid(tenant_id):
    """Trigger hybrid identity detection via Graph API."""
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    if not t.get("client_id"):
        return jsonify({"ok": False, "error": "Tenant not configured (no Client ID)"}), 400

    # Start a background job to run Get-HybridStatus.ps1
    from helpers import get_tenant_pfx_password, tenant_pfx_path
    pfx_path = tenant_pfx_path(tenant_id)
    if not pfx_path.exists():
        return jsonify({"ok": False, "error": "Certificate not found"}), 400

    pfx_password = get_tenant_pfx_password(t)
    env = build_ps_env(pfx_password)
    script = SCRIPTS_DIR / "Get-HybridStatus.ps1"

    cmd = [
        "pwsh", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        str(script),
        "-TenantId", t["tenant_id"] or t["domain"],
        "-ClientId", t["client_id"],
        "-CertPath", str(pfx_path),
    ]

    job_id = start_job(
        f"Hybrid detection — {t['name']}",
        "hybrid_check", cmd, tenant_id=tenant_id, env=env,
    )
    log_action(current_user(), "hybrid_check_started",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/tenants/<tenant_id>/ad-agent/hybrid-status")
@login_required
def hybrid_status(tenant_id):
    """Get current hybrid detection status for a tenant."""
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    config = get_tenant_ad_config(tenant_id)
    agent = get_ad_agent_for_tenant(tenant_id)
    return jsonify({
        "ok": True,
        "hybrid_detected": bool(config["hybrid_detected"]) if config else False,
        "sync_server_names": config["sync_server_names"] if config else [],
        "hybrid_checked_at": config["hybrid_checked_at"] if config else None,
        "agent_configured": agent is not None,
        "agent_status": agent["status"] if agent else None,
        "agent_last_seen": agent["last_seen_at"] if agent else None,
    })


@bp.route("/tenants/<tenant_id>/ad-agent/server-names", methods=["POST"])
@admin_required
def update_server_names(tenant_id):
    """Manually add/edit AD sync server names."""
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    data = request.get_json(silent=True) or {}
    server_names = data.get("server_names", [])
    if not isinstance(server_names, list):
        return jsonify({"ok": False, "error": "server_names must be an array"}), 400
    # Sanitize each name
    server_names = [sanitize_text(s, max_length=253) for s in server_names if s.strip()]
    upsert_tenant_ad_config(tenant_id, sync_server_names=server_names)
    log_action(current_user(), "ad_server_names_updated",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Servers: {', '.join(server_names)}")
    return jsonify({"ok": True, "server_names": server_names})


# --- Agent lifecycle ---

@bp.route("/tenants/<tenant_id>/ad-agent/status")
@login_required
def agent_status(tenant_id):
    """Get full agent status for tenant detail page."""
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    config = get_tenant_ad_config(tenant_id)
    agent = get_ad_agent_for_tenant(tenant_id)
    agents_all = list_ad_agents(tenant_id)
    tokens = list_enrollment_tokens(tenant_id)
    templates = list_agent_templates(tenant_id)
    recent_commands = list_agent_commands(tenant_id, limit=10)
    return jsonify({
        "ok": True,
        "config": config,
        "agent": agent,
        "agents_all": agents_all,
        "tokens": tokens,
        "templates": templates,
        "recent_commands": recent_commands,
    })


@bp.route("/tenants/<tenant_id>/ad-agent/enroll", methods=["POST"])
@admin_required
def generate_enrollment(tenant_id):
    """Generate a one-time enrollment token for agent bootstrap."""
    t = get_tenant(tenant_id)
    if not t:
        abort(404)

    # Generate a cryptographically random token
    raw_token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()

    token_id = create_enrollment_token(
        tenant_id, token_hash, expires_at, current_user()
    )
    log_action(current_user(), "agent_enrollment_created",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Token ID: {token_id}, expires: {expires_at}")
    return jsonify({
        "ok": True,
        "token_id": token_id,
        "enrollment_token": raw_token,
        "expires_at": expires_at,
    })


@bp.route("/tenants/<tenant_id>/ad-agent/download")
@admin_required
def download_agent(tenant_id):
    """Download a tenant-specific agent package (ZIP).

    Generates a fresh enrollment token and bundles it with the agent script
    and a tenant-specific enrollment config file.
    """
    t = get_tenant(tenant_id)
    if not t:
        abort(404)

    # Generate enrollment token
    raw_token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    token_id = create_enrollment_token(
        tenant_id, token_hash, expires_at, current_user()
    )

    # Build enrollment config (tenant-specific, no long-lived secrets)
    portal_url = request.host_url.rstrip("/")
    enrollment_config = {
        "portal_url": portal_url,
        "tenant_id": tenant_id,
        "tenant_name": t["name"],
        "enrollment_token": raw_token,
        "token_expires_at": expires_at,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Read the agent script
    agent_script_path = SCRIPTS_DIR / "AD-Agent.ps1"
    try:
        agent_script = agent_script_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "Agent script not found on server"}), 500

    # Read the install helper
    install_script_path = SCRIPTS_DIR / "Install-ADAgent.ps1"
    try:
        install_script = install_script_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        install_script = None

    # Build ZIP in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("OfficeSentry-AD-Agent/AD-Agent.ps1", agent_script)
        zf.writestr("OfficeSentry-AD-Agent/enrollment.json",
                     json.dumps(enrollment_config, indent=2))
        if install_script:
            zf.writestr("OfficeSentry-AD-Agent/Install-ADAgent.ps1", install_script)
        zf.writestr("OfficeSentry-AD-Agent/README.txt", _agent_readme(t["name"]))
    buf.seek(0)

    log_action(current_user(), "agent_package_downloaded",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Token ID: {token_id}")

    safe_name = t["name"].replace(" ", "_").replace("/", "_")[:30]
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"OfficeSentry-AD-Agent-{safe_name}.zip",
    )


@bp.route("/tenants/<tenant_id>/ad-agent/revoke", methods=["POST"])
@admin_required
def revoke_agent_route(tenant_id):
    """Revoke an agent for this tenant."""
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    data = request.get_json(silent=True) or {}
    agent_id = data.get("agent_id", "")
    if not agent_id:
        # Revoke the active agent
        agent = get_ad_agent_for_tenant(tenant_id)
        if not agent:
            return jsonify({"ok": False, "error": "No active agent found"}), 404
        agent_id = agent["id"]
    else:
        agent = get_ad_agent(agent_id)
        if not agent or agent["tenant_id"] != tenant_id:
            return jsonify({"ok": False, "error": "Agent not found for this tenant"}), 404

    revoke_ad_agent(agent_id)
    log_action(current_user(), "agent_revoked",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Agent ID: {agent_id}")
    return jsonify({"ok": True, "message": "Agent revoked"})


@bp.route("/tenants/<tenant_id>/ad-agent/rotate", methods=["POST"])
@admin_required
def rotate_agent_credentials(tenant_id):
    """Rotate the agent's shared secret."""
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    agent = get_ad_agent_for_tenant(tenant_id)
    if not agent:
        return jsonify({"ok": False, "error": "No active agent found"}), 404

    # Generate new secret
    new_secret = secrets.token_urlsafe(48)
    new_secret_enc = encrypt_value(new_secret, current_app.secret_key)
    update_ad_agent(agent["id"], agent_secret_enc=new_secret_enc)

    log_action(current_user(), "agent_credentials_rotated",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Agent ID: {agent['id']}")
    return jsonify({
        "ok": True,
        "message": "Agent credentials rotated. Agent will need to re-enroll.",
    })


# --- Commands ---

@bp.route("/tenants/<tenant_id>/ad-agent/command", methods=["POST"])
@admin_required
def dispatch_command(tenant_id):
    """Queue a command for the agent to execute."""
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    agent = get_ad_agent_for_tenant(tenant_id)
    if not agent:
        return jsonify({"ok": False, "error": "No active agent for this tenant"}), 400

    data = request.get_json(silent=True) or {}
    command_type = data.get("command_type", "").strip()
    parameters = data.get("parameters", {})

    if command_type not in VALID_COMMAND_TYPES:
        return jsonify({"ok": False,
                        "error": f"Invalid command type. Valid: {', '.join(sorted(VALID_COMMAND_TYPES))}"}), 400

    if not isinstance(parameters, dict):
        return jsonify({"ok": False, "error": "parameters must be a JSON object"}), 400

    cmd_id = create_agent_command(
        tenant_id, command_type, parameters,
        current_user(), agent_id=agent["id"],
    )
    log_action(current_user(), "agent_command_dispatched",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Command: {command_type}, ID: {cmd_id}")
    return jsonify({"ok": True, "command_id": cmd_id})


@bp.route("/tenants/<tenant_id>/ad-agent/commands")
@login_required
def list_commands(tenant_id):
    """List recent commands for a tenant's agent."""
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    status_filter = request.args.get("status")
    limit = min(int(request.args.get("limit", 50)), 200)
    commands = list_agent_commands(tenant_id, limit=limit, status=status_filter)
    return jsonify({"ok": True, "commands": commands})


# --- Templates ---

@bp.route("/tenants/<tenant_id>/ad-agent/templates")
@login_required
def templates_list(tenant_id):
    """List user templates for a tenant."""
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    templates = list_agent_templates(tenant_id)
    return jsonify({"ok": True, "templates": templates})


@bp.route("/tenants/<tenant_id>/ad-agent/templates", methods=["POST"])
@admin_required
def template_create(tenant_id):
    """Create a new user template."""
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    data = request.get_json(silent=True) or {}
    name = sanitize_text(data.get("name", ""), max_length=200)
    description = sanitize_text(data.get("description", ""), max_length=1000)
    template_data = data.get("template_data", {})

    if not name:
        return jsonify({"ok": False, "error": "Template name is required"}), 400
    if not isinstance(template_data, dict):
        return jsonify({"ok": False, "error": "template_data must be a JSON object"}), 400

    tmpl_id = create_agent_template(
        tenant_id, name, description, template_data, current_user()
    )
    log_action(current_user(), "agent_template_created",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Template: {name}, ID: {tmpl_id}")
    return jsonify({"ok": True, "template_id": tmpl_id})


@bp.route("/tenants/<tenant_id>/ad-agent/templates/<template_id>", methods=["PUT"])
@admin_required
def template_update(tenant_id, template_id):
    """Update an existing user template."""
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    tmpl = get_agent_template(template_id)
    if not tmpl or tmpl["tenant_id"] != tenant_id:
        return jsonify({"ok": False, "error": "Template not found"}), 404

    data = request.get_json(silent=True) or {}
    fields = {}
    if "name" in data:
        fields["name"] = sanitize_text(data["name"], max_length=200)
    if "description" in data:
        fields["description"] = sanitize_text(data["description"], max_length=1000)
    if "template_data" in data:
        if not isinstance(data["template_data"], dict):
            return jsonify({"ok": False, "error": "template_data must be a JSON object"}), 400
        fields["template_data"] = data["template_data"]

    update_agent_template(template_id, **fields)
    return jsonify({"ok": True, "message": "Template updated"})


@bp.route("/tenants/<tenant_id>/ad-agent/templates/<template_id>", methods=["DELETE"])
@admin_required
def template_delete(tenant_id, template_id):
    """Delete a user template."""
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    tmpl = get_agent_template(template_id)
    if not tmpl or tmpl["tenant_id"] != tenant_id:
        return jsonify({"ok": False, "error": "Template not found"}), 404

    delete_agent_template(template_id)
    log_action(current_user(), "agent_template_deleted",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Template: {tmpl['name']}")
    return jsonify({"ok": True, "message": "Template deleted"})


# ===================================================================
# Agent-facing API routes (HMAC auth, no CSRF)
# ===================================================================

@agent_api_bp.route("/api/agent/enroll", methods=["POST"])
def agent_enroll():
    """Exchange a one-time enrollment token for agent credentials.

    Request body (JSON):
        tenant_id:        tenant record ID
        enrollment_token: the raw token from enrollment.json
        agent_name:       optional display name for the agent
        server_names:     optional list of AD server names
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False, "error": "JSON body required"}), 400

    tenant_id = data.get("tenant_id", "").strip()
    raw_token = data.get("enrollment_token", "").strip()
    agent_name = data.get("agent_name", "Default Agent").strip()[:200]
    server_names = data.get("server_names", [])

    if not tenant_id or not raw_token:
        return jsonify({"ok": False, "error": "tenant_id and enrollment_token required"}), 400

    # Verify tenant exists
    t = get_tenant(tenant_id)
    if not t:
        return jsonify({"ok": False, "error": "Unknown tenant"}), 404

    # Hash the token and look up
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    token_record = validate_enrollment_token(tenant_id, token_hash)
    if not token_record:
        log.warning("Agent enrollment: invalid/expired token for tenant %s", tenant_id)
        return jsonify({"ok": False, "error": "Invalid or expired enrollment token"}), 401

    # Generate agent credentials
    agent_secret = secrets.token_urlsafe(48)
    agent_secret_enc = encrypt_value(agent_secret, current_app.secret_key)

    # Create agent record
    agent_id = create_ad_agent(tenant_id, agent_name, agent_secret_enc)

    # Update agent with server names and IP
    update_fields = {"last_seen_at": datetime.now(timezone.utc).isoformat(),
                     "last_ip": request.remote_addr or ""}
    if isinstance(server_names, list) and server_names:
        update_fields["server_names"] = server_names
    update_ad_agent(agent_id, **update_fields)

    # Consume the token
    consume_enrollment_token(token_record["id"], agent_id)

    # Update hybrid config if server names provided
    if isinstance(server_names, list) and server_names:
        upsert_tenant_ad_config(
            tenant_id,
            hybrid_detected=1,
            sync_server_names=server_names,
            hybrid_checked_at=datetime.now(timezone.utc).isoformat(),
        )

    log_action("agent", "agent_enrolled",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Agent ID: {agent_id}, Name: {agent_name}, IP: {request.remote_addr}")

    log.info("Agent enrolled: %s for tenant %s from %s",
             agent_id, tenant_id, request.remote_addr)

    return jsonify({
        "ok": True,
        "agent_id": agent_id,
        "agent_secret": agent_secret,
        "message": "Enrollment successful. Store the agent_secret securely.",
    })


@agent_api_bp.route("/api/agent/heartbeat", methods=["POST"])
@agent_auth_required
def agent_heartbeat():
    """Agent heartbeat — updates last_seen and reports status."""
    agent = g.agent
    data = request.get_json(silent=True) or {}

    update_fields = {
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
        "last_ip": request.remote_addr or "",
    }
    if "agent_version" in data:
        update_fields["agent_version"] = str(data["agent_version"])[:50]
    if "server_names" in data and isinstance(data["server_names"], list):
        update_fields["server_names"] = data["server_names"]

    update_ad_agent(agent["id"], **update_fields)
    return jsonify({"ok": True, "message": "Heartbeat received"})


@agent_api_bp.route("/api/agent/poll", methods=["POST"])
@agent_auth_required
def agent_poll():
    """Agent polls for pending commands."""
    agent = g.agent

    # Update last_seen
    update_ad_agent(agent["id"],
                    last_seen_at=datetime.now(timezone.utc).isoformat(),
                    last_ip=request.remote_addr or "")

    # Fetch pending commands
    commands = poll_pending_commands(agent["tenant_id"], agent["id"])

    # Strip internal fields, return only what agent needs
    cmd_list = []
    for c in commands:
        cmd_list.append({
            "id": c["id"],
            "command_type": c["command_type"],
            "parameters": c["parameters"],
        })

    return jsonify({"ok": True, "commands": cmd_list})


@agent_api_bp.route("/api/agent/result", methods=["POST"])
@agent_auth_required
def agent_result():
    """Agent reports command execution results."""
    agent = g.agent
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False, "error": "JSON body required"}), 400

    command_id = data.get("command_id", "").strip()
    result = data.get("result", {})
    status = data.get("status", "completed")

    if not command_id:
        return jsonify({"ok": False, "error": "command_id required"}), 400

    # Verify command belongs to this agent's tenant
    cmd = get_agent_command(command_id)
    if not cmd or cmd["tenant_id"] != agent["tenant_id"]:
        return jsonify({"ok": False, "error": "Command not found"}), 404
    if cmd["status"] not in ("running", "pending"):
        return jsonify({"ok": False, "error": "Command already completed"}), 400

    complete_agent_command(command_id, result, status)

    log_action("agent", "agent_command_completed",
               tenant_id=agent["tenant_id"],
               details=f"Command: {cmd['command_type']}, ID: {command_id}, Status: {status}")

    return jsonify({"ok": True, "message": "Result recorded"})


# ===================================================================
# Helpers
# ===================================================================

def _agent_readme(tenant_name: str) -> str:
    return f"""Office Sentry AD Agent — {tenant_name}
{'=' * (30 + len(tenant_name))}

QUICK START
-----------
1. Copy this folder to the server where Active Directory is reachable.

2. Ensure PowerShell 5.1+ and the ActiveDirectory module are available:
     Install-WindowsFeature RSAT-AD-PowerShell   (Windows Server)
     Add-WindowsCapability -Name Rsat.ActiveDirectory.DS-LDS.Tools~~~~0.0.1.0 -Online  (Windows 10/11)

3. Run the agent for the first time (it will enroll using the bundled token):
     .\\AD-Agent.ps1

4. The enrollment token in enrollment.json is single-use and expires in 24 hours.
   After successful enrollment, the file is automatically deleted.

5. To install as a scheduled task (runs every 60 seconds):
     .\\Install-ADAgent.ps1

SECURITY NOTES
--------------
- The enrollment token is ONE-TIME USE. After enrollment it is consumed.
- Agent credentials are stored locally using Windows DPAPI encryption.
- All communication with the portal uses HMAC-SHA256 signed requests.
- The agent can only access data for tenant: {tenant_name}
- To revoke access, use the portal's "Revoke Agent" button.

REQUIREMENTS
------------
- Windows Server 2016+ or Windows 10/11 with RSAT
- PowerShell 5.1+ (PowerShell 7 recommended)
- ActiveDirectory PowerShell module
- Network access to the portal URL (outbound HTTPS only)
"""
