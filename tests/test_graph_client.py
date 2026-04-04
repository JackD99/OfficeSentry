"""
tests/test_graph_client.py — Tests for the Python Graph API client and jobs.
"""

import json
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ---------------------------------------------------------------------------
# graph_client.GraphClient
# ---------------------------------------------------------------------------

class TestGraphClient:
    """Tests for GraphClient initialization and requests."""

    @patch("graph_client.GraphClient._load_pfx")
    @patch("graph_client.msal.ConfidentialClientApplication")
    def test_client_init(self, mock_msal, mock_load):
        from graph_client import GraphClient
        mock_load.return_value = {"private_key": "fake", "thumbprint": "ABC123"}

        client = GraphClient("tenant.com", "client-id", "/fake.pfx", "pass")
        assert client.tenant_id == "tenant.com"
        assert client.client_id == "client-id"
        mock_load.assert_called_once()
        mock_msal.assert_called_once()

    @patch("graph_client.GraphClient._load_pfx")
    @patch("graph_client.msal.ConfidentialClientApplication")
    def test_acquire_token_success(self, mock_msal_cls, mock_load):
        from graph_client import GraphClient
        mock_load.return_value = {"private_key": "fake", "thumbprint": "ABC"}

        mock_app = MagicMock()
        mock_app.acquire_token_for_client.return_value = {"access_token": "tok123"}
        mock_msal_cls.return_value = mock_app

        client = GraphClient("t", "c", "/f.pfx", "p")
        assert client.token == "tok123"

    @patch("graph_client.GraphClient._load_pfx")
    @patch("graph_client.msal.ConfidentialClientApplication")
    def test_acquire_token_failure(self, mock_msal_cls, mock_load):
        from graph_client import GraphAuthError, GraphClient
        mock_load.return_value = {"private_key": "fake", "thumbprint": "ABC"}

        mock_app = MagicMock()
        mock_app.acquire_token_for_client.return_value = {"error": "invalid_client"}
        mock_msal_cls.return_value = mock_app

        client = GraphClient("t", "c", "/f.pfx", "p")
        with pytest.raises(GraphAuthError):
            _ = client.token

    @patch("graph_client.GraphClient._load_pfx")
    @patch("graph_client.msal.ConfidentialClientApplication")
    def test_get_request(self, mock_msal_cls, mock_load):
        from graph_client import GraphClient
        mock_load.return_value = {"private_key": "fake", "thumbprint": "ABC"}
        mock_app = MagicMock()
        mock_app.acquire_token_for_client.return_value = {"access_token": "tok"}
        mock_msal_cls.return_value = mock_app

        client = GraphClient("t", "c", "/f.pfx", "p")

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"value": [{"id": "contoso.com"}]}

        with patch.object(client._session, "get", return_value=mock_resp):
            result = client.get("/domains")
            assert result["value"][0]["id"] == "contoso.com"

    @patch("graph_client.GraphClient._load_pfx")
    @patch("graph_client.msal.ConfidentialClientApplication")
    def test_get_paginated(self, mock_msal_cls, mock_load):
        from graph_client import GraphClient
        mock_load.return_value = {"private_key": "fake", "thumbprint": "ABC"}
        mock_app = MagicMock()
        mock_app.acquire_token_for_client.return_value = {"access_token": "tok"}
        mock_msal_cls.return_value = mock_app

        client = GraphClient("t", "c", "/f.pfx", "p")

        # Page 1 has nextLink, page 2 doesn't
        page1 = MagicMock()
        page1.ok = True
        page1.status_code = 200
        page1.json.return_value = {
            "value": [{"id": "a"}],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/domains?$skip=1",
        }
        page2 = MagicMock()
        page2.ok = True
        page2.status_code = 200
        page2.json.return_value = {"value": [{"id": "b"}]}

        with patch.object(client._session, "get", side_effect=[page1, page2]):
            items = client.get_paginated("/domains")
            assert len(items) == 2
            assert items[0]["id"] == "a"
            assert items[1]["id"] == "b"

    @patch("graph_client.GraphClient._load_pfx")
    @patch("graph_client.msal.ConfidentialClientApplication")
    def test_get_401_retries(self, mock_msal_cls, mock_load):
        from graph_client import GraphClient
        mock_load.return_value = {"private_key": "fake", "thumbprint": "ABC"}
        mock_app = MagicMock()
        mock_app.acquire_token_for_client.return_value = {"access_token": "new_tok"}
        mock_msal_cls.return_value = mock_app

        client = GraphClient("t", "c", "/f.pfx", "p")

        resp_401 = MagicMock()
        resp_401.ok = False
        resp_401.status_code = 401
        resp_401.text = "Unauthorized"

        resp_ok = MagicMock()
        resp_ok.ok = True
        resp_ok.status_code = 200
        resp_ok.json.return_value = {"value": []}

        with patch.object(client._session, "get", side_effect=[resp_401, resp_ok]):
            result = client.get("/test")
            assert result == {"value": []}


# ---------------------------------------------------------------------------
# graph_jobs — function tests
# ---------------------------------------------------------------------------

class TestGraphJobs:
    """Tests for the Python Graph job functions."""

    @patch("graph_jobs.GraphClient")
    def test_fetch_tenant_domains(self, mock_client_cls):
        from graph_jobs import fetch_tenant_domains

        mock_client = MagicMock()
        mock_client.get_paginated.return_value = [
            {
                "id": "contoso.com",
                "authenticationType": "Managed",
                "isVerified": True,
                "isDefault": True,
                "isInitial": False,
                "isRoot": True,
                "isAdminManaged": True,
                "supportedServices": ["Email", "OfficeCommunicationsOnline"],
            }
        ]
        mock_client_cls.return_value = mock_client

        tenant = {"tenant_id": "tid", "domain": "contoso.com", "client_id": "cid"}
        result = fetch_tenant_domains(tenant, "/fake.pfx", "pass")

        assert result["ok"] is True
        assert result["count"] == 1
        assert result["domains"][0]["name"] == "contoso.com"
        assert result["domains"][0]["capabilities"] == "Email, OfficeCommunicationsOnline"

    @patch("graph_jobs.GraphClient")
    def test_detect_hybrid_status_sync_enabled(self, mock_client_cls):
        from graph_jobs import detect_hybrid_status

        mock_client = MagicMock()

        def mock_get(endpoint, params=None):
            return {
                "value": [{
                    "onPremisesSyncEnabled": True,
                    "onPremisesLastSyncDateTime": "2026-04-01T12:00:00Z",
                }]
            }

        mock_client.get.side_effect = mock_get
        mock_client.get_paginated.return_value = []
        mock_client_cls.return_value = mock_client

        tenant = {"tenant_id": "tid", "domain": "contoso.com", "client_id": "cid"}
        result = detect_hybrid_status(tenant, "/fake.pfx", "pass")

        assert result["ok"] is True
        assert result["hybrid_detected"] is True
        assert result["sync_enabled"] is True
        assert "2026-04-01" in result["last_sync"]

    @patch("graph_jobs.GraphClient")
    def test_detect_hybrid_status_cloud_only(self, mock_client_cls):
        from graph_jobs import detect_hybrid_status

        mock_client = MagicMock()
        mock_client.get.return_value = {
            "value": [{"onPremisesSyncEnabled": False}]
        }
        mock_client.get_paginated.return_value = []
        mock_client_cls.return_value = mock_client

        tenant = {"tenant_id": "tid", "domain": "contoso.com", "client_id": "cid"}
        result = detect_hybrid_status(tenant, "/fake.pfx", "pass")

        assert result["ok"] is True
        assert result["hybrid_detected"] is False
        assert "cloud-only" in result["details"]

    @patch("graph_jobs.GraphClient")
    def test_fetch_secure_score(self, mock_client_cls):
        from graph_jobs import fetch_secure_score

        mock_client = MagicMock()
        mock_client.get.return_value = {
            "value": [{
                "currentScore": 42.5,
                "maxScore": 100.0,
                "createdDateTime": "2026-04-01T00:00:00Z",
                "averageComparativeScores": [
                    {"basis": "AllTenants", "averageScore": 50.0}
                ],
                "enabledServices": ["EXO", "SPO"],
                "controlScores": [
                    {
                        "controlName": "MFARegistration",
                        "description": "MFA",
                        "score": 5,
                        "maxScore": 10,
                        "scoreInPercentage": 50,
                        "controlCategory": "Identity",
                    }
                ],
            }]
        }
        mock_client_cls.return_value = mock_client

        tenant = {"tenant_id": "tid", "domain": "contoso.com", "client_id": "cid"}
        result = fetch_secure_score(tenant, "/fake.pfx", "pass")

        assert result["ok"] is True
        assert result["currentScore"] == 42.5
        assert result["maxScore"] == 100.0
        assert result["percentage"] == 42.5
        assert result["averageComparatives"]["AllTenants"] == 50.0
        assert len(result["controlScores"]) == 1

    @patch("graph_jobs.GraphClient")
    def test_fetch_secure_score_no_data(self, mock_client_cls):
        from graph_jobs import fetch_secure_score

        mock_client = MagicMock()
        mock_client.get.return_value = {"value": []}
        mock_client_cls.return_value = mock_client

        tenant = {"tenant_id": "tid", "domain": "contoso.com", "client_id": "cid"}
        result = fetch_secure_score(tenant, "/fake.pfx", "pass")

        assert result["ok"] is False
        assert "No Secure Score" in result["error"]

    @patch("graph_jobs.GraphClient")
    def test_fetch_tenant_domains_auth_error(self, mock_client_cls):
        from graph_jobs import fetch_tenant_domains
        from graph_client import GraphAuthError

        mock_client_cls.side_effect = GraphAuthError("bad cert")

        tenant = {"tenant_id": "tid", "domain": "contoso.com", "client_id": "cid"}
        result = fetch_tenant_domains(tenant, "/fake.pfx", "pass")

        assert result["ok"] is False
        assert "bad cert" in result["error"]

    @patch("graph_jobs.GraphClient")
    def test_fetch_auth_methods(self, mock_client_cls):
        from graph_jobs import fetch_auth_methods

        mock_client = MagicMock()
        mock_client.get.side_effect = lambda ep, **kw: {
            "/policies/authenticationMethodsPolicy": {
                "description": "Test policy",
                "registrationEnforcement": {"authenticationMethodsRegistrationCampaign": {}},
                "authenticationMethodConfigurations": [
                    {"id": "microsoftAuthenticator", "state": "enabled"},
                    {"id": "fido2", "state": "disabled"},
                ],
            },
            "/reports/authenticationMethods/usersRegisteredByMethod": {"value": []},
        }.get(ep, {})
        mock_client_cls.return_value = mock_client

        tenant = {"tenant_id": "tid", "domain": "contoso.com", "client_id": "cid"}
        result = fetch_auth_methods(tenant, "/fake.pfx", "pass")

        assert result["ok"] is True
        assert len(result["methods"]) == 2
        assert result["methods"][0]["method"] == "microsoftAuthenticator"

    @patch("graph_jobs.GraphClient")
    def test_fetch_partner_relationships(self, mock_client_cls):
        from graph_jobs import fetch_partner_relationships

        mock_client = MagicMock()
        mock_client.get_paginated.return_value = [
            {
                "id": "rel1",
                "displayName": "Test Rel",
                "partner": {"displayName": "MSP Corp", "tenantId": "partner-tid"},
                "status": "active",
                "duration": "P730D",
                "createdDateTime": "2025-01-01",
                "endDateTime": "2027-01-01",
                "autoExtendDuration": "P180D",
                "accessDetails": {
                    "unifiedRoles": [{"roleDefinitionId": "role-1"}]
                },
            }
        ]
        mock_client_cls.return_value = mock_client

        tenant = {"tenant_id": "tid", "domain": "contoso.com", "client_id": "cid"}
        result = fetch_partner_relationships(tenant, "/fake.pfx", "pass")

        assert result["ok"] is True
        assert result["count"] == 1
        assert result["relationships"][0]["partner"] == "MSP Corp"
        assert result["relationships"][0]["roles"] == "role-1"

    @patch("graph_jobs.GraphClient")
    def test_fetch_app_consent_requests(self, mock_client_cls):
        from graph_jobs import fetch_app_consent_requests

        mock_client = MagicMock()
        mock_client.get_paginated.return_value = [
            {
                "id": "req1",
                "appId": "app-1",
                "appDisplayName": "Test App",
                "consentType": "pending",
                "pendingScopes": [{"displayName": "Mail.Read"}],
            }
        ]
        mock_client_cls.return_value = mock_client

        tenant = {"tenant_id": "tid", "domain": "contoso.com", "client_id": "cid"}
        result = fetch_app_consent_requests(tenant, "/fake.pfx", "pass")

        assert result["ok"] is True
        assert result["count"] == 1
        assert result["requests"][0]["appName"] == "Test App"
        assert result["requests"][0]["pending"] == "Mail.Read"

    @patch("graph_jobs.GraphClient")
    def test_fetch_app_registrations(self, mock_client_cls):
        from graph_jobs import fetch_app_registrations

        mock_client = MagicMock()
        mock_client.get_paginated.side_effect = lambda ep, **kw: {
            "/applications": [
                {
                    "id": "app1",
                    "appId": "aid1",
                    "displayName": "My App",
                    "signInAudience": "AzureADMyOrg",
                    "createdDateTime": "2025-01-01",
                    "passwordCredentials": [{"endDateTime": "2026-01-01"}],
                    "keyCredentials": [],
                    "requiredResourceAccess": [],
                }
            ],
            "/servicePrincipals": [
                {
                    "id": "sp1",
                    "appId": "aid1",
                    "displayName": "My App",
                    "servicePrincipalType": "Application",
                    "accountEnabled": True,
                    "appOwnerOrganizationId": "org1",
                    "createdDateTime": "2025-01-01",
                    "loginUrl": None,
                    "tags": ["WindowsAzureActiveDirectoryIntegratedApp"],
                }
            ],
        }.get(ep, [])
        mock_client_cls.return_value = mock_client

        tenant = {"tenant_id": "tid", "domain": "contoso.com", "client_id": "cid"}
        result = fetch_app_registrations(tenant, "/fake.pfx", "pass", "both")

        assert result["ok"] is True
        assert result["appRegistrationCount"] == 1
        assert result["appRegistrations"][0]["hasSecrets"] is True
        assert result["enterpriseAppCount"] == 1

    @patch("graph_jobs.GraphClient")
    def test_fetch_directory_audit_logs(self, mock_client_cls):
        from graph_jobs import fetch_directory_audit_logs

        mock_client = MagicMock()
        mock_client.get_paginated.return_value = [
            {
                "id": "log1",
                "activityDisplayName": "Update user",
                "category": "UserManagement",
                "result": "success",
                "initiatedBy": {
                    "user": {"userPrincipalName": "admin@contoso.com"},
                    "app": {},
                },
                "targetResources": [
                    {"displayName": "John Doe", "@odata.type": "#microsoft.graph.user"}
                ],
                "activityDateTime": "2026-04-01T12:00:00Z",
                "correlationId": "corr-1",
            }
        ]
        mock_client_cls.return_value = mock_client

        tenant = {"tenant_id": "tid", "domain": "contoso.com", "client_id": "cid"}
        result = fetch_directory_audit_logs(tenant, "/fake.pfx", "pass")

        assert result["ok"] is True
        assert result["count"] == 1
        assert result["logs"][0]["actorUPN"] == "admin@contoso.com"

    @patch("graph_jobs.GraphClient")
    def test_fetch_directory_audit_logs_with_dates(self, mock_client_cls):
        from graph_jobs import fetch_directory_audit_logs

        mock_client = MagicMock()
        mock_client.get_paginated.return_value = []
        mock_client_cls.return_value = mock_client

        tenant = {"tenant_id": "tid", "domain": "contoso.com", "client_id": "cid"}
        result = fetch_directory_audit_logs(
            tenant, "/fake.pfx", "pass",
            start_date="2026-03-01", end_date="2026-04-01",
        )

        assert result["ok"] is True
        # Verify filter params were passed
        call_args = mock_client.get_paginated.call_args
        params = call_args[1].get("params", call_args[0][1] if len(call_args[0]) > 1 else {})
        assert "$filter" in params

    @patch("graph_jobs.GraphClient")
    def test_fetch_conditional_access(self, mock_client_cls):
        from graph_jobs import fetch_conditional_access

        mock_client = MagicMock()
        mock_client.get_paginated.side_effect = lambda ep, **kw: {
            "/identity/conditionalAccess/policies": [
                {
                    "id": "pol1",
                    "displayName": "Block Legacy",
                    "state": "enabled",
                    "createdDateTime": "2025-01-01",
                    "modifiedDateTime": "2025-06-01",
                    "conditions": {"clientAppTypes": ["exchangeActiveSync"]},
                    "grantControls": {"builtInControls": ["block"]},
                    "sessionControls": {},
                }
            ],
            "/identity/conditionalAccess/namedLocations": [
                {
                    "id": "loc1",
                    "displayName": "Office",
                    "@odata.type": "#microsoft.graph.ipNamedLocation",
                    "createdDateTime": "2025-01-01",
                    "modifiedDateTime": "2025-01-01",
                    "isTrusted": True,
                }
            ],
        }.get(ep, [])
        mock_client_cls.return_value = mock_client

        tenant = {"tenant_id": "tid", "domain": "contoso.com", "client_id": "cid"}
        result = fetch_conditional_access(tenant, "/fake.pfx", "pass", "both")

        assert result["ok"] is True
        assert result["policyCount"] == 1
        assert result["locationCount"] == 1
        assert result["policies"][0]["displayName"] == "Block Legacy"

    @patch("graph_jobs.GraphClient")
    def test_fetch_service_health_status(self, mock_client_cls):
        from graph_jobs import fetch_service_health

        mock_client = MagicMock()
        mock_client.get_paginated.return_value = [
            {"service": "Exchange Online", "status": "serviceOperational"},
            {"service": "SharePoint Online", "status": "serviceDegradation"},
        ]
        mock_client_cls.return_value = mock_client

        tenant = {"tenant_id": "tid", "domain": "contoso.com", "client_id": "cid"}
        result = fetch_service_health(tenant, "/fake.pfx", "pass", "status")

        assert result["ok"] is True
        assert result["count"] == 2

    @patch("graph_jobs.GraphClient")
    def test_fetch_service_health_incidents(self, mock_client_cls):
        from graph_jobs import fetch_service_health

        mock_client = MagicMock()
        mock_client.get_paginated.return_value = [
            {"title": "EXO issue", "service": "Exchange", "classification": "incident"},
        ]
        mock_client_cls.return_value = mock_client

        tenant = {"tenant_id": "tid", "domain": "contoso.com", "client_id": "cid"}
        result = fetch_service_health(tenant, "/fake.pfx", "pass", "incidents")

        assert result["ok"] is True
        assert result["count"] == 1

    @patch("graph_jobs.GraphClient")
    def test_fetch_service_health_overview(self, mock_client_cls):
        from graph_jobs import fetch_service_health

        mock_client = MagicMock()
        mock_client.get_paginated.side_effect = [
            # healthOverviews
            [
                {"service": "EXO", "status": "serviceOperational"},
                {"service": "SPO", "status": "serviceDegradation"},
            ],
            # issues (active)
            [{"title": "SPO degraded"}],
        ]
        mock_client_cls.return_value = mock_client

        tenant = {"tenant_id": "tid", "domain": "contoso.com", "client_id": "cid"}
        result = fetch_service_health(tenant, "/fake.pfx", "pass", "overview")

        assert result["ok"] is True
        assert result["overview"]["totalServices"] == 2
        assert result["overview"]["operationalServices"] == 1
        assert result["overview"]["degradedServices"] == 1
        assert result["overview"]["activeIncidents"] == 1


# ---------------------------------------------------------------------------
# helpers — start_python_job / _python_worker
# ---------------------------------------------------------------------------

class TestPythonJobWorker:
    """Tests for the Python job execution helpers."""

    def test_python_worker_success(self, app):
        """_python_worker runs function and stores result."""
        from helpers import _python_worker
        from database import get_job, create_job

        with app.app_context():
            job_id = create_job("test", "test_type")

            def my_func(*, log_line):
                log_line("doing work")
                return {"ok": True, "data": 42}

            _python_worker(job_id, my_func, (), {})

            job = get_job(job_id)
            assert job["status"] == "done"
            output = job["output"]  # already a list
            assert "doing work" in output
            # Last line should be JSON result
            result = json.loads(output[-1])
            assert result["ok"] is True

    def test_python_worker_failure(self, app):
        """_python_worker handles exceptions."""
        from helpers import _python_worker
        from database import get_job, create_job

        with app.app_context():
            job_id = create_job("test", "test_type")

            def bad_func(*, log_line):
                raise RuntimeError("boom")

            _python_worker(job_id, bad_func, (), {})

            job = get_job(job_id)
            assert job["status"] == "failed"

    def test_python_worker_ok_false_marks_failed(self, app):
        """_python_worker marks job failed when func returns ok=False."""
        from helpers import _python_worker
        from database import get_job, create_job

        with app.app_context():
            job_id = create_job("test", "test_type")

            def my_func(*, log_line):
                return {"ok": False, "error": "something went wrong"}

            _python_worker(job_id, my_func, (), {})

            job = get_job(job_id)
            assert job["status"] == "failed"

    def test_start_python_job(self, app):
        """start_python_job creates a job and returns job_id."""
        from helpers import start_python_job

        def noop(*, log_line):
            return {"ok": True}

        with app.test_request_context():
            from flask import session
            session["username"] = "admin"
            with patch("helpers._python_worker"):
                jid = start_python_job("test label", "test_type", noop)
                assert jid  # non-empty string


# ---------------------------------------------------------------------------
# Entra App management (device code flow)
# ---------------------------------------------------------------------------

class TestEntraAppManagement:
    """Tests for create_entra_app and remove_entra_app."""

    @patch("graph_jobs._graph_request")
    @patch("graph_jobs._device_code_auth")
    def test_create_entra_app_success(self, mock_auth, mock_req):
        from graph_jobs import create_entra_app
        import tempfile, os

        mock_auth.return_value = "fake-token"

        # Create a temp CER file with a real self-signed cert
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from datetime import datetime, timezone, timedelta

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test")])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=365))
            .sign(key, hashes.SHA256())
        )
        cer_data = cert.public_bytes(serialization.Encoding.DER)

        with tempfile.NamedTemporaryFile(suffix=".cer", delete=False) as f:
            f.write(cer_data)
            cer_path = f.name

        try:
            # Mock Graph API responses
            call_count = {"n": 0}

            def mock_graph(method, endpoint, token, **kwargs):
                call_count["n"] += 1
                if method == "POST" and endpoint == "/applications":
                    return {"id": "obj-123", "appId": "client-456", "displayName": "Test App"}
                if method == "POST" and endpoint == "/servicePrincipals":
                    return {"id": "sp-789"}
                if method == "PATCH":
                    return None
                if method == "GET" and "/servicePrincipals" in endpoint:
                    return {"value": [{
                        "id": "res-sp-1",
                        "appRoles": [
                            {"value": "Sites.Read.All", "id": "role-1",
                             "allowedMemberTypes": ["Application"]},
                        ],
                    }]}
                if method == "POST" and "appRoleAssignments" in endpoint:
                    return {}
                return {}

            mock_req.side_effect = mock_graph

            result = create_entra_app("admin-cid", cer_path, "Test App", "tenant-1")

            assert result["ok"] is True
            assert result["clientId"] == "client-456"
            assert result["appId"] == "obj-123"
            assert "entra.microsoft.com" in result["consentUrl"]
        finally:
            os.unlink(cer_path)

    @patch("graph_jobs._device_code_auth")
    def test_create_entra_app_auth_failure(self, mock_auth):
        from graph_jobs import create_entra_app
        from graph_client import GraphAuthError

        mock_auth.side_effect = GraphAuthError("User cancelled")

        result = create_entra_app("admin-cid", "/fake.cer")
        assert result["ok"] is False
        assert "User cancelled" in result["error"]

    @patch("graph_jobs._graph_request")
    @patch("graph_jobs._device_code_auth")
    def test_remove_entra_app_success(self, mock_auth, mock_req):
        from graph_jobs import remove_entra_app
        import tempfile, os

        mock_auth.return_value = "fake-token"

        # Create temp files to verify cleanup
        pfx_f = tempfile.NamedTemporaryFile(suffix=".pfx", delete=False)
        pfx_f.close()
        cer_f = tempfile.NamedTemporaryFile(suffix=".cer", delete=False)
        cer_f.close()

        def mock_graph(method, endpoint, token, **kwargs):
            if method == "GET":
                return {"value": [{"id": "obj-123"}]}
            return None  # DELETE returns 204

        mock_req.side_effect = mock_graph

        try:
            result = remove_entra_app("admin-cid", "client-456", pfx_f.name, cer_f.name)
            assert result["ok"] is True
            assert not os.path.exists(pfx_f.name)
            assert not os.path.exists(cer_f.name)
        finally:
            # Cleanup in case test fails
            for f in (pfx_f.name, cer_f.name):
                if os.path.exists(f):
                    os.unlink(f)

    @patch("graph_jobs._graph_request")
    @patch("graph_jobs._device_code_auth")
    def test_remove_entra_app_not_found(self, mock_auth, mock_req):
        from graph_jobs import remove_entra_app

        mock_auth.return_value = "fake-token"
        mock_req.return_value = {"value": []}  # App not found

        lines = []
        result = remove_entra_app("admin-cid", "client-456", log_line=lines.append)

        assert result["ok"] is True
        assert any("not found" in l for l in lines)
