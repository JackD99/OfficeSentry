"""Microsoft Secure Score, with the daily history Microsoft still holds."""

from __future__ import annotations

from datetime import date, timedelta

from ..graph import GraphError
from .base import Collector, RunContext, register

BASELINE_DAYS = 30  # control-level changes are shown against the day closest to this far back
MAX_PAGES = 100  # 90 daily scores at the smallest page Microsoft has sent is 18 pages


def baseline(scores: list[dict], days: int = BASELINE_DAYS) -> dict | None:
    """The daily score closest to ``days`` before the latest one (the oldest, if history is shorter)."""
    if len(scores) < 2:
        return None
    latest = date.fromisoformat(scores[0]["createdDateTime"][:10])
    target = latest - timedelta(days=days)
    earlier = [s for s in scores[1:] if date.fromisoformat(s["createdDateTime"][:10]) <= target]
    return earlier[0] if earlier else scores[-1]


@register
class SecureScore(Collector):
    key = "secure_score"
    title = "Secure Score"
    description = "Current Secure Score, per-control scores and up to 90 days of daily history."
    # Control profiles give each control its title and maximum points; same permission.
    permissions = ("SecurityEvents.Read.All",)

    def collect(self, ctx: RunContext) -> None:
        # One snapshot per day, newest first: the 90 days Microsoft keeps, however it pages them (the live
        # recording shows pages of five, whatever $top asks for); older history lives in our own metrics.
        scores = ctx.graph.get_all("/security/secureScores", params={"$top": "90"}, max_pages=MAX_PAGES).items
        if not scores:
            ctx.warn("Microsoft returned no Secure Score for this tenant yet")
            return
        latest = scores[0]
        daily = [
            (
                s["createdDateTime"][:10],
                {
                    "date": s["createdDateTime"][:10],
                    "currentScore": s.get("currentScore"),
                    "maxScore": s.get("maxScore"),
                },
            )
            for s in scores
        ]
        ctx.objects("secure_score_daily", daily)
        controls = [
            (
                c.get("controlName"),
                {
                    "controlName": c.get("controlName"),
                    "controlCategory": c.get("controlCategory"),
                    "score": c.get("score"),
                    "scoreInPercentage": c.get("scoreInPercentage"),
                    "implementationStatus": c.get("implementationStatus"),
                    "description": c.get("description"),
                },
            )
            for c in latest.get("controlScores", [])
            if c.get("controlName")
        ]
        ctx.objects("secure_score_control", controls)

        past = baseline(scores)
        if past:
            day = past["createdDateTime"][:10]
            ctx.objects(
                "secure_score_control_past",
                [
                    (
                        f"{day}:{c['controlName']}",
                        {
                            "date": day,
                            "controlName": c["controlName"],
                            "score": c.get("score"),
                        },
                    )
                    for c in past.get("controlScores", [])
                    if c.get("controlName")
                ],
            )
        try:
            profiles = ctx.paged(ctx.graph.get_all("/security/secureScoreControlProfiles"))
        except GraphError as exc:
            ctx.log(f"Control names not available: {exc.message}", "warning")
            profiles = []
        ctx.objects(
            "secure_score_control_profile",
            [
                (
                    p["id"],
                    {
                        "title": p.get("title"),
                        "maxScore": p.get("maxScore"),
                        "controlCategory": p.get("controlCategory"),
                        "service": p.get("service"),
                        "userImpact": p.get("userImpact"),
                        "implementationCost": p.get("implementationCost"),
                        "rank": p.get("rank"),
                        "deprecated": p.get("deprecated"),
                    },
                )
                for p in profiles
                if p.get("id")
            ],
        )

        current, maximum = latest.get("currentScore") or 0, latest.get("maxScore") or 0
        ctx.metric("secure_score.current", current)
        ctx.metric("secure_score.max", maximum)
        if maximum:
            ctx.metric("secure_score.pct", round(100 * current / maximum, 1))
