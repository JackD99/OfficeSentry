"""Microsoft Secure Score, with the daily history Microsoft still holds."""

from __future__ import annotations

from .base import Collector, RunContext, register


@register
class SecureScore(Collector):
    key = "secure_score"
    title = "Secure Score"
    description = "Current Secure Score, per-control scores and up to 90 days of daily history."
    permissions = ("SecurityEvents.Read.All",)

    def collect(self, ctx: RunContext) -> None:
        # One snapshot per day, newest first. A single page of 90 covers what
        # Microsoft keeps; older history lives in our own metrics.
        scores = ctx.graph.get("/security/secureScores", params={"$top": "90"}).get("value", [])
        if not scores:
            ctx.warn("Microsoft returned no Secure Score for this tenant yet")
            return
        latest = scores[0]
        daily = [(s["createdDateTime"][:10], {
            "date": s["createdDateTime"][:10],
            "currentScore": s.get("currentScore"),
            "maxScore": s.get("maxScore"),
        }) for s in scores]
        ctx.objects("secure_score_daily", daily)
        controls = [(c.get("controlName"), {
            "controlName": c.get("controlName"),
            "controlCategory": c.get("controlCategory"),
            "score": c.get("score"),
            "scoreInPercentage": c.get("scoreInPercentage"),
            "implementationStatus": c.get("implementationStatus"),
            "description": c.get("description"),
        }) for c in latest.get("controlScores", []) if c.get("controlName")]
        ctx.objects("secure_score_control", controls)

        current, maximum = latest.get("currentScore") or 0, latest.get("maxScore") or 0
        ctx.metric("secure_score.current", current)
        ctx.metric("secure_score.max", maximum)
        if maximum:
            ctx.metric("secure_score.pct", round(100 * current / maximum, 1))
