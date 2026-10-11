"""Process Invoice: submit a fictional invoice and watch it being processed asynchronously.

The browser never waits for validation. The API stores the invoice and returns at once
(Pending); Event Grid announces the new blob, Service Bus queues the event, and a worker job
validates it. This page checks the status every few seconds — only for the submission being
watched, and only until it reaches a final state or three minutes have passed.
"""

from __future__ import annotations

import time

import streamlit as st

from invoiceiq.frontend import components, state
from invoiceiq.frontend.api_client import ApiError
from invoiceiq.frontend.presentation import (
    group_findings,
    money,
    severity_style,
    submission_badge,
    timeline_rows,
)

MAX_UPLOAD_BYTES = 64 * 1024
WATCH_LIMIT_SECONDS = 180
ACTIVE, WATCH, STARTED, POLL = (
    "submission_active",
    "submission_watch",
    "submission_started",
    "submission_poll_s",
)

components.hero(
    "Process an invoice",
    "Submit a fictional JSON invoice. It is stored in private Blob Storage, announced by Event "
    "Grid, queued in Service Bus and validated by a background worker — the page follows along.",
    tag="Event-driven · asynchronous · no AI involved",
)

if not (state.health() or {}).get("async_processing"):
    st.info("Invoice processing is not enabled on this deployment.", icon=":material/info:")
    st.stop()

client = state.client()

# --- Submit --------------------------------------------------------------------------------------
left, right = st.columns([3, 2], gap="large")
with left:
    st.markdown("**1 · Choose an invoice**")
    mode = st.radio(
        "Invoice source",
        ["Sample invoice", "Upload JSON"],
        horizontal=True,
        label_visibility="collapsed",
    )
    sample_id = content = None
    problem = None
    if mode == "Sample invoice":
        try:
            invoices = client.list_invoices()
        except ApiError as error:
            components.backend_error(error)
            st.stop()
        labels = {i["invoice_id"]: f"{i['invoice_number']} · {i['vendor_name']}" for i in invoices}
        sample_id = st.selectbox("Sample invoice", list(labels), format_func=labels.__getitem__)
    else:
        upload = st.file_uploader("Fictional invoice JSON (max 64 KB)", type=["json"])
        if upload is not None:
            data = upload.getvalue()
            if len(data) > MAX_UPLOAD_BYTES:
                problem = "The file is larger than 64 KB."
            else:
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError:
                    problem = "The file is not UTF-8 text."
        if problem:
            st.error(problem, icon=":material/error:")
    st.caption(
        "Use fictional data only — never real invoices or personal data. Files are deleted after "
        "7 days. Uploads have their own daily allowance, separate from AI questions."
    )
    ready = sample_id is not None or (content is not None and problem is None)
    if st.button("Process invoice", type="primary", icon=":material/send:", disabled=not ready):
        try:
            submission = client.submit_invoice(
                session_id=state.session_id(), sample_invoice_id=sample_id, content=content
            )
        except ApiError as error:
            st.error(error.message, icon=":material/error:")
        else:
            st.session_state[ACTIVE] = submission["submission_id"]
            st.session_state[WATCH] = True
            st.session_state[STARTED] = time.monotonic()
            st.session_state[POLL] = (submission.get("poll_after_ms") or 2000) / 1000

with right:
    st.markdown("**How it works**")
    st.markdown(
        "1. **FastAPI** records the submission as *Pending* and uploads the file to a private "
        "**Blob Storage** container.\n"
        "2. **Event Grid** emits `BlobCreated` and delivers it to a **Service Bus** queue.\n"
        "3. A **Container Apps Job** starts from zero, claims the submission and runs the "
        "deterministic validation engine.\n"
        "4. The result is written to **Table Storage**; this page picks it up."
    )
    st.caption(
        "In Azure, the worker starts only when a message is waiting, so the first result takes "
        "about 20–40 seconds. Duplicate events and retries are handled safely."
    )


# --- Status --------------------------------------------------------------------------------------
def _render_result(sub: dict) -> None:
    invoice = sub.get("invoice") or {}
    result = sub["result"]
    head = st.columns([2, 1, 1, 1])
    with head[0]:
        components.status_badge(result["status"])
        st.markdown(f"#### {invoice.get('invoice_number') or sub.get('invoice_id')}")
        st.caption(invoice.get("vendor") or "")
    with head[1]:
        components.amount_block("Stated total", invoice.get("total"), invoice.get("currency"))
    with head[2]:
        components.amount_block(
            "Calculated total", result["calculated"].get("total"), invoice.get("currency")
        )
    with head[3]:
        st.markdown('<div class="iq-eyebrow">Findings</div>', unsafe_allow_html=True)
        st.markdown(f"### {len(result['findings'])}")
    components.definition_list(
        [
            ("Invoice ID", invoice.get("invoice_id")),
            ("Buyer", invoice.get("buyer")),
            ("Issue date", invoice.get("issue_date")),
            ("Purchase order", invoice.get("purchase_order")),
            ("Line items", invoice.get("line_items")),
            ("VAT stated", money(invoice.get("vat_amount"), invoice.get("currency"))),
            ("Required approval", result.get("required_approval_role") or "None"),
            ("Engine", result.get("engine_version")),
        ]
    )
    groups = group_findings(result["findings"])
    if not groups:
        st.success("All validation checks passed.", icon=":material/verified:")
    for group in groups:
        with st.expander(f"{group.title} — {len(group.findings)} finding(s)", icon=group.icon):
            for finding in group.findings:
                fs = severity_style(finding["severity"])
                with st.container(border=True):
                    top = st.columns([3, 1])
                    top[0].markdown(f"**{finding['title']}** · `{finding['finding_id']}`")
                    with top[1]:
                        st.badge(fs.label, icon=fs.icon, color=fs.color)  # type: ignore[arg-type]
                    st.markdown(finding["description"])
                    cols = st.columns(3)
                    cols[0].markdown(f"**Expected**  \n{finding.get('expected_value') or '—'}")
                    cols[1].markdown(f"**Actual**  \n{finding.get('actual_value') or '—'}")
                    cols[2].markdown(f"**Policy**  \n`{finding['policy_reference']}`")
                    st.markdown(f":material/task_alt: **Recommended action:** "
                                f"{finding['recommended_action']}")  # fmt: skip


def _render(sub: dict) -> None:
    label, color, icon = submission_badge(sub["display_status"])
    st.badge(label, icon=icon, color=color)  # type: ignore[arg-type]
    st.dataframe(timeline_rows(sub), hide_index=True, width="stretch")
    attempts = f" · attempt {sub['attempts']}" if sub["attempts"] > 1 else ""
    st.caption(f"Submission `{sub['submission_id']}` · correlation ID `{sub['correlation_id']}`"
               f"{attempts}")  # fmt: skip
    error = sub.get("error")
    status = sub["display_status"]
    if status == "Completed" and sub.get("result"):
        _render_result(sub)
    elif status in ("Failed", "DeadLettered") and error:
        st.error(error["message"], icon=":material/error:")
        for detail in error.get("details", []):
            st.markdown(f"- `{detail}`")
    elif status == "Stalled":
        st.warning(
            "This submission stopped making progress. It may have been moved to the dead-letter "
            "queue for inspection. Please submit the invoice again.",
            icon=":material/hourglass_disabled:",
        )
    elif error:  # a retry is pending
        st.info(error["message"], icon=":material/autorenew:")


active = st.session_state.get(ACTIVE)
if active:
    st.divider()
    st.markdown("**2 · Processing status**")
    watching = bool(st.session_state.get(WATCH))

    @st.fragment(run_every=st.session_state.get(POLL, 2.0) if watching else None)
    def status_panel() -> None:
        try:
            sub = client.get_submission(active)
        except ApiError as error:
            components.backend_error(error)
            return
        _render(sub)
        elapsed = time.monotonic() - st.session_state.get(STARTED, time.monotonic())
        done = sub["terminal"] or sub["display_status"] == "Stalled"
        if st.session_state.get(WATCH) and (done or elapsed > WATCH_LIMIT_SECONDS):
            st.session_state[WATCH] = False
            st.rerun()  # redraw without automatic refresh
        poll = (sub.get("poll_after_ms") or 0) / 1000
        if st.session_state.get(WATCH) and poll and poll != st.session_state.get(POLL):
            st.session_state[POLL] = poll
            st.rerun()  # apply the slower refresh interval suggested by the API
        if not st.session_state.get(WATCH) and not done:
            st.button("Check again", icon=":material/refresh:")

    status_panel()

# --- Recent ------------------------------------------------------------------------------------
try:
    recent = client.list_submissions(state.session_id(), limit=5)
except ApiError:
    recent = []
if recent:
    with st.expander("Your recent submissions", icon=":material/history:"):
        st.dataframe(
            [
                {
                    "Submitted (UTC)": r["created_at"][11:19],
                    "Invoice": r.get("invoice_number") or r.get("sample_invoice_id") or "upload",
                    "Status": r["display_status"],
                    "Attempts": r["attempts"],
                    "Submission": r["submission_id"][:8],
                }
                for r in recent
            ],
            hide_index=True,
            width="stretch",
        )
