"""InvoiceIQ Streamlit entry point.

Run locally (with the FastAPI backend running on port 8000):
    uv run streamlit run src/invoiceiq/frontend/app.py
"""

from __future__ import annotations

import streamlit as st

from invoiceiq.frontend import components, idle_pause, state
from invoiceiq.frontend.api_client import ApiError
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
    # Idle pause + per-tab chat history persistence (browser-side; see idle_pause.py).
    restored = idle_pause.mount(
        st.session_state.get("chats", {}), state.selected_invoice(), st.session_state
    )

if restored:
    try:
        valid_ids = {inv["invoice_id"] for inv in state.client().list_invoices()}
    except ApiError:
        valid_ids = set()
    result = idle_pause.restore_history(restored, valid_ids)
    if result is not None:
        chats, selected = result
        current = st.session_state.setdefault("chats", {})
        fresh_session = not any(current.values())
        for invoice_id, messages in chats.items():
            if not current.get(invoice_id):  # fill empty conversations; never overwrite new ones
                current[invoice_id] = messages
        if selected and fresh_session:
            # Return to the invoice that was open (the picker re-creates itself, see components).
            state.select_invoice(selected)
            generation = st.session_state.get(components.PICKER_GENERATION_KEY, 0) + 1
            st.session_state[components.PICKER_GENERATION_KEY] = generation

navigation.run()
