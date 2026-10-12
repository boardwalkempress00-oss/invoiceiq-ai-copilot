"""Security regression: submitted invoice values are never rendered as Markdown or HTML.

Invoice numbers, vendor names, line IDs, currency codes and schema field names come from the
submitted file. On the Process Invoice page they must appear as inert text: no links, images,
formatting or markup — whatever the payload looks like.
"""

from __future__ import annotations

import html
import json
import string
import time
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from invoiceiq.frontend import components
from tests.frontend.conftest import FRONTEND_DIR, make_api_client

PAYLOADS = [
    "![pixel](https://tracker.example/p.png)",
    "[click me](https://phish.example/login)",
    "[js](javascript:alert(1))",
    "<img src=x onerror=alert(1)>",
    "<script>alert('x')</script>",
    "<a href='https://phish.example'>pay here</a>",
    "**bold** _italic_ `code` # heading",
    "https://autolink.example/path",
    "`` ` `` </code><b>break out</b>",
    "line1\n\n- list item\n> quote",
]
RAW_MARKERS = ("<img", "<script", "<a ", "<b>", "](", "![")


# --- Helpers ---------------------------------------------------------------------------------


@pytest.mark.parametrize("payload", PAYLOADS)
def test_md_literal_escapes_every_punctuation_character(payload):
    out = components.md_literal(payload)
    i = 0
    while i < len(out):  # every punctuation character must be preceded by a backslash
        if out[i] == "\\":
            assert out[i + 1] in string.punctuation
            i += 2
            continue
        assert out[i] not in string.punctuation, (payload, out)
        i += 1
    assert "\n" not in out


@pytest.mark.parametrize("payload", PAYLOADS)
def test_html_helpers_escape_payloads(payload):
    finding = {
        "title": payload,
        "finding_id": payload,
        "description": payload,
        "expected_value": payload,
        "actual_value": payload,
        "policy_reference": payload,
        "recommended_action": payload,
    }
    for rendered in (
        components.untrusted_html(payload),
        components.finding_html(finding),
        components.details_html([payload, payload]),
    ):
        # The payload appears only in escaped form; its own markup never survives.
        assert html.escape(payload) in rendered
        if "<" in payload or "&" in payload:
            assert payload not in rendered
        for tag in ("<img", "<script", "<a ", "</code><b>"):
            assert tag not in rendered


def test_none_and_missing_values_are_safe():
    assert components.untrusted_html(None) == '<div class="iq-sub"></div>'
    assert components.md_literal(None) == ""
    assert "—" in components.finding_html({})


# --- The real page with a hostile invoice ------------------------------------------------------

HOSTILE = "![pixel](https://tracker.example/p.png) <img src=x onerror=alert(1)> [x](javascript:1)"


def hostile_invoice() -> str:
    data = json.loads((FRONTEND_DIR.parents[2] / "data" / "invoices" / "inv-003.json").read_text())
    data["invoice_number"] = HOSTILE
    data["vendor"]["name"] = HOSTILE
    data["currency"] = "<b>EUR</b>](x)"  # becomes part of a CUR-001 finding
    data["line_items"][0]["line_id"] = "<img src=y>[l](https://e.example)"
    data["line_items"][0]["line_total"] = "1.00"  # forces a line finding quoting the line ID
    return json.dumps(data)


def run_page_for(content: str) -> tuple[AppTest, dict[str, Any]]:
    client = make_api_client(async_processing_enabled=True, local_pipeline_delay_seconds=0)
    sub = client.submit_invoice(session_id="security-test-session", content=content)
    for _ in range(100):
        sub = client.get_submission(sub["submission_id"])
        if sub["terminal"]:
            break
        time.sleep(0.02)
    at = AppTest.from_file(str(FRONTEND_DIR / "app.py"), default_timeout=30)
    at.session_state["api_client"] = client
    at.run()
    at.switch_page("views/process.py")
    at.session_state["submission_active"] = sub["submission_id"]
    at.session_state["submission_watch"] = False
    at.run()
    assert not at.exception
    return at, sub


def markdown_like_values(at: AppTest) -> list[str]:
    values = []
    for kind in ("markdown", "caption", "error", "info", "warning", "success", "subheader"):
        values += [str(e.value) for e in at.get(kind)]
    return values


def html_bodies(at: AppTest) -> list[str]:
    return [e.proto.body for e in at.get("html")]


def test_hostile_invoice_values_render_as_text_only():
    at, sub = run_page_for(hostile_invoice())
    assert sub["status"] == "Completed"
    # Not interpreted anywhere Markdown is rendered.
    for value in markdown_like_values(at):
        for marker in RAW_MARKERS:
            assert marker not in value, (marker, value[:120])
        assert "javascript:" not in value and "tracker.example" not in value
    # Rendered (escaped) as HTML text instead — present, but inert.
    bodies = " ".join(html_bodies(at))
    assert "&lt;img src=x onerror=alert(1)&gt;" in bodies  # invoice number / vendor
    assert "&lt;b&gt;EUR&lt;/b&gt;" in bodies  # currency quoted in a finding
    assert "&lt;img src=y&gt;" in bodies  # line ID quoted in a finding
    for tag in ("<img", "<script", "<a "):
        assert tag not in bodies


def test_hostile_schema_field_names_render_as_text_only():
    content = json.dumps(
        {"invoice_id": "ok-1", "![x](https://tracker.example/k.png)<img src=z>": "value"}
    )
    at, sub = run_page_for(content)
    assert sub["status"] == "Failed" and sub["error"]["code"] == "invalid_schema"
    for value in markdown_like_values(at):
        for marker in RAW_MARKERS:
            assert marker not in value, (marker, value[:120])
    bodies = " ".join(html_bodies(at))
    assert "&lt;img src=z&gt;" in bodies  # the field name, escaped, in the details list
    assert "<img" not in bodies
    assert "value" not in " ".join(markdown_like_values(at))  # submitted values never echoed
