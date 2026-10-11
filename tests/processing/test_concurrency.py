"""Real threads: concurrent workers, duplicate deliveries and concurrent uploads."""

from __future__ import annotations

import threading
from collections import Counter
from datetime import timedelta

from invoiceiq.processing.blobs import InMemoryBlobStore
from invoiceiq.processing.messages import InMemoryQueue
from invoiceiq.processing.models import SubmissionStatus
from invoiceiq.processing.processor import InvoiceProcessor, Outcome, ProcessorConfig
from invoiceiq.processing.store import InMemorySubmissionStore
from tests.processing.conftest import CONTAINER, HOST, Pipeline, sample_bytes

SUBMISSIONS, DUPLICATES, WORKERS = 6, 3, 4


def test_duplicates_across_concurrent_workers_produce_exactly_one_result_each(clock):
    import time

    from invoiceiq.processing.models import utcnow

    store, blobs = InMemorySubmissionStore(), InMemoryBlobStore()
    queue = InMemoryQueue(lock_duration=timedelta(seconds=30), max_delivery_count=10)
    config = ProcessorConfig(
        container=CONTAINER,
        max_invoice_bytes=65_536,
        expected_blob_host=HOST,
        lease_seconds=10,
        contention_wait_seconds=2,
        poll_seconds=0.005,
        retry_backoff_seconds=(),
    )
    helper = Pipeline(clock, store, blobs, queue, config)
    records = [helper.submit(sample_bytes(f"inv-00{i % 3 + 1}")) for i in range(SUBMISSIONS)]
    for record in records:
        event_id = helper.publish(record)
        for _ in range(DUPLICATES - 1):
            helper.publish(record, event_id=event_id)  # at-least-once: same event, again

    completed_writes: Counter[str] = Counter()
    real_replace = store.replace

    def counting_replace(rec, etag):
        result = real_replace(rec, etag)
        if rec.status == SubmissionStatus.COMPLETED:
            completed_writes[rec.submission_id] += 1
        return result

    store.replace = counting_replace  # type: ignore[method-assign]
    outcomes: Counter[Outcome] = Counter()
    lock = threading.Lock()

    def worker(n: int) -> None:
        processor = InvoiceProcessor(
            store, blobs, queue, config, clock=utcnow, sleep=time.sleep, worker_id=f"w{n}"
        )
        idle = 0
        while idle < 20:
            messages = queue.receive(1, 0)
            if not messages:
                idle += 1
                time.sleep(0.01)
                continue
            idle = 0
            outcome = processor.handle(messages[0])
            with lock:
                outcomes[outcome] += 1

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert all(
        store.snapshot(r.submission_id).status == SubmissionStatus.COMPLETED for r in records
    )
    assert completed_writes == {r.submission_id: 1 for r in records}  # exactly one result each
    assert outcomes[Outcome.COMPLETED] == SUBMISSIONS
    assert outcomes[Outcome.DUPLICATE] == SUBMISSIONS * (DUPLICATES - 1)
    assert queue.active_count() == 0 and not queue.dead_letters


def test_concurrent_upload_reservations_never_exceed_the_allowance():
    store = InMemorySubmissionStore()
    granted: list[bool] = []
    lock = threading.Lock()

    def reserve(n: int) -> None:
        decision = store.reserve_upload("2026-10-11", f"upload-session-{n % 3}", 100, 25)
        with lock:
            granted.append(decision.allowed)

    threads = [threading.Thread(target=reserve, args=(n,)) for n in range(60)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert granted.count(True) == 25
