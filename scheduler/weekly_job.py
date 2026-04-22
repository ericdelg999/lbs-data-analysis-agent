"""Backward-compatible shim for scripts that still import scheduler.weekly_job."""

from scheduler.report_job import get_db_connection, get_week_ending, main, run_pipeline


if __name__ == "__main__":
    main()
