"""Public demo: process a predefined fictional scenario with the real code, entirely in memory.

What is real: the status lifecycle, the Event Grid–shaped `BlobCreated` event, peek-lock queue
semantics, the idempotent `InvoiceProcessor` and the deterministic validation engine — the same
code the Azure worker runs. What is simulated: Blob Storage, Event Grid, Service Bus and Table
Storage are in-memory stand-ins, and nothing waits for a worker to start from zero.

This module imports only in-memory implementations. It never constructs an Azure client, so the
demo cannot read or write Blob Storage, Service Bus or Table Storage, whatever else is configured.
Visitors choose from a fixed catalog of scenarios; there is no upload path.

Memory is bounded: at most `max_runs` demo runs are kept, each for at most `ttl` seconds, and
blobs are dropped as soon as a run finishes.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from invoiceiq.configuration import Settings
from invoiceiq.processing.blobs import InMemoryBlobStore
from invoiceiq.processing.events import blob_created_event
from invoiceiq.processing.messages import InMemoryQueue
from invoiceiq.processing.models import (
    SubmissionRecord,
    SubmissionSource,
    SubmissionStatus,
    blob_name_for,
    new_submission_id,
    upload_session_key,
    utcnow,
)
from invoiceiq.processing.processor import InvoiceProcessor, ProcessorConfig
from invoiceiq.processing.store import InMemorySubmissionStore, QuotaDecision, Versioned

DEMO_CONTAINER = "demo-in-memory"
DEMO_ACCOUNT_URL = "http://demo.invalid/in-memory"
_SCENARIO_ID = re.compile(r"^demo-[a-z0-9-]{1,40}$")
PROCESSING_MODE = "simulated"
PROVENANCE = (
    "Simulated in this container: the real validation and processing code ran with in-memory "
    "stand-ins for Blob Storage, Event Grid, Service Bus and Table Storage. No Azure service "
    "was used."
)


class DemoError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


@dataclass(frozen=True)
class DemoScenario:
    scenario_id: str
    title: str
    story: str
    expected_status: str
    expected_rules: tuple[str, ...]
    invoice_number: str | None
    vendor: str | None
    data: bytes


class DemoCatalog:
    """The fixed, reviewed set of fictional scenarios (data/demo-scenarios)."""

    def __init__(self, scenarios: list[DemoScenario]) -> None:
        self._by_id = {s.scenario_id: s for s in scenarios}

    @classmethod
    def load(cls, directory: Path) -> DemoCatalog:
        index = json.loads((directory / "catalog.json").read_text(encoding="utf-8"))
        scenarios = []
        for item in index["scenarios"]:
            sid = item["scenario_id"]
            if not _SCENARIO_ID.match(sid) or Path(item["file"]).name != item["file"]:
                raise ValueError(f"invalid scenario entry: {sid!r}")
            data = (directory / item["file"]).read_bytes()
            invoice = json.loads(data)
            scenarios.append(
                DemoScenario(
                    scenario_id=sid,
                    title=item["title"],
                    story=item["story"],
                    expected_status=item["expected_status"],
                    expected_rules=tuple(item["expected_rules"]),
                    invoice_number=invoice.get("invoice_number"),
                    vendor=(invoice.get("vendor") or {}).get("name"),
                    data=data,
                )
            )
        return cls(scenarios)

    def list(self) -> list[DemoScenario]:
        return list(self._by_id.values())

    def get(self, scenario_id: str) -> DemoScenario | None:
        return self._by_id.get(scenario_id)


class BoundedSubmissionStore(InMemorySubmissionStore):
    """In-memory store with a hard cap on rows and an expiry; old quota days are dropped."""

    def __init__(self, *, capacity: int, clock: Callable[[], datetime] = utcnow) -> None:
        super().__init__()
        self.capacity = capacity
        self._clock = clock
        self._order: OrderedDict[str, None] = OrderedDict()
        self._evict_lock = threading.Lock()

    def create(self, record: SubmissionRecord) -> Versioned:
        with self._evict_lock:
            self.delete_expired(self._clock(), limit=self.capacity)
            for sid in list(self._order):
                if sid not in self._rows:
                    self._order.pop(sid, None)
            while len(self._rows) >= self.capacity and self._order:
                oldest, _ = self._order.popitem(last=False)
                with self._lock:
                    self._rows.pop(oldest, None)
            row = super().create(record)
            self._order[record.submission_id] = None
            return row

    def reserve_upload(
        self, day: str, session_key: str, session_limit: int, global_limit: int
    ) -> QuotaDecision:
        with self._lock:  # only today's counters are kept
            for key in [k for k in self._quota if k[0] != day]:
                del self._quota[key]
        return super().reserve_upload(day, session_key, session_limit, global_limit)

    def __len__(self) -> int:
        return len(self._rows)


@dataclass(frozen=True)
class DemoRun:
    record: SubmissionRecord
    scenario: DemoScenario


class DemoService:
    def __init__(
        self,
        settings: Settings,
        catalog: DemoCatalog,
        reference: dict[str, Any],
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.settings, self.catalog, self.reference = settings, catalog, reference
        self._clock = clock
        self.store = BoundedSubmissionStore(capacity=settings.demo_max_runs_in_memory, clock=clock)
        self.blobs = InMemoryBlobStore()
        self.queue = InMemoryQueue(lock_duration=timedelta(seconds=60), max_delivery_count=5)
        self._processor = InvoiceProcessor(
            self.store,
            self.blobs,
            self.queue,
            ProcessorConfig(container=DEMO_CONTAINER, max_invoice_bytes=65_536),
            clock=clock,
            worker_id="demo",
        )
        self._run_lock = threading.Lock()  # one demo run at a time: tiny, CPU-only work

    @classmethod
    def from_settings(cls, settings: Settings) -> DemoService:
        catalog = DemoCatalog.load(settings.demo_scenarios_dir)
        reference = json.loads(settings.production_reference_path.read_text(encoding="utf-8"))
        return cls(settings, catalog, reference)

    def now(self) -> datetime:
        return self._clock()

    def run(self, *, session_id: str, scenario_id: str) -> DemoRun:
        scenario = self.catalog.get(scenario_id)
        if scenario is None:
            raise DemoError(404, "scenario_not_found", "Unknown demo scenario.")
        now = self._clock()
        session_key = "demo-" + upload_session_key(session_id)
        decision = self.store.reserve_upload(
            now.strftime("%Y-%m-%d"),
            session_key,
            self.settings.demo_session_daily_limit,
            self.settings.demo_global_daily_limit,
        )
        if not decision.allowed:
            raise DemoError(
                429, "demo_limit_reached", "The demo's daily allowance is used up. Try tomorrow."
            )
        sid = new_submission_id()
        record = SubmissionRecord(
            submission_id=sid,
            status=SubmissionStatus.PENDING,
            source=SubmissionSource.DEMO,
            session_key=session_key,
            correlation_id=f"demo-{uuid.uuid4().hex[:24]}",
            blob_name=blob_name_for(sid),
            size_bytes=len(scenario.data),
            sha256="demo",
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(seconds=self.settings.demo_run_ttl_seconds),
            sample_invoice_id=scenario.scenario_id,
        )
        with self._run_lock:
            self.store.create(record)
            etag = self.blobs.upload_new(
                record.blob_name,
                scenario.data,
                content_type="application/json",
                metadata={},
                request_id=record.correlation_id,
            )
            event = blob_created_event(
                event_id=str(uuid.uuid4()),
                account_url=DEMO_ACCOUNT_URL,
                container=DEMO_CONTAINER,
                blob_name=record.blob_name,
                content_length=record.size_bytes,
                etag=etag,
                client_request_id=record.correlation_id,
                event_time=self._clock(),
            )
            self.queue.send(json.dumps(event), message_id=event["id"])
            for message in self.queue.receive(1, 0):
                self._processor.handle(message)
            self.blobs.blobs.pop(record.blob_name, None)  # keep memory small
            self.blobs.metadata.pop(record.blob_name, None)
            self.blobs.request_ids.pop(record.blob_name, None)
            finished = self.store.get(sid)
        assert finished is not None
        return DemoRun(finished.record, scenario)
