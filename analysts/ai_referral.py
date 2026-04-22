"""
AI Referral Analyst - summarizes traffic and revenue from AI assistant platforms.

Reads:  metrics_ai_referral_weekly, metrics_funnel_weekly
Writes: findings (module='ai_referral')
"""

from datetime import datetime

from analysts import _write_findings
from analysts.period_aggregator import get_ai_referral_period, get_funnel_period, safe_pct_change, safe_rate

FINDING_MODULE = "ai_referral"

GROWTH_THRESHOLD_PCT = 20.0
MIN_SESSIONS_TOP_REFERRER = 5


def _base_evidence(completeness: dict, period_weeks: int) -> dict:
    return {
        "period_weeks": period_weeks,
        "period_start": completeness["window"]["current_start"],
        "period_end": completeness["window"]["current_end"],
        "partial_period": completeness["current_partial"],
    }


def summarize_ai_traffic(db_conn, period_ending, period_weeks: int) -> list[dict]:
    """Produce a rolling-period AI referral summary finding."""
    referral_result = get_ai_referral_period(db_conn, period_ending, period_weeks)
    funnel_result = get_funnel_period(db_conn, period_ending, period_weeks)

    current = referral_result["current"] or {}
    prior = referral_result["prior"] or {}
    yoy = referral_result["yoy"] or {}
    completeness = referral_result["completeness"]
    funnel_current = funnel_result["current"]

    total_sessions = sum(row.get("sessions", 0) for row in current.values())
    total_revenue = sum(float(row.get("revenue") or 0) for row in current.values())
    total_conversions = sum(row.get("conversions", 0) for row in current.values())

    prior_sessions = sum(row.get("sessions", 0) for row in prior.values())
    prior_revenue = sum(float(row.get("revenue") or 0) for row in prior.values())
    yoy_sessions = sum(row.get("sessions", 0) for row in yoy.values()) if yoy else None

    top_referrer = None
    min_top_referrer = MIN_SESSIONS_TOP_REFERRER * period_weeks
    top_candidates = [row for row in current.values() if (row.get("sessions") or 0) >= min_top_referrer]
    if top_candidates:
        top_referrer = max(top_candidates, key=lambda row: row.get("sessions", 0))

    site_sessions = funnel_current.get("sessions") if funnel_current else None
    site_conversion_rate = funnel_current.get("overall_conversion_rate") if funnel_current else None
    ai_conversion_rate = safe_rate(total_conversions, total_sessions)
    ai_session_share = safe_rate(total_sessions, site_sessions)
    sessions_change = safe_pct_change(total_sessions, prior_sessions)

    evidence = {
        **_base_evidence(completeness, period_weeks),
        "total_sessions": total_sessions,
        "total_revenue": total_revenue,
        "total_conversions": total_conversions,
        "ai_conversion_rate": ai_conversion_rate,
        "site_conversion_rate": site_conversion_rate,
        "ai_sessions_share_of_site": ai_session_share,
        "prior_sessions": prior_sessions if prior_sessions > 0 else None,
        "prior_revenue": prior_revenue if prior_revenue > 0 else None,
        "sessions_change_pct": round(sessions_change, 1) if sessions_change is not None else None,
    }

    if top_referrer:
        evidence["top_referrer_domain"] = top_referrer.get("referrer_domain")
        evidence["top_referrer_label"] = top_referrer.get("referrer_label")
        evidence["top_referrer_sessions"] = top_referrer.get("sessions")
        evidence["top_referrer_revenue"] = float(top_referrer.get("revenue") or 0)
        evidence["top_referrer_conversion_rate"] = top_referrer.get("conversion_rate")

    if not completeness["yoy_empty"] and yoy_sessions is not None:
        evidence["yoy_change_pct"] = safe_pct_change(total_sessions, yoy_sessions)
        evidence["yoy_partial"] = completeness["yoy_partial"]

    if total_sessions == 0:
        return [{
            "week_ending": period_ending.isoformat(),
            "period_weeks": period_weeks,
            "module": FINDING_MODULE,
            "finding_type": "alert",
            "severity": "low",
            "title": "No AI referral traffic detected in this period",
            "evidence": evidence,
            "likely_cause": "LBS is not yet being cited in AI assistant responses, or AI users are not clicking through.",
            "suggested_action": (
                "Monitor the trend. Consider AEO work on high-intent product pages once the rest of the report is stable."
            ),
            "urgency": "backlog",
        }]

    if sessions_change is not None and sessions_change > GROWTH_THRESHOLD_PCT:
        finding_type = "opportunity"
        severity = "medium"
        urgency = "monitor"
    elif sessions_change is not None and sessions_change < -GROWTH_THRESHOLD_PCT:
        finding_type = "alert"
        severity = "medium"
        urgency = "monitor"
    else:
        finding_type = "alert"
        severity = "low"
        urgency = "backlog"

    if sessions_change is not None:
        change_suffix = f", {'up' if sessions_change > 0 else 'down'} {abs(sessions_change):.0f}% vs prior period"
    else:
        change_suffix = ""

    if sessions_change is not None and sessions_change > 0:
        likely_cause = "AI assistants are sending more traffic to LBS than in the prior period."
    elif sessions_change is not None and sessions_change < 0:
        likely_cause = "AI referral traffic softened. Citation patterns or user click-through may have changed."
    else:
        likely_cause = "AI referral traffic is present but still small and early-stage."

    if total_sessions > 10:
        suggested_action = (
            "Track the trend and inspect which product pages attract AI traffic. Optimize those pages for citation quality."
        )
    else:
        suggested_action = "Monitor the trend. AI referral is still a small but strategically useful signal."

    return [{
        "week_ending": period_ending.isoformat(),
        "period_weeks": period_weeks,
        "module": FINDING_MODULE,
        "finding_type": finding_type,
        "severity": severity,
        "title": (
            f"AI referral traffic: {total_sessions} sessions, ${total_revenue:,.0f} revenue{change_suffix}"
        ),
        "evidence": evidence,
        "likely_cause": likely_cause,
        "suggested_action": suggested_action,
        "urgency": urgency,
    }]


def run(db_conn, week_ending: str, period_weeks: int = 4) -> int:
    """Run AI referral analysis and write findings. Returns finding count."""
    period_ending = datetime.strptime(week_ending, "%Y-%m-%d").date()

    findings = []
    findings.extend(summarize_ai_traffic(db_conn, period_ending, period_weeks) or [])

    count = _write_findings(db_conn, findings, FINDING_MODULE, week_ending, period_weeks=period_weeks)
    print(f"  [ai_referral] {count} findings written for {week_ending} ({period_weeks}-week window)")
    return count
