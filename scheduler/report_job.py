"""
Report job orchestrator - runs the full pipeline and produces a rolling-period report.

Execution order:
  1. Ingest: bigcommerce -> ga4 -> gsc -> google_ads
  2. Transform: product_metrics -> brand_metrics -> funnel_metrics
                -> channel_metrics (paid_metrics.py) -> search_metrics -> paid_metrics
  3. Analyze: merchandising -> funnel -> search -> paid_media -> ai_referral
  4. Report: executive_briefer

Transforms stay weekly-grain. Analysts and the briefer aggregate at read time
using a configurable rolling period (default 4 weeks).
"""

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysts import ai_referral, executive_briefer, funnel, merchandising, paid_media, search
from ingestion import bigcommerce, ga4, google_ads, gsc
from transforms import ai_referral_metrics, brand_metrics, funnel_metrics, paid_metrics, product_metrics, search_metrics


def get_week_ending() -> str:
    """Return the most recent Sunday as a YYYY-MM-DD string."""
    today = date.today()
    days_since_sunday = (today.weekday() + 1) % 7
    last_sunday = today - timedelta(days=days_since_sunday)
    return last_sunday.strftime("%Y-%m-%d")


def get_db_connection():
    """
    Return a psycopg2 connection from DATABASE_URL components.

    Important: the password is treated literally and not URL-decoded.
    """
    import psycopg2

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


def run_pipeline(week_ending: str | None = None, period_weeks: int = 4, skip_ingestion: bool = False):
    """Execute the full report pipeline for the requested rolling window."""
    period_ending = week_ending or get_week_ending()
    lookback_days = int(os.getenv("REPORT_LOOKBACK_DAYS", 7))
    print(
        f"[{datetime.now()}] Starting report pipeline for "
        f"{period_weeks}-week period ending {period_ending}"
    )

    db_conn = get_db_connection()

    if skip_ingestion:
        print("Step 1/4: Ingestion (SKIPPED — using existing data)")
    else:
        print("Step 1/4: Ingestion")
        bigcommerce.run(db_conn, lookback_days)
        ga4.run(db_conn, lookback_days)
        gsc.run(db_conn, lookback_days)
        try:
            google_ads.run(db_conn, lookback_days)
        except Exception as exc:
            print(f"  [google_ads] SKIPPED - ingestion failed: {exc}")
            print("  [google_ads] Pipeline continues with available data. Paid media findings may be stale.")

    print("Step 2/4: Transforms")
    product_metrics.compute_product_weekly(db_conn, period_ending)
    brand_metrics.compute_brand_weekly(db_conn, period_ending)
    funnel_metrics.compute_funnel_weekly(db_conn, period_ending)
    paid_metrics.compute_channel_weekly(db_conn, period_ending)
    search_metrics.compute_search_weekly(db_conn, period_ending)
    paid_metrics.compute_paid_weekly(db_conn, period_ending)
    ai_referral_metrics.compute_ai_referral_weekly(db_conn, period_ending)

    print("Step 3/4: Analysis")
    merchandising.run(db_conn, period_ending, period_weeks=period_weeks)
    funnel.run(db_conn, period_ending, period_weeks=period_weeks)
    search.run(db_conn, period_ending, period_weeks=period_weeks)
    paid_media.run(db_conn, period_ending, period_weeks=period_weeks)
    ai_referral.run(db_conn, period_ending, period_weeks=period_weeks)

    print("Step 4/4: Report generation")
    report = executive_briefer.generate_report(db_conn, period_ending, period_weeks=period_weeks)

    db_conn.close()
    print(f"[{datetime.now()}] Pipeline complete for {period_weeks}-week period ending {period_ending}")
    return report


def main():
    """CLI entry point for the report pipeline."""
    parser = argparse.ArgumentParser(description="LBS report pipeline")
    parser.add_argument(
        "--period-weeks",
        type=int,
        default=int(os.getenv("ANALYSIS_PERIOD_WEEKS", "4")),
        help="Rolling aggregation window in weeks (default 4)",
    )
    parser.add_argument(
        "--week-ending",
        type=str,
        default=None,
        help="Override period ending date (YYYY-MM-DD Sunday). Default: most recent Sunday.",
    )
    parser.add_argument(
        "--skip-ingestion",
        action="store_true",
        default=False,
        help="Skip ingestion step and use existing data. Useful for re-running analysis/report only.",
    )
    args = parser.parse_args()
    run_pipeline(week_ending=args.week_ending, period_weeks=args.period_weeks, skip_ingestion=args.skip_ingestion)


if __name__ == "__main__":
    main()
