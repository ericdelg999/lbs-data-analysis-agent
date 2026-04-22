"""
Funnel metrics transform - site-wide weekly funnel + new vs returning split.

Reads:  raw_ga4_daily, raw_ga4_products_daily, raw_ga4_traffic_channels_daily
Writes: metrics_funnel_weekly
"""

from datetime import datetime, timedelta


def safe_rate(numerator, denominator):
    """Return rate or None when the denominator is missing or zero."""
    if not denominator:
        return None
    return float(numerator) / float(denominator)


def wow_pct(current, prior):
    """Return week-over-week percentage change or None when undefined."""
    if prior is None or prior == 0:
        return None
    current_val = float(current)
    prior_val = float(prior)
    return float((current_val - prior_val) / abs(prior_val) * 100)


def compute_funnel_weekly(db_conn, week_ending: str) -> int:
    """
    Compute site-wide funnel metrics for week_ending and upsert.

    Returns: 1 for a written row, or 0 if no source data exists.
    """
    week_ending_date = datetime.strptime(week_ending, "%Y-%m-%d").date()
    week_start = week_ending_date - timedelta(days=6)
    prior_week_ending = week_ending_date - timedelta(days=7)

    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT
                SUM(sessions) AS sessions,
                SUM(engaged_sessions) AS engaged_sessions
            FROM raw_ga4_daily
            WHERE date BETWEEN %s AND %s
        """, (week_start, week_ending_date))
        sessions, engaged_sessions = cur.fetchone()

        if sessions is None:
            return 0

        cur.execute("""
            SELECT
                SUM(views) AS pdp_views,
                SUM(add_to_carts) AS add_to_carts,
                SUM(checkouts) AS checkouts,
                SUM(purchases) AS purchases,
                SUM(purchase_revenue) AS revenue
            FROM raw_ga4_products_daily
            WHERE date BETWEEN %s AND %s
        """, (week_start, week_ending_date))
        pdp_views, add_to_carts, checkouts, purchases, revenue = cur.fetchone()

        cur.execute("""
            SELECT
                SUM(new_users)                        AS new_user_sessions,
                SUM(sessions) - SUM(new_users)        AS returning_user_sessions,
                SUM(conversions)                      AS transactions
            FROM raw_ga4_traffic_channels_daily
            WHERE date BETWEEN %s AND %s
        """, (week_start, week_ending_date))
        new_user_sessions, returning_user_sessions, transactions = cur.fetchone()

        cur.execute("""
            SELECT revenue, sessions, overall_conversion_rate
            FROM metrics_funnel_weekly
            WHERE week_ending = %s
        """, (prior_week_ending,))
        prior_row = cur.fetchone()

    pdp_views = pdp_views or 0
    add_to_carts = add_to_carts or 0
    checkouts = checkouts or 0
    purchases = purchases or 0
    revenue = revenue or 0
    new_user_sessions = new_user_sessions or 0
    returning_user_sessions = returning_user_sessions or 0

    session_to_pdp_rate = safe_rate(pdp_views, sessions)
    pdp_to_atc_rate = safe_rate(add_to_carts, pdp_views)
    atc_to_checkout_rate = safe_rate(checkouts, add_to_carts)
    checkout_to_purchase_rate = safe_rate(purchases, checkouts)
    # Use channel-level conversions (session-scoped transactions) not product purchase
    # events (raw_ga4_products_daily.purchases). Product events count one per line item,
    # inflating the rate by ~10x on a multi-SKU wholesale catalog.
    transactions = transactions or 0
    overall_conversion_rate = safe_rate(transactions, sessions)

    prior_revenue = prior_sessions = prior_conversion_rate = None
    if prior_row:
        prior_revenue, prior_sessions, prior_conversion_rate = prior_row

    values = (
        week_ending_date,
        sessions,
        engaged_sessions,
        pdp_views,
        add_to_carts,
        checkouts,
        purchases,
        revenue,
        # Approximation: GA4 new_users is a user count, not true new-user sessions.
        new_user_sessions,
        returning_user_sessions,
        None,
        None,
        session_to_pdp_rate,
        pdp_to_atc_rate,
        atc_to_checkout_rate,
        checkout_to_purchase_rate,
        overall_conversion_rate,
        wow_pct(revenue, prior_revenue),
        wow_pct(sessions, prior_sessions),
        wow_pct(overall_conversion_rate, prior_conversion_rate),
    )

    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO metrics_funnel_weekly (
                week_ending, sessions, engaged_sessions,
                pdp_views, add_to_carts, checkouts, purchases, revenue,
                new_user_sessions, returning_user_sessions,
                new_user_revenue, returning_user_revenue,
                session_to_pdp_rate, pdp_to_atc_rate,
                atc_to_checkout_rate, checkout_to_purchase_rate,
                overall_conversion_rate,
                revenue_wow_pct, sessions_wow_pct, conversion_wow_pct
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (week_ending) DO UPDATE SET
                sessions                  = EXCLUDED.sessions,
                engaged_sessions          = EXCLUDED.engaged_sessions,
                pdp_views                 = EXCLUDED.pdp_views,
                add_to_carts              = EXCLUDED.add_to_carts,
                checkouts                 = EXCLUDED.checkouts,
                purchases                 = EXCLUDED.purchases,
                revenue                   = EXCLUDED.revenue,
                new_user_sessions         = EXCLUDED.new_user_sessions,
                returning_user_sessions   = EXCLUDED.returning_user_sessions,
                new_user_revenue          = EXCLUDED.new_user_revenue,
                returning_user_revenue    = EXCLUDED.returning_user_revenue,
                session_to_pdp_rate       = EXCLUDED.session_to_pdp_rate,
                pdp_to_atc_rate           = EXCLUDED.pdp_to_atc_rate,
                atc_to_checkout_rate      = EXCLUDED.atc_to_checkout_rate,
                checkout_to_purchase_rate = EXCLUDED.checkout_to_purchase_rate,
                overall_conversion_rate   = EXCLUDED.overall_conversion_rate,
                revenue_wow_pct           = EXCLUDED.revenue_wow_pct,
                sessions_wow_pct          = EXCLUDED.sessions_wow_pct,
                conversion_wow_pct        = EXCLUDED.conversion_wow_pct
        """, values)
    db_conn.commit()
    return 1
