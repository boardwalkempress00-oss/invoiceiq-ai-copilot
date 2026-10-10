"""InvoiceIQ Streamlit entry point.

Run locally (with the FastAPI backend running on port 8000):
    uv run streamlit run src/invoiceiq/frontend/app.py
"""

from __future__ import annotations

import streamlit as st

from invoiceiq.frontend import components, state
from invoiceiq.frontend.presentation import ai_mode

st.set_page_config(
    page_title="InvoiceIQ — AI Finance Operations Copilot",
    page_icon=":material/receipt_long:",
    layout="wide",
    initial_sidebar_state="expanded",
)
components.inject_css()

P = components.PAGE_PATHS
navigation = st.navigation(
    {
        "Workspace": [
            st.Page(P["dashboard"], title="Dashboard", icon=":material/dashboard:", default=True),
            st.Page(P["analysis"], title="Invoice Analysis", icon=":material/fact_check:"),
            st.Page(P["chat"], title="AI Chat", icon=":material/forum:"),
        ],
        "About this project": [
            st.Page(P["architecture"], title="Architecture", icon=":material/account_tree:"),
            st.Page(P["observability"], title="Observability", icon=":material/monitoring:"),
        ],
    }
)

with st.sidebar:
    st.markdown("### :material/receipt_long: InvoiceIQ")
    st.caption("AI Finance Operations Copilot")
    mode = ai_mode(state.health())
    st.badge(mode.label, icon=mode.icon, color={
        "success": "green", "info": "blue", "warning": "orange", "error": "red"
    }[mode.tone])  # fmt: skip
    st.caption(
        "Portfolio demo. All invoices, companies, people and policies are **fictional**. "
        "Not accounting, tax or legal advice."
    )

navigation.run()
