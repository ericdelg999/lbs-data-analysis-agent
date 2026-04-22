-- ══════════════════════════════════════════════════════════════════════════════
-- LBS Intelligence System — Database Schema
-- Run once against your Supabase project to initialize all tables.
-- ══════════════════════════════════════════════════════════════════════════════


-- ─────────────────────────────────────────────────────────────────────────────
-- LAYER 1: RAW INGESTION TABLES
-- Stores normalized API data at daily grain. Never written to by analyst modules.
-- ─────────────────────────────────────────────────────────────────────────────

-- Site-level GA4 metrics by day
CREATE TABLE IF NOT EXISTS raw_ga4_daily (
    id                      SERIAL PRIMARY KEY,
    date                    DATE NOT NULL UNIQUE,
    sessions                INTEGER,
    engaged_sessions        INTEGER,
    bounce_rate             NUMERIC(5,4),
    avg_session_duration    NUMERIC(8,2),
    new_users               INTEGER,
    total_users             INTEGER,
    screen_page_views       INTEGER,
    ingested_at             TIMESTAMPTZ DEFAULT NOW()
);

-- Per-page GA4 metrics by day (feeds funnel and PDP analysis)
CREATE TABLE IF NOT EXISTS raw_ga4_pages_daily (
    id                      SERIAL PRIMARY KEY,
    date                    DATE NOT NULL,
    page_path               TEXT NOT NULL,
    page_title              TEXT,
    sessions                INTEGER,
    engaged_sessions        INTEGER,
    bounce_rate             NUMERIC(5,4),
    avg_time_on_page        NUMERIC(8,2),
    screen_page_views       INTEGER,
    ingested_at             TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (date, page_path)
);

-- Per-product ecommerce events by day
-- IMPORTANT: item_id = BigCommerce SKU field (verified 2026-04-03)
-- Join to raw_bc_products on: raw_ga4_products_daily.item_id = raw_bc_products.sku
CREATE TABLE IF NOT EXISTS raw_ga4_products_daily (
    id                      SERIAL PRIMARY KEY,
    date                    DATE NOT NULL,
    item_id                 TEXT NOT NULL,       -- = BC SKU (e.g. "GE-1234", "SSL-5678")
    item_name               TEXT,
    item_category           TEXT,
    item_brand              TEXT,
    views                   INTEGER DEFAULT 0,   -- view_item events
    add_to_carts            INTEGER DEFAULT 0,   -- add_to_cart events
    checkouts               INTEGER DEFAULT 0,   -- begin_checkout events containing item
    purchases               INTEGER DEFAULT 0,   -- purchase events
    purchase_revenue        NUMERIC(12,2) DEFAULT 0,
    ingested_at             TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (date, item_id)
);

-- Traffic by channel group by day (organic, paid search, email, direct, AI, etc.)
CREATE TABLE IF NOT EXISTS raw_ga4_traffic_channels_daily (
    id                      SERIAL PRIMARY KEY,
    date                    DATE NOT NULL,
    channel_group           TEXT NOT NULL,       -- GA4 default channel grouping
    sessions                INTEGER DEFAULT 0,
    engaged_sessions        INTEGER DEFAULT 0,
    conversions             INTEGER DEFAULT 0,
    revenue                 NUMERIC(12,2) DEFAULT 0,
    new_users               INTEGER DEFAULT 0,
    ingested_at             TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (date, channel_group)
);

-- Traffic by source + medium by day (used for AI referrer tracking)
CREATE TABLE IF NOT EXISTS raw_ga4_sources_daily (
    id                      SERIAL PRIMARY KEY,
    date                    DATE NOT NULL,
    session_source          TEXT NOT NULL,
    session_medium          TEXT NOT NULL DEFAULT '(none)',
    sessions                INTEGER DEFAULT 0,
    engaged_sessions        INTEGER DEFAULT 0,
    conversions             INTEGER DEFAULT 0,
    revenue                 NUMERIC(12,2) DEFAULT 0,
    new_users               INTEGER DEFAULT 0,
    ingested_at             TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (date, session_source, session_medium)
);

-- Google Search Console: query + page performance by day
CREATE TABLE IF NOT EXISTS raw_gsc_daily (
    id                      SERIAL PRIMARY KEY,
    date                    DATE NOT NULL,
    query                   TEXT NOT NULL,
    page                    TEXT NOT NULL,
    clicks                  INTEGER DEFAULT 0,
    impressions             INTEGER DEFAULT 0,
    ctr                     NUMERIC(6,4),
    avg_position            NUMERIC(6,2),
    ingested_at             TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (date, query, page)
);

-- Google Ads: campaign + ad group performance by day
CREATE TABLE IF NOT EXISTS raw_gads_daily (
    id                          SERIAL PRIMARY KEY,
    date                        DATE NOT NULL,
    campaign_id                 TEXT NOT NULL,
    campaign_name               TEXT,
    ad_group_id                 TEXT,
    ad_group_name               TEXT,
    impressions                 INTEGER DEFAULT 0,
    clicks                      INTEGER DEFAULT 0,
    cost                        NUMERIC(10,2) DEFAULT 0,  -- converted from micros
    conversions                 NUMERIC(8,2) DEFAULT 0,
    conversion_value            NUMERIC(12,2) DEFAULT 0,
    search_impression_share     NUMERIC(6,4),             -- % of eligible impressions received
    search_lost_is_rank         NUMERIC(6,4),             -- % lost to low ad rank (fix: quality/bid)
    search_lost_is_budget       NUMERIC(6,4),             -- % lost to budget (fix: increase budget)
    ingested_at                 TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (date, campaign_id, ad_group_id)
);

-- BigCommerce product catalog snapshot
-- Refreshed weekly. is_visible = TRUE means product is live and shoppable.
-- Inventory not tracked for most SKUs (dropship model — if visible, assume in stock).
CREATE TABLE IF NOT EXISTS raw_bc_products (
    id                      SERIAL PRIMARY KEY,
    bc_product_id           INTEGER NOT NULL UNIQUE,
    sku                     TEXT,                -- matches GA4 item_id
    mpn                     TEXT,                -- manufacturer part number (no brand prefix)
    name                    TEXT NOT NULL,
    bc_brand_id             INTEGER,
    brand_name              TEXT,
    price                   NUMERIC(10,2),
    cost_price              NUMERIC(10,2),
    inventory_level         INTEGER,
    inventory_tracking      TEXT,                -- 'product', 'variant', 'none'
    is_visible              BOOLEAN DEFAULT TRUE,
    custom_url              TEXT,                -- URL slug for linking to PDP
    date_modified           TIMESTAMPTZ,
    snapshotted_at          TIMESTAMPTZ DEFAULT NOW()
);

-- BigCommerce brand catalog
CREATE TABLE IF NOT EXISTS raw_bc_brands (
    id                      SERIAL PRIMARY KEY,
    bc_brand_id             INTEGER NOT NULL UNIQUE,
    name                    TEXT NOT NULL,
    page_title              TEXT,
    meta_keywords           TEXT,
    image_url               TEXT,
    snapshotted_at          TIMESTAMPTZ DEFAULT NOW()
);

-- BigCommerce category tree
CREATE TABLE IF NOT EXISTS raw_bc_categories (
    id                      SERIAL PRIMARY KEY,
    bc_category_id          INTEGER NOT NULL UNIQUE,
    parent_id               INTEGER,
    name                    TEXT NOT NULL,
    url                     TEXT,
    is_visible              BOOLEAN DEFAULT TRUE,
    snapshotted_at          TIMESTAMPTZ DEFAULT NOW()
);

-- BigCommerce orders
CREATE TABLE IF NOT EXISTS raw_bc_orders (
    id                      SERIAL PRIMARY KEY,
    bc_order_id             INTEGER NOT NULL UNIQUE,
    date_created            TIMESTAMPTZ NOT NULL,
    status                  TEXT,
    subtotal                NUMERIC(10,2),
    total_inc_tax           NUMERIC(10,2),
    customer_id             INTEGER,
    is_deleted              BOOLEAN DEFAULT FALSE,
    ingested_at             TIMESTAMPTZ DEFAULT NOW()
);

-- BigCommerce order line items
CREATE TABLE IF NOT EXISTS raw_bc_order_items (
    id                      SERIAL PRIMARY KEY,
    bc_order_id             INTEGER REFERENCES raw_bc_orders(bc_order_id),
    bc_product_id           INTEGER,
    sku                     TEXT,
    name                    TEXT,
    quantity                INTEGER,
    price_inc_tax           NUMERIC(10,2),
    base_total              NUMERIC(10,2),
    ingested_at             TIMESTAMPTZ DEFAULT NOW()
);


-- ─────────────────────────────────────────────────────────────────────────────
-- LAYER 2: COMPUTED METRICS TABLES
-- Populated by transforms/ scripts after ingestion. Analyst modules read these.
-- Never written to directly by ingestion jobs.
-- ─────────────────────────────────────────────────────────────────────────────

-- Per-product weekly metrics — PRIMARY TABLE for Merchandising Analyst
-- Filter to is_visible = TRUE for all queries. Min views threshold applied in transforms.
CREATE TABLE IF NOT EXISTS metrics_product_weekly (
    id                      SERIAL PRIMARY KEY,
    week_ending             DATE NOT NULL,
    item_id                 TEXT NOT NULL,       -- BC SKU
    item_name               TEXT,
    bc_product_id           INTEGER,
    bc_brand_id             INTEGER,
    brand_name              TEXT,
    price                   NUMERIC(10,2),
    is_visible              BOOLEAN,
    page_url                TEXT,                -- full PDP URL for direct linking
    -- Raw counts
    views                   INTEGER DEFAULT 0,
    add_to_carts            INTEGER DEFAULT 0,
    checkouts               INTEGER DEFAULT 0,
    purchases               INTEGER DEFAULT 0,
    revenue                 NUMERIC(12,2) DEFAULT 0,
    pdp_bounce_rate         NUMERIC(5,4),        -- from raw_ga4_pages_daily join
    -- Computed rates (NUMERIC(10,4) to handle GA4 event-scope artifacts where
    -- add_to_carts can exceed views, producing rates > 100; analysts filter to <= 1.0)
    atc_rate                NUMERIC(10,4),       -- add_to_carts / views
    checkout_rate           NUMERIC(10,4),       -- checkouts / add_to_carts
    purchase_rate           NUMERIC(10,4),       -- purchases / views
    cart_abandonment_rate   NUMERIC(10,4),       -- 1 - (checkouts / add_to_carts)
    -- Prior week for WoW comparison
    prev_views              INTEGER,
    prev_atc_rate           NUMERIC(10,4),
    prev_purchase_rate      NUMERIC(10,4),
    views_wow_pct           NUMERIC(12,3),       -- can be very large for historical backfill
    atc_rate_wow_pct        NUMERIC(12,3),
    purchase_rate_wow_pct   NUMERIC(12,3),
    UNIQUE (week_ending, item_id)
);

-- Brand-level weekly rollup — PRIORITY for Merchandising Analyst
-- More actionable than categories for a multi-brand dropship catalog.
CREATE TABLE IF NOT EXISTS metrics_brand_weekly (
    id                          SERIAL PRIMARY KEY,
    week_ending                 DATE NOT NULL,
    bc_brand_id                 INTEGER NOT NULL,
    brand_name                  TEXT NOT NULL,
    active_product_count        INTEGER,         -- products with views > threshold
    total_views                 INTEGER DEFAULT 0,
    total_add_to_carts          INTEGER DEFAULT 0,
    total_purchases             INTEGER DEFAULT 0,
    total_revenue               NUMERIC(12,2) DEFAULT 0,
    blended_atc_rate            NUMERIC(10,4),   -- total ATCs / total views across brand
    blended_purchase_rate       NUMERIC(10,4),
    prev_total_views            INTEGER,
    prev_blended_atc_rate       NUMERIC(10,4),
    prev_total_revenue          NUMERIC(12,2),
    views_wow_pct               NUMERIC(12,3),
    atc_rate_wow_pct            NUMERIC(12,3),
    revenue_wow_pct             NUMERIC(12,3),
    UNIQUE (week_ending, bc_brand_id)
);

-- Category-level weekly rollup (secondary to brand analysis)
CREATE TABLE IF NOT EXISTS metrics_category_weekly (
    id                          SERIAL PRIMARY KEY,
    week_ending                 DATE NOT NULL,
    bc_category_id              INTEGER NOT NULL,
    category_name               TEXT NOT NULL,
    active_product_count        INTEGER,
    total_views                 INTEGER DEFAULT 0,
    total_add_to_carts          INTEGER DEFAULT 0,
    total_purchases             INTEGER DEFAULT 0,
    total_revenue               NUMERIC(12,2) DEFAULT 0,
    blended_atc_rate            NUMERIC(10,4),
    blended_purchase_rate       NUMERIC(10,4),
    revenue_wow_pct             NUMERIC(12,3),
    atc_rate_wow_pct            NUMERIC(12,3),
    UNIQUE (week_ending, bc_category_id)
);

-- Site-wide funnel metrics by week
CREATE TABLE IF NOT EXISTS metrics_funnel_weekly (
    id                          SERIAL PRIMARY KEY,
    week_ending                 DATE NOT NULL UNIQUE,
    sessions                    INTEGER,
    engaged_sessions            INTEGER,
    pdp_views                   INTEGER,
    add_to_carts                INTEGER,
    checkouts                   INTEGER,
    purchases                   INTEGER,
    revenue                     NUMERIC(12,2),
    -- New vs returning split (from GA4)
    new_user_sessions           INTEGER,
    returning_user_sessions     INTEGER,
    new_user_revenue            NUMERIC(12,2),
    returning_user_revenue      NUMERIC(12,2),
    -- Funnel rates
    session_to_pdp_rate         NUMERIC(10,4),
    pdp_to_atc_rate             NUMERIC(10,4),
    atc_to_checkout_rate        NUMERIC(10,4),
    checkout_to_purchase_rate   NUMERIC(10,4),
    overall_conversion_rate     NUMERIC(10,4),
    -- WoW
    revenue_wow_pct             NUMERIC(12,3),
    sessions_wow_pct            NUMERIC(12,3),
    conversion_wow_pct          NUMERIC(12,3)
);

-- Channel-level performance by week (organic, paid, email, direct, AI, etc.)
CREATE TABLE IF NOT EXISTS metrics_channel_weekly (
    id                      SERIAL PRIMARY KEY,
    week_ending             DATE NOT NULL,
    channel_group           TEXT NOT NULL,
    sessions                INTEGER DEFAULT 0,
    engaged_sessions        INTEGER DEFAULT 0,
    conversions             INTEGER DEFAULT 0,
    revenue                 NUMERIC(12,2) DEFAULT 0,
    conversion_rate         NUMERIC(10,4),
    prev_sessions           INTEGER,
    prev_conversion_rate    NUMERIC(10,4),
    sessions_wow_pct        NUMERIC(12,3),
    revenue_wow_pct         NUMERIC(12,3),
    UNIQUE (week_ending, channel_group)
);

-- GSC search metrics by week
CREATE TABLE IF NOT EXISTS metrics_search_weekly (
    id                      SERIAL PRIMARY KEY,
    week_ending             DATE NOT NULL,
    query                   TEXT NOT NULL,
    page                    TEXT,
    clicks                  INTEGER DEFAULT 0,
    impressions             INTEGER DEFAULT 0,
    ctr                     NUMERIC(6,4),
    avg_position            NUMERIC(6,2),
    is_branded              BOOLEAN DEFAULT FALSE,  -- TRUE only for LBS house brands
    prev_clicks             INTEGER,
    prev_impressions        INTEGER,
    clicks_wow_pct          NUMERIC(12,3),
    impressions_wow_pct     NUMERIC(12,3),
    ctr_wow_pct             NUMERIC(12,3),
    UNIQUE (week_ending, query, page)
);

-- Google Ads campaign metrics by week
CREATE TABLE IF NOT EXISTS metrics_paid_weekly (
    id                              SERIAL PRIMARY KEY,
    week_ending                     DATE NOT NULL,
    campaign_id                     TEXT NOT NULL,
    campaign_name                   TEXT,
    spend                           NUMERIC(10,2) DEFAULT 0,
    clicks                          INTEGER DEFAULT 0,
    impressions                     INTEGER DEFAULT 0,
    conversions                     NUMERIC(8,2) DEFAULT 0,
    conversion_value                NUMERIC(12,2) DEFAULT 0,
    cpc                             NUMERIC(8,4),
    roas                            NUMERIC(8,4),           -- conversion_value / spend
    avg_search_impression_share     NUMERIC(6,4),
    avg_search_lost_is_rank         NUMERIC(6,4),           -- lost to quality/bid
    avg_search_lost_is_budget       NUMERIC(6,4),           -- lost to budget
    prev_spend                      NUMERIC(10,2),
    prev_roas                       NUMERIC(8,4),
    spend_wow_pct                   NUMERIC(12,3),
    roas_wow_pct                    NUMERIC(12,3),
    impression_share_wow_pct        NUMERIC(12,3),
    UNIQUE (week_ending, campaign_id)
);

-- AI referral traffic by domain by week
CREATE TABLE IF NOT EXISTS metrics_ai_referral_weekly (
    id                      SERIAL PRIMARY KEY,
    week_ending             DATE NOT NULL,
    referrer_domain         TEXT NOT NULL,       -- e.g. 'chatgpt.com', 'perplexity.ai'
    referrer_label          TEXT,                -- e.g. 'ChatGPT', 'Perplexity'
    sessions                INTEGER DEFAULT 0,
    engaged_sessions        INTEGER DEFAULT 0,
    conversions             INTEGER DEFAULT 0,
    revenue                 NUMERIC(12,2) DEFAULT 0,
    conversion_rate         NUMERIC(10,4),
    top_landing_page        TEXT,
    prev_sessions           INTEGER,
    sessions_wow_pct        NUMERIC(12,3),
    revenue_wow_pct         NUMERIC(12,3),
    UNIQUE (week_ending, referrer_domain)
);


-- ─────────────────────────────────────────────────────────────────────────────
-- LAYER 3: REFERENCE / CONFIG TABLES
-- Configurable data that drives analyst logic. Update without code changes.
-- ─────────────────────────────────────────────────────────────────────────────

-- Maps GA4 item_id (= BC SKU) to BC product details
-- Join key: ga4_item_id = raw_bc_products.sku
-- Populated from BC API. Re-run when catalog changes significantly.
CREATE TABLE IF NOT EXISTS ref_product_ga4_map (
    id              SERIAL PRIMARY KEY,
    ga4_item_id     TEXT NOT NULL UNIQUE,    -- BC SKU (e.g. "GE-1234")
    bc_product_id   INTEGER NOT NULL,
    mpn             TEXT,
    brand_name      TEXT,
    is_active       BOOLEAN DEFAULT TRUE,
    verified_at     DATE
);

-- LBS house brand keyword variants for GSC branded classification
-- "Branded" = queries containing these terms. Does NOT include manufacturer brands.
CREATE TABLE IF NOT EXISTS ref_branded_keywords (
    id          SERIAL PRIMARY KEY,
    keyword     TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO ref_branded_keywords (keyword) VALUES
    ('light bulb surplus'),
    ('lightbulbsurplus'),
    ('lbs lighting'),
    ('lbslighting'),
    ('contractor essentials'),
    ('contractoressentials')
ON CONFLICT DO NOTHING;

-- Known AI referrer domains for session tracking
CREATE TABLE IF NOT EXISTS ref_ai_referrers (
    id          SERIAL PRIMARY KEY,
    domain      TEXT NOT NULL UNIQUE,
    label       TEXT NOT NULL,           -- human-readable name
    is_active   BOOLEAN DEFAULT TRUE,
    added_at    TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO ref_ai_referrers (domain, label) VALUES
    ('chatgpt.com',             'ChatGPT'),
    ('chat.openai.com',         'ChatGPT'),
    ('perplexity.ai',           'Perplexity'),
    ('claude.ai',               'Claude'),
    ('gemini.google.com',       'Gemini'),
    ('bard.google.com',         'Gemini'),
    ('copilot.microsoft.com',   'Copilot'),
    ('bing.com',                'Bing/Copilot')
ON CONFLICT DO NOTHING;

-- Anomaly detection thresholds per metric
-- Analyst modules use these to decide what is worth flagging.
CREATE TABLE IF NOT EXISTS ref_anomaly_thresholds (
    id              SERIAL PRIMARY KEY,
    metric_name     TEXT NOT NULL,
    table_source    TEXT NOT NULL,
    threshold_pct   NUMERIC(6,3) NOT NULL,   -- flag if abs WoW change exceeds this
    min_sample_size INTEGER DEFAULT 50,       -- ignore if entity had fewer than this (views, sessions, etc.)
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (metric_name, table_source)
);

INSERT INTO ref_anomaly_thresholds (metric_name, table_source, threshold_pct, min_sample_size, notes) VALUES
    ('atc_rate',            'metrics_product_weekly',   20.0,   50,  'Flag products with >20% WoW ATC rate change'),
    ('purchase_rate',       'metrics_product_weekly',   25.0,   20,  'Flag products with >25% WoW purchase rate change'),
    ('blended_atc_rate',    'metrics_brand_weekly',     15.0,   200, 'Flag brands with >15% WoW blended ATC rate change'),
    ('revenue',             'metrics_brand_weekly',     25.0,   200, 'Flag brands with >25% WoW revenue change'),
    ('roas',                'metrics_paid_weekly',      20.0,   10,  'Flag campaigns with >20% WoW ROAS change'),
    ('spend',               'metrics_paid_weekly',      30.0,   10,  'Flag campaigns with >30% WoW spend change'),
    ('clicks',              'metrics_search_weekly',    30.0,   10,  'Flag queries with >30% WoW click change'),
    ('overall_conversion_rate', 'metrics_funnel_weekly', 10.0,  100, 'Flag if site-wide conversion rate changes >10%')
ON CONFLICT DO NOTHING;


-- ─────────────────────────────────────────────────────────────────────────────
-- LAYER 4: OUTPUT TABLES
-- Written by analyst modules and report generator. Read by Streamlit dashboard.
-- ─────────────────────────────────────────────────────────────────────────────

-- Structured findings from each analyst module (before LLM summarization)
CREATE TABLE IF NOT EXISTS findings (
    id              SERIAL PRIMARY KEY,
    week_ending     DATE NOT NULL,
    period_weeks    INTEGER NOT NULL DEFAULT 1,
    module          TEXT NOT NULL,       -- 'merchandising', 'funnel', 'search', 'paid_media', 'ai_referral'
    finding_type    TEXT NOT NULL,       -- 'issue', 'opportunity', 'alert', 'positive'
    severity        TEXT NOT NULL,       -- 'high', 'medium', 'low'
    title           TEXT NOT NULL,
    evidence        JSONB,               -- supporting data; always inspectable
    likely_cause    TEXT,
    suggested_action TEXT,
    urgency         TEXT,                -- 'this_week', 'monitor', 'backlog'
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Generated weekly reports
CREATE TABLE IF NOT EXISTS reports (
    id                  SERIAL PRIMARY KEY,
    week_ending         DATE NOT NULL,
    period_weeks        INTEGER NOT NULL DEFAULT 1,
    executive_summary   TEXT,
    full_report_md      TEXT,            -- full markdown report
    action_items        JSONB,           -- structured action item list
    generated_at        TIMESTAMPTZ DEFAULT NOW(),
    model_used          TEXT,
    UNIQUE (week_ending, period_weeks)
);

-- Anomalies detected by threshold rules
CREATE TABLE IF NOT EXISTS anomalies (
    id              SERIAL PRIMARY KEY,
    week_ending     DATE NOT NULL,
    table_source    TEXT,
    entity_type     TEXT,                -- 'product', 'brand', 'campaign', 'query'
    entity_id       TEXT,
    entity_name     TEXT,
    metric          TEXT,
    current_value   NUMERIC,
    prior_value     NUMERIC,
    pct_change      NUMERIC,
    threshold_used  NUMERIC,
    flagged_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Ingestion run log — tracks data freshness and silent failures
CREATE TABLE IF NOT EXISTS ingestion_log (
    id                  SERIAL PRIMARY KEY,
    source              TEXT NOT NULL,   -- 'ga4', 'gsc', 'google_ads', 'bigcommerce'
    run_at              TIMESTAMPTZ DEFAULT NOW(),
    date_range_start    DATE,
    date_range_end      DATE,
    rows_written        INTEGER DEFAULT 0,
    status              TEXT NOT NULL,   -- 'success', 'partial', 'failed'
    error_message       TEXT,
    duration_seconds    NUMERIC(8,2)
);


-- ─────────────────────────────────────────────────────────────────────────────
-- INDEXES
-- ─────────────────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_ga4_products_date ON raw_ga4_products_daily (date);
CREATE INDEX IF NOT EXISTS idx_ga4_products_item ON raw_ga4_products_daily (item_id);
CREATE INDEX IF NOT EXISTS idx_ga4_sources_date ON raw_ga4_sources_daily (date);
CREATE INDEX IF NOT EXISTS idx_ga4_sources_source ON raw_ga4_sources_daily (session_source);
CREATE INDEX IF NOT EXISTS idx_gsc_date ON raw_gsc_daily (date);
CREATE INDEX IF NOT EXISTS idx_gads_date ON raw_gads_daily (date);
CREATE INDEX IF NOT EXISTS idx_bc_products_sku ON raw_bc_products (sku);
CREATE INDEX IF NOT EXISTS idx_bc_products_brand ON raw_bc_products (bc_brand_id);
CREATE INDEX IF NOT EXISTS idx_metrics_product_week ON metrics_product_weekly (week_ending);
CREATE INDEX IF NOT EXISTS idx_metrics_brand_week ON metrics_brand_weekly (week_ending);
CREATE INDEX IF NOT EXISTS idx_findings_week ON findings (week_ending);
CREATE INDEX IF NOT EXISTS idx_findings_period_week ON findings (week_ending, period_weeks);
CREATE INDEX IF NOT EXISTS idx_reports_period_week ON reports (week_ending, period_weeks);
CREATE INDEX IF NOT EXISTS idx_ingestion_log_source ON ingestion_log (source, run_at);
