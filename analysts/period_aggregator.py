"""
Period aggregator - reads metrics_*_weekly tables and sums across N consecutive
weeks to produce period-level totals with current-vs-prior-period and YoY
(same N-week window, 52 weeks back) comparisons.

Transforms stay unchanged (weekly grain, weekly WoW columns). This helper
re-derives period totals and rates at read time, from raw numerators/denominators
so rate averaging is mathematically correct (SUM(num)/SUM(denom), never
AVG(weekly_rate)).

Partial periods are returned with a completeness flag; callers decide whether
to surface the data or suppress.

Conventions:
- period_ending MUST be a Sunday (matches weekly_job.get_week_ending)
- period_weeks >= 1; period_weeks=1 gives identical behavior to the old weekly pipeline
- YoY window is 364 days back (52 weeks), preserving Sunday alignment
- All NUMERIC fields are cast to float before math (psycopg2 returns Decimal)
- None propagates correctly through safe_rate / safe_pct_change
"""

import logging
from datetime import date, timedelta
from typing import Optional

log = logging.getLogger(__name__)

YOY_OFFSET_DAYS = 364


# ─────────────────────────────────────────────────────────────────────────────
# Period window math
# ─────────────────────────────────────────────────────────────────────────────

def compute_period_window(period_ending: date, period_weeks: int) -> tuple[date, date]:
    """Return (period_start, period_ending) validated as a Sunday-anchored N-week window."""
    if period_ending.weekday() != 6:
        raise ValueError(
            f"period_ending must be a Sunday (weekday=6), got {period_ending} "
            f"({period_ending.strftime('%A')}, weekday={period_ending.weekday()})"
        )
    if period_weeks < 1:
        raise ValueError(f"period_weeks must be >= 1, got {period_weeks}")
    period_start = period_ending - timedelta(days=7 * period_weeks - 1)
    return period_start, period_ending


def compute_prior_window(period_ending: date, period_weeks: int) -> tuple[date, date]:
    """Return (start, end) for the N-week window immediately before period_ending."""
    prior_end = period_ending - timedelta(days=7 * period_weeks)
    prior_start = prior_end - timedelta(days=7 * period_weeks - 1)
    return prior_start, prior_end


def compute_yoy_window(period_ending: date, period_weeks: int) -> tuple[date, date]:
    """Return (start, end) for the N-week window ending 364 days before period_ending."""
    yoy_end = period_ending - timedelta(days=YOY_OFFSET_DAYS)
    yoy_start = yoy_end - timedelta(days=7 * period_weeks - 1)
    return yoy_start, yoy_end


# ─────────────────────────────────────────────────────────────────────────────
# Math utilities
# ─────────────────────────────────────────────────────────────────────────────

def safe_rate(numerator, denominator) -> Optional[float]:
    """Divide, returning None for None/zero denominator. Always returns float or None."""
    if numerator is None or denominator is None:
        return None
    denom = float(denominator)
    if denom == 0:
        return None
    return float(numerator) / denom


def safe_pct_change(current, prior) -> Optional[float]:
    """Percent change (current - prior) / |prior| * 100, or None if undefined."""
    if current is None or prior is None:
        return None
    prior_f = float(prior)
    if prior_f == 0:
        return None
    return (float(current) - prior_f) / abs(prior_f) * 100.0


def _count_distinct_weeks(db_conn, table: str, window_start: date, window_end: date) -> int:
    """Count distinct week_ending rows present in the table within the window."""
    with db_conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(DISTINCT week_ending) FROM {table} "  # nosec B608 - table name is hardcoded in callers
            f"WHERE week_ending BETWEEN %s AND %s",
            (window_start, window_end),
        )
        return int(cur.fetchone()[0] or 0)


def _build_completeness(
    db_conn,
    table: str,
    period_ending: date,
    period_weeks: int,
) -> dict:
    """Build the completeness dict. Counts distinct weeks present in each window."""
    current_start, current_end = compute_period_window(period_ending, period_weeks)
    prior_start, prior_end = compute_prior_window(period_ending, period_weeks)
    yoy_start, yoy_end = compute_yoy_window(period_ending, period_weeks)

    current_weeks = _count_distinct_weeks(db_conn, table, current_start, current_end)
    prior_weeks = _count_distinct_weeks(db_conn, table, prior_start, prior_end)
    yoy_weeks = _count_distinct_weeks(db_conn, table, yoy_start, yoy_end)

    return {
        "period_weeks": period_weeks,
        "current_weeks_available": current_weeks,
        "current_weeks_expected": period_weeks,
        "current_partial": current_weeks < period_weeks,
        "current_empty": current_weeks == 0,
        "prior_weeks_available": prior_weeks,
        "prior_weeks_expected": period_weeks,
        "prior_partial": prior_weeks < period_weeks,
        "prior_empty": prior_weeks == 0,
        "yoy_weeks_available": yoy_weeks,
        "yoy_weeks_expected": period_weeks,
        "yoy_partial": yoy_weeks < period_weeks,
        "yoy_empty": yoy_weeks == 0,
        "window": {
            "current_start": current_start.isoformat(),
            "current_end": current_end.isoformat(),
            "prior_start": prior_start.isoformat(),
            "prior_end": prior_end.isoformat(),
            "yoy_start": yoy_start.isoformat(),
            "yoy_end": yoy_end.isoformat(),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Funnel (singleton per week)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_funnel_window(db_conn, window_start: date, window_end: date) -> Optional[dict]:
    """Sum funnel metrics across the window, re-derive rates from raw numerators."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                SUM(sessions),
                SUM(engaged_sessions),
                SUM(pdp_views),
                SUM(add_to_carts),
                SUM(checkouts),
                SUM(purchases),
                SUM(revenue),
                SUM(new_user_sessions),
                SUM(returning_user_sessions),
                SUM(new_user_revenue),
                SUM(returning_user_revenue),
                COUNT(DISTINCT week_ending)
            FROM metrics_funnel_weekly
            WHERE week_ending BETWEEN %s AND %s
            """,
            (window_start, window_end),
        )
        row = cur.fetchone()

    if row is None or row[11] == 0:
        return None

    (
        sessions, engaged_sessions, pdp_views, add_to_carts, checkouts,
        purchases, revenue, new_user_sessions, returning_user_sessions,
        new_user_revenue, returning_user_revenue, weeks_present,
    ) = row

    return {
        "sessions": int(sessions or 0),
        "engaged_sessions": int(engaged_sessions or 0),
        "pdp_views": int(pdp_views or 0),
        "add_to_carts": int(add_to_carts or 0),
        "checkouts": int(checkouts or 0),
        "purchases": int(purchases or 0),
        "revenue": float(revenue or 0),
        "new_user_sessions": int(new_user_sessions or 0),
        "returning_user_sessions": int(returning_user_sessions or 0),
        "new_user_revenue": float(new_user_revenue or 0),
        "returning_user_revenue": float(returning_user_revenue or 0),
        "weeks_present": int(weeks_present or 0),
        # Rates re-derived from summed numerators/denominators — NOT averaged across weeks
        "session_to_pdp_rate": safe_rate(pdp_views, sessions),
        "pdp_to_atc_rate": safe_rate(add_to_carts, pdp_views),
        "atc_to_checkout_rate": safe_rate(checkouts, add_to_carts),
        "checkout_to_purchase_rate": safe_rate(purchases, checkouts),
        "overall_conversion_rate": safe_rate(purchases, sessions),
    }


def get_funnel_period(db_conn, period_ending: date, period_weeks: int) -> dict:
    """Return funnel metrics for current / prior / YoY periods + completeness."""
    completeness = _build_completeness(db_conn, "metrics_funnel_weekly", period_ending, period_weeks)
    w = completeness["window"]

    return {
        "current": _fetch_funnel_window(db_conn, date.fromisoformat(w["current_start"]), date.fromisoformat(w["current_end"])),
        "prior": _fetch_funnel_window(db_conn, date.fromisoformat(w["prior_start"]), date.fromisoformat(w["prior_end"])),
        "yoy": _fetch_funnel_window(db_conn, date.fromisoformat(w["yoy_start"]), date.fromisoformat(w["yoy_end"])),
        "completeness": completeness,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Brand (multi-entity: bc_brand_id)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_brand_window(db_conn, window_start: date, window_end: date) -> dict:
    """Sum brand metrics per bc_brand_id across the window. Returns dict keyed by bc_brand_id."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                bc_brand_id,
                MAX(brand_name) AS brand_name,
                MAX(active_product_count) AS active_product_count,
                SUM(total_views) AS total_views,
                SUM(total_add_to_carts) AS total_add_to_carts,
                SUM(total_purchases) AS total_purchases,
                SUM(total_revenue) AS total_revenue,
                COUNT(DISTINCT week_ending) AS weeks_present
            FROM metrics_brand_weekly
            WHERE week_ending BETWEEN %s AND %s
            GROUP BY bc_brand_id
            """,
            (window_start, window_end),
        )
        rows = cur.fetchall()

    result = {}
    for (bc_brand_id, brand_name, active_product_count, total_views,
         total_add_to_carts, total_purchases, total_revenue, weeks_present) in rows:
        result[bc_brand_id] = {
            "bc_brand_id": bc_brand_id,
            "brand_name": brand_name,
            "active_product_count": int(active_product_count or 0),
            "total_views": int(total_views or 0),
            "total_add_to_carts": int(total_add_to_carts or 0),
            "total_purchases": int(total_purchases or 0),
            "total_revenue": float(total_revenue or 0),
            "weeks_present": int(weeks_present or 0),
            "blended_atc_rate": safe_rate(total_add_to_carts, total_views),
            "blended_purchase_rate": safe_rate(total_purchases, total_views),
        }
    return result


def get_brand_period(db_conn, period_ending: date, period_weeks: int) -> dict:
    """Return brand metrics for current / prior / YoY, keyed by bc_brand_id."""
    completeness = _build_completeness(db_conn, "metrics_brand_weekly", period_ending, period_weeks)
    w = completeness["window"]

    return {
        "current": _fetch_brand_window(db_conn, date.fromisoformat(w["current_start"]), date.fromisoformat(w["current_end"])),
        "prior": _fetch_brand_window(db_conn, date.fromisoformat(w["prior_start"]), date.fromisoformat(w["prior_end"])),
        "yoy": _fetch_brand_window(db_conn, date.fromisoformat(w["yoy_start"]), date.fromisoformat(w["yoy_end"])),
        "completeness": completeness,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Product (multi-entity: item_id)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_product_window(db_conn, window_start: date, window_end: date) -> dict:
    """Sum product metrics per item_id across the window. Returns dict keyed by item_id."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                item_id,
                MAX(item_name) AS item_name,
                MAX(bc_product_id) AS bc_product_id,
                MAX(bc_brand_id) AS bc_brand_id,
                MAX(brand_name) AS brand_name,
                MAX(price) AS price,
                BOOL_OR(is_visible) AS is_visible,
                MAX(page_url) AS page_url,
                SUM(views) AS views,
                SUM(add_to_carts) AS add_to_carts,
                SUM(checkouts) AS checkouts,
                SUM(purchases) AS purchases,
                SUM(revenue) AS revenue,
                COUNT(DISTINCT week_ending) AS weeks_present
            FROM metrics_product_weekly
            WHERE week_ending BETWEEN %s AND %s
            GROUP BY item_id
            """,
            (window_start, window_end),
        )
        rows = cur.fetchall()

    result = {}
    for (item_id, item_name, bc_product_id, bc_brand_id, brand_name, price,
         is_visible, page_url, views, add_to_carts, checkouts, purchases,
         revenue, weeks_present) in rows:
        result[item_id] = {
            "item_id": item_id,
            "item_name": item_name,
            "bc_product_id": bc_product_id,
            "bc_brand_id": bc_brand_id,
            "brand_name": brand_name,
            "price": float(price) if price is not None else None,
            "is_visible": is_visible,
            "page_url": page_url,
            "views": int(views or 0),
            "add_to_carts": int(add_to_carts or 0),
            "checkouts": int(checkouts or 0),
            "purchases": int(purchases or 0),
            "revenue": float(revenue or 0),
            "weeks_present": int(weeks_present or 0),
            "atc_rate": safe_rate(add_to_carts, views),
            "checkout_rate": safe_rate(checkouts, add_to_carts),
            "purchase_rate": safe_rate(purchases, views),
            "cart_abandonment_rate": (
                None if safe_rate(checkouts, add_to_carts) is None
                else 1.0 - safe_rate(checkouts, add_to_carts)
            ),
        }
    return result


def get_product_period(db_conn, period_ending: date, period_weeks: int) -> dict:
    """Return product metrics for current / prior / YoY, keyed by item_id."""
    completeness = _build_completeness(db_conn, "metrics_product_weekly", period_ending, period_weeks)
    w = completeness["window"]

    return {
        "current": _fetch_product_window(db_conn, date.fromisoformat(w["current_start"]), date.fromisoformat(w["current_end"])),
        "prior": _fetch_product_window(db_conn, date.fromisoformat(w["prior_start"]), date.fromisoformat(w["prior_end"])),
        "yoy": _fetch_product_window(db_conn, date.fromisoformat(w["yoy_start"]), date.fromisoformat(w["yoy_end"])),
        "completeness": completeness,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Channel (multi-entity: channel_group)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_channel_window(db_conn, window_start: date, window_end: date) -> dict:
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                channel_group,
                SUM(sessions),
                SUM(engaged_sessions),
                SUM(conversions),
                SUM(revenue),
                COUNT(DISTINCT week_ending)
            FROM metrics_channel_weekly
            WHERE week_ending BETWEEN %s AND %s
            GROUP BY channel_group
            """,
            (window_start, window_end),
        )
        rows = cur.fetchall()

    result = {}
    for channel_group, sessions, engaged_sessions, conversions, revenue, weeks_present in rows:
        result[channel_group] = {
            "channel_group": channel_group,
            "sessions": int(sessions or 0),
            "engaged_sessions": int(engaged_sessions or 0),
            "conversions": int(conversions or 0),
            "revenue": float(revenue or 0),
            "weeks_present": int(weeks_present or 0),
            "conversion_rate": safe_rate(conversions, sessions),
        }
    return result


def get_channel_period(db_conn, period_ending: date, period_weeks: int) -> dict:
    completeness = _build_completeness(db_conn, "metrics_channel_weekly", period_ending, period_weeks)
    w = completeness["window"]
    return {
        "current": _fetch_channel_window(db_conn, date.fromisoformat(w["current_start"]), date.fromisoformat(w["current_end"])),
        "prior": _fetch_channel_window(db_conn, date.fromisoformat(w["prior_start"]), date.fromisoformat(w["prior_end"])),
        "yoy": _fetch_channel_window(db_conn, date.fromisoformat(w["yoy_start"]), date.fromisoformat(w["yoy_end"])),
        "completeness": completeness,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Search (multi-entity: query, page) — GSC 16-month history caveat applies
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_search_window(db_conn, window_start: date, window_end: date) -> dict:
    """
    Sum search metrics per (query, page) across the window.
    avg_position is impressions-weighted (correct formula for weighted-avg rank).
    Keyed by (query, page) tuple.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                query,
                page,
                BOOL_OR(is_branded) AS is_branded,
                SUM(clicks) AS clicks,
                SUM(impressions) AS impressions,
                CASE
                    WHEN SUM(impressions) > 0
                    THEN SUM(avg_position * impressions) / NULLIF(SUM(impressions), 0)
                    ELSE NULL
                END AS avg_position,
                COUNT(DISTINCT week_ending) AS weeks_present
            FROM metrics_search_weekly
            WHERE week_ending BETWEEN %s AND %s
            GROUP BY query, page
            """,
            (window_start, window_end),
        )
        rows = cur.fetchall()

    result = {}
    for query, page, is_branded, clicks, impressions, avg_position, weeks_present in rows:
        result[(query, page)] = {
            "query": query,
            "page": page,
            "is_branded": bool(is_branded),
            "clicks": int(clicks or 0),
            "impressions": int(impressions or 0),
            "avg_position": float(avg_position) if avg_position is not None else None,
            "ctr": safe_rate(clicks, impressions),
            "weeks_present": int(weeks_present or 0),
        }
    return result


def get_search_period(db_conn, period_ending: date, period_weeks: int) -> dict:
    """
    Return search metrics for current / prior / YoY windows.

    NOTE: GSC backfill is only 16 months. YoY (52 weeks back = ~12 months) is
    just within range for recent dates but YoY window may be partial for any
    period older than ~4 months from 'now'. Caller should check
    completeness['yoy_weeks_available'] before trusting YoY comparisons.
    """
    completeness = _build_completeness(db_conn, "metrics_search_weekly", period_ending, period_weeks)
    w = completeness["window"]
    return {
        "current": _fetch_search_window(db_conn, date.fromisoformat(w["current_start"]), date.fromisoformat(w["current_end"])),
        "prior": _fetch_search_window(db_conn, date.fromisoformat(w["prior_start"]), date.fromisoformat(w["prior_end"])),
        "yoy": _fetch_search_window(db_conn, date.fromisoformat(w["yoy_start"]), date.fromisoformat(w["yoy_end"])),
        "completeness": completeness,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Paid Media (multi-entity: campaign_id)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_paid_window(db_conn, window_start: date, window_end: date) -> dict:
    """
    Sum paid metrics per campaign_id. Impression-share metrics are
    impressions-weighted (rough proxy — true weight would be eligible impressions,
    which isn't stored).
    """
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                campaign_id,
                MAX(campaign_name) AS campaign_name,
                SUM(spend) AS spend,
                SUM(clicks) AS clicks,
                SUM(impressions) AS impressions,
                SUM(conversions) AS conversions,
                SUM(conversion_value) AS conversion_value,
                CASE
                    WHEN SUM(impressions) > 0
                    THEN SUM(avg_search_impression_share * impressions) / NULLIF(SUM(impressions), 0)
                    ELSE NULL
                END AS avg_search_impression_share,
                CASE
                    WHEN SUM(impressions) > 0
                    THEN SUM(avg_search_lost_is_rank * impressions) / NULLIF(SUM(impressions), 0)
                    ELSE NULL
                END AS avg_search_lost_is_rank,
                CASE
                    WHEN SUM(impressions) > 0
                    THEN SUM(avg_search_lost_is_budget * impressions) / NULLIF(SUM(impressions), 0)
                    ELSE NULL
                END AS avg_search_lost_is_budget,
                COUNT(DISTINCT week_ending) AS weeks_present
            FROM metrics_paid_weekly
            WHERE week_ending BETWEEN %s AND %s
            GROUP BY campaign_id
            """,
            (window_start, window_end),
        )
        rows = cur.fetchall()

    result = {}
    for (campaign_id, campaign_name, spend, clicks, impressions, conversions,
         conversion_value, avg_is, avg_lost_rank, avg_lost_budget, weeks_present) in rows:
        result[campaign_id] = {
            "campaign_id": campaign_id,
            "campaign_name": campaign_name,
            "spend": float(spend or 0),
            "clicks": int(clicks or 0),
            "impressions": int(impressions or 0),
            "conversions": float(conversions or 0),
            "conversion_value": float(conversion_value or 0),
            "cpc": safe_rate(spend, clicks),
            "roas": safe_rate(conversion_value, spend),
            "avg_search_impression_share": float(avg_is) if avg_is is not None else None,
            "avg_search_lost_is_rank": float(avg_lost_rank) if avg_lost_rank is not None else None,
            "avg_search_lost_is_budget": float(avg_lost_budget) if avg_lost_budget is not None else None,
            "weeks_present": int(weeks_present or 0),
        }
    return result


def get_paid_period(db_conn, period_ending: date, period_weeks: int) -> dict:
    completeness = _build_completeness(db_conn, "metrics_paid_weekly", period_ending, period_weeks)
    w = completeness["window"]
    return {
        "current": _fetch_paid_window(db_conn, date.fromisoformat(w["current_start"]), date.fromisoformat(w["current_end"])),
        "prior": _fetch_paid_window(db_conn, date.fromisoformat(w["prior_start"]), date.fromisoformat(w["prior_end"])),
        "yoy": _fetch_paid_window(db_conn, date.fromisoformat(w["yoy_start"]), date.fromisoformat(w["yoy_end"])),
        "completeness": completeness,
    }


# ─────────────────────────────────────────────────────────────────────────────
# AI Referral (multi-entity: referrer_domain)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_ai_referral_window(db_conn, window_start: date, window_end: date) -> dict:
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                referrer_domain,
                MAX(referrer_label) AS referrer_label,
                SUM(sessions) AS sessions,
                SUM(engaged_sessions) AS engaged_sessions,
                SUM(conversions) AS conversions,
                SUM(revenue) AS revenue,
                COUNT(DISTINCT week_ending) AS weeks_present
            FROM metrics_ai_referral_weekly
            WHERE week_ending BETWEEN %s AND %s
            GROUP BY referrer_domain
            """,
            (window_start, window_end),
        )
        rows = cur.fetchall()

    result = {}
    for (referrer_domain, referrer_label, sessions, engaged_sessions,
         conversions, revenue, weeks_present) in rows:
        result[referrer_domain] = {
            "referrer_domain": referrer_domain,
            "referrer_label": referrer_label,
            "sessions": int(sessions or 0),
            "engaged_sessions": int(engaged_sessions or 0),
            "conversions": int(conversions or 0),
            "revenue": float(revenue or 0),
            "weeks_present": int(weeks_present or 0),
            "conversion_rate": safe_rate(conversions, sessions),
        }
    return result


def get_ai_referral_period(db_conn, period_ending: date, period_weeks: int) -> dict:
    completeness = _build_completeness(db_conn, "metrics_ai_referral_weekly", period_ending, period_weeks)
    w = completeness["window"]
    return {
        "current": _fetch_ai_referral_window(db_conn, date.fromisoformat(w["current_start"]), date.fromisoformat(w["current_end"])),
        "prior": _fetch_ai_referral_window(db_conn, date.fromisoformat(w["prior_start"]), date.fromisoformat(w["prior_end"])),
        "yoy": _fetch_ai_referral_window(db_conn, date.fromisoformat(w["yoy_start"]), date.fromisoformat(w["yoy_end"])),
        "completeness": completeness,
    }
