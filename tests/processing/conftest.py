"""Fixtures for the asynchronous processing tests: fake clock, stores, queue and builders."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from invoiceiq.configuration import PROJECT_ROOT
from invoiceiq.processing.blobs import InMemoryBlobStore
from invoiceiq.processing.events import blob_created_event
from invoiceiq.processing.messages import InMemoryQueue
from invoiceiq.processing.models import (
    SubmissionRecord,
    SubmissionSource,
    SubmissionStatus,
    blob_name_for,
    new_submission_id,
)
from invoiceiq.processing.processor import InvoiceProcessor, ProcessorConfig
from invoiceiq.processing.store import InMemorySubmissionStore

CONTAINER = "invoice-uploads"
ACCOUNT_URL = "https://stinvoiceiqtest.blob.core.windows.net"
HOST = "stinvoiceiqtest.blob.core.windows.net"
INVOICES = PROJECT_ROOT / "data" / "invoices"
T0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    """Deterministic time. `sleep` advances it, so waits and lease expiry need no real time."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start
        self.on_sleep: list = []  # callbacks run on each sleep (simulate other workers)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        for callback in list(self.on_sleep):
            callback()


@dataclass
class Pipeline:
    clock: FakeClock
    store: InMemorySubmissionStore
    blobs: InMemoryBlobStore
    queue: InMemoryQueue
    config: ProcessorConfig
    processors: dict[str, InvoiceProcessor] = field(default_factory=dict)

    def processor(self, worker_id: str = "w1") -> InvoiceProcessor:
        if worker_id not in self.processors:
            self.processors[worker_id] = InvoiceProcessor(
                self.store,
                self.blobs,
                self.queue,
                self.config,
                clock=self.clock,
                sleep=self.clock.sleep,
                worker_id=worker_id,
            )
        return self.processors[worker_id]

    def submit(self, data: bytes, *, session_key: str = "upload-session-x") -> SubmissionRecord:
        """What the API does: Pending row first, then the create-only blob upload."""
        sid = new_submission_id()
        record = SubmissionRecord(
            submission_id=sid,
            status=SubmissionStatus.PENDING,
            source=SubmissionSource.UPLOAD,
            session_key=session_key,
            correlation_id=f"corr{sid[:12]}",
            blob_name=blob_name_for(sid),
            size_bytes=len(data),
            sha256="0" * 64,
            created_at=self.clock(),
            updated_at=self.clock(),
            expires_at=self.clock() + timedelta(days=7),
        )
        self.store.create(record)
        self.blobs.upload_new(
            record.blob_name,
            data,
            content_type="application/json",
            metadata={},
            request_id=record.correlation_id,
        )
        return record

    def event_body(self, record: SubmissionRecord, *, event_id: str | None = None) -> str:
        return json.dumps(
            blob_created_event(
                event_id=event_id or str(uuid.uuid4()),
                account_url=ACCOUNT_URL,
                container=CONTAINER,
                blob_name=record.blob_name,
                content_length=record.size_bytes,
                etag='"0x1"',
                client_request_id=record.correlation_id,
                event_time=self.clock(),
            )
        )

    def publish(self, record: SubmissionRecord, *, event_id: str | None = None) -> str:
        """What Event Grid does: one message per delivery (duplicates keep the same id)."""
        event_id = event_id or str(uuid.uuid4())
        self.queue.send(
            self.event_body(record, event_id=event_id),
            message_id=event_id,
            correlation_id=record.correlation_id,
        )
        return event_id

    def row(self, record: SubmissionRecord) -> SubmissionRecord:
        return self.store.snapshot(record.submission_id)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def pipe(clock: FakeClock) -> Pipeline:
    config = ProcessorConfig(
        container=CONTAINER,
        max_invoice_bytes=65_536,
        expected_blob_host=HOST,
        lease_seconds=60,
        max_delivery_count=5,
        contention_wait_seconds=15,
        poll_seconds=1,
        renew_margin_seconds=15,
        retry_backoff_seconds=(2, 4),
    )
    queue = InMemoryQueue(lock_duration=timedelta(seconds=60), max_delivery_count=5, clock=clock)
    return Pipeline(clock, InMemorySubmissionStore(), InMemoryBlobStore(), queue, config)


def sample_bytes(name: str = "inv-003") -> bytes:
    return (INVOICES / f"{name}.json").read_bytes()
