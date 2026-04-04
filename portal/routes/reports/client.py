"""Client dashboard and audit trail routes."""

from flask import render_template, request, abort

from helpers import login_required, current_user
from database import (
    get_tenant, list_tenants, get_audit_log,
    get_bpa_results, get_latest_secure_score,
    get_tenant_branding, get_default_branding,
)

from routes.reports import bp, _collect_reports, TYPE_BADGE_COLORS


# ---------------------------------------------------------------------------
# Client Dashboard
# ---------------------------------------------------------------------------

@bp.route("/client-dashboard")
@login_required
def client_dashboard():
    """Read-only client-facing dashboard scoped to a single tenant."""
    from database import get_user
    from permissions import is_client_user, get_client_tenant_id
    from report_engine import WEASYPRINT_AVAILABLE

    user = get_user(current_user())
    if not user:
        abort(403)

    # Determine tenant: client users get their scoped tenant,
    # other users can view any tenant's client dashboard via query param
    if is_client_user(user):
        tenant_id = get_client_tenant_id(user)
    else:
        tenant_id = request.args.get("tenant_id")

    if not tenant_id:
        abort(404)

    tenant = get_tenant(tenant_id)
    if not tenant:
        abort(404)

    # Gather data
    bpa = get_bpa_results(tenant_id)
    score = get_latest_secure_score(tenant_id)
    branding = get_tenant_branding(tenant_id) or get_default_branding()
    reports_list, _ = _collect_reports(tenant_id)

    bpa_pass = sum(1 for r in bpa if r.get("status") == "pass")
    bpa_fail = sum(1 for r in bpa if r.get("status") == "fail")

    return render_template("client_dashboard.html",
                           tenant=tenant,
                           secure_score=score,
                           bpa_results=bpa,
                           bpa_pass=bpa_pass,
                           bpa_fail=bpa_fail,
                           reports=reports_list,
                           branding=branding,
                           pdf_available=WEASYPRINT_AVAILABLE,
                           type_badge_colors=TYPE_BADGE_COLORS)


# ---------------------------------------------------------------------------
# Audit Trail
# ---------------------------------------------------------------------------

@bp.route("/audit-log")
@login_required
def audit_trail():
    tenant_id = request.args.get("tenant_id")
    entries = get_audit_log(limit=500, tenant_id=tenant_id)
    tenants = list_tenants()
    return render_template("audit_trail.html", entries=entries,
                           tenants=tenants, filter_tenant=tenant_id)
