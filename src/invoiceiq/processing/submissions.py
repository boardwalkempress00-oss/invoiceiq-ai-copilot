"""API-side submission handling: accept an invoice, record it, upload it.

Order matters (it is what makes the asynchronous path race-free):

1. Check size and JSON syntax — invalid files are rejected here; nothing is stored.
2. Take one unit of the *upload* allowance (per session and global, per UTC day).
3. Create the status row as **Pending** — before the blob exists, so every BlobCreated event
   refers to a known submission.
4. Upload the blob create-only, with the correlation ID as client request ID.
   If the upload fails, the row is marked Failed (upload_failed) and the caller gets HTTP 503.

Processing happens elsewhere (the worker); this module never validates invoices itself.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta

from invoiceiq.configuration import Settings
from invoiceiq.core.invoice_service import InvoiceService
from invoiceiq.logging_config import correlation_id_var
from invoiceiq.processing.blobs import BlobStore, BlobStoreError
from invoiceiq.processing.invoice_input import check_json_syntax
from invoiceiq.processing.models import (
    ErrorCode,
    SubmissionRecord,
    SubmissionSource,
    SubmissionStatus,
    blob_name_for,
    is_submission_id,
    new_submission_id,
    upload_session_key,
    utcnow,
)
from invoiceiq.processing.store import ConcurrencyConflict, SubmissionStore, SubmissionStoreError

logger = logging.getLogger(__name__)


class SubmissionError(Exception):
    """A request the API answers with an error status and a user-safe message."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


class SubmissionService:
    def __init__(
        self,
        settings: Settings,
        store: SubmissionStore,
        blobs: BlobStore,
        invoices: InvoiceService,
        *,
        clock: Callable[[], datetime] = utcnow,
        on_uploaded: Callable[[SubmissionRecord, str], None] | None = None,
    ) -> None:
        self.settings, self.store, self.blobs, self.invoices = settings, store, blobs, invoices
        self._clock = clock
        self._on_uploaded = on_uploaded

    def now(self) -> datetime:
        return self._clock()

    # -- submit -----------------------------------------------------------------------------------

    def submit(
        self,
        *,
        session_id: str,
        sample_invoice_id: str | None = None,
        content: str | None = None,
    ) -> SubmissionRecord:
        if (sample_invoice_id is None) == (content is None):
            raise SubmissionError(
                422, "invalid_request", "Provide either a sample invoice or JSON content."
            )
        if sample_invoice_id is not None:
            invoice = self.invoices.get_invoice(sample_invoice_id)  # 404 if unknown
            data = invoice.model_dump_json(indent=2).encode("utf-8")
            source = SubmissionSource.SAMPLE
        else:
            data = (content or "").encode("utf-8")
            source = SubmissionSource.UPLOAD

        problem = check_json_syntax(data, max_bytes=self.settings.submission_max_bytes)
        if problem is not None:
            status = 413 if problem.code == ErrorCode.TOO_LARGE else 422
            raise SubmissionError(status, problem.code.value, problem.message)

        now = self._clock()
        session_key = upload_session_key(session_id)
        try:
            decision = self.store.reserve_upload(
                now.strftime("%Y-%m-%d"),
                session_key,
                self.settings.submission_session_daily_limit,
                self.settings.submission_global_daily_limit,
            )
        except SubmissionStoreError as exc:
            raise self._unavailable(exc) from exc
        if not decision.allowed:
            scope = "this session" if decision.reason == "session_limit" else "the demo"
            raise SubmissionError(
                429,
                "upload_limit_reached",
                f"The daily invoice-processing allowance for {scope} is used up. Try again "
                "tomorrow (UTC). This allowance is separate from AI questions.",
            )

        submission_id = new_submission_id()
        record = SubmissionRecord(
            submission_id=submission_id,
            status=SubmissionStatus.PENDING,
            source=source,
            session_key=session_key,
            correlation_id=correlation_id_var.get(),
            blob_name=blob_name_for(submission_id),
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(days=self.settings.submission_retention_days),
            sample_invoice_id=sample_invoice_id,
        )
        try:
            created = self.store.create(record)
        except SubmissionStoreError as exc:
            raise self._unavailable(exc) from exc

        try:
            etag = self.blobs.upload_new(
                record.blob_name,
                data,
                content_type="application/json",
                metadata={"submissionid": submission_id, "correlationid": record.correlation_id},
                request_id=record.correlation_id,
            )
        except BlobStoreError as exc:
            logger.error("submission.upload_failed", extra={"error": type(exc).__name__})
            failed = replace(
                record,
                status=SubmissionStatus.FAILED,
                updated_at=self._clock(),
                finished_at=self._clock(),
                error_code=ErrorCode.UPLOAD_FAILED,
                error_message="The invoice could not be stored. Please try again.",
            )
            # If this write fails too, the row stays Pending and is reported as stalled.
            with contextlib.suppress(ConcurrencyConflict, SubmissionStoreError):
                self.store.replace(failed, created.etag)
            raise SubmissionError(
                503, "upload_failed", "The invoice could not be stored. Please try again."
            ) from exc

        logger.info(
            "submission.accepted",
            extra={"submission_id": submission_id, "source": source, "size_bytes": len(data)},
        )
        if self._on_uploaded is not None:
            self._on_uploaded(record, etag)
        return record

    # -- read -------------------------------------------------------------------------------------

    def get(self, submission_id: str) -> SubmissionRecord | None:
        if not is_submission_id(submission_id):
            return None
        try:
            row = self.store.get(submission_id)
        except SubmissionStoreError as exc:
            raise self._unavailable(exc) from exc
        if row is None or row.record.expires_at <= self._clock():
            return None  # expired rows are hidden even before cleanup removes them
        return row.record

    def list_recent(self, session_id: str, limit: int = 10) -> list[SubmissionRecord]:
        try:
            rows = self.store.list_for_session(upload_session_key(session_id), limit)
        except SubmissionStoreError as exc:
            raise self._unavailable(exc) from exc
        now = self._clock()
        return [r for r in rows if r.expires_at > now]

    def is_stalled(self, record: SubmissionRecord) -> bool:
        """Not terminal and no progress for a while (e.g. dead-lettered by the broker)."""
        if record.status.terminal:
            return False
        if record.lease_active(self._clock()):
            return False
        age = (self._clock() - record.updated_at).total_seconds()
        return age > self.settings.submission_stalled_after_seconds

    @staticmethod
    def _unavailable(exc: Exception) -> SubmissionError:
        logger.error("submission.store_unavailable", extra={"error": type(exc).__name__})
        return SubmissionError(
            503, "processing_unavailable", "Invoice processing is temporarily unavailable."
        )
