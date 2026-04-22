"""
Brand metrics transform - rolls up product metrics to brand level.

Reads:  metrics_product_weekly
Writes: metrics_brand_weekly
"""

import os
from datetime import datetime, timedelta

import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

MIN_BRAND_VIEWS = int(os.getenv("MIN_BRAND_VIEWS_THRESHOLD", "200"))


def wow_pct(current, prior):
    """Return week-over-week percentage change or None when undefined."""
    if prior is None or prior == 0:
        return None
    current_val = float(current)
    prior_val = float(prior)
    return float((current_val - prior_val) / abs(prior_val) * 100)


def compute_brand_weekly(db_conn, week_ending: str) -> int:
    """
    Roll up metrics_product_weekly into metrics_brand_weekly for week_ending.

    Returns: number of brand rows written
    """
    week_ending_date = datetime.strptime(week_ending, "%Y-%m-%d").date()
    prior_week_ending = week_ending_date - timedelta(days=7)

    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT
                bc_brand_id,
                brand_name,
                COUNT(DISTINCT item_id) AS active_product_count,
                SUM(views) AS total_views,
                SUM(add_to_carts) AS total_add_to_carts,
                SUM(purchases) AS total_purchases,
                SUM(revenue) AS total_revenue,
                SUM(add_to_carts)::numeric / NULLIF(SUM(views), 0) AS blended_atc_rate,
                SUM(purchases)::numeric / NULLIF(SUM(views), 0) AS blended_purchase_rate
            FROM metrics_product_weekly
            WHERE week_ending = %s
              AND bc_brand_id IS NOT NULL
            GROUP BY bc_brand_id, brand_name
            HAVING SUM(views) >= %s
        """, (week_ending_date, MIN_BRAND_VIEWS))
        current_rows = cur.fetchall()

        cur.execute("""
            SELECT bc_brand_id, total_views, blended_atc_rate, total_revenue
            FROM metrics_brand_weekly
            WHERE week_ending = %s
        """, (prior_week_ending,))
        prior_lookup = {
            row[0]: (row[1], row[2], row[3])
            for row in cur.fetchall()
        }

    if not current_rows:
        return 0

    rows = []
    for row in current_rows:
        bc_brand_id = row[0]
        prev_total_views, prev_blended_atc_rate, prev_total_revenue = prior_lookup.get(
            bc_brand_id, (None, None, None)
        )

        rows.append((
            week_ending_date,
            row[0],   # bc_brand_id
            row[1],   # brand_name
            row[2],   # active_product_count
            row[3],   # total_views
            row[4],   # total_add_to_carts
            row[5],   # total_purchases
            row[6],   # total_revenue
            row[7],   # blended_atc_rate
            row[8],   # blended_purchase_rate
            prev_total_views,
            prev_blended_atc_rate,
            prev_total_revenue,
            wow_pct(row[3], prev_total_views),
            wow_pct(row[7], prev_blended_atc_rate),
            wow_pct(row[6], prev_total_revenue),
        ))

    with db_conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO metrics_brand_weekly (
                week_ending, bc_brand_id, brand_name, active_product_count,
                total_views, total_add_to_carts, total_purchases, total_revenue,
                blended_atc_rate, blended_purchase_rate,
                prev_total_views, prev_blended_atc_rate, prev_total_revenue,
                views_wow_pct, atc_rate_wow_pct, revenue_wow_pct
            ) VALUES %s
            ON CONFLICT (week_ending, bc_brand_id) DO UPDATE SET
                brand_name            = EXCLUDED.brand_name,
                active_product_count  = EXCLUDED.active_product_count,
                total_views           = EXCLUDED.total_views,
                total_add_to_carts    = EXCLUDED.total_add_to_carts,
                total_purchases       = EXCLUDED.total_purchases,
                total_revenue         = EXCLUDED.total_revenue,
                blended_atc_rate      = EXCLUDED.blended_atc_rate,
                blended_purchase_rate = EXCLUDED.blended_purchase_rate,
                prev_total_views      = EXCLUDED.prev_total_views,
                prev_blended_atc_rate = EXCLUDED.prev_blended_atc_rate,
                prev_total_revenue    = EXCLUDED.prev_total_revenue,
                views_wow_pct         = EXCLUDED.views_wow_pct,
                atc_rate_wow_pct      = EXCLUDED.atc_rate_wow_pct,
                revenue_wow_pct       = EXCLUDED.revenue_wow_pct
        """, rows, template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)")
    db_conn.commit()
    return len(rows)
