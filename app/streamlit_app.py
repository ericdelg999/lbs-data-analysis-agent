"""
LBS Intelligence — Streamlit Dashboard

Pages:
  1. Weekly Report — display the latest generated report, select prior weeks
  2. Product Opportunities — ranked table of high-ATC/low-checkout and high-traffic/low-ATC products
  3. Brand Performance — brand-level metrics table with WoW changes
  4. Funnel — site-wide funnel visualization with WoW comparison
  5. Paid Search — Google Ads performance with impression share breakdown
  6. Search — GSC query performance, branded vs non-branded
  7. AI Referral — AI traffic and revenue trends
  8. Findings Log — raw findings table for inspection (auditing LLM inputs)
"""

import streamlit as st
import os
import psycopg2
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="LBS Intelligence",
    page_icon="💡",
    layout="wide"
)


@st.cache_resource
def get_connection():
    """Cached database connection."""
    # TODO: return psycopg2.connect(os.getenv("DATABASE_URL"))
    pass


@st.cache_data(ttl=3600)
def load_latest_report(_conn):
    """Load the most recent weekly report."""
    # TODO: SELECT * FROM reports ORDER BY week_ending DESC LIMIT 1
    pass


@st.cache_data(ttl=3600)
def load_product_opportunities(_conn, week_ending: str):
    """Load ranked product opportunity list for the week."""
    # TODO: SELECT from metrics_product_weekly WHERE week_ending = ?
    # Return two dataframes: high_atc_low_checkout, high_traffic_low_atc
    pass


@st.cache_data(ttl=3600)
def load_brand_performance(_conn, week_ending: str):
    """Load brand-level metrics for the week."""
    # TODO: SELECT from metrics_brand_weekly WHERE week_ending = ?
    pass


def main():
    st.title("💡 LBS Intelligence")

    conn = get_connection()

    # Week selector
    # TODO: Pull available week_ending values from reports table
    # week_ending = st.selectbox("Week ending", available_weeks)

    page = st.sidebar.radio("View", [
        "Weekly Report",
        "Product Opportunities",
        "Brand Performance",
        "Funnel",
        "Paid Search",
        "Organic Search",
        "AI Referral",
        "Findings Log"
    ])

    if page == "Weekly Report":
        # TODO: load_latest_report(), render markdown with st.markdown()
        st.info("Weekly report will display here once pipeline has run.")

    elif page == "Product Opportunities":
        st.header("Product Opportunities")
        st.subheader("High Add-to-Cart, Low Checkout Conversion")
        st.caption("Strong buying intent — check shipping cost and checkout friction")
        # TODO: load_product_opportunities(), display as st.dataframe() with link to page_url

        st.subheader("High Traffic, Low Add-to-Cart")
        st.caption("Traffic not converting — check listing content and pricing")
        # TODO: same pattern

    elif page == "Brand Performance":
        st.header("Brand Performance")
        # TODO: load_brand_performance(), styled dataframe with WoW delta columns colored

    # TODO: implement remaining pages

    else:
        st.info(f"{page} — coming soon.")


if __name__ == "__main__":
    main()
