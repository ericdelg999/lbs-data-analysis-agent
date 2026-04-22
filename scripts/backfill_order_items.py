"""
One-time backfill of raw_bc_order_items for all historical orders.

raw_bc_orders has ~18,600 rows but raw_bc_order_items only has ~450 because
ingest_orders() fetches line items only for newly-inserted orders. This script
fetches order products for all orders that are missing items.

Runtime: ~30 min for ~18k orders at the default 0.1s sleep between API calls.
Progress is printed every 500 orders. Safe to interrupt and re-run — it skips
any order that already has rows in raw_bc_order_items.

Usage:
    python scripts/backfill_order_items.py
    python scripts/backfill_order_items.py --sleep 0.05
    python scripts/backfill_order_items.py --dry-run
"""

import argparse
import os
import sys
import time
from datetime import datetime
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

BC_STORE_HASH = os.getenv("BC_STORE_HASH")
BC_ACCESS_TOKEN = os.getenv("BC_ACCESS_TOKEN")
BASE_URL_V2 = f"https://api.bigcommerce.com/stores/{BC_STORE_HASH}/v2"
HEADERS = {
    "X-Auth-Token": BC_ACCESS_TOKEN,
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def get_db_connection():
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


def fetch_order_products(order_id: int) -> list:
    """Fetch all product line items for a single order via BC v2 API."""
    params = {"limit": 250}
    all_items = []
    page = 1
    while True:
        params["page"] = page
        resp = requests.get(
            f"{BASE_URL_V2}/orders/{order_id}/products",
            headers=HEADERS,
            params=params,
            timeout=30,
        )
        if resp.status_code == 204:
            break
        resp.raise_for_status()
        items = resp.json()
        if not items:
            break
        all_items.extend(items)
        if len(items) < 250:
            break
        page += 1
    return all_items


def get_missing_order_ids(db_conn) -> list[int]:
    """
    Return all order IDs in raw_bc_orders that have no rows in raw_bc_order_items.
    Sorted ascending so restarts pick up where they left off.
    """
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT o.bc_order_id
            FROM raw_bc_orders o
            WHERE NOT EXISTS (
                SELECT 1 FROM raw_bc_order_items oi
                WHERE oi.bc_order_id = o.bc_order_id
            )
            ORDER BY o.bc_order_id
        """)
        return [row[0] for row in cur.fetchall()]


def _write_sentinel(db_conn, order_id: int):
    """
    Write a bc_product_id=0 sentinel row so this order won't appear as "missing"
    on the next run. Sentinel is invisible to brand analytics (filtered by
    _get_brand_bc_order_stats's `AND oi.bc_product_id != 0` clause).

    Used for orders that return 404 (deleted/inaccessible) or have zero catalog
    line items (digital-only, gift cards, etc.).
    """
    with db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_bc_order_items
                (bc_order_id, bc_product_id, sku, name, quantity, price_inc_tax, base_total)
            VALUES (%s, 0, NULL, NULL, NULL, NULL, NULL)
            """,
            (order_id,),
        )
    db_conn.commit()


def insert_order_items(db_conn, order_id: int, items: list) -> int:
    """
    Insert line items for a single order. Commits immediately so restarts
    are safe — any order with committed items won't be re-queued.
    Returns number of catalog item rows inserted (sentinels not counted).
    """
    rows = []
    for item in items:
        rows.append((
            order_id,
            item.get("product_id"),
            item.get("sku"),
            item.get("name"),
            item.get("quantity"),
            item.get("price_inc_tax"),
            # BC v2 order_products field is total_inc_tax (line total), stored as base_total
            item.get("total_inc_tax"),
        ))

    if rows:
        with db_conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO raw_bc_order_items
                    (bc_order_id, bc_product_id, sku, name, quantity, price_inc_tax, base_total)
                VALUES %s
            """, rows)
        db_conn.commit()
        return len(rows)
    else:
        # Order has no catalog line items — write sentinel so it won't re-queue
        _write_sentinel(db_conn, order_id)
        return 0


def run_backfill(dry_run: bool = False, sleep_secs: float = 0.1):
    db_conn = get_db_connection()

    print(f"[{datetime.now():%H:%M:%S}] Querying for orders missing item records...")
    order_ids = get_missing_order_ids(db_conn)
    total = len(order_ids)
    print(f"[{datetime.now():%H:%M:%S}] {total:,} orders need item backfill")

    if dry_run:
        print("[dry-run] Exiting without fetching or inserting.")
        db_conn.close()
        return

    if total == 0:
        print("Nothing to do — raw_bc_order_items is already complete.")
        db_conn.close()
        return

    eta_min = total * sleep_secs / 60
    print(
        f"[{datetime.now():%H:%M:%S}] Starting backfill. "
        f"Estimated runtime ~{eta_min:.0f} min at {sleep_secs}s/call. "
        f"Progress printed every 500 orders. Safe to Ctrl+C and re-run.\n"
    )

    items_written = 0
    errors = 0
    start_time = datetime.now()

    for i, order_id in enumerate(order_ids, start=1):
        try:
            items = fetch_order_products(order_id)
            written = insert_order_items(db_conn, order_id, items)
            items_written += written
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else "?"
            if status_code == 404:
                # Order is deleted or inaccessible — write sentinel so it won't re-queue
                _write_sentinel(db_conn, order_id)
            else:
                errors += 1
                print(f"  [warn] order {order_id}: HTTP {status_code}")
        except Exception as exc:
            errors += 1
            print(f"  [warn] order {order_id}: {exc}")

        if i % 500 == 0 or i == total:
            elapsed = (datetime.now() - start_time).total_seconds()
            rate = i / elapsed if elapsed > 0 else i
            remaining_secs = (total - i) / rate if rate > 0 else 0
            print(
                f"  [{datetime.now():%H:%M:%S}] {i:,}/{total:,} orders "
                f"({i/total*100:.0f}%) — "
                f"{items_written:,} items written, {errors} errors — "
                f"~{remaining_secs/60:.0f}m remaining"
            )

        time.sleep(sleep_secs)

    db_conn.close()
    elapsed_min = (datetime.now() - start_time).total_seconds() / 60
    print(
        f"\n[{datetime.now():%H:%M:%S}] Backfill complete in {elapsed_min:.1f} min: "
        f"{items_written:,} items across {total:,} orders ({errors} errors)"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Backfill raw_bc_order_items for all historical orders"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count missing orders only, don't fetch or insert",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.1,
        help="Seconds to sleep between BC API calls (default: 0.1)",
    )
    args = parser.parse_args()
    run_backfill(dry_run=args.dry_run, sleep_secs=args.sleep)


if __name__ == "__main__":
    main()
