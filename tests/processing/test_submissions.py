"""API-side submission service: input policy, allowance, ordering and failure handling."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from invoiceiq.configuration import Settings
from invoiceiq.core.invoice_service import (
    InvoiceNotFoundError,
    InvoiceService,
    JsonInvoiceRepository,
)
from invoiceiq.logging_config import correlation_id_var
from invoiceiq.processing.blobs import BlobStoreError, InMemoryBlobStore
from invoiceiq.processing.models import ErrorCode, SubmissionSource, SubmissionStatus
from invoiceiq.processing.store import InMemorySubmissionStore, SubmissionStoreError
from invoiceiq.processing.submissions import SubmissionError, SubmissionService
from tests.processing.conftest import INVOICES

SESSION = "session-abcdefgh"


@pytest.fixture
def service(clock):
    settings = Settings(
        _env_file=None,
        async_processing_enabled=True,
        submission_session_daily_limit=3,
        submission_global_daily_limit=5,
    )
    uploaded = []
    svc = SubmissionService(
        settings,
        InMemorySubmissionStore(),
        InMemoryBlobStore(),
        InvoiceService(JsonInvoiceRepository(INVOICES)),
        clock=clock,
        on_uploaded=lambda record, etag: uploaded.append(record.submission_id),
    )
    svc.uploaded = uploaded  # type: ignore[attr-defined]
    return svc


def test_sample_submission_creates_pending_row_and_blob(service):
    token = correlation_id_var.set("corr-1234567890")
    try:
        record = service.submit(session_id=SESSION, sample_invoice_id="inv-002")
    finally:
        correlation_id_var.reset(token)
    assert record.status == SubmissionStatus.PENDING and record.source == SubmissionSource.SAMPLE
    assert record.blob_name == f"submissions/{record.submission_id}.json"
    assert service.blobs.request_ids[record.blob_name] == "corr-1234567890"
    assert service.blobs.metadata[record.blob_name]["submissionid"] == record.submission_id
    assert service.uploaded == [record.submission_id]
    assert record.expires_at - record.created_at == timedelta(days=7)


def test_row_exists_before_the_blob_is_uploaded(service, monkeypatch):
    seen = {}
    real_upload = service.blobs.upload_new

    def upload(name, data, **kw):
        sid = name.split("/")[1].removesuffix(".json")
        seen["row_status"] = service.store.get(sid).record.status
        return real_upload(name, data, **kw)

    monkeypatch.setattr(service.blobs, "upload_new", upload)
    service.submit(session_id=SESSION, content='{"invoice_id": "x-1"}')
    assert seen["row_status"] == SubmissionStatus.PENDING


@pytest.mark.parametrize(
    ("content", "status", "code"),
    [
        ("", 422, "invalid_json"),
        ("   ", 422, "invalid_json"),
        ("{not json", 422, "invalid_json"),
        ("[1, 2]", 422, "invalid_json"),
        ('{"a": Infinity}', 422, "invalid_json"),
        ("{" + '"a": "' + "x" * 70_000 + '"}', 413, "too_large"),
    ],
)
def test_bad_files_are_rejected_synchronously_and_nothing_is_stored(service, content, status, code):
    with pytest.raises(SubmissionError) as err:
        service.submit(session_id=SESSION, content=content)
    assert (err.value.status_code, err.value.code) == (status, code)
    assert service.blobs.blobs == {} and service.store.list_for_session("x", 10) == []
    # A rejected file does not use the allowance.
    assert service.store.reserve_upload("2026-10-11", "probe", 1, 1).allowed


def test_schema_problems_are_left_to_the_worker(service):
    record = service.submit(session_id=SESSION, content='{"invoice_id": "NOT VALID"}')
    assert record.status == SubmissionStatus.PENDING  # the worker marks it Failed


def test_exactly_one_source_is_required(service):
    with pytest.raises(SubmissionError, match="either"):
        service.submit(session_id=SESSION)
    with pytest.raises(SubmissionError, match="either"):
        service.submit(session_id=SESSION, sample_invoice_id="inv-001", content="{}")


def test_unknown_sample_is_not_found(service):
    with pytest.raises(InvoiceNotFoundError):
        service.submit(session_id=SESSION, sample_invoice_id="inv-999")


def test_session_and_global_upload_allowances(service):
    for _ in range(3):
        service.submit(session_id=SESSION, sample_invoice_id="inv-001")
    with pytest.raises(SubmissionError) as err:
        service.submit(session_id=SESSION, sample_invoice_id="inv-001")
    assert (err.value.status_code, err.value.code) == (429, "upload_limit_reached")
    assert "this session" in err.value.message and "separate from AI questions" in err.value.message

    service.submit(session_id="other-session-1", sample_invoice_id="inv-001")
    service.submit(session_id="other-session-1", sample_invoice_id="inv-001")
    with pytest.raises(SubmissionError, match="the demo"):  # global cap of 5 reached
        service.submit(session_id="third-session-1", sample_invoice_id="inv-001")


def test_upload_failure_marks_row_failed_and_returns_503(service):
    service.blobs.fail_next = [BlobStoreError("ServiceRequestError")]
    with pytest.raises(SubmissionError) as err:
        service.submit(session_id=SESSION, sample_invoice_id="inv-001")
    assert (err.value.status_code, err.value.code) == (503, "upload_failed")
    [row] = service.list_recent(SESSION)
    assert row.status == SubmissionStatus.FAILED and row.error_code == ErrorCode.UPLOAD_FAILED
    assert service.uploaded == []  # no event for a blob that does not exist


def test_store_outage_is_reported_as_unavailable(service):
    service.store.fail_next = [SubmissionStoreError("down")]
    with pytest.raises(SubmissionError) as err:
        service.submit(session_id=SESSION, sample_invoice_id="inv-001")
    assert (err.value.status_code, err.value.code) == (503, "processing_unavailable")


def test_expired_rows_are_hidden_before_cleanup(service, clock):
    record = service.submit(session_id=SESSION, sample_invoice_id="inv-001")
    assert service.get(record.submission_id) is not None
    clock.advance(7 * 24 * 3600 + 1)
    assert service.get(record.submission_id) is None
    assert service.list_recent(SESSION) == []


def test_stalled_detection(service, clock):
    record = service.submit(session_id=SESSION, sample_invoice_id="inv-001")
    assert not service.is_stalled(record)
    clock.advance(181)
    assert service.is_stalled(record)
    assert not service.is_stalled(replace(record, status=SubmissionStatus.COMPLETED))
    held = replace(
        record, status=SubmissionStatus.PROCESSING, lease_expires_at=clock() + timedelta(seconds=30)
    )
    assert not service.is_stalled(held)  # a worker is actively on it


def test_invalid_submission_id_is_not_looked_up(service):
    assert service.get("../../etc/passwd") is None
