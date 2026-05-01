import os
import tempfile

import streamlit as st

# Bridge st.secrets → os.environ. On Streamlit Cloud secrets live in
# st.secrets only; the codebase reads config via os.getenv() everywhere.
# Locally, load_dotenv() in each module already handles .env.
try:
    _secrets_items = list(st.secrets.items())
except Exception:
    _secrets_items = []
for _k, _v in _secrets_items:
    if isinstance(_v, str) and _k not in os.environ:
        os.environ[_k] = _v

# Materialize the service account JSON contents to a file so existing
# from_service_account_file() calls work unchanged on Cloud.
_sa_info = os.getenv("GOOGLE_SERVICE_ACCOUNT_INFO")
if _sa_info and not os.path.exists(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")):
    _sa_path = os.path.join(tempfile.gettempdir(), "google_service_account.json")
    with open(_sa_path, "w", encoding="utf-8") as _f:
        _f.write(_sa_info)
    os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = _sa_path

st.set_page_config(page_title="LBS Intelligence", page_icon="💡", layout="wide")

st.title("Light Bulb Surplus Intelligence")
st.markdown(
    """
**AI-powered ecommerce analytics for Light Bulb Surplus.**

Use the sidebar to navigate:
- **Weekly Report** — Latest executive briefing and historical reports
- **Analytics Assistant** — Ask GA4 questions in plain English
"""
)
