"""Settings → Updates: what's new in Office Sentry, and the Update button (``updates.py``).

The page never touches Docker: pressing Update writes a request for the updater container
(``updater.py``) and the page follows the updater's progress until the new version answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from .. import __version__, updates
from ..db import audit, transaction
from ..formatting import parse as parse_time
from .access import needs
from .release_notes import render

bp = Blueprint("updates", __name__, url_prefix="/admin")

DEPLOY_UPDATING = f"{updates.DEPLOY_DOCS}#updating"
DEPLOY_BUTTON = f"{updates.DEPLOY_DOCS}#the-update-button"
DEPLOY_ROLLBACK = f"{updates.DEPLOY_DOCS}#rolling-back-an-update"
DEPLOY_GUIDES = "https://officesentry.co.uk/docs/deploy/"  # docs/deploy/README.md on the website
RECENT = timedelta(days=1)  # how long the page leads with an update's outcome, or a request never picked up


@dataclass
class Offer:
    """The release the page offers, every release it brings, and why there's no Update button when there
    isn't one."""

    target: updates.Release | None
    brings: list[updates.Release] = field(default_factory=list)  # newest first, the target included
    reason: str = ""  # "" when the button is offered

    @property
    def button(self) -> bool:
        return self.target is not None and not self.reason

    @property
    def asks(self) -> bool:
        """Whether any release it brings has an Action required section."""
        return any(r.action for r in self.brings)

    @property
    def upgrades_database(self) -> bool:
        return any(r.upgrades_database for r in self.brings)


def _cfg():
    return current_app.extensions["officesentry.config"]


def offer(
    current: updates.Version | None,
    newer: list[updates.Release],
    state: updates.Status,
    waiting: updates.Request | None,
    public_demo: bool = False,
    docker: bool = True,
) -> Offer:
    if current is None:
        return Offer(None, reason="development")
    if not newer:
        return Offer(None, reason="current")
    # The button only installs a release of the running major version; a new major version is updated by hand.
    same = [r for r in newer if r.parsed.major == current.major]
    target = same[0] if same else newer[-1]
    plan = Offer(target, [r for r in newer if r.parsed <= target.parsed])
    if public_demo:
        plan.reason = "demo"
    elif not same:
        plan.reason = "major"
    elif waiting or state.running_job:
        plan.reason = "running"
    elif not state.connected:
        plan.reason = "updater_gone" if state.seen_at else ("updater_off" if docker else "no_docker")
    elif not state.ready:
        plan.reason = "not_ready"
    return plan


def _signature(state: updates.Status, waiting: updates.Request | None) -> str:
    """Changes whenever the progress shown does, so the page knows to redraw."""
    job = state.job
    if waiting and not (job and job.id == waiting.id):
        return f"waiting:{waiting.id}"
    if not job:
        return "none"
    steps = ",".join(f"{s.key}={s.state}:{len(s.detail)}" for s in job.steps)
    return f"{job.id}:{job.state}:{job.restarted_updater}:{steps}"


@bp.get("/updates")
@needs("system.update")
def index():
    cfg = _cfg()
    now = datetime.now(UTC)
    current = updates.running()
    state = updates.status(cfg.data_dir, now)
    waiting = updates.waiting(cfg.data_dir, state, now)
    newer = updates.newer(g.db, current)
    docker = in_docker()
    plan = offer(current, newer, state, waiting, public_demo=cfg.public_demo, docker=docker)
    job = state.job
    # A request from the last day that the updater never picked up (it isn't running, say).
    asked = updates.read_request(cfg.data_dir)
    at = parse_time(asked.at) if asked else None
    missed = asked if at and now - at < RECENT and not waiting and not (job and job.id == asked.id) else None
    ended = parse_time(job.finished_at) if job and job.finished else None
    busy = bool(waiting or state.running_job)
    return render_template(
        "updates.html",
        version=__version__,
        state=state,
        job=job,
        busy=busy,
        update_running=busy,  # base.html: the worker is restarting, which the steps already say
        just_finished=bool(ended and now - ended < RECENT),
        waiting=waiting,
        missed=missed,
        plan=plan,
        later=[r for r in newer if plan.target and r.parsed > plan.target.parsed],
        notes=lambda text, release: render(text, release.version),
        check=updates.check_state(g.db),
        signature=_signature(state, waiting),
        docker=docker,
        releases_page=updates.RELEASES_PAGE,
        deploy_updating=DEPLOY_UPDATING,
        deploy_button=DEPLOY_BUTTON,
        deploy_rollback=DEPLOY_ROLLBACK,
        deploy_guides=DEPLOY_GUIDES,
    )


def in_docker() -> bool:
    return Path("/.dockerenv").exists()


@bp.get("/updates/progress")
@needs("system.update")
def progress():
    """For the page's script: whether what it shows is still current. Answered by whichever portal is
    running, so it carries on across the restart."""
    cfg = _cfg()
    now = datetime.now(UTC)
    state = updates.status(cfg.data_dir, now)
    waiting = updates.waiting(cfg.data_dir, state, now)
    return jsonify(
        signature=_signature(state, waiting),
        version=__version__,
        busy=bool(waiting or state.running_job),
    )


@bp.post("/updates/start")
@needs("system.update")
def start():
    cfg = _cfg()
    now = datetime.now(UTC)
    current = updates.running()
    state = updates.status(cfg.data_dir, now)
    waiting = updates.waiting(cfg.data_dir, state, now)
    plan = offer(current, updates.newer(g.db, current), state, waiting, cfg.public_demo, in_docker())
    wanted = updates.Version.parse(request.form.get("version"))
    if wanted is None:
        abort(400)
    if not plan.button or plan.target.parsed != wanted:
        flash("That update can't be started now; this page says why.", "error")
        return redirect(url_for("updates.index"))
    if plan.asks and request.form.get("read") != "1":
        flash("Tick that you've read Action required first.", "error")
        return redirect(url_for("updates.index"))
    updates.request_update(cfg.data_dir, wanted, g.user["username"], now)
    with transaction(g.db):
        audit(g.db, g.user["username"], "update_requested", detail=f"{current} to {wanted}")
    return redirect(url_for("updates.index"))


@bp.post("/updates/check")
@needs("system.update")
def check_now():
    try:
        found = updates.check(g.db)
    except updates.CheckFailed as exc:
        flash(f"Couldn't check for new releases: {exc}", "error")
    else:
        latest = updates.available(g.db)
        if latest:
            flash(f"Office Sentry {latest.version} is available.", "success")
        elif found:
            flash(f"Office Sentry {__version__} is the newest release.", "success")
        else:
            flash("GitHub lists no releases yet.", "success")
    return redirect(url_for("updates.index"))


@bp.post("/updates/settings")
@needs("system.update")
def settings():
    on = request.form.get("check") == "on"
    with transaction(g.db):
        if updates.checking(g.db) != on:
            updates.set_checking(g.db, on)
            audit(g.db, g.user["username"], "update_check_changed", detail="on" if on else "off")
    flash("Office Sentry checks for new releases once a day." if on else "The daily check for releases is off.")
    return redirect(url_for("updates.index"))
