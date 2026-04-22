"""
Search Analyst - monitors GSC performance and surfaces keyword trends.

Reads:  metrics_search_weekly
Writes: findings (module='search')
"""

from datetime import datetime

from analysts import _write_findings
from analysts.period_aggregator import get_search_period, safe_pct_change

FINDING_MODULE = "search"

CLICK_DROP_PCT = -30.0
MIN_PRIOR_CLICKS = 5
CTR_DROP_PCT = -15.0
IMPRESSION_STABLE_PCT = -5.0
MIN_IMPRESSIONS = 50
BRANDED_SHARE_DROP_PPTS = 5.0


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


def _truncate_query(query: str, max_len: int = 60) -> str:
    """Keep long queries from blowing up finding titles."""
    if len(query) <= max_len:
        return query
    return query[: max_len - 3] + "..."


def find_click_drops(db_conn, period_ending, period_weeks: int) -> list[dict]:
    """Flag queries with significant click decline across rolling periods."""
    result = get_search_period(db_conn, period_ending, period_weeks)
    current = result["current"] or {}
    prior = result["prior"] or {}
    yoy = result["yoy"] or {}
    completeness = result["completeness"]

    min_prior_clicks = max(MIN_PRIOR_CLICKS, 5 * period_weeks)

    findings = []
    for entity_key, current_row in current.items():
        prior_row = prior.get(entity_key)
        if not prior_row or (prior_row.get("clicks") or 0) < min_prior_clicks:
            continue

        clicks_change = safe_pct_change(current_row.get("clicks"), prior_row.get("clicks"))
        if clicks_change is None or clicks_change > CLICK_DROP_PCT:
            continue

        short_query = _truncate_query(current_row.get("query"))
        is_branded = current_row.get("is_branded")
        severity = "high" if is_branded else "medium"
        urgency = "this_week" if is_branded else "monitor"

        evidence = {
            **_base_evidence(completeness, period_weeks),
            "query": current_row.get("query"),
            "page": current_row.get("page"),
            "is_branded": is_branded,
            "clicks": current_row.get("clicks"),
            "prior_clicks": prior_row.get("clicks"),
            "clicks_change_pct": _as_float(clicks_change),
            "impressions": current_row.get("impressions"),
            "ctr": _as_float(current_row.get("ctr")),
            "avg_position": _as_float(current_row.get("avg_position")),
            "threshold_pct": CLICK_DROP_PCT,
            "comparison_basis": _comparison_label(period_weeks),
        }
        yoy_row = yoy.get(entity_key)
        if yoy_row:
            _attach_yoy(evidence, current_row.get("clicks"), yoy_row.get("clicks"), completeness)

        findings.append({
            "week_ending": period_ending.isoformat(),
            "period_weeks": period_weeks,
            "module": FINDING_MODULE,
            "finding_type": "issue",
            "severity": severity,
            "title": (
                f"{'[Branded] ' if is_branded else ''}\"{short_query}\" lost "
                f"{abs(float(clicks_change)):.0f}% clicks {_comparison_label(period_weeks)} "
                f"({prior_row.get('clicks')} -> {current_row.get('clicks')})"
            ),
            "evidence": evidence,
            "likely_cause": (
                "Branded query loss - potential brand awareness decline or SERP change."
                if is_branded else
                "Ranking loss, new competitor, or SERP feature change."
            ),
            "suggested_action": (
                "Investigate brand SERP presence and check for competitor bidding on brand terms."
                if is_branded else
                "Check the live SERP for this query - a competitor or AI Overview may have displaced clicks."
            ),
            "urgency": urgency,
        })

    findings.sort(key=lambda finding: finding["evidence"].get("prior_clicks", 0), reverse=True)
    return findings[:8]


def find_ctr_erosion(db_conn, period_ending, period_weeks: int) -> list[dict]:
    """Flag queries where impressions held steady but CTR dropped significantly."""
    result = get_search_period(db_conn, period_ending, period_weeks)
    current = result["current"] or {}
    prior = result["prior"] or {}
    yoy = result["yoy"] or {}
    completeness = result["completeness"]

    min_impressions = max(MIN_IMPRESSIONS, 25 * period_weeks)

    findings = []
    for entity_key, current_row in current.items():
        prior_row = prior.get(entity_key)
        if not prior_row or (prior_row.get("impressions") or 0) < min_impressions:
            continue
        if (current_row.get("clicks") or 0) == 0:
            continue

        impressions_change = safe_pct_change(current_row.get("impressions"), prior_row.get("impressions"))
        ctr_change = safe_pct_change(current_row.get("ctr"), prior_row.get("ctr"))
        if impressions_change is None or ctr_change is None:
            continue
        if impressions_change < IMPRESSION_STABLE_PCT or ctr_change > CTR_DROP_PCT:
            continue

        short_query = _truncate_query(current_row.get("query"))
        evidence = {
            **_base_evidence(completeness, period_weeks),
            "query": current_row.get("query"),
            "page": current_row.get("page"),
            "clicks": current_row.get("clicks"),
            "impressions": current_row.get("impressions"),
            "ctr": _as_float(current_row.get("ctr")),
            "avg_position": _as_float(current_row.get("avg_position")),
            "impressions_change_pct": _as_float(impressions_change),
            "ctr_change_pct": _as_float(ctr_change),
            "prior_impressions": prior_row.get("impressions"),
            "threshold_impression_stable_pct": IMPRESSION_STABLE_PCT,
            "threshold_ctr_drop_pct": CTR_DROP_PCT,
            "comparison_basis": _comparison_label(period_weeks),
        }
        yoy_row = yoy.get(entity_key)
        if yoy_row:
            _attach_yoy(evidence, current_row.get("ctr"), yoy_row.get("ctr"), completeness)

        findings.append({
            "week_ending": period_ending.isoformat(),
            "period_weeks": period_weeks,
            "module": FINDING_MODULE,
            "finding_type": "alert",
            "severity": "medium",
            "title": (
                f"\"{short_query}\" - impressions "
                f"{'up' if float(impressions_change) > 0 else 'stable'} "
                f"({float(impressions_change):+.0f}%) but CTR down {abs(float(ctr_change)):.0f}% "
                f"{_comparison_label(period_weeks)}"
            ),
            "evidence": evidence,
            "likely_cause": (
                "AI Overview, featured snippet, or a more crowded SERP may be absorbing clicks before organic results."
            ),
            "suggested_action": (
                "Check the live SERP. If AI Overview or richer SERP features are present, adjust page titles and "
                "consider AEO optimization."
            ),
            "urgency": "monitor",
        })

    findings.sort(key=lambda finding: finding["evidence"].get("impressions", 0), reverse=True)
    return findings[:5]


def find_branded_share_shift(db_conn, period_ending, period_weeks: int) -> list[dict]:
    """Compare branded vs non-branded share of clicks this period vs the prior period."""
    result = get_search_period(db_conn, period_ending, period_weeks)
    current = result["current"] or {}
    prior = result["prior"] or {}
    yoy = result["yoy"] or {}
    completeness = result["completeness"]

    current_branded = sum(row.get("clicks", 0) for row in current.values() if row.get("is_branded"))
    current_non_branded = sum(row.get("clicks", 0) for row in current.values() if not row.get("is_branded"))
    prior_branded = sum(row.get("clicks", 0) for row in prior.values() if row.get("is_branded"))
    prior_non_branded = sum(row.get("clicks", 0) for row in prior.values() if not row.get("is_branded"))

    current_total = current_branded + current_non_branded
    prior_total = prior_branded + prior_non_branded
    if current_total == 0 or prior_total == 0:
        return []

    current_branded_share = float(current_branded) / float(current_total) * 100
    prior_branded_share = float(prior_branded) / float(prior_total) * 100
    share_change = current_branded_share - prior_branded_share
    if abs(share_change) <= BRANDED_SHARE_DROP_PPTS:
        return []

    evidence = {
        **_base_evidence(completeness, period_weeks),
        "current_branded_clicks": current_branded,
        "current_non_branded_clicks": current_non_branded,
        "prior_branded_clicks": prior_branded,
        "prior_non_branded_clicks": prior_non_branded,
        "current_branded_share_pct": round(current_branded_share, 2),
        "prior_branded_share_pct": round(prior_branded_share, 2),
        "share_change_ppts": round(share_change, 2),
        "threshold_ppts": BRANDED_SHARE_DROP_PPTS,
        "comparison_basis": _comparison_label(period_weeks),
    }

    if not completeness["yoy_empty"]:
        yoy_branded = sum(row.get("clicks", 0) for row in yoy.values() if row.get("is_branded"))
        yoy_non_branded = sum(row.get("clicks", 0) for row in yoy.values() if not row.get("is_branded"))
        yoy_total = yoy_branded + yoy_non_branded
        if yoy_total > 0:
            yoy_share = float(yoy_branded) / float(yoy_total) * 100
            evidence["yoy_change_pct"] = safe_pct_change(current_branded_share, yoy_share)
            evidence["yoy_partial"] = completeness["yoy_partial"]

    negative = share_change < 0
    return [{
        "week_ending": period_ending.isoformat(),
        "period_weeks": period_weeks,
        "module": FINDING_MODULE,
        "finding_type": "alert" if negative else "positive",
        "severity": "medium" if negative else "low",
        "title": (
            f"Branded search share {'dropped' if negative else 'grew'} "
            f"{abs(share_change):.1f} ppts {_comparison_label(period_weeks)} "
            f"({prior_branded_share:.1f}% -> {current_branded_share:.1f}%)"
        ),
        "evidence": evidence,
        "likely_cause": (
            f"Brand awareness is {'weakening' if negative else 'strengthening'} relative to non-branded search demand."
        ),
        "suggested_action": (
            "Review branded PPC coverage and brand SERP presence."
            if negative else
            "Positive trend - branded awareness is strengthening. Maintain brand coverage."
        ),
        "urgency": "monitor" if negative else "backlog",
    }]


def run(db_conn, week_ending: str, period_weeks: int = 4) -> int:
    """Run all search analysis and write findings. Returns finding count."""
    period_ending = datetime.strptime(week_ending, "%Y-%m-%d").date()

    findings = []
    findings.extend(find_click_drops(db_conn, period_ending, period_weeks) or [])
    findings.extend(find_ctr_erosion(db_conn, period_ending, period_weeks) or [])
    findings.extend(find_branded_share_shift(db_conn, period_ending, period_weeks) or [])

    if len(findings) > 15:
        severity_order = {"high": 0, "medium": 1, "low": 2}
        findings.sort(key=lambda finding: severity_order.get(finding["severity"], 3))
        findings = findings[:15]

    count = _write_findings(db_conn, findings, FINDING_MODULE, week_ending, period_weeks=period_weeks)
    print(f"  [search] {count} findings written for {week_ending} ({period_weeks}-week window)")
    return count
