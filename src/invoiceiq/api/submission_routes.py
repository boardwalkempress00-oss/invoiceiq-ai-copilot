"""Invoice submission endpoints (asynchronous processing).

    POST /submissions                 accept a sample or uploaded JSON invoice → 202 Pending
    GET  /submissions/{submission_id} status, timeline and — once Completed — the findings
    GET  /submissions?session_id=…    this session's recent submissions (no findings)

All three answer 404 `feature_disabled` unless ASYNC_PROCESSING_ENABLED is set. Submission IDs
are random 128-bit values; knowing one is what grants read access to it (fictional data only).
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Path, Query
from fastapi.responses import JSONResponse

from invoiceiq.api.routes import Container
from invoiceiq.api.schemas import (
    SESSION_ID_PATTERN,
    ErrorResponse,
    SubmissionErrorOut,
    SubmissionOut,
    SubmissionRequest,
    TimelineStepOut,
)
from invoiceiq.core.models import ValidationResult
from invoiceiq.processing.models import SUBMISSION_ID_PATTERN, SubmissionRecord
from invoiceiq.processing.submissions import SubmissionError, SubmissionService

router = APIRouter(tags=["submissions"])
_ERRORS = {
    404: {"model": ErrorResponse},
    413: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}
POLL_PENDING_MS = 2_000
POLL_SLOW_MS = 5_000
POLL_SLOW_AFTER_SECONDS = 30


def _service(container: Container) -> SubmissionService:
    if container.submission_service is None:
        raise SubmissionError(404, "feature_disabled", "Invoice processing is not enabled.")
    return container.submission_service


def to_out(service: SubmissionService, record: SubmissionRecord, *, detail: bool) -> SubmissionOut:
    stalled = service.is_stalled(record)
    result = invoice = None
    if detail and record.result_json:
        payload = json.loads(record.result_json)
        result = ValidationResult.model_validate(payload["result"])
        invoice = payload.get("invoice")
    error = None
    if record.error_code is not None:
        error = SubmissionErrorOut(
            code=record.error_code.value,
            message=record.error_message or "",
            details=list(record.error_details),
        )
    poll = None
    if not record.status.terminal and not stalled:
        age = (service.now() - record.created_at).total_seconds()
        poll = POLL_SLOW_MS if age > POLL_SLOW_AFTER_SECONDS else POLL_PENDING_MS
    finished_step = {
        "Completed": "Completed",
        "Failed": "Failed",
        "DeadLettered": "Dead-lettered",
    }.get(record.status.value, "Finished")
    return SubmissionOut(
        submission_id=record.submission_id,
        status=record.status.value,
        display_status="Stalled" if stalled else record.status.value,
        terminal=record.status.terminal,
        source=record.source.value,
        sample_invoice_id=record.sample_invoice_id,
        attempts=record.attempts,
        size_bytes=record.size_bytes,
        created_at=record.created_at,
        updated_at=record.updated_at,
        queued_at=record.queued_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        invoice_id=record.invoice_id,
        invoice_number=record.invoice_number,
        correlation_id=record.correlation_id,
        error=error,
        timeline=[
            TimelineStepOut(step="Uploaded to Blob Storage", at=record.created_at),
            TimelineStepOut(step="BlobCreated event queued", at=record.queued_at),
            TimelineStepOut(step="Worker started", at=record.started_at),
            TimelineStepOut(step=finished_step, at=record.finished_at),
        ],
        invoice=invoice,
        result=result,
        poll_after_ms=poll,
    )


@router.post("/submissions", status_code=202, response_model=SubmissionOut, responses=_ERRORS)
def create_submission(body: SubmissionRequest, container: Container) -> SubmissionOut:
    service = _service(container)
    record = service.submit(
        session_id=body.session_id,
        sample_invoice_id=body.sample_invoice_id,
        content=body.content,
    )
    return to_out(service, record, detail=False)


@router.get("/submissions/{submission_id}", response_model=SubmissionOut, responses=_ERRORS)
def get_submission(
    submission_id: Annotated[str, Path(pattern=SUBMISSION_ID_PATTERN)], container: Container
) -> SubmissionOut:
    service = _service(container)
    record = service.get(submission_id)
    if record is None:
        raise SubmissionError(404, "submission_not_found", "Submission not found or expired.")
    return to_out(service, record, detail=True)


@router.get("/submissions", response_model=list[SubmissionOut], responses=_ERRORS)
def list_submissions(
    session_id: Annotated[str, Query(pattern=SESSION_ID_PATTERN)],
    container: Container,
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
) -> list[SubmissionOut]:
    service = _service(container)
    return [to_out(service, r, detail=False) for r in service.list_recent(session_id, limit)]


def submission_error_response(error: SubmissionError) -> JSONResponse:
    from invoiceiq.api.middleware import error_response

    return error_response(error.status_code, error.code, error.message)
