"""
BigCommerce ingestion — pulls products, brands, categories, and orders.

Auth: API key (X-Auth-Token header). Credentials from .env.
Tables written: raw_bc_products, raw_bc_brands, raw_bc_categories,
                raw_bc_orders, raw_bc_order_items, ref_product_ga4_map

Run schedule: weekly (Monday morning), catalog snapshotted fresh each run.
Orders pulled for the configured lookback period (default 7 days).

v3 API used for catalog (products, brands, categories).
v2 API used for orders (v3 orders endpoint is not available on all plans).
"""

import os
import requests
import psycopg2.extras
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from dotenv import load_dotenv

load_dotenv()

BC_STORE_HASH = os.getenv("BC_STORE_HASH")
BC_ACCESS_TOKEN = os.getenv("BC_ACCESS_TOKEN")
BASE_URL = f"https://api.bigcommerce.com/stores/{BC_STORE_HASH}/v3"
BASE_URL_V2 = f"https://api.bigcommerce.com/stores/{BC_STORE_HASH}/v2"
HEADERS = {
    "X-Auth-Token": BC_ACCESS_TOKEN,
    "Content-Type": "application/json",
    "Accept": "application/json",
}


# ─────────────────────────────────────────────────────────────────────────────
# Pagination helpers
# ─────────────────────────────────────────────────────────────────────────────

def fetch_all_pages(endpoint: str, params: dict = None) -> list:
    """
    Paginate through a BC v3 endpoint and return all records.

    v3 response shape: { "data": [...], "meta": { "pagination": { "total_pages": N } } }
    """
    params = {**(params or {}), "limit": 250}
    all_items = []
    page = 1
    while True:
        params["page"] = page
        resp = requests.get(f"{BASE_URL}{endpoint}", headers=HEADERS, params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        items = body.get("data", [])
        all_items.extend(items)
        total_pages = body.get("meta", {}).get("pagination", {}).get("total_pages", 1)
        if page >= total_pages or not items:
            break
        page += 1
    return all_items


def fetch_all_pages_v2(endpoint: str, params: dict = None) -> list:
    """
    Paginate through a BC v2 endpoint and return all records.

    v2 response shape: plain JSON array. 204 = no more data.
    """
    params = {**(params or {}), "limit": 250}
    all_items = []
    page = 1
    while True:
        params["page"] = page
        resp = requests.get(f"{BASE_URL_V2}{endpoint}", headers=HEADERS, params=params, timeout=30)
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


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion functions
# ─────────────────────────────────────────────────────────────────────────────

def ingest_brands(db_conn) -> int:
    """
    Fetch all brands from BigCommerce and upsert into raw_bc_brands.

    Returns: number of rows written
    """
    brands = fetch_all_pages("/catalog/brands")
    if not brands:
        return 0

    rows = []
    for b in brands:
        # BC returns meta_keywords as a list; flatten to comma-separated string
        meta_keywords = b.get("meta_keywords")
        if isinstance(meta_keywords, list):
            meta_keywords = ",".join(meta_keywords)
        rows.append((
            b["id"],
            b.get("name"),
            b.get("page_title"),
            meta_keywords,
            b.get("image_url"),
        ))

    with db_conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO raw_bc_brands (bc_brand_id, name, page_title, meta_keywords, image_url, snapshotted_at)
            VALUES %s
            ON CONFLICT (bc_brand_id) DO UPDATE SET
                name          = EXCLUDED.name,
                page_title    = EXCLUDED.page_title,
                meta_keywords = EXCLUDED.meta_keywords,
                image_url     = EXCLUDED.image_url,
                snapshotted_at = NOW()
        """, rows, template="(%s, %s, %s, %s, %s, NOW())")
    db_conn.commit()
    return len(rows)


def ingest_categories(db_conn) -> int:
    """
    Fetch category tree from BigCommerce and upsert into raw_bc_categories.

    Returns: number of rows written
    """
    categories = fetch_all_pages("/catalog/categories")
    if not categories:
        return 0

    rows = []
    for c in categories:
        rows.append((
            c["id"],
            c.get("parent_id"),
            c.get("name"),
            c.get("url"),
            c.get("is_visible", True),
        ))

    with db_conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO raw_bc_categories (bc_category_id, parent_id, name, url, is_visible, snapshotted_at)
            VALUES %s
            ON CONFLICT (bc_category_id) DO UPDATE SET
                parent_id   = EXCLUDED.parent_id,
                name        = EXCLUDED.name,
                url         = EXCLUDED.url,
                is_visible  = EXCLUDED.is_visible,
                snapshotted_at = NOW()
        """, rows, template="(%s, %s, %s, %s, %s, NOW())")
    db_conn.commit()
    return len(rows)


def ingest_products(db_conn) -> int:
    """
    Fetch all products from BigCommerce and upsert into raw_bc_products.
    Also rebuilds ref_product_ga4_map (SKU → bc_product_id mapping).

    Products with no SKU are skipped — they can't be joined to GA4 data.
    Brand names are resolved from raw_bc_brands (must run ingest_brands first).

    Returns: number of product rows written
    """
    # Load brand_id → name lookup from DB (populated by ingest_brands above)
    with db_conn.cursor() as cur:
        cur.execute("SELECT bc_brand_id, name FROM raw_bc_brands")
        brand_lookup = {row[0]: row[1] for row in cur.fetchall()}

    # Restrict to only the fields we store — at 30k SKUs the default payload is
    # huge (modifiers, variants, custom_fields, etc.) and will exhaust rate limits.
    products = fetch_all_pages("/catalog/products", params={
        "include_fields": (
            "sku,mpn,name,brand_id,price,cost_price,inventory_level,"
            "inventory_tracking,is_visible,custom_url,date_modified"
        ),
    })
    if not products:
        return 0

    product_rows = []
    map_rows = []

    for p in products:
        sku = (p.get("sku") or "").strip()
        if not sku:
            continue  # can't join to GA4 without a SKU

        brand_id = p.get("brand_id")
        brand_name = brand_lookup.get(brand_id)

        # custom_url is a nested object: { "url": "/path/", "is_customized": bool }
        custom_url = None
        if isinstance(p.get("custom_url"), dict):
            custom_url = p["custom_url"].get("url")

        product_rows.append((
            p["id"],            # bc_product_id
            sku,
            p.get("mpn"),
            p.get("name"),
            brand_id,
            brand_name,
            p.get("price"),
            p.get("cost_price"),
            p.get("inventory_level"),
            p.get("inventory_tracking", "none"),
            p.get("is_visible", True),
            custom_url,
            p.get("date_modified"),
        ))

        map_rows.append((
            sku,                # ga4_item_id = BC SKU
            p["id"],            # bc_product_id
            p.get("mpn"),
            brand_name,
        ))

    with db_conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO raw_bc_products (
                bc_product_id, sku, mpn, name, bc_brand_id, brand_name,
                price, cost_price, inventory_level, inventory_tracking,
                is_visible, custom_url, date_modified, snapshotted_at
            ) VALUES %s
            ON CONFLICT (bc_product_id) DO UPDATE SET
                sku                = EXCLUDED.sku,
                mpn                = EXCLUDED.mpn,
                name               = EXCLUDED.name,
                bc_brand_id        = EXCLUDED.bc_brand_id,
                brand_name         = EXCLUDED.brand_name,
                price              = EXCLUDED.price,
                cost_price         = EXCLUDED.cost_price,
                inventory_level    = EXCLUDED.inventory_level,
                inventory_tracking = EXCLUDED.inventory_tracking,
                is_visible         = EXCLUDED.is_visible,
                custom_url         = EXCLUDED.custom_url,
                date_modified      = EXCLUDED.date_modified,
                snapshotted_at     = NOW()
        """, product_rows,
            template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())")

        # Rebuild SKU → bc_product_id mapping for GA4 joins
        psycopg2.extras.execute_values(cur, """
            INSERT INTO ref_product_ga4_map (ga4_item_id, bc_product_id, mpn, brand_name, is_active, verified_at)
            VALUES %s
            ON CONFLICT (ga4_item_id) DO UPDATE SET
                bc_product_id = EXCLUDED.bc_product_id,
                mpn           = EXCLUDED.mpn,
                brand_name    = EXCLUDED.brand_name,
                is_active     = TRUE,
                verified_at   = CURRENT_DATE
        """, map_rows, template="(%s,%s,%s,%s,TRUE,CURRENT_DATE)")

    db_conn.commit()
    return len(product_rows)


def ingest_orders(db_conn, lookback_days: int = 7) -> int:
    """
    Fetch orders created in the past `lookback_days` and insert into
    raw_bc_orders and raw_bc_order_items.

    Skips order rows that already exist (ON CONFLICT DO NOTHING).
    Fetches line items only for newly inserted orders to avoid duplicates.

    BC v2 returns dates in RFC 2822 format — parsed to Python datetime before insert.

    Returns: number of new order rows written
    """
    # BC v2 `min_date_created` expects RFC 2822 — ISO 8601 with a colon in the
    # offset (+00:00) has been observed to silently return 0 results.
    date_min = (datetime.utcnow() - timedelta(days=lookback_days)).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    orders = fetch_all_pages_v2("/orders", params={"min_date_created": date_min})
    if not orders:
        return 0

    new_order_ids = []

    with db_conn.cursor() as cur:
        for o in orders:
            date_created = _parse_bc_date(o.get("date_created"))
            cur.execute("""
                INSERT INTO raw_bc_orders
                    (bc_order_id, date_created, status, subtotal, total_inc_tax, customer_id, is_deleted)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (bc_order_id) DO NOTHING
            """, (
                o["id"],
                date_created,
                o.get("status"),
                # subtotal = pre-tax merchandise total (Option A). Paired with
                # total_inc_tax so downstream can derive tax + shipping cleanly.
                o.get("subtotal_ex_tax"),
                o.get("total_inc_tax"),
                o.get("customer_id"),
                o.get("is_deleted", False),
            ))
            if cur.rowcount > 0:
                new_order_ids.append(o["id"])

    db_conn.commit()

    # Fetch line items only for newly inserted orders
    if not new_order_ids:
        return 0

    item_rows = []
    for order_id in new_order_ids:
        try:
            items = fetch_all_pages_v2(f"/orders/{order_id}/products")
            for item in items:
                item_rows.append((
                    order_id,
                    item.get("product_id"),
                    item.get("sku"),
                    item.get("name"),
                    item.get("quantity"),
                    item.get("price_inc_tax"),
                    # BC v2 order_products exposes `total_inc_tax` (line total).
                    # There is no `base_total_inc_tax` field — previous version
                    # wrote NULLs here.
                    item.get("total_inc_tax"),
                ))
        except requests.HTTPError:
            # Order products endpoint can 404 for deleted/inaccessible orders
            continue

    if item_rows:
        with db_conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO raw_bc_order_items
                    (bc_order_id, bc_product_id, sku, name, quantity, price_inc_tax, base_total)
                VALUES %s
            """, item_rows)
        db_conn.commit()

    return len(new_order_ids)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_bc_date(date_str: str):
    """
    Parse a BC v2 RFC 2822 date string (e.g. 'Mon, 14 Mar 2024 01:00:00 +0000')
    into a Python datetime. Returns None if parsing fails.
    """
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str)
    except Exception:
        return None


def _log_ingestion(db_conn, status: str, rows_written: int,
                   start: datetime, lookback_days: int, error_message: str = None):
    """Write a row to ingestion_log."""
    duration = (datetime.utcnow() - start).total_seconds()
    date_start = (datetime.utcnow() - timedelta(days=lookback_days)).date()
    date_end = datetime.utcnow().date()
    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ingestion_log
                (source, date_range_start, date_range_end, rows_written, status, error_message, duration_seconds)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, ("bigcommerce", date_start, date_end, rows_written, status, error_message, duration))
    db_conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(db_conn, lookback_days: int = 7):
    """Run all BigCommerce ingestion. Called by scheduler/weekly_job.py."""
    if not BC_STORE_HASH or not BC_ACCESS_TOKEN:
        raise RuntimeError(
            "BC_STORE_HASH and BC_ACCESS_TOKEN must be set in .env before running BigCommerce ingestion"
        )

    start = datetime.utcnow()
    rows = 0
    try:
        rows += ingest_brands(db_conn)
        rows += ingest_categories(db_conn)
        rows += ingest_products(db_conn)
        rows += ingest_orders(db_conn, lookback_days)
        _log_ingestion(db_conn, "success", rows, start, lookback_days)
        print(f"  [bigcommerce] {rows} rows written in {(datetime.utcnow() - start).total_seconds():.1f}s")
    except Exception as e:
        # If any step raised mid-transaction, the connection is in an aborted
        # state — must rollback before _log_ingestion can write the failure row.
        db_conn.rollback()
        _log_ingestion(db_conn, "failed", rows, start, lookback_days, str(e))
        raise
    return rows
