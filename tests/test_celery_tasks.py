"""
tests/test_celery_tasks.py — Tests for the Celery task queue system.

Tests cover:
- Celery app factory
- start_job() with Celery-first/thread-fallback
- cancel_job() with Celery revoke/subprocess-fallback
- Report schedule execution
- Task fallback behaviour
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


# ---------------------------------------------------------------------------
# Celery app factory
# ---------------------------------------------------------------------------

class TestCeleryFactory:
    """Test the Celery app factory."""

    def test_make_celery_returns_celery_instance(self):
        from celery_app import make_celery
        celery = make_celery()
        assert celery is not None
        assert celery.main == "portal"

    def test_make_celery_with_flask_app(self, app):
        from celery_app import make_celery
        celery = make_celery(app)
        assert celery is not None
        # Should have ContextTask class
        assert celery.Task is not None

    def test_beat_schedule_configured(self):
        from celery_app import make_celery
        celery = make_celery()
        schedule = celery.conf.beat_schedule
        assert "check-report-schedules" in schedule
        assert "evaluate-alert-rules" in schedule
        assert schedule["check-report-schedules"]["schedule"] == 3600.0
        assert schedule["evaluate-alert-rules"]["schedule"] == 300.0

    def test_celery_config_json_serializer(self):
        from celery_app import make_celery
        celery = make_celery()
        assert celery.conf.task_serializer == "json"
        assert celery.conf.result_serializer == "json"


# ---------------------------------------------------------------------------
# start_job() fallback behavior
# ---------------------------------------------------------------------------

class TestStartJobFallback:
    """Test that start_job() falls back to threading when Celery is unavailable."""

    @patch("helpers.threading")
    @patch("helpers.create_job", return_value="job-123")
    @patch("helpers.current_user", return_value="test-user")
    def test_start_job_uses_thread_when_celery_unavailable(
        self, mock_user, mock_create_job, mock_threading
    ):
        from helpers import start_job

        # Mock the Celery import to fail
        with patch.dict("sys.modules", {"tasks": None}):
            with patch("builtins.__import__", side_effect=ImportError):
                # Should fall back to threading
                job_id = start_job("Test Job", "test", ["echo", "hello"])
                assert job_id == "job-123"

    @patch("helpers.threading")
    @patch("helpers.create_job", return_value="job-456")
    @patch("helpers.current_user", return_value="test-user")
    def test_start_job_creates_job_in_db(
        self, mock_user, mock_create_job, mock_threading
    ):
        from helpers import start_job

        with patch.dict("sys.modules", {"tasks": None}):
            with patch("builtins.__import__", side_effect=ImportError):
                job_id = start_job("Test", "test", ["echo", "hi"])
                assert job_id == "job-456"
                mock_create_job.assert_called_once()


# ---------------------------------------------------------------------------
# cancel_job() fallback behavior
# ---------------------------------------------------------------------------

class TestCancelJobFallback:
    """Test cancel_job() with Celery revoke and subprocess fallback."""

    def test_cancel_nonexistent_job(self):
        from helpers import cancel_job
        result = cancel_job("nonexistent")
        assert not result

    @patch("helpers.get_job_task_id", return_value=None)
    def test_cancel_job_without_celery_task_id(self, mock_get_task):
        from helpers import cancel_job
        # No celery task id, no running process — should return False
        result = cancel_job("some-job")
        assert not result


# ---------------------------------------------------------------------------
# Database: Celery task ID tracking
# ---------------------------------------------------------------------------

class TestCeleryTaskTracking:
    """Test celery_task_id storage in jobs table."""

    def test_update_and_get_task_id(self):
        job_id = db.create_job("Test Job", "test")
        db.update_job_task_id(job_id, "celery-task-abc123")
        task_id = db.get_job_task_id(job_id)
        assert task_id == "celery-task-abc123"

    def test_get_task_id_nonexistent_job(self):
        task_id = db.get_job_task_id("nonexistent")
        assert task_id is None or task_id == ""

    def test_job_created_without_task_id(self):
        job_id = db.create_job("No Celery", "test")
        task_id = db.get_job_task_id(job_id)
        assert task_id == "" or task_id is None


# ---------------------------------------------------------------------------
# Report schedules
# ---------------------------------------------------------------------------

class TestReportSchedules:
    """Test report schedule database operations."""

    def test_get_due_schedules_empty(self):
        schedules = db.get_due_schedules()
        assert schedules == []

    def test_due_schedule_returned(self):
        # Create a tenant first (FK constraint)
        t_id = db.create_tenant("Sched Tenant", "sched.com")
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO report_schedules "
                "(id, name, audit_type, frequency, hour, tenant_id, "
                "next_run, enabled, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, datetime('now', '-1 hour'), 1, ?)",
                ("sched-1", "Test Schedule", "spo", "daily", 9, t_id, "admin"),
            )
        schedules = db.get_due_schedules()
        assert len(schedules) >= 1

    def test_update_schedule_run(self):
        t_id = db.create_tenant("Sched Tenant 2", "sched2.com")
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO report_schedules "
                "(id, name, audit_type, frequency, hour, tenant_id, "
                "next_run, enabled, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, datetime('now', '-1 hour'), 1, ?)",
                ("sched-2", "Test Schedule 2", "spo", "daily", 9, t_id, "admin"),
            )
        db.update_schedule_run("sched-2")
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT last_run FROM report_schedules WHERE id = ?",
                ("sched-2",)
            ).fetchone()
            assert row["last_run"] is not None


# ---------------------------------------------------------------------------
# Task module smoke tests
# ---------------------------------------------------------------------------

class TestTaskModule:
    """Smoke tests for the tasks module."""

    def test_fire_webhook_event_no_webhooks(self):
        """fire_webhook_event should not fail when no webhooks exist."""
        from tasks import fire_webhook_event
        # Should not raise
        fire_webhook_event("test.event", {"key": "value"})

    def test_deliver_sync_invalid_url(self):
        """_deliver_sync should gracefully handle unreachable URLs."""
        from tasks import _deliver_sync
        webhook = {
            "url": "http://127.0.0.1:1/nonexistent",
            "secret": "test-secret",
        }
        result = _deliver_sync(webhook, "test", {"msg": "hello"})
        assert result is False

    def test_publish_line_no_redis(self):
        """_publish_line should silently fail without Redis."""
        from tasks import _publish_line
        # Should not raise even if Redis is unavailable
        _publish_line("job-123", "test line")
