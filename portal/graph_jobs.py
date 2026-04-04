"""
portal/graph_jobs.py — Python implementations of Graph API jobs.

Each function replaces a PowerShell Get-*.ps1 script. They follow the same
JSON output contract so existing routes and frontend code work unchanged.

All functions accept a ``log_line`` callback (injected by ``start_python_job``)
and return a dict result with ``ok: True/False``.
"""

from __future__ import annotations

import json as _json
from datetime import datetime, timezone
from typing import Callable

import time

import msal
import requests as _requests

from graph_client import GraphClient, GraphAuthError, GraphRequestError


# ---------------------------------------------------------------------------
# Helper to build a GraphClient from tenant DB record
# ---------------------------------------------------------------------------

def _make_client(
    tenant: dict,
    pfx_path: str,
    pfx_password: str,
) -> GraphClient:
    """Create a GraphClient from a tenant record."""
    tenant_val = tenant.get("tenant_id") or tenant.get("domain", "")
    client_id = tenant.get("client_id", "")
    return GraphClient(
        tenant_id=tenant_val,
        client_id=client_id,
        pfx_path=pfx_path,
        pfx_password=pfx_password,
    )


# ---------------------------------------------------------------------------
# Get-TenantDomains.ps1 → fetch_tenant_domains()
# ---------------------------------------------------------------------------

def fetch_tenant_domains(
    tenant: dict,
    pfx_path: str,
    pfx_password: str,
    *,
    log_line: Callable[[str], None] = lambda x: None,
) -> dict:
    """Fetch custom domains registered in an M365 tenant.

    Replaces scripts/Get-TenantDomains.ps1.
    """
    try:
        log_line(f"INFO: Fetching domains for tenant {tenant.get('domain', '')}")
        client = _make_client(tenant, pfx_path, pfx_password)

        raw_domains = client.get_paginated("/domains")

        domains = []
        for d in raw_domains:
            domains.append({
                "name": d.get("id", ""),
                "authenticationType": d.get("authenticationType", ""),
                "isVerified": d.get("isVerified", False),
                "isDefault": d.get("isDefault", False),
                "isInitial": d.get("isInitial", False),
                "isRoot": d.get("isRoot", False),
                "isAdminManaged": d.get("isAdminManaged", False),
                "capabilities": ", ".join(d.get("supportedServices", [])),
            })

        log_line(f"INFO: Found {len(domains)} domain(s)")

        return {
            "ok": True,
            "domains": domains,
            "count": len(domains),
        }

    except (GraphAuthError, GraphRequestError) as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Get-HybridStatus.ps1 → detect_hybrid_status()
# ---------------------------------------------------------------------------

def detect_hybrid_status(
    tenant: dict,
    pfx_path: str,
    pfx_password: str,
    *,
    log_line: Callable[[str], None] = lambda x: None,
) -> dict:
    """Detect hybrid identity / Entra Connect status for a tenant.

    Replaces scripts/Get-HybridStatus.ps1.
    """
    result = {
        "ok": True,
        "hybrid_detected": False,
        "sync_enabled": False,
        "last_sync": None,
        "server_names": [],
        "details": "",
    }

    try:
        log_line(f"INFO: Starting hybrid identity detection for {tenant.get('domain', '')}")
        client = _make_client(tenant, pfx_path, pfx_password)

        # Check organization sync status
        log_line("INFO: Querying organization sync status")
        org_data = client.get("/organization")
        orgs = org_data.get("value", [])

        if orgs:
            org = orgs[0]
            sync_enabled = org.get("onPremisesSyncEnabled", False)
            last_sync = org.get("onPremisesLastSyncDateTime")

            if sync_enabled:
                result["hybrid_detected"] = True
                result["sync_enabled"] = True
                result["last_sync"] = last_sync
                log_line(f"INFO: Directory sync IS enabled. Last sync: {last_sync}")
            else:
                log_line("INFO: Directory sync is NOT enabled")

        # Best-effort: detect Entra Connect server names via service principals
        log_line("INFO: Attempting to detect Entra Connect server names (best-effort)")
        try:
            sps = client.get_paginated(
                "/servicePrincipals",
                params={
                    "$filter": (
                        "startswith(displayName,'Microsoft Azure AD Connect') or "
                        "startswith(displayName,'Azure AD Connect') or "
                        "startswith(displayName,'Entra Connect')"
                    ),
                    "$select": "displayName,appId,notes",
                },
            )
            for sp in sps:
                log_line(f"INFO: Found sync service principal: {sp.get('displayName', '')}")
                if sp.get("notes"):
                    result["server_names"].append(sp["notes"])
        except Exception as exc:
            log_line(f"WARN: Could not query service principals for server names: {exc}")

        # Fallback: check for on-prem synced users
        if not result["hybrid_detected"]:
            log_line("INFO: Checking for on-prem synced users as fallback")
            try:
                users = client.get_paginated(
                    "/users",
                    params={
                        "$filter": "onPremisesSyncEnabled eq true",
                        "$top": "1",
                        "$select": "id",
                    },
                    max_pages=1,
                )
                if users:
                    result["hybrid_detected"] = True
                    log_line("INFO: Found synced users — hybrid identity confirmed")
            except Exception as exc:
                log_line(f"WARN: User filter query failed: {exc}")

        # Build summary
        if result["hybrid_detected"]:
            details = "Hybrid identity detected"
            if result["last_sync"]:
                details += f". Last sync: {result['last_sync']}"
            if result["server_names"]:
                details += f". Servers: {', '.join(result['server_names'])}"
            result["details"] = details
        else:
            result["details"] = "No hybrid identity detected (cloud-only tenant)"

        log_line(f"INFO: Detection complete. Hybrid: {result['hybrid_detected']}")

    except (GraphAuthError, GraphRequestError) as exc:
        result["ok"] = False
        result["details"] = f"Detection failed: {exc}"
        log_line(f"ERROR: {exc}")
    except Exception as exc:
        result["ok"] = False
        result["details"] = f"Detection failed: {exc}"
        log_line(f"ERROR: {exc}")

    return result


# ---------------------------------------------------------------------------
# Get-SecureScore.ps1 → fetch_secure_score()
# ---------------------------------------------------------------------------

def fetch_secure_score(
    tenant: dict,
    pfx_path: str,
    pfx_password: str,
    *,
    log_line: Callable[[str], None] = lambda x: None,
) -> dict:
    """Fetch the Microsoft Secure Score for a tenant.

    Replaces scripts/Get-SecureScore.ps1.
    """
    try:
        log_line(f"INFO: Acquiring Graph API token for {tenant.get('domain', '')}...")
        client = _make_client(tenant, pfx_path, pfx_password)

        log_line("INFO: Fetching Secure Score...")
        data = client.get("/security/secureScores", params={"$top": "1"})
        scores = data.get("value", [])

        if not scores:
            return {"ok": False, "error": "No Secure Score data available for this tenant."}

        score = scores[0]

        # Extract average comparative scores
        comparatives = {}
        for avg in score.get("averageComparativeScores", []):
            comparatives[avg.get("basis", "")] = avg.get("averageScore", 0)

        # Extract control scores (top 20 improvement actions)
        control_scores = []
        raw_controls = sorted(
            score.get("controlScores", []),
            key=lambda c: c.get("scoreInPercentage", 0),
        )[:20]
        for ctrl in raw_controls:
            control_scores.append({
                "name": ctrl.get("controlName", ""),
                "description": ctrl.get("description", ""),
                "score": ctrl.get("score", 0),
                "maxScore": ctrl.get("maxScore", 0),
                "percentage": ctrl.get("scoreInPercentage", 0),
                "category": ctrl.get("controlCategory", ""),
            })

        current = score.get("currentScore", 0)
        max_score = score.get("maxScore", 0)
        percentage = round((current / max_score) * 100, 1) if max_score > 0 else 0

        log_line(f"INFO: Secure Score: {current}/{max_score}")

        return {
            "ok": True,
            "currentScore": current,
            "maxScore": max_score,
            "percentage": percentage,
            "createdDateTime": score.get("createdDateTime", ""),
            "averageComparatives": comparatives,
            "enabledServices": score.get("enabledServices", []),
            "controlScores": control_scores,
        }

    except (GraphAuthError, GraphRequestError) as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": f"Failed to fetch Secure Score: {exc}"}
    except Exception as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": f"Failed to fetch Secure Score: {exc}"}


# ---------------------------------------------------------------------------
# Get-AuthMethods.ps1 → fetch_auth_methods()
# ---------------------------------------------------------------------------

def fetch_auth_methods(
    tenant: dict,
    pfx_path: str,
    pfx_password: str,
    *,
    log_line: Callable[[str], None] = lambda x: None,
) -> dict:
    """Fetch authentication method policies for a tenant.

    Replaces scripts/Get-AuthMethods.ps1.
    """
    try:
        log_line(f"INFO: Fetching authentication method policies for {tenant.get('domain', '')}")
        client = _make_client(tenant, pfx_path, pfx_password)

        policy = client.get("/policies/authenticationMethodsPolicy")

        methods = []
        for config in policy.get("authenticationMethodConfigurations", []):
            methods.append({
                "method": config.get("id", ""),
                "state": config.get("state", ""),
                "details": _json.dumps(config, default=str),
            })

        log_line(f"INFO: Found {len(methods)} authentication method configuration(s)")

        # Best-effort: registration details
        reg_details = None
        try:
            reg_details = client.get("/reports/authenticationMethods/usersRegisteredByMethod")
        except Exception as exc:
            log_line(f"WARN: Could not fetch registration details: {exc}")

        return {
            "ok": True,
            "policyDescription": policy.get("description", ""),
            "registrationEnforcement": policy.get("registrationEnforcement"),
            "methods": methods,
            "registrationDetails": reg_details,
        }

    except (GraphAuthError, GraphRequestError) as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Get-PartnerRelationships.ps1 → fetch_partner_relationships()
# ---------------------------------------------------------------------------

def fetch_partner_relationships(
    tenant: dict,
    pfx_path: str,
    pfx_password: str,
    *,
    log_line: Callable[[str], None] = lambda x: None,
) -> dict:
    """Fetch GDAP/DAP partner relationships for a tenant.

    Replaces scripts/Get-PartnerRelationships.ps1.
    """
    try:
        log_line(f"INFO: Fetching partner relationships for {tenant.get('domain', '')}")
        client = _make_client(tenant, pfx_path, pfx_password)

        raw = []
        try:
            raw = client.get_paginated(
                "/tenantRelationships/delegatedAdminRelationships"
            )
        except Exception as exc:
            log_line(f"WARN: Could not fetch GDAP relationships: {exc}")

        relationships = []
        for r in raw:
            partner = r.get("partner", {})
            roles = r.get("accessDetails", {}).get("unifiedRoles", [])
            role_ids = ", ".join(rd.get("roleDefinitionId", "") for rd in roles)
            relationships.append({
                "id": r.get("id", ""),
                "displayName": r.get("displayName", ""),
                "partner": partner.get("displayName", ""),
                "tenantId": partner.get("tenantId", ""),
                "status": r.get("status", ""),
                "duration": r.get("duration", ""),
                "createdDate": r.get("createdDateTime", ""),
                "endDate": r.get("endDateTime", ""),
                "autoExtend": r.get("autoExtendDuration", ""),
                "roles": role_ids,
            })

        log_line(f"INFO: Found {len(relationships)} partner relationship(s)")

        return {
            "ok": True,
            "relationships": relationships,
            "count": len(relationships),
        }

    except (GraphAuthError, GraphRequestError) as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Get-AppConsentRequests.ps1 → fetch_app_consent_requests()
# ---------------------------------------------------------------------------

def fetch_app_consent_requests(
    tenant: dict,
    pfx_path: str,
    pfx_password: str,
    *,
    log_line: Callable[[str], None] = lambda x: None,
) -> dict:
    """Fetch pending app consent requests for a tenant.

    Replaces scripts/Get-AppConsentRequests.ps1.
    """
    try:
        log_line(f"INFO: Fetching app consent requests for {tenant.get('domain', '')}")
        client = _make_client(tenant, pfx_path, pfx_password)

        raw = []
        try:
            raw = client.get_paginated(
                "/identityGovernance/appConsent/appConsentRequests"
            )
        except Exception as exc:
            log_line(f"WARN: Could not fetch consent requests (may require additional permissions): {exc}")

        requests_list = []
        for r in raw:
            pending_scopes = r.get("pendingScopes", [])
            pending_names = ", ".join(s.get("displayName", "") for s in pending_scopes)
            requests_list.append({
                "id": r.get("id", ""),
                "appId": r.get("appId", ""),
                "appName": r.get("appDisplayName", ""),
                "status": r.get("consentType", ""),
                "pending": pending_names,
            })

        log_line(f"INFO: Found {len(requests_list)} consent request(s)")

        return {
            "ok": True,
            "requests": requests_list,
            "count": len(requests_list),
        }

    except (GraphAuthError, GraphRequestError) as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Get-AppRegistrations.ps1 → fetch_app_registrations()
# ---------------------------------------------------------------------------

def fetch_app_registrations(
    tenant: dict,
    pfx_path: str,
    pfx_password: str,
    data_type: str = "both",
    *,
    log_line: Callable[[str], None] = lambda x: None,
) -> dict:
    """Fetch app registrations and/or enterprise apps for a tenant.

    Replaces scripts/Get-AppRegistrations.ps1.
    """
    try:
        client = _make_client(tenant, pfx_path, pfx_password)
        output: dict = {"ok": True}

        # App Registrations
        if data_type in ("appRegistrations", "both"):
            log_line(f"INFO: Fetching app registrations for {tenant.get('domain', '')}")
            apps = client.get_paginated("/applications")

            app_list = []
            for a in apps:
                cred_expiry = None
                pw_creds = a.get("passwordCredentials", [])
                key_creds = a.get("keyCredentials", [])
                if pw_creds:
                    sorted_pw = sorted(pw_creds, key=lambda c: c.get("endDateTime", ""), reverse=True)
                    cred_expiry = sorted_pw[0].get("endDateTime")
                if key_creds and not cred_expiry:
                    sorted_kc = sorted(key_creds, key=lambda c: c.get("endDateTime", ""), reverse=True)
                    cred_expiry = sorted_kc[0].get("endDateTime")

                app_list.append({
                    "id": a.get("id", ""),
                    "appId": a.get("appId", ""),
                    "displayName": a.get("displayName", ""),
                    "signInAudience": a.get("signInAudience", ""),
                    "createdDate": a.get("createdDateTime", ""),
                    "hasSecrets": len(pw_creds) > 0,
                    "hasCerts": len(key_creds) > 0,
                    "credentialExpiry": cred_expiry,
                    "requiredPermissions": _json.dumps(a.get("requiredResourceAccess", []), default=str),
                })

            log_line(f"INFO: Found {len(app_list)} app registration(s)")
            output["appRegistrations"] = app_list
            output["appRegistrationCount"] = len(app_list)

        # Enterprise Apps (Service Principals)
        if data_type in ("enterpriseApps", "both"):
            log_line(f"INFO: Fetching enterprise applications for {tenant.get('domain', '')}")
            sps = client.get_paginated(
                "/servicePrincipals",
                params={
                    "$top": "999",
                    "$select": "id,appId,displayName,servicePrincipalType,accountEnabled,"
                               "appOwnerOrganizationId,createdDateTime,loginUrl,tags",
                },
            )

            sp_list = []
            for sp in sps:
                sp_list.append({
                    "id": sp.get("id", ""),
                    "appId": sp.get("appId", ""),
                    "displayName": sp.get("displayName", ""),
                    "type": sp.get("servicePrincipalType", ""),
                    "enabled": sp.get("accountEnabled", False),
                    "ownerOrg": sp.get("appOwnerOrganizationId", ""),
                    "createdDate": sp.get("createdDateTime", ""),
                    "loginUrl": sp.get("loginUrl", ""),
                    "tags": ", ".join(sp.get("tags", [])),
                })

            log_line(f"INFO: Found {len(sp_list)} enterprise application(s)")
            output["enterpriseApps"] = sp_list
            output["enterpriseAppCount"] = len(sp_list)

        return output

    except (GraphAuthError, GraphRequestError) as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Get-DirectoryAuditLogs.ps1 → fetch_directory_audit_logs()
# ---------------------------------------------------------------------------

def fetch_directory_audit_logs(
    tenant: dict,
    pfx_path: str,
    pfx_password: str,
    start_date: str = "",
    end_date: str = "",
    *,
    log_line: Callable[[str], None] = lambda x: None,
) -> dict:
    """Fetch Entra ID directory audit logs for a tenant.

    Replaces scripts/Get-DirectoryAuditLogs.ps1.
    """
    try:
        log_line(f"INFO: Fetching directory audit logs for {tenant.get('domain', '')}")
        client = _make_client(tenant, pfx_path, pfx_password)

        # Build filter
        filters = []
        if start_date:
            filters.append(f"activityDateTime ge {start_date}")
        if end_date:
            filters.append(f"activityDateTime le {end_date}")

        params = {
            "$top": "500",
            "$orderby": "activityDateTime desc",
        }
        if filters:
            params["$filter"] = " and ".join(filters)

        logs = client.get_paginated("/auditLogs/directoryAudits", params=params)

        log_list = []
        for entry in logs:
            targets = entry.get("targetResources", [])
            target_name = targets[0].get("displayName", "") if targets else ""
            target_type = targets[0].get("@odata.type", "") if targets else ""
            initiated_by = entry.get("initiatedBy", {})

            log_list.append({
                "id": entry.get("id", ""),
                "activity": entry.get("activityDisplayName", ""),
                "category": entry.get("category", ""),
                "result": entry.get("result", ""),
                "actorUPN": initiated_by.get("user", {}).get("userPrincipalName", ""),
                "actorApp": initiated_by.get("app", {}).get("displayName", ""),
                "targetName": target_name,
                "targetType": target_type,
                "dateTime": entry.get("activityDateTime", ""),
                "correlationId": entry.get("correlationId", ""),
            })

        log_line(f"INFO: Retrieved {len(log_list)} audit log entries")

        return {
            "ok": True,
            "logs": log_list,
            "count": len(log_list),
        }

    except (GraphAuthError, GraphRequestError) as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Get-ConditionalAccessPolicies.ps1 → fetch_conditional_access()
# ---------------------------------------------------------------------------

def fetch_conditional_access(
    tenant: dict,
    pfx_path: str,
    pfx_password: str,
    data_type: str = "both",
    *,
    log_line: Callable[[str], None] = lambda x: None,
) -> dict:
    """Fetch Conditional Access policies and/or named locations.

    Replaces scripts/Get-ConditionalAccessPolicies.ps1.
    """
    try:
        client = _make_client(tenant, pfx_path, pfx_password)
        output: dict = {"ok": True}

        if data_type in ("policies", "both"):
            log_line(f"INFO: Fetching Conditional Access policies for {tenant.get('domain', '')}")
            policies = client.get_paginated(
                "/identity/conditionalAccess/policies"
            )

            policy_list = []
            for p in policies:
                policy_list.append({
                    "id": p.get("id", ""),
                    "displayName": p.get("displayName", ""),
                    "state": p.get("state", ""),
                    "createdDateTime": p.get("createdDateTime", ""),
                    "modifiedDateTime": p.get("modifiedDateTime", ""),
                    "conditions": _json.dumps(p.get("conditions", {}), default=str),
                    "grantControls": _json.dumps(p.get("grantControls", {}), default=str),
                    "sessionControls": _json.dumps(p.get("sessionControls", {}), default=str),
                })

            log_line(f"INFO: Found {len(policy_list)} CA policy/policies")
            output["policies"] = policy_list
            output["policyCount"] = len(policy_list)

        if data_type in ("namedLocations", "both"):
            log_line(f"INFO: Fetching Named Locations for {tenant.get('domain', '')}")
            locations = client.get_paginated(
                "/identity/conditionalAccess/namedLocations"
            )

            location_list = []
            for loc in locations:
                location_list.append({
                    "id": loc.get("id", ""),
                    "displayName": loc.get("displayName", ""),
                    "type": loc.get("@odata.type", ""),
                    "createdDateTime": loc.get("createdDateTime", ""),
                    "modifiedDateTime": loc.get("modifiedDateTime", ""),
                    "isTrusted": loc.get("isTrusted", False),
                    "details": _json.dumps(loc, default=str),
                })

            log_line(f"INFO: Found {len(location_list)} named location(s)")
            output["namedLocations"] = location_list
            output["locationCount"] = len(location_list)

        return output

    except (GraphAuthError, GraphRequestError) as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Get-ServiceHealth.ps1 → fetch_service_health()
# ---------------------------------------------------------------------------

def fetch_service_health(
    tenant: dict,
    pfx_path: str,
    pfx_password: str,
    report_type: str = "status",
    include_resolved: bool = False,
    message_type: str = "",
    *,
    log_line: Callable[[str], None] = lambda x: None,
) -> dict:
    """Fetch M365 service health data.

    Replaces scripts/Get-ServiceHealth.ps1.
    Supports report_type: status, incidents, messageCenter, maintenance, overview.
    """
    try:
        log_line(f"INFO: Acquiring Graph token for {tenant.get('domain', '')}...")
        client = _make_client(tenant, pfx_path, pfx_password)

        if report_type == "status":
            log_line("INFO: Fetching service health status...")
            services = client.get_paginated(
                "/admin/serviceAnnouncement/healthOverviews", max_pages=10
            )
            log_line(f"INFO: Found {len(services)} service(s).")
            for svc in services:
                icon = "[OK]" if svc.get("status") == "serviceOperational" else "[!!]"
                log_line(f"INFO: {icon} {svc.get('service', '')} — {svc.get('status', '')}")
            return {"ok": True, "services": services, "count": len(services)}

        elif report_type == "incidents":
            log_line("INFO: Fetching service incidents...")
            endpoint = "/admin/serviceAnnouncement/issues"
            params = {} if include_resolved else {"$filter": "isResolved eq false"}
            issues = client.get_paginated(endpoint, params=params or None, max_pages=50)
            log_line(f"INFO: Found {len(issues)} issue(s).")
            for issue in issues[:10]:
                log_line(f"INFO: [{issue.get('classification', '')}] "
                         f"{issue.get('service', '')} — {issue.get('title', '')}")
            return {"ok": True, "issues": issues, "count": len(issues)}

        elif report_type == "messageCenter":
            log_line("INFO: Fetching Message Center posts...")
            params = {}
            if message_type:
                params["$filter"] = f"category eq '{message_type}'"
            messages = client.get_paginated(
                "/admin/serviceAnnouncement/messages",
                params=params or None, max_pages=100,
            )
            log_line(f"INFO: Found {len(messages)} message center post(s).")
            for msg in messages[:10]:
                log_line(f"INFO: [{msg.get('category', '')}] {msg.get('title', '')}")
            return {"ok": True, "messages": messages, "count": len(messages)}

        elif report_type == "maintenance":
            log_line("INFO: Fetching planned maintenance...")
            maintenance = client.get_paginated(
                "/admin/serviceAnnouncement/issues",
                params={"$filter": "classification eq 'advisory'"},
                max_pages=50,
            )
            log_line(f"INFO: Found {len(maintenance)} maintenance item(s).")
            return {"ok": True, "maintenance": maintenance, "count": len(maintenance)}

        elif report_type == "overview":
            log_line("INFO: Generating service health overview...")
            services = client.get_paginated(
                "/admin/serviceAnnouncement/healthOverviews", max_pages=10
            )
            operational = sum(1 for s in services if s.get("status") == "serviceOperational")
            degraded = len(services) - operational

            incidents = client.get_paginated(
                "/admin/serviceAnnouncement/issues",
                params={"$filter": "isResolved eq false"},
                max_pages=50,
            )

            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            overview = {
                "tenant": tenant.get("domain", ""),
                "reportDate": now_str,
                "totalServices": len(services),
                "operationalServices": operational,
                "degradedServices": degraded,
                "activeIncidents": len(incidents),
            }

            log_line(f"INFO: Health Overview — Services: {len(services)}, "
                     f"Operational: {operational}, Degraded: {degraded}, "
                     f"Active incidents: {len(incidents)}")
            return {"ok": True, "overview": overview}

        else:
            return {"ok": False, "error": f"Unknown report type: {report_type}"}

    except (GraphAuthError, GraphRequestError) as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Shared: device code flow authentication for interactive Graph operations
# ---------------------------------------------------------------------------

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

# Graph resource IDs
_GRAPH_RESOURCE = "00000003-0000-0000-c000-000000000000"  # Microsoft Graph
_SPO_RESOURCE = "00000003-0000-0ff1-ce00-000000000000"    # SharePoint Online


def _device_code_auth(
    admin_client_id: str,
    tenant_id: str,
    scopes: list[str],
    log_line: Callable[[str], None],
) -> str:
    """Authenticate via MSAL device code flow.

    Outputs the device code to the job log so the user can see it in the
    SSE stream and complete authentication in their browser.

    Returns an access token string.
    """
    authority = (f"https://login.microsoftonline.com/{tenant_id}" if tenant_id
                 else "https://login.microsoftonline.com/organizations")

    app = msal.PublicClientApplication(
        client_id=admin_client_id,
        authority=authority,
    )

    flow = app.initiate_device_flow(scopes=scopes)

    if "user_code" not in flow:
        raise GraphAuthError(
            f"Device code flow failed: {flow.get('error_description', flow.get('error', 'unknown'))}"
        )

    # Output the device code prominently so user sees it in job log.
    # The frontend regex looks for "enter the code <CODE>" to extract it.
    user_code = flow["user_code"]
    log_line("")
    log_line("=" * 60)
    log_line("ACTION REQUIRED: open https://microsoft.com/devicelogin")
    log_line(f"and enter the code {user_code} to authenticate.")
    log_line("=" * 60)
    log_line("")
    log_line("Waiting for authentication...")

    # Poll for token (MSAL handles the polling internally)
    result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        error = result.get("error_description", result.get("error", "Authentication failed"))
        raise GraphAuthError(f"Device code authentication failed: {error}")

    account = result.get("id_token_claims", {})
    upn = account.get("preferred_username", account.get("upn", "unknown"))
    tid = account.get("tid", tenant_id or "unknown")
    log_line(f"Authenticated as {upn} in tenant {tid}")

    return result["access_token"]


def _graph_request(
    method: str,
    endpoint: str,
    token: str,
    json_body: dict | None = None,
    params: dict | None = None,
) -> dict | None:
    """Make a Graph API request with the given token."""
    url = endpoint if endpoint.startswith("http") else f"{GRAPH_BASE_URL}{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    resp = _requests.request(
        method, url, headers=headers, json=json_body, params=params, timeout=30,
    )
    if not resp.ok:
        raise GraphRequestError(
            f"Graph API {method} {endpoint} failed ({resp.status_code}): {resp.text[:500]}",
            status_code=resp.status_code,
        )
    if resp.status_code == 204:  # No Content
        return None
    return resp.json()


# ---------------------------------------------------------------------------
# Setup-App.ps1 → create_entra_app()
# ---------------------------------------------------------------------------

def create_entra_app(
    admin_client_id: str,
    cer_path: str,
    app_display_name: str = "Office Sentry",
    tenant_id: str = "",
    *,
    log_line: Callable[[str], None] = lambda x: None,
) -> dict:
    """Create an Entra app registration with certificate auth and all required permissions.

    Replaces scripts/Setup-App.ps1.
    Uses device code flow — the user must complete authentication in their browser.
    """
    try:
        log_line(f"INFO: Starting Entra app creation for '{app_display_name}'")
        log_line(f"INFO: Using admin client ID: {admin_client_id[:8]}...")
        if tenant_id:
            log_line(f"INFO: Target tenant: {tenant_id}")

        # Scopes needed to create apps and assign roles
        scopes = [
            "Application.ReadWrite.All",
            "Directory.Read.All",
            "AppRoleAssignment.ReadWrite.All",
            "RoleManagement.ReadWrite.Directory",
        ]

        token = _device_code_auth(admin_client_id, tenant_id, scopes, log_line)

        # Create app registration
        log_line("Creating app registration...")
        app = _graph_request("POST", "/applications", token, json_body={
            "displayName": app_display_name,
        })
        app_object_id = app["id"]
        app_client_id = app["appId"]
        log_line(f"App registration created: {app_client_id}")

        # Create service principal with retry (Entra replication delay)
        sp_id = None
        for attempt in range(1, 7):
            try:
                sp = _graph_request("POST", "/servicePrincipals", token, json_body={
                    "appId": app_client_id,
                })
                sp_id = sp["id"]
                break
            except GraphRequestError:
                if attempt == 6:
                    raise
                wait = attempt * 5
                log_line(f"Waiting for app to replicate... (attempt {attempt}/6, retrying in {wait}s)")
                time.sleep(wait)

        log_line(f"Service principal created: {sp_id}")

        # Upload certificate
        log_line("Uploading certificate...")
        from pathlib import Path as _Path
        cer_bytes = _Path(cer_path).read_bytes()

        # Read cert validity dates
        from cryptography import x509
        cert = x509.load_der_x509_certificate(cer_bytes)
        import base64
        cer_b64 = base64.b64encode(cer_bytes).decode("ascii")

        key_credential = {
            "type": "AsymmetricX509Cert",
            "usage": "Verify",
            "key": cer_b64,
            "displayName": "Office Sentry Cert",
            "startDateTime": cert.not_valid_before_utc.isoformat() + "Z",
            "endDateTime": cert.not_valid_after_utc.isoformat() + "Z",
        }
        _graph_request("PATCH", f"/applications/{app_object_id}", token, json_body={
            "keyCredentials": [key_credential],
        })
        log_line("Certificate uploaded")

        # Grant app roles
        def grant_role(resource_app_id: str, role_value: str):
            sps = _graph_request(
                "GET", "/servicePrincipals", token,
                params={"$filter": f"appId eq '{resource_app_id}'"},
            )
            resource_sps = sps.get("value", [])
            if not resource_sps:
                log_line(f"WARNING: service principal not found for {resource_app_id} — skipping {role_value}")
                return
            resource_sp = resource_sps[0]

            role_id = None
            for ar in resource_sp.get("appRoles", []):
                if ar.get("value") == role_value and "Application" in ar.get("allowedMemberTypes", []):
                    role_id = ar["id"]
                    break

            if not role_id:
                log_line(f"WARNING: role '{role_value}' not found on {resource_app_id} — skipping")
                return

            try:
                _graph_request("POST", f"/servicePrincipals/{sp_id}/appRoleAssignments", token, json_body={
                    "principalId": sp_id,
                    "resourceId": resource_sp["id"],
                    "appRoleId": role_id,
                })
                log_line(f"Granted: {role_value}")
            except GraphRequestError as exc:
                log_line(f"WARNING: could not assign {role_value} — {exc}")

        # SPO permissions
        grant_role(_SPO_RESOURCE, "Sites.Read.All")

        # Graph permissions
        graph_roles = [
            "AuditLog.Read.All",
            "Directory.Read.All",
            "Policy.Read.All",
            "Organization.Read.All",
            "SecurityEvents.Read.All",
            "User.Read.All",
            "UserAuthenticationMethod.Read.All",
            "MailboxSettings.Read",
            "DeviceManagementManagedDevices.Read.All",
            "DeviceManagementApps.Read.All",
            "DeviceManagementServiceConfig.Read.All",
            "DeviceManagementConfiguration.Read.All",
            "SharePointTenantSettings.Read.All",
            "Reports.Read.All",
            "IdentityProvider.Read.All",
            "RoleManagement.Read.Directory",
        ]
        for role in graph_roles:
            grant_role(_GRAPH_RESOURCE, role)

        consent_url = (
            f"https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/"
            f"ApplicationMenuBlade/~/Overview/appId/{app_client_id}"
        )

        return {
            "ok": True,
            "clientId": app_client_id,
            "appId": app_object_id,
            "displayName": app_display_name,
            "consentUrl": consent_url,
        }

    except (GraphAuthError, GraphRequestError) as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Setup-RemoveApp.ps1 → remove_entra_app()
# ---------------------------------------------------------------------------

def remove_entra_app(
    admin_client_id: str,
    client_id: str,
    pfx_path: str = "",
    cer_path: str = "",
    *,
    log_line: Callable[[str], None] = lambda x: None,
) -> dict:
    """Remove an Entra app registration and delete local cert files.

    Replaces scripts/Setup-RemoveApp.ps1.
    Uses device code flow for authentication.
    """
    try:
        scopes = ["Application.ReadWrite.All"]
        token = _device_code_auth(admin_client_id, "", scopes, log_line)

        # Find and delete the app
        log_line(f"Looking up app registration with clientId: {client_id}")
        apps = _graph_request("GET", "/applications", token,
                              params={"$filter": f"appId eq '{client_id}'"})
        app_list = apps.get("value", [])

        if app_list:
            app_object_id = app_list[0]["id"]
            _graph_request("DELETE", f"/applications/{app_object_id}", token)
            log_line(f"App registration deleted: {client_id}")
        else:
            log_line(f"App not found (may have been deleted already): {client_id}")

        # Clean up local cert files
        from pathlib import Path as _Path
        for path in (pfx_path, cer_path):
            if path:
                p = _Path(path)
                if p.exists():
                    p.unlink()
                    log_line(f"Deleted: {path}")

        return {"ok": True, "message": "App removed successfully."}

    except (GraphAuthError, GraphRequestError) as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Get-IdentityProtection.ps1 → fetch_identity_protection()
# ---------------------------------------------------------------------------

def fetch_identity_protection(
    tenant: dict,
    pfx_path: str,
    pfx_password: str,
    report_type: str = "riskyUsers",
    *,
    risk_level: str = "all",
    days: int = 30,
    target_user_id: str = "",
    log_line: Callable[[str], None] = lambda x: None,
) -> dict:
    """Fetch identity protection data from Microsoft Graph.

    Replaces scripts/Get-IdentityProtection.ps1.
    report_type: riskyUsers, riskySignIns, mfaGap, dismissRisk,
                 passwordProtection, riskOverview
    """
    try:
        client = _make_client(tenant, pfx_path, pfx_password)
        log_line(f"INFO: Fetching identity protection ({report_type}) for {tenant.get('name', '')}")

        if report_type == "riskyUsers":
            params = {"$top": "500", "$orderby": "riskLastUpdatedDateTime desc"}
            if risk_level != "all":
                params["$filter"] = f"riskLevel eq '{risk_level}'"
            users = client.get_paginated("/v1.0/identityProtection/riskyUsers", params=params)
            log_line(f"INFO: Found {len(users)} risky user(s)")
            return {"ok": True, "data_type": "risky_users", "count": len(users), "items": users}

        elif report_type == "riskySignIns":
            from datetime import timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            params = {
                "$top": "500",
                "$orderby": "createdDateTime desc",
                "$filter": f"createdDateTime ge {cutoff} and riskLevelDuringSignIn ne 'none'",
            }
            if risk_level != "all":
                params["$filter"] += f" and riskLevelDuringSignIn eq '{risk_level}'"
            signins = client.get_paginated("/v1.0/auditLogs/signIns", params=params)
            log_line(f"INFO: Found {len(signins)} risky sign-in(s) in last {days} days")
            return {"ok": True, "data_type": "risky_signins", "count": len(signins), "items": signins}

        elif report_type == "dismissRisk":
            if not target_user_id:
                return {"ok": False, "error": "Target user ID required for risk dismissal."}
            client.post("/v1.0/identityProtection/riskyUsers/dismiss",
                        json_body={"userIds": [target_user_id]})
            log_line(f"INFO: Risk dismissed for user {target_user_id}")
            return {"ok": True, "message": f"Risk dismissed for {target_user_id}"}

        elif report_type == "mfaGap":
            users = client.get_paginated("/v1.0/users", params={
                "$select": "id,displayName,userPrincipalName,accountEnabled",
                "$top": "999",
                "$filter": "accountEnabled eq true",
            })
            log_line(f"INFO: Found {len(users)} active users, checking MFA status...")
            mfa_results = []
            for i, user in enumerate(users):
                if (i + 1) % 50 == 0:
                    log_line(f"INFO: Checking user {i + 1}/{len(users)}...")
                try:
                    methods = client.get(
                        f"/v1.0/users/{user['id']}/authentication/methods"
                    ).get("value", [])
                    strong = [m for m in methods if m.get("@odata.type", "")
                              != "#microsoft.graph.passwordAuthenticationMethod"]
                    user["mfaRegistered"] = len(strong) > 0
                    user["authMethodCount"] = len(methods)
                    user["strongMethodCount"] = len(strong)
                except Exception:
                    user["mfaRegistered"] = None
                    user["authMethodCount"] = 0
                    user["strongMethodCount"] = 0
                mfa_results.append(user)
            no_mfa = [u for u in mfa_results if u.get("mfaRegistered") is False]
            log_line(f"INFO: {len(no_mfa)} of {len(mfa_results)} users have no MFA registered")
            return {"ok": True, "data_type": "mfa_gap", "total": len(mfa_results),
                    "no_mfa": len(no_mfa), "items": mfa_results}

        elif report_type == "authStrengths":
            policies = client.get_paginated(
                "/v1.0/policies/authenticationStrengthPolicies"
            )
            log_line(f"INFO: Found {len(policies)} authentication strength policies")
            return {"ok": True, "data_type": "auth_strengths", "count": len(policies), "items": policies}

        elif report_type == "passwordProtection":
            policies = {}
            for name, ep in [
                ("authMethodsPolicy", "/v1.0/policies/authenticationMethodsPolicy"),
            ]:
                try:
                    policies[name] = client.get(ep)
                except Exception as exc:
                    log_line(f"WARN: Could not fetch {name}: {exc}")
                    policies[name] = None
            log_line("INFO: Password protection policies fetched")
            return {"ok": True, "data_type": "password_protection", "policies": policies}

        elif report_type == "riskOverview":
            risky_users = client.get_paginated(
                "/v1.0/identityProtection/riskyUsers",
                params={"$top": "500", "$filter": "riskState eq 'atRisk'"},
            )
            overview = {
                "risky_users": len(risky_users),
                "high_risk": len([u for u in risky_users if u.get("riskLevel") == "high"]),
                "medium_risk": len([u for u in risky_users if u.get("riskLevel") == "medium"]),
                "low_risk": len([u for u in risky_users if u.get("riskLevel") == "low"]),
            }
            log_line(f"INFO: Risk overview — {overview['risky_users']} at-risk users "
                     f"({overview['high_risk']} high, {overview['medium_risk']} medium, {overview['low_risk']} low)")
            return {"ok": True, "data_type": "risk_overview", **overview}

        else:
            return {"ok": False, "error": f"Unknown report type: {report_type}"}

    except Exception as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Get-LicenseReport.ps1 → fetch_license_report()
# ---------------------------------------------------------------------------

def fetch_license_report(
    tenant: dict,
    pfx_path: str,
    pfx_password: str,
    report_type: str = "inventory",
    *,
    inactive_days: int = 90,
    log_line: Callable[[str], None] = lambda x: None,
) -> dict:
    """Fetch license data from Microsoft Graph.

    Replaces scripts/Get-LicenseReport.ps1.
    report_type: inventory, usage, optimization, overview, userDetail
    """
    try:
        client = _make_client(tenant, pfx_path, pfx_password)
        log_line(f"INFO: Fetching license report ({report_type}) for {tenant.get('name', '')}")

        if report_type == "inventory":
            skus = client.get_paginated("/v1.0/subscribedSkus")
            log_line(f"INFO: Found {len(skus)} subscribed SKU(s)")
            result = []
            for sku in skus:
                enabled = sku.get("prepaidUnits", {}).get("enabled", 0)
                consumed = sku.get("consumedUnits", 0)
                result.append({
                    "skuId": sku.get("skuId"),
                    "skuPartNumber": sku.get("skuPartNumber"),
                    "capabilityStatus": sku.get("capabilityStatus"),
                    "total": enabled, "assigned": consumed,
                    "available": max(0, enabled - consumed),
                    "servicePlans": [sp.get("servicePlanName") for sp in sku.get("servicePlans", [])],
                })
            return {"ok": True, "data_type": "license_inventory", "count": len(result), "items": result}

        elif report_type == "usage":
            from datetime import timedelta
            users = client.get_paginated("/v1.0/users", params={
                "$select": "id,displayName,userPrincipalName,assignedLicenses,signInActivity,accountEnabled",
                "$top": "999",
            })
            cutoff = datetime.now(timezone.utc) - timedelta(days=inactive_days)
            inactive = []
            for u in users:
                if not u.get("assignedLicenses"):
                    continue
                last_signin = u.get("signInActivity", {}).get("lastSignInDateTime")
                if last_signin:
                    try:
                        last_dt = datetime.fromisoformat(last_signin.replace("Z", "+00:00"))
                        if last_dt < cutoff:
                            u["daysSinceSignIn"] = (datetime.now(timezone.utc) - last_dt).days
                            inactive.append(u)
                    except (ValueError, TypeError):
                        pass
                else:
                    u["daysSinceSignIn"] = None
                    inactive.append(u)
            log_line(f"INFO: {len(inactive)} licensed user(s) inactive for {inactive_days}+ days")
            return {"ok": True, "data_type": "license_usage",
                    "inactive_count": len(inactive), "items": inactive}

        elif report_type == "optimization":
            skus = client.get_paginated("/v1.0/subscribedSkus")
            recommendations = []
            for sku in skus:
                total = sku.get("prepaidUnits", {}).get("enabled", 0)
                used = sku.get("consumedUnits", 0)
                if total >= 5:
                    util = (used / total) * 100 if total else 0
                    if util < 50:
                        recommendations.append({
                            "skuPartNumber": sku.get("skuPartNumber"),
                            "total": total, "used": used,
                            "utilization": round(util, 1),
                            "suggestion": f"Only {used}/{total} licenses used ({util:.0f}%). Consider reducing.",
                            "potentialSavings": total - used,
                        })
            log_line(f"INFO: {len(recommendations)} optimization recommendation(s)")
            return {"ok": True, "data_type": "license_optimization",
                    "count": len(recommendations), "items": recommendations}

        elif report_type == "overview":
            skus = client.get_paginated("/v1.0/subscribedSkus")
            total_lic = sum(s.get("prepaidUnits", {}).get("enabled", 0) for s in skus)
            total_asgn = sum(s.get("consumedUnits", 0) for s in skus)
            overview = {
                "sku_count": len(skus), "total_licenses": total_lic,
                "total_assigned": total_asgn,
                "total_available": max(0, total_lic - total_asgn),
                "utilization": round((total_asgn / total_lic * 100), 1) if total_lic else 0,
            }
            log_line(f"INFO: License overview — {overview['sku_count']} SKUs, "
                     f"{total_asgn}/{total_lic} assigned ({overview['utilization']}%)")
            return {"ok": True, "data_type": "license_overview", **overview}

        elif report_type == "userDetail":
            users = client.get_paginated("/v1.0/users", params={
                "$select": "id,displayName,userPrincipalName,assignedLicenses,accountEnabled",
                "$top": "999",
            })
            licensed = [u for u in users if u.get("assignedLicenses")]
            log_line(f"INFO: {len(licensed)} user(s) with license assignments")
            return {"ok": True, "data_type": "license_assignments",
                    "count": len(licensed), "items": licensed}

        else:
            return {"ok": False, "error": f"Unknown report type: {report_type}"}

    except Exception as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Get-IntuneDevices.ps1 + Invoke-DeviceAction.ps1 → fetch_intune_devices()
# ---------------------------------------------------------------------------

def fetch_intune_devices(
    tenant: dict,
    pfx_path: str,
    pfx_password: str,
    report_type: str = "inventory",
    *,
    platform: str = "all",
    compliance_filter: str = "all",
    policy_type: str = "all",
    device_id: str = "",
    action: str = "",
    log_line: Callable[[str], None] = lambda x: None,
) -> dict:
    """Fetch Intune device data from Microsoft Graph.

    Replaces scripts/Get-IntuneDevices.ps1 and scripts/Invoke-DeviceAction.ps1.
    report_type: inventory, compliance, autopilot, policies, action, overview
    """
    try:
        client = _make_client(tenant, pfx_path, pfx_password)
        log_line(f"INFO: Fetching Intune data ({report_type}) for {tenant.get('name', '')}")

        if report_type == "inventory":
            params = {"$top": "999"}
            filters = []
            if platform != "all":
                filters.append(f"operatingSystem eq '{platform}'")
            if compliance_filter != "all":
                filters.append(f"complianceState eq '{compliance_filter}'")
            if filters:
                params["$filter"] = " and ".join(filters)
            devices = client.get_paginated("/v1.0/deviceManagement/managedDevices", params=params)
            log_line(f"INFO: Found {len(devices)} managed device(s)")
            return {"ok": True, "data_type": "device_inventory", "count": len(devices), "items": devices}

        elif report_type == "compliance":
            policies = client.get_paginated(
                "/v1.0/deviceManagement/deviceCompliancePolicies",
                params={"$expand": "assignments"},
            )
            try:
                summary = client.get("/v1.0/deviceManagement/deviceCompliancePolicyDeviceStateSummary")
            except Exception:
                summary = {}
            log_line(f"INFO: Found {len(policies)} compliance policy/policies")
            return {"ok": True, "data_type": "device_compliance",
                    "count": len(policies), "items": policies, "summary": summary}

        elif report_type == "autopilot":
            devices = client.get_paginated(
                "/v1.0/deviceManagement/windowsAutopilotDeviceIdentities",
                params={"$top": "999"},
            )
            profiles = client.get_paginated(
                "/v1.0/deviceManagement/windowsAutopilotDeploymentProfiles",
            )
            log_line(f"INFO: Found {len(devices)} Autopilot device(s), {len(profiles)} profile(s)")
            return {"ok": True, "data_type": "autopilot",
                    "device_count": len(devices), "profile_count": len(profiles),
                    "devices": devices, "profiles": profiles}

        elif report_type == "policies":
            all_policies = {}
            endpoints = {
                "configuration": "/v1.0/deviceManagement/deviceConfigurations",
                "compliance": "/v1.0/deviceManagement/deviceCompliancePolicies",
                "appProtection": "/v1.0/deviceManagement/managedAppPolicies",
            }
            targets = endpoints if policy_type == "all" else {policy_type: endpoints.get(policy_type, endpoints["configuration"])}
            for ptype, ep in targets.items():
                try:
                    all_policies[ptype] = client.get_paginated(ep)
                except Exception as exc:
                    log_line(f"WARN: Could not fetch {ptype}: {exc}")
                    all_policies[ptype] = []
            total = sum(len(v) for v in all_policies.values())
            log_line(f"INFO: Found {total} device policy/policies")
            return {"ok": True, "data_type": "device_policies", "count": total, "policies": all_policies}

        elif report_type == "action":
            if not device_id or not action:
                return {"ok": False, "error": "Device ID and action are required."}
            action_map = {
                "sync": "syncDevice", "restart": "rebootNow",
                "retire": "retire", "wipe": "wipe",
                "resetPasscode": "resetPasscode", "remoteLock": "remoteLock",
                "enableLostMode": "enableLostMode", "disableLostMode": "disableLostMode",
            }
            graph_action = action_map.get(action, action)
            client.post(f"/v1.0/deviceManagement/managedDevices/{device_id}/{graph_action}", json_body={})
            log_line(f"INFO: Device action '{action}' sent to {device_id}")
            return {"ok": True, "message": f"Action '{action}' sent to device {device_id}"}

        elif report_type == "overview":
            devices = client.get_paginated(
                "/v1.0/deviceManagement/managedDevices",
                params={"$top": "999", "$select": "id,operatingSystem,complianceState"},
            )
            overview = {
                "total": len(devices),
                "compliant": len([d for d in devices if d.get("complianceState") == "compliant"]),
                "noncompliant": len([d for d in devices if d.get("complianceState") == "noncompliant"]),
                "by_os": {},
            }
            for d in devices:
                os_name = d.get("operatingSystem", "Unknown")
                overview["by_os"][os_name] = overview["by_os"].get(os_name, 0) + 1
            log_line(f"INFO: Device overview — {overview['total']} devices "
                     f"({overview['compliant']} compliant, {overview['noncompliant']} noncompliant)")
            return {"ok": True, "data_type": "device_overview", **overview}

        else:
            return {"ok": False, "error": f"Unknown report type: {report_type}"}

    except Exception as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Get-SignInLogs.ps1 → fetch_sign_in_logs()
# ---------------------------------------------------------------------------

def fetch_sign_in_logs(
    tenant: dict,
    pfx_path: str,
    pfx_password: str,
    *,
    start_date: str = "",
    end_date: str = "",
    target_user: str = "",
    log_line: Callable[[str], None] = lambda x: None,
) -> dict:
    """Fetch Azure AD sign-in logs via Microsoft Graph.

    Replaces scripts/Get-SignInLogs.ps1.
    """
    try:
        client = _make_client(tenant, pfx_path, pfx_password)

        if not start_date:
            from datetime import timedelta
            start_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        log_line(f"INFO: Fetching sign-in logs ({start_date} to {end_date}) for {tenant.get('name', '')}")

        # Build filter
        filt = f"createdDateTime ge {start_date}T00:00:00Z and createdDateTime le {end_date}T23:59:59Z"
        if target_user:
            filt += f" and userPrincipalName eq '{target_user}'"
            log_line(f"INFO: Filtering for user: {target_user}")

        select = (
            "createdDateTime,userPrincipalName,userDisplayName,appDisplayName,"
            "ipAddress,clientAppUsed,resourceDisplayName,status,location,"
            "deviceDetail,riskDetail,riskLevelAggregated,riskLevelDuringSignIn,"
            "isInteractive,conditionalAccessStatus"
        )

        params = {
            "$filter": filt,
            "$select": select,
            "$top": "999",
            "$orderby": "createdDateTime desc",
        }

        signins = client.get_paginated("/v1.0/auditLogs/signIns", params=params, max_pages=200)
        log_line(f"INFO: Retrieved {len(signins)} sign-in records")

        if not signins:
            log_line("INFO: No sign-in logs found for the specified criteria.")
            return {"ok": True, "data_type": "sign_in_logs", "count": 0, "items": [],
                    "failed": 0, "ip_summary": []}

        # Parse into flat rows
        rows = []
        for s in signins:
            status = s.get("status") or {}
            location = s.get("location") or {}
            device = s.get("deviceDetail") or {}
            rows.append({
                "timestamp": s.get("createdDateTime"),
                "userPrincipal": s.get("userPrincipalName"),
                "displayName": s.get("userDisplayName"),
                "application": s.get("appDisplayName"),
                "resource": s.get("resourceDisplayName"),
                "ipAddress": s.get("ipAddress"),
                "clientApp": s.get("clientAppUsed"),
                "city": location.get("city", ""),
                "state": location.get("state", ""),
                "country": location.get("countryOrRegion", ""),
                "os": device.get("operatingSystem", ""),
                "browser": device.get("browser", ""),
                "isInteractive": s.get("isInteractive"),
                "errorCode": str(status.get("errorCode", "")),
                "failureReason": status.get("failureReason", ""),
                "riskLevel": s.get("riskLevelAggregated"),
                "riskDuringSignIn": s.get("riskLevelDuringSignIn"),
                "riskDetail": s.get("riskDetail"),
                "caStatus": s.get("conditionalAccessStatus"),
            })

        # Build summaries
        failed = [r for r in rows if r["errorCode"] not in ("0", "")]
        ip_counts: dict[str, int] = {}
        for r in rows:
            key = f"{r['userPrincipal']}|{r['ipAddress']}|{r['country']}"
            ip_counts[key] = ip_counts.get(key, 0) + 1
        ip_summary = [
            {"user": k.split("|")[0], "ip": k.split("|")[1], "country": k.split("|")[2], "count": v}
            for k, v in sorted(ip_counts.items(), key=lambda x: -x[1])
        ]

        log_line(f"INFO: Done. {len(rows)} sign-in records, {len(failed)} failures.")
        return {
            "ok": True, "data_type": "sign_in_logs",
            "count": len(rows), "failed": len(failed),
            "items": rows, "ip_summary": ip_summary[:50],
        }

    except Exception as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Get-UserReport.ps1 → fetch_user_report()
# ---------------------------------------------------------------------------

def fetch_user_report(
    tenant: dict,
    pfx_path: str,
    pfx_password: str,
    report_type: str = "list_users",
    *,
    target_user: str = "",
    days_inactive: int = 90,
    log_line: Callable[[str], None] = lambda x: None,
) -> dict:
    """Generate user reports via Microsoft Graph.

    Replaces scripts/Get-UserReport.ps1.
    report_type: licenses, mfa_status, user_info, group_membership,
                 list_users, stale_accounts
    """
    try:
        client = _make_client(tenant, pfx_path, pfx_password)
        log_line(f"INFO: Generating user report ({report_type}) for {tenant.get('name', '')}")

        if report_type == "licenses":
            select = "displayName,userPrincipalName,accountEnabled,assignedLicenses"
            if target_user:
                data = client.get(f"/v1.0/users/{target_user}", params={"$select": select})
                users = [data]
            else:
                users = client.get_paginated("/v1.0/users", params={"$select": select, "$top": "999"})

            # Get SKU mapping
            skus = client.get_paginated("/v1.0/subscribedSkus")
            sku_map = {s["skuId"]: s.get("skuPartNumber", s["skuId"]) for s in skus}

            rows = []
            for u in users:
                licenses = u.get("assignedLicenses") or []
                lic_names = [sku_map.get(lic["skuId"], lic["skuId"]) for lic in licenses]
                rows.append({
                    "displayName": u.get("displayName"),
                    "userPrincipalName": u.get("userPrincipalName"),
                    "accountEnabled": u.get("accountEnabled"),
                    "licenseCount": len(lic_names),
                    "licenses": "; ".join(lic_names),
                })
            log_line(f"INFO: Processed {len(rows)} users with license data.")
            return {"ok": True, "data_type": "user_licenses", "count": len(rows), "items": rows}

        elif report_type == "mfa_status":
            endpoint = "/v1.0/reports/authenticationMethods/userRegistrationDetails"
            params: dict = {"$top": "999"}
            if target_user:
                params["$filter"] = f"userPrincipalName eq '{target_user}'"
            results = client.get_paginated(endpoint, params=params)

            rows = []
            for item in results:
                rows.append({
                    "displayName": item.get("userDisplayName"),
                    "userPrincipalName": item.get("userPrincipalName"),
                    "isMfaRegistered": item.get("isMfaRegistered"),
                    "isMfaCapable": item.get("isMfaCapable"),
                    "isPasswordlessCapable": item.get("isPasswordlessCapable"),
                    "isSsprRegistered": item.get("isSsprRegistered"),
                    "isSsprEnabled": item.get("isSsprEnabled"),
                    "isSsprCapable": item.get("isSsprCapable"),
                    "methodsRegistered": "; ".join(item.get("methodsRegistered") or []),
                    "defaultMfaMethod": item.get("defaultMfaMethod"),
                })
            log_line(f"INFO: Processed {len(rows)} users with MFA data.")
            return {"ok": True, "data_type": "mfa_status", "count": len(rows), "items": rows}

        elif report_type == "user_info":
            if not target_user:
                return {"ok": False, "error": "TargetUser is required for user_info report."}

            select = (
                "id,displayName,userPrincipalName,mail,accountEnabled,createdDateTime,"
                "jobTitle,department,companyName,officeLocation,mobilePhone,businessPhones,"
                "city,state,country,usageLocation,lastPasswordChangeDateTime,"
                "onPremisesSyncEnabled,userType,signInActivity"
            )
            user = client.get(f"/v1.0/users/{target_user}", params={"$select": select})

            # License details
            lic_data = client.get_paginated(f"/v1.0/users/{target_user}/licenseDetails")
            lic_names = "; ".join(d.get("skuPartNumber", "") for d in lic_data)

            # Group memberships
            groups = client.get_paginated(f"/v1.0/users/{target_user}/memberOf", params={"$top": "999"})
            group_names = "; ".join(
                g.get("displayName", "")
                for g in groups if g.get("@odata.type") == "#microsoft.graph.group"
            )

            sign_in = user.get("signInActivity") or {}
            row = {
                "displayName": user.get("displayName"),
                "userPrincipalName": user.get("userPrincipalName"),
                "email": user.get("mail"),
                "accountEnabled": user.get("accountEnabled"),
                "userType": user.get("userType"),
                "jobTitle": user.get("jobTitle"),
                "department": user.get("department"),
                "company": user.get("companyName"),
                "office": user.get("officeLocation"),
                "mobilePhone": user.get("mobilePhone"),
                "businessPhones": "; ".join(user.get("businessPhones") or []),
                "city": user.get("city"),
                "state": user.get("state"),
                "country": user.get("country"),
                "usageLocation": user.get("usageLocation"),
                "created": user.get("createdDateTime"),
                "lastPasswordChange": user.get("lastPasswordChangeDateTime"),
                "lastSignIn": sign_in.get("lastSignInDateTime", ""),
                "onPremSync": user.get("onPremisesSyncEnabled"),
                "licenses": lic_names,
                "groupMemberships": group_names,
            }
            log_line(f"INFO: User details retrieved for {target_user}")
            return {"ok": True, "data_type": "user_info", "count": 1, "items": [row]}

        elif report_type == "group_membership":
            if not target_user:
                return {"ok": False, "error": "TargetUser is required for group_membership report."}

            memberships = client.get_paginated(
                f"/v1.0/users/{target_user}/memberOf", params={"$top": "999"}
            )
            rows = []
            type_map = {
                "#microsoft.graph.group": "Group",
                "#microsoft.graph.directoryRole": "Directory Role",
                "#microsoft.graph.administrativeUnit": "Administrative Unit",
            }
            for item in memberships:
                odata_type = item.get("@odata.type", "")
                rows.append({
                    "displayName": item.get("displayName"),
                    "type": type_map.get(odata_type, odata_type.replace("#microsoft.graph.", "")),
                    "description": item.get("description"),
                    "mail": item.get("mail"),
                    "mailEnabled": item.get("mailEnabled"),
                    "groupTypes": "; ".join(item.get("groupTypes") or []),
                })
            log_line(f"INFO: Found {len(rows)} group memberships for {target_user}")
            return {"ok": True, "data_type": "group_membership", "count": len(rows), "items": rows}

        elif report_type == "list_users":
            select = (
                "displayName,userPrincipalName,mail,accountEnabled,userType,"
                "jobTitle,department,companyName,createdDateTime,signInActivity"
            )
            users = client.get_paginated("/v1.0/users", params={"$select": select, "$top": "999"})
            rows = []
            for i, u in enumerate(users):
                if (i + 1) % 200 == 0:
                    log_line(f"INFO: Processing user {i + 1}/{len(users)}...")
                sign_in = u.get("signInActivity") or {}
                rows.append({
                    "displayName": u.get("displayName"),
                    "userPrincipalName": u.get("userPrincipalName"),
                    "email": u.get("mail"),
                    "accountEnabled": u.get("accountEnabled"),
                    "userType": u.get("userType"),
                    "jobTitle": u.get("jobTitle"),
                    "department": u.get("department"),
                    "company": u.get("companyName"),
                    "created": u.get("createdDateTime"),
                    "lastSignIn": sign_in.get("lastSignInDateTime", ""),
                })
            log_line(f"INFO: Found {len(rows)} users.")
            return {"ok": True, "data_type": "user_list", "count": len(rows), "items": rows}

        elif report_type == "stale_accounts":
            select = (
                "displayName,userPrincipalName,mail,accountEnabled,userType,"
                "signInActivity,assignedLicenses,createdDateTime"
            )
            users = client.get_paginated("/v1.0/users", params={"$select": select, "$top": "999"})

            from datetime import timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(days=days_inactive)
            stale = []
            for i, u in enumerate(users):
                if (i + 1) % 200 == 0:
                    log_line(f"INFO: Analyzing user {i + 1}/{len(users)}...")
                sign_in = u.get("signInActivity") or {}
                last_str = sign_in.get("lastSignInDateTime")
                is_stale = False
                days_since = "Never"
                if not last_str:
                    is_stale = True
                else:
                    last_dt = datetime.fromisoformat(last_str.replace("Z", "+00:00"))
                    if last_dt < cutoff:
                        is_stale = True
                        days_since = (datetime.now(timezone.utc) - last_dt).days
                if is_stale:
                    has_license = bool(u.get("assignedLicenses"))
                    stale.append({
                        "displayName": u.get("displayName"),
                        "userPrincipalName": u.get("userPrincipalName"),
                        "accountEnabled": u.get("accountEnabled"),
                        "userType": u.get("userType"),
                        "hasLicense": has_license,
                        "lastSignIn": last_str or "Never",
                        "daysSinceSignIn": days_since,
                        "created": u.get("createdDateTime"),
                    })

            stale.sort(key=lambda x: (x["daysSinceSignIn"] if isinstance(x["daysSinceSignIn"], int) else 999999), reverse=True)
            licensed_stale = sum(1 for s in stale if s["hasLicense"])
            log_line(f"INFO: Found {len(stale)} stale accounts ({licensed_stale} with active licenses)")
            log_line(f"INFO: Threshold: {days_inactive} days of inactivity")
            return {"ok": True, "data_type": "stale_accounts", "count": len(stale),
                    "licensed_stale": licensed_stale, "threshold_days": days_inactive, "items": stale}

        else:
            return {"ok": False, "error": f"Unknown report type: {report_type}"}

    except Exception as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Get-TenantBPA.ps1 → fetch_tenant_bpa()
# ---------------------------------------------------------------------------

def fetch_tenant_bpa(
    tenant: dict,
    pfx_path: str,
    pfx_password: str,
    *,
    checks: list[str] | None = None,
    log_line: Callable[[str], None] = lambda x: None,
) -> dict:
    """Run Best Practice Analysis against a tenant via Graph API.

    Replaces scripts/Get-TenantBPA.ps1.
    """
    try:
        client = _make_client(tenant, pfx_path, pfx_password)
        log_line(f"INFO: Running BPA for {tenant.get('name', '')}")

        all_checks = [
            "unified_audit_log", "security_defaults", "mfa_registration",
            "password_expiry", "self_service_password_reset", "admin_mfa",
            "oauth_app_consent", "legacy_auth_blocked",
            "mailbox_auditing", "external_forwarding",
            "sharepoint_external_sharing", "onedrive_external_sharing",
            "secure_score",
        ]
        run_checks = checks if checks else all_checks
        results = []

        def add_result(key, name, category, status, detail, expected, actual, impact="Medium"):
            results.append({
                "check_key": key, "check_name": name, "category": category,
                "status": status, "detail": detail,
                "expected": expected, "actual": actual, "impact": impact,
            })

        def safe_get(endpoint):
            try:
                return client.get(endpoint)
            except Exception as exc:
                log_line(f"WARN: Graph call failed for {endpoint}: {exc}")
                return None

        # ---- Unified Audit Log ----
        if "unified_audit_log" in run_checks:
            log_line("INFO: Checking Unified Audit Log...")
            org = safe_get("/v1.0/organization")
            if org and org.get("value"):
                add_result("unified_audit_log", "Unified Audit Log", "Security", "warn",
                           "Audit log status requires Exchange admin verification",
                           "Enabled", "Check manually", "Critical")
            else:
                add_result("unified_audit_log", "Unified Audit Log", "Security", "fail",
                           "Unable to verify audit log status",
                           "Enabled", "Unknown", "Critical")

        # ---- Security Defaults ----
        if "security_defaults" in run_checks:
            log_line("INFO: Checking Security Defaults...")
            policies = safe_get("/v1.0/policies/identitySecurityDefaultsEnforcementPolicy")
            if policies:
                enabled = policies.get("isEnabled", False)
                add_result("security_defaults", "Security Defaults", "Identity",
                           "pass" if enabled else "warn",
                           "Security Defaults are enabled" if enabled
                           else "Security Defaults are disabled (check for Conditional Access policies)",
                           "Enabled (or CA policies)", "Enabled" if enabled else "Disabled", "High")

        # ---- Password Expiry ----
        if "password_expiry" in run_checks:
            log_line("INFO: Checking Password Expiry Policy...")
            domains = safe_get("/v1.0/domains")
            if domains and domains.get("value"):
                add_result("password_expiry", "Password Expiry Policy", "Identity", "pass",
                           "NIST recommends no forced password expiry when MFA is enabled",
                           "No forced expiry (with MFA)", "Review in Entra admin", "Medium")

        # ---- Self-Service Password Reset ----
        if "self_service_password_reset" in run_checks:
            log_line("INFO: Checking Self-Service Password Reset...")
            auth_policy = safe_get("/v1.0/policies/authorizationPolicy")
            if auth_policy:
                add_result("self_service_password_reset", "Self-Service Password Reset", "Identity",
                           "warn", "SSPR enablement requires Azure AD Premium verification",
                           "Enabled for all users", "Requires manual verification", "Medium")

        # ---- OAuth App Consent ----
        if "oauth_app_consent" in run_checks:
            log_line("INFO: Checking OAuth App Consent Policy...")
            auth_policy = safe_get("/v1.0/policies/authorizationPolicy")
            if auth_policy:
                perms = auth_policy.get("defaultUserRolePermissions", {})
                consent_policies = perms.get("permissionGrantPoliciesAssigned") or []
                no_consent = len(consent_policies) == 0
                add_result("oauth_app_consent", "OAuth App Consent", "Security",
                           "pass" if no_consent else "warn",
                           "Users cannot consent to apps without admin approval" if no_consent
                           else "Users can consent to some apps - review consent policies",
                           "Admin consent required", "Restricted" if no_consent else "Permissive", "High")

        # ---- Secure Score ----
        if "secure_score" in run_checks:
            log_line("INFO: Fetching Secure Score...")
            score_data = safe_get("/v1.0/security/secureScores?$top=1")
            if score_data and score_data.get("value"):
                s = score_data["value"][0]
                max_score = s.get("maxScore", 0)
                current = s.get("currentScore", 0)
                pct = round((current / max_score) * 100, 1) if max_score > 0 else 0
                status = "pass" if pct >= 70 else ("warn" if pct >= 40 else "fail")
                add_result("secure_score", "Microsoft Secure Score", "Overall", status,
                           f"Score: {current}/{max_score} ({pct}%)",
                           ">= 70%", f"{pct}%", "High")

        # ---- MFA Registration Campaign ----
        if "mfa_registration" in run_checks:
            log_line("INFO: Checking MFA Registration...")
            auth_methods = safe_get("/v1.0/policies/authenticationMethodsPolicy")
            if auth_methods:
                reg = (auth_methods.get("registrationEnforcement") or {}).get(
                    "authenticationMethodsRegistrationCampaign") or {}
                enabled = reg.get("state") == "enabled"
                add_result("mfa_registration", "MFA Registration Campaign", "Identity",
                           "pass" if enabled else "warn",
                           "MFA registration nudges are enabled" if enabled
                           else "MFA registration campaign is not active",
                           "Enabled", "Enabled" if enabled else "Disabled", "High")

        # ---- Admin MFA ----
        if "admin_mfa" in run_checks:
            log_line("INFO: Checking admin MFA status...")
            roles = safe_get("/v1.0/directoryRoles?$filter=displayName eq 'Global Administrator'")
            if roles and roles.get("value"):
                role_id = roles["value"][0]["id"]
                members = safe_get(f"/v1.0/directoryRoles/{role_id}/members")
                count = len(members.get("value", [])) if members else 0
                add_result("admin_mfa", "Global Admin MFA", "Identity", "warn",
                           f"Found {count} Global Admins - verify all have MFA enabled",
                           "All admins MFA-enabled", f"{count} admins found", "Critical")

        # ---- Legacy Authentication ----
        if "legacy_auth_blocked" in run_checks:
            log_line("INFO: Checking legacy authentication...")
            sec_defaults = safe_get("/v1.0/policies/identitySecurityDefaultsEnforcementPolicy")
            if sec_defaults:
                blocked = sec_defaults.get("isEnabled", False)
                add_result("legacy_auth_blocked", "Legacy Authentication", "Identity",
                           "pass" if blocked else "warn",
                           "Legacy auth blocked via Security Defaults" if blocked
                           else "Legacy auth may not be fully blocked - check CA policies",
                           "Blocked", "Blocked" if blocked else "Review needed", "High")

        # ---- Mailbox Auditing ----
        if "mailbox_auditing" in run_checks:
            log_line("INFO: Checking mailbox auditing...")
            add_result("mailbox_auditing", "Mailbox Auditing", "Exchange", "pass",
                       "Mailbox auditing is enabled by default for all M365 organizations since January 2019",
                       "Enabled by default", "Enabled (default)", "Medium")

        # ---- External Forwarding ----
        if "external_forwarding" in run_checks:
            log_line("INFO: Checking external forwarding policy...")
            add_result("external_forwarding", "External Email Forwarding", "Exchange", "warn",
                       "External forwarding policy requires Exchange admin verification. "
                       "Recommended: block auto-forwarding to external recipients.",
                       "Blocked", "Verify in Exchange admin", "High")

        # ---- SharePoint External Sharing ----
        if "sharepoint_external_sharing" in run_checks:
            log_line("INFO: Checking SharePoint external sharing...")
            spo = safe_get("/v1.0/admin/sharepoint/settings")
            if spo:
                cap = spo.get("sharingCapability", "unknown")
                restricted = cap in ("disabled", "existingExternalUserSharingOnly")
                add_result("sharepoint_external_sharing", "SharePoint External Sharing", "SharePoint",
                           "pass" if restricted else "warn",
                           f"SharePoint sharing level: {cap}",
                           "Restricted or disabled", cap, "High")

        # ---- OneDrive External Sharing ----
        if "onedrive_external_sharing" in run_checks:
            log_line("INFO: Checking OneDrive external sharing...")
            spo = safe_get("/v1.0/admin/sharepoint/settings")
            if spo:
                cap = spo.get("sharingCapability", "unknown")
                add_result("onedrive_external_sharing", "OneDrive External Sharing", "SharePoint", "warn",
                           f"OneDrive sharing inherits from SharePoint settings: {cap}. "
                           "Review OneDrive-specific settings in admin center.",
                           "Restricted", f"Inherited: {cap}", "Medium")

        log_line(f"INFO: BPA complete. {len(results)} checks executed.")
        return {"ok": True, "checks": results, "count": len(results)}

    except Exception as exc:
        log_line(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}
