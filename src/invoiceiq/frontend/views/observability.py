"""Observability: real measurements recorded by this browser session. Nothing is simulated."""

from __future__ import annotations

import streamlit as st

from invoiceiq.frontend import components, state
from invoiceiq.frontend.presentation import measurement_rows, summarize

st.title("Observability")
st.caption(
    "Measurements recorded by this browser session's calls to the backend. Values that were not "
    "measured are shown as “—”, never estimated."
)

client = state.client()
head = st.columns([3, 1])
with head[1]:
    refresh = st.button("Refresh backend status", icon=":material/refresh:",
                        width="stretch")  # fmt: skip
health = state.health(force=refresh)
with head[0]:
    components.ai_mode_banner(health)

if health:
    ai = health["ai"]
    components.definition_list(
        [
            ("Backend version", health["version"]),
            ("Environment", health["environment"]),
            ("AI provider", ai["provider"]),
            ("Live (paid) model", "Yes" if ai["live"] else "No"),
            ("AI available", "Yes" if ai["available"] else "No"),
            (
                "Daily AI requests remaining",
                ai["daily_requests_remaining"]
                if ai["daily_requests_remaining"] is not None
                else "unknown (limiter unavailable)",
            ),
        ]
    )

measurements = list(client.measurements)
summary = summarize(measurements)


def fmt_ms(value: float | None) -> str:
    return "—" if value is None else f"{value:,.0f} ms"


st.subheader("This session")
row = st.columns(4)
row[0].metric("API requests", summary.requests, border=True)
row[1].metric("Median API round trip", fmt_ms(summary.median_round_trip_ms), border=True,
              help="Measured by the frontend: request sent → response received.")  # fmt: skip
row[2].metric("Median server time", fmt_ms(summary.median_server_ms), border=True,
              help="From the X-Response-Time-Ms header set by the backend.")  # fmt: skip
row[3].metric("Failed requests", summary.failed_requests, border=True)

row = st.columns(4)
row[0].metric("Chat requests", summary.chat_requests, border=True)
row[1].metric("Last AI latency", fmt_ms(summary.last_ai_ms), border=True,
              help="Time spent in the AI provider call, measured by the backend.")  # fmt: skip
tokens = summary.prompt_tokens + summary.completion_tokens
token_note = ""
if "estimated" in summary.token_sources:
    token_note = " (estimated)"
row[2].metric(
    f"Tokens used{token_note}",
    f"{tokens:,}" if summary.chat_requests else "—",
    border=True,
    help="Reported by Azure OpenAI when available. The mock provider estimates ~4 characters "
    "per token and is labelled as an estimate.",
)
row[3].metric(
    "Estimated AI cost",
    f"{summary.estimated_cost:.6f} {summary.cost_currency}"
    if summary.estimated_cost is not None
    else "—",
    border=True,
    help="Tokens × configured model prices (AI_PRICE_*). Shown as — when prices are not "
    "configured. Not an invoice from Azure.",
)

st.subheader("Recent requests")
if measurements:
    st.dataframe(measurement_rows(measurements), hide_index=True, width="stretch")
    st.caption(
        "Correlation IDs are random identifiers that match the backend's structured logs. They "
        "contain no user data. Prompts and questions are never logged."
    )
else:
    st.info("No requests recorded yet. Open an invoice or ask a question to collect "
            "measurements.", icon=":material/hourglass_empty:")  # fmt: skip

with st.expander("What is logged on the server", icon=":material/description:"):
    st.markdown(
        "The backend writes one JSON log line per request (method, path, status, duration, "
        "correlation ID) and one per chat answer (provider, token counts, AI latency, number of "
        "citations). Secrets, prompts, questions and stack traces are never logged or returned. "
        "Shipping these logs to Azure Monitor / Log Analytics is part of the deployment phase."
    )
