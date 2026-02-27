"""
portal/celery_app.py — Celery application factory with Flask context support.

Usage:
  # Start worker:
  cd portal && celery -A celery_app.celery worker --loglevel=info

  # Start beat (periodic tasks):
  cd portal && celery -A celery_app.celery beat --loglevel=info
"""

import os
from celery import Celery


def make_celery(app=None):
    """Create a Celery instance, optionally bound to a Flask app."""
    celery = Celery(
        "portal",
        broker=os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0"),
        backend=os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/0"),
    )
    celery.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        task_track_started=True,
        worker_hijack_root_logger=False,
        beat_schedule={
            "check-report-schedules": {
                "task": "tasks.check_report_schedules",
                "schedule": 3600.0,  # every hour
            },
            "evaluate-alert-rules": {
                "task": "tasks.evaluate_alert_rules",
                "schedule": 300.0,  # every 5 minutes
            },
        },
    )
    if app:
        celery.conf.update(app.config)
        TaskBase = celery.Task

        class ContextTask(TaskBase):
            abstract = True

            def __call__(self, *args, **kwargs):
                with app.app_context():
                    return TaskBase.__call__(self, *args, **kwargs)

        celery.Task = ContextTask

    return celery


# Module-level celery instance for worker CLI
celery = make_celery()
