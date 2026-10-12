"""Process Invoice page: hidden unless enabled; submits and follows a submission to the end."""

from __future__ import annotations

import time

import pytest
from streamlit.testing.v1 import AppTest

from invoiceiq.frontend.presentation import submission_badge, timeline_rows
from tests.frontend.conftest import FRONTEND_DIR, make_api_client


def test_submission_badges_cover_every_status():
    for status in ("Pending", "Processing", "Completed", "Failed", "DeadLettered", "Stalled"):
        label, color, icon = submission_badge(status)
        assert label and color != "gray" and icon.startswith(":material/")
    assert submission_badge("Unknown")[1] == "gray"


def test_timeline_rows_show_time_since_upload():
    rows = timeline_rows(
        {
            "created_at": "2026-10-11T12:00:00Z",
            "timeline": [
                {"step": "Uploaded to Blob Storage", "at": "2026-10-11T12:00:00Z"},
                {"step": "Worker started", "at": "2026-10-11T12:00:23.5Z"},
                {"step": "Completed", "at": None},
            ],
        }
    )
    assert rows[1] == {
        "Step": "Worker started",
        "Time (UTC)": "12:00:23",
        "Since upload": "+23.5 s",
    }
    assert rows[2]["Time (UTC)"] == "—" and rows[2]["Since upload"] == "—"


def test_page_is_not_in_navigation_when_disabled(run_page):
    at = run_page()
    assert not at.exception
    with pytest.raises(ValueError, match="Could not find a navigation page"):
        at.switch_page("views/process.py")  # not registered in st.navigation


def test_page_explains_when_backend_has_it_disabled():
    at = AppTest.from_file(str(FRONTEND_DIR / "views" / "process.py"), default_timeout=30)
    at.session_state["api_client"] = make_api_client()
    at.run()
    assert any("not enabled" in i.value for i in at.info)


def test_sample_submission_is_followed_until_completed(run_page):
    client = make_api_client(async_processing_enabled=True, local_pipeline_delay_seconds=0)
    at = run_page("process", client=client)
    assert not at.exception
    at.selectbox[0].set_value("inv-003")
    [process] = [b for b in at.button if b.label == "Process invoice"]
    process.click()
    at.run()
    assert not at.exception
    submission_id = at.session_state["submission_active"]
    for _ in range(50):
        if client.get_submission(submission_id)["terminal"]:
            break
        time.sleep(0.02)
    at.run()  # the status panel re-reads the submission
    assert not at.exception
    # The invoice number comes from the submitted file, so it is rendered as escaped HTML.
    html_text = " ".join(e.proto.body for e in at.get("html"))
    assert "KFS-2026-0203" in html_text
    assert any(e.label.startswith("Invoice arithmetic — 2 finding(s)") for e in at.expander)
    assert at.session_state["submission_watch"] is False  # polling stopped at the final state
