"""Try processing: run a predefined fictional scenario through the processing pipeline (simulated).

The result is produced by the real validation and processing code, but Blob Storage, Event Grid,
Service Bus and Table Storage are in-memory stand-ins. The page says so prominently and shows a
historical, verified Azure run separately — the two are never mixed.
"""

from __future__ import annotations

import secrets

import streamlit as st

from invoiceiq.frontend import components, state
from invoiceiq.frontend.api_client import ApiError
from invoiceiq.frontend.presentation import group_findings, money, severity_style

LAST = "demo_last_run"
SCROLL = "demo_scroll_token"  # new random value per successful run; see scroll_into_view_once

components.hero(
    "Try invoice processing",
    "Choose a fictional invoice. It runs through the same processing and validation code as the "
    "Azure pipeline — simulated here, so the result is instant and nothing is stored in Azure.",
    tag="Simulated · runs in this container · no Azure services used",
)

if not (state.health() or {}).get("demo_mode"):
    st.info("The processing demo is not enabled on this deployment.", icon=":material/info:")
    st.stop()

client = state.client()
try:
    scenarios = client.demo_scenarios()
except ApiError as error:
    components.backend_error(error)
    st.stop()

st.subheader("1 · Pick a scenario")
st.caption("Ten fictional invoices that together exercise all twelve validation rules.")
columns = st.columns(2)
for i, scenario in enumerate(scenarios):
    with columns[i % 2], st.container(border=True):
        st.markdown(f"**{scenario['title']}**")
        st.html(components.untrusted_html(scenario["story"]))
        st.html(
            components.untrusted_html(
                f"{scenario.get('invoice_number') or ''} · {scenario.get('vendor') or ''}"
            )
        )
        rules = ", ".join(scenario["rules_exercised"]) or "none — a clean invoice"
        st.caption(f"Rules exercised: {components.md_literal(rules)}")
        if st.button(
            "Run scenario", key=f"run_{scenario['scenario_id']}", icon=":material/play_arrow:"
        ):
            try:
                st.session_state[LAST] = client.run_demo(
                    session_id=state.session_id(), scenario_id=scenario["scenario_id"]
                )
                st.session_state[SCROLL] = secrets.token_hex(8)
            except ApiError as error:
                st.error(components.md_literal(error.message), icon=":material/error:")
        last = st.session_state.get(LAST)
        if last and last.get("scenario_id") == scenario["scenario_id"]:
            st.success("Scenario processed — showing results below.", icon=":material/south:")


def _render_run(run: dict) -> None:
    st.badge("Simulated in this container", icon=":material/science:", color="blue")
    st.caption(components.md_literal(run["provenance"]))
    st.html(
        components.timeline_html(
            "Since submission",
            [
                (f"+{s['offset_ms']:.1f} ms" if s["offset_ms"] is not None else "—", s["step"])
                for s in run["steps"]
            ],
        )
    )
    result, invoice = run.get("result"), run.get("invoice") or {}
    if not result:
        error = run.get("error") or {}
        st.error(components.md_literal(error.get("message", "The scenario did not complete.")))
        return
    head = st.columns([2, 1, 1, 1])
    with head[0]:
        components.status_badge(result["status"])
        st.html(components.untrusted_html(invoice.get("invoice_number"), "iq-h4"))
        if invoice.get("vendor"):
            st.html(components.untrusted_html(invoice["vendor"]))
    with head[1]:
        components.amount_block("Stated total", invoice.get("total"), invoice.get("currency"))
    with head[2]:
        components.amount_block(
            "Calculated total", result["calculated"].get("total"), invoice.get("currency")
        )
    with head[3]:
        st.html('<div class="iq-eyebrow">Findings</div>')
        st.markdown(f"### {len(result['findings'])}")
    components.definition_list(
        [
            ("Buyer", invoice.get("buyer")),
            ("Issue date", invoice.get("issue_date")),
            ("Purchase order", invoice.get("purchase_order")),
            ("Line items", invoice.get("line_items")),
            ("VAT stated", money(invoice.get("vat_amount"), invoice.get("currency"))),
            ("Required approval", result.get("required_approval_role") or "None"),
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
                    st.badge(fs.label, icon=fs.icon, color=fs.color)  # type: ignore[arg-type]
                    st.html(components.finding_html(finding))


def _render_reference() -> None:
    try:
        ref = client.demo_reference()
    except ApiError:
        st.caption("The verified reference run could not be loaded.")
        return
    st.badge(ref["label"], icon=":material/verified:", color="green")
    st.caption(
        components.md_literal(
            f"{ref['run']} on {ref['date']} · {ref['invoice']} · result: {ref['result']}"
        )
    )
    st.html(
        components.timeline_html(
            "Since upload", [(f"+{s['offset_seconds']:.1f} s", s["step"]) for s in ref["steps"]]
        )
    )
    summary = ref["summary"]
    st.caption(
        components.md_literal(
            f"Across {summary['submissions_tested']} test submissions in Azure: "
            f"{summary['upload_to_result_seconds_min']}–"
            f"{summary['upload_to_result_seconds_max']} s "
            f"from upload to result; {summary['duplicates']} duplicates, {summary['retries']} "
            f"retries, {summary['dead_letters']} dead-letters, "
            f"{summary['azure_openai_requests']} Azure OpenAI requests. Source: {ref['source']}."
        )
    )


run = st.session_state.get(LAST)
if run:
    st.divider()
    components.scroll_into_view_once("demo_result_anchor", st.session_state.get(SCROLL))
    st.subheader("2 · Processing result")
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown(f"**This demo run** — {components.md_literal(run['scenario_title'])}")
        _render_run(run)
    with right:
        st.markdown("**Verified in Azure — historical reference run**")
        _render_reference()
        st.caption(
            "Why the demo is instant: in Azure the worker starts from zero when a message "
            "arrives (image pull and container start take most of the half minute). Here the "
            "same code runs inside the web app, so there is nothing to start."
        )
