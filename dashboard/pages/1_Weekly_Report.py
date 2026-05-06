import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PERIOD_WEEKS = int(os.getenv("ANALYSIS_PERIOD_WEEKS", "4"))


def _escape_streamlit_markdown(text: str) -> str:
    """Escape `$` so Streamlit's markdown engine doesn't interpret revenue
    figures as LaTeX math delimiters. Without this, "$154 ... $400" renders
    as a mangled formula with `**` becoming multiplication signs."""
    return (text or "").replace("$", r"\$")


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


def get_available_reports(db_conn) -> list[dict]:
    """Return report metadata sorted newest-first."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT week_ending, period_weeks, generated_at, model_used
            FROM reports
            WHERE period_weeks BETWEEN 1 AND 52
            ORDER BY week_ending DESC, period_weeks DESC, generated_at DESC
            """
        )
        rows = cur.fetchall()

    return [
        {
            "week_ending": row[0],
            "period_weeks": row[1] or 1,
            "generated_at": row[2],
            "model_used": row[3],
        }
        for row in rows
    ]


def get_report(db_conn, week_ending, period_weeks: int) -> dict | None:
    """Return a report row for the selected period."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT week_ending, period_weeks, full_report_md, action_items, generated_at, model_used
            FROM reports
            WHERE week_ending = %s
              AND period_weeks = %s
            """,
            (week_ending, period_weeks),
        )
        row = cur.fetchone()

    if not row:
        return None

    return {
        "week_ending": row[0],
        "period_weeks": row[1] or 1,
        "full_report_md": row[2],
        "action_items": row[3],
        "generated_at": row[4],
        "model_used": row[5],
    }


def build_report_job_env() -> dict[str, str]:
    """Return subprocess env with broken proxy vars removed."""
    env = os.environ.copy()
    for key in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "GIT_HTTP_PROXY", "GIT_HTTPS_PROXY"):
        env.pop(key, None)
    return env


st.set_page_config(page_title="LBS Intelligence Report", page_icon="📊", layout="wide")

st.markdown(
    """
    <style>
    .stSelectbox [data-baseweb="select"] > div {
        color: #063c6e !important;
        background-color: #ffffff !important;
    }
    [data-baseweb="popover"] li {
        color: #063c6e !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("LBS Intelligence Report")

db_conn = get_db_connection()
try:
    reports = get_available_reports(db_conn)

    if not reports:
        st.warning("No reports found. Run the report job first.")
        st.code("python scheduler/report_job.py")
        st.stop()

    report_options = {
        f"{report['week_ending']}|{report['period_weeks']}": report
        for report in reports
    }
    selected_key = st.selectbox(
        "Available reports",
        options=list(report_options.keys()),
        format_func=lambda value: (
            f"{report_options[value]['week_ending'].strftime('%b %d, %Y')} "
            f"— {report_options[value]['period_weeks']}-week report"
        ),
    )
    selected_report = report_options[selected_key]
    selected_period_weeks = selected_report["period_weeks"] or 1
    report = get_report(db_conn, selected_report["week_ending"], selected_period_weeks)

    if report:
        header_col, meta_col = st.columns([6, 2])
        with meta_col:
            if report["generated_at"]:
                st.caption(
                    "Generated: "
                    f"{report['generated_at'].strftime('%b %d, %Y %I:%M%p')}"
                )
            if report["model_used"]:
                model_display = report["model_used"].replace("gpt-", "GPT-")
                st.caption(f"Model: {model_display}")

        with header_col:
            st.caption(
                f"{selected_report['week_ending'].strftime('%b %d, %Y')} — {selected_period_weeks}-week report"
            )

        st.markdown(_escape_streamlit_markdown(report["full_report_md"]))
    else:
        st.error(
            f"Report not found for {selected_period_weeks}-week period ending "
            f"{selected_report['week_ending']}"
        )

    st.divider()
    button_col, period_col, options_col, help_col = st.columns([2, 2, 2, 3])
    with period_col:
        run_period_weeks = int(
            st.number_input(
                "Fresh report window (weeks)",
                min_value=1,
                max_value=52,
                value=DEFAULT_PERIOD_WEEKS,
                step=1,
            )
        )

    with options_col:
        skip_ingestion = st.checkbox(
            "Skip ingestion",
            value=False,
            help="Re-run analysis and report only using existing data. ~2 min vs 5-10 min with ingestion.",
        )

    with button_col:
        if st.button("Run Fresh Report", type="primary"):
            spinner_msg = (
                "Re-running analysis and report (~2 min)..."
                if skip_ingestion
                else "Running full pipeline including data ingestion (~5-10 min)..."
            )
            with st.spinner(spinner_msg):
                cmd = [
                    sys.executable,
                    str(PROJECT_ROOT / "scheduler" / "report_job.py"),
                    "--period-weeks",
                    str(run_period_weeks),
                ]
                if skip_ingestion:
                    cmd.append("--skip-ingestion")
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    cwd=str(PROJECT_ROOT),
                    env=build_report_job_env(),
                )

            if result.returncode == 0:
                st.success("Report generated. Reloading latest data...")
                st.rerun()
            else:
                st.error("Pipeline failed.")
                if result.stderr:
                    st.code(result.stderr)
                elif result.stdout:
                    st.code(result.stdout)

    with help_col:
        st.caption(
            "With ingestion: pulls fresh data from all sources, then generates report. "
            "Skip ingestion: re-runs analysis and report only using existing data."
        )
finally:
    db_conn.close()
