"""Start here and Try processing pages: navigation, feature flags and provenance labels."""

from __future__ import annotations

import re

import pytest

from tests.frontend.conftest import make_api_client
from tests.frontend.test_pages import all_text


def html(at) -> str:
    return "\n".join(e.proto.body for e in at.get("html"))


def text(at) -> str:
    """Visible text with Markdown backslash-escapes (from components.md_literal) removed."""
    return re.sub(r"\\(.)", r"\1", all_text(at)) + "\n" + html(at)


def demo_client():
    return make_api_client(demo_mode_enabled=True)


# --- Start here ---------------------------------------------------------------------------------


def test_start_here_is_the_default_page(run_page):
    at = run_page()
    assert not at.exception
    assert "InvoiceIQ — AI Finance Operations Copilot" in html(at)
    assert any(b.label == "Try InvoiceIQ" for b in at.button)


def test_start_here_separates_real_simulated_and_planned(run_page):
    at = run_page(client=demo_client())
    assert not at.exception
    body = text(at)
    for label in ("Verified in Azure", "In this public app", "Planned"):
        assert label in body or label in str(at.get("badge")), label
    assert "**simulated** in this container" in body
    assert "about 28 seconds" in body  # the measured cold start, not an invented figure


def test_try_invoiceiq_opens_the_demo_when_enabled(run_page):
    at = run_page(client=demo_client())
    next(b for b in at.button if b.key == "try_main").click().run()
    assert not at.exception
    assert "Try invoice processing" in html(at)


def test_try_invoiceiq_falls_back_to_analysis_when_demo_disabled(run_page):
    at = run_page()
    assert "Processing demo: not enabled." in text(at)
    assert not any(b.key == "go_demo" for b in at.button)
    next(b for b in at.button if b.key == "try_main").click().run()
    assert not at.exception
    assert at.title[0].value == "Invoice Analysis"


# --- Navigation by flag -------------------------------------------------------------------------


def test_demo_page_is_not_registered_when_disabled(run_page):
    with pytest.raises(ValueError, match="Could not find a navigation page"):
        run_page("demo")


def test_demo_only_does_not_expose_live_processing(run_page):
    at = run_page("demo", client=demo_client())
    assert not at.exception
    with pytest.raises(ValueError, match="Could not find a navigation page"):
        at.switch_page("views/process.py")


def test_both_flags_register_both_pages(run_page):
    client = make_api_client(demo_mode_enabled=True, async_processing_enabled=True)
    at = run_page("demo", client=client)
    assert not at.exception
    at.switch_page("views/process.py").run()
    assert not at.exception


# --- Try processing -----------------------------------------------------------------------------


def test_demo_gallery_lists_ten_scenarios(run_page):
    at = run_page("demo", client=demo_client())
    assert not at.exception
    run_buttons = [b for b in at.button if (b.key or "").startswith("run_demo-")]
    assert len(run_buttons) == 10
    assert "Simulated · runs in this container · no Azure services used" in html(at)


def test_running_a_scenario_shows_simulated_result_beside_the_verified_reference(run_page):
    at = run_page("demo", client=demo_client())
    next(b for b in at.button if b.key == "run_demo-vat-error").click().run()
    assert not at.exception
    run = at.session_state["demo_last_run"]
    assert run["processing_mode"] == "simulated" and run["status"] == "Completed"

    body = text(at)
    assert "This demo run" in body
    assert "No Azure service was used" in body
    assert "Verified in Azure — historical reference run" in body
    assert "Phase E1 on 2026-10-12" in body
    assert "24.5–35.7 s" in body
    assert any(e.label.startswith("Invoice arithmetic — 2 finding(s)") for e in at.expander)
    # The demo's own timings are in milliseconds; the Azure reference stays in seconds.
    demo_steps, reference_steps = at.dataframe[0].value, at.dataframe[1].value
    assert all(v.endswith(" ms") or v == "—" for v in demo_steps["Since submission"])
    assert list(reference_steps["Since upload"]) == [
        "+0.0 s", "+0.8 s", "+4.5 s", "+26.6 s", "+29.3 s",
    ]  # fmt: skip


def test_clean_scenario_reports_all_checks_passed(run_page):
    at = run_page("demo", client=demo_client())
    next(b for b in at.button if b.key == "run_demo-clean").click().run()
    assert not at.exception
    assert any("All validation checks passed" in s.value for s in at.success)


def test_demo_allowance_message_is_shown(run_page):
    client = make_api_client(demo_mode_enabled=True, demo_session_daily_limit=1)
    at = run_page("demo", client=client)
    next(b for b in at.button if b.key == "run_demo-clean").click().run()
    next(b for b in at.button if b.key == "run_demo-clean").click().run()
    assert not at.exception
    assert any("daily allowance" in e.value for e in at.error)
