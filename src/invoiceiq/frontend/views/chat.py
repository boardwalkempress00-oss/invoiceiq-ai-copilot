"""AI Chat: questions about the selected invoice, with evidence, citations and AI-mode labels."""

from __future__ import annotations

import os
from typing import Any

import streamlit as st

from invoiceiq.frontend import components, state
from invoiceiq.frontend.api_client import ApiError
from invoiceiq.frontend.presentation import (
    READ_ONLY_REASON_TEXT,
    SUGGESTED_QUESTIONS,
    answer_source_label,
    history_payload,
    spend_label,
    token_note,
)

MAX_QUESTION_CHARS = int(os.getenv("CHAT_MAX_QUESTION_CHARS", "500"))


def render_assistant(message: dict[str, Any], policy: dict[str, dict[str, Any]]) -> None:
    if message.get("error"):
        st.error(message["content"], icon=":material/error:")
        if message.get("correlation_id"):
            st.caption(f"Reference: `{message['correlation_id']}`")
        return
    response = message["response"]
    label, color = answer_source_label(response)
    st.badge(label, icon=":material/smart_toy:" if response["mode"] == "ai" else ":material/lock:",
             color=color)  # type: ignore[arg-type]  # fmt: skip
    if response.get("notice"):
        st.warning(response["notice"], icon=":material/info:")
    st.markdown(response["answer"])

    citations = response.get("citations", [])
    with st.expander(f"Evidence cited ({len(citations)})", icon=":material/link:"):
        if not citations:
            st.caption("No evidence was cited.")
        for citation in citations:
            st.markdown(f"**`{citation['evidence_id']}`** · {citation['title']} "
                        f"· _{citation['kind']}_")  # fmt: skip
            section = policy.get(citation["evidence_id"])
            if section:
                st.html(f'<div class="iq-quote">{components.esc(section["text"])}</div>')
        if not response.get("policy_evidence_found", True):
            st.caption("No purchasing-policy passage matched this question.")
        if response.get("removed_citations"):
            st.caption("Removed unverifiable citations: "
                       + ", ".join(response["removed_citations"]))  # fmt: skip

    with st.expander("Verified facts · validation engine", icon=":material/verified:"):
        st.caption("Produced by deterministic Python validation, not by the AI model.")
        st.dataframe(
            [
                {"Fact": f["label"], "Value": f["value"], "Source": f["source_id"]}
                for f in response["verified_facts"]
            ],
            hide_index=True,
            width="stretch",
        )

    parts = [f"Round trip {message['round_trip_ms']:.0f} ms"]
    if response["timings"].get("ai_ms") is not None:
        parts.append(f"AI {response['timings']['ai_ms']:.0f} ms")
    if response.get("usage"):
        usage = response["usage"]
        parts.append(f"{usage['total_tokens']} tokens{token_note([usage['source']])}")
    if response.get("cost_is_simulated") or response.get("estimated_cost") is not None:
        spend = spend_label(
            response.get("estimated_cost"),
            response.get("cost_currency"),
            bool(response.get("cost_is_simulated")),
        )
        parts.append(f"AI spend {spend}")
    parts.append(f"ID `{response['correlation_id']}`")
    st.caption(" · ".join(parts))


st.title("AI Chat")
client = state.client()
health = state.health()
components.ai_mode_banner(health)

try:
    invoices = client.list_invoices()
    policy = client.policy_sections()
except ApiError as error:
    components.backend_error(error)
    st.stop()

top = st.columns([3, 1])
with top[0]:
    invoice_id = components.invoice_selector(invoices, key="invoice_picker")
messages = state.chat_messages(invoice_id)
with top[1]:
    st.write("")
    if st.button("Clear conversation", icon=":material/delete_sweep:", disabled=not messages,
                 width="stretch"):  # fmt: skip
        state.clear_chat(invoice_id)
        st.rerun()

st.caption(
    "Answers explain the validation results and cite invoice evidence or policy sections. "
    "Facts come from the validation engine; recommendations are AI-generated and must be "
    "reviewed by a person."
)

st.markdown("**Suggested questions**")
pending: str | None = None
suggestion_cols = st.columns(3)
for index, suggestion in enumerate(SUGGESTED_QUESTIONS):
    if suggestion_cols[index % 3].button(suggestion, key=f"suggest-{index}", width="stretch"):
        pending = suggestion

for message in messages:
    with st.chat_message(message["role"]):
        if message["role"] == "user":
            st.markdown(message["content"])
        else:
            render_assistant(message, policy)

typed = st.chat_input("Ask about this invoice…", max_chars=MAX_QUESTION_CHARS, key="chat_input")
question = typed or pending
if question:
    history = history_payload(messages)
    messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        try:
            with st.spinner("Checking the evidence…"):
                response = client.chat(
                    invoice_id=invoice_id,
                    question=question,
                    session_id=state.session_id(),
                    history=history,
                )
            reply = {
                "role": "assistant",
                "content": response["answer"],
                "response": response,
                "round_trip_ms": client.measurements[-1].round_trip_ms,
            }
        except ApiError as error:
            reply = {
                "role": "assistant",
                "content": f"Sorry, the question could not be answered. {error.message}",
                "error": True,
                "correlation_id": error.correlation_id,
            }
        messages.append(reply)
    # Rerun so the whole page (including the Clear button) reflects the updated history.
    st.rerun()

last = next((m["response"] for m in reversed(messages) if m.get("response")), None)
if last:
    remaining = last["limits"].get("session_remaining")
    if remaining is not None:
        st.caption(f"AI questions left in this session today: {remaining}")
    if last.get("read_only_reason"):
        st.caption(READ_ONLY_REASON_TEXT.get(last["read_only_reason"], ""))
