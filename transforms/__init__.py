# Transforms layer — reads raw_ tables, computes metrics, writes metrics_ tables.
# Always runs after ingestion completes. Never touches raw_ tables for output.
# Run order: product_metrics → brand_metrics → funnel_metrics → channel_metrics
#            → search_metrics → paid_metrics
