"""Start here: what InvoiceIQ is, what a visitor can try, and what is real versus simulated."""

from __future__ import annotations

import streamlit as st

from invoiceiq.frontend import components, state

features = state.health() or {}
demo = bool(features.get("demo_mode"))

components.hero(
    "InvoiceIQ — AI Finance Operations Copilot",
    "An assistant for accounts payable that never does the maths. Python checks every invoice "
    "with exact arithmetic against a purchasing policy; an AI model only explains the verified "
    "results and cites its evidence.",
    tag="Portfolio project · all invoices, companies and people are fictional",
)

if st.button("Try InvoiceIQ", type="primary", icon=":material/play_arrow:", key="try_main"):
    components.go("demo" if demo else "analysis")
st.caption(
    "Run a processing scenario in seconds — no sign-up, nothing to upload."
    if demo
    else "Start by inspecting a fictional invoice and its validation findings."
)

st.subheader("Try it in 60 seconds")
cards = st.columns(3)
with cards[0], st.container(border=True):
    st.markdown("**1 · Process an invoice**")
    st.markdown(
        "Pick one of ten fictional scenarios — wrong VAT, missing approval, foreign currency — "
        "and watch it go through the processing pipeline."
        if demo
        else "The asynchronous processing demo is not enabled on this deployment."
    )
    if demo and st.button("Open the demo", key="go_demo"):
        components.go("demo")
with cards[1], st.container(border=True):
    st.markdown("**2 · Inspect a validation**")
    st.markdown(
        "See line items, stated versus recalculated totals, and every finding with the policy "
        "section behind it."
    )
    if st.button("Open Invoice Analysis", key="go_analysis"):
        components.go("analysis")
with cards[2], st.container(border=True):
    st.markdown("**3 · Ask the copilot**")
    st.markdown(
        "Ask why an invoice was flagged. Answers cite their evidence; this public app uses the "
        "offline mock model, so it costs nothing."
    )
    if st.button("Open AI Chat", key="go_chat"):
        components.go("chat")

st.subheader("What is real and what is simulated")
real = st.columns(3)
with real[0], st.container(border=True):
    st.badge("Verified in Azure", icon=":material/verified:", color="green")
    st.markdown(
        "- Event-driven processing: Blob Storage → Event Grid → Service Bus → a worker that "
        "starts from zero — tested end to end with six submissions.\n"
        "- Live Azure OpenAI answers, in a short controlled test.\n"
        "- Managed identities, least-privilege access, scale to zero."
    )
with real[1], st.container(border=True):
    st.badge("In this public app", icon=":material/public:", color="blue")
    st.markdown(
        "- Validation and policy citations: real.\n"
        "- AI answers: offline mock model (no cost, no live AI).\n"
        + (
            "- Processing demo: **simulated** in this container with the real code — no Azure "
            "services are used."
            if demo
            else "- Processing demo: not enabled."
        )
    )
with real[2], st.container(border=True):
    st.badge("Planned", icon=":material/schedule:", color="gray")
    st.markdown(
        "- Uploading your own invoices (kept off in public).\n"
        "- Azure AI Search for larger policy sets.\n"
        "- Authentication for a non-demo deployment."
    )

with st.container(border=True):
    st.markdown("**Why the first page can take about half a minute**")
    st.markdown(
        "InvoiceIQ scales to zero when nobody is using it, so it costs nothing while idle. The "
        "first visit after a quiet period starts a fresh container (measured: about 28 seconds "
        "on average). In the event-driven pipeline, the worker also starts from zero — measured "
        "at 24–36 seconds from upload to result."
    )

st.subheader("Built with")
st.markdown(
    "Python 3.12 · FastAPI · Streamlit · Pydantic · exact `Decimal` arithmetic · BM25 retrieval · "
    "Azure OpenAI · Azure Container Apps and Jobs · Blob Storage · Event Grid · Service Bus · "
    "Table Storage · managed identities · Terraform · GitHub Actions"
)
if st.button("See the architecture and evidence", icon=":material/account_tree:", key="go_arch"):
    components.go("architecture")
