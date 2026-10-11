"""Submission records and the processing status lifecycle.

    Pending ──claim──▶ Processing ──▶ Completed      (validation ran; findings stored)
       ▲                   │     ├──▶ Failed         (the submitted invoice is unusable; final)
       └──transient error──┘     └──▶ DeadLettered   (retries exhausted; final)

Completed, Failed and DeadLettered are terminal: no transition ever leaves them. Every
transition is a compare-and-set on the row's ETag, so concurrent workers cannot overwrite each
other. "Stalled" is not stored: the API derives it for rows that stopped making progress.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

SUBMISSION_ID_PATTERN = r"^[0-9a-f]{32}$"
_SUBMISSION_ID_RE = re.compile(SUBMISSION_ID_PATTERN)
BLOB_PREFIX = "submissions/"
_BLOB_NAME_RE = re.compile(r"^submissions/([0-9a-f]{32})\.json$")


class SubmissionStatus(StrEnum):
    PENDING = "Pending"
    PROCESSING = "Processing"
    COMPLETED = "Completed"
    FAILED = "Failed"
    DEAD_LETTERED = "DeadLettered"

    @property
    def terminal(self) -> bool:
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES = frozenset(
    {SubmissionStatus.COMPLETED, SubmissionStatus.FAILED, SubmissionStatus.DEAD_LETTERED}
)


class ErrorCode(StrEnum):
    # Problems with the submitted invoice: final, never retried.
    INVALID_JSON = "invalid_json"
    INVALID_SCHEMA = "invalid_schema"
    TOO_LARGE = "too_large"
    BLOB_NOT_FOUND = "blob_not_found"
    # Problems with the platform.
    UPLOAD_FAILED = "upload_failed"  # the API could not store the blob (final)
    TRANSIENT = "transient_error"  # last error of an attempt that will be retried
    RETRIES_EXHAUSTED = "retries_exhausted"  # moved to the dead-letter queue (final)


class SubmissionSource(StrEnum):
    SAMPLE = "sample"
    UPLOAD = "upload"


@dataclass(frozen=True)
class SubmissionRecord:
    submission_id: str
    status: SubmissionStatus
    source: SubmissionSource
    session_key: str
    correlation_id: str
    blob_name: str
    size_bytes: int
    sha256: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    sample_invoice_id: str | None = None
    attempts: int = 0
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    invoice_id: str | None = None
    invoice_number: str | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    error_details: tuple[str, ...] = ()
    # Completed only: {"result": ValidationResult, "invoice": summary}, as JSON text.
    result_json: str | None = None
    engine_version: str | None = None
    event_id: str | None = None
    message_id: str | None = None

    def lease_active(self, now: datetime) -> bool:
        return (
            self.status == SubmissionStatus.PROCESSING
            and self.lease_expires_at is not None
            and self.lease_expires_at > now
        )


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_submission_id() -> str:
    return uuid.uuid4().hex


def is_submission_id(value: str) -> bool:
    return bool(_SUBMISSION_ID_RE.match(value))


def blob_name_for(submission_id: str) -> str:
    if not is_submission_id(submission_id):
        raise ValueError("invalid submission id")
    return f"{BLOB_PREFIX}{submission_id}.json"


def submission_id_from_blob(blob_name: str) -> str | None:
    match = _BLOB_NAME_RE.match(blob_name)
    return match.group(1) if match else None


def upload_session_key(session_id: str) -> str:
    """Per-session key for the upload allowance. A separate namespace from the AI allowance."""
    return "upload-session-" + hashlib.sha256(session_id.encode()).hexdigest()[:32]
