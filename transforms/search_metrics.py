"""
Search metrics transform - aggregates GSC data weekly, classifies branded queries.

Reads:  raw_gsc_daily, ref_branded_keywords
Writes: metrics_search_weekly
"""

from datetime import datetime, timedelta

import psycopg2.extras


def is_branded(query: str, branded_keywords: list[str]) -> bool:
    """Return True if query contains any branded keyword (case-insensitive)."""
    q = query.lower()
    return any(kw in q for kw in branded_keywords)


def wow_pct(current, prior):
    """Return week-over-week percentage change or None when undefined."""
    if prior is None or prior == 0:
        return None
    current_val = float(current)
    prior_val = float(prior)
    return float((current_val - prior_val) / abs(prior_val) * 100)


def compute_search_weekly(db_conn, week_ending: str) -> int:
    """
    Aggregate GSC daily data into weekly query-level metrics.

    Returns: number of query rows written
    """
    week_ending_date = datetime.strptime(week_ending, "%Y-%m-%d").date()
    week_start = week_ending_date - timedelta(days=6)
    prior_week_ending = week_ending_date - timedelta(days=7)

    with db_conn.cursor() as cur:
        cur.execute("SELECT keyword FROM ref_branded_keywords")
        branded_keywords = [row[0].lower() for row in cur.fetchall()]

        cur.execute("""
            SELECT
                query,
                page,
                SUM(clicks) AS clicks,
                SUM(impressions) AS impressions,
                SUM(clicks)::numeric / NULLIF(SUM(impressions), 0) AS ctr,
                SUM(avg_position * impressions) / NULLIF(SUM(impressions), 0) AS avg_position
            FROM raw_gsc_daily
            WHERE date BETWEEN %s AND %s
            GROUP BY query, page
        """, (week_start, week_ending_date))
        current_rows = cur.fetchall()

        cur.execute("""
            SELECT query, page, clicks, impressions
            FROM metrics_search_weekly
            WHERE week_ending = %s
        """, (prior_week_ending,))
        prior_lookup = {
            (row[0], row[1]): (row[2], row[3])
            for row in cur.fetchall()
        }

    if not current_rows:
        return 0

    rows = []
    for row in current_rows:
        query_val = row[0]
        page_val = row[1]
        clicks_val = row[2]
        impressions_val = row[3]
        ctr_val = row[4]
        avg_position_val = row[5]

        branded = is_branded(query_val, branded_keywords)
        prev_clicks, prev_impressions = prior_lookup.get(
            (query_val, page_val), (None, None)
        )

        if prev_clicks is not None and prev_impressions and prev_impressions > 0:
            prev_ctr = float(prev_clicks) / float(prev_impressions)
        else:
            prev_ctr = None

        rows.append((
            week_ending_date,
            query_val,
            page_val,
            clicks_val,
            impressions_val,
            ctr_val,
            avg_position_val,
            branded,
            prev_clicks,
            prev_impressions,
            wow_pct(clicks_val, prev_clicks),
            wow_pct(impressions_val, prev_impressions),
            wow_pct(ctr_val, prev_ctr),
        ))

    with db_conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO metrics_search_weekly (
                week_ending, query, page,
                clicks, impressions, ctr, avg_position,
                is_branded,
                prev_clicks, prev_impressions,
                clicks_wow_pct, impressions_wow_pct, ctr_wow_pct
            ) VALUES %s
            ON CONFLICT (week_ending, query, page) DO UPDATE SET
                clicks              = EXCLUDED.clicks,
                impressions         = EXCLUDED.impressions,
                ctr                 = EXCLUDED.ctr,
                avg_position        = EXCLUDED.avg_position,
                is_branded          = EXCLUDED.is_branded,
                prev_clicks         = EXCLUDED.prev_clicks,
                prev_impressions    = EXCLUDED.prev_impressions,
                clicks_wow_pct      = EXCLUDED.clicks_wow_pct,
                impressions_wow_pct = EXCLUDED.impressions_wow_pct,
                ctr_wow_pct         = EXCLUDED.ctr_wow_pct
        """, rows, template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)")
    db_conn.commit()
    return len(rows)
