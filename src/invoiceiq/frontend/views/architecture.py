"""Architecture: what is deployed, how it was verified, and what is still planned.

Every status says where the evidence comes from — verified in Azure, verified locally by
automated tests, or planned. Measured numbers are copied from docs/evidence, never estimated.
"""

from __future__ import annotations

import streamlit as st

EVIDENCE_DOC = "docs/evidence/production-verification.md"

st.title("Architecture")
st.markdown(
    "InvoiceIQ separates **deterministic finance logic** from **language generation**. Python "
    "decides whether an invoice is correct; the AI model only explains verified results. "
    "Invoices can also be processed **asynchronously** through an event-driven pipeline on "
    "Azure — built, deployed and verified, but switched **off** for public visitors."
)

legend = st.columns(3)
legend[0].badge("Verified in Azure", icon=":material/verified:", color="green")
legend[1].badge("Verified locally (automated tests)", icon=":material/science:", color="blue")
legend[2].badge("Planned (not built yet)", icon=":material/schedule:", color="gray")

DIAGRAM = """
digraph G {
  rankdir=LR; bgcolor="transparent"; nodesep=0.3; ranksep=0.5;
  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=10,
        color="#1f5f99", fillcolor="#eef4fb", fontcolor="#1b2430", penwidth=1.2];
  edge [color="#5b6676", fontname="Helvetica", fontsize=8, fontcolor="#5b6676"];

  user [label="Browser", shape=oval, fillcolor="#ffffff"];
  subgraph cluster_app {
    label="Container App · 0–1 replicas · scale to zero"; fontname="Helvetica"; fontsize=10;
    style="rounded"; color="#0ca30c"; fontcolor="#0b6b0b";
    ui  [label="Streamlit UI\\n(public HTTPS)"];
    api [label="FastAPI\\n(localhost only)"];
    val [label="Validation engine\\nPython Decimal"];
    rag [label="BM25 policy retrieval"];
    lim [label="Usage limiter"];
  }
  aoai [label="Azure OpenAI\\n(mock in production;\\nlive in controlled tests)",
        fillcolor="#f3eefb", color="#6b4fa0"];
  usage [label="Table aiusage\\n(daily AI quotas)", shape=cylinder];

  subgraph cluster_ep {
    label="Event-driven processing · deployed, off by default"; fontname="Helvetica";
    fontsize=10; style="rounded"; color="#0ca30c"; fontcolor="#0b6b0b";
    blob [label="Blob container\\ninvoice-uploads (private)", shape=cylinder];
    eg   [label="Event Grid\\nBlobCreated"];
    sb   [label="Service Bus Basic\\nqueue + dead-letter"];
    job  [label="Container Apps Job\\nworker (KEDA, 0–3)"];
    jobs [label="Table invoicejobs\\n(status + results)", shape=cylinder];
    dl   [label="Dead-letter storage\\naccount (dedicated)", shape=cylinder];
  }
  srch [label="Azure AI Search", style="rounded,dashed", fillcolor="#ffffff", color="#9aa3ae",
        fontcolor="#5b6676"];

  user -> ui [label="HTTPS"];
  ui -> api [label="HTTP (internal)"];
  api -> val; api -> rag; api -> lim;
  api -> aoai [label="managed identity"];
  lim -> usage [label="ETags"];
  api -> blob [label="upload"]; api -> jobs [label="Pending"];
  blob -> eg -> sb [label="managed identity"];
  eg -> dl [style=dashed, label="undeliverable"];
  sb -> job [label="KEDA"];
  job -> blob [label="read"]; job -> jobs [label="result"];
  rag -> srch [style=dashed, color="#9aa3ae", label="replaceable"];
}
"""
st.graphviz_chart(DIAGRAM, width="stretch")

st.subheader("Components and evidence")
st.dataframe(
    [
        {"Component": "Streamlit frontend", "Status": "✓ Verified in Azure",
         "Evidence": "Production app; five-page smoke tests after every change."},
        {"Component": "FastAPI backend", "Status": "✓ Verified in Azure",
         "Evidence": "Runs inside the same container, reachable only on localhost."},
        {"Component": "Validation engine", "Status": "✓ Verified in Azure",
         "Evidence": "Results in Azure matched local expectations for all six test invoices."},
        {"Component": "BM25 policy retrieval", "Status": "✓ Verified in Azure",
         "Evidence": "Citations such as PP-3.1 in live answers and in mock mode."},
        {"Component": "Mock AI provider", "Status": "✓ Verified in Azure",
         "Evidence": "Production default; AI spend shown as exactly 0."},
        {"Component": "Azure OpenAI provider", "Status": "✓ Verified in Azure (controlled)",
         "Evidence": "5 live requests in an IP-restricted test window; production stays in mock "
                     "mode."},
        {"Component": "Usage limiter on Table Storage", "Status": "✓ Verified in Azure",
         "Evidence": "Daily counters decrement per question; session cap enforced."},
        {"Component": "Blob upload · Event Grid · Service Bus Basic",
         "Status": "✓ Verified in Azure",
         "Evidence": "6 test submissions delivered once each; switched off afterwards."},
        {"Component": "Background worker (Container Apps Job)", "Status": "✓ Verified in Azure",
         "Evidence": "Scaled from zero by KEDA; at most 3 concurrent executions."},
        {"Component": "Retries, lease takeover, dead-lettering",
         "Status": "✓ Verified locally (automated tests)",
         "Evidence": "Deterministic tests with Service Bus semantics; not exercised in Azure."},
        {"Component": "Public demo of invoice processing",
         "Status": "✓ Verified locally (automated tests)",
         "Evidence": "Simulated scenario gallery with in-memory stand-ins, labelled as simulated; "
                     "uses no Azure storage or messaging. Not yet deployed."},
        {"Component": "Azure AI Search", "Status": "○ Planned",
         "Evidence": "Could replace BM25 behind the existing PolicyRetriever interface."},
    ],
    hide_index=True,
    width="stretch",
)  # fmt: skip

st.subheader("Measured in Azure")
st.dataframe(
    [
        {"Measurement": "Upload → result (event-driven, worker from zero)",
         "Result": "24.5 – 35.7 s", "Source": "6 test submissions (E1, E2)"},
        {"Measurement": "Upload → message queued in Service Bus", "Result": "≈ 0.8 s",
         "Source": "E1 status timeline"},
        {"Measurement": "Worker validation time", "Result": "< 0.1 s",
         "Source": "Worker logs"},
        {"Measurement": "Web app wake-up from zero (Resume)", "Result": "average 28 s",
         "Source": "3 cold starts after probe tuning"},
        {"Measurement": "Duplicates, retries, dead-letters in tests", "Result": "0 / 0 / 0",
         "Source": "Service Bus and Event Grid metrics, worker logs"},
        {"Measurement": "Azure OpenAI requests during processing tests", "Result": "0",
         "Source": "Azure OpenAI metrics"},
    ],
    hide_index=True,
    width="stretch",
)  # fmt: skip
st.caption(
    "Most of the processing time is the worker starting from zero (image pull and container "
    "creation); validation itself is instant. That is the price of paying nothing while idle. "
    f"Full evidence: `{EVIDENCE_DOC}`."
)

left, right = st.columns(2)
with left, st.container(border=True):
    st.markdown("**How a chat question is answered**")
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
    st.markdown("**How an invoice is processed asynchronously**")
    st.markdown(
        "1. The API records the submission as *Pending*, then stores the file in a private "
        "Blob container.\n"
        "2. Event Grid publishes `BlobCreated` to a Service Bus Basic queue.\n"
        "3. KEDA sees the message and starts a Container Apps Job from zero.\n"
        "4. The worker claims the submission (lease + ETag), validates it with the same engine "
        "and stores the result *before* completing the message.\n"
        "5. Duplicates are recognised; failures retry, then dead-letter instead of being lost."
    )

st.subheader("Security boundaries")
st.dataframe(
    [
        {"Identity": "Web app", "Can access": "Azure OpenAI (inference), table aiusage, "
         "table invoicejobs, container invoice-uploads (read/write)"},
        {"Identity": "Worker", "Can access": "Container invoice-uploads (read), table "
         "invoicejobs, Service Bus queue (receive) — no Azure OpenAI"},
        {"Identity": "Scaler (KEDA)", "Can access": "Service Bus queue metrics only; not usable "
         "by container code"},
        {"Identity": "Event Grid topic", "Can access": "Service Bus queue (send), dedicated "
         "dead-letter storage account; plus an unused legacy role on one container in the main "
         "account, pending clean-up"},
    ],
    hide_index=True,
    width="stretch",
)  # fmt: skip
st.caption(
    "All access uses managed identities and role assignments scoped to a single resource. "
    "Storage shared keys, Service Bus keys and Azure OpenAI keys are disabled."
)

st.subheader("Design principles and trade-offs")
st.markdown(
    "- **The AI never does financial arithmetic.** It cannot change an amount or a status.\n"
    '- **Grounded or nothing.** Missing evidence produces an explicit "does not answer" reply.\n'
    "- **Fail closed on cost.** If usage cannot be recorded, no AI call is made.\n"
    "- **Pay nothing while idle.** Web app and worker scale to zero; the cost is a cold start "
    "of roughly half a minute.\n"
    "- **At-least-once delivery, exactly-once results.** Idempotent processing instead of "
    "paid duplicate detection (Service Bus Basic has no fixed monthly charge).\n"
    "- **Replaceable edges.** Repository, retriever, AI provider, stores and queue are "
    "interfaces."
)
