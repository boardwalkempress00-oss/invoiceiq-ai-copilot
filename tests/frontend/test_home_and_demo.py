"""Start here and Try processing pages: navigation, feature flags and provenance labels."""

from __future__ import annotations

import json
import re
from html import unescape

import pytest

from tests.frontend.conftest import make_api_client
from tests.frontend.test_pages import all_text


def html(at) -> str:
    return "\n".join(e.proto.body for e in at.get("html"))


def text(at) -> str:
    """Visible text with Markdown backslash-escapes (from components.md_literal) removed."""
    return re.sub(r"\\(.)", r"\1", all_text(at)) + "\n" + html(at)


def timelines(at) -> list[list[tuple[str, str]]]:
    """(time, event) rows of each timeline table on the page, in order."""
    tables = [
        b
        for b in (e.proto.body for e in at.get("html"))
        if b.startswith('<table class="iq-timeline"')
    ]
    rows = []
    for table in tables:
        cells = re.findall(r'<td class="iq-t">(.*?)</td><td>(.*?)</td>', table)
        rows.append([(unescape(t), unescape(event)) for t, event in cells])
    return rows


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
    demo_rows, reference_rows = timelines(at)
    assert [event for _, event in demo_rows][-1] == "Completed"
    assert all(t.endswith(" ms") for t, _ in demo_rows)
    assert reference_rows == [
        ("+0.0 s", "Submission accepted; file stored in Blob Storage"),
        ("+0.8 s", "BlobCreated delivered to the Service Bus queue"),
        ("+4.5 s", "KEDA detects the message and starts a worker job from zero"),
        ("+26.6 s", "Worker container started (image pulled, container created)"),
        ("+29.3 s", "Invoice validated and result stored"),
    ]
    assert "Source: docs/evidence/production-verification.md" in body


def test_timelines_put_the_time_first_and_never_scroll_sideways(run_page):
    at = run_page("demo", client=demo_client())
    next(b for b in at.button if b.key == "run_demo-vat-error").click().run()
    assert not at.dataframe  # canvas grids clip long text in narrow columns
    tables = [
        b
        for b in (e.proto.body for e in at.get("html"))
        if b.startswith('<table class="iq-timeline"')
    ]
    assert len(tables) == 2
    for table in tables:
        header = re.findall(r"<th(?: [^>]*)?>(.*?)</th>", table)
        assert header[0] in ("Since submission", "Since upload") and header[1] == "Event"
    css = html(at)
    assert (
        ".iq-timeline" in css and "table-layout: fixed" in css and "overflow-wrap: anywhere" in css
    )
    assert re.search(r"\.iq-timeline \.iq-t \{[^}]*white-space: nowrap", css)


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


# --- Scroll to the result after a run ----------------------------------------------------------
# Scrolling itself happens in the browser (checked by tests/browser/demo_autoscroll_e2e.py).
# These tests pin the Python side: a fresh token per successful run, the same token on ordinary
# reruns, no token for a refused run, and nothing from the invoice inside the script.

FEEDBACK = "Scenario processed — showing results below."
SCENARIO_IDS = [
    "demo-clean", "demo-approved-manager", "demo-approval-missing", "demo-insufficient-authority",
    "demo-vat-error", "demo-line-error", "demo-nonstandard-vat", "demo-foreign-supplier",
    "demo-foreign-currency", "demo-missing-fields",
]  # fmt: skip


def scroll_anchor(at):
    found = [
        e for e in at.get("bidi_component") if e.proto.component_name.endswith("scroll_anchor")
    ]
    return found[0] if found else None


def anchor_token(at) -> str | None:
    anchor = scroll_anchor(at)
    return json.loads(anchor.proto.json)["token"] if anchor else None


def click_run(at, scenario_id: str):
    next(b for b in at.button if b.key == f"run_{scenario_id}").click().run()
    assert not at.exception
    return at


def test_no_scroll_anchor_before_any_run(run_page):
    at = run_page("demo", client=demo_client())
    assert scroll_anchor(at) is None
    assert FEEDBACK not in text(at)


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_every_successful_run_requests_a_scroll_to_the_result(run_page, scenario_id):
    at = click_run(run_page("demo", client=demo_client()), scenario_id)
    token = anchor_token(at)
    assert token and re.fullmatch(r"[0-9a-f]{16}", token)
    assert [s.value for s in at.subheader][-1] == "2 · Processing result"
    # The anchor sits directly above the result heading, and the feedback is shown once.
    assert sum(FEEDBACK in s.value for s in at.success) == 1


def test_a_new_run_gets_a_new_token_and_reruns_keep_it(run_page):
    at = click_run(run_page("demo", client=demo_client()), "demo-vat-error")
    first = anchor_token(at)
    at.run()  # an ordinary rerun: same token, so the browser does not scroll again
    assert anchor_token(at) == first
    click_run(at, "demo-clean")
    second = anchor_token(at)
    assert second and second != first
    # Only the card that ran last shows the feedback.
    assert sum(FEEDBACK in s.value for s in at.success) == 1


def test_a_refused_run_does_not_request_a_scroll(run_page):
    client = make_api_client(demo_mode_enabled=True, demo_session_daily_limit=1)
    at = click_run(run_page("demo", client=client), "demo-clean")
    first = anchor_token(at)
    click_run(at, "demo-vat-error")
    assert any("daily allowance" in e.value for e in at.error)
    assert anchor_token(at) == first


def test_scroll_script_contains_no_page_data(run_page):
    at = click_run(run_page("demo", client=demo_client()), "demo-vat-error")
    anchor = scroll_anchor(at)
    script = anchor.proto.js_content
    run = at.session_state["demo_last_run"]
    for value in (anchor_token(at), run["run_id"], run["invoice"]["invoice_number"], "Bøgely"):
        assert value not in script
    assert set(json.loads(anchor.proto.json)) == {"token"}
