"""
Google Analytics 4 ingestion - pulls site, page, product, and channel data.

Auth: Google service account JSON key (GOOGLE_SERVICE_ACCOUNT_JSON in .env).
Tables written: raw_ga4_daily, raw_ga4_pages_daily, raw_ga4_products_daily,
                raw_ga4_traffic_channels_daily, raw_ga4_sources_daily

Key note: GA4 item_id = BigCommerce SKU field (verified 2026-04-03).
Join to BC data via raw_bc_products.sku.

Run schedule: weekly (Monday morning), pulls last 7 days at daily grain.
"""

import os
from datetime import datetime, timedelta

import psycopg2.extras
from dotenv import load_dotenv
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    RunReportRequest,
)
from google.oauth2 import service_account

load_dotenv()

GA4_PROPERTY_ID = os.getenv("GA4_PROPERTY_ID")
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]
PAGE_SIZE = 10_000

# Resolve service account path relative to project root, not CWD.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_client() -> BetaAnalyticsDataClient:
    """Return authenticated GA4 Data API client."""
    resolved_path = SERVICE_ACCOUNT_FILE
    if not os.path.isabs(resolved_path):
        resolved_path = os.path.join(PROJECT_ROOT, resolved_path)

    credentials = service_account.Credentials.from_service_account_file(
        resolved_path,
        scopes=SCOPES,
    )
    return BetaAnalyticsDataClient(credentials=credentials)


def _run_report_paginated(client, dimensions: list[str], metrics: list[str],
                          start_date: str, end_date: str) -> list:
    """
    Run a GA4 report with automatic pagination.
    Returns list of response Row objects.
    """
    all_rows = []
    offset = 0

    while True:
        request = RunReportRequest(
            property=f"properties/{GA4_PROPERTY_ID}",
            dimensions=[Dimension(name=d) for d in dimensions],
            metrics=[Metric(name=m) for m in metrics],
            date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
            limit=PAGE_SIZE,
            offset=offset,
        )
        response = client.run_report(request)
        all_rows.extend(response.rows)
        if len(all_rows) >= response.row_count or not response.rows:
            break
        offset += PAGE_SIZE

    return all_rows


def _normalize_ga4_dimension(value: str | None) -> str | None:
    """
    Normalize GA4 dimension values to a clean string or None.

    GA4 can surface missing values as empty strings, "(not set)", or other
    null-like strings depending on the report and client behavior.
    """
    if value is None:
        return None

    normalized = value.strip()
    if not normalized:
        return None

    if normalized.lower() in {"(not set)", "none", "null"}:
        return None

    return normalized


def ingest_site_metrics(client, db_conn, start_date: str, end_date: str) -> int:
    """
    Pull site-level daily metrics and upsert into raw_ga4_daily.

    Returns: number of rows written
    """
    response_rows = _run_report_paginated(
        client,
        dimensions=["date"],
        metrics=[
            "sessions",
            "engagedSessions",
            "bounceRate",
            "averageSessionDuration",
            "newUsers",
            "totalUsers",
            "screenPageViews",
        ],
        start_date=start_date,
        end_date=end_date,
    )
    if not response_rows:
        return 0

    rows = []
    for row in response_rows:
        rows.append((
            datetime.strptime(row.dimension_values[0].value, "%Y%m%d").date(),
            int(row.metric_values[0].value),
            int(row.metric_values[1].value),
            float(row.metric_values[2].value),
            float(row.metric_values[3].value),
            int(row.metric_values[4].value),
            int(row.metric_values[5].value),
            int(row.metric_values[6].value),
        ))

    with db_conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO raw_ga4_daily
                (date, sessions, engaged_sessions, bounce_rate, avg_session_duration,
                 new_users, total_users, screen_page_views, ingested_at)
            VALUES %s
            ON CONFLICT (date) DO UPDATE SET
                sessions             = EXCLUDED.sessions,
                engaged_sessions     = EXCLUDED.engaged_sessions,
                bounce_rate          = EXCLUDED.bounce_rate,
                avg_session_duration = EXCLUDED.avg_session_duration,
                new_users            = EXCLUDED.new_users,
                total_users          = EXCLUDED.total_users,
                screen_page_views    = EXCLUDED.screen_page_views,
                ingested_at          = NOW()
        """, rows, template="(%s, %s, %s, %s, %s, %s, %s, %s, NOW())")
    db_conn.commit()
    return len(rows)


def ingest_page_metrics(client, db_conn, start_date: str, end_date: str) -> int:
    """
    Pull per-page daily metrics and upsert into raw_ga4_pages_daily.

    Returns: number of rows written
    """
    # pageTitle excluded from dimensions — including it causes GA4 to split
    # rows by title variant for the same path, producing duplicate (date, page_path)
    # keys that violate the DB UNIQUE constraint in a single execute_values batch.
    response_rows = _run_report_paginated(
        client,
        dimensions=["date", "pagePath"],
        metrics=[
            "sessions",
            "engagedSessions",
            "bounceRate",
            "averageSessionDuration",
            "screenPageViews",
        ],
        start_date=start_date,
        end_date=end_date,
    )
    if not response_rows:
        return 0

    rows = []
    for row in response_rows:
        screen_page_views = int(row.metric_values[4].value)
        if screen_page_views == 0:
            continue

        rows.append((
            datetime.strptime(row.dimension_values[0].value, "%Y%m%d").date(),
            row.dimension_values[1].value,
            None,  # page_title — not fetched to avoid duplicate key splits
            int(row.metric_values[0].value),
            int(row.metric_values[1].value),
            float(row.metric_values[2].value),
            float(row.metric_values[3].value),
            screen_page_views,
        ))

    if not rows:
        return 0

    with db_conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO raw_ga4_pages_daily
                (date, page_path, page_title, sessions, engaged_sessions,
                 bounce_rate, avg_time_on_page, screen_page_views, ingested_at)
            VALUES %s
            ON CONFLICT (date, page_path) DO UPDATE SET
                page_title           = EXCLUDED.page_title,
                sessions             = EXCLUDED.sessions,
                engaged_sessions     = EXCLUDED.engaged_sessions,
                bounce_rate          = EXCLUDED.bounce_rate,
                avg_time_on_page     = EXCLUDED.avg_time_on_page,
                screen_page_views    = EXCLUDED.screen_page_views,
                ingested_at          = NOW()
        """, rows, template="(%s, %s, %s, %s, %s, %s, %s, %s, NOW())")
    db_conn.commit()
    return len(rows)


def ingest_product_metrics(client, db_conn, start_date: str, end_date: str) -> int:
    """
    Pull per-product ecommerce events and upsert into raw_ga4_products_daily.

    Returns: number of rows written
    """
    response_rows = _run_report_paginated(
        client,
        dimensions=["date", "itemId", "itemName", "itemCategory", "itemBrand"],
        metrics=[
            "itemsViewed",
            "itemsAddedToCart",
            "itemsCheckedOut",
            "itemsPurchased",
            "itemRevenue",
        ],
        start_date=start_date,
        end_date=end_date,
    )
    if not response_rows:
        return 0

    # GA4 can return multiple rows per (date, item_id) when itemName/Category/Brand
    # vary across events (e.g., product renamed mid-day). Aggregate to avoid
    # duplicate key errors on the (date, item_id) UNIQUE constraint.
    deduped = {}  # key: (date, item_id) -> aggregated row dict
    for row in response_rows:
        item_id = row.dimension_values[1].value.strip()
        if not item_id or item_id == "(not set)":
            continue

        date_val = datetime.strptime(row.dimension_values[0].value, "%Y%m%d").date()
        views = int(row.metric_values[0].value)
        key = (date_val, item_id)

        if key not in deduped:
            deduped[key] = {
                "date": date_val,
                "item_id": item_id,
                "item_name": row.dimension_values[2].value,
                "item_category": row.dimension_values[3].value,
                "item_brand": row.dimension_values[4].value,
                "views": views,
                "add_to_carts": int(row.metric_values[1].value),
                "checkouts": int(row.metric_values[2].value),
                "purchases": int(row.metric_values[3].value),
                "purchase_revenue": float(row.metric_values[4].value),
                "max_views": views,  # track which sub-row had most views for name/category
            }
        else:
            d = deduped[key]
            d["views"] += views
            d["add_to_carts"] += int(row.metric_values[1].value)
            d["checkouts"] += int(row.metric_values[2].value)
            d["purchases"] += int(row.metric_values[3].value)
            d["purchase_revenue"] += float(row.metric_values[4].value)
            # Keep name/category/brand from the sub-row with the most views
            if views > d["max_views"]:
                d["max_views"] = views
                d["item_name"] = row.dimension_values[2].value
                d["item_category"] = row.dimension_values[3].value
                d["item_brand"] = row.dimension_values[4].value

    if not deduped:
        return 0

    rows = [
        (d["date"], d["item_id"], d["item_name"], d["item_category"], d["item_brand"],
         d["views"], d["add_to_carts"], d["checkouts"], d["purchases"], d["purchase_revenue"])
        for d in deduped.values()
    ]

    with db_conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO raw_ga4_products_daily
                (date, item_id, item_name, item_category, item_brand,
                 views, add_to_carts, checkouts, purchases, purchase_revenue, ingested_at)
            VALUES %s
            ON CONFLICT (date, item_id) DO UPDATE SET
                item_name        = EXCLUDED.item_name,
                item_category    = EXCLUDED.item_category,
                item_brand       = EXCLUDED.item_brand,
                views            = EXCLUDED.views,
                add_to_carts     = EXCLUDED.add_to_carts,
                checkouts        = EXCLUDED.checkouts,
                purchases        = EXCLUDED.purchases,
                purchase_revenue = EXCLUDED.purchase_revenue,
                ingested_at      = NOW()
        """, rows, template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())")
    db_conn.commit()
    return len(rows)


def ingest_channel_metrics(client, db_conn, start_date: str, end_date: str) -> int:
    """
    Pull traffic by channel group and upsert into raw_ga4_traffic_channels_daily.

    Returns: number of rows written
    """
    try:
        response_rows = _run_report_paginated(
            client,
            dimensions=["date", "sessionDefaultChannelGroup"],
            metrics=["sessions", "engagedSessions", "keyEvents", "totalRevenue", "newUsers"],
            start_date=start_date,
            end_date=end_date,
        )
    except Exception as exc:
        if "keyEvents" not in str(exc):
            raise
        response_rows = _run_report_paginated(
            client,
            dimensions=["date", "sessionDefaultChannelGroup"],
            metrics=["sessions", "engagedSessions", "conversions", "totalRevenue", "newUsers"],
            start_date=start_date,
            end_date=end_date,
        )
    if not response_rows:
        return 0

    rows = []
    for row in response_rows:
        rows.append((
            datetime.strptime(row.dimension_values[0].value, "%Y%m%d").date(),
            row.dimension_values[1].value,
            int(row.metric_values[0].value),
            int(row.metric_values[1].value),
            int(float(row.metric_values[2].value)),
            float(row.metric_values[3].value),
            int(row.metric_values[4].value),
        ))

    with db_conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO raw_ga4_traffic_channels_daily
                (date, channel_group, sessions, engaged_sessions,
                 conversions, revenue, new_users, ingested_at)
            VALUES %s
            ON CONFLICT (date, channel_group) DO UPDATE SET
                sessions         = EXCLUDED.sessions,
                engaged_sessions = EXCLUDED.engaged_sessions,
                conversions      = EXCLUDED.conversions,
                revenue          = EXCLUDED.revenue,
                new_users        = EXCLUDED.new_users,
                ingested_at      = NOW()
        """, rows, template="(%s, %s, %s, %s, %s, %s, %s, NOW())")
    db_conn.commit()
    return len(rows)


def ingest_source_metrics(client, db_conn, start_date: str, end_date: str) -> int:
    """
    Pull traffic by session source + medium and upsert into raw_ga4_sources_daily.
    Used by AI referral transform to identify traffic from specific referrer domains.

    Returns: number of rows written
    """
    try:
        response_rows = _run_report_paginated(
            client,
            dimensions=["date", "sessionSource", "sessionMedium"],
            metrics=["sessions", "engagedSessions", "keyEvents", "totalRevenue", "newUsers"],
            start_date=start_date,
            end_date=end_date,
        )
    except Exception as exc:
        if "keyEvents" not in str(exc):
            raise
        response_rows = _run_report_paginated(
            client,
            dimensions=["date", "sessionSource", "sessionMedium"],
            metrics=["sessions", "engagedSessions", "conversions", "totalRevenue", "newUsers"],
            start_date=start_date,
            end_date=end_date,
        )
    if not response_rows:
        return 0

    # GA4 can return multiple rows that normalize to the same
    # (date, session_source, session_medium) key once null-like values are
    # coalesced (for example "(not set)" -> "(none)"). Aggregate first so the
    # batch upsert does not hit the UNIQUE constraint more than once.
    deduped = {}
    for row in response_rows:
        source = _normalize_ga4_dimension(row.dimension_values[1].value)
        if source is None:
            continue

        medium = _normalize_ga4_dimension(row.dimension_values[2].value)
        if medium is None:
            medium = "(none)"

        date_val = datetime.strptime(row.dimension_values[0].value, "%Y%m%d").date()
        key = (date_val, source, medium)

        if key not in deduped:
            deduped[key] = {
                "date": date_val,
                "source": source,
                "medium": medium,
                "sessions": int(row.metric_values[0].value),
                "engaged_sessions": int(row.metric_values[1].value),
                "conversions": int(float(row.metric_values[2].value)),
                "revenue": float(row.metric_values[3].value),
                "new_users": int(row.metric_values[4].value),
            }
        else:
            d = deduped[key]
            d["sessions"] += int(row.metric_values[0].value)
            d["engaged_sessions"] += int(row.metric_values[1].value)
            d["conversions"] += int(float(row.metric_values[2].value))
            d["revenue"] += float(row.metric_values[3].value)
            d["new_users"] += int(row.metric_values[4].value)

    if not deduped:
        return 0

    rows = [
        (
            d["date"],
            d["source"],
            d["medium"],
            d["sessions"],
            d["engaged_sessions"],
            d["conversions"],
            d["revenue"],
            d["new_users"],
        )
        for d in deduped.values()
    ]

    with db_conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO raw_ga4_sources_daily
                (date, session_source, session_medium, sessions, engaged_sessions,
                 conversions, revenue, new_users, ingested_at)
            VALUES %s
            ON CONFLICT (date, session_source, session_medium) DO UPDATE SET
                sessions         = EXCLUDED.sessions,
                engaged_sessions = EXCLUDED.engaged_sessions,
                conversions      = EXCLUDED.conversions,
                revenue          = EXCLUDED.revenue,
                new_users        = EXCLUDED.new_users,
                ingested_at      = NOW()
        """, rows, template="(%s, %s, %s, %s, %s, %s, %s, %s, NOW())")
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
        """, ("ga4", date_start, date_end, rows_written, status, error_message, duration))
    db_conn.commit()


def run(db_conn, lookback_days: int = 7):
    """Run all GA4 ingestion. Called by scheduler/weekly_job.py."""
    if not GA4_PROPERTY_ID or not SERVICE_ACCOUNT_FILE:
        raise RuntimeError(
            "GA4_PROPERTY_ID and GOOGLE_SERVICE_ACCOUNT_JSON must be set in .env"
        )

    end = datetime.now().date()
    start = end - timedelta(days=lookback_days)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    ingest_start = datetime.utcnow()
    rows = 0
    try:
        client = get_client()
        rows += ingest_site_metrics(client, db_conn, start_str, end_str)
        rows += ingest_page_metrics(client, db_conn, start_str, end_str)
        rows += ingest_product_metrics(client, db_conn, start_str, end_str)
        rows += ingest_channel_metrics(client, db_conn, start_str, end_str)
        rows += ingest_source_metrics(client, db_conn, start_str, end_str)
        _log_ingestion(db_conn, "success", rows, ingest_start, lookback_days)
        print(f"  [ga4] {rows} rows written in "
              f"{(datetime.utcnow() - ingest_start).total_seconds():.1f}s")
    except Exception as e:
        db_conn.rollback()
        _log_ingestion(db_conn, "failed", rows, ingest_start, lookback_days, str(e))
        raise
    return rows
