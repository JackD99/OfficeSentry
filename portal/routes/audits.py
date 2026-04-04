"""
portal/routes/audits.py — Breach investigation and batch routes.
"""

import logging

from flask import (
    Blueprint, render_template, request, jsonify, abort,
)

from database import (
    get_tenant, list_tenants, touch_tenant_audit, log_action,
)
from helpers import (
    login_required, current_user,
    tenant_pfx_path, tenant_output_dir,
    build_ps_env, start_job, start_python_job, get_tenant_pfx_password,
    validate_email,
    SCRIPTS_DIR, PWSH,
)

log = logging.getLogger("portal")

bp = Blueprint("audits", __name__)


# ---------------------------------------------------------------------------
# Audit Catalogue
# ---------------------------------------------------------------------------

# Shared audit-type metadata used by catalogue and batch templates
AUDIT_CATALOGUE = [
    # Breach Investigation
    {
        "key": "signin", "category": "breach",
        "name": "Sign-In Logs", "icon": "box-arrow-in-right", "color": "warning",
        "desc": "Azure AD sign-in history — failed attempts, unusual locations, risky sign-ins.",
    },
    {
        "key": "oauth", "category": "breach",
        "name": "OAuth Consent", "icon": "key", "color": "info",
        "desc": "Reviews third-party app permissions — detects consent phishing.",
    },
]


@bp.route("/audits")
@login_required
def catalogue():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]

    return render_template(
        "audits_catalogue.html",
        tenants=ready_tenants,
        audit_types=AUDIT_CATALOGUE,
        focus_category=request.args.get("category", ""),
        focus_type=request.args.get("type", ""),
    )


# ---------------------------------------------------------------------------
# Breach Investigation
# ---------------------------------------------------------------------------

@bp.route("/tenants/<tenant_id>/audit/breach")
@login_required
def audit_breach(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    ready = bool(t["client_id"]) and tenant_pfx_path(tenant_id).exists()
    return render_template("audit_breach.html", tenant=t, ready=ready)


@bp.route("/tenants/<tenant_id>/audit/breach/run", methods=["POST"])
@login_required
def audit_breach_run(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404

    investigation = request.form.get("investigation", "")
    date_start    = request.form.get("date_start", "").strip()
    date_end      = request.form.get("date_end", "").strip()
    target_user   = request.form.get("target_user", "").strip()

    if target_user and not validate_email(target_user):
        return jsonify({"ok": False, "error": "Invalid target user email format."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    tenant_val = t["tenant_id"] or t["domain"]
    client     = t["client_id"]
    pfx_path   = str(tenant_pfx_path(tenant_id))
    out_dir    = str(tenant_output_dir(tenant_id))

    env = build_ps_env(pfx_pass)

    script_map = {
        "signin":     "Get-SignInLogs.ps1",
        "oauth":      "Audit-OAuthConsent.ps1",
    }
    label_map = {
        "signin":     "Sign-In Log Analysis",
        "oauth":      "OAuth Consent Review",
    }

    if investigation not in script_map:
        return jsonify({"ok": False, "error": f"Unknown investigation type: {investigation}"})

    label = f"{label_map[investigation]} — {t['name']}"

    # Sign-in logs use native Python (Graph API)
    if investigation == "signin":
        from graph_jobs import fetch_sign_in_logs
        job_id = start_python_job(
            label, "breach",
            fetch_sign_in_logs,
            args=(dict(t), pfx_path, pfx_pass),
            kwargs={
                "start_date": date_start,
                "end_date": date_end,
                "target_user": target_user,
            },
            tenant_id=tenant_id,
        )
    else:
        script_name = script_map[investigation]

        base_args = [
            "-Tenant",          tenant_val,
            "-ClientId",        client,
            "-CertificatePath", pfx_path,
            "-OutputFolder",    out_dir,
        ]

        if date_start:
            base_args += ["-StartDate", date_start]
        if date_end:
            base_args += ["-EndDate", date_end]

        cmd = [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
               str(SCRIPTS_DIR / script_name)] + base_args

        job_id = start_job(label, "breach", cmd, tenant_id=tenant_id, env=env)
    touch_tenant_audit(tenant_id)

    log_action(current_user(), f"breach_{investigation}",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Target: {target_user or 'all'}, Range: {date_start} to {date_end}")

    return jsonify({"ok": True, "job_id": job_id})


# ---------------------------------------------------------------------------
# Batch Operations
# ---------------------------------------------------------------------------

@bp.route("/batch")
@login_required
def batch_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("batch.html", tenants=ready_tenants)


BATCH_TYPES = {
    # Breach
    "breach_signin":  "Sign-In Logs",
    "breach_oauth":   "OAuth Consent",
}

BREACH_SCRIPT_MAP = {
    "breach_oauth":    "Audit-OAuthConsent.ps1",
}


@bp.route("/batch/audit", methods=["POST"])
@login_required
def batch_audit():
    tenant_ids  = request.form.getlist("tenant_ids")
    audit_type  = request.form.get("audit_type", "breach_signin")

    if not tenant_ids:
        return jsonify({"ok": False, "error": "No tenants selected."})

    if audit_type not in BATCH_TYPES:
        return jsonify({"ok": False, "error": "Invalid audit type."})

    batch_target    = request.form.get("target_user", "").strip()
    batch_start     = request.form.get("date_start", "").strip()
    batch_end       = request.form.get("date_end", "").strip()

    # Validate breach target email if provided
    if batch_target and audit_type.startswith("breach_"):
        if not validate_email(batch_target):
            return jsonify({"ok": False, "error": "Invalid target user email format."})

    job_ids = []
    tenant_jobs = []  # [{tenant_id, tenant_name, job_id}]

    for tid in tenant_ids:
        t = get_tenant(tid)
        if not t or not t["client_id"]:
            continue

        pfx_pass = get_tenant_pfx_password(t)
        if not pfx_pass:
            continue

        tenant_val = t["tenant_id"] or t["domain"]
        pfx_path   = str(tenant_pfx_path(tid))
        out_dir    = str(tenant_output_dir(tid))
        env = build_ps_env(pfx_pass)
        label_name = BATCH_TYPES[audit_type]

        if audit_type == "breach_signin":
            # Sign-in logs use native Python
            from graph_jobs import fetch_sign_in_logs
            label = f"Batch {label_name} — {t['name']}"
            jid = start_python_job(
                label, "batch",
                fetch_sign_in_logs,
                args=(dict(t), pfx_path, pfx_pass),
                kwargs={
                    "start_date": batch_start,
                    "end_date": batch_end,
                    "target_user": batch_target,
                },
                tenant_id=tid,
            )
            touch_tenant_audit(tid)
            job_ids.append(jid)
            tenant_jobs.append({
                "tenant_id": tid, "tenant_name": t["name"], "job_id": jid,
            })
            continue
        elif audit_type in BREACH_SCRIPT_MAP:
            base_args = [
                "-Tenant",          tenant_val,
                "-ClientId",        t["client_id"],
                "-CertificatePath", pfx_path,
                "-OutputFolder",    out_dir,
            ]
            if batch_start:
                base_args += ["-StartDate", batch_start]
            if batch_end:
                base_args += ["-EndDate", batch_end]
            cmd = [
                PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
                str(SCRIPTS_DIR / BREACH_SCRIPT_MAP[audit_type]),
            ] + base_args
        else:
            continue

        label = f"Batch {label_name} — {t['name']}"
        jid = start_job(label, "batch", cmd, tenant_id=tid, env=env)
        touch_tenant_audit(tid)
        job_ids.append(jid)
        tenant_jobs.append({
            "tenant_id": tid,
            "tenant_name": t["name"],
            "job_id": jid,
        })

    log_action(current_user(), "batch_audit",
               details=f"Type: {audit_type}, Tenants: {len(job_ids)}")

    return jsonify({
        "ok": True,
        "job_ids": job_ids,
        "tenant_jobs": tenant_jobs,
        "message": f"Started {len(job_ids)} {BATCH_TYPES[audit_type]} job(s).",
    })
