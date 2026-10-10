"""Observability: real measurements recorded by this browser session. Nothing is simulated."""

from __future__ import annotations

import streamlit as st

from invoiceiq.frontend import components, state
from invoiceiq.frontend.presentation import measurement_rows, summarize, token_note

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
row[2].metric(
    f"Tokens used{token_note(summary.token_sources)}",
    f"{tokens:,}" if summary.chat_requests else "—",
    border=True,
    help="Reported by Azure OpenAI for live answers. In mock mode the counts are simulated "
    "(~4 characters per token) to show the pipeline — no model is called.",
)
all_simulated = (
    summary.chat_requests > 0 and summary.simulated_chat_requests == summary.chat_requests
)
if all_simulated:
    spend_value = f"0 {summary.cost_currency or 'USD'}"
elif summary.estimated_cost is not None:
    spend_value = f"{summary.estimated_cost:.6f} {summary.cost_currency}"
else:
    spend_value = "—"
row[3].metric(
    "AI spend (mock: no model calls)" if all_simulated else "Estimated AI cost",
    spend_value,
    border=True,
    help="Live answers: reported tokens × configured model prices (AI_PRICE_*), an estimate, "
    "not an Azure invoice. Mock answers call no model, so their spend is exactly 0.",
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
