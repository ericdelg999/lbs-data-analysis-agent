"""
AI referral metrics transform - aggregates GA4 source-level traffic for known AI domains.

Reads:  raw_ga4_sources_daily, ref_ai_referrers
Writes: metrics_ai_referral_weekly
"""

from datetime import datetime, timedelta

import psycopg2.extras


def wow_pct(current, prior):
    """Return week-over-week percentage change or None when undefined."""
    if prior is None or prior == 0:
        return None
    current_val = float(current)
    prior_val = float(prior)
    return float((current_val - prior_val) / abs(prior_val) * 100)


def compute_ai_referral_weekly(db_conn, week_ending: str) -> int:
    """Aggregate AI referrer source data into weekly AI referral metrics."""
    week_ending_date = datetime.strptime(week_ending, "%Y-%m-%d").date()
    week_start = week_ending_date - timedelta(days=6)
    prior_week_ending = week_ending_date - timedelta(days=7)

    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT
                s.session_source AS referrer_domain,
                r.label AS referrer_label,
                SUM(s.sessions) AS sessions,
                SUM(s.engaged_sessions) AS engaged_sessions,
                SUM(s.conversions) AS conversions,
                SUM(s.revenue) AS revenue,
                SUM(s.conversions)::numeric / NULLIF(SUM(s.sessions), 0) AS conversion_rate
            FROM raw_ga4_sources_daily s
            INNER JOIN ref_ai_referrers r
                ON s.session_source = r.domain
               AND r.is_active = TRUE
            WHERE s.date BETWEEN %s AND %s
            GROUP BY s.session_source, r.label
        """, (week_start, week_ending_date))
        current_rows = cur.fetchall()

        if not current_rows:
            return 0

        cur.execute("""
            SELECT referrer_domain, sessions, revenue
            FROM metrics_ai_referral_weekly
            WHERE week_ending = %s
        """, (prior_week_ending,))
        prior_lookup = {
            row[0]: (row[1], row[2])
            for row in cur.fetchall()
        }

    rows = []
    for row in current_rows:
        referrer_domain = row[0]
        referrer_label = row[1]
        sessions_val = row[2]
        engaged_val = row[3]
        conversions_val = row[4]
        revenue_val = row[5]
        conversion_rate_val = row[6]

        prior_sessions, prior_revenue = prior_lookup.get(referrer_domain, (None, None))

        rows.append((
            week_ending_date,
            referrer_domain,
            referrer_label,
            sessions_val,
            engaged_val,
            conversions_val,
            revenue_val,
            conversion_rate_val,
            None,
            prior_sessions,
            wow_pct(sessions_val, prior_sessions),
            wow_pct(revenue_val, prior_revenue),
        ))

    with db_conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO metrics_ai_referral_weekly (
                week_ending, referrer_domain, referrer_label,
                sessions, engaged_sessions, conversions, revenue,
                conversion_rate, top_landing_page,
                prev_sessions, sessions_wow_pct, revenue_wow_pct
            ) VALUES %s
            ON CONFLICT (week_ending, referrer_domain) DO UPDATE SET
                referrer_label       = EXCLUDED.referrer_label,
                sessions             = EXCLUDED.sessions,
                engaged_sessions     = EXCLUDED.engaged_sessions,
                conversions          = EXCLUDED.conversions,
                revenue              = EXCLUDED.revenue,
                conversion_rate      = EXCLUDED.conversion_rate,
                top_landing_page     = EXCLUDED.top_landing_page,
                prev_sessions        = EXCLUDED.prev_sessions,
                sessions_wow_pct     = EXCLUDED.sessions_wow_pct,
                revenue_wow_pct      = EXCLUDED.revenue_wow_pct
        """, rows, template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)")
    db_conn.commit()
    print(f"  [ai_referral_metrics] {len(rows)} referrer rows written for {week_ending}")
    return len(rows)
