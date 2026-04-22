import json
import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest
from google.oauth2 import service_account
from openai import OpenAI

load_dotenv()

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
        "query": "Which product pages have the most traffic but the lowest add-to-cart rate? Last 30 days.",
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

TRANSLATE_SYSTEM_PROMPT = """You are a Google Analytics 4 Data API expert. Translate the user's natural language question into GA4 Data API report parameters.

The GA4 property is for lightbulbsurplus.com, an ecommerce site.

Return ONLY valid JSON (no markdown, no code fences, no explanation) with this exact structure:
{
  "reports": [
    {
      "dimensions": ["dimension1", "dimension2"],
      "metrics": ["metric1", "metric2"],
      "start_date": "30daysAgo",
      "end_date": "today"
    }
  ]
}

You can return multiple reports if the question requires comparing different time periods or different slices.

Common GA4 dimensions: date, pagePath, sessionDefaultChannelGroup, sessionSource, sessionMedium, deviceCategory, country, city, itemId, itemName, itemBrand, itemCategory, landingPage, newVsReturning
Common GA4 metrics: sessions, engagedSessions, bounceRate, averageSessionDuration, newUsers, totalUsers, screenPageViews, totalRevenue, itemsViewed, itemsAddedToCart, itemsCheckedOut, itemsPurchased, itemRevenue, ecommercePurchases, keyEvents, conversions
Date formats: "today", "yesterday", "NdaysAgo" (e.g., "30daysAgo", "90daysAgo"), or "YYYY-MM-DD"

Rules:
- For funnel analysis, use item-scoped metrics: itemsViewed, itemsAddedToCart, itemsCheckedOut, itemsPurchased
- For site-level metrics, use sessions, totalRevenue, ecommercePurchases
- For time comparisons (this month vs last), use two report entries with different date ranges
- Limit dimensions to 3 max per report
- Always include the most relevant dimensions for the question
- For ANY product revenue / top products / revenue movers / product trend question, the metrics list MUST include all three of: itemRevenue, transactions, itemsPurchased. These let downstream analysis distinguish "one large bulk order" (few transactions, many units) from "broad demand" (many transactions). Do not omit transactions or itemsPurchased for revenue-ranking questions even if the user only asked about revenue."""


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
    response = client.responses.create(
        model=GA4_MODEL,
        input=[
            {"role": "system", "content": [{"type": "input_text", "text": TRANSLATE_SYSTEM_PROMPT}]},
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
        start_date = str(report.get("start_date", "30daysAgo")).strip()
        end_date = str(report.get("end_date", "today")).strip()

        if not metrics:
            continue

        normalized_reports.append(
            {
                "dimensions": dimensions,
                "metrics": metrics,
                "start_date": start_date,
                "end_date": end_date,
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
) -> list[dict]:
    """
    Run a GA4 report and return rows as a list of dicts.
    Falls back from keyEvents to conversions when the property rejects keyEvents.
    """
    property_id = os.getenv("GA4_PROPERTY_ID")
    active_metrics = list(metrics)

    request = RunReportRequest(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name=dimension) for dimension in dimensions],
        metrics=[Metric(name=metric) for metric in active_metrics],
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        limit=10_000,
    )

    try:
        response = client.run_report(request)
    except Exception as exc:
        if "keyEvents" not in str(exc) or "keyEvents" not in active_metrics:
            raise
        active_metrics = ["conversions" if metric == "keyEvents" else metric for metric in active_metrics]
        request = RunReportRequest(
            property=f"properties/{property_id}",
            dimensions=[Dimension(name=dimension) for dimension in dimensions],
            metrics=[Metric(name=metric) for metric in active_metrics],
            date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
            limit=10_000,
        )
        response = client.run_report(request)

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
        )
        total_rows += len(rows)
        all_results.append(
            {
                "report_index": index,
                "dimensions": spec["dimensions"],
                "metrics": spec["metrics"],
                "date_range": f"{spec['start_date']} to {spec['end_date']}",
                "row_count": len(rows),
                "rows": rows[:200],
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
    run_clicked = st.button("Run Analysis", type="primary", disabled=not query.strip())
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


if run_clicked and query.strip():
    with st.spinner("Querying GA4 and analyzing... this usually takes 15-30 seconds."):
        try:
            result_text, raw_results = run_ga4_query(query.strip())
            st.session_state["ga4_query_history"] = [
                {"question": query.strip(), "result": result_text, "reports": raw_results}
            ] + st.session_state["ga4_query_history"][:4]
            st.divider()
            st.markdown(result_text)
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
            st.markdown(item["result"])
            render_raw_query_details(item.get("reports", []))
