"""Worker processing rules under at-least-once delivery (deterministic, fake clock)."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta

import pytest

from invoiceiq.processing.blobs import BlobStoreError
from invoiceiq.processing.messages import MessageLockLostError
from invoiceiq.processing.models import ErrorCode, SubmissionStatus
from invoiceiq.processing.processor import Outcome, RunLimits, run_once
from invoiceiq.processing.store import ConcurrencyConflict, SubmissionStoreError
from tests.processing.conftest import sample_bytes


def receive_one(pipe):
    messages = pipe.queue.receive(1, 0)
    assert len(messages) == 1
    return messages[0]


# --- Happy path and settlement order ------------------------------------------------------------


def test_valid_invoice_is_validated_and_message_completed(pipe):
    record = pipe.submit(sample_bytes("inv-003"))
    pipe.publish(record)
    assert pipe.processor().handle(receive_one(pipe)) == Outcome.COMPLETED

    row = pipe.row(record)
    assert row.status == SubmissionStatus.COMPLETED and row.attempts == 1
    assert row.lease_owner is None and row.lease_expires_at is None
    assert row.invoice_number == "KFS-2026-0203" and row.queued_at and row.finished_at
    payload = json.loads(row.result_json)
    assert payload["result"]["status"] == "Requires Review"
    assert {f["rule_id"] for f in payload["result"]["findings"]} == {"CALC-003", "CALC-004"}
    assert payload["invoice"]["vendor"] == "Kystlinje Facility Services ApS"
    assert pipe.queue.active_count() == 0 and pipe.queue.completed


def test_message_is_completed_only_after_the_result_is_persisted(pipe, monkeypatch):
    record = pipe.submit(sample_bytes())
    pipe.publish(record)
    calls: list[str] = []
    real_replace, real_complete = pipe.store.replace, pipe.queue.complete

    def spy_replace(rec, etag):
        calls.append(f"store:{rec.status}")
        return real_replace(rec, etag)

    def spy_complete(message):
        calls.append("complete")
        return real_complete(message)

    monkeypatch.setattr(pipe.store, "replace", spy_replace)
    monkeypatch.setattr(pipe.queue, "complete", spy_complete)
    pipe.processor().handle(receive_one(pipe))
    assert calls == ["store:Processing", "store:Completed", "complete"]


def test_worker_logs_carry_the_api_correlation_id(pipe):
    import logging

    from invoiceiq.logging_config import correlation_id_var

    seen: dict[str, str] = {}

    class Capture(logging.Handler):
        def emit(self, record):
            seen[record.getMessage()] = correlation_id_var.get()

    record = pipe.submit(sample_bytes())
    pipe.publish(record)
    log = logging.getLogger("invoiceiq.processing.processor")
    handler, level = Capture(), log.level
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        pipe.processor().handle(receive_one(pipe))
    finally:
        log.removeHandler(handler)
        log.setLevel(level)
    assert seen["worker.validated"] == record.correlation_id  # from data.clientRequestId
    assert correlation_id_var.get() == "-"  # reset after the message


def test_queued_time_is_when_the_message_reached_the_queue(pipe):
    record = pipe.submit(sample_bytes())
    pipe.clock.advance(2)  # Event Grid delivery latency
    pipe.publish(record)
    enqueued = pipe.clock()
    pipe.clock.advance(20)  # KEDA polling + job start
    pipe.processor().handle(receive_one(pipe))
    row = pipe.row(record)
    assert row.queued_at == enqueued and row.started_at == enqueued + timedelta(seconds=20)


# --- Duplicates ---------------------------------------------------------------------------------


def test_duplicate_event_after_completion_is_completed_without_reprocessing(pipe):
    record = pipe.submit(sample_bytes())
    event_id = pipe.publish(record)
    pipe.processor().handle(receive_one(pipe))
    before = pipe.store.get(record.submission_id)

    pipe.publish(record, event_id=event_id)  # Event Grid redelivery: same event id
    assert pipe.processor("w2").handle(receive_one(pipe)) == Outcome.DUPLICATE
    after = pipe.store.get(record.submission_id)
    assert after.etag == before.etag  # untouched
    assert pipe.queue.active_count() == 0


# --- Concurrent workers and leases --------------------------------------------------------------


def _held_by_other(pipe, record, *, lease_seconds=60):
    row = pipe.store.get(record.submission_id)
    held = replace(
        row.record,
        status=SubmissionStatus.PROCESSING,
        attempts=1,
        lease_owner="other-worker:lock-x",
        lease_expires_at=pipe.clock() + timedelta(seconds=lease_seconds),
    )
    return pipe.store.replace(held, row.etag)


def test_never_completes_while_another_worker_holds_an_active_lease(pipe):
    record = pipe.submit(sample_bytes())
    pipe.publish(record)
    _held_by_other(pipe, record, lease_seconds=600)  # outlives the contention wait

    outcome = pipe.processor().handle(receive_one(pipe))
    assert outcome == Outcome.CONTENDED
    assert pipe.queue.completed == []  # NOT completed on the other worker's behalf
    assert pipe.queue.active_count() == 1  # abandoned → will be redelivered
    assert pipe.row(record).lease_owner == "other-worker:lock-x"  # claim left alone


def test_waits_for_the_other_worker_and_completes_as_duplicate_when_it_finishes(pipe):
    record = pipe.submit(sample_bytes())
    pipe.publish(record)
    held = _held_by_other(pipe, record)

    def other_worker_finishes():  # runs while our worker waits
        current = pipe.store.get(record.submission_id)
        if current.record.lease_owner == held.record.lease_owner:
            done = replace(current.record, status=SubmissionStatus.COMPLETED, lease_owner=None)
            pipe.store.replace(done, current.etag)

    pipe.clock.on_sleep.append(other_worker_finishes)
    assert pipe.processor().handle(receive_one(pipe)) == Outcome.DUPLICATE
    assert pipe.queue.completed  # completed only after the row became terminal


def test_expired_lease_of_a_crashed_worker_is_taken_over(pipe):
    record = pipe.submit(sample_bytes())
    pipe.publish(record)
    _held_by_other(pipe, record, lease_seconds=60)
    pipe.clock.advance(61)  # the other worker died; its lease expired

    assert pipe.processor().handle(receive_one(pipe)) == Outcome.COMPLETED
    row = pipe.row(record)
    assert row.status == SubmissionStatus.COMPLETED and row.attempts == 2


def test_lease_is_waited_out_within_the_contention_window_then_taken_over(pipe):
    record = pipe.submit(sample_bytes())
    pipe.publish(record)
    _held_by_other(pipe, record, lease_seconds=5)  # expires during our 15 s wait
    assert pipe.processor().handle(receive_one(pipe)) == Outcome.COMPLETED


def test_superseded_claim_stands_down_when_another_worker_owns_the_row(pipe, monkeypatch):
    record = pipe.submit(sample_bytes())
    pipe.publish(record)
    real_replace = pipe.store.replace

    def takeover_before_commit(rec, etag):
        if rec.status == SubmissionStatus.COMPLETED:
            _held_by_other(pipe, record)  # another worker took over (our lease had expired)
            raise ConcurrencyConflict(rec.submission_id)
        return real_replace(rec, etag)

    monkeypatch.setattr(pipe.store, "replace", takeover_before_commit)
    assert pipe.processor().handle(receive_one(pipe)) == Outcome.CONTENDED
    assert pipe.queue.completed == [] and pipe.queue.active_count() == 1


def test_superseded_claim_completes_when_the_other_worker_already_finished(pipe, monkeypatch):
    record = pipe.submit(sample_bytes())
    pipe.publish(record)
    real_replace = pipe.store.replace

    def finished_elsewhere(rec, etag):
        if rec.status == SubmissionStatus.COMPLETED:
            current = pipe.store.get(rec.submission_id)
            pipe.store.force(replace(current.record, status=SubmissionStatus.COMPLETED))
            raise ConcurrencyConflict(rec.submission_id)
        return real_replace(rec, etag)

    monkeypatch.setattr(pipe.store, "replace", finished_elsewhere)
    assert pipe.processor().handle(receive_one(pipe)) == Outcome.DUPLICATE
    assert pipe.queue.active_count() == 0


# --- Locks and long-running work -----------------------------------------------------------------


def test_long_processing_renews_the_lock_and_extends_the_lease(pipe, monkeypatch):
    record = pipe.submit(sample_bytes())
    pipe.publish(record)
    real_download = pipe.blobs.download
    renewals = []
    real_renew = pipe.queue.renew_lock

    def slow_download(name, *, max_bytes):
        pipe.clock.advance(50)  # most of the 60 s lock and lease are used up
        return real_download(name, max_bytes=max_bytes)

    def spy_renew(message):
        renewals.append(pipe.clock())
        return real_renew(message)

    monkeypatch.setattr(pipe.blobs, "download", slow_download)
    monkeypatch.setattr(pipe.queue, "renew_lock", spy_renew)
    assert pipe.processor().handle(receive_one(pipe)) == Outcome.COMPLETED
    assert renewals  # lock renewed before persisting
    assert pipe.queue.active_count() == 0  # completed with the renewed lock


def test_lock_lost_after_persisting_makes_the_redelivery_a_duplicate(pipe, monkeypatch):
    record = pipe.submit(sample_bytes())
    pipe.publish(record)

    def lose_lock(message):
        raise MessageLockLostError("expired")

    monkeypatch.setattr(pipe.queue, "complete", lose_lock)
    assert pipe.processor().handle(receive_one(pipe)) == Outcome.COMPLETED
    assert pipe.row(record).status == SubmissionStatus.COMPLETED
    monkeypatch.undo()

    pipe.clock.advance(61)  # lock expires → message redelivered
    assert pipe.processor("w2").handle(receive_one(pipe)) == Outcome.DUPLICATE
    assert pipe.queue.active_count() == 0


# --- Invalid input: final, never retried --------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"{not json", ErrorCode.INVALID_JSON),
        (b"\xff\xfe", ErrorCode.INVALID_JSON),
        (b"[1, 2]", ErrorCode.INVALID_JSON),
        (b'{"value": NaN}', ErrorCode.INVALID_JSON),
        (b'{"invoice_id": "Bad ID!", "secret_note": "x"}', ErrorCode.INVALID_SCHEMA),
    ],
)
def test_unusable_invoices_fail_once_and_are_not_retried(pipe, data, code):
    record = pipe.submit(data)
    pipe.publish(record)
    assert pipe.processor().handle(receive_one(pipe)) == Outcome.FAILED
    row = pipe.row(record)
    assert row.status == SubmissionStatus.FAILED and row.error_code == code
    assert pipe.queue.active_count() == 0 and not pipe.queue.dead_letters


def test_schema_errors_name_fields_but_never_echo_values(pipe):
    record = pipe.submit(b'{"invoice_id": "Bad ID!", "secret_note": "do-not-echo-123"}')
    pipe.publish(record)
    pipe.processor().handle(receive_one(pipe))
    row = pipe.row(record)
    text = row.error_message + " ".join(row.error_details)
    assert "invoice_id" in text and "secret_note" in text
    assert "Bad ID!" not in text and "do-not-echo-123" not in text


def test_missing_blob_is_a_final_failure(pipe):
    record = pipe.submit(sample_bytes())
    del pipe.blobs.blobs[record.blob_name]  # e.g. removed by retention
    pipe.publish(record)
    assert pipe.processor().handle(receive_one(pipe)) == Outcome.FAILED
    assert pipe.row(record).error_code == ErrorCode.BLOB_NOT_FOUND


def test_oversized_blob_is_rejected_from_the_event_without_downloading(pipe, monkeypatch):
    record = pipe.submit(b"{}" + b" " * 70_000)
    pipe.publish(record)
    monkeypatch.setattr(pipe.blobs, "download", lambda *a, **k: pytest.fail("downloaded"))
    assert pipe.processor().handle(receive_one(pipe)) == Outcome.FAILED
    assert pipe.row(record).error_code == ErrorCode.TOO_LARGE


# --- Transient failures, retries and dead-lettering ---------------------------------------------


def test_transient_error_returns_row_to_pending_and_retries(pipe):
    record = pipe.submit(sample_bytes())
    pipe.publish(record)
    pipe.blobs.fail_next = [BlobStoreError("ServiceRequestError")]

    assert pipe.processor().handle(receive_one(pipe)) == Outcome.RETRY
    row = pipe.row(record)
    assert row.status == SubmissionStatus.PENDING and row.error_code == ErrorCode.TRANSIENT
    assert row.lease_owner is None and row.attempts == 1

    message = receive_one(pipe)  # abandoned → redelivered at once
    assert message.delivery_count == 2
    assert pipe.processor().handle(message) == Outcome.COMPLETED
    row = pipe.row(record)
    assert row.status == SubmissionStatus.COMPLETED and row.attempts == 2
    assert row.error_code is None


def test_permission_failure_is_retried_then_dead_lettered_with_status(pipe):
    record = pipe.submit(sample_bytes())
    pipe.publish(record)
    pipe.blobs.fail_next = [BlobStoreError("ClientAuthenticationError")] * 5

    outcomes = [pipe.processor().handle(receive_one(pipe)) for _ in range(5)]
    assert outcomes == [Outcome.RETRY] * 4 + [Outcome.DEAD_LETTERED]
    row = pipe.row(record)
    assert row.status == SubmissionStatus.DEAD_LETTERED
    assert row.error_code == ErrorCode.RETRIES_EXHAUSTED and row.attempts == 5
    assert [e.dead_letter_reason for e in pipe.queue.dead_letters] == ["processing-failed"]
    assert pipe.queue.active_count() == 0


def test_store_outage_before_claim_retries_and_finally_dead_letters(pipe):
    record = pipe.submit(sample_bytes())
    pipe.publish(record)
    pipe.store.fail_next = [SubmissionStoreError("ServiceRequestError")] * 5
    outcomes = [pipe.processor().handle(receive_one(pipe)) for _ in range(5)]
    assert outcomes[-1] == Outcome.DEAD_LETTERED
    assert pipe.row(record).status == SubmissionStatus.PENDING  # could not be updated


def test_unexpected_bug_takes_the_same_retry_path(pipe, monkeypatch):
    record = pipe.submit(sample_bytes())
    pipe.publish(record)
    monkeypatch.setattr(
        "invoiceiq.processing.processor.validate_invoice",
        lambda invoice: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert pipe.processor().handle(receive_one(pipe)) == Outcome.RETRY
    assert pipe.row(record).status == SubmissionStatus.PENDING


def test_crash_without_settling_leads_to_broker_dead_letter_and_stalled_row(pipe):
    record = pipe.submit(sample_bytes())
    pipe.publish(record)
    for _ in range(5):  # the worker dies after receiving, every time
        receive_one(pipe)
        pipe.clock.advance(61)
    assert pipe.queue.receive(1, 0) == []
    assert pipe.queue.dead_letters[0].dead_letter_reason == "MaxDeliveryCountExceeded"
    assert pipe.row(record).status == SubmissionStatus.PENDING  # API reports it as stalled


# --- Rejected messages ---------------------------------------------------------------------------


def test_foreign_event_is_dead_lettered_without_touching_storage(pipe):
    pipe.queue.send(json.dumps({"eventType": "Something.Else"}), message_id="x")
    assert pipe.processor().handle(receive_one(pipe)) == Outcome.REJECTED
    assert pipe.queue.dead_letters[0].dead_letter_reason == "unexpected-event"


def test_event_for_unknown_submission_is_dead_lettered(pipe):
    record = pipe.submit(sample_bytes())
    body = pipe.event_body(record)
    pipe.store.delete_expired(pipe.clock() + timedelta(days=30), 10)  # row gone
    pipe.queue.send(body, message_id="m")
    assert pipe.processor().handle(receive_one(pipe)) == Outcome.REJECTED
    assert pipe.queue.dead_letters[0].dead_letter_reason == "unknown-submission"


# --- Run loop ------------------------------------------------------------------------------------


def test_run_once_drains_up_to_the_cap_and_cleans_up_expired_rows(pipe):
    old = pipe.submit(sample_bytes())
    pipe.clock.advance(8 * 24 * 3600)  # `old` expired
    records = [pipe.submit(sample_bytes()) for _ in range(3)]
    for r in records:
        pipe.publish(r)
    tally = run_once(pipe.processor(), RunLimits(max_messages=2, budget_seconds=240))
    assert tally[Outcome.COMPLETED] == 2 and pipe.queue.active_count() == 1
    assert tally["expired_rows_deleted"] == 1
    assert pipe.store.get(old.submission_id) is None


def test_run_once_does_not_start_work_that_could_outlive_the_job(pipe):
    pipe.publish(pipe.submit(sample_bytes()))
    tally = run_once(pipe.processor(), RunLimits(budget_seconds=60))  # < lease + margin
    assert sum(v for k, v in tally.items() if k != "expired_rows_deleted") == 0
    assert pipe.queue.active_count() == 1


def test_cleanup_failure_is_ignored(pipe):
    pipe.store.fail_next = [SubmissionStoreError("down")]
    tally = run_once(pipe.processor(), RunLimits())
    assert tally["expired_rows_deleted"] == 0


# --- No AI from the worker -----------------------------------------------------------------------


def test_worker_never_loads_the_ai_package_or_model_sdk():
    code = (
        "import sys, invoiceiq.worker, invoiceiq.processing.processor; "
        "bad = sorted(m for m in sys.modules if m.startswith(('invoiceiq.ai', 'openai'))); "
        "print(','.join(bad))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""
