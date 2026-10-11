"""Focused evidence for the delivery guarantees reviewed before Phase B was committed.

1. Message lock expiration while two workers process the same submission.
2. Recoverable leases after a worker crash.
3. Service Bus delivery-count normalisation feeding the final-attempt decision.
4. No message is ever completed before a durable terminal result exists.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from invoiceiq.processing.blobs import BlobStoreError, InMemoryBlobStore
from invoiceiq.processing.messages import AzureServiceBusSource
from invoiceiq.processing.models import SubmissionStatus
from invoiceiq.processing.processor import InvoiceProcessor, Outcome
from invoiceiq.processing.store import SubmissionStoreError
from tests.processing.conftest import sample_bytes


def receive_one(pipe):
    [message] = pipe.queue.receive(1, 0)
    return message


@pytest.fixture
def completion_guard(pipe, monkeypatch):
    """Fail the test if any message is completed while its submission is not terminal."""
    real_complete = pipe.queue.complete
    completions: list[str] = []

    def guarded(message):
        event_sid = message.body.split(b"submissions/")[1][:32].decode()
        row = pipe.store.get(event_sid)
        assert row is not None and row.record.status.terminal, (
            f"completed while submission was {row.record.status if row else 'missing'}"
        )
        completions.append(event_sid)
        return real_complete(message)

    monkeypatch.setattr(pipe.queue, "complete", guarded)
    return completions


def count_terminal_writes(pipe, monkeypatch) -> Counter[str]:
    writes: Counter[str] = Counter()
    real_replace = pipe.store.replace

    def counting(rec, etag):
        result = real_replace(rec, etag)
        if rec.status.terminal:
            writes[rec.submission_id] += 1
        return result

    monkeypatch.setattr(pipe.store, "replace", counting)
    return writes


# --- 1. Lock expiration during concurrent processing --------------------------------------------


def test_lock_expires_mid_processing_and_second_worker_takes_over(
    pipe, monkeypatch, completion_guard
):
    """Worker A stalls past its 60 s lock and lease. Service Bus redelivers to worker B, which
    takes over the expired lease and finishes. A then fails its renewal and its ETag-checked
    write, re-reads the terminal row and stands down. Exactly one result is written."""
    record = pipe.submit(sample_bytes())
    pipe.publish(record)
    writes = count_terminal_writes(pipe, monkeypatch)
    real_download = pipe.blobs.download
    outcome_b: list[Outcome] = []
    stalled = []

    def stall_then_let_b_run(name, *, max_bytes):
        if not stalled:  # only worker A's download stalls
            stalled.append(True)
            pipe.clock.advance(61)  # A's lock and lease both expire while it works
            outcome_b.append(pipe.processor("B").handle(receive_one(pipe)))
        return real_download(name, max_bytes=max_bytes)

    monkeypatch.setattr(pipe.blobs, "download", stall_then_let_b_run)
    outcome_a = pipe.processor("A").handle(receive_one(pipe))

    assert outcome_b == [Outcome.COMPLETED]
    assert outcome_a == Outcome.DUPLICATE  # A's completion fails (lock lost) but is harmless
    assert writes == {record.submission_id: 1}
    row = pipe.row(record)
    assert row.status == SubmissionStatus.COMPLETED and row.attempts == 2
    # Both workers attempted completion only with a terminal row; A's attempt failed (lock lost).
    assert completion_guard == [record.submission_id] * 2
    assert len(pipe.queue.completed) == 1 and pipe.queue.active_count() == 0


def test_lock_expires_while_waiting_on_an_active_lease_is_renewed(pipe, monkeypatch):
    """A worker waiting for another worker's lease keeps renewing its own message lock, so its
    message is not redelivered to a third worker while it waits."""
    record = pipe.submit(sample_bytes())
    pipe.publish(record)
    row = pipe.store.get(record.submission_id)
    pipe.store.replace(
        replace(
            row.record,
            status=SubmissionStatus.PROCESSING,
            lease_owner="other:lock",
            lease_expires_at=pipe.clock() + timedelta(seconds=600),
        ),
        row.etag,
    )
    renewals = []
    real_renew = pipe.queue.renew_lock
    monkeypatch.setattr(pipe.queue, "renew_lock", lambda m: renewals.append(1) or real_renew(m))
    # Make the wait longer than the lock so renewal is required.
    processor = pipe.processor()
    processor.config = replace(processor.config, contention_wait_seconds=90)
    assert processor.handle(receive_one(pipe)) == Outcome.CONTENDED
    assert renewals  # the lock was renewed during the wait
    assert pipe.queue.completed == []


# --- 2. Recoverable leases after a crash ----------------------------------------------------------


class WorkerCrash(BaseException):
    """Simulates the process dying (not an exception the worker can handle)."""


def test_worker_crash_after_claim_is_recovered_by_the_next_delivery(
    pipe, monkeypatch, completion_guard
):
    record = pipe.submit(sample_bytes())
    pipe.publish(record)
    real_download = pipe.blobs.download

    def crash(name, *, max_bytes):
        raise WorkerCrash

    monkeypatch.setattr(pipe.blobs, "download", crash)
    with pytest.raises(WorkerCrash):
        pipe.processor("crashing").handle(receive_one(pipe))
    crashed = pipe.row(record)
    assert crashed.status == SubmissionStatus.PROCESSING and crashed.lease_owner.startswith(
        "crashing:"
    )
    assert pipe.queue.receive(1, 0) == []  # still locked: nobody else can take it yet

    monkeypatch.setattr(pipe.blobs, "download", real_download)
    pipe.clock.advance(61)  # lock and lease expire → Service Bus redelivers
    message = receive_one(pipe)
    assert message.delivery_count == 2
    assert pipe.processor("recovery").handle(message) == Outcome.COMPLETED
    row = pipe.row(record)
    assert row.status == SubmissionStatus.COMPLETED and row.attempts == 2
    assert row.lease_owner is None and completion_guard == [record.submission_id]


def test_repeated_crashes_end_in_the_dead_letter_queue_and_a_stalled_row(pipe, monkeypatch):
    record = pipe.submit(sample_bytes())
    pipe.publish(record)
    monkeypatch.setattr(pipe.blobs, "download", lambda *a, **k: (_ for _ in ()).throw(WorkerCrash))
    for _ in range(5):
        with pytest.raises(WorkerCrash):
            pipe.processor().handle(receive_one(pipe))
        pipe.clock.advance(61)
    assert pipe.queue.receive(1, 0) == []
    assert pipe.queue.dead_letters[0].dead_letter_reason == "MaxDeliveryCountExceeded"
    row = pipe.row(record)
    # Non-terminal with an expired lease → the API reports it as Stalled.
    assert row.status == SubmissionStatus.PROCESSING and not row.lease_active(pipe.clock())


# --- 3. Delivery-count normalisation --------------------------------------------------------------


class _Receiver:
    def __init__(self, raw):
        self.raw, self.settled = raw, []

    def receive_messages(self, max_message_count, max_wait_time):
        return [self.raw]

    def complete_message(self, m):
        self.settled.append("complete")

    def abandon_message(self, m):
        self.settled.append("abandon")

    def dead_letter_message(self, m, reason, error_description):
        self.settled.append(("dead_letter", reason))

    def renew_message_lock(self, m):
        return self.raw.locked_until_utc

    def close(self):
        pass


@pytest.mark.parametrize(
    ("amqp_delivery_count", "expected_settlement"),
    [
        (0, "abandon"),  # first delivery → attempt 1 of 5 → retry
        (3, "abandon"),  # fourth delivery → attempt 4 → retry
        (4, ("dead_letter", "processing-failed")),  # fifth delivery → final attempt
    ],
)
def test_normalised_delivery_count_drives_the_final_attempt(
    pipe, amqp_delivery_count, expected_settlement
):
    """The Azure adapter turns AMQP's 'prior attempts' into a 1-based attempt number, which the
    processor compares with the queue's max delivery count (5)."""
    record = pipe.submit(sample_bytes())
    raw = SimpleNamespace(
        body=iter([pipe.event_body(record).encode()]),
        message_id="m",
        correlation_id=record.correlation_id,
        delivery_count=amqp_delivery_count,
        enqueued_time_utc=pipe.clock(),
        sequence_number=1,
        locked_until_utc=pipe.clock(),
        lock_token="t",
    )
    receiver = _Receiver(raw)
    client = SimpleNamespace(get_queue_receiver=lambda *a, **k: receiver, close=lambda: None)
    source = AzureServiceBusSource(client, "invoice-events")
    failing_blobs = InMemoryBlobStore()
    failing_blobs.fail_next = [BlobStoreError("ServiceRequestError")]
    processor = InvoiceProcessor(
        pipe.store, failing_blobs, source, pipe.config, clock=pipe.clock, sleep=pipe.clock.sleep
    )
    [message] = source.receive(1, 0)
    assert message.delivery_count == amqp_delivery_count + 1
    processor.handle(message)
    assert receiver.settled == [expected_settlement]
    expected_status = (
        SubmissionStatus.DEAD_LETTERED
        if expected_settlement != "abandon"
        else SubmissionStatus.PENDING
    )
    assert pipe.row(record).status == expected_status


# --- 4. Never complete before a durable terminal result -------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (sample_bytes("inv-001"), Outcome.COMPLETED),
        (b"{broken", Outcome.FAILED),
        (b'{"invoice_id": "BAD"}', Outcome.FAILED),
    ],
)
def test_every_completion_is_preceded_by_a_terminal_row(pipe, completion_guard, data, expected):
    record = pipe.submit(data)
    event_id = pipe.publish(record)
    pipe.publish(record, event_id=event_id)  # plus a duplicate delivery
    outcomes = [pipe.processor().handle(receive_one(pipe)) for _ in range(2)]
    assert outcomes == [expected, Outcome.DUPLICATE]
    assert completion_guard == [record.submission_id] * 2


def test_failed_terminal_write_never_completes_the_message(pipe, completion_guard):
    record = pipe.submit(sample_bytes())
    pipe.publish(record)
    real_replace = pipe.store.replace

    def fail_terminal(rec, etag):
        if rec.status.terminal:
            raise SubmissionStoreError("ServiceResponseError")
        return real_replace(rec, etag)

    pipe.store.replace = fail_terminal  # type: ignore[method-assign]
    assert pipe.processor().handle(receive_one(pipe)) == Outcome.RETRY
    assert completion_guard == [] and pipe.queue.active_count() == 1
    assert pipe.row(record).status != SubmissionStatus.COMPLETED
