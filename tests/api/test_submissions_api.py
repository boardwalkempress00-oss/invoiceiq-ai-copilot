"""Submission endpoints: feature flag, contract, errors, and isolation from the AI chat."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from invoiceiq.api.main import create_app
from invoiceiq.configuration import Settings
from invoiceiq.core.invoice_service import InvoiceService, JsonInvoiceRepository
from invoiceiq.processing.blobs import InMemoryBlobStore
from invoiceiq.processing.local_pipeline import LocalPipeline
from invoiceiq.processing.processor import ProcessorConfig
from invoiceiq.processing.store import InMemorySubmissionStore
from invoiceiq.processing.submissions import SubmissionService

SESSION = "session-abcdefgh"


def make(**overrides: object) -> tuple[TestClient, LocalPipeline]:
    settings = Settings(_env_file=None, async_processing_enabled=True, **overrides)  # type: ignore[arg-type]
    store, blobs = InMemorySubmissionStore(), InMemoryBlobStore()
    pipeline = LocalPipeline(
        store,
        blobs,
        ProcessorConfig(
            container=settings.upload_container, max_invoice_bytes=settings.submission_max_bytes
        ),
        background=False,
    )
    service = SubmissionService(
        settings,
        store,
        blobs,
        InvoiceService(JsonInvoiceRepository(settings.invoices_dir)),
        on_uploaded=pipeline.on_uploaded,
    )
    return TestClient(create_app(settings, submission_service=service)), pipeline


@pytest.fixture
def enabled() -> tuple[TestClient, LocalPipeline]:
    return make()


def submit(client: TestClient, **body: object):
    return client.post("/submissions", json={"session_id": SESSION, **body})


# --- Feature flag ------------------------------------------------------------------------------


def test_disabled_by_default_everywhere():
    client = TestClient(create_app(Settings(_env_file=None)))
    assert client.get("/health").json()["async_processing"] is False
    for response in (
        submit(client, sample_invoice_id="inv-001"),
        client.get("/submissions/" + "a" * 32),
        client.get("/submissions", params={"session_id": SESSION}),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "feature_disabled"


def test_health_reports_feature_and_openapi_lists_routes(enabled):
    client, _ = enabled
    assert client.get("/health").json()["async_processing"] is True
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/submissions", "/submissions/{submission_id}"} <= set(paths)


# --- Happy path ----------------------------------------------------------------------------------


def test_sample_submission_is_accepted_then_completed(enabled):
    client, pipeline = enabled
    response = submit(client, sample_invoice_id="inv-003")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "Pending" and body["terminal"] is False
    assert body["poll_after_ms"] == 2000 and body["result"] is None
    assert body["correlation_id"] == response.headers["X-Correlation-ID"]

    assert pipeline.drain() == ["completed"]
    done = client.get(f"/submissions/{body['submission_id']}").json()
    assert done["status"] == done["display_status"] == "Completed" and done["terminal"]
    assert done["poll_after_ms"] is None and done["attempts"] == 1
    assert done["result"]["status"] == "Requires Review"
    assert {f["rule_id"] for f in done["result"]["findings"]} == {"CALC-003", "CALC-004"}
    assert done["invoice"]["invoice_number"] == "KFS-2026-0203"
    assert [s["step"] for s in done["timeline"]] == [
        "Uploaded to Blob Storage",
        "BlobCreated event queued",
        "Worker started",
        "Completed",
    ]
    assert all(s["at"] for s in done["timeline"])


def test_uploaded_json_is_processed(enabled):
    client, pipeline = enabled
    content = (Settings(_env_file=None).invoices_dir / "inv-002.json").read_text()
    body = submit(client, content=content).json()
    pipeline.drain()
    done = client.get(f"/submissions/{body['submission_id']}").json()
    assert done["source"] == "upload" and done["result"]["status"] == "Flagged"


def test_background_local_pipeline_completes_without_manual_draining():
    client = TestClient(
        create_app(
            Settings(_env_file=None, async_processing_enabled=True, local_pipeline_delay_seconds=0)
        )
    )
    sid = submit(client, sample_invoice_id="inv-001").json()["submission_id"]
    for _ in range(100):
        body = client.get(f"/submissions/{sid}").json()
        if body["terminal"]:
            break
        time.sleep(0.02)
    assert body["status"] == "Completed"


# --- Error policy --------------------------------------------------------------------------------


def test_invalid_json_is_rejected_synchronously(enabled):
    client, pipeline = enabled
    response = submit(client, content="{nope")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_json"
    assert pipeline.queue.active_count() == 0


def test_too_large_is_413(enabled):
    client, _ = enabled
    response = submit(client, content='{"a": "' + "x" * 70_000 + '"}')
    assert response.status_code == 413 and response.json()["error"]["code"] == "too_large"


def test_schema_error_fails_asynchronously_with_details(enabled):
    client, pipeline = enabled
    body = submit(client, content='{"invoice_id": "Bad Id", "total": "abc"}').json()
    assert body["status"] == "Pending"
    pipeline.drain()
    done = client.get(f"/submissions/{body['submission_id']}").json()
    assert done["status"] == "Failed" and done["terminal"]
    assert done["error"]["code"] == "invalid_schema"
    assert any(d.startswith("invoice_id:") for d in done["error"]["details"])
    assert "Bad Id" not in str(done["error"]) and done["result"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"session_id": SESSION},
        {"session_id": SESSION, "sample_invoice_id": "inv-001", "content": "{}"},
        {"sample_invoice_id": "inv-001"},
        {"session_id": "x", "sample_invoice_id": "inv-001"},
        {"session_id": SESSION, "sample_invoice_id": "../etc"},
        {"session_id": SESSION, "sample_invoice_id": "inv-001", "unexpected": 1},
    ],
)
def test_malformed_requests_are_422(enabled, body):
    client, _ = enabled
    response = client.post("/submissions", json=body)
    assert response.status_code == 422


def test_unknown_sample_is_404(enabled):
    client, _ = enabled
    assert submit(client, sample_invoice_id="inv-999").status_code == 404


def test_submission_lookup_errors(enabled):
    client, _ = enabled
    assert client.get("/submissions/not-an-id").status_code == 422
    missing = client.get("/submissions/" + "b" * 32)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "submission_not_found"


# --- Allowance and isolation from the AI chat ----------------------------------------------------


def test_upload_allowance_is_separate_from_the_ai_allowance():
    client, _ = make(submission_session_daily_limit=2)
    ai_before = client.get("/health").json()["ai"]["daily_requests_remaining"]
    assert submit(client, sample_invoice_id="inv-001").status_code == 202
    assert submit(client, sample_invoice_id="inv-001").status_code == 202
    limited = submit(client, sample_invoice_id="inv-001")
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "upload_limit_reached"

    # Uploads did not consume AI questions, and chat works for the same session.
    assert client.get("/health").json()["ai"]["daily_requests_remaining"] == ai_before
    chat = client.post(
        "/chat",
        json={"invoice_id": "inv-001", "question": "Explain this invoice.", "session_id": SESSION},
    )
    assert chat.status_code == 200 and chat.json()["mode"] == "ai"
    assert chat.json()["limits"]["session_remaining"] == 9


def test_exhausted_ai_allowance_does_not_block_uploads():
    client, _ = make(session_request_limit=0)
    assert submit(client, sample_invoice_id="inv-001").status_code == 202


def test_list_returns_only_this_sessions_submissions_without_findings(enabled):
    client, pipeline = enabled
    mine = submit(client, sample_invoice_id="inv-001").json()["submission_id"]
    client.post(
        "/submissions", json={"session_id": "someone-else-1", "sample_invoice_id": "inv-002"}
    )
    pipeline.drain()
    listed = client.get("/submissions", params={"session_id": SESSION}).json()
    assert [s["submission_id"] for s in listed] == [mine]
    assert listed[0]["result"] is None and listed[0]["status"] == "Completed"


def test_misconfigured_azure_backend_fails_closed_and_chat_keeps_working():
    settings = Settings(_env_file=None, async_processing_enabled=True, submission_backend="azure")
    client = TestClient(create_app(settings))  # no endpoints configured
    assert client.get("/health").json()["async_processing"] is False
    assert submit(client, sample_invoice_id="inv-001").status_code == 404
    chat = client.post(
        "/chat",
        json={"invoice_id": "inv-001", "question": "Explain this invoice.", "session_id": SESSION},
    )
    assert chat.status_code == 200
