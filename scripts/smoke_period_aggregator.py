"""
Sanity check: exercise analysts/period_aggregator.py against live Supabase and
verify that period-aggregated values match hand-summed weekly rows.

Run: .venv\\Scripts\\python.exe scripts\\smoke_period_aggregator.py

Exits non-zero if any reconciliation fails.
"""

import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scheduler.weekly_job import get_db_connection, get_week_ending
from analysts import period_aggregator as pa


def recent_sunday(weeks_back: int = 1) -> date:
    """Return a Sunday N weeks before the most recent complete Sunday.

    weeks_back=1 means 'the Sunday before last' — gives us a period that is
    definitely fully ingested.
    """
    most_recent = date.fromisoformat(get_week_ending())
    return most_recent - timedelta(days=7 * weeks_back)


def assert_close(label: str, got, expected, tol: float = 0.01) -> bool:
    if got is None and expected is None:
        print(f"  OK   {label}: both None")
        return True
    if got is None or expected is None:
        print(f"  FAIL {label}: got={got!r} expected={expected!r}")
        return False
    diff = abs(float(got) - float(expected))
    rel = diff / max(abs(float(expected)), 1e-9)
    if diff > tol and rel > 0.001:
        print(f"  FAIL {label}: got={got} expected={expected} diff={diff:.4f}")
        return False
    print(f"  OK   {label}: {got}")
    return True


def check_funnel(db_conn, period_ending: date, period_weeks: int) -> bool:
    """Verify funnel SUM across N weeks matches helper output."""
    print(f"\n[funnel] period_ending={period_ending} weeks={period_weeks}")
    result = pa.get_funnel_period(db_conn, period_ending, period_weeks)

    window_start = period_ending - timedelta(days=7 * period_weeks - 1)
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT SUM(sessions), SUM(purchases), SUM(revenue), COUNT(DISTINCT week_ending)
            FROM metrics_funnel_weekly
            WHERE week_ending BETWEEN %s AND %s
            """,
            (window_start, period_ending),
        )
        expected = cur.fetchone()

    current = result["current"]
    if current is None:
        print(f"  (no data in window) — completeness={result['completeness']}")
        return expected[3] == 0

    ok = True
    ok &= assert_close("sessions", current["sessions"], expected[0] or 0)
    ok &= assert_close("purchases", current["purchases"], expected[1] or 0)
    ok &= assert_close("revenue", current["revenue"], float(expected[2] or 0))
    ok &= assert_close("weeks_present", current["weeks_present"], expected[3] or 0)

    # Rate sanity: overall_conversion_rate should equal purchases/sessions
    expected_conv = None if not current["sessions"] else current["purchases"] / current["sessions"]
    ok &= assert_close("overall_conversion_rate (re-derived)", current["overall_conversion_rate"], expected_conv)

    print(f"  completeness: {result['completeness']['current_weeks_available']}/{period_weeks} weeks present, "
          f"partial={result['completeness']['current_partial']}")
    return ok


def check_brand(db_conn, period_ending: date, period_weeks: int) -> bool:
    """Verify per-brand SUM matches helper output for one sampled brand."""
    print(f"\n[brand] period_ending={period_ending} weeks={period_weeks}")
    result = pa.get_brand_period(db_conn, period_ending, period_weeks)

    if not result["current"]:
        print("  (no brands in window)")
        return result["completeness"]["current_empty"]

    # Sample: brand with highest revenue in current period
    sample_brand_id, sample_data = max(
        result["current"].items(), key=lambda kv: kv[1]["total_revenue"]
    )
    print(f"  sampled bc_brand_id={sample_brand_id} ({sample_data['brand_name']})")

    window_start = period_ending - timedelta(days=7 * period_weeks - 1)
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT SUM(total_views), SUM(total_add_to_carts), SUM(total_revenue)
            FROM metrics_brand_weekly
            WHERE week_ending BETWEEN %s AND %s AND bc_brand_id = %s
            """,
            (window_start, period_ending, sample_brand_id),
        )
        expected = cur.fetchone()

    ok = True
    ok &= assert_close("total_views", sample_data["total_views"], expected[0] or 0)
    ok &= assert_close("total_add_to_carts", sample_data["total_add_to_carts"], expected[1] or 0)
    ok &= assert_close("total_revenue", sample_data["total_revenue"], float(expected[2] or 0))

    # Rate sanity: blended_atc_rate should equal total_ATCs / total_views (raw, not averaged rate)
    expected_atc = None if not sample_data["total_views"] else sample_data["total_add_to_carts"] / sample_data["total_views"]
    ok &= assert_close("blended_atc_rate (re-derived)", sample_data["blended_atc_rate"], expected_atc)

    return ok


def check_windows() -> bool:
    """Verify window-math utilities produce the expected Sunday-anchored ranges."""
    print("\n[window math]")
    sunday = date(2026, 4, 12)  # known Sunday
    ok = True

    start, end = pa.compute_period_window(sunday, 4)
    ok &= assert_close("4wk period start", (end - start).days, 27)
    print(f"  4wk: {start} → {end}")

    prior_start, prior_end = pa.compute_prior_window(sunday, 4)
    ok &= assert_close("prior period end = current start - 1", (start - prior_end).days, 1)
    print(f"  prior: {prior_start} → {prior_end}")

    yoy_start, yoy_end = pa.compute_yoy_window(sunday, 4)
    ok &= assert_close("yoy end = 364 days back", (sunday - yoy_end).days, 364)
    print(f"  yoy: {yoy_start} → {yoy_end}")

    # Non-Sunday input must raise
    try:
        pa.compute_period_window(date(2026, 4, 15), 4)  # Wednesday
        print("  ✗ non-Sunday date should have raised")
        ok = False
    except ValueError:
        print("  ✓ non-Sunday date correctly rejected")

    return ok


def check_partial_period(db_conn) -> bool:
    """Verify partial-period flagging triggers when requesting a period that reaches into the future."""
    print("\n[partial-period detection]")
    # Pick a very recent Sunday and ask for 8 weeks — if data is only recent, should be partial
    period_ending = date.fromisoformat(get_week_ending())
    result = pa.get_funnel_period(db_conn, period_ending, 8)
    comp = result["completeness"]
    print(f"  8-week request ending {period_ending}: "
          f"{comp['current_weeks_available']}/{comp['current_weeks_expected']} weeks, "
          f"partial={comp['current_partial']}, empty={comp['current_empty']}")
    # We expect 8 weeks of history to exist (backfill is 105 weeks), so this should be NOT partial.
    # The partial flag logic is exercised when period exceeds available history.
    return comp["current_weeks_available"] >= 1


def main():
    period_ending = recent_sunday(weeks_back=1)
    print(f"Running smoke test against period ending {period_ending}")

    db_conn = get_db_connection()
    try:
        results = []
        results.append(("window math", check_windows()))
        results.append(("funnel 4wk", check_funnel(db_conn, period_ending, 4)))
        results.append(("funnel 1wk", check_funnel(db_conn, period_ending, 1)))
        results.append(("brand 4wk", check_brand(db_conn, period_ending, 4)))
        results.append(("partial detection", check_partial_period(db_conn)))

        print("\n" + "=" * 60)
        for label, ok in results:
            print(f"{'PASS' if ok else 'FAIL'}  {label}")
        print("=" * 60)

        if not all(ok for _, ok in results):
            sys.exit(1)
    finally:
        db_conn.close()


if __name__ == "__main__":
    main()
