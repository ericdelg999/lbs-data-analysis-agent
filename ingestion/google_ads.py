"""
Google Ads ingestion - pulls campaign and ad group performance data.

Auth: OAuth2 + developer token. Credentials from .env.
Table written: raw_gads_daily

Key metrics include impression share split (lost to rank vs lost to budget) -
critical for LBS which struggles with impression share vs competitors.

Run schedule: weekly (Monday morning), pulls last 7 days at daily grain.
"""

import os
from datetime import datetime, timedelta

import psycopg2.extras
from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient

load_dotenv()

CUSTOMER_ID = os.getenv("GOOGLE_ADS_CUSTOMER_ID", "").replace("-", "")
MANAGER_CUSTOMER_ID = os.getenv("GOOGLE_ADS_MANAGER_CUSTOMER_ID", "").replace("-", "")


def get_client() -> GoogleAdsClient:
    """Return authenticated Google Ads API client."""
    config = {
        "developer_token": os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "client_id": os.getenv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret": os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token": os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
        "use_proto_plus": True,
    }
    if MANAGER_CUSTOMER_ID:
        config["login_customer_id"] = MANAGER_CUSTOMER_ID
    return GoogleAdsClient.load_from_dict(config)


def _fetch_campaign_share_metrics(client, start_date: str, end_date: str) -> dict:
    """
    Fetch campaign-level impression share metrics keyed by (date, campaign_id).

    Google Ads does not expose all impression share metrics on FROM ad_group.
    We fetch them at campaign level and attach them to each ad-group row so
    downstream campaign aggregation remains correct.
    """
    query = f"""
        SELECT
            segments.date,
            campaign.id,
            metrics.search_impression_share,
            metrics.search_rank_lost_impression_share,
            metrics.search_budget_lost_impression_share
        FROM campaign
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
    """

    ga_service = client.get_service("GoogleAdsService")
    stream = ga_service.search_stream(customer_id=CUSTOMER_ID, query=query)

    share_lookup = {}
    for batch in stream:
        for row in batch.results:
            key = (
                datetime.strptime(str(row.segments.date), "%Y-%m-%d").date(),
                str(row.campaign.id),
            )
            share_lookup[key] = (
                float(row.metrics.search_impression_share),
                float(row.metrics.search_rank_lost_impression_share),
                float(row.metrics.search_budget_lost_impression_share),
            )

    return share_lookup


def ingest_campaign_performance(client, db_conn, start_date: str, end_date: str) -> int:
    """
    Pull campaign + ad group performance by day and upsert into raw_gads_daily.

    Returns: number of rows written
    """
    share_lookup = _fetch_campaign_share_metrics(client, start_date, end_date)

    query = f"""
        SELECT
            segments.date,
            campaign.id,
            campaign.name,
            ad_group.id,
            ad_group.name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value
        FROM ad_group
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
    """

    ga_service = client.get_service("GoogleAdsService")
    stream = ga_service.search_stream(customer_id=CUSTOMER_ID, query=query)

    rows = []
    for batch in stream:
        for row in batch.results:
            date_val = datetime.strptime(str(row.segments.date), "%Y-%m-%d").date()
            campaign_id = str(row.campaign.id)
            search_impression_share, search_lost_is_rank, search_lost_is_budget = (
                share_lookup.get((date_val, campaign_id), (None, None, None))
            )

            rows.append((
                date_val,
                campaign_id,
                row.campaign.name,
                str(row.ad_group.id),
                row.ad_group.name,
                int(row.metrics.impressions),
                int(row.metrics.clicks),
                float(row.metrics.cost_micros) / 1_000_000,
                float(row.metrics.conversions),
                float(row.metrics.conversions_value),
                search_impression_share,
                search_lost_is_rank,
                search_lost_is_budget,
            ))

    if not rows:
        return 0

    with db_conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO raw_gads_daily
                (date, campaign_id, campaign_name, ad_group_id, ad_group_name,
                 impressions, clicks, cost, conversions, conversion_value,
                 search_impression_share, search_lost_is_rank,
                 search_lost_is_budget, ingested_at)
            VALUES %s
            ON CONFLICT (date, campaign_id, ad_group_id) DO UPDATE SET
                campaign_name           = EXCLUDED.campaign_name,
                ad_group_name           = EXCLUDED.ad_group_name,
                impressions             = EXCLUDED.impressions,
                clicks                  = EXCLUDED.clicks,
                cost                    = EXCLUDED.cost,
                conversions             = EXCLUDED.conversions,
                conversion_value        = EXCLUDED.conversion_value,
                search_impression_share = EXCLUDED.search_impression_share,
                search_lost_is_rank     = EXCLUDED.search_lost_is_rank,
                search_lost_is_budget   = EXCLUDED.search_lost_is_budget,
                ingested_at             = NOW()
        """, rows, template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())")
    db_conn.commit()
    return len(rows)


def _log_ingestion(db_conn, status: str, rows_written: int,
                   start: datetime, lookback_days: int, error_message: str = None):
    """Write a row to ingestion_log."""
    duration = (datetime.utcnow() - start).total_seconds()
    date_start = (datetime.utcnow() - timedelta(days=lookback_days)).date()
    date_end = datetime.utcnow().date()
    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ingestion_log
                (source, date_range_start, date_range_end, rows_written,
                 status, error_message, duration_seconds)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, ("google_ads", date_start, date_end, rows_written, status, error_message, duration))
    db_conn.commit()


def run(db_conn, lookback_days: int = 7):
    """Run Google Ads ingestion. Called by scheduler/weekly_job.py."""
    required_vars = [
        "GOOGLE_ADS_DEVELOPER_TOKEN",
        "GOOGLE_ADS_CLIENT_ID",
        "GOOGLE_ADS_CLIENT_SECRET",
        "GOOGLE_ADS_REFRESH_TOKEN",
    ]
    missing = [var for var in required_vars if not os.getenv(var)]
    if missing or not CUSTOMER_ID:
        raise RuntimeError(
            f"Google Ads env vars missing: {missing or ['GOOGLE_ADS_CUSTOMER_ID']}"
        )

    end = datetime.now().date()
    start = end - timedelta(days=lookback_days)

    ingest_start = datetime.utcnow()
    rows = 0
    try:
        client = get_client()
        rows += ingest_campaign_performance(
            client,
            db_conn,
            start.strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d"),
        )
        _log_ingestion(db_conn, "success", rows, ingest_start, lookback_days)
        print(f"  [google_ads] {rows} rows written in "
              f"{(datetime.utcnow() - ingest_start).total_seconds():.1f}s")
    except Exception as e:
        db_conn.rollback()
        _log_ingestion(db_conn, "failed", rows, ingest_start, lookback_days, str(e))
        raise
    return rows
