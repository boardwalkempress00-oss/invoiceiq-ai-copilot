"""Architecture: what is implemented in Milestone 1 and what is planned for Milestone 2."""

from __future__ import annotations

import streamlit as st

st.title("Architecture")
st.markdown(
    "InvoiceIQ separates **deterministic finance logic** from **language generation**. Python "
    "decides whether an invoice is correct; the AI model only explains verified results."
)

legend = st.columns(2)
legend[0].badge("Implemented · Milestone 1", icon=":material/check_circle:", color="green")
legend[1].badge("Planned · Milestone 2 (not built yet)", icon=":material/schedule:", color="gray")

DIAGRAM = """
digraph G {
  rankdir=LR; bgcolor="transparent"; nodesep=0.35; ranksep=0.55;
  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11,
        color="#1f5f99", fillcolor="#eef4fb", fontcolor="#1b2430", penwidth=1.2];
  edge [color="#5b6676", fontname="Helvetica", fontsize=9, fontcolor="#5b6676"];

  user [label="Browser\\n(recruiter)", shape=oval, fillcolor="#ffffff"];
  subgraph cluster_m1 {
    label="Implemented — one container (Milestone 1)"; fontname="Helvetica"; fontsize=11;
    style="rounded"; color="#0ca30c"; fontcolor="#0b6b0b";
    ui   [label="Streamlit UI\\n(public, port 8501)"];
    api  [label="FastAPI backend\\n(localhost only)"];
    val  [label="Validation engine\\nPython Decimal"];
    rag  [label="BM25 policy\\nretrieval"];
    lim  [label="Usage limiter\\nsession + daily caps"];
    data [label="Fictional invoices\\n+ policy (files)", shape=folder];
  }
  aoai [label="Azure OpenAI\\n(or offline mock)", fillcolor="#f3eefb", color="#6b4fa0"];

  subgraph cluster_m2 {
    label="Planned — Milestone 2 (not implemented)"; fontname="Helvetica"; fontsize=11;
    style="rounded,dashed"; color="#9aa3ae"; fontcolor="#5b6676";
    node [style="rounded,dashed", fillcolor="#ffffff", color="#9aa3ae", fontcolor="#5b6676"];
    blob [label="Blob Storage\\ninvoice upload"];
    eg   [label="Event Grid"];
    sb   [label="Service Bus queue\\n+ dead-letter"];
    wrk  [label="Background worker"];
    tbl  [label="Table Storage\\njobs + usage quota"];
    srch [label="Azure AI Search"];
  }

  user -> ui [label="HTTPS"];
  ui -> api [label="HTTP (internal)"];
  api -> val; api -> rag; api -> lim; val -> data; rag -> data;
  api -> aoai;
  blob -> eg -> sb -> wrk [style=dashed, color="#9aa3ae"];
  wrk -> val [style=dashed, color="#9aa3ae", label="same engine"];
  wrk -> tbl [style=dashed, color="#9aa3ae"];
  lim -> tbl [style=dashed, color="#9aa3ae", label="production limiter"];
  rag -> srch [style=dashed, color="#9aa3ae", label="replaceable"];
}
"""
st.graphviz_chart(DIAGRAM, width="stretch")

st.subheader("Components")
st.dataframe(
    [
        {"Component": "Streamlit frontend", "Status": "✓ Implemented",
         "Role": "Public UI. Calls the API only; contains no validation or AI logic."},
        {"Component": "FastAPI backend", "Status": "✓ Implemented",
         "Role": "Typed REST API (Pydantic). Thin routes delegate to services."},
        {"Component": "Validation engine", "Status": "✓ Implemented",
         "Role": "Pure Python Decimal, round-half-up: arithmetic, VAT, approvals, fields."},
        {"Component": "BM25 policy retrieval", "Status": "✓ Implemented",
         "Role": "Keyword ranking over numbered policy sections with stable citation IDs."},
        {"Component": "Azure OpenAI provider", "Status": "✓ Implemented (not yet deployed)",
         "Role": "Official OpenAI SDK, v1 endpoint, Entra ID auth, timeouts, bounded retries."},
        {"Component": "Mock AI provider", "Status": "✓ Implemented",
         "Role": "Deterministic offline answers from the same evidence. No cost."},
        {"Component": "Usage limiter", "Status": "✓ Implemented (local stores)",
         "Role": "Per-session and daily caps, fail-closed. Memory and SQLite stores."},
        {"Component": "Azure Table Storage limiter", "Status": "○ Planned",
         "Role": "Persistent, ETag-protected quota store required before live AI goes public."},
        {"Component": "Blob Storage · Event Grid · Service Bus", "Status": "○ Planned (M2)",
         "Role": "Event-driven invoice ingestion with retries and dead-lettering."},
        {"Component": "Background worker", "Status": "○ Planned (M2)",
         "Role": "Calls the same InvoiceService.process() used by the API today."},
        {"Component": "Azure AI Search", "Status": "○ Planned (M2)",
         "Role": "Could replace BM25 behind the existing PolicyRetriever interface."},
    ],
    hide_index=True,
    width="stretch",
)  # fmt: skip

left, right = st.columns(2)
with left, st.container(border=True):
    st.markdown("**How a chat question is answered (implemented)**")
    st.markdown(
        "1. Streamlit sends the question and invoice ID to `POST /chat`.\n"
        "2. The backend loads the invoice and runs the validation engine.\n"
        "3. Policy sections cited by findings, plus BM25 matches, become evidence.\n"
        "4. Session and daily allowances are checked (fail-closed).\n"
        "5. The model receives instructions, evidence and the question as separate messages.\n"
        "6. Citations not present in the evidence are removed from the answer.\n"
        "7. Verified facts are attached by Python — never by the model."
    )
with right, st.container(border=True):
    st.markdown("**Synchronous today, event-driven next**")
    st.markdown(
        "**Milestone 1 (now):** the user waits while the API validates and answers in a single "
        "HTTP request. Simple and cheap — ideal for three bundled sample invoices.\n\n"
        "**Milestone 2 (planned):** uploaded invoices land in Blob Storage; Event Grid "
        "publishes an event; a Service Bus queue buffers work; a worker validates each invoice "
        "and records the job status. Failures retry and then dead-letter instead of being lost. "
        "The validation engine itself does not change."
    )

st.subheader("Design principles")
st.markdown(
    "- **The AI never does financial arithmetic.** It cannot change an amount or a status.\n"
    '- **Grounded or nothing.** Missing evidence produces an explicit "does not answer" reply.\n'
    "- **Fail closed on cost.** If usage cannot be recorded, no AI call is made.\n"
    "- **Replaceable edges.** Repository, retriever, AI provider and usage store are interfaces."
)
