"""
Paid media metrics transform - aggregates Google Ads data to weekly campaign level
and GA4 channel data to weekly channel level.
"""

from datetime import datetime, timedelta

import psycopg2.extras


def wow_pct(current, prior):
    """Return week-over-week percentage change or None when undefined."""
    if current is None or prior is None or prior == 0:
        return None
    current_val = float(current)
    prior_val = float(prior)
    return float((current_val - prior_val) / abs(prior_val) * 100)


def compute_paid_weekly(db_conn, week_ending: str) -> int:
    """
    Aggregate raw_gads_daily into weekly campaign-level metrics.

    Returns: number of campaign rows written
    """
    week_ending_date = datetime.strptime(week_ending, "%Y-%m-%d").date()
    week_start = week_ending_date - timedelta(days=6)
    prior_week_ending = week_ending_date - timedelta(days=7)

    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT
                campaign_id,
                MAX(campaign_name) AS campaign_name,
                SUM(cost) AS spend,
                SUM(clicks) AS clicks,
                SUM(impressions) AS impressions,
                SUM(conversions) AS conversions,
                SUM(conversion_value) AS conversion_value,
                SUM(search_impression_share * impressions) / NULLIF(SUM(impressions), 0)
                    AS avg_search_impression_share,
                SUM(search_lost_is_rank * impressions) / NULLIF(SUM(impressions), 0)
                    AS avg_search_lost_is_rank,
                SUM(search_lost_is_budget * impressions) / NULLIF(SUM(impressions), 0)
                    AS avg_search_lost_is_budget,
                SUM(cost) / NULLIF(SUM(clicks), 0) AS cpc,
                SUM(conversion_value) / NULLIF(SUM(cost), 0) AS roas
            FROM raw_gads_daily
            WHERE date BETWEEN %s AND %s
            GROUP BY campaign_id
        """, (week_start, week_ending_date))
        current_rows = cur.fetchall()

        cur.execute("""
            SELECT campaign_id, spend, roas, avg_search_impression_share
            FROM metrics_paid_weekly
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
        campaign_id_val = row[0]
        campaign_name_val = row[1]
        spend_val = row[2]
        clicks_val = row[3]
        impressions_val = row[4]
        conversions_val = row[5]
        conversion_value_val = row[6]
        avg_sis_val = row[7]
        avg_lost_rank_val = row[8]
        avg_lost_budget_val = row[9]
        cpc_val = row[10]
        roas_val = row[11]

        prior_spend, prior_roas, prior_impression_share = prior_lookup.get(
            campaign_id_val, (None, None, None)
        )

        rows.append((
            week_ending_date,
            campaign_id_val,
            campaign_name_val,
            spend_val,
            clicks_val,
            impressions_val,
            conversions_val,
            conversion_value_val,
            cpc_val,
            roas_val,
            avg_sis_val,
            avg_lost_rank_val,
            avg_lost_budget_val,
            prior_spend,
            prior_roas,
            wow_pct(spend_val, prior_spend),
            wow_pct(roas_val, prior_roas),
            wow_pct(avg_sis_val, prior_impression_share),
        ))

    with db_conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO metrics_paid_weekly (
                week_ending, campaign_id, campaign_name,
                spend, clicks, impressions, conversions, conversion_value,
                cpc, roas,
                avg_search_impression_share, avg_search_lost_is_rank, avg_search_lost_is_budget,
                prev_spend, prev_roas,
                spend_wow_pct, roas_wow_pct, impression_share_wow_pct
            ) VALUES %s
            ON CONFLICT (week_ending, campaign_id) DO UPDATE SET
                campaign_name               = EXCLUDED.campaign_name,
                spend                       = EXCLUDED.spend,
                clicks                      = EXCLUDED.clicks,
                impressions                 = EXCLUDED.impressions,
                conversions                 = EXCLUDED.conversions,
                conversion_value            = EXCLUDED.conversion_value,
                cpc                         = EXCLUDED.cpc,
                roas                        = EXCLUDED.roas,
                avg_search_impression_share = EXCLUDED.avg_search_impression_share,
                avg_search_lost_is_rank     = EXCLUDED.avg_search_lost_is_rank,
                avg_search_lost_is_budget   = EXCLUDED.avg_search_lost_is_budget,
                prev_spend                  = EXCLUDED.prev_spend,
                prev_roas                   = EXCLUDED.prev_roas,
                spend_wow_pct               = EXCLUDED.spend_wow_pct,
                roas_wow_pct                = EXCLUDED.roas_wow_pct,
                impression_share_wow_pct    = EXCLUDED.impression_share_wow_pct
        """, rows, template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)")
    db_conn.commit()
    return len(rows)


def compute_channel_weekly(db_conn, week_ending: str) -> int:
    """
    Aggregate raw_ga4_traffic_channels_daily into metrics_channel_weekly.

    Returns: number of channel rows written
    """
    week_ending_date = datetime.strptime(week_ending, "%Y-%m-%d").date()
    week_start = week_ending_date - timedelta(days=6)
    prior_week_ending = week_ending_date - timedelta(days=7)

    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT
                channel_group,
                SUM(sessions) AS sessions,
                SUM(engaged_sessions) AS engaged_sessions,
                SUM(conversions) AS conversions,
                SUM(revenue) AS revenue,
                SUM(conversions)::numeric / NULLIF(SUM(sessions), 0) AS conversion_rate
            FROM raw_ga4_traffic_channels_daily
            WHERE date BETWEEN %s AND %s
            GROUP BY channel_group
        """, (week_start, week_ending_date))
        current_rows = cur.fetchall()

        cur.execute("""
            SELECT channel_group, sessions, conversion_rate, revenue
            FROM metrics_channel_weekly
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
        channel_val = row[0]
        sessions_val = row[1]
        engaged_val = row[2]
        conversions_val = row[3]
        revenue_val = row[4]
        conversion_rate_val = row[5]

        prior_sessions, prior_conversion_rate, prior_revenue = prior_lookup.get(
            channel_val, (None, None, None)
        )

        rows.append((
            week_ending_date,
            channel_val,
            sessions_val,
            engaged_val,
            conversions_val,
            revenue_val,
            conversion_rate_val,
            prior_sessions,
            prior_conversion_rate,
            wow_pct(sessions_val, prior_sessions),
            wow_pct(revenue_val, prior_revenue),
        ))

    with db_conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO metrics_channel_weekly (
                week_ending, channel_group,
                sessions, engaged_sessions, conversions, revenue,
                conversion_rate,
                prev_sessions, prev_conversion_rate,
                sessions_wow_pct, revenue_wow_pct
            ) VALUES %s
            ON CONFLICT (week_ending, channel_group) DO UPDATE SET
                sessions             = EXCLUDED.sessions,
                engaged_sessions     = EXCLUDED.engaged_sessions,
                conversions          = EXCLUDED.conversions,
                revenue              = EXCLUDED.revenue,
                conversion_rate      = EXCLUDED.conversion_rate,
                prev_sessions        = EXCLUDED.prev_sessions,
                prev_conversion_rate = EXCLUDED.prev_conversion_rate,
                sessions_wow_pct     = EXCLUDED.sessions_wow_pct,
                revenue_wow_pct      = EXCLUDED.revenue_wow_pct
        """, rows, template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)")
    db_conn.commit()
    return len(rows)
