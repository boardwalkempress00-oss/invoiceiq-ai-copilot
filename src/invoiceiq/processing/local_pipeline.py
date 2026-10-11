"""In-process stand-in for Event Grid + Service Bus + the worker job (local development only).

After an upload, it builds a BlobCreated event exactly as Event Grid would, puts it on an
in-memory peek-lock queue (Service Bus semantics), and a background thread runs the real
`InvoiceProcessor` on it. Everything except the transport is the production code path, so the
Streamlit page shows the same Pending → Processing → Completed sequence as in Azure.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import timedelta

from invoiceiq.processing.blobs import BlobStore
from invoiceiq.processing.events import blob_created_event
from invoiceiq.processing.messages import InMemoryQueue
from invoiceiq.processing.models import SubmissionRecord, utcnow
from invoiceiq.processing.processor import InvoiceProcessor, Outcome, ProcessorConfig
from invoiceiq.processing.store import SubmissionStore

logger = logging.getLogger(__name__)
LOCAL_ACCOUNT_URL = "http://127.0.0.1:10000/devstoreaccount1"


class LocalPipeline:
    def __init__(
        self,
        store: SubmissionStore,
        blobs: BlobStore,
        config: ProcessorConfig,
        *,
        delay_seconds: float = 1.5,
        background: bool = True,
    ) -> None:
        self.config = config
        self.delay_seconds = delay_seconds
        self.background = background
        self.queue = InMemoryQueue(
            lock_duration=timedelta(seconds=config.lease_seconds),
            max_delivery_count=config.max_delivery_count,
        )
        self.processor = InvoiceProcessor(store, blobs, self.queue, config, worker_id="local")
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def on_uploaded(self, record: SubmissionRecord, etag: str) -> None:
        event_id = str(uuid.uuid4())
        event = blob_created_event(
            event_id=event_id,
            account_url=LOCAL_ACCOUNT_URL,
            container=self.config.container,
            blob_name=record.blob_name,
            content_length=record.size_bytes,
            etag=etag,
            client_request_id=record.correlation_id,
            event_time=utcnow(),
        )
        body = json.dumps(event)
        if not self.background:
            self.queue.send(body, message_id=event_id, correlation_id=record.correlation_id)
            return
        timer = threading.Timer(
            self.delay_seconds,
            self.queue.send,
            args=(body,),
            kwargs={"message_id": event_id, "correlation_id": record.correlation_id},
        )
        timer.daemon = True
        timer.start()
        self._ensure_worker()

    def drain(self) -> list[Outcome]:
        """Process everything currently available, synchronously (tests)."""
        outcomes = []
        while messages := self.queue.receive(1, 0):
            outcomes.extend(self.processor.handle(m) for m in messages)
        return outcomes

    def _ensure_worker(self) -> None:
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._loop, name="invoiceiq-local-worker", daemon=True
                )
                self._thread.start()

    def _loop(self) -> None:
        while True:
            try:
                if self.queue.wait_for_message(timeout=5.0):
                    self.drain()
            except Exception:  # keep the simulation alive in development
                logger.exception("local_pipeline.error")
