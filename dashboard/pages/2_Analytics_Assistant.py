import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import DateRange, Dimension, Filter, FilterExpression, Metric, RunReportRequest
from google.oauth2 import service_account
from openai import OpenAI

load_dotenv()

# Bridge st.secrets → os.environ for direct navigation to this page on
# Streamlit Cloud (Home.py may not have run in this session). Mirror of
# the bootstrap in dashboard/Home.py — idempotent.
try:
    _secrets_items = list(st.secrets.items())
except Exception:
    _secrets_items = []
for _k, _v in _secrets_items:
    if isinstance(_v, str) and _k not in os.environ:
        os.environ[_k] = _v

_sa_info = os.getenv("GOOGLE_SERVICE_ACCOUNT_INFO")
if _sa_info and not os.path.exists(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")):
    _sa_path = os.path.join(tempfile.gettempdir(), "google_service_account.json")
    with open(_sa_path, "w", encoding="utf-8") as _f:
        _f.write(_sa_info)
    os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = _sa_path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GA4_MODEL = os.getenv("GA4_MODEL", "gpt-5.4-mini")

TEMPLATES = [
    {
        "label": "Funnel Dropout Analysis",
        "query": "Where are we losing customers in the purchase funnel? Show me the step-by-step dropout rates for the last 30 days.",
    },
    {
        "label": "Channel Conversion Comparison",
        "query": "Which traffic channels have the best checkout conversion rate this month? Compare all channels.",
    },
    {
        "label": "Top Products Revenue Trend",
        "query": "What are our top 20 products by revenue this month vs. last month? Flag any big movers.",
    },
    {
        "label": "High Traffic / Low Conversion",
        "query": "Which products (by item name) have the most item views but the lowest add-to-cart rate over the last 30 days? Use item-scoped metrics — itemsViewed and itemsAddedToCart — not page metrics. Exclude items with fewer than 50 views.",
    },
    {
        "label": "Mobile vs Desktop Trend",
        "query": "How is mobile vs. desktop conversion rate trending over the last 90 days?",
    },
    {
        "label": "Traffic Source Growth",
        "query": "Which traffic sources are growing vs. declining in the last 90 days? Focus on sessions and revenue.",
    },
]

GA4_DATE_PATTERN = re.compile(r"^(today|yesterday|\d+daysAgo|\d{4}-\d{2}-\d{2})$")
ITEM_DIM_NAMES = ("itemId", "itemName", "itemBrand", "itemCategory")


def _escape_streamlit_markdown(text: str) -> str:
    """Escape `$` so Streamlit's markdown engine doesn't interpret revenue
    figures as LaTeX math delimiters. Without this, "$154 AOV ... $150-$400"
    renders as a mangled math expression."""
    return (text or "").replace("$", r"\$")


def _validate_ga4_date(value: str, fallback: str) -> str:
    """Substitute a safe default if the LLM emits a phrase the GA4 API can't parse.

    GA4 Data API accepts ONLY: today, yesterday, NdaysAgo, or YYYY-MM-DD.
    Natural-language phrases like "first day of this month" return a 400.
    """
    cleaned = (value or "").strip()
    return cleaned if GA4_DATE_PATTERN.match(cleaned) else fallback


def _build_filter(filter_spec: dict | None) -> FilterExpression | None:
    """Translate a translator-emitted filter dict into a GA4 FilterExpression.

    Supports a single-dimension string filter (the common case for brand /
    product-name lookups). Returns None if the spec is missing or malformed.
    """
    if not filter_spec or not isinstance(filter_spec, dict):
        return None
    dim = str(filter_spec.get("dimension", "")).strip()
    val = str(filter_spec.get("value", "")).strip()
    if not dim or not val:
        return None
    match_type_str = str(filter_spec.get("match_type", "CONTAINS")).strip().upper()
    match_type = getattr(
        Filter.StringFilter.MatchType,
        match_type_str,
        Filter.StringFilter.MatchType.CONTAINS,
    )
    return FilterExpression(
        filter=Filter(
            field_name=dim,
            string_filter=Filter.StringFilter(
                value=val,
                match_type=match_type,
                case_sensitive=bool(filter_spec.get("case_sensitive", False)),
            ),
        )
    )


def _aggregate_item_transaction_rows(spec: dict, rows: list[dict]) -> tuple[dict, list[dict]]:
    """Collapse (item, transactionId) rows into per-item totals.

    Why: when the translator includes `transactionId` alongside an item dimension
    every row represents one item-in-one-order. The LLM cannot reliably aggregate
    thousands of such rows in its head and tends to misread "1 row = 1 transaction
    for this item" as "1 transaction TOTAL for this item." Doing the aggregation
    in Python is deterministic and matches what the analyst prompt expects:
    per-item revenue, units sold, and a count of distinct transactions.

    No-op if the report doesn't have both transactionId and an item dimension.
    """
    dims = spec.get("dimensions", []) or []
    if "transactionId" not in dims:
        return spec, rows
    item_dim = next((d for d in dims if d in ITEM_DIM_NAMES), None)
    if item_dim is None:
        return spec, rows

    by_item: dict[str, dict] = {}
    for row in rows:
        key = row.get(item_dim, "(unknown)")
        bucket = by_item.setdefault(
            key,
            {
                item_dim: key,
                "itemRevenue": 0.0,
                "itemsPurchased": 0,
                "_txn_ids": set(),
            },
        )
        try:
            bucket["itemRevenue"] += float(row.get("itemRevenue", "0") or 0)
        except (TypeError, ValueError):
            pass
        try:
            bucket["itemsPurchased"] += int(float(row.get("itemsPurchased", "0") or 0))
        except (TypeError, ValueError):
            pass
        txn = row.get("transactionId")
        if txn:
            bucket["_txn_ids"].add(txn)

    aggregated = []
    for bucket in by_item.values():
        aggregated.append(
            {
                item_dim: bucket[item_dim],
                "itemRevenue": round(bucket["itemRevenue"], 2),
                "itemsPurchased": bucket["itemsPurchased"],
                "transactions": len(bucket["_txn_ids"]),
            }
        )
    aggregated.sort(key=lambda r: r["itemRevenue"], reverse=True)

    new_spec = dict(spec)
    new_spec["dimensions"] = [item_dim]
    new_spec["metrics"] = ["itemRevenue", "itemsPurchased", "transactions"]
    return new_spec, aggregated


def _build_translate_prompt(today: str) -> str:
    """System prompt for the question→GA4-params translator. Today's date is
    injected so the model can convert relative phrases like "this month" into
    concrete YYYY-MM-DD ranges."""
    return f"""You are a Google Analytics 4 Data API expert. Translate the user's natural language question into GA4 Data API report parameters.

The GA4 property is for lightbulbsurplus.com, an ecommerce site. Today's date is {today}.

Return ONLY valid JSON (no markdown, no code fences, no explanation) with this exact structure:
{{
  "reports": [
    {{
      "dimensions": ["dimension1", "dimension2"],
      "metrics": ["metric1", "metric2"],
      "start_date": "30daysAgo",
      "end_date": "today",
      "filter": {{
        "dimension": "itemBrand",
        "match_type": "CONTAINS",
        "value": "Keystone",
        "case_sensitive": false
      }}
    }}
  ]
}}

The `filter` field is OPTIONAL. Include it ONLY when the user names a specific brand, product, page path, channel, or other concrete value to narrow the report. Without a filter, the report returns the whole catalog/site (and may be capped by row limits).

You can return multiple reports if the question requires comparing different time periods or different slices.

Common GA4 dimensions:
- session-scoped: date, pagePath, sessionDefaultChannelGroup, sessionSource, sessionMedium, deviceCategory, country, city, landingPage, newVsReturning
- item-scoped: itemId, itemName, itemBrand, itemCategory
- event-scoped: transactionId
Common GA4 metrics:
- session-scoped: sessions, engagedSessions, bounceRate, averageSessionDuration, newUsers, totalUsers, screenPageViews, totalRevenue, ecommercePurchases, transactions, keyEvents, conversions
- item-scoped: itemRevenue, itemsViewed, itemsAddedToCart, itemsCheckedOut, itemsPurchased

GA4 API DATE FORMAT (strict — anything else returns a 400 error):
- "today", "yesterday"
- "NdaysAgo" (e.g., "7daysAgo", "30daysAgo", "90daysAgo")
- "YYYY-MM-DD" (e.g., "{today}")
NEVER output natural-language phrases like "first day of this month", "start of last quarter", or "beginning of the year". Convert them using today = {today}:
- "this month" / "this month so far" → start_date: first of current month in YYYY-MM-DD, end_date: "today"
- "last month" → start_date: first of prior month YYYY-MM-DD, end_date: last day of prior month YYYY-MM-DD
- "this year" → start_date: "YYYY-01-01" of current year, end_date: "today"
- "last 30 days" → start_date: "30daysAgo", end_date: "today"

GA4 SCOPE COMPATIBILITY (critical — the API returns 400 if violated):
- item-scoped dimensions are INCOMPATIBLE with session-scoped metrics. Never put `transactions`, `totalRevenue`, `sessions`, `ecommercePurchases`, or `keyEvents` in the same report as `itemId`/`itemName`/`itemBrand`/`itemCategory`.
- For per-item analysis, use only item-scoped metrics with item-scoped dimensions.
- `transactionId` (event-scoped) IS compatible with item-scoped metrics — use it when you need per-item transaction counts.

Rules:
- For funnel analysis, use item-scoped metrics: itemsViewed, itemsAddedToCart, itemsCheckedOut, itemsPurchased
- For site-level metrics (no item dimensions), use sessions, totalRevenue, ecommercePurchases, transactions
- TREND / COMPARISON DETECTION (critical): if the question contains ANY of these signals — "plummeted", "declined", "dropped", "fell", "fallen", "down", "up", "grown", "growing", "increasing", "decreasing", "trending", "vs last", "compared to", "year over year", "YoY", "month over month", "MoM" — you MUST return TWO reports with identical dimensions, metrics, and filter, but different date ranges. Without two reports the answer cannot prove the trend.
  - "Has Keystone plummeted in the last 90 days?" → Report 1: 90daysAgo→today; Report 2: 180daysAgo→91daysAgo
  - "Is mobile traffic growing?" → Report 1: 30daysAgo→today; Report 2: 60daysAgo→31daysAgo
  - "Revenue this month vs last month" → Report 1: first of current month→today; Report 2: prior month full range (YYYY-MM-01→YYYY-MM-DD last day)
  - "Sales YoY for last 30 days" → Report 1: 30daysAgo→today; Report 2: same dates one year prior in YYYY-MM-DD form
- Limit dimensions to 3 max per report
- Always include the most relevant dimensions for the question
- For "product page" / "high traffic / low ATC" / per-product engagement questions, use the item dimension `itemName` (or `itemId`) — NOT `pagePath`. `pagePath` includes category, blog, and search pages that don't have add-to-cart and will pollute the result. Item metrics: `itemsViewed`, `itemsAddedToCart`, `itemsCheckedOut`, `itemsPurchased`.
- For product revenue / top products / revenue movers / product trend / brand performance questions: the report MUST include `transactionId` as a dimension alongside the item dimension (e.g., `itemName` + `transactionId`). Required metrics: `itemRevenue` and `itemsPurchased`. The result is one row per (item, transactionId) pair; downstream code aggregates per item and counts distinct transactions to distinguish "one large bulk order" (few transactions, many units) from "broad demand" (many transactions). DO NOT include `transactions` as a metric in any report containing item-scoped dimensions — it will cause a 400 error.
- DIMENSION FILTERS (use them when the user names a specific subject):
  - "How is Keystone doing?" / "Keystone sales last 90 days" → filter `{{"dimension": "itemBrand", "match_type": "CONTAINS", "value": "Keystone", "case_sensitive": false}}`
  - "Sales of GE bulbs" → filter on `itemBrand` CONTAINS "GE" (or `itemName` if you're not sure the brand dimension is populated for that vendor)
  - "Performance of /led-bulbs/ category page" → filter on `pagePath` CONTAINS "/led-bulbs/"
  - "Direct traffic conversion rate" → filter on `sessionDefaultChannelGroup` EXACT "Direct"
  Without a filter, brand/product-specific questions get diluted by the whole catalog. ALWAYS add a filter when the user names a brand, vendor, or product term.
- Match types: "CONTAINS" (case-insensitive partial match — safe default), "EXACT" (full equality), "BEGINS_WITH" (prefix match)."""


def clear_broken_proxy_env():
    """Remove known bad proxy vars so direct API calls use normal outbound access."""
    for key in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "GIT_HTTP_PROXY", "GIT_HTTPS_PROXY"):
        os.environ.pop(key, None)


def get_ga4_client() -> BetaAnalyticsDataClient:
    """Return authenticated GA4 Data API client."""
    clear_broken_proxy_env()
    sa_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not os.path.isabs(sa_file):
        sa_file = str(PROJECT_ROOT / sa_file)
    credentials = service_account.Credentials.from_service_account_file(
        sa_file,
        scopes=["https://www.googleapis.com/auth/analytics.readonly"],
    )
    return BetaAnalyticsDataClient(credentials=credentials)


def get_openai_client() -> OpenAI:
    """Return an OpenAI client with direct outbound networking."""
    clear_broken_proxy_env()
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def load_system_prompt() -> str:
    """Load the LBS analyst context from prompts/ga4_analyst_context.md."""
    prompt_path = PROJECT_ROOT / "prompts" / "ga4_analyst_context.md"
    return prompt_path.read_text(encoding="utf-8")


def _normalize_metrics(metrics: list[str]) -> list[str]:
    """Normalize report metrics to strings."""
    return [str(metric).strip() for metric in metrics if str(metric).strip()]


def _normalize_dimensions(dimensions: list[str]) -> list[str]:
    """Normalize report dimensions and cap the list to 3."""
    cleaned = [str(dimension).strip() for dimension in dimensions if str(dimension).strip()]
    return cleaned[:3]


def _parse_json_payload(text: str) -> dict:
    """Strip code fences if present and parse JSON text."""
    payload = text.strip()
    if payload.startswith("```"):
        payload = payload.split("\n", 1)[1]
        if payload.endswith("```"):
            payload = payload[:-3]
    return json.loads(payload.strip())


def _extract_response_text(resp) -> str:
    """Extract text from an OpenAI Responses API response object."""
    output = getattr(resp, "output", None)
    if not output:
        return ""
    chunks = []
    for item in output:
        if getattr(item, "type", "") == "refusal":
            continue
        contents = getattr(item, "content", None)
        if contents is None and hasattr(item, "message"):
            contents = getattr(item.message, "content", None)
        if not contents:
            continue
        for part in contents:
            text_val = getattr(part, "text", None) or getattr(part, "output_text", None)
            if text_val:
                chunks.append(str(text_val))
    return "\n".join(chunks).strip()


def translate_question(client: OpenAI, question: str) -> list[dict]:
    """Ask OpenAI to translate a natural language question into GA4 API params."""
    today = datetime.now().strftime("%Y-%m-%d")
    response = client.responses.create(
        model=GA4_MODEL,
        input=[
            {"role": "system", "content": [{"type": "input_text", "text": _build_translate_prompt(today)}]},
            {"role": "user", "content": [{"type": "input_text", "text": question}]},
        ],
    )
    text = _extract_response_text(response)
    parsed = _parse_json_payload(text)
    reports = parsed.get("reports", [])

    normalized_reports = []
    for report in reports:
        dimensions = _normalize_dimensions(report.get("dimensions", []))
        metrics = _normalize_metrics(report.get("metrics", []))
        start_date = _validate_ga4_date(str(report.get("start_date", "")), "30daysAgo")
        end_date = _validate_ga4_date(str(report.get("end_date", "")), "today")

        if not metrics:
            continue

        filter_spec = report.get("filter") if isinstance(report.get("filter"), dict) else None

        normalized_reports.append(
            {
                "dimensions": dimensions,
                "metrics": metrics,
                "start_date": start_date,
                "end_date": end_date,
                "filter": filter_spec,
            }
        )

    if not normalized_reports:
        raise json.JSONDecodeError("No valid reports returned by translator", text, 0)

    return normalized_reports


def run_ga4_report(
    client: BetaAnalyticsDataClient,
    dimensions: list[str],
    metrics: list[str],
    start_date: str,
    end_date: str,
    filter_spec: dict | None = None,
) -> list[dict]:
    """
    Run a GA4 report and return rows as a list of dicts.
    Falls back from keyEvents to conversions when the property rejects keyEvents.
    """
    property_id = os.getenv("GA4_PROPERTY_ID")
    active_metrics = list(metrics)
    dim_filter = _build_filter(filter_spec)

    def _build_request(metrics_list):
        kwargs = dict(
            property=f"properties/{property_id}",
            dimensions=[Dimension(name=dimension) for dimension in dimensions],
            metrics=[Metric(name=metric) for metric in metrics_list],
            date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
            limit=10_000,
        )
        if dim_filter is not None:
            kwargs["dimension_filter"] = dim_filter
        return RunReportRequest(**kwargs)

    request = _build_request(active_metrics)

    try:
        response = client.run_report(request)
    except Exception as exc:
        if "keyEvents" not in str(exc) or "keyEvents" not in active_metrics:
            raise
        active_metrics = ["conversions" if metric == "keyEvents" else metric for metric in active_metrics]
        response = client.run_report(_build_request(active_metrics))

    rows = []
    dim_headers = [header.name for header in response.dimension_headers]
    met_headers = [header.name for header in response.metric_headers]
    for row in response.rows:
        row_dict = {}
        for index, header in enumerate(dim_headers):
            row_dict[header] = row.dimension_values[index].value
        for index, header in enumerate(met_headers):
            row_dict[header] = row.metric_values[index].value
        rows.append(row_dict)
    return rows


def interpret_results(client: OpenAI, question: str, results: list[dict], system_prompt: str) -> str:
    """Ask OpenAI to interpret GA4 data in LBS business context."""
    results_text = json.dumps(results, indent=2)
    user_message = f"""Original question: {question}

Here is the raw GA4 data:

{results_text}

Analyze this data and answer the original question. Use specific numbers. Flag anything that needs attention."""

    response = client.responses.create(
        model=GA4_MODEL,
        input=[
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_message}]},
        ],
    )
    return _extract_response_text(response)


def run_ga4_query(question: str) -> tuple[str, list[dict]]:
    """Full pipeline: question -> GA4 API -> interpreted answer."""
    openai_client = get_openai_client()
    ga4_client = get_ga4_client()
    system_prompt = load_system_prompt()

    report_specs = translate_question(openai_client, question)

    all_results = []
    total_rows = 0
    for index, spec in enumerate(report_specs):
        rows = run_ga4_report(
            ga4_client,
            dimensions=spec["dimensions"],
            metrics=spec["metrics"],
            start_date=spec["start_date"],
            end_date=spec["end_date"],
            filter_spec=spec.get("filter"),
        )
        effective_spec, effective_rows = _aggregate_item_transaction_rows(spec, rows)
        total_rows += len(effective_rows)
        all_results.append(
            {
                "report_index": index,
                "dimensions": effective_spec["dimensions"],
                "metrics": effective_spec["metrics"],
                "date_range": f"{effective_spec['start_date']} to {effective_spec['end_date']}",
                "filter": effective_spec.get("filter"),
                "row_count": len(effective_rows),
                "rows": effective_rows[:200],
            }
        )

    if total_rows == 0:
        return (
            "### No data returned for this query\n"
            "Try a broader date range, a simpler dimension slice, or a more direct question.",
            all_results,
        )

    answer = interpret_results(openai_client, question, all_results, system_prompt)
    return answer, all_results


st.set_page_config(page_title="LBS Query GA4", page_icon="🔍", layout="wide")

st.markdown(
    """
    <style>
    /* Ensure selectbox and inputs are always readable */
    .stSelectbox [data-baseweb="select"] > div {
        color: #063c6e !important;
        background-color: #ffffff !important;
    }
    [data-baseweb="popover"] li {
        color: #063c6e !important;
    }
    /* Template buttons - navy border, readable text */
    .stButton > button:not([kind="primary"]) {
        border: 1px solid #063c6e !important;
        color: #063c6e !important;
        background-color: #ffffff !important;
    }
    .stButton > button:not([kind="primary"]):hover {
        background-color: #e8f4fd !important;
        border-color: #0eb5fd !important;
        color: #063c6e !important;
    }
    /* Text area border visibility */
    .stTextArea textarea {
        border: 1px solid #063c6e !important;
        color: #063c6e !important;
    }
    .stTextArea textarea:focus {
        border-color: #0eb5fd !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Query GA4")
st.caption("Ask natural language questions about LBS site analytics. Data is live from Google Analytics 4.")

missing = []
if not os.getenv("OPENAI_API_KEY"):
    missing.append("OPENAI_API_KEY")
if not os.getenv("GA4_PROPERTY_ID"):
    missing.append("GA4_PROPERTY_ID")
if not os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"):
    missing.append("GOOGLE_SERVICE_ACCOUNT_JSON")
if missing:
    st.error(f"Missing environment variables: {', '.join(missing)}")
    st.caption("Check your .env file.")
    st.stop()

st.session_state.setdefault("ga4_query", "")
st.session_state.setdefault("ga4_query_input", st.session_state["ga4_query"])
st.session_state.setdefault("ga4_query_history", [])

st.subheader("Quick Queries")
row1 = st.columns(3)
row2 = st.columns(3)
all_cols = row1 + row2

for index, template in enumerate(TEMPLATES):
    with all_cols[index]:
        if st.button(template["label"], key=f"tpl_{index}", use_container_width=True):
            st.session_state["ga4_query"] = template["query"]
            st.session_state["ga4_query_input"] = template["query"]

st.subheader("Your Question")
query = st.text_area(
    label="Ask anything about LBS GA4 data",
    height=120,
    placeholder="e.g. Which product categories have the highest add-to-cart rate this month?",
    key="ga4_query_input",
)
st.session_state["ga4_query"] = query

button_col, status_col = st.columns([2, 5])
with button_col:
    run_clicked = st.button("Run Analysis", type="primary")
with status_col:
    st.caption("Sends your question to GA4 via the Data API, then interprets results with AI.")

def render_raw_query_details(reports: list[dict]) -> None:
    """Show translated GA4 params + sample rows under the answer."""
    if not reports:
        return
    with st.expander("Raw query details", expanded=False):
        for report in reports:
            st.markdown(
                f"**Report {report['report_index'] + 1}** — "
                f"{report['date_range']} · {report['row_count']} rows"
            )
            st.markdown(
                f"- **Dimensions:** `{', '.join(report['dimensions']) or '(none)'}`  \n"
                f"- **Metrics:** `{', '.join(report['metrics'])}`"
            )
            if report["rows"]:
                st.dataframe(report["rows"], use_container_width=True, height=240)
            else:
                st.caption("No rows returned for this report.")


if run_clicked and not query.strip():
    st.warning("Please enter a question first.")
elif run_clicked and query.strip():
    with st.spinner("Querying GA4 and analyzing... this usually takes 15-30 seconds."):
        try:
            result_text, raw_results = run_ga4_query(query.strip())
            st.session_state["ga4_query_history"] = [
                {"question": query.strip(), "result": result_text, "reports": raw_results}
            ] + st.session_state["ga4_query_history"][:4]
            st.divider()
            st.markdown(_escape_streamlit_markdown(result_text))
            render_raw_query_details(raw_results)
        except json.JSONDecodeError:
            st.error("AI returned invalid query parameters. Try rephrasing your question.")
        except Exception as exc:
            error_message = str(exc)
            st.error(f"Query failed: {error_message}")
            if "credentials" in error_message.lower() or "auth" in error_message.lower():
                st.caption("Check that GOOGLE_SERVICE_ACCOUNT_JSON points to a valid file.")
            elif "quota" in error_message.lower() or "rate" in error_message.lower():
                st.caption("GA4 API quota may be exceeded. Wait a minute and try again.")
            elif "proxy" in error_message.lower():
                st.caption("A local proxy setting blocked the request. Reload the page and try again.")
            else:
                st.caption("Check the terminal for full error details.")

if st.session_state["ga4_query_history"]:
    st.divider()
    with st.expander("Recent Queries", expanded=False):
        for item in st.session_state["ga4_query_history"]:
            st.markdown(f"**{item['question']}**")
            st.markdown(_escape_streamlit_markdown(item["result"]))
            render_raw_query_details(item.get("reports", []))
