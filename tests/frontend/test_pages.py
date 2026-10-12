"""Streamlit page tests with AppTest, backed by the real FastAPI app in mock mode."""

from __future__ import annotations

import ast
from pathlib import Path

import httpx

from invoiceiq.frontend.api_client import ApiClient
from tests.frontend.conftest import FRONTEND_DIR, make_api_client


def all_text(at) -> str:
    parts = [e.value for e in at.markdown] + [e.value for e in at.caption]
    parts += [e.value for e in at.info] + [e.value for e in at.success]
    parts += [e.value for e in at.warning] + [e.value for e in at.error]
    parts += [e.label for e in at.expander] + [str(e.value) for e in at.metric]
    parts += [e.label for e in at.metric]
    return "\n".join(str(p) for p in parts)


def button(at, label: str):
    return next(b for b in at.button if b.label == label)


# --- Dashboard -------------------------------------------------------------------------------


def test_dashboard_renders_invoices_and_kpis(run_page):
    at = run_page("dashboard")
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics == {"Invoices": "3", "✓ Passed": "1", "! Requires Review": "1", "⚑ Flagged": "1"}
    assert len([b for b in at.button if b.label == "Analyse invoice"]) == 3
    text = all_text(at)
    assert "Mock AI" in text or any("Mock AI" in str(b) for b in at.sidebar)


def test_dashboard_card_opens_analysis_for_that_invoice(run_page):
    at = run_page("dashboard")
    next(b for b in at.button if b.key == "open-inv-003").click().run()
    assert not at.exception
    assert at.session_state["selected_invoice"] == "inv-003"
    assert at.title[0].value == "Invoice Analysis"
    assert at.selectbox(key="invoice_picker").value == "inv-003"


def test_dashboard_shows_friendly_error_when_backend_down(run_page):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    offline = ApiClient(
        http=httpx.Client(base_url="http://x", transport=httpx.MockTransport(refuse))
    )
    at = run_page("dashboard", client=offline)
    assert not at.exception
    assert any("not reachable" in e.value for e in at.error)


# --- Invoice Analysis ------------------------------------------------------------------------


def test_analysis_passed_invoice(run_page):
    at = run_page("analysis", selected_invoice="inv-001")
    assert not at.exception
    assert any("All validation checks passed" in s.value for s in at.success)
    assert len(at.dataframe) >= 2  # line items + totals (+ checks)


def test_analysis_groups_related_arithmetic_findings(run_page):
    at = run_page("analysis", selected_invoice="inv-003")
    assert not at.exception
    labels = [e.label for e in at.expander]
    assert any(label.startswith("Invoice arithmetic — 2 finding(s)") for label in labels)
    text = all_text(at)
    assert "earliest failing step is the **VAT amount**" in text
    assert "CALC-003" in text and "CALC-004" in text  # individual findings still shown
    assert any(label.startswith("PP-4.2") for label in labels)  # policy text available


def test_analysis_flagged_invoice_shows_approval_group(run_page):
    at = run_page("analysis", selected_invoice="inv-002")
    assert not at.exception
    assert any(e.label.startswith("Approval controls") for e in at.expander)


def test_analysis_switching_invoice(run_page):
    at = run_page("analysis", selected_invoice="inv-001")
    at.selectbox(key="invoice_picker").select("inv-002").run()
    assert not at.exception
    assert at.session_state["selected_invoice"] == "inv-002"


# --- Chat ------------------------------------------------------------------------------------


def test_chat_suggested_question_returns_cited_mock_answer(run_page):
    at = run_page("chat", selected_invoice="inv-002")
    assert not at.exception
    assert any("Mock AI" in i.value for i in at.info)
    button(at, "Why was this invoice flagged?").click().run()
    assert not at.exception
    messages = at.session_state["chats"]["inv-002"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    response = messages[1]["response"]
    assert response["mode"] == "ai" and response["provider"] == "mock"
    assert any(e.label.startswith("Evidence cited") for e in at.expander)
    assert any(e.label.startswith("Verified facts") for e in at.expander)


def test_chat_history_is_scoped_per_invoice(run_page):
    at = run_page("chat", selected_invoice="inv-002")
    at.chat_input(key="chat_input").set_value("Explain this invoice.").run()
    at.selectbox(key="invoice_picker").select("inv-001").run()
    assert not at.exception
    chats = at.session_state["chats"]
    assert len(chats["inv-002"]) == 2
    assert chats.get("inv-001", []) == []


def test_chat_sends_history_on_follow_up(run_page):
    client = make_api_client()
    at = run_page("chat", client=client, selected_invoice="inv-003")
    button(at, "Explain this invoice.").click().run()
    button(at, "What should the finance team do next?").click().run()
    assert not at.exception
    assert len(at.session_state["chats"]["inv-003"]) == 4


def test_chat_read_only_when_session_allowance_used(run_page):
    at = run_page("chat", client=make_api_client(session_request_limit=1),
                  selected_invoice="inv-001")  # fmt: skip
    button(at, "Explain this invoice.").click().run()
    button(at, "Explain this invoice.").click().run()
    assert not at.exception
    last = at.session_state["chats"]["inv-001"][-1]["response"]
    assert last["mode"] == "read_only"
    assert last["read_only_reason"] == "session_limit_reached"


def test_chat_shows_read_only_banner_when_ai_disabled(run_page):
    at = run_page("chat", client=make_api_client(ai_provider="disabled"))
    assert not at.exception
    assert any("Read-only mode" in w.value for w in at.warning)


def test_chat_clear_conversation(run_page):
    at = run_page("chat", selected_invoice="inv-001")
    button(at, "Explain this invoice.").click().run()
    button(at, "Clear conversation").click().run()
    assert not at.exception
    assert at.session_state["chats"]["inv-001"] == []


# --- Architecture and Observability ----------------------------------------------------------


def test_architecture_distinguishes_evidence_levels(run_page):
    at = run_page("architecture")
    assert not at.exception
    table = at.dataframe[0].value
    statuses = dict(zip(table["Component"], table["Status"], strict=True))
    assert statuses["Validation engine"] == "✓ Verified in Azure"
    assert statuses["Background worker (Container Apps Job)"] == "✓ Verified in Azure"
    assert statuses["Azure OpenAI provider"] == "✓ Verified in Azure (controlled)"
    assert statuses["Retries, lease takeover, dead-lettering"].startswith("✓ Verified locally")
    assert statuses["Public demo of invoice processing"] == "◐ Deployed · simulated in memory"
    assert statuses["Azure AI Search"] == "○ Planned"
    # Every status is one of the four evidence levels; nothing is left unlabelled.
    assert all(
        s.startswith(
            ("✓ Verified in Azure", "✓ Verified locally", "◐ Deployed · simulated", "○ Planned")
        )
        for s in statuses.values()
    )


def test_observability_with_no_traffic_shows_no_invented_metrics(run_page):
    client = make_api_client()
    at = run_page("observability", client=client)
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Chat requests"] == "0"
    assert metrics["Last AI latency"] == "—"
    assert metrics["Estimated AI cost"] == "—"


def test_observability_reflects_real_chat_measurements(run_page):
    client = make_api_client()
    client.chat(invoice_id="inv-001", question="Explain this invoice.",
                session_id="obs-session-01", history=[])  # fmt: skip
    at = run_page("observability", client=client)
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Chat requests"] == "1"
    assert metrics["Last AI latency"] != "—"
    assert "Tokens used (simulated)" in metrics
    assert metrics["AI spend (mock: no model calls)"] == "0 USD"
    table = at.dataframe[0].value
    assert "POST /chat" in list(table["Request"])


# --- Architecture rule: the frontend never duplicates business logic -------------------------


def test_frontend_does_not_import_backend_logic():
    forbidden = ("invoiceiq.core", "invoiceiq.ai", "invoiceiq.api", "openai", "azure")
    for path in Path(FRONTEND_DIR).rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith(forbidden), f"{path.name} imports {name}"


def test_sidebar_starts_collapsed_on_small_screens():
    # "auto" shows the sidebar on desktop and collapses it on phones, where an expanded sidebar
    # covers the page. AppTest has no viewport, so this checks the configuration itself.
    tree = ast.parse((Path(FRONTEND_DIR) / "app.py").read_text(encoding="utf-8"))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "set_page_config"
    ]  # fmt: skip
    assert len(calls) == 1
    kwargs = {k.arg: k.value.value for k in calls[0].keywords if isinstance(k.value, ast.Constant)}
    assert kwargs["initial_sidebar_state"] == "auto"


def test_no_legacy_pages_directory():
    # A pages/ folder next to app.py makes Streamlit's legacy navigation bypass app.py in the
    # browser (AppTest does not reproduce this), losing the sidebar, styles and layout.
    assert not (Path(FRONTEND_DIR) / "pages").exists()
