"""
portal/report_engine.py — PDF report generation engine.

Uses WeasyPrint to render Jinja2 HTML templates into branded PDF documents.
WeasyPrint is optional — if not installed, PDF endpoints return a helpful error.
"""

import logging
from datetime import datetime

from flask import render_template

log = logging.getLogger("portal")

# Graceful WeasyPrint import — it requires system libraries (cairo, pango)
try:
    from weasyprint import HTML
    WEASYPRINT_AVAILABLE = True
except (ImportError, OSError):
    WEASYPRINT_AVAILABLE = False
    log.info("WeasyPrint not available — PDF generation disabled. "
             "Install with: pip install weasyprint")


REPORT_TYPES = {
    "executive_summary": {
        "template": "reports/pdf_executive_summary.html",
        "title": "Executive Summary",
        "description": "One-page risk overview with key metrics",
    },
    "security_posture": {
        "template": "reports/pdf_security_posture.html",
        "title": "Security Posture Report",
        "description": "365Inspect findings organized by severity",
    },
    "bpa_compliance": {
        "template": "reports/pdf_bpa_compliance.html",
        "title": "BPA Compliance Report",
        "description": "Best Practice Analysis pass/fail results with guidance",
    },
    "identity_risk": {
        "template": "reports/pdf_identity_risk.html",
        "title": "Identity Risk Report",
        "description": "MFA gaps, risky users, and authentication analysis",
    },
    "license_optimization": {
        "template": "reports/pdf_license_optimization.html",
        "title": "License Optimization Report",
        "description": "Unused licenses and cost savings opportunities",
    },
    "breach_investigation": {
        "template": "reports/pdf_breach_investigation.html",
        "title": "Breach Investigation Report",
        "description": "Investigation timeline, IOCs, and affected accounts",
    },
}


def get_report_data(report_type: str, tenant_id: str) -> dict | None:
    """Fetch data for a specific report type from the database."""
    from database import (
        get_tenant, get_bpa_results, get_latest_secure_score,
        get_latest_license_snapshot, get_latest_device_inventory,
        get_cached_data, list_incident_cases,
    )

    tenant = get_tenant(tenant_id)
    if not tenant:
        return None

    data = {
        "tenant": dict(tenant),
        "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "report_title": REPORT_TYPES.get(report_type, {}).get("title", "Report"),
    }

    if report_type == "executive_summary":
        data["bpa_results"] = get_bpa_results(tenant_id)
        score = get_latest_secure_score(tenant_id)
        data["secure_score"] = dict(score) if score else None
        snap = get_latest_license_snapshot(tenant_id)
        data["license_snapshot"] = dict(snap) if snap else None
        inv = get_latest_device_inventory(tenant_id)
        data["device_inventory"] = dict(inv) if inv else None
        # Summarize BPA
        bpa = data["bpa_results"]
        data["bpa_summary"] = {
            "pass": sum(1 for r in bpa if r.get("status") == "pass"),
            "fail": sum(1 for r in bpa if r.get("status") == "fail"),
            "warn": sum(1 for r in bpa if r.get("status") == "warning"),
            "total": len(bpa),
        }

    elif report_type == "security_posture":
        cached = get_cached_data(tenant_id, "security_scan")
        findings = []
        if cached:
            import json
            try:
                findings = json.loads(cached["data"]) if isinstance(cached["data"], str) else cached["data"]
            except (json.JSONDecodeError, TypeError):
                findings = []
        data["findings"] = findings if isinstance(findings, list) else []
        # Group by severity
        severity_groups = {"Critical": [], "High": [], "Medium": [], "Low": [], "Informational": []}
        for f in data["findings"]:
            sev = f.get("severity", f.get("Impact", "Medium"))
            if sev in severity_groups:
                severity_groups[sev].append(f)
            else:
                severity_groups["Medium"].append(f)
        data["severity_groups"] = severity_groups

    elif report_type == "bpa_compliance":
        data["bpa_results"] = get_bpa_results(tenant_id)
        # Group by category
        categories = {}
        for r in data["bpa_results"]:
            cat = r.get("category", "General")
            categories.setdefault(cat, []).append(r)
        data["categories"] = categories

    elif report_type == "identity_risk":
        cached_risky = get_cached_data(tenant_id, "risky_users")
        cached_signins = get_cached_data(tenant_id, "risky_signins")
        cached_mfa = get_cached_data(tenant_id, "mfa_report")
        import json
        data["risky_users"] = []
        data["risky_signins"] = []
        data["mfa_report"] = []
        if cached_risky:
            try:
                d = json.loads(cached_risky["data"]) if isinstance(cached_risky["data"], str) else cached_risky["data"]
                data["risky_users"] = d if isinstance(d, list) else []
            except (json.JSONDecodeError, TypeError):
                pass
        if cached_signins:
            try:
                d = json.loads(cached_signins["data"]) if isinstance(cached_signins["data"], str) else cached_signins["data"]
                data["risky_signins"] = d if isinstance(d, list) else []
            except (json.JSONDecodeError, TypeError):
                pass
        if cached_mfa:
            try:
                d = json.loads(cached_mfa["data"]) if isinstance(cached_mfa["data"], str) else cached_mfa["data"]
                data["mfa_report"] = d if isinstance(d, list) else []
            except (json.JSONDecodeError, TypeError):
                pass

    elif report_type == "license_optimization":
        snap = get_latest_license_snapshot(tenant_id)
        data["license_snapshot"] = dict(snap) if snap else None
        import json
        data["sku_data"] = []
        if snap:
            try:
                d = json.loads(snap["sku_data"]) if isinstance(snap["sku_data"], str) else snap["sku_data"]
                data["sku_data"] = d if isinstance(d, list) else []
            except (json.JSONDecodeError, TypeError):
                pass

    elif report_type == "breach_investigation":
        cases = list_incident_cases(tenant_id=tenant_id, status=None)
        data["cases"] = cases

    return data


def generate_pdf(report_type: str, tenant_id: str, branding: dict | None = None) -> tuple[bytes, str]:
    """Generate a PDF report.

    Returns (pdf_bytes, filename).
    Raises RuntimeError if WeasyPrint is unavailable, ValueError for bad inputs.
    """
    if not WEASYPRINT_AVAILABLE:
        raise RuntimeError(
            "WeasyPrint is not installed. Install system dependencies and run: "
            "pip install weasyprint"
        )

    if report_type not in REPORT_TYPES:
        raise ValueError(f"Unknown report type: {report_type}")

    report_info = REPORT_TYPES[report_type]
    data = get_report_data(report_type, tenant_id)
    if data is None:
        raise ValueError("Tenant not found")

    # Merge branding
    if not branding:
        from database import get_tenant_branding, get_default_branding
        branding = get_tenant_branding(tenant_id) or get_default_branding()
    data["branding"] = branding

    html_string = render_template(report_info["template"], **data)
    pdf_bytes = HTML(string=html_string).write_pdf()

    domain = data["tenant"].get("domain", "unknown")
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M")
    filename = f"{report_type}_{domain}_{timestamp}.pdf"

    return pdf_bytes, filename


def generate_pdf_from_html(html_string: str) -> bytes:
    """Convert raw HTML string to PDF bytes."""
    if not WEASYPRINT_AVAILABLE:
        raise RuntimeError("WeasyPrint is not installed.")
    return HTML(string=html_string).write_pdf()
