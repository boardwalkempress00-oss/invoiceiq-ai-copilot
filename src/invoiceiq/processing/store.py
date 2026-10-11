"""Persistence for submission status and the public upload allowance.

Every write is conditional: `create` fails if the row exists, `replace` fails unless the caller
holds the current ETag. The processing rules (who may move a submission to which state) live in
`processor.py`; stores only guarantee that a write never silently overwrites a newer one.

The upload allowance is deliberately independent of the AI chat allowance: separate counters,
separate settings, separate storage rows. Uploading an invoice never uses up AI questions.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from invoiceiq.processing.models import SubmissionRecord


class SubmissionStoreError(RuntimeError):
    """The store could not be reached or answered unexpectedly (retryable)."""


class ConcurrencyConflict(SubmissionStoreError):
    """The row changed since it was read (ETag mismatch) or no longer exists."""


class SubmissionExistsError(SubmissionStoreError):
    pass


@dataclass(frozen=True)
class Versioned:
    record: SubmissionRecord
    etag: str


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    session_remaining: int
    global_remaining: int
    reason: str | None = None  # "session_limit" | "global_limit"


class SubmissionStore(Protocol):
    def create(self, record: SubmissionRecord) -> Versioned: ...

    def get(self, submission_id: str) -> Versioned | None: ...

    def replace(self, record: SubmissionRecord, etag: str) -> Versioned: ...

    def list_for_session(self, session_key: str, limit: int) -> list[SubmissionRecord]: ...

    def delete_expired(self, now: datetime, limit: int) -> int: ...

    def reserve_upload(
        self, day: str, session_key: str, session_limit: int, global_limit: int
    ) -> QuotaDecision: ...


def decide_quota(
    session_used: int, global_used: int, session_limit: int, global_limit: int
) -> QuotaDecision:
    """Whether one more upload fits; remaining counts assume it is accepted."""
    if global_used >= global_limit:
        return QuotaDecision(False, max(session_limit - session_used, 0), 0, "global_limit")
    if session_used >= session_limit:
        return QuotaDecision(False, 0, global_limit - global_used, "session_limit")
    return QuotaDecision(
        True, session_limit - session_used - 1, global_limit - global_used - 1, None
    )


class InMemorySubmissionStore:
    """Thread-safe, process-local store for tests and local development."""

    def __init__(self) -> None:
        self._rows: dict[str, Versioned] = {}
        self._quota: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()
        self._version = 0
        self.fail_next: list[Exception] = []  # test hook: raise these on the next calls

    def _maybe_fail(self) -> None:
        if self.fail_next:
            raise self.fail_next.pop(0)

    def _etag(self) -> str:
        self._version += 1
        return f'W/"{self._version}"'

    def create(self, record: SubmissionRecord) -> Versioned:
        with self._lock:
            self._maybe_fail()
            if record.submission_id in self._rows:
                raise SubmissionExistsError(record.submission_id)
            row = Versioned(record, self._etag())
            self._rows[record.submission_id] = row
            return row

    def get(self, submission_id: str) -> Versioned | None:
        with self._lock:
            self._maybe_fail()
            return self._rows.get(submission_id)

    def replace(self, record: SubmissionRecord, etag: str) -> Versioned:
        with self._lock:
            self._maybe_fail()
            current = self._rows.get(record.submission_id)
            if current is None or current.etag != etag:
                raise ConcurrencyConflict(record.submission_id)
            row = Versioned(record, self._etag())
            self._rows[record.submission_id] = row
            return row

    def list_for_session(self, session_key: str, limit: int) -> list[SubmissionRecord]:
        with self._lock:
            self._maybe_fail()
            rows = [v.record for v in self._rows.values() if v.record.session_key == session_key]
        return sorted(rows, key=lambda r: r.created_at, reverse=True)[:limit]

    def delete_expired(self, now: datetime, limit: int) -> int:
        with self._lock:
            self._maybe_fail()
            expired = [k for k, v in self._rows.items() if v.record.expires_at <= now][:limit]
            for key in expired:
                del self._rows[key]
            return len(expired)

    def reserve_upload(
        self, day: str, session_key: str, session_limit: int, global_limit: int
    ) -> QuotaDecision:
        with self._lock:
            self._maybe_fail()
            used_s = self._quota.get((day, session_key), 0)
            used_g = self._quota.get((day, "global"), 0)
            decision = decide_quota(used_s, used_g, session_limit, global_limit)
            if decision.allowed:
                self._quota[(day, session_key)] = used_s + 1
                self._quota[(day, "global")] = used_g + 1
            return decision

    # -- test helpers ---------------------------------------------------------------------------

    def force(self, record: SubmissionRecord) -> None:
        """Overwrite a row unconditionally (tests only)."""
        with self._lock:
            self._rows[record.submission_id] = Versioned(record, self._etag())

    def snapshot(self, submission_id: str) -> SubmissionRecord:
        with self._lock:
            return replace(self._rows[submission_id].record)
