"""
Funnel Analyst - monitors site-wide conversion funnel across rolling periods.

Reads:  metrics_funnel_weekly, metrics_channel_weekly
Writes: findings (module='funnel')
"""

from datetime import datetime

from analysts import _write_findings
from analysts.period_aggregator import get_channel_period, get_funnel_period, safe_pct_change

FINDING_MODULE = "funnel"

STAGE_DROP_THRESHOLD_PCT = 10.0
CHANNEL_SESSION_DROP_PCT = 20.0
CHANNEL_MIN_SESSIONS = 50
NEW_RETURNING_DIVERGE_PCT = 20.0


def _as_float(value):
    """Return float(value) unless the value is None."""
    if value is None:
        return None
    return float(value)


def _comparison_label(period_weeks: int) -> str:
    return "WoW" if period_weeks == 1 else f"vs prior {period_weeks}-week period"


def _base_evidence(completeness: dict, period_weeks: int) -> dict:
    return {
        "period_weeks": period_weeks,
        "period_start": completeness["window"]["current_start"],
        "period_end": completeness["window"]["current_end"],
        "partial_period": completeness["current_partial"],
    }


def _attach_yoy(evidence: dict, current_value, yoy_value, completeness: dict):
    if completeness["yoy_empty"] or yoy_value is None:
        return
    evidence["yoy_change_pct"] = safe_pct_change(current_value, yoy_value)
    evidence["yoy_partial"] = completeness["yoy_partial"]


def _stage_cause(stage_key, pct_change):
    if pct_change > 0:
        return "Improvement in funnel conversion at this stage."
    causes = {
        "session_to_pdp_rate": "Traffic quality shift - visitors are landing without strong product-page intent.",
        "pdp_to_atc_rate": "Product listing issues - pricing, imagery, or content are not converting visits into cart adds.",
        "atc_to_checkout_rate": "Cart friction - shipping, promo code confusion, or account/login barriers.",
        "checkout_to_purchase_rate": "Checkout abandonment - payment failure, unexpected charges, or trust issues.",
        "overall_conversion_rate": "Site-wide conversion softened across the full funnel.",
    }
    return causes.get(stage_key, "Funnel stage conversion decreased.")


def _stage_action(stage_key, pct_change):
    if pct_change > 0:
        return "Monitor to confirm the improvement holds next period."
    actions = {
        "session_to_pdp_rate": "Check top landing pages and traffic sources for shifts in audience quality.",
        "pdp_to_atc_rate": "Review pricing, product images, and listing content on high-traffic PDPs.",
        "atc_to_checkout_rate": "Review the cart for friction - shipping calculator, promo code field, login walls.",
        "checkout_to_purchase_rate": "Check payment gateway errors and review checkout for unexpected charges.",
        "overall_conversion_rate": "Review the full funnel to isolate where the period-over-period loss started.",
    }
    return actions.get(stage_key, "Investigate the funnel stage for issues.")


def find_funnel_stage_drops(db_conn, period_ending, period_weeks: int) -> list[dict]:
    """Compare each funnel stage rate vs the prior rolling period."""
    result = get_funnel_period(db_conn, period_ending, period_weeks)
    current = result["current"]
    prior = result["prior"]
    yoy = result["yoy"]
    completeness = result["completeness"]

    if current is None:
        print("  [funnel] WARNING: No funnel data in current period window")
        return []
    if prior is None:
        print("  [funnel] No prior period data - skipping stage drops")
        return []

    stages = [
        ("session_to_pdp_rate", "Session -> PDP", "medium"),
        ("pdp_to_atc_rate", "PDP -> Add-to-Cart", "medium"),
        ("atc_to_checkout_rate", "Add-to-Cart -> Checkout", "medium"),
        ("checkout_to_purchase_rate", "Checkout -> Purchase", "high"),
        ("overall_conversion_rate", "Overall Conversion", "high"),
    ]

    findings = []
    for stage_key, stage_label, severity in stages:
        current_rate = current.get(stage_key)
        prior_rate = prior.get(stage_key)
        if (current_rate is not None and current_rate > 1.0) or (prior_rate is not None and prior_rate > 1.0):
            continue
        pct_change = safe_pct_change(current_rate, prior_rate)
        if pct_change is None or abs(pct_change) <= STAGE_DROP_THRESHOLD_PCT:
            continue

        evidence = {
            **_base_evidence(completeness, period_weeks),
            "stage": stage_key,
            "current_rate": _as_float(current_rate),
            "prior_rate": _as_float(prior_rate),
            "pct_change": round(pct_change, 1),
            "current_sessions": current.get("sessions"),
            "current_revenue": _as_float(current.get("revenue")),
            "threshold_pct": STAGE_DROP_THRESHOLD_PCT,
            "comparison_basis": _comparison_label(period_weeks),
        }
        if yoy:
            _attach_yoy(evidence, current_rate, yoy.get(stage_key), completeness)

        findings.append({
            "week_ending": period_ending.isoformat(),
            "period_weeks": period_weeks,
            "module": FINDING_MODULE,
            "finding_type": "issue" if pct_change < 0 else "positive",
            "severity": severity,
            "title": (
                f"{stage_label} rate {'dropped' if pct_change < 0 else 'improved'} "
                f"{abs(pct_change):.1f}% {_comparison_label(period_weeks)} "
                f"({_as_float(prior_rate):.1%} -> {_as_float(current_rate):.1%})"
            ),
            "evidence": evidence,
            "likely_cause": _stage_cause(stage_key, pct_change),
            "suggested_action": _stage_action(stage_key, pct_change),
            "urgency": "this_week" if severity == "high" and pct_change < 0 else "monitor",
        })

    return findings


def find_new_vs_returning_divergence(db_conn, period_ending, period_weeks: int) -> list[dict]:
    """Flag if new-user and returning-user sessions diverge across rolling periods."""
    result = get_funnel_period(db_conn, period_ending, period_weeks)
    current = result["current"]
    prior = result["prior"]
    completeness = result["completeness"]

    if current is None or prior is None:
        return []

    new_change = safe_pct_change(current.get("new_user_sessions"), prior.get("new_user_sessions"))
    returning_change = safe_pct_change(
        current.get("returning_user_sessions"), prior.get("returning_user_sessions")
    )
    if new_change is None or returning_change is None:
        return []

    divergence = abs(new_change - returning_change)
    if divergence <= NEW_RETURNING_DIVERGE_PCT:
        return []

    return [{
        "week_ending": period_ending.isoformat(),
        "period_weeks": period_weeks,
        "module": FINDING_MODULE,
        "finding_type": "alert",
        "severity": "medium",
        "title": (
            f"New vs returning traffic diverging - new users "
            f"{'down' if new_change < 0 else 'up'} {abs(new_change):.0f}%, returning "
            f"{'down' if returning_change < 0 else 'up'} {abs(returning_change):.0f}% "
            f"{_comparison_label(period_weeks)}"
        ),
        "evidence": {
            **_base_evidence(completeness, period_weeks),
            "new_user_sessions": current.get("new_user_sessions"),
            "returning_user_sessions": current.get("returning_user_sessions"),
            "prior_new_user_sessions": prior.get("new_user_sessions"),
            "prior_returning_user_sessions": prior.get("returning_user_sessions"),
            "new_change_pct": round(new_change, 1),
            "returning_change_pct": round(returning_change, 1),
            "divergence_ppts": round(divergence, 1),
            "threshold_ppts": NEW_RETURNING_DIVERGE_PCT,
            "comparison_basis": _comparison_label(period_weeks),
        },
        "likely_cause": (
            "Acquisition and retention are moving in different directions. One side of the funnel likely changed "
            "more than the other."
        ),
        "suggested_action": "Cross-reference with channel anomalies to identify which traffic source changed.",
        "urgency": "monitor",
    }]


def find_channel_anomalies(db_conn, period_ending, period_weeks: int) -> list[dict]:
    """Flag channels with significant change in sessions across rolling periods."""
    result = get_channel_period(db_conn, period_ending, period_weeks)
    current = result["current"] or {}
    prior = result["prior"] or {}
    yoy = result["yoy"] or {}
    completeness = result["completeness"]

    high_impact_channels = {"Organic Search", "Paid Search", "Direct"}
    min_sessions = CHANNEL_MIN_SESSIONS * period_weeks
    findings = []

    for channel_group, current_row in current.items():
        if (current_row.get("sessions") or 0) < min_sessions:
            continue

        prior_row = prior.get(channel_group)
        sessions_change = (
            safe_pct_change(current_row.get("sessions"), prior_row.get("sessions"))
            if prior_row else None
        )
        if sessions_change is None or abs(sessions_change) <= CHANNEL_SESSION_DROP_PCT:
            continue

        negative = sessions_change < 0
        if negative:
            severity = "high" if channel_group in high_impact_channels else "medium"
            finding_type = "issue"
            urgency = "this_week" if severity == "high" else "monitor"
        else:
            severity = "low"
            finding_type = "positive"
            urgency = "backlog"

        if "Organic" in channel_group:
            cause_hint = "algorithm update, ranking shift, or SERP behavior change"
        elif "Paid" in channel_group:
            cause_hint = "budget, bid, or targeting changes"
        else:
            cause_hint = "seasonal demand or campaign mix shift"

        evidence = {
            **_base_evidence(completeness, period_weeks),
            "channel_group": channel_group,
            "sessions": current_row.get("sessions"),
            "prior_sessions": prior_row.get("sessions") if prior_row else None,
            "sessions_change_pct": _as_float(sessions_change),
            "revenue": _as_float(current_row.get("revenue")) or 0.0,
            "revenue_change_pct": (
                safe_pct_change(current_row.get("revenue"), prior_row.get("revenue"))
                if prior_row else None
            ),
            "conversion_rate": _as_float(current_row.get("conversion_rate")),
            "threshold_pct": CHANNEL_SESSION_DROP_PCT,
            "comparison_basis": _comparison_label(period_weeks),
        }
        yoy_row = yoy.get(channel_group)
        if yoy_row:
            _attach_yoy(evidence, current_row.get("sessions"), yoy_row.get("sessions"), completeness)

        findings.append({
            "week_ending": period_ending.isoformat(),
            "period_weeks": period_weeks,
            "module": FINDING_MODULE,
            "finding_type": finding_type,
            "severity": severity,
            "title": (
                f"{channel_group} sessions {'dropped' if negative else 'grew'} "
                f"{abs(float(sessions_change)):.0f}% {_comparison_label(period_weeks)} "
                f"({prior_row.get('sessions') if prior_row else 0} -> {current_row.get('sessions')})"
            ),
            "evidence": evidence,
            "likely_cause": (
                f"{channel_group} traffic {'declined' if negative else 'increased'} significantly. "
                f"Check for {cause_hint}."
            ),
            "suggested_action": f"Investigate {channel_group} for upstream changes - {cause_hint}.",
            "urgency": urgency,
        })

    return findings


def run(db_conn, week_ending: str, period_weeks: int = 4) -> int:
    """Run all funnel analysis and write findings. Returns finding count."""
    period_ending = datetime.strptime(week_ending, "%Y-%m-%d").date()

    findings = []
    findings.extend(find_funnel_stage_drops(db_conn, period_ending, period_weeks) or [])
    findings.extend(find_new_vs_returning_divergence(db_conn, period_ending, period_weeks) or [])
    findings.extend(find_channel_anomalies(db_conn, period_ending, period_weeks) or [])

    if len(findings) > 15:
        severity_order = {"high": 0, "medium": 1, "low": 2}
        type_order = {"issue": 0, "alert": 1, "opportunity": 2, "positive": 3}
        findings.sort(
            key=lambda finding: (
                type_order.get(finding["finding_type"], 4),
                severity_order.get(finding["severity"], 3),
            )
        )
        findings = findings[:15]

    count = _write_findings(db_conn, findings, FINDING_MODULE, week_ending, period_weeks=period_weeks)
    print(f"  [funnel] {count} findings written for {week_ending} ({period_weeks}-week window)")
    return count
