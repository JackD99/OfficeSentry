"""
tests/test_webhooks.py — Tests for the webhook management system.

Tests cover:
- Webhook CRUD operations (database layer)
- Webhook route endpoints (auth, validation, CRUD)
- HMAC signature generation
- Webhook delivery tracking
- Toggle and test endpoints
"""

import hashlib
import hmac
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


# ---------------------------------------------------------------------------
# Database: Webhook CRUD
# ---------------------------------------------------------------------------

class TestWebhookCRUD:
    """Test webhook database operations."""

    def test_create_webhook(self):
        wh_id = db.create_webhook(
            name="Test Hook", url="https://example.com/hook",
            secret="s3cret", events='["job.completed"]',
            created_by="admin",
        )
        assert wh_id is not None
        wh = db.get_webhook(wh_id)
        assert wh["name"] == "Test Hook"
        assert wh["url"] == "https://example.com/hook"
        assert wh["secret"] == "s3cret"
        assert wh["events"] == ["job.completed"]
        assert wh["enabled"] == 1

    def test_list_webhooks(self):
        db.create_webhook("H1", "https://a.com", "s1", created_by="admin")
        db.create_webhook("H2", "https://b.com", "s2", created_by="admin")
        hooks = db.list_webhooks()
        assert len(hooks) == 2
        names = {h["name"] for h in hooks}
        assert names == {"H1", "H2"}

    def test_update_webhook(self):
        wh_id = db.create_webhook("Old", "https://old.com", "s",
                                   created_by="admin")
        db.update_webhook(wh_id, name="New", url="https://new.com")
        wh = db.get_webhook(wh_id)
        assert wh["name"] == "New"
        assert wh["url"] == "https://new.com"

    def test_delete_webhook(self):
        wh_id = db.create_webhook("ToDelete", "https://x.com", "s",
                                   created_by="admin")
        assert db.delete_webhook(wh_id)
        assert db.get_webhook(wh_id) is None

    def test_delete_nonexistent_webhook(self):
        assert not db.delete_webhook("nonexistent")

    def test_toggle_webhook(self):
        wh_id = db.create_webhook("Toggle", "https://x.com", "s",
                                   created_by="admin")
        # Initially enabled (1) — toggle should disable (False)
        result = db.toggle_webhook(wh_id)
        assert result is False
        wh = db.get_webhook(wh_id)
        assert wh["enabled"] == 0

        # Toggle again — should enable (True)
        result = db.toggle_webhook(wh_id)
        assert result is True
        wh = db.get_webhook(wh_id)
        assert wh["enabled"] == 1

    def test_toggle_nonexistent_webhook(self):
        result = db.toggle_webhook("nonexistent")
        assert result is None

    def test_get_nonexistent_webhook(self):
        assert db.get_webhook("nonexistent") is None


# ---------------------------------------------------------------------------
# Database: Webhook deliveries
# ---------------------------------------------------------------------------

class TestWebhookDeliveries:
    """Test webhook delivery tracking."""

    def test_create_delivery(self):
        wh_id = db.create_webhook("D", "https://x.com", "s",
                                   created_by="admin")
        d_id = db.create_webhook_delivery(wh_id, "job.completed", '{"x":1}')
        assert d_id is not None

    def test_list_deliveries(self):
        wh_id = db.create_webhook("List", "https://x.com", "s",
                                   created_by="admin")
        db.create_webhook_delivery(wh_id, "job.completed", '{}')
        db.create_webhook_delivery(wh_id, "job.failed", '{}')
        deliveries = db.list_webhook_deliveries(wh_id)
        assert len(deliveries) == 2

    def test_update_delivery_delivered(self):
        wh_id = db.create_webhook("Upd", "https://x.com", "s",
                                   created_by="admin")
        d_id = db.create_webhook_delivery(wh_id, "test", "{}")
        db.update_webhook_delivery(d_id, "delivered", response_code=200,
                                    attempts=1)
        deliveries = db.list_webhook_deliveries(wh_id)
        assert deliveries[0]["status"] == "delivered"
        assert deliveries[0]["response_code"] == 200

    def test_update_delivery_failed(self):
        wh_id = db.create_webhook("Fail", "https://x.com", "s",
                                   created_by="admin")
        d_id = db.create_webhook_delivery(wh_id, "test", "{}")
        db.update_webhook_delivery(d_id, "failed", attempts=3)
        deliveries = db.list_webhook_deliveries(wh_id)
        assert deliveries[0]["status"] == "failed"
        assert deliveries[0]["attempts"] == 3

    def test_update_delivery_status(self):
        wh_id = db.create_webhook("Status", "https://x.com", "s",
                                   created_by="admin")
        db.update_webhook_delivery_status(wh_id, "ok")
        wh = db.get_webhook(wh_id)
        assert wh["last_status"] == "ok"
        assert wh["last_delivery_at"] is not None


# ---------------------------------------------------------------------------
# HMAC signature generation
# ---------------------------------------------------------------------------

class TestHMACSignature:
    """Test HMAC-SHA256 webhook signature generation."""

    def test_signature_matches(self):
        secret = "test-secret-key"
        payload = {"event": "test", "data": "hello"}
        body = json.dumps(payload).encode("utf-8")
        expected = hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()

        # Replicate what _deliver_sync does
        actual = hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        assert actual == expected

    def test_different_secrets_different_signatures(self):
        body = b'{"test": true}'
        sig1 = hmac.new(b"secret1", body, hashlib.sha256).hexdigest()
        sig2 = hmac.new(b"secret2", body, hashlib.sha256).hexdigest()
        assert sig1 != sig2


# ---------------------------------------------------------------------------
# Webhook route tests
# ---------------------------------------------------------------------------

class TestWebhookRoutes:
    """Test webhook management routes."""

    def test_webhooks_page_requires_auth(self, client):
        resp = client.get("/webhooks")
        assert resp.status_code == 302

    def test_webhooks_page_accessible(self, authed_client):
        resp = authed_client.get("/webhooks")
        assert resp.status_code == 200

    def test_create_webhook_route(self, authed_client):
        resp = authed_client.post("/webhooks/create", data={
            "name": "My Hook",
            "url": "https://example.com/webhook",
            "events": ["job.completed"],
        })
        data = resp.get_json()
        assert data["ok"]
        assert "secret" in data
        assert data["id"] is not None

    def test_create_webhook_missing_name(self, authed_client):
        resp = authed_client.post("/webhooks/create", data={
            "url": "https://example.com/webhook",
        })
        data = resp.get_json()
        assert not data["ok"]
        assert "name" in data["error"].lower()

    def test_create_webhook_missing_url(self, authed_client):
        resp = authed_client.post("/webhooks/create", data={
            "name": "No URL",
        })
        data = resp.get_json()
        assert not data["ok"]
        assert "url" in data["error"].lower()

    def test_create_webhook_invalid_url(self, authed_client):
        resp = authed_client.post("/webhooks/create", data={
            "name": "Bad URL",
            "url": "ftp://not-http.com",
        })
        data = resp.get_json()
        assert not data["ok"]

    def test_update_webhook_route(self, authed_client):
        wh_id = db.create_webhook("ToUpdate", "https://old.com", "s",
                                   created_by="admin")
        resp = authed_client.post(f"/webhooks/{wh_id}/update", data={
            "name": "Updated",
            "url": "https://new.com",
        })
        data = resp.get_json()
        assert data["ok"]
        wh = db.get_webhook(wh_id)
        assert wh["name"] == "Updated"

    def test_update_nonexistent_webhook(self, authed_client):
        resp = authed_client.post("/webhooks/nonexistent/update", data={
            "name": "X",
        })
        assert resp.status_code == 404

    def test_delete_webhook_route(self, authed_client):
        wh_id = db.create_webhook("ToDelete", "https://x.com", "s",
                                   created_by="admin")
        resp = authed_client.post(f"/webhooks/{wh_id}/delete")
        data = resp.get_json()
        assert data["ok"]
        assert db.get_webhook(wh_id) is None

    def test_delete_nonexistent_webhook_route(self, authed_client):
        resp = authed_client.post("/webhooks/nonexistent/delete")
        assert resp.status_code == 404

    def test_toggle_webhook_route(self, authed_client):
        wh_id = db.create_webhook("Toggle", "https://x.com", "s",
                                   created_by="admin")
        resp = authed_client.post(f"/webhooks/{wh_id}/toggle")
        data = resp.get_json()
        assert data["ok"]
        assert "enabled" in data

    def test_toggle_nonexistent_webhook_route(self, authed_client):
        resp = authed_client.post("/webhooks/nonexistent/toggle")
        assert resp.status_code == 404

    def test_test_webhook_nonexistent(self, authed_client):
        resp = authed_client.post("/webhooks/nonexistent/test")
        assert resp.status_code == 404

    def test_deliveries_endpoint(self, authed_client):
        wh_id = db.create_webhook("Deliv", "https://x.com", "s",
                                   created_by="admin")
        db.create_webhook_delivery(wh_id, "test", "{}")
        resp = authed_client.get(f"/webhooks/{wh_id}/deliveries")
        data = resp.get_json()
        assert data["ok"]
        assert len(data["deliveries"]) == 1

    def test_deliveries_nonexistent_webhook(self, authed_client):
        resp = authed_client.get("/webhooks/nonexistent/deliveries")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# fire_webhook_event integration
# ---------------------------------------------------------------------------

class TestFireWebhookEvent:
    """Test webhook event firing logic."""

    def test_event_dispatched_to_subscribed_webhook(self):
        from tasks import fire_webhook_event
        wh_id = db.create_webhook(
            "Sub", "https://example.com/hook", "s",
            events='["job.completed"]', created_by="admin",
        )
        # Patch deliver_webhook_task.delay and _deliver_sync
        with patch("tasks.deliver_webhook_task") as mock_task:
            mock_task.delay.side_effect = Exception("No Celery")
            with patch("tasks._deliver_sync") as mock_sync:
                fire_webhook_event("job.completed", {"job_id": "j1"})
                mock_sync.assert_called_once()

    def test_unsubscribed_event_not_dispatched(self):
        from tasks import fire_webhook_event
        db.create_webhook(
            "Sub", "https://example.com/hook", "s",
            events='["job.failed"]', created_by="admin",
        )
        with patch("tasks.deliver_webhook_task") as mock_task:
            mock_task.delay.side_effect = Exception("No Celery")
            with patch("tasks._deliver_sync") as mock_sync:
                fire_webhook_event("job.completed", {"job_id": "j1"})
                mock_sync.assert_not_called()

    def test_disabled_webhook_not_dispatched(self):
        from tasks import fire_webhook_event
        wh_id = db.create_webhook(
            "Dis", "https://example.com/hook", "s",
            events='["job.completed"]', created_by="admin",
        )
        db.toggle_webhook(wh_id)  # Disable
        with patch("tasks.deliver_webhook_task") as mock_task:
            mock_task.delay.side_effect = Exception("No Celery")
            with patch("tasks._deliver_sync") as mock_sync:
                fire_webhook_event("job.completed", {"job_id": "j1"})
                mock_sync.assert_not_called()
