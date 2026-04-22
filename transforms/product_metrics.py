"""
Product metrics transform - aggregates daily GA4 product events into
weekly per-product metrics with context from BigCommerce catalog.

Reads:  raw_ga4_products_daily, raw_ga4_pages_daily, raw_bc_products
Writes: metrics_product_weekly
"""

import os
from datetime import datetime, timedelta

import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

MIN_VIEWS = int(os.getenv("MIN_PRODUCT_VIEWS_THRESHOLD", "50"))


def wow_pct(current, prior):
    """Return week-over-week percentage change or None when undefined."""
    if prior is None or prior == 0:
        return None
    current_val = float(current)
    prior_val = float(prior)
    return float((current_val - prior_val) / abs(prior_val) * 100)


def compute_product_weekly(db_conn, week_ending: str) -> int:
    """
    Compute and upsert metrics_product_weekly for the given week_ending.

    Returns: number of product rows written
    """
    week_ending_date = datetime.strptime(week_ending, "%Y-%m-%d").date()
    week_start = week_ending_date - timedelta(days=6)
    prior_week_ending = week_ending_date - timedelta(days=7)

    with db_conn.cursor() as cur:
        cur.execute("""
            WITH weekly_ga4 AS (
                SELECT
                    item_id,
                    MAX(item_name) AS item_name,
                    SUM(views) AS views,
                    SUM(add_to_carts) AS add_to_carts,
                    SUM(checkouts) AS checkouts,
                    SUM(purchases) AS purchases,
                    SUM(purchase_revenue) AS revenue
                FROM raw_ga4_products_daily
                WHERE date BETWEEN %(week_start)s AND %(week_ending)s
                GROUP BY item_id
            ),
            enriched AS (
                SELECT
                    g.item_id,
                    g.item_name,
                    p.bc_product_id,
                    p.bc_brand_id,
                    p.brand_name,
                    p.price,
                    p.custom_url AS page_url,
                    g.views,
                    g.add_to_carts,
                    g.checkouts,
                    g.purchases,
                    g.revenue,
                    bounce.pdp_bounce_rate
                FROM weekly_ga4 g
                LEFT JOIN raw_bc_products p
                    ON p.sku = g.item_id
                LEFT JOIN LATERAL (
                    SELECT AVG(pg.bounce_rate) AS pdp_bounce_rate
                    FROM raw_ga4_pages_daily pg
                    WHERE pg.page_path = p.custom_url
                      AND pg.date BETWEEN %(week_start)s AND %(week_ending)s
                ) bounce ON TRUE
                WHERE COALESCE(p.is_visible, TRUE) = TRUE
                  AND g.views >= %(min_views)s
            )
            SELECT
                item_id,
                item_name,
                bc_product_id,
                bc_brand_id,
                brand_name,
                price,
                TRUE AS is_visible,
                page_url,
                views,
                add_to_carts,
                checkouts,
                purchases,
                revenue,
                pdp_bounce_rate,
                add_to_carts::numeric / NULLIF(views, 0) AS atc_rate,
                checkouts::numeric / NULLIF(add_to_carts, 0) AS checkout_rate,
                purchases::numeric / NULLIF(views, 0) AS purchase_rate,
                1.0 - (checkouts::numeric / NULLIF(add_to_carts, 0)) AS cart_abandonment_rate
            FROM enriched
        """, {
            "week_start": week_start,
            "week_ending": week_ending_date,
            "min_views": MIN_VIEWS,
        })
        current_rows = cur.fetchall()

        cur.execute("""
            SELECT item_id, views, atc_rate, purchase_rate
            FROM metrics_product_weekly
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
        item_id = row[0]
        prev_views, prev_atc_rate, prev_purchase_rate = prior_lookup.get(
            item_id, (None, None, None)
        )

        rows.append((
            week_ending_date,
            row[0],   # item_id
            row[1],   # item_name
            row[2],   # bc_product_id
            row[3],   # bc_brand_id
            row[4],   # brand_name
            row[5],   # price
            row[6],   # is_visible
            row[7],   # page_url
            row[8],   # views
            row[9],   # add_to_carts
            row[10],  # checkouts
            row[11],  # purchases
            row[12],  # revenue
            row[13],  # pdp_bounce_rate
            row[14],  # atc_rate
            row[15],  # checkout_rate
            row[16],  # purchase_rate
            row[17],  # cart_abandonment_rate
            prev_views,
            prev_atc_rate,
            prev_purchase_rate,
            wow_pct(row[8], prev_views),
            wow_pct(row[14], prev_atc_rate),
            wow_pct(row[16], prev_purchase_rate),
        ))

    with db_conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO metrics_product_weekly (
                week_ending, item_id, item_name, bc_product_id, bc_brand_id, brand_name,
                price, is_visible, page_url,
                views, add_to_carts, checkouts, purchases, revenue, pdp_bounce_rate,
                atc_rate, checkout_rate, purchase_rate, cart_abandonment_rate,
                prev_views, prev_atc_rate, prev_purchase_rate,
                views_wow_pct, atc_rate_wow_pct, purchase_rate_wow_pct
            ) VALUES %s
            ON CONFLICT (week_ending, item_id) DO UPDATE SET
                item_name             = EXCLUDED.item_name,
                bc_product_id         = EXCLUDED.bc_product_id,
                bc_brand_id           = EXCLUDED.bc_brand_id,
                brand_name            = EXCLUDED.brand_name,
                price                 = EXCLUDED.price,
                is_visible            = EXCLUDED.is_visible,
                page_url              = EXCLUDED.page_url,
                views                 = EXCLUDED.views,
                add_to_carts          = EXCLUDED.add_to_carts,
                checkouts             = EXCLUDED.checkouts,
                purchases             = EXCLUDED.purchases,
                revenue               = EXCLUDED.revenue,
                pdp_bounce_rate       = EXCLUDED.pdp_bounce_rate,
                atc_rate              = EXCLUDED.atc_rate,
                checkout_rate         = EXCLUDED.checkout_rate,
                purchase_rate         = EXCLUDED.purchase_rate,
                cart_abandonment_rate = EXCLUDED.cart_abandonment_rate,
                prev_views            = EXCLUDED.prev_views,
                prev_atc_rate         = EXCLUDED.prev_atc_rate,
                prev_purchase_rate    = EXCLUDED.prev_purchase_rate,
                views_wow_pct         = EXCLUDED.views_wow_pct,
                atc_rate_wow_pct      = EXCLUDED.atc_rate_wow_pct,
                purchase_rate_wow_pct = EXCLUDED.purchase_rate_wow_pct
        """, rows, template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)")
    db_conn.commit()
    return len(rows)
