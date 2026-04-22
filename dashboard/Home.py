import os
import tempfile

import streamlit as st

# On Streamlit Cloud the credentials file isn't in the repo (gitignored).
# If GOOGLE_SERVICE_ACCOUNT_INFO is provided as a secret, materialize it to
# disk and point GOOGLE_SERVICE_ACCOUNT_JSON at that path so existing
# from_service_account_file() calls work unchanged.
_sa_info = os.getenv("GOOGLE_SERVICE_ACCOUNT_INFO")
if _sa_info and not os.path.exists(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")):
    _sa_path = os.path.join(tempfile.gettempdir(), "google_service_account.json")
    with open(_sa_path, "w", encoding="utf-8") as _f:
        _f.write(_sa_info)
    os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = _sa_path

st.set_page_config(page_title="LBS Intelligence", page_icon="💡", layout="wide")

st.title("Light Bulb Surplus Intelligence")
st.info("To view this app: copy the URL from the terminal and paste it into Chrome.")
st.markdown(
    """
**AI-powered ecommerce analytics for Light Bulb Surplus.**

Use the sidebar to navigate:
- **Weekly Report** - Latest executive briefing and historical reports
- **Query GA4** - Coming next in the next dashboard spec
"""
)
