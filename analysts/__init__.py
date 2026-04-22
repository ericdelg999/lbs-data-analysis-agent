"""
Analyst modules - read metrics_ tables, produce structured findings JSON.
Each module returns a list of finding dicts matching the findings table schema.
No LLM calls here. All logic is deterministic rules + threshold comparisons.
Output is written to the findings table by each module's run() function.
"""

import psycopg2.extras
from psycopg2.extras import Json


def _write_findings(
    db_conn,
    findings: list[dict],
    module: str,
    week_ending,
    period_weeks: int = 1,
) -> int:
    """
    Delete prior findings for this module+week, insert new ones.
    Idempotent: safe to re-run. Returns count of findings written.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM findings WHERE module = %s AND week_ending = %s AND period_weeks = %s",
            (module, week_ending, period_weeks)
        )
        if not findings:
            db_conn.commit()
            return 0
        psycopg2.extras.execute_values(cur, """
            INSERT INTO findings (
                week_ending, module, finding_type, severity,
                title, evidence, likely_cause, suggested_action, urgency, period_weeks
            ) VALUES %s
        """, [
            (
                f["week_ending"], f["module"], f["finding_type"], f["severity"],
                f["title"], Json(f["evidence"]), f["likely_cause"],
                f["suggested_action"], f["urgency"], f.get("period_weeks", period_weeks)
            )
            for f in findings
        ], template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)")
    db_conn.commit()
    return len(findings)
