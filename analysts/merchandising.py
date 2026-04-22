"""
Merchandising Analyst - highest priority module for LBS.

Reads:  metrics_product_weekly, metrics_brand_weekly, ref_anomaly_thresholds,
        raw_bc_orders, raw_bc_order_items, raw_bc_products (for brand order context)
Writes: findings (module='merchandising')
"""

from datetime import date, datetime

from dotenv import load_dotenv

from analysts import _write_findings
from analysts.period_aggregator import get_brand_period, get_product_period, safe_pct_change

load_dotenv()

FINDING_MODULE = "merchandising"

HIGH_ATC_MIN = 0.30
LOW_CHECKOUT_MAX = 0.50
HIGH_TRAFFIC_MIN = 25
LOW_ATC_MAX = 0.05
MIN_ATC_VOLUME = 3


def _as_float(value):
    """Return float(value) unless the value is None."""
    if value is None:
        return None
    return float(value)


def _comparison_label(period_weeks: int) -> str:
    return "WoW" if period_weeks == 1 else f"vs prior {period_weeks}-week period"


def _base_evidence(completeness: dict, period_weeks: int) -> dict:
    return {
        "period_weeks": period_weeks,
        "period_start": completeness["window"]["current_start"],
        "period_end": completeness["window"]["current_end"],
        "partial_period": completeness["current_partial"],
    }


def _attach_yoy(evidence: dict, current_value, yoy_value, completeness: dict):
    if completeness["yoy_empty"] or yoy_value is None:
        return
    evidence["yoy_change_pct"] = safe_pct_change(current_value, yoy_value)
    evidence["yoy_partial"] = completeness["yoy_partial"]


def find_high_atc_low_checkout(db_conn, period_ending, period_weeks: int) -> list[dict]:
    """Rank products with high add-to-cart rate but low checkout conversion."""
    result = get_product_period(db_conn, period_ending, period_weeks)
    current = result["current"] or {}
    prior = result["prior"] or {}
    yoy = result["yoy"] or {}
    completeness = result["completeness"]

    min_atc_volume = MIN_ATC_VOLUME * period_weeks

    candidates = []
    for item_id, current_row in current.items():
        if not current_row.get("is_visible"):
            continue
        atc_rate = current_row.get("atc_rate")
        checkout_rate = current_row.get("checkout_rate")
        if atc_rate is None or checkout_rate is None:
            continue
        if atc_rate < HIGH_ATC_MIN or atc_rate > 1.0:
            continue
        if checkout_rate > LOW_CHECKOUT_MAX:
            continue
        if (current_row.get("add_to_carts") or 0) < min_atc_volume:
            continue
        candidates.append((item_id, current_row))

    candidates.sort(key=lambda entry: entry[1].get("add_to_carts", 0), reverse=True)

    findings = []
    for item_id, current_row in candidates[:6]:
        prior_row = prior.get(item_id)
        yoy_row = yoy.get(item_id)
        atc_rate = current_row.get("atc_rate")
        checkout_rate = current_row.get("checkout_rate")

        evidence = {
            **_base_evidence(completeness, period_weeks),
            "item_id": item_id,
            "item_name": current_row.get("item_name"),
            "brand_name": current_row.get("brand_name"),
            "price": _as_float(current_row.get("price")),
            "page_url": current_row.get("page_url"),
            "views": current_row.get("views"),
            "add_to_carts": current_row.get("add_to_carts"),
            "checkouts": current_row.get("checkouts"),
            "purchases": current_row.get("purchases"),
            "revenue": _as_float(current_row.get("revenue")) or 0.0,
            "atc_rate": _as_float(atc_rate),
            "checkout_rate": _as_float(checkout_rate),
            "cart_abandonment_rate": _as_float(current_row.get("cart_abandonment_rate")),
            "prior_atc_rate": min(1.0, _as_float(prior_row.get("atc_rate"))) if prior_row and prior_row.get("atc_rate") is not None else None,
            "prior_checkout_rate": min(1.0, _as_float(prior_row.get("checkout_rate"))) if prior_row and prior_row.get("checkout_rate") is not None else None,
            "checkout_rate_change_pct": (
                safe_pct_change(checkout_rate, prior_row.get("checkout_rate"))
                if prior_row else None
            ),
            "atc_rate_change_pct": (
                safe_pct_change(atc_rate, prior_row.get("atc_rate"))
                if prior_row else None
            ),
            "threshold_atc_min": HIGH_ATC_MIN,
            "threshold_checkout_max": LOW_CHECKOUT_MAX,
            "comparison_basis": _comparison_label(period_weeks),
        }
        if yoy_row:
            _attach_yoy(evidence, checkout_rate, yoy_row.get("checkout_rate"), completeness)

        findings.append({
            "week_ending": period_ending.isoformat(),
            "period_weeks": period_weeks,
            "module": FINDING_MODULE,
            "finding_type": "opportunity",
            "severity": "high",
            "title": (
                f"{current_row.get('item_name')} - {float(atc_rate):.0%} ATC rate but only "
                f"{float(checkout_rate):.0%} reach checkout"
            ),
            "evidence": evidence,
            "likely_cause": (
                "Shipping cost or price friction at checkout. Compare final "
                "checkout total to product page price."
            ),
            "suggested_action": (
                f"Review shipping cost display and final checkout price for {current_row.get('item_name')}. "
                "Check competitor pricing."
            ),
            "urgency": "this_week",
        })

    return findings


def find_high_traffic_low_atc(db_conn, period_ending, period_weeks: int) -> list[dict]:
    """Rank high-traffic products with poor add-to-cart rate."""
    result = get_product_period(db_conn, period_ending, period_weeks)
    current = result["current"] or {}
    prior = result["prior"] or {}
    yoy = result["yoy"] or {}
    completeness = result["completeness"]

    high_traffic_min = HIGH_TRAFFIC_MIN * period_weeks

    candidates = []
    for item_id, current_row in current.items():
        if not current_row.get("is_visible"):
            continue
        atc_rate = current_row.get("atc_rate")
        if atc_rate is None:
            continue
        if (current_row.get("views") or 0) < high_traffic_min:
            continue
        if atc_rate > LOW_ATC_MAX:
            continue
        candidates.append((item_id, current_row))

    candidates.sort(key=lambda entry: entry[1].get("views", 0), reverse=True)

    findings = []
    for item_id, current_row in candidates[:4]:
        prior_row = prior.get(item_id)
        yoy_row = yoy.get(item_id)
        atc_rate = current_row.get("atc_rate")

        evidence = {
            **_base_evidence(completeness, period_weeks),
            "item_id": item_id,
            "item_name": current_row.get("item_name"),
            "brand_name": current_row.get("brand_name"),
            "price": _as_float(current_row.get("price")),
            "page_url": current_row.get("page_url"),
            "views": current_row.get("views"),
            "add_to_carts": current_row.get("add_to_carts"),
            "atc_rate": _as_float(atc_rate),
            "checkout_rate": _as_float(current_row.get("checkout_rate")),
            "threshold_views_min": high_traffic_min,
            "threshold_atc_max": LOW_ATC_MAX,
            "prior_views": prior_row.get("views") if prior_row else None,
            "prior_atc_rate": min(1.0, _as_float(prior_row.get("atc_rate"))) if prior_row and prior_row.get("atc_rate") is not None else None,
            "views_change_pct": (
                safe_pct_change(current_row.get("views"), prior_row.get("views"))
                if prior_row else None
            ),
            "atc_rate_change_pct": (
                safe_pct_change(atc_rate, prior_row.get("atc_rate"))
                if prior_row else None
            ),
            "comparison_basis": _comparison_label(period_weeks),
        }
        if yoy_row:
            _attach_yoy(evidence, atc_rate, yoy_row.get("atc_rate"), completeness)

        findings.append({
            "week_ending": period_ending.isoformat(),
            "period_weeks": period_weeks,
            "module": FINDING_MODULE,
            "finding_type": "opportunity",
            "severity": "medium",
            "title": (
                f"{current_row.get('item_name')} - {current_row.get('views')} views but only "
                f"{float(atc_rate):.1%} add-to-cart rate"
            ),
            "evidence": evidence,
            "likely_cause": (
                "Wrong audience reaching page, poor product listing content, or pricing not competitive."
            ),
            "suggested_action": (
                f"Review listing content, images, and pricing for {current_row.get('item_name')}. "
                "Check if traffic source matches buyer intent."
            ),
            "urgency": "monitor",
        })

    return findings


def find_yoy_traffic_declines(db_conn, period_ending, period_weeks: int) -> list[dict]:
    """Flag high-traffic products whose demand is materially below the same period last year."""
    result = get_product_period(db_conn, period_ending, period_weeks)
    current = result["current"] or {}
    yoy = result["yoy"] or {}
    completeness = result["completeness"]

    if completeness["yoy_empty"]:
        return []

    high_traffic_min = HIGH_TRAFFIC_MIN * period_weeks

    findings = []
    for item_id, current_row in current.items():
        if not current_row.get("is_visible"):
            continue
        current_views = current_row.get("views") or 0
        yoy_row = yoy.get(item_id)
        yoy_views = yoy_row.get("views") if yoy_row else None
        if current_views < high_traffic_min or yoy_views is None or yoy_views <= current_views * 2:
            continue

        evidence = {
            **_base_evidence(completeness, period_weeks),
            "item_id": item_id,
            "item_name": current_row.get("item_name"),
            "brand_name": current_row.get("brand_name"),
            "views": current_views,
            "yoy_views": yoy_views,
            "yoy_change_pct": safe_pct_change(current_views, yoy_views),
            "yoy_partial": completeness["yoy_partial"],
            "comparison_basis": "yoy_decline",
        }

        findings.append({
            "week_ending": period_ending.isoformat(),
            "period_weeks": period_weeks,
            "module": FINDING_MODULE,
            "finding_type": "alert",
            "severity": "medium",
            "title": (
                f"{current_row.get('item_name')} - traffic down sharply vs same period last year "
                f"({yoy_views} -> {current_views} views)"
            ),
            "evidence": evidence,
            "likely_cause": (
                "Search demand or ranking may have softened materially relative to last year."
            ),
            "suggested_action": (
                f"Check whether {current_row.get('item_name')} lost search visibility, pricing competitiveness, "
                "or seasonal demand."
            ),
            "urgency": "monitor",
        })

    findings.sort(key=lambda finding: finding["evidence"].get("views", 0), reverse=True)
    return findings[:3]


def _order_items_covers_period(db_conn, required_start: date) -> bool:
    """
    Return True only if raw_bc_order_items has data for orders placed on or before
    required_start — meaning the backfill has fully covered at least through the
    prior comparison window.

    Prevents mid-backfill reports from presenting undercounted order stats as final.
    Sentinels (bc_product_id=0) are excluded so an all-sentinel backfill doesn't
    falsely report coverage.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM raw_bc_orders o
                JOIN raw_bc_order_items oi ON o.bc_order_id = oi.bc_order_id
                WHERE o.date_created::date <= %s
                  AND oi.bc_product_id != 0
            )
            """,
            (required_start,),
        )
        return bool(cur.fetchone()[0])


def _get_brand_bc_order_stats(db_conn, period_start: date, period_end: date) -> dict:
    """
    Query raw_bc_orders + raw_bc_order_items for all brands in a date window.

    Returns a dict keyed by bc_brand_id with order count, unique customers,
    and revenue from BC actuals. Excludes Incomplete (abandoned carts),
    Cancelled, and Refunded orders.

    bc_product_id = 0 means a custom line item not in the catalog — these are
    excluded from brand attribution since we can't assign them to a brand.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                p.bc_brand_id,
                COUNT(DISTINCT o.bc_order_id)                                         AS order_count,
                COUNT(DISTINCT CASE WHEN o.customer_id != 0 THEN o.customer_id END)  AS unique_customers,
                COALESCE(SUM(oi.base_total), 0)                                       AS bc_revenue
            FROM raw_bc_orders o
            JOIN raw_bc_order_items oi ON o.bc_order_id = oi.bc_order_id
            JOIN raw_bc_products p     ON oi.bc_product_id = p.bc_product_id
            WHERE o.date_created::date BETWEEN %s AND %s
              AND o.status NOT IN ('Incomplete', 'Cancelled', 'Refunded')
              AND oi.bc_product_id != 0
            GROUP BY p.bc_brand_id
            """,
            (period_start, period_end),
        )
        rows = cur.fetchall()

    return {
        row[0]: {
            "order_count": row[1],
            "unique_customers": row[2],
            "bc_revenue": float(row[3]) if row[3] is not None else 0.0,
        }
        for row in rows
    }


def find_brand_anomalies(db_conn, period_ending, period_weeks: int) -> list[dict]:
    """
    Flag brands with significant decline in ATC rate or revenue.
    Uses thresholds from ref_anomaly_thresholds for metrics_brand_weekly.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT metric_name, threshold_pct, min_sample_size
            FROM ref_anomaly_thresholds
            WHERE table_source = 'metrics_brand_weekly'
              AND metric_name IN ('blended_atc_rate', 'revenue')
            """
        )
        thresholds = {row[0]: (_as_float(row[1]), row[2]) for row in cur.fetchall()}

    atc_threshold, atc_min_sample = thresholds.get("blended_atc_rate", (15.0, 50))
    revenue_threshold, revenue_min_sample = thresholds.get("revenue", (25.0, 50))
    min_sample_size = min(atc_min_sample, revenue_min_sample) * period_weeks

    result = get_brand_period(db_conn, period_ending, period_weeks)
    current = result["current"] or {}
    prior = result["prior"] or {}
    yoy = result["yoy"] or {}
    completeness = result["completeness"]

    # Pull BC order stats for current and prior windows so the briefer can
    # distinguish a whale order (1 customer, 1 order) from broad demand change.
    w = completeness["window"]
    try:
        current_order_stats = _get_brand_bc_order_stats(
            db_conn,
            date.fromisoformat(w["current_start"]),
            date.fromisoformat(w["current_end"]),
        )
        prior_order_stats = _get_brand_bc_order_stats(
            db_conn,
            date.fromisoformat(w["prior_start"]),
            date.fromisoformat(w["prior_end"]),
        )
        # Only trust order stats if the backfill has reached as far back as the
        # prior window start. A partial backfill would undercount order context
        # and cause the briefer to misclassify broad demand declines as timing artifacts.
        order_data_available = _order_items_covers_period(
            db_conn, date.fromisoformat(w["prior_start"])
        )
        if not order_data_available:
            print("  [merchandising] BC order items backfill incomplete — order context suppressed")
    except Exception as exc:
        print(f"  [merchandising] BC order stats unavailable: {exc}")
        current_order_stats = {}
        prior_order_stats = {}
        order_data_available = False

    findings = []
    saw_prior_data = False

    for brand_id, current_row in current.items():
        if (current_row.get("total_views") or 0) < min_sample_size:
            continue

        prior_row = prior.get(brand_id)
        yoy_row = yoy.get(brand_id)

        revenue_change = (
            safe_pct_change(current_row.get("total_revenue"), prior_row.get("total_revenue"))
            if prior_row else None
        )
        atc_change = (
            safe_pct_change(current_row.get("blended_atc_rate"), prior_row.get("blended_atc_rate"))
            if prior_row else None
        )

        if revenue_change is not None or atc_change is not None:
            saw_prior_data = True

        revenue_triggered = revenue_change is not None and abs(revenue_change) > revenue_threshold
        atc_triggered = atc_change is not None and abs(atc_change) > atc_threshold
        if not revenue_triggered and not atc_triggered:
            continue

        primary_metric = "revenue" if revenue_triggered else "blended_atc_rate"
        primary_change = revenue_change if revenue_triggered else atc_change
        yoy_value = None
        current_value = None
        if primary_metric == "revenue":
            current_value = current_row.get("total_revenue")
            yoy_value = yoy_row.get("total_revenue") if yoy_row else None
            primary_label = "revenue"
        else:
            current_value = current_row.get("blended_atc_rate")
            yoy_value = yoy_row.get("blended_atc_rate") if yoy_row else None
            primary_label = "blended ATC rate"

        if primary_change > 0:
            finding_type = "positive"
            severity = "low"
            urgency = "backlog"
        elif revenue_triggered and revenue_change < -25:
            finding_type = "alert"
            severity = "high"
            urgency = "this_week"
        else:
            finding_type = "alert"
            severity = "medium"
            urgency = "monitor"

        bc_stats = current_order_stats.get(brand_id, {})
        prior_bc_stats = prior_order_stats.get(brand_id, {})

        evidence = {
            **_base_evidence(completeness, period_weeks),
            "bc_brand_id": brand_id,
            "brand_name": current_row.get("brand_name"),
            "active_product_count": current_row.get("active_product_count"),
            "total_views": current_row.get("total_views"),
            "total_add_to_carts": current_row.get("total_add_to_carts"),
            "total_purchases": current_row.get("total_purchases"),
            "total_revenue": _as_float(current_row.get("total_revenue")),
            "blended_atc_rate": _as_float(current_row.get("blended_atc_rate")),
            "blended_purchase_rate": _as_float(current_row.get("blended_purchase_rate")),
            "prior_total_views": prior_row.get("total_views") if prior_row else None,
            "prior_total_revenue": _as_float(prior_row.get("total_revenue")) if prior_row else None,
            "prior_blended_atc_rate": _as_float(prior_row.get("blended_atc_rate")) if prior_row else None,
            "revenue_change_pct": revenue_change,
            "atc_rate_change_pct": atc_change,
            "threshold_revenue_pct": float(revenue_threshold),
            "threshold_atc_pct": float(atc_threshold),
            "comparison_basis": _comparison_label(period_weeks),
            # BC order concentration — lets the briefer distinguish a whale order
            # from broad demand change. Populated after order items backfill runs.
            "order_data_available": order_data_available,
            "bc_order_count": bc_stats.get("order_count"),
            "bc_unique_customers": bc_stats.get("unique_customers"),
            "bc_revenue": bc_stats.get("bc_revenue"),
            "prior_bc_order_count": prior_bc_stats.get("order_count"),
            "prior_bc_unique_customers": prior_bc_stats.get("unique_customers"),
            "prior_bc_revenue": prior_bc_stats.get("bc_revenue"),
        }
        _attach_yoy(evidence, current_value, yoy_value, completeness)

        findings.append({
            "week_ending": period_ending.isoformat(),
            "period_weeks": period_weeks,
            "module": FINDING_MODULE,
            "finding_type": finding_type,
            "severity": severity,
            "title": (
                f"{current_row.get('brand_name')} - {primary_label} "
                f"{'dropped' if primary_change < 0 else 'grew'} {abs(primary_change):.0f}% "
                f"{_comparison_label(period_weeks)} across {current_row.get('active_product_count')} products"
            ),
            "evidence": evidence,
            "likely_cause": (
                "Pricing change, vendor data update, or shift in demand for this brand's products."
            ),
            "suggested_action": (
                f"Review top products under {current_row.get('brand_name')} for pricing or listing changes. "
                "Check if vendor data was updated."
            ),
            "urgency": urgency,
        })

    if not saw_prior_data:
        print("  [merchandising] No prior period data - skipping brand anomalies")

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    findings.sort(
        key=lambda f: (
            severity_rank.get(f["severity"], 3),
            -abs(f["evidence"].get("revenue_change_pct") or f["evidence"].get("atc_rate_change_pct") or 0),
        )
    )
    return findings[:6]


def run(db_conn, week_ending: str, period_weeks: int = 4) -> int:
    """Run all merchandising analysis and write findings. Returns finding count."""
    period_ending = datetime.strptime(week_ending, "%Y-%m-%d").date()

    findings = []
    findings.extend(find_high_atc_low_checkout(db_conn, period_ending, period_weeks) or [])
    findings.extend(find_high_traffic_low_atc(db_conn, period_ending, period_weeks) or [])
    findings.extend(find_yoy_traffic_declines(db_conn, period_ending, period_weeks) or [])
    findings.extend(find_brand_anomalies(db_conn, period_ending, period_weeks) or [])

    if len(findings) > 15:
        severity_order = {"high": 0, "medium": 1, "low": 2}

        def _impact(evidence: dict) -> int:
            return (
                evidence.get("add_to_carts")
                or evidence.get("views")
                or evidence.get("total_add_to_carts")
                or evidence.get("total_views")
                or 0
            )

        findings.sort(
            key=lambda finding: (
                severity_order.get(finding["severity"], 3),
                -_impact(finding["evidence"]),
            )
        )
        findings = findings[:15]

    count = _write_findings(db_conn, findings, FINDING_MODULE, week_ending, period_weeks=period_weeks)
    print(f"  [merchandising] {count} findings written for {week_ending} ({period_weeks}-week window)")
    return count
