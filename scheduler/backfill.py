"""
Historical backfill - populates Supabase with 16-24 months of raw + weekly data.

Run once:
  python scheduler/backfill.py

Resume-safe:
  - Raw ingestion uses source-level upserts / conflict handling
  - Transforms overwrite their weekly rows via upsert
  - Chunk-level results are logged to ingestion_log

Examples:
  python scheduler/backfill.py
  python scheduler/backfill.py --months 6
  python scheduler/backfill.py --source ga4
  python scheduler/backfill.py --transforms-only
  python scheduler/backfill.py --skip-transforms
  python scheduler/backfill.py --start-date 2025-01-01
"""

import argparse
import os
import sys
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlparse

import psycopg2
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from ingestion import bigcommerce, ga4, google_ads, gsc
from transforms import (
    ai_referral_metrics,
    brand_metrics,
    funnel_metrics,
    paid_metrics,
    product_metrics,
    search_metrics,
)

SUPPORTED_SOURCES = ("ga4", "gsc", "google_ads", "bigcommerce")
MAX_MONTHS_BY_SOURCE = {
    "ga4": 24,
    "gsc": 16,
    "google_ads": 24,
    "bigcommerce": 24,
}
GSC_LAG_DAYS = 3


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp for durations and log rows."""
    return datetime.now(timezone.utc)


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the backfill job."""
    parser = argparse.ArgumentParser(
        description="Backfill historical raw data and weekly metrics.",
    )
    parser.add_argument(
        "--months",
        type=int,
        default=24,
        help="Number of months to backfill when --start-date is not provided (default: 24).",
    )
    parser.add_argument(
        "--transforms-only",
        action="store_true",
        help="Skip ingestion and recompute weekly transforms only.",
    )
    parser.add_argument(
        "--source",
        choices=SUPPORTED_SOURCES,
        help="Limit ingestion to one source.",
    )
    parser.add_argument(
        "--start-date",
        help="Override the computed start date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--skip-transforms",
        action="store_true",
        help="Run ingestion only and skip weekly transform recomputation.",
    )
    args = parser.parse_args()

    if args.months <= 0:
        parser.error("--months must be greater than 0")
    if args.transforms_only and args.skip_transforms:
        parser.error("--transforms-only and --skip-transforms cannot be used together")

    return args


def get_db_connection():
    """
    Return a psycopg2 connection using DATABASE_URL components as kwargs.

    Important: the password is treated literally and is not URL-decoded.
    """
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set in .env")

    parsed = urlparse(url)
    return psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        user=parsed.username,
        password=parsed.password,
        dbname=parsed.path.lstrip("/") or "postgres",
    )


def parse_iso_date(value: str) -> date:
    """Parse a YYYY-MM-DD string into a date."""
    return datetime.strptime(value, "%Y-%m-%d").date()


def add_months(value: date, months: int) -> date:
    """Return a date shifted by whole months, clamping to month end when needed."""
    month_index = (value.year * 12 + (value.month - 1)) + months
    year = month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def compute_requested_start(args: argparse.Namespace, today: date) -> date:
    """Return the requested global start date."""
    if args.start_date:
        return parse_iso_date(args.start_date)
    return add_months(today, -args.months)


def compute_source_window(source: str, requested_start: date, today: date) -> tuple[date, date]:
    """Return the effective start/end dates for a given source."""
    max_start = add_months(today, -MAX_MONTHS_BY_SOURCE[source])
    start = max(requested_start, max_start)
    end = today

    if source == "gsc":
        end = today - timedelta(days=GSC_LAG_DAYS)

    return start, end


def iter_month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split a date range into calendar-month chunks, oldest first."""
    if start > end:
        return []

    chunks = []
    cursor = start
    while cursor <= end:
        month_end = date(cursor.year, cursor.month, monthrange(cursor.year, cursor.month)[1])
        chunk_end = min(month_end, end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def get_last_sunday(reference_date: date) -> date:
    """Return the most recent Sunday on or before reference_date."""
    days_since_sunday = (reference_date.weekday() + 1) % 7
    return reference_date - timedelta(days=days_since_sunday)


def iter_week_endings(start: date, end: date) -> list[date]:
    """Return all Sundays from start..end inclusive, oldest first."""
    if start > end:
        return []

    days_until_sunday = (6 - start.weekday()) % 7
    first_sunday = start + timedelta(days=days_until_sunday)
    if first_sunday > end:
        return []

    week_endings = []
    current = first_sunday
    while current <= end:
        week_endings.append(current)
        current += timedelta(days=7)
    return week_endings


def log_ingestion_chunk(
    db_conn,
    source: str,
    date_start: date | None,
    date_end: date | None,
    rows_written: int,
    status: str,
    started_at: datetime,
    error_message: str | None = None,
):
    """Write one chunk-level row to ingestion_log."""
    duration_seconds = (utc_now() - started_at).total_seconds()
    with db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingestion_log
                (source, date_range_start, date_range_end, rows_written,
                 status, error_message, duration_seconds)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                source,
                date_start,
                date_end,
                rows_written,
                status,
                error_message,
                duration_seconds,
            ),
        )
    db_conn.commit()


def run_chunk(
    db_conn,
    source: str,
    chunk_start: date,
    chunk_end: date,
    runner: Callable[[], int],
    label: str,
    chunk_index: int,
    total_chunks: int,
    failures: list[dict],
) -> int:
    """Run one ingestion chunk, log the result, and continue on failure."""
    started_at = utc_now()
    rows_written = 0
    try:
        rows_written = runner()
        log_ingestion_chunk(
            db_conn=db_conn,
            source=source,
            date_start=chunk_start,
            date_end=chunk_end,
            rows_written=rows_written,
            status="success",
            started_at=started_at,
        )
        duration = (utc_now() - started_at).total_seconds()
        print(
            f"[{label}] Chunk {chunk_index}/{total_chunks}: "
            f"{chunk_start} to {chunk_end} - {rows_written:,} rows ({duration:.1f}s)"
        )
        return rows_written
    except Exception as exc:
        db_conn.rollback()
        log_ingestion_chunk(
            db_conn=db_conn,
            source=source,
            date_start=chunk_start,
            date_end=chunk_end,
            rows_written=rows_written,
            status="failed",
            started_at=started_at,
            error_message=str(exc),
        )
        duration = (utc_now() - started_at).total_seconds()
        print(
            f"[{label}] Chunk {chunk_index}/{total_chunks}: "
            f"{chunk_start} to {chunk_end} - FAILED ({duration:.1f}s): {exc}"
        )
        failures.append(
            {
                "phase": "ingestion",
                "source": source,
                "start": chunk_start,
                "end": chunk_end,
                "error": str(exc),
            }
        )
        return 0


def run_ga4_backfill(db_conn, start: date, end: date, failures: list[dict]) -> int:
    """Backfill GA4 data in month chunks."""
    if not ga4.GA4_PROPERTY_ID or not ga4.SERVICE_ACCOUNT_FILE:
        raise RuntimeError("GA4_PROPERTY_ID and GOOGLE_SERVICE_ACCOUNT_JSON must be set in .env")

    chunks = iter_month_chunks(start, end)
    if not chunks:
        print("[ga4] No date range to process.")
        return 0

    client = ga4.get_client()
    total_rows = 0

    for index, (chunk_start, chunk_end) in enumerate(chunks, start=1):
        start_str = chunk_start.strftime("%Y-%m-%d")
        end_str = chunk_end.strftime("%Y-%m-%d")
        total_rows += run_chunk(
            db_conn=db_conn,
            source="ga4",
            chunk_start=chunk_start,
            chunk_end=chunk_end,
            label="ga4",
            chunk_index=index,
            total_chunks=len(chunks),
            failures=failures,
            runner=lambda s=start_str, e=end_str: (
                ga4.ingest_site_metrics(client, db_conn, s, e)
                + ga4.ingest_page_metrics(client, db_conn, s, e)
                + ga4.ingest_product_metrics(client, db_conn, s, e)
                + ga4.ingest_channel_metrics(client, db_conn, s, e)
                + ga4.ingest_source_metrics(client, db_conn, s, e)
            ),
        )

    return total_rows


def run_gsc_backfill(db_conn, start: date, end: date, failures: list[dict]) -> int:
    """Backfill GSC data in month chunks."""
    if not gsc.GSC_SITE_URL or not gsc.SERVICE_ACCOUNT_FILE:
        raise RuntimeError("GSC_SITE_URL and GOOGLE_SERVICE_ACCOUNT_JSON must be set in .env")

    chunks = iter_month_chunks(start, end)
    if not chunks:
        print("[gsc] No date range to process.")
        return 0

    service = gsc.get_service()
    total_rows = 0

    for index, (chunk_start, chunk_end) in enumerate(chunks, start=1):
        start_str = chunk_start.strftime("%Y-%m-%d")
        end_str = chunk_end.strftime("%Y-%m-%d")
        total_rows += run_chunk(
            db_conn=db_conn,
            source="gsc",
            chunk_start=chunk_start,
            chunk_end=chunk_end,
            label="gsc",
            chunk_index=index,
            total_chunks=len(chunks),
            failures=failures,
            runner=lambda s=start_str, e=end_str: gsc.ingest_query_data(service, db_conn, s, e),
        )

    return total_rows


def run_google_ads_backfill(db_conn, start: date, end: date, failures: list[dict]) -> int:
    """Backfill Google Ads data in month chunks."""
    required_vars = [
        "GOOGLE_ADS_DEVELOPER_TOKEN",
        "GOOGLE_ADS_CLIENT_ID",
        "GOOGLE_ADS_CLIENT_SECRET",
        "GOOGLE_ADS_REFRESH_TOKEN",
        "GOOGLE_ADS_CUSTOMER_ID",
    ]
    missing = [var for var in required_vars if not os.getenv(var)]
    if missing:
        raise RuntimeError(f"Google Ads env vars missing: {missing}")

    chunks = iter_month_chunks(start, end)
    if not chunks:
        print("[google_ads] No date range to process.")
        return 0

    client = google_ads.get_client()
    total_rows = 0

    for index, (chunk_start, chunk_end) in enumerate(chunks, start=1):
        start_str = chunk_start.strftime("%Y-%m-%d")
        end_str = chunk_end.strftime("%Y-%m-%d")
        total_rows += run_chunk(
            db_conn=db_conn,
            source="google_ads",
            chunk_start=chunk_start,
            chunk_end=chunk_end,
            label="google_ads",
            chunk_index=index,
            total_chunks=len(chunks),
            failures=failures,
            runner=lambda s=start_str, e=end_str: google_ads.ingest_campaign_performance(
                client,
                db_conn,
                s,
                e,
            ),
        )

    return total_rows


def run_bigcommerce_backfill(db_conn, start: date, end: date, failures: list[dict]) -> int:
    """Backfill BigCommerce catalog snapshot + historical orders."""
    if not bigcommerce.BC_STORE_HASH or not bigcommerce.BC_ACCESS_TOKEN:
        raise RuntimeError(
            "BC_STORE_HASH and BC_ACCESS_TOKEN must be set in .env before backfill"
        )

    total_rows = 0
    today = date.today()

    total_rows += run_chunk(
        db_conn=db_conn,
        source="bigcommerce",
        chunk_start=today,
        chunk_end=today,
        label="bigcommerce",
        chunk_index=1,
        total_chunks=2,
        failures=failures,
        runner=lambda: (
            bigcommerce.ingest_brands(db_conn)
            + bigcommerce.ingest_categories(db_conn)
            + bigcommerce.ingest_products(db_conn)
        ),
    )

    lookback_days = max((end - start).days + 1, 1)
    total_rows += run_chunk(
        db_conn=db_conn,
        source="bigcommerce",
        chunk_start=start,
        chunk_end=end,
        label="bigcommerce",
        chunk_index=2,
        total_chunks=2,
        failures=failures,
        runner=lambda days=lookback_days: bigcommerce.ingest_orders(db_conn, lookback_days=days),
    )

    return total_rows


def run_transforms(db_conn, start: date, end: date, failures: list[dict]) -> tuple[int, int]:
    """Recompute weekly metrics from oldest week to newest."""
    week_endings = iter_week_endings(start, get_last_sunday(end))
    if not week_endings:
        print("[transforms] No Sundays in range. Skipping transforms.")
        return 0, 0

    total_rows = 0
    completed_weeks = 0
    transform_steps = [
        ("product_metrics", product_metrics.compute_product_weekly),
        ("brand_metrics", brand_metrics.compute_brand_weekly),
        ("funnel_metrics", funnel_metrics.compute_funnel_weekly),
        ("channel_metrics", paid_metrics.compute_channel_weekly),
        ("search_metrics", search_metrics.compute_search_weekly),
        ("paid_metrics", paid_metrics.compute_paid_weekly),
        ("ai_referral_metrics", ai_referral_metrics.compute_ai_referral_weekly),
    ]

    for index, week_ending in enumerate(week_endings, start=1):
        started_at = utc_now()
        week_rows = 0
        try:
            for _, transform_fn in transform_steps:
                week_rows += transform_fn(db_conn, week_ending.strftime("%Y-%m-%d"))

            duration = (utc_now() - started_at).total_seconds()
            print(
                f"[transforms] Week {index}/{len(week_endings)}: "
                f"{week_ending} - done ({duration:.1f}s, {week_rows:,} rows)"
            )
            total_rows += week_rows
            completed_weeks += 1
        except Exception as exc:
            db_conn.rollback()
            duration = (utc_now() - started_at).total_seconds()
            print(
                f"[transforms] Week {index}/{len(week_endings)}: "
                f"{week_ending} - FAILED ({duration:.1f}s): {exc}"
            )
            failures.append(
                {
                    "phase": "transform",
                    "week_ending": week_ending,
                    "error": str(exc),
                }
            )

    return total_rows, completed_weeks


def print_source_plan(args: argparse.Namespace, requested_start: date, today: date):
    """Print the effective source windows before work starts."""
    sources = [args.source] if args.source else list(SUPPORTED_SOURCES)
    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"Starting backfill"
    )
    print(f"Requested start: {requested_start}")
    print(f"Today: {today}")

    if args.transforms_only:
        print("Mode: transforms-only")
    elif args.skip_transforms:
        print("Mode: ingestion-only")
    else:
        print("Mode: ingestion + transforms")

    for source in sources:
        source_start, source_end = compute_source_window(source, requested_start, today)
        if source_start > source_end:
            print(f"  - {source}: no eligible date range")
        else:
            print(f"  - {source}: {source_start} to {source_end}")


def print_failure_summary(failures: list[dict]):
    """Print any failed ingestion chunks or transform weeks."""
    if not failures:
        return

    print("\n[WARN] Some backfill steps failed:")
    for failure in failures:
        if failure["phase"] == "ingestion":
            print(
                f"  - ingestion/{failure['source']}: "
                f"{failure['start']} to {failure['end']} - {failure['error']}"
            )
        else:
            print(
                f"  - transform/{failure['week_ending']}: {failure['error']}"
            )


def main():
    """Run the requested ingestion and transform backfill workflow."""
    args = parse_args()
    today = date.today()
    requested_start = compute_requested_start(args, today)

    if requested_start > today:
        raise RuntimeError("--start-date cannot be in the future")

    print_source_plan(args, requested_start, today)

    started_at = utc_now()
    failures: list[dict] = []
    total_ingested_rows = 0
    total_transform_rows = 0
    transformed_weeks = 0

    db_conn = get_db_connection()
    try:
        if not args.transforms_only:
            sources = [args.source] if args.source else list(SUPPORTED_SOURCES)
            ingestion_runners = {
                "ga4": run_ga4_backfill,
                "gsc": run_gsc_backfill,
                "google_ads": run_google_ads_backfill,
                "bigcommerce": run_bigcommerce_backfill,
            }

            for source in sources:
                source_start, source_end = compute_source_window(source, requested_start, today)
                if source_start > source_end:
                    print(f"[{source}] Skipping - no eligible date range.")
                    continue
                source_started_at = utc_now()
                try:
                    total_ingested_rows += ingestion_runners[source](
                        db_conn,
                        source_start,
                        source_end,
                        failures,
                    )
                except Exception as exc:
                    db_conn.rollback()
                    log_ingestion_chunk(
                        db_conn=db_conn,
                        source=source,
                        date_start=source_start,
                        date_end=source_end,
                        rows_written=0,
                        status="failed",
                        started_at=source_started_at,
                        error_message=str(exc),
                    )
                    print(f"[{source}] Source setup failed: {exc}")
                    failures.append(
                        {
                            "phase": "ingestion",
                            "source": source,
                            "start": source_start,
                            "end": source_end,
                            "error": str(exc),
                        }
                    )

        if not args.skip_transforms:
            total_transform_rows, transformed_weeks = run_transforms(
                db_conn,
                requested_start,
                today,
                failures,
            )
    finally:
        db_conn.close()

    total_duration = utc_now() - started_at
    print(
        f"\n[DONE] Backfill complete: "
        f"{total_ingested_rows:,} ingested rows, "
        f"{transformed_weeks} weeks transformed, "
        f"{total_transform_rows:,} transform rows ({total_duration})"
    )
    print_failure_summary(failures)


if __name__ == "__main__":
    main()
