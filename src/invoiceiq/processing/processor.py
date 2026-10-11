"""Idempotent processing of BlobCreated messages (at-least-once delivery).

Rules, in the order the worker applies them to every message:

1. **Verify the event** (`events.py`). Anything unexpected is dead-lettered as
   `unexpected-event`; storage is not touched.
2. **Read the status row.** A terminal row (Completed / Failed / DeadLettered) means the work is
   already done: the message is a duplicate and is completed.
3. **Claim with a lease.** The worker moves the row to Processing with `lease_owner` and
   `lease_expires_at` (compare-and-set on the ETag). If *another* worker holds an unexpired lease,
   this worker does **not** complete its message: it waits (renewing its message lock) until the
   row becomes terminal (→ complete as duplicate) or the lease expires (→ take over). If neither
   happens within `contention_wait_seconds`, it abandons the message so it is redelivered later.
   A crashed worker's lease simply expires and the next delivery takes over.
4. **Process.** Download (size-capped), parse, validate with the existing deterministic engine.
   Before writing, the lease (and message lock) are extended if they are about to run out.
5. **Persist, then settle.** The terminal row is written with the ETag of the claim. Only after
   that write succeeds is the message completed. If the claim was superseded (ETag changed), the
   worker re-reads: a terminal row means another worker finished (complete); otherwise it
   abandons. A lost message lock after persisting is harmless — the redelivery is a duplicate.
6. **Failures.** Problems with the invoice itself are final (`Failed`). Platform errors are
   retried: the row returns to Pending with the error, the message is abandoned after a short
   backoff, and on the last allowed delivery the row becomes DeadLettered and the message is
   dead-lettered explicitly. If the worker dies before that, Service Bus dead-letters the message
   after `max_delivery_count` and the API reports the row as stalled.

Validation is a pure function of the stored blob, so processing twice yields the same result.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import socket
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from invoiceiq.core.invoice_validator import validate_invoice
from invoiceiq.logging_config import correlation_id_var
from invoiceiq.processing.blobs import (
    BlobNotFoundError,
    BlobStore,
    BlobStoreError,
    BlobTooLargeError,
)
from invoiceiq.processing.events import BlobCreatedEvent, InvalidEventError, parse_blob_created
from invoiceiq.processing.invoice_input import InputProblem, invoice_summary, parse_invoice
from invoiceiq.processing.messages import (
    MessageLockLostError,
    MessageSource,
    MessageSourceError,
    QueueMessage,
)
from invoiceiq.processing.models import (
    ErrorCode,
    SubmissionRecord,
    SubmissionStatus,
    utcnow,
)
from invoiceiq.processing.store import (
    ConcurrencyConflict,
    SubmissionStore,
    SubmissionStoreError,
    Versioned,
)

logger = logging.getLogger(__name__)
_SAFE_CORRELATION = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_MAX_CLAIM_CONFLICTS = 20  # waiting is bounded by time; this bounds write races


class Outcome(StrEnum):
    COMPLETED = "completed"  # validated and stored; message completed
    FAILED = "failed"  # invoice unusable; final status stored; message completed
    DUPLICATE = "duplicate"  # row already terminal; message completed
    CONTENDED = "contended"  # another worker still holds the lease; message abandoned
    RETRY = "retry"  # transient error; row back to Pending; message abandoned
    DEAD_LETTERED = "dead_lettered"  # retries exhausted; row DeadLettered; message dead-lettered
    REJECTED = "rejected"  # not a valid event / unknown submission; message dead-lettered
    UNSETTLED = "unsettled"  # settlement failed; the lock will expire and redeliver


@dataclass(frozen=True)
class ProcessorConfig:
    container: str
    max_invoice_bytes: int
    expected_blob_host: str | None = None
    lease_seconds: float = 60.0
    max_delivery_count: int = 5
    contention_wait_seconds: float = 15.0
    poll_seconds: float = 1.0
    renew_margin_seconds: float = 15.0
    retry_backoff_seconds: tuple[float, ...] = (2.0, 4.0, 8.0, 10.0)


def default_worker_id() -> str:
    return f"{socket.gethostname()[:40]}-{os.getpid()}-{secrets.token_hex(3)}"


class InvoiceProcessor:
    def __init__(
        self,
        store: SubmissionStore,
        blobs: BlobStore,
        source: MessageSource,
        config: ProcessorConfig,
        *,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        worker_id: str | None = None,
    ) -> None:
        self.store, self.blobs, self.source, self.config = store, blobs, source, config
        self._clock, self._sleep = clock, sleep
        self.worker_id = worker_id or default_worker_id()
        self._lock_until: dict[str, datetime | None] = {}

    def now(self) -> datetime:
        return self._clock()

    # -- entry point ------------------------------------------------------------------------------

    def handle(self, message: QueueMessage) -> Outcome:
        token = correlation_id_var.set(self._safe_correlation(message.correlation_id))
        self._lock_until[message.lock_token] = message.locked_until
        try:
            outcome = self._handle(message)
        except Exception:  # last line of defence: never crash the run loop
            logger.exception("worker.unexpected_error", extra=self._msg_fields(message))
            outcome = self._abandon(message, Outcome.UNSETTLED)
        finally:
            self._lock_until.pop(message.lock_token, None)
            correlation_id_var.reset(token)
        logger.info(
            "worker.message_handled", extra={**self._msg_fields(message), "outcome": outcome}
        )
        return outcome

    def _handle(self, message: QueueMessage) -> Outcome:
        try:
            event = parse_blob_created(
                message.body,
                container=self.config.container,
                expected_host=self.config.expected_blob_host,
            )
        except InvalidEventError as exc:
            logger.warning("worker.event_rejected", extra={"reason": str(exc)})
            return self._dead_letter(message, "unexpected-event", str(exc), Outcome.REJECTED)
        if event.client_request_id:
            correlation_id_var.set(self._safe_correlation(event.client_request_id))

        lease_owner = f"{self.worker_id}:{message.lock_token}"
        try:
            claimed = self._claim(event, message, lease_owner)
        except SubmissionStoreError as exc:
            return self._transient(message, None, exc)
        if isinstance(claimed, Outcome):
            return claimed
        try:
            return self._process(event, message, claimed)
        except (SubmissionStoreError, BlobStoreError, MessageSourceError) as exc:
            return self._transient(message, claimed, exc)
        except Exception as exc:  # a bug, not an outage: same retry/dead-letter path, logged
            logger.exception("worker.processing_bug")
            return self._transient(message, claimed, exc)

    # -- claim ------------------------------------------------------------------------------------

    def _claim(
        self, event: BlobCreatedEvent, message: QueueMessage, lease_owner: str
    ) -> Versioned | Outcome:
        give_up_at = self._clock() + timedelta(seconds=self.config.contention_wait_seconds)
        conflicts = 0
        while conflicts < _MAX_CLAIM_CONFLICTS:
            current = self.store.get(event.submission_id)
            if current is None:
                logger.warning("worker.unknown_submission")
                return self._dead_letter(
                    message, "unknown-submission", "No status row for this blob.", Outcome.REJECTED
                )
            row = current.record
            if row.status.terminal:
                logger.info("worker.duplicate", extra={"status": row.status})
                return self._complete(message, Outcome.DUPLICATE)
            now = self._clock()
            if row.lease_active(now) and row.lease_owner != lease_owner:
                # Another worker is processing this submission right now. Never complete our
                # message on its behalf: wait for it to finish or for its lease to expire.
                if now >= give_up_at:
                    logger.info("worker.contended", extra={"lease_owner": row.lease_owner})
                    return self._abandon(message, Outcome.CONTENDED)
                self._keep_lock(message)
                self._sleep(self.config.poll_seconds)
                continue
            if row.status == SubmissionStatus.PROCESSING:
                logger.info("worker.lease_takeover", extra={"previous_owner": row.lease_owner})
            claim = replace(
                row,
                status=SubmissionStatus.PROCESSING,
                attempts=row.attempts + 1,
                lease_owner=lease_owner,
                lease_expires_at=now + timedelta(seconds=self.config.lease_seconds),
                started_at=now,
                updated_at=now,
                # When the event reached Service Bus (eventTime is when the blob was created).
                queued_at=row.queued_at or message.enqueued_time or event.event_time,
                event_id=event.event_id,
                message_id=message.message_id,
            )
            try:
                claimed = self.store.replace(claim, current.etag)
            except ConcurrencyConflict:
                conflicts += 1
                continue  # someone else wrote first; re-read and decide again
            logger.info("worker.claimed", extra={"attempt": claim.attempts})
            return claimed
        return self._abandon(message, Outcome.CONTENDED)

    # -- process ----------------------------------------------------------------------------------

    def _process(
        self, event: BlobCreatedEvent, message: QueueMessage, claimed: Versioned
    ) -> Outcome:
        row = claimed.record
        limit = self.config.max_invoice_bytes
        if event.content_length is not None and event.content_length > limit:
            problem = InputProblem(ErrorCode.TOO_LARGE, "The invoice file is too large.")
            return self._finish_failed(message, claimed, problem)
        try:
            data = self.blobs.download(row.blob_name, max_bytes=limit)
        except BlobNotFoundError:
            problem = InputProblem(
                ErrorCode.BLOB_NOT_FOUND, "The uploaded file no longer exists (retention)."
            )
            return self._finish_failed(message, claimed, problem)
        except BlobTooLargeError:
            problem = InputProblem(ErrorCode.TOO_LARGE, "The invoice file is too large.")
            return self._finish_failed(message, claimed, problem)

        parsed = parse_invoice(data, max_bytes=limit)
        if isinstance(parsed, InputProblem):
            return self._finish_failed(message, claimed, parsed)

        started = time.perf_counter()
        result = validate_invoice(parsed)
        claimed = self._extend_if_needed(claimed, message)
        now = self._clock()
        done = replace(
            claimed.record,
            status=SubmissionStatus.COMPLETED,
            finished_at=now,
            updated_at=now,
            lease_owner=None,
            lease_expires_at=None,
            invoice_id=parsed.invoice_id,
            invoice_number=parsed.invoice_number,
            error_code=None,
            error_message=None,
            error_details=(),
            engine_version=result.engine_version,
            result_json=json.dumps(
                {"result": result.model_dump(mode="json"), "invoice": invoice_summary(parsed)},
                separators=(",", ":"),
            ),
        )
        logger.info(
            "worker.validated",
            extra={
                "invoice_status": result.status,
                "findings": len(result.findings),
                "validation_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return self._commit(message, claimed, done, Outcome.COMPLETED)

    def _finish_failed(
        self, message: QueueMessage, claimed: Versioned, problem: InputProblem
    ) -> Outcome:
        now = self._clock()
        failed = replace(
            claimed.record,
            status=SubmissionStatus.FAILED,
            finished_at=now,
            updated_at=now,
            lease_owner=None,
            lease_expires_at=None,
            error_code=problem.code,
            error_message=problem.message,
            error_details=problem.details,
        )
        logger.info("worker.invoice_rejected", extra={"error_code": problem.code})
        return self._commit(message, claimed, failed, Outcome.FAILED)

    def _commit(
        self,
        message: QueueMessage,
        claimed: Versioned,
        final_row: SubmissionRecord,
        outcome: Outcome,
    ) -> Outcome:
        try:
            self.store.replace(final_row, claimed.etag)
        except ConcurrencyConflict:
            # Our claim was superseded (lease expired and another worker took over).
            current = self.store.get(claimed.record.submission_id)
            if current is not None and current.record.status.terminal:
                return self._complete(message, Outcome.DUPLICATE)
            logger.info("worker.claim_superseded")
            return self._abandon(message, Outcome.CONTENDED)
        # The result is durable: only now may the message be removed from the queue.
        return self._complete(message, outcome)

    def _extend_if_needed(self, claimed: Versioned, message: QueueMessage) -> Versioned:
        """Long-running safety: keep both the message lock and the row lease ahead of the work."""
        self._keep_lock(message)
        row = claimed.record
        now = self._clock()
        margin = timedelta(seconds=self.config.renew_margin_seconds)
        if row.lease_expires_at is None or row.lease_expires_at - now > margin:
            return claimed
        extended = replace(
            row,
            lease_expires_at=now + timedelta(seconds=self.config.lease_seconds),
            updated_at=now,
        )
        try:
            return self.store.replace(extended, claimed.etag)
        except ConcurrencyConflict:
            return claimed  # superseded; `_commit` will detect it and stand down

    def _keep_lock(self, message: QueueMessage) -> None:
        locked_until = self._lock_until.get(message.lock_token)
        if locked_until is None:
            return
        remaining = (locked_until - self._clock()).total_seconds()
        if remaining > self.config.renew_margin_seconds:
            return
        try:
            self._lock_until[message.lock_token] = self.source.renew_lock(message)
        except MessageLockLostError:
            # The message will be redelivered. Work already persisted stays valid; anything
            # still in progress is safe to repeat.
            logger.warning("worker.lock_lost", extra=self._msg_fields(message))
            self._lock_until[message.lock_token] = None

    # -- failures ---------------------------------------------------------------------------------

    def _transient(
        self, message: QueueMessage, claimed: Versioned | None, exc: Exception
    ) -> Outcome:
        error = type(exc).__name__
        attempt = message.delivery_count
        last = attempt >= self.config.max_delivery_count
        logger.warning(
            "worker.transient_error",
            extra={"error": error, "delivery_count": attempt, "final_attempt": last},
        )
        if last:
            if claimed is not None:
                now = self._clock()
                self._best_effort(
                    claimed,
                    status=SubmissionStatus.DEAD_LETTERED,
                    finished_at=now,
                    updated_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                    error_code=ErrorCode.RETRIES_EXHAUSTED,
                    error_message=f"Processing failed after {attempt} attempts ({error}).",
                )
            return self._dead_letter(message, "processing-failed", error, Outcome.DEAD_LETTERED)
        if claimed is not None:
            self._best_effort(
                claimed,
                status=SubmissionStatus.PENDING,
                updated_at=self._clock(),
                lease_owner=None,
                lease_expires_at=None,
                error_code=ErrorCode.TRANSIENT,
                error_message=f"Attempt {attempt} failed ({error}); retrying.",
            )
        backoff = self.config.retry_backoff_seconds
        if backoff:
            self._keep_lock(message)
            self._sleep(backoff[min(max(attempt, 1), len(backoff)) - 1])
        return self._abandon(message, Outcome.RETRY)

    def _best_effort(self, claimed: Versioned, **changes: object) -> None:
        try:
            self.store.replace(replace(claimed.record, **changes), claimed.etag)  # type: ignore[arg-type]
        except SubmissionStoreError as exc:
            logger.warning("worker.status_write_failed", extra={"error": type(exc).__name__})

    # -- settlement -------------------------------------------------------------------------------

    def _complete(self, message: QueueMessage, outcome: Outcome) -> Outcome:
        try:
            self.source.complete(message)
        except MessageSourceError as exc:
            # Lock lost or broker unavailable: the message comes back, and the terminal row makes
            # the redelivery a harmless duplicate.
            logger.warning("worker.complete_failed", extra={"error": type(exc).__name__})
        return outcome

    def _abandon(self, message: QueueMessage, outcome: Outcome) -> Outcome:
        try:
            self.source.abandon(message)
        except MessageSourceError as exc:
            logger.warning("worker.abandon_failed", extra={"error": type(exc).__name__})
        return outcome

    def _dead_letter(
        self, message: QueueMessage, reason: str, description: str, outcome: Outcome
    ) -> Outcome:
        try:
            self.source.dead_letter(message, reason, description[:200])
        except MessageSourceError as exc:
            logger.warning("worker.dead_letter_failed", extra={"error": type(exc).__name__})
            return Outcome.UNSETTLED
        return outcome

    # -- helpers ----------------------------------------------------------------------------------

    @staticmethod
    def _safe_correlation(value: str | None) -> str:
        return value if value and _SAFE_CORRELATION.match(value) else "-"

    @staticmethod
    def _msg_fields(message: QueueMessage) -> dict[str, object]:
        return {
            "message_id": message.message_id,
            "sequence_number": message.sequence_number,
            "delivery_count": message.delivery_count,
        }


# -- run loop ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunLimits:
    max_messages: int = 20
    receive_wait_seconds: float = 5.0
    # Must stay below the Container Apps Job replica timeout; no new message is taken when less
    # than one lease (+ margin) of budget remains.
    budget_seconds: float = 240.0
    cleanup_limit: int = 50


def run_once(
    processor: InvoiceProcessor,
    limits: RunLimits,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> Counter[str]:
    """One job execution: drain up to `max_messages`, then tidy up opportunistically."""
    started = monotonic()
    reserve = processor.config.lease_seconds + processor.config.renew_margin_seconds
    tally: Counter[str] = Counter()
    while sum(tally.values()) < limits.max_messages:
        remaining = limits.budget_seconds - (monotonic() - started)
        if remaining < reserve:
            logger.info("worker.budget_reached", extra={"remaining_seconds": round(remaining, 1)})
            break
        try:
            messages = processor.source.receive(1, min(limits.receive_wait_seconds, remaining))
        except MessageSourceError as exc:
            logger.error("worker.receive_failed", extra={"error": type(exc).__name__})
            break
        if not messages:
            break
        for message in messages:
            tally[processor.handle(message)] += 1
    # Opportunistic, bounded cleanup of expired status rows. Not guaranteed: if no job runs, or
    # this fails, rows stay until a later run. Blob deletion is guaranteed separately by the
    # storage lifecycle policy.
    try:
        tally["expired_rows_deleted"] += processor.store.delete_expired(
            processor.now(), limits.cleanup_limit
        )
    except SubmissionStoreError as exc:
        logger.warning("worker.cleanup_failed", extra={"error": type(exc).__name__})
    logger.info("worker.run_summary", extra={"outcomes": dict(tally)})
    return tally
