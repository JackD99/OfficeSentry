"""
portal/tasks.py — Celery tasks for background job execution, scheduling, and webhooks.

Tasks:
  - run_ps_job: Execute a PowerShell script as a background Celery task
  - check_report_schedules: Periodic — execute due report schedules
  - evaluate_alert_rules: Periodic — evaluate alert rules against recent events
  - deliver_webhook_task: Deliver a webhook with HMAC signing and retry
"""

import hashlib
import hmac
import json
import logging
import os
import subprocess
import urllib.request

from celery_app import make_celery

celery = make_celery()
log = logging.getLogger("portal.tasks")


@celery.task(bind=True, max_retries=2, soft_time_limit=3600, time_limit=3660)
def run_ps_job(self, job_id: str, cmd: list, env: dict = None):
    """Run a PowerShell script as a Celery task with output streaming."""
    from database import append_job_output, finish_job

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=env,
        )
        for line in proc.stdout:
            stripped = line.rstrip()
            append_job_output(job_id, stripped)
            # Publish to Redis for SSE
            _publish_line(job_id, stripped)

        rc = proc.wait()
        status = "done" if rc == 0 else "failed"
        finish_job(job_id, status)
        _publish_line(job_id, None, status=status)

        # Fire webhook
        event = "job.completed" if status == "done" else "job.failed"
        _fire_job_event(job_id, event, status)

        log.info("Celery job %s finished: %s (rc=%d)", job_id, status, rc)
    except Exception as exc:
        from database import append_job_output, finish_job
        append_job_output(job_id, f"ERROR: {exc}")
        finish_job(job_id, "failed")
        _fire_job_event(job_id, "job.failed", "failed")
        log.error("Celery job %s error: %s", job_id, exc)
        raise


def _publish_line(job_id: str, line: str | None, status: str = "running"):
    """Publish a job output line to Redis pub/sub for SSE."""
    try:
        import redis
        r = redis.Redis.from_url(
            os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
        )
        payload = json.dumps({"line": line, "status": status})
        r.publish(f"job:{job_id}", payload)
    except Exception:
        pass


def _fire_job_event(job_id: str, event: str, status: str):
    """Fire webhook event for job completion."""
    try:
        from database import get_job
        job = get_job(job_id)
        if not job:
            return
        fire_webhook_event(event, {
            "job_id": job_id,
            "label": job.get("label", ""),
            "type": job.get("type", ""),
            "tenant_id": job.get("tenant_id", ""),
            "status": status,
        })
    except Exception:
        pass


@celery.task
def check_report_schedules():
    """Periodic task: execute due report schedules."""
    from database import get_due_schedules, update_schedule_run, get_tenant
    from helpers import build_ps_env, get_tenant_pfx_password, SCRIPTS_DIR, PWSH

    schedules = get_due_schedules()
    for sched in schedules:
        try:
            tenant_id = sched.get("tenant_id")
            if not tenant_id:
                update_schedule_run(sched["id"])
                continue

            tenant = get_tenant(tenant_id)
            if not tenant or not tenant.get("client_id"):
                update_schedule_run(sched["id"])
                continue

            pfx_pass = get_tenant_pfx_password(tenant)
            env = build_ps_env(pfx_pass)

            audit_type = sched.get("audit_type", "spo")
            script_map = {
                "spo": SCRIPTS_DIR / "Audit-SitePermissions.ps1",
                "security": None,  # 365Inspect handled separately
            }
            script = script_map.get(audit_type)
            if not script or not script.exists():
                update_schedule_run(sched["id"])
                continue

            from helpers import start_job
            cmd = [PWSH, "-NoProfile", "-NonInteractive",
                   "-ExecutionPolicy", "Bypass", str(script)]
            start_job(
                f"Scheduled: {sched['name']}",
                audit_type, cmd,
                tenant_id=tenant_id, env=env,
            )
            update_schedule_run(sched["id"])
            log.info("Executed schedule %s for tenant %s", sched["id"], tenant_id)
        except Exception as exc:
            log.error("Schedule %s error: %s", sched["id"], exc)
            update_schedule_run(sched["id"])


@celery.task
def evaluate_alert_rules():
    """Periodic task: evaluate alert rules (delegates to existing logic)."""
    try:
        from routes.standards import process_alert_rules
        process_alert_rules("periodic_check")
    except Exception as exc:
        log.error("Alert evaluation error: %s", exc)


@celery.task(bind=True, max_retries=5, default_retry_delay=30)
def deliver_webhook_task(self, webhook_id: str, event_type: str, payload: dict):
    """Deliver a webhook with HMAC-SHA256 signing and retry."""
    from database import (
        get_webhook, create_webhook_delivery, update_webhook_delivery,
        update_webhook_delivery_status,
    )

    webhook = get_webhook(webhook_id)
    if not webhook or not webhook.get("enabled"):
        return

    delivery_id = create_webhook_delivery(
        webhook_id, event_type, json.dumps(payload)
    )

    try:
        body = json.dumps(payload).encode("utf-8")

        # HMAC-SHA256 signature
        signature = hmac.new(
            webhook["secret"].encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()

        req = urllib.request.Request(
            webhook["url"],
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature": f"sha256={signature}",
                "X-Webhook-Event": event_type,
                "X-Webhook-ID": delivery_id,
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=15) as resp:
            status_code = resp.status
            if status_code < 400:
                update_webhook_delivery(delivery_id, "delivered",
                                        response_code=status_code,
                                        attempts=self.request.retries + 1)
                update_webhook_delivery_status(webhook_id, "ok")
                log.info("Webhook %s delivered to %s (event=%s)",
                         webhook_id, webhook["url"], event_type)
            else:
                raise Exception(f"HTTP {status_code}")

    except Exception as exc:
        attempts = self.request.retries + 1
        update_webhook_delivery(delivery_id, "failed",
                                attempts=attempts)
        update_webhook_delivery_status(webhook_id, f"failed: {exc}")
        log.warning("Webhook %s delivery failed (attempt %d): %s",
                     webhook_id, attempts, exc)
        # Retry with exponential backoff
        raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))


def fire_webhook_event(event_type: str, payload: dict):
    """Fire webhook event to all subscribed, enabled webhooks."""
    from database import list_webhooks

    webhooks = list_webhooks()
    for wh in webhooks:
        if not wh.get("enabled"):
            continue
        events = wh.get("events", [])
        if events and event_type not in events:
            continue
        try:
            deliver_webhook_task.delay(wh["id"], event_type, payload)
        except Exception:
            # Celery not available — try synchronous delivery
            _deliver_sync(wh, event_type, payload)


def _deliver_sync(webhook: dict, event_type: str, payload: dict):
    """Synchronous webhook delivery fallback when Celery is unavailable."""
    try:
        body = json.dumps(payload).encode("utf-8")
        signature = hmac.new(
            webhook["secret"].encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        req = urllib.request.Request(
            webhook["url"],
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature": f"sha256={signature}",
                "X-Webhook-Event": event_type,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status < 400
    except Exception as exc:
        log.warning("Sync webhook delivery failed: %s", exc)
        return False
