"""Compare GSC query + page performance: Apr 1-15 vs Mar 1-15, 2026.

Writes results to stdout. Designed for one-shot diagnosis of the
sales-slowdown question (are we losing SEO impressions/clicks/position?).
"""
import os
import sys
from urllib.parse import urlparse

import psycopg2
from dotenv import load_dotenv

load_dotenv()

APR_START, APR_END = "2026-04-01", "2026-04-15"
MAR_START, MAR_END = "2026-03-01", "2026-03-15"


def get_conn():
    url = os.getenv("DATABASE_URL")
    p = urlparse(url)
    return psycopg2.connect(
        host=p.hostname, port=p.port or 5432,
        user=p.username, password=p.password,
        dbname=p.path.lstrip("/") or "postgres",
    )


def fetch(cur, sql, params):
    cur.execute(sql, params)
    return cur.fetchall()


def main():
    conn = get_conn()
    cur = conn.cursor()

    # 1. Top-line totals
    totals_sql = """
        SELECT
            SUM(clicks)          AS clicks,
            SUM(impressions)     AS impressions,
            AVG(avg_position)    AS avg_position,
            CASE WHEN SUM(impressions) > 0
                 THEN SUM(clicks)::float / SUM(impressions) END AS ctr
        FROM raw_gsc_daily
        WHERE date BETWEEN %s AND %s
    """
    apr = fetch(cur, totals_sql, (APR_START, APR_END))[0]
    mar = fetch(cur, totals_sql, (MAR_START, MAR_END))[0]

    print("=" * 70)
    print(f"GSC TOTALS — Apr 1-15 vs Mar 1-15, 2026")
    print("=" * 70)
    print(f"{'Metric':<15} {'Apr MTD':>15} {'Mar same wk':>15} {'Delta':>15}")
    labels = ["Clicks", "Impressions", "Avg Position", "CTR"]
    for i, lbl in enumerate(labels):
        a = float(apr[i] or 0)
        m = float(mar[i] or 0)
        if m:
            delta = (a - m) / m * 100
            delta_str = f"{delta:+.1f}%"
        else:
            delta_str = "n/a"
        if lbl == "CTR":
            a_str, m_str = f"{a*100:.2f}%", f"{m*100:.2f}%"
        elif lbl == "Avg Position":
            a_str, m_str = f"{a:.2f}", f"{m:.2f}"
        else:
            a_str, m_str = f"{int(a):,}", f"{int(m):,}"
        print(f"{lbl:<15} {a_str:>15} {m_str:>15} {delta_str:>15}")

    # 2. Biggest impression LOSERS (queries that dropped the most)
    print()
    print("=" * 70)
    print("TOP IMPRESSION LOSERS (queries with biggest drop vs March)")
    print("=" * 70)
    losers_sql = """
        WITH apr AS (
            SELECT query,
                   SUM(impressions) AS imp,
                   SUM(clicks)      AS clk,
                   AVG(avg_position) AS pos
            FROM raw_gsc_daily
            WHERE date BETWEEN %s AND %s
            GROUP BY query
        ),
        mar AS (
            SELECT query,
                   SUM(impressions) AS imp,
                   SUM(clicks)      AS clk,
                   AVG(avg_position) AS pos
            FROM raw_gsc_daily
            WHERE date BETWEEN %s AND %s
            GROUP BY query
        )
        SELECT
            COALESCE(m.query, a.query) AS query,
            COALESCE(m.imp, 0) AS mar_imp,
            COALESCE(a.imp, 0) AS apr_imp,
            COALESCE(a.imp, 0) - COALESCE(m.imp, 0) AS delta_imp,
            COALESCE(m.clk, 0) AS mar_clk,
            COALESCE(a.clk, 0) AS apr_clk,
            COALESCE(m.pos, 0) AS mar_pos,
            COALESCE(a.pos, 0) AS apr_pos
        FROM mar m
        FULL OUTER JOIN apr a ON a.query = m.query
        WHERE COALESCE(m.imp, 0) >= 50  -- meaningful prior volume
        ORDER BY delta_imp ASC
        LIMIT 15
    """
    rows = fetch(cur, losers_sql, (APR_START, APR_END, MAR_START, MAR_END))
    print(f"{'Query':<45} {'Mar imp':>8} {'Apr imp':>8} {'d imp':>8} {'Mar clk':>8} {'Apr clk':>8}")
    for r in rows:
        q = (r[0] or "")[:42]
        print(f"{q:<45} {r[1]:>8} {r[2]:>8} {r[3]:>+8} {r[4]:>8} {r[5]:>8}")

    # 3. Click losers (where clicks dropped even if impressions held)
    print()
    print("=" * 70)
    print("TOP CLICK LOSERS (queries where clicks fell most)")
    print("=" * 70)
    click_sql = """
        WITH apr AS (
            SELECT query, SUM(clicks) AS clk, SUM(impressions) AS imp, AVG(avg_position) AS pos
            FROM raw_gsc_daily WHERE date BETWEEN %s AND %s GROUP BY query
        ),
        mar AS (
            SELECT query, SUM(clicks) AS clk, SUM(impressions) AS imp, AVG(avg_position) AS pos
            FROM raw_gsc_daily WHERE date BETWEEN %s AND %s GROUP BY query
        )
        SELECT
            COALESCE(m.query, a.query) AS query,
            COALESCE(m.clk, 0) AS mar_clk,
            COALESCE(a.clk, 0) AS apr_clk,
            COALESCE(a.clk, 0) - COALESCE(m.clk, 0) AS delta_clk,
            COALESCE(m.pos, 0) AS mar_pos,
            COALESCE(a.pos, 0) AS apr_pos,
            COALESCE(m.imp, 0) AS mar_imp,
            COALESCE(a.imp, 0) AS apr_imp
        FROM mar m
        FULL OUTER JOIN apr a ON a.query = m.query
        WHERE COALESCE(m.clk, 0) >= 5
        ORDER BY delta_clk ASC
        LIMIT 15
    """
    rows = fetch(cur, click_sql, (APR_START, APR_END, MAR_START, MAR_END))
    print(f"{'Query':<45} {'Mar clk':>8} {'Apr clk':>8} {'d clk':>8} {'Mar pos':>8} {'Apr pos':>8}")
    for r in rows:
        q = (r[0] or "")[:42]
        print(f"{q:<45} {r[1]:>8} {r[2]:>8} {r[3]:>+8} {float(r[4]):>8.1f} {float(r[5]):>8.1f}")

    # 4. Position droppers (queries where ranking got worse with meaningful impressions)
    print()
    print("=" * 70)
    print("BIGGEST POSITION DROPS (rank got worse on queries that matter)")
    print("=" * 70)
    pos_sql = """
        WITH apr AS (
            SELECT query, SUM(clicks) AS clk, SUM(impressions) AS imp, AVG(avg_position) AS pos
            FROM raw_gsc_daily WHERE date BETWEEN %s AND %s GROUP BY query
        ),
        mar AS (
            SELECT query, SUM(clicks) AS clk, SUM(impressions) AS imp, AVG(avg_position) AS pos
            FROM raw_gsc_daily WHERE date BETWEEN %s AND %s GROUP BY query
        )
        SELECT
            m.query,
            m.clk AS mar_clk,
            COALESCE(a.clk, 0) AS apr_clk,
            m.pos AS mar_pos,
            COALESCE(a.pos, 99) AS apr_pos,
            COALESCE(a.pos, 99) - m.pos AS pos_delta,
            m.imp AS mar_imp,
            COALESCE(a.imp, 0) AS apr_imp
        FROM mar m
        LEFT JOIN apr a ON a.query = m.query
        WHERE m.imp >= 100
        ORDER BY pos_delta DESC
        LIMIT 15
    """
    rows = fetch(cur, pos_sql, (APR_START, APR_END, MAR_START, MAR_END))
    print(f"{'Query':<45} {'Mar pos':>8} {'Apr pos':>8} {'d pos':>8} {'Mar imp':>8} {'Apr imp':>8}")
    for r in rows:
        q = (r[0] or "")[:42]
        print(f"{q:<45} {float(r[3]):>8.1f} {float(r[4]):>8.1f} {float(r[5]):>+8.1f} {r[6]:>8} {r[7]:>8}")

    # 5. Data coverage sanity check
    print()
    print("=" * 70)
    print("DATA COVERAGE (latest date in raw_gsc_daily)")
    print("=" * 70)
    cur.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM raw_gsc_daily")
    mn, mx, cnt = cur.fetchone()
    print(f"Date range: {mn} to {mx}  ({cnt:,} rows)")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
