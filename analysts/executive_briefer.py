"""
Executive Briefer — aggregates all module findings and calls OpenAI API
to generate the weekly report narrative.

Reads:  findings (all modules for week_ending)
Writes: reports

This is the ONLY module that calls the LLM. It receives structured findings
from all analyst modules and asks the model to:
1. Write an executive summary (top 3-5 issues, top 3-5 opportunities)
2. Write narrative sections for each module's findings
3. Produce a ranked, plain-English action item list with urgency

The LLM does NOT generate findings — it translates structured data into prose.
"""

import os
import re
import json
from datetime import datetime

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError, APIError, APIConnectionError, Timeout
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

load_dotenv()

OPENAI_MODEL = os.getenv("BRIEFER_MODEL", "gpt-5.4")

SYSTEM_PROMPT_TEMPLATE = """You are a seasoned ecommerce analyst embedded at Light Bulb Surplus (LBS),
a large catalog lighting brand that sells wholesale/bulk lighting products via drop-ship.
You have a sharp eye for separating signal from noise, and you translate analytics into
concise, actionable intelligence — not data summaries. Cut what doesn't matter. Every
sentence must earn its place.

You are generating a rolling {period_weeks}-week intelligence briefing for the business owner and team.

Context about LBS:
- ~30k SKUs, mostly drop-ship, BigCommerce platform
- B2B wholesale buyer base — most sessions are returning buyers who know what they want
- Two traffic types: brand+MPN searches and intent-based keyword searches
- White label brands: Contractor Essentials, LBS Lighting
- Competes on price and availability, not brand exclusivity
- Report readers are operators who need to act, not data scientists who want to explore

Noise rules — skip or suppress these:
- Organic search queries with fewer than 20 prior-period clicks: too small to act on
- Any % change where the underlying volume is fewer than 5 units (clicks, orders, sessions)
- Channel or segment findings where the revenue change is under $500 absolute
- Findings marked "positive" with no clear action needed: mention briefly or omit entirely

Each finding's evidence dict contains:
- period_weeks: length of the aggregation window (usually 4 weeks)
- period_start / period_end: the dates covered
- partial_period: true if the window has fewer weeks than expected — call this out explicitly
- yoy_change_pct: change vs the SAME {period_weeks}-week window exactly 52 weeks ago (same calendar period last year)
- yoy_partial: true if YoY data has gaps — note this

When writing YoY context, always clarify it as "vs the same period last year" so readers
understand it is not a full-year comparison.
When a finding has YoY data, lead with period-vs-prior change, then add YoY as context.
When partial_period is true, explicitly note that the period is incomplete.

Brand Performance findings include BC order data (bc_order_count, bc_unique_customers,
prior_bc_order_count, prior_bc_unique_customers). When order_data_available is true:
- ALWAYS include order context inline. Format: "X orders from Y customers (prior: A from B)"
- CRITICAL — classify each brand finding by what the order data actually shows, not just the revenue change:

  DEMAND GROWING, REVENUE FELL (bc_order_count > prior_bc_order_count AND
  bc_unique_customers >= prior_bc_unique_customers but revenue_change_pct < 0):
  → Pricing/mix compression, NOT a demand decline. Do NOT group with brands showing true
    demand loss. Frame as: "demand growing (X orders from Y customers, up from A/B) but
    revenue down Z% — avg order value compressed." Action: review product/pricing mix.
    List separately as a pricing-mix flag, not a brand concern.

  DEMAND DECLINING (bc_order_count < prior_bc_order_count OR
  bc_unique_customers < prior_bc_unique_customers):
  → bc_order_count <= 2 OR bc_unique_customers <= 2: "— may reflect order timing, not a trend"
  → bc_order_count >= 5 AND bc_unique_customers >= 3: "— broad demand decline"
  → These belong in the Executive Summary as genuine brand concerns.

  DEMAND FLAT/STABLE (counts within ±10% of prior):
  → Note as stable demand with revenue/mix pressure. Not a demand emergency.

- If bc_order_count is None/0 and order_data_available is true: brand had no qualifying orders this period
When order_data_available is false: use GA4 revenue only, note "order-level detail unavailable."

Report length rules:
- Action Items: maximum 10 items, ranked by business impact. Cut the rest.
- Organic Search: maximum 4-5 findings, highest-volume queries only. No sub-10-click examples.
- Product Opportunities: 6-7 products maximum across both sub-sections combined.
- Total report should be concise — every section tight, no padding, no restating what the numbers already show.

Report tone: direct, confident, action-oriented. Write like a seasoned analyst, not a reporting tool.
Output ONLY the markdown report. No preamble, no commentary, no "Sure!" or "Here's your report"."""


# ── OpenAI Client ────────────────────────────────────────────────────────────

_api_key = os.getenv("OPENAI_API_KEY", "").strip()
client = OpenAI(api_key=_api_key)


@retry(
    reraise=True,
    wait=wait_exponential(min=1, max=60),
    stop=stop_after_attempt(5),
    retry=retry_if_exception_type((RateLimitError, APIError, APIConnectionError, Timeout)),
)
def _create_response(**kwargs):
    """Call OpenAI Responses API with automatic retry on transient errors."""
    return client.responses.create(**kwargs)


def _extract_response_text(resp) -> str:
    """Extract text from OpenAI Responses API response object."""
    output = getattr(resp, "output", None)
    if not output:
        return ""
    chunks = []
    for item in output:
        if getattr(item, "type", "") == "refusal":
            print(f"  [executive_briefer] WARNING: Model refused request: "
                  f"{getattr(item, 'refusal', 'Unknown')}")
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


# ── Findings Loader ──────────────────────────────────────────────────────────

def build_system_prompt(period_weeks: int) -> str:
    """Return the rolling-period system prompt for the current run."""
    return SYSTEM_PROMPT_TEMPLATE.format(period_weeks=period_weeks)


def load_findings(db_conn, week_ending: str, period_weeks: int = 1) -> list[dict]:
    """Load all findings for the week from the findings table."""
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT week_ending, module, finding_type, severity,
                   title, evidence, likely_cause, suggested_action, urgency
            FROM findings
            WHERE week_ending = %s
              AND period_weeks = %s
            ORDER BY
                CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                CASE urgency WHEN 'this_week' THEN 0 WHEN 'monitor' THEN 1 ELSE 2 END,
                module
        """, (week_ending, period_weeks))
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()

    findings = []
    for row in rows:
        finding = dict(zip(columns, row))
        # date objects aren't JSON-serializable
        finding["week_ending"] = str(finding["week_ending"])
        # evidence is already a dict (psycopg2 auto-deserializes JSONB)
        findings.append(finding)

    return findings


# ── Prompt Builder ───────────────────────────────────────────────────────────

def build_report_prompt(findings: list[dict], week_ending: str, period_weeks: int) -> str:
    """
    Build the prompt sent to the LLM.
    Findings are passed as compact JSON — the model writes the narrative.
    """
    findings_json = json.dumps(findings, default=str)
    return f"""Generate a rolling {period_weeks}-week intelligence briefing for the period ending {week_ending}.

Here are the structured findings from all analyst modules:

{findings_json}

Write the report in this exact structure:

# LBS Intelligence Briefing — {period_weeks}-Week Period Ending {week_ending}

## Executive Summary
### Comparison Context
[State the aggregation window: current {period_weeks}-week period ending {week_ending}, the immediately prior {period_weeks}-week period, and the YoY comparison window when available. Call out if the current period is partial.]

[Top 3-5 issues and top 3-5 opportunities. One sentence each. Most important first.]

## Product Opportunities
[Products with high add-to-cart but low checkout conversion — ranked list with specific action for each. Use period-over-period change first, then YoY if available.]
[Products with high traffic but low add-to-cart — ranked list]

## Brand Performance
[Brand-level findings — any significant period-over-period changes]

## Funnel
[Site-wide funnel findings, new vs returning split if notable. Include YoY context where useful.]

## Paid Search
[Google Ads findings — always separate rank vs budget impression share loss]

## Organic Search
[GSC findings — click trends, CTR erosion, branded share]

## AI Referral Traffic
[Sessions, revenue, top source, trend]

## Action Items
[Numbered list. Format: Issue | Likely Cause | Recommended Action | Urgency]
Urgency options: THIS WEEK / MONITOR / BACKLOG
Example: High ATC low checkout on Product X | Shipping cost shock at checkout | Add shipping estimate to PDP | THIS WEEK

Keep the report concise. Every sentence should be useful."""


# ── Report Parsing Helpers ───────────────────────────────────────────────────

def _extract_section(report_md: str, section_name: str) -> str:
    """Extract content between ## Section Name and the next ## heading."""
    lines = report_md.split("\n")
    in_section = False
    section_lines = []

    for line in lines:
        if line.strip().startswith("## ") and section_name.lower() in line.lower():
            in_section = True
            continue
        elif line.strip().startswith("## ") and in_section:
            break
        elif in_section:
            section_lines.append(line)

    result = "\n".join(section_lines).strip()
    return result if result else None


def _parse_action_items(report_md: str) -> list[dict]:
    """
    Parse the Action Items section from the report into structured dicts.
    Expected format per item: numbered list with pipe-separated fields
    e.g. "1. Issue description | Likely cause | Recommended action | THIS WEEK"
    """
    section = _extract_section(report_md, "Action Items")
    if not section:
        return []

    items = []
    for line in section.split("\n"):
        line = line.strip()
        if not line:
            continue

        # Strip leading number + period or bullet
        cleaned = re.sub(r"^\d+\.\s*", "", line)
        cleaned = re.sub(r"^[-*]\s*", "", cleaned)

        if not cleaned:
            continue

        parts = [p.strip() for p in cleaned.split("|")]
        item = {"raw": cleaned}

        if len(parts) >= 4:
            item["issue"] = parts[0]
            item["likely_cause"] = parts[1]
            item["recommended_action"] = parts[2]
            item["urgency"] = parts[3].upper().strip()
        elif len(parts) >= 2:
            item["issue"] = parts[0]
            item["recommended_action"] = parts[1]
        else:
            item["issue"] = cleaned

        items.append(item)

    return items


# ── Main Report Generator ───────────────────────────────────────────────────

def generate_report(db_conn, week_ending: str, period_weeks: int = 4) -> str:
    """
    Load findings, call OpenAI, write report to reports table.
    Returns the generated report markdown.
    """
    # 1. Load findings
    findings = load_findings(db_conn, week_ending, period_weeks=period_weeks)
    print(f"  [executive_briefer] Loaded {len(findings)} findings for {week_ending}")

    # Cap findings at 50 to manage prompt size / API cost
    if len(findings) > 50:
        findings = findings[:50]
        print(f"  [executive_briefer] Capped to 50 findings")

    # 2. Handle empty findings (baseline week)
    if not findings:
        findings = [{
            "week_ending": week_ending,
            "period_weeks": period_weeks,
            "module": "system",
            "finding_type": "alert",
            "severity": "low",
            "title": "Baseline period — no analyst findings generated",
            "evidence": {
                "note": ("This is the first run or all analysts returned 0 findings "
                         "(likely no prior period data for comparison)."),
                "period_weeks": period_weeks,
            },
            "likely_cause": "First reporting period of data collection — no historical comparison available yet.",
            "suggested_action": "Run historical backfill to enable period comparisons, then re-run the pipeline.",
            "urgency": "this_week"
        }]

    # 3. Build prompt
    prompt = build_report_prompt(findings, week_ending, period_weeks)

    # 4. Call OpenAI Responses API
    input_messages = [
        {"role": "system", "content": [{"type": "input_text", "text": build_system_prompt(period_weeks)}]},
        {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
    ]
    resp = _create_response(model=OPENAI_MODEL, input=input_messages)
    report_md = _extract_response_text(resp)

    if not report_md:
        raise RuntimeError("OpenAI returned empty response — check API key and model availability")

    # 5. Parse executive summary from the report
    executive_summary = _extract_section(report_md, "Executive Summary")

    # 6. Parse action items into structured JSONB
    action_items = _parse_action_items(report_md)

    # 7. Upsert into reports table
    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO reports (week_ending, period_weeks, executive_summary, full_report_md, action_items, model_used)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (week_ending, period_weeks) DO UPDATE SET
                executive_summary = EXCLUDED.executive_summary,
                full_report_md    = EXCLUDED.full_report_md,
                action_items      = EXCLUDED.action_items,
                generated_at      = NOW(),
                model_used        = EXCLUDED.model_used
        """, (
            week_ending,
            period_weeks,
            executive_summary,
            report_md,
            json.dumps(action_items),
            OPENAI_MODEL,
        ))
    db_conn.commit()

    print(f"  [executive_briefer] Report generated ({len(report_md)} chars, "
          f"{len(action_items)} action items) for {week_ending}")
    return report_md
