"""
Paid Media Analyst - monitors Google Ads performance in business terms.

Reads:  metrics_paid_weekly
Writes: findings (module='paid_media')
"""

from datetime import datetime

from analysts import _write_findings
from analysts.period_aggregator import get_paid_period, safe_pct_change

FINDING_MODULE = "paid_media"

ROAS_DROP_PCT = -20.0
SPEND_CUT_THRESHOLD = -10.0
IS_LOSS_RANK_THRESHOLD = 0.15
IS_LOSS_BUDGET_THRESHOLD = 0.10
IS_WOW_DROP_PCT = -8.0
SPEND_SPIKE_PCT = 30.0
ROAS_LIFT_THRESHOLD = 10.0


def _as_float(value):
    """Return float(value) unless the value is None."""
    if value is None:
        return None
    return float(value)


def _comparison_label(period_weeks: int) -> str:
    return "WoW" if period_weeks == 1 else f"vs prior {period_weeks}-week period"


def _base_evidence(completeness: dict, period_weeks: int, current_row: dict) -> dict:
    return {
        "period_weeks": period_weeks,
        "period_start": completeness["window"]["current_start"],
        "period_end": completeness["window"]["current_end"],
        "partial_period": completeness["current_partial"],
        "partial_campaign": (current_row.get("weeks_present") or 0) < period_weeks,
        "impression_share_weighted_by": "impressions",
    }


def _attach_yoy(evidence: dict, current_value, yoy_value, completeness: dict):
    if completeness["yoy_empty"] or yoy_value is None:
        return
    evidence["yoy_change_pct"] = safe_pct_change(current_value, yoy_value)
    evidence["yoy_partial"] = completeness["yoy_partial"]


def find_roas_drops(db_conn, period_ending, period_weeks: int) -> list[dict]:
    """Flag campaigns with significant ROAS decline where spend was not meaningfully reduced."""
    result = get_paid_period(db_conn, period_ending, period_weeks)
    current = result["current"] or {}
    prior = result["prior"] or {}
    yoy = result["yoy"] or {}
    completeness = result["completeness"]

    findings = []
    for campaign_id, current_row in current.items():
        prior_row = prior.get(campaign_id)
        if not prior_row:
            continue

        roas_change = safe_pct_change(current_row.get("roas"), prior_row.get("roas"))
        spend_change = safe_pct_change(current_row.get("spend"), prior_row.get("spend"))
        if roas_change is None or roas_change > ROAS_DROP_PCT:
            continue
        if spend_change is not None and spend_change < SPEND_CUT_THRESHOLD:
            continue

        evidence = {
            **_base_evidence(completeness, period_weeks, current_row),
            "campaign_id": campaign_id,
            "campaign_name": current_row.get("campaign_name"),
            "spend": _as_float(current_row.get("spend")),
            "roas": _as_float(current_row.get("roas")),
            "prior_spend": _as_float(prior_row.get("spend")),
            "prior_roas": _as_float(prior_row.get("roas")),
            "roas_change_pct": _as_float(roas_change),
            "spend_change_pct": _as_float(spend_change),
            "conversions": _as_float(current_row.get("conversions")),
            "conversion_value": _as_float(current_row.get("conversion_value")),
            "cpc": _as_float(current_row.get("cpc")),
            "threshold_roas_drop_pct": ROAS_DROP_PCT,
            "comparison_basis": _comparison_label(period_weeks),
        }
        yoy_row = yoy.get(campaign_id)
        if yoy_row:
            _attach_yoy(evidence, current_row.get("roas"), yoy_row.get("roas"), completeness)

        findings.append({
            "week_ending": period_ending.isoformat(),
            "period_weeks": period_weeks,
            "module": FINDING_MODULE,
            "finding_type": "issue",
            "severity": "high",
            "title": (
                f"{current_row.get('campaign_name')} - ROAS dropped {abs(float(roas_change)):.0f}% "
                f"{_comparison_label(period_weeks)} ({_as_float(prior_row.get('roas')):.2f} -> "
                f"{_as_float(current_row.get('roas')):.2f}) while spend "
                f"{'grew' if (spend_change or 0) > 0 else 'held steady'}"
            ),
            "evidence": evidence,
            "likely_cause": (
                "Conversion quality is degrading - similar spend is producing fewer or lower-value conversions."
            ),
            "suggested_action": (
                f"Review the search terms report for {current_row.get('campaign_name')}. Check for irrelevant "
                "queries, landing-page issues, or audience drift."
            ),
            "urgency": "this_week",
        })

    return findings


def find_impression_share_loss(db_conn, period_ending, period_weeks: int) -> list[dict]:
    """Flag campaigns with notable impression-share loss, split by likely cause."""
    result = get_paid_period(db_conn, period_ending, period_weeks)
    current = result["current"] or {}
    prior = result["prior"] or {}
    yoy = result["yoy"] or {}
    completeness = result["completeness"]

    findings = []
    produced_campaign_ids = set()

    for campaign_id, current_row in current.items():
        prior_row = prior.get(campaign_id)
        yoy_row = yoy.get(campaign_id)
        lost_is_rank = current_row.get("avg_search_lost_is_rank")
        lost_is_budget = current_row.get("avg_search_lost_is_budget")
        impression_share = current_row.get("avg_search_impression_share")

        rank_trigger = lost_is_rank is not None and float(lost_is_rank) > IS_LOSS_RANK_THRESHOLD
        budget_trigger = lost_is_budget is not None and float(lost_is_budget) > IS_LOSS_BUDGET_THRESHOLD
        if not rank_trigger and not budget_trigger:
            continue

        produced_campaign_ids.add(campaign_id)
        common_evidence = {
            **_base_evidence(completeness, period_weeks, current_row),
            "campaign_id": campaign_id,
            "campaign_name": current_row.get("campaign_name"),
            "avg_search_impression_share": _as_float(impression_share),
            "avg_search_lost_is_rank": _as_float(lost_is_rank),
            "avg_search_lost_is_budget": _as_float(lost_is_budget),
            "impression_share_change_pct": (
                safe_pct_change(impression_share, prior_row.get("avg_search_impression_share"))
                if prior_row else None
            ),
            "spend": _as_float(current_row.get("spend")),
            "impressions": current_row.get("impressions"),
            "comparison_basis": _comparison_label(period_weeks),
        }

        if rank_trigger:
            evidence = {
                **common_evidence,
                "loss_type": "rank",
                "threshold": IS_LOSS_RANK_THRESHOLD,
            }
            if yoy_row:
                _attach_yoy(evidence, impression_share, yoy_row.get("avg_search_impression_share"), completeness)
            findings.append({
                "week_ending": period_ending.isoformat(),
                "period_weeks": period_weeks,
                "module": FINDING_MODULE,
                "finding_type": "issue",
                "severity": "medium",
                "title": (
                    f"{current_row.get('campaign_name')} - losing {float(lost_is_rank):.0%} of impressions "
                    "to ad rank"
                ),
                "evidence": evidence,
                "likely_cause": "Low Quality Score or insufficient bids are limiting visibility.",
                "suggested_action": (
                    f"Review Quality Scores and bids for {current_row.get('campaign_name')}. Improve ad relevance "
                    "and landing-page alignment."
                ),
                "urgency": "monitor",
            })

        if budget_trigger:
            evidence = {
                **common_evidence,
                "loss_type": "budget",
                "threshold": IS_LOSS_BUDGET_THRESHOLD,
            }
            if yoy_row:
                _attach_yoy(evidence, impression_share, yoy_row.get("avg_search_impression_share"), completeness)
            findings.append({
                "week_ending": period_ending.isoformat(),
                "period_weeks": period_weeks,
                "module": FINDING_MODULE,
                "finding_type": "issue",
                "severity": "medium",
                "title": (
                    f"{current_row.get('campaign_name')} - losing {float(lost_is_budget):.0%} of impressions "
                    "to budget"
                ),
                "evidence": evidence,
                "likely_cause": "Daily budget is capping reach before demand is exhausted.",
                "suggested_action": (
                    f"Increase budget for {current_row.get('campaign_name')} or reallocate spend from lower-return "
                    "campaigns."
                ),
                "urgency": "monitor",
            })

    for campaign_id, current_row in current.items():
        if campaign_id in produced_campaign_ids:
            continue

        prior_row = prior.get(campaign_id)
        if not prior_row:
            continue

        impression_share_change = safe_pct_change(
            current_row.get("avg_search_impression_share"),
            prior_row.get("avg_search_impression_share"),
        )
        if impression_share_change is None or impression_share_change >= IS_WOW_DROP_PCT:
            continue

        evidence = {
            **_base_evidence(completeness, period_weeks, current_row),
            "campaign_id": campaign_id,
            "campaign_name": current_row.get("campaign_name"),
            "avg_search_impression_share": _as_float(current_row.get("avg_search_impression_share")),
            "avg_search_lost_is_rank": _as_float(current_row.get("avg_search_lost_is_rank")),
            "avg_search_lost_is_budget": _as_float(current_row.get("avg_search_lost_is_budget")),
            "impression_share_change_pct": _as_float(impression_share_change),
            "spend": _as_float(current_row.get("spend")),
            "impressions": current_row.get("impressions"),
            "loss_type": "share_decline",
            "threshold": IS_WOW_DROP_PCT,
            "comparison_basis": _comparison_label(period_weeks),
        }
        yoy_row = yoy.get(campaign_id)
        if yoy_row:
            _attach_yoy(
                evidence,
                current_row.get("avg_search_impression_share"),
                yoy_row.get("avg_search_impression_share"),
                completeness,
            )

        findings.append({
            "week_ending": period_ending.isoformat(),
            "period_weeks": period_weeks,
            "module": FINDING_MODULE,
            "finding_type": "issue",
            "severity": "medium",
            "title": (
                f"{current_row.get('campaign_name')} - impression share dropped "
                f"{abs(float(impression_share_change)):.0f}% {_comparison_label(period_weeks)} "
                f"to {float(current_row.get('avg_search_impression_share')):.0%}"
            ),
            "evidence": evidence,
            "likely_cause": "Auction pressure or campaign constraints reduced visibility across the period.",
            "suggested_action": (
                f"Review impression share trend, auction pressure, and campaign settings for "
                f"{current_row.get('campaign_name')}."
            ),
            "urgency": "monitor",
        })

    return findings


def find_spend_anomalies(db_conn, period_ending, period_weeks: int) -> list[dict]:
    """Flag spend spikes without proportional return improvement."""
    result = get_paid_period(db_conn, period_ending, period_weeks)
    current = result["current"] or {}
    prior = result["prior"] or {}
    yoy = result["yoy"] or {}
    completeness = result["completeness"]

    findings = []
    for campaign_id, current_row in current.items():
        prior_row = prior.get(campaign_id)
        if not prior_row:
            continue

        spend_change = safe_pct_change(current_row.get("spend"), prior_row.get("spend"))
        roas_change = safe_pct_change(current_row.get("roas"), prior_row.get("roas"))
        if spend_change is None or spend_change < SPEND_SPIKE_PCT:
            continue
        if roas_change is not None and roas_change > ROAS_LIFT_THRESHOLD:
            continue

        evidence = {
            **_base_evidence(completeness, period_weeks, current_row),
            "campaign_id": campaign_id,
            "campaign_name": current_row.get("campaign_name"),
            "spend": _as_float(current_row.get("spend")),
            "prior_spend": _as_float(prior_row.get("spend")),
            "spend_change_pct": _as_float(spend_change),
            "roas": _as_float(current_row.get("roas")),
            "roas_change_pct": _as_float(roas_change),
            "conversions": _as_float(current_row.get("conversions")),
            "conversion_value": _as_float(current_row.get("conversion_value")),
            "threshold_spend_spike_pct": SPEND_SPIKE_PCT,
            "threshold_roas_lift_pct": ROAS_LIFT_THRESHOLD,
            "comparison_basis": _comparison_label(period_weeks),
        }
        yoy_row = yoy.get(campaign_id)
        if yoy_row:
            _attach_yoy(evidence, current_row.get("spend"), yoy_row.get("spend"), completeness)

        findings.append({
            "week_ending": period_ending.isoformat(),
            "period_weeks": period_weeks,
            "module": FINDING_MODULE,
            "finding_type": "alert",
            "severity": "medium",
            "title": (
                f"{current_row.get('campaign_name')} - spend up {float(spend_change):.0f}% "
                f"{_comparison_label(period_weeks)} (${_as_float(prior_row.get('spend')):.0f} -> "
                f"${_as_float(current_row.get('spend')):.0f}) without proportional return"
            ),
            "evidence": evidence,
            "likely_cause": (
                "Budget or bids increased faster than conversion quality or demand."
            ),
            "suggested_action": (
                f"Confirm the spend increase for {current_row.get('campaign_name')} was intentional. If not, "
                "check bid strategy changes or new keyword expansion."
            ),
            "urgency": "this_week" if float(spend_change) > 50 else "monitor",
        })

    return findings


def run(db_conn, week_ending: str, period_weeks: int = 4) -> int:
    """Run all paid media analysis and write findings. Returns finding count."""
    period_ending = datetime.strptime(week_ending, "%Y-%m-%d").date()

    findings = []
    findings.extend(find_roas_drops(db_conn, period_ending, period_weeks) or [])
    findings.extend(find_impression_share_loss(db_conn, period_ending, period_weeks) or [])
    findings.extend(find_spend_anomalies(db_conn, period_ending, period_weeks) or [])

    if len(findings) > 15:
        severity_order = {"high": 0, "medium": 1, "low": 2}
        findings.sort(key=lambda finding: severity_order.get(finding["severity"], 3))
        findings = findings[:15]

    count = _write_findings(db_conn, findings, FINDING_MODULE, week_ending, period_weeks=period_weeks)
    print(f"  [paid_media] {count} findings written for {week_ending} ({period_weeks}-week window)")
    return count
