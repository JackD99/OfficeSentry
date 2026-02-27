"""
tests/test_sse.py — Tests for Server-Sent Events (SSE) endpoints.

Tests cover:
- SSE endpoint returns correct content-type
- SSE falls back to polling when Redis unavailable
- Dashboard events endpoint
- Authentication required
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


# ---------------------------------------------------------------------------
# Job stream SSE endpoint
# ---------------------------------------------------------------------------

class TestJobStreamSSE:
    """Test the /jobs/<job_id>/stream SSE endpoint."""

    def test_stream_requires_auth(self, client):
        resp = client.get("/jobs/some-job/stream")
        assert resp.status_code == 302

    def test_stream_nonexistent_job(self, authed_client):
        resp = authed_client.get("/jobs/nonexistent/stream")
        assert resp.status_code == 404

    def test_stream_returns_event_stream_content_type(self, authed_client):
        """SSE endpoint should return text/event-stream."""
        job_id = db.create_job("SSE Test", "test")
        db.finish_job(job_id, "done")

        resp = authed_client.get(f"/jobs/{job_id}/stream")
        assert resp.content_type.startswith("text/event-stream")

    def test_stream_has_no_cache_header(self, authed_client):
        job_id = db.create_job("Cache Test", "test")
        db.finish_job(job_id, "done")

        resp = authed_client.get(f"/jobs/{job_id}/stream")
        assert "no-cache" in resp.headers.get("Cache-Control", "")

    def test_stream_completed_job_sends_done(self, authed_client):
        """A completed job should immediately send a done event."""
        job_id = db.create_job("Done Job", "test")
        db.append_job_output(job_id, "line 1")
        db.append_job_output(job_id, "line 2")
        db.finish_job(job_id, "done")

        resp = authed_client.get(f"/jobs/{job_id}/stream")
        # Read the response data
        data = resp.get_data(as_text=True)
        assert "done" in data

    def test_stream_falls_back_without_redis(self, authed_client):
        """Without Redis, stream should use polling fallback."""
        job_id = db.create_job("Fallback", "test")
        db.finish_job(job_id, "done")

        # Redis is not available in tests, so should fall back automatically
        resp = authed_client.get(f"/jobs/{job_id}/stream")
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/event-stream")


# ---------------------------------------------------------------------------
# Dashboard events SSE endpoint
# ---------------------------------------------------------------------------

class TestDashboardEventsSSE:
    """Test the /events/dashboard SSE endpoint."""

    def test_dashboard_events_requires_auth(self, client):
        resp = client.get("/events/dashboard")
        assert resp.status_code == 302

    def test_dashboard_events_returns_event_stream(self, authed_client):
        # Use a short-circuit approach since the endpoint loops forever
        # We'll check the content type from the response headers
        resp = authed_client.get("/events/dashboard")
        assert resp.content_type.startswith("text/event-stream")


# ---------------------------------------------------------------------------
# Job status endpoint (non-SSE, used as fallback target)
# ---------------------------------------------------------------------------

class TestJobStatusEndpoint:
    """Test the standard job status polling endpoint."""

    def test_job_status_returns_json(self, authed_client):
        job_id = db.create_job("Status Test", "test")
        resp = authed_client.get(f"/jobs/{job_id}/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "running"

    def test_job_status_not_found(self, authed_client):
        resp = authed_client.get("/jobs/nonexistent/status")
        assert resp.status_code == 404

    def test_job_status_includes_output(self, authed_client):
        job_id = db.create_job("Output Test", "test")
        db.append_job_output(job_id, "hello world")
        resp = authed_client.get(f"/jobs/{job_id}/status")
        data = resp.get_json()
        assert "hello world" in data["output"]

    def test_job_status_after_completion(self, authed_client):
        job_id = db.create_job("Complete", "test")
        db.finish_job(job_id, "done")
        resp = authed_client.get(f"/jobs/{job_id}/status")
        data = resp.get_json()
        assert data["status"] == "done"
        assert data["ended"] is not None


# ---------------------------------------------------------------------------
# Running count endpoint
# ---------------------------------------------------------------------------

class TestRunningCount:
    """Test the /jobs/running-count endpoint."""

    def test_running_count_requires_auth(self, client):
        resp = client.get("/jobs/running-count")
        assert resp.status_code == 302

    def test_running_count_returns_zero(self, authed_client):
        resp = authed_client.get("/jobs/running-count")
        data = resp.get_json()
        assert data["count"] == 0

    def test_running_count_with_running_job(self, authed_client):
        db.create_job("Running", "test")
        resp = authed_client.get("/jobs/running-count")
        data = resp.get_json()
        assert data["count"] == 1
