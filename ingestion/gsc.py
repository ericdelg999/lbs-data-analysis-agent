"""
Google Search Console ingestion - pulls query + page performance data.

Auth: Same Google service account as GA4 (GOOGLE_SERVICE_ACCOUNT_JSON in .env).
Table written: raw_gsc_daily

GSC data has a ~2-3 day lag. Pull with end_date = today - 3 days to avoid
incomplete data at the trailing edge.

Run schedule: weekly (Monday morning), pulls last 7 days at daily grain.
"""

import os
from datetime import datetime, timedelta

import psycopg2.extras
from dotenv import load_dotenv
from googleapiclient.discovery import build
from google.oauth2 import service_account

load_dotenv()

GSC_SITE_URL = os.getenv("GSC_SITE_URL")
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
GSC_ROW_LIMIT = 25000

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_service():
    """Return authenticated Search Console API service."""
    resolved_path = SERVICE_ACCOUNT_FILE
    if not os.path.isabs(resolved_path):
        resolved_path = os.path.join(PROJECT_ROOT, resolved_path)

    credentials = service_account.Credentials.from_service_account_file(
        resolved_path,
        scopes=SCOPES,
    )
    return build("searchconsole", "v1", credentials=credentials)


def _fetch_gsc_day(service, site_url: str, query_date: str) -> list[dict]:
    """Fetch all GSC rows for a single date, paginating with startRow."""
    all_rows = []
    start_row = 0

    while True:
        body = {
            "startDate": query_date,
            "endDate": query_date,
            "dimensions": ["query", "page"],
            "rowLimit": GSC_ROW_LIMIT,
            "startRow": start_row,
        }
        response = service.searchanalytics().query(
            siteUrl=site_url,
            body=body,
        ).execute()
        rows = response.get("rows", [])
        all_rows.extend(rows)
        if len(rows) < GSC_ROW_LIMIT:
            break
        start_row += GSC_ROW_LIMIT

    return all_rows


def ingest_query_data(service, db_conn, start_date: str, end_date: str) -> int:
    """
    Pull query + page performance by day and upsert into raw_gsc_daily.

    Returns: number of rows written
    """
    total_rows = 0
    current = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()

    while current <= end:
        day_str = current.strftime("%Y-%m-%d")
        gsc_rows = _fetch_gsc_day(service, GSC_SITE_URL, day_str)

        if gsc_rows:
            rows = []
            for row in gsc_rows:
                keys = row.get("keys", [])
                if len(keys) < 2:
                    continue

                rows.append((
                    current,
                    keys[0],
                    keys[1],
                    int(row.get("clicks", 0)),
                    int(row.get("impressions", 0)),
                    float(row.get("ctr", 0)),
                    float(row.get("position", 0)),
                ))

            if rows:
                with db_conn.cursor() as cur:
                    psycopg2.extras.execute_values(cur, """
                        INSERT INTO raw_gsc_daily
                            (date, query, page, clicks, impressions,
                             ctr, avg_position, ingested_at)
                        VALUES %s
                        ON CONFLICT (date, query, page) DO UPDATE SET
                            clicks       = EXCLUDED.clicks,
                            impressions  = EXCLUDED.impressions,
                            ctr          = EXCLUDED.ctr,
                            avg_position = EXCLUDED.avg_position,
                            ingested_at  = NOW()
                    """, rows, template="(%s, %s, %s, %s, %s, %s, %s, NOW())")
                db_conn.commit()
                total_rows += len(rows)

        current += timedelta(days=1)

    return total_rows


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
        """, ("gsc", date_start, date_end, rows_written, status, error_message, duration))
    db_conn.commit()


def run(db_conn, lookback_days: int = 7):
    """Run GSC ingestion. Called by scheduler/weekly_job.py."""
    if not GSC_SITE_URL or not SERVICE_ACCOUNT_FILE:
        raise RuntimeError(
            "GSC_SITE_URL and GOOGLE_SERVICE_ACCOUNT_JSON must be set in .env"
        )

    end = (datetime.now() - timedelta(days=3)).date()
    start = end - timedelta(days=lookback_days)

    ingest_start = datetime.utcnow()
    rows = 0
    try:
        service = get_service()
        rows += ingest_query_data(
            service,
            db_conn,
            start.strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d"),
        )
        _log_ingestion(db_conn, "success", rows, ingest_start, lookback_days)
        print(f"  [gsc] {rows} rows written in "
              f"{(datetime.utcnow() - ingest_start).total_seconds():.1f}s")
    except Exception as e:
        db_conn.rollback()
        _log_ingestion(db_conn, "failed", rows, ingest_start, lookback_days, str(e))
        raise
    return rows
