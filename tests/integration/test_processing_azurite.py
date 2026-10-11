"""Submission store and blob store against Azurite, plus the full worker path on real storage.

    docker run -d -p 127.0.0.1:10000:10000 -p 127.0.0.1:10002:10002 \\
        mcr.microsoft.com/azure-storage/azurite:3.35.0 \\
        azurite --blobHost 0.0.0.0 --tableHost 0.0.0.0 --skipApiVersionCheck --loose

Skipped when Azurite is not reachable, unless AZURITE_REQUIRED=1 (CI). Service Bus and Event Grid
have no Azurite equivalent: the queue is the in-memory peek-lock queue and the event is built
exactly as Event Grid builds it. Real delivery is verified in Azure (Phase E).
"""

from __future__ import annotations

import json
import os
import socket
import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from invoiceiq.configuration import PROJECT_ROOT, Settings
from invoiceiq.core.invoice_service import InvoiceService, JsonInvoiceRepository
from invoiceiq.processing.blobs import (
    AzureBlobStore,
    BlobAlreadyExistsError,
    BlobNotFoundError,
    BlobTooLargeError,
)
from invoiceiq.processing.events import blob_created_event
from invoiceiq.processing.messages import InMemoryQueue
from invoiceiq.processing.models import (
    SubmissionRecord,
    SubmissionSource,
    SubmissionStatus,
    blob_name_for,
    new_submission_id,
)
from invoiceiq.processing.processor import InvoiceProcessor, Outcome, ProcessorConfig
from invoiceiq.processing.store import (
    ConcurrencyConflict,
    InMemorySubmissionStore,
    SubmissionExistsError,
)
from invoiceiq.processing.submissions import SubmissionService

# Azurite's publicly documented development account key — not a secret.
_KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
CONNECTION_STRING = (
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
    f"AccountKey={_KEY};"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
    "TableEndpoint=http://127.0.0.1:10002/devstoreaccount1;"
)
BLOB_HOST = "127.0.0.1:10000"
T0 = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def _reachable(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture(autouse=True)
def _require_azurite() -> None:
    if not (_reachable(10000) and _reachable(10002)):
        if os.getenv("AZURITE_REQUIRED") == "1":
            pytest.fail("Azurite (blob 10000 + table 10002) is required but not reachable")
        pytest.skip("Azurite not running")


def azure_settings(**overrides: object) -> Settings:
    suffix = uuid.uuid4().hex[:10]
    return Settings(
        _env_file=None,
        async_processing_enabled=True,
        submission_backend="azure",
        azure_table_connection_string=CONNECTION_STRING,
        azure_blob_connection_string=CONNECTION_STRING,
        submissions_table=f"jobs{suffix}",
        upload_container=f"uploads-{suffix}",
        **overrides,  # type: ignore[arg-type]
    )


def azure_store(settings: Settings):
    from invoiceiq.processing.azure_table_store import AzureTableSubmissionStore

    store = AzureTableSubmissionStore.from_settings(settings)
    store.ensure_table()
    return store


def record(**changes: object) -> SubmissionRecord:
    sid = new_submission_id()
    base = SubmissionRecord(
        submission_id=sid,
        status=SubmissionStatus.PENDING,
        source=SubmissionSource.UPLOAD,
        session_key="upload-session-abc",
        correlation_id="corr-0123456789",
        blob_name=blob_name_for(sid),
        size_bytes=10,
        sha256="0" * 64,
        created_at=T0,
        updated_at=T0,
        expires_at=T0 + timedelta(days=7),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


@pytest.fixture(params=["memory", "azure_table"])
def store(request):
    if request.param == "memory":
        return InMemorySubmissionStore()
    return azure_store(azure_settings())


# --- Store contract (identical behaviour for memory and Azure Table) -----------------------------


def test_create_get_and_conditional_replace(store):
    rec = record()
    created = store.create(rec)
    with pytest.raises(SubmissionExistsError):
        store.create(rec)
    fetched = store.get(rec.submission_id)
    assert fetched.record == rec and fetched.etag == created.etag

    claimed = store.replace(
        replace(rec, status=SubmissionStatus.PROCESSING, attempts=1, lease_owner="w1:lock-1"),
        created.etag,
    )
    with pytest.raises(ConcurrencyConflict):  # stale ETag
        store.replace(replace(rec, status=SubmissionStatus.PROCESSING), created.etag)
    assert store.get(rec.submission_id).record.lease_owner == "w1:lock-1"
    assert claimed.etag != created.etag
    assert store.get("f" * 32) is None


def test_large_results_and_details_round_trip(store):
    big = json.dumps({"result": "x" * 100_000})  # several 30k-character chunks
    rec = record(
        status=SubmissionStatus.COMPLETED,
        result_json=big,
        error_details=("a: b", "c: d"),
        finished_at=T0 + timedelta(seconds=3),
    )
    store.create(rec)
    assert store.get(rec.submission_id).record == rec
    # A later replace without a result removes the old chunks.
    v = store.get(rec.submission_id)
    store.replace(replace(rec, result_json=None, error_details=()), v.etag)
    assert store.get(rec.submission_id).record.result_json is None


def test_list_for_session_and_delete_expired(store):
    mine = [record(created_at=T0 + timedelta(seconds=i)) for i in range(3)]
    other = record(session_key="upload-session-other")
    expired = record(created_at=T0 - timedelta(hours=1), expires_at=T0 - timedelta(seconds=1))
    for rec in [*mine, other, expired]:
        store.create(rec)
    listed = store.list_for_session("upload-session-abc", 10)
    assert [r.submission_id for r in listed[:3]] == [r.submission_id for r in reversed(mine)]
    assert store.delete_expired(T0, 10) == 1
    assert store.get(expired.submission_id) is None and store.get(other.submission_id)


def test_upload_allowance_is_atomic_under_concurrency(store):
    granted: list[bool] = []
    lock = threading.Lock()

    def reserve(n: int) -> None:
        decision = store.reserve_upload("2026-10-11", f"upload-session-{n % 4}", 100, 10)
        with lock:
            granted.append(decision.allowed)

    threads = [threading.Thread(target=reserve, args=(n,)) for n in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert granted.count(True) == 10
    assert not store.reserve_upload("2026-10-11", "upload-session-new", 100, 10).allowed
    assert store.reserve_upload("2026-10-12", "upload-session-new", 100, 10).allowed  # new day


# --- Blob store ----------------------------------------------------------------------------------


def test_blob_upload_is_create_only_and_sends_the_correlation_id():
    from azure.storage.blob import ContainerClient

    settings = azure_settings()
    seen: list[str] = []

    def hook(request) -> None:
        if request.http_request.method == "PUT":
            seen.append(request.http_request.headers.get("x-ms-client-request-id", ""))

    blobs = AzureBlobStore(
        ContainerClient.from_connection_string(
            CONNECTION_STRING, settings.upload_container, raw_request_hook=hook
        )
    )
    blobs.ensure_container()
    name = blob_name_for(new_submission_id())
    blobs.upload_new(
        name, b'{"a": 1}', content_type="application/json", metadata={}, request_id="corr-xyz123"
    )
    assert "corr-xyz123" in seen  # becomes data.clientRequestId in the BlobCreated event
    with pytest.raises(BlobAlreadyExistsError):
        blobs.upload_new(
            name, b"{}", content_type="application/json", metadata={}, request_id="corr-2"
        )
    assert blobs.download(name, max_bytes=100) == b'{"a": 1}'
    with pytest.raises(BlobTooLargeError):
        blobs.download(name, max_bytes=4)
    with pytest.raises(BlobNotFoundError):
        blobs.download(blob_name_for(new_submission_id()), max_bytes=100)


# --- Full path on real storage -------------------------------------------------------------------


def test_submission_is_processed_end_to_end_on_azurite_storage():
    settings = azure_settings()
    store = azure_store(settings)
    blobs = AzureBlobStore.from_settings(settings)
    blobs.ensure_container()
    queue = InMemoryQueue()

    def publish(rec: SubmissionRecord, etag: str) -> None:  # what Event Grid does
        event = blob_created_event(
            event_id=str(uuid.uuid4()),
            account_url=f"http://{BLOB_HOST}/devstoreaccount1",
            container=settings.upload_container,
            blob_name=rec.blob_name,
            content_length=rec.size_bytes,
            etag=etag,
            client_request_id=rec.correlation_id,
            event_time=datetime.now(UTC),
        )
        for _ in range(2):  # delivered twice (at-least-once)
            queue.send(json.dumps(event), message_id=event["id"])

    service = SubmissionService(
        settings,
        store,
        blobs,
        InvoiceService(JsonInvoiceRepository(PROJECT_ROOT / "data" / "invoices")),
        on_uploaded=publish,
    )
    rec = service.submit(session_id="session-abcdefgh", sample_invoice_id="inv-002")
    processor = InvoiceProcessor(
        store,
        blobs,
        queue,
        ProcessorConfig(
            container=settings.upload_container,
            max_invoice_bytes=settings.submission_max_bytes,
            expected_blob_host=BLOB_HOST,
        ),
    )
    outcomes = []
    while messages := queue.receive(1, 0):
        outcomes.append(processor.handle(messages[0]))
    assert outcomes == [Outcome.COMPLETED, Outcome.DUPLICATE]
    done = service.get(rec.submission_id)
    assert done.status == SubmissionStatus.COMPLETED and done.attempts == 1
    result = json.loads(done.result_json)["result"]
    assert result["status"] == "Flagged" and result["required_approval_role"]


def test_app_factory_wires_the_azure_backend():
    from fastapi.testclient import TestClient

    from invoiceiq.api.main import create_app

    client = TestClient(create_app(azure_settings()))
    assert client.get("/health").json()["async_processing"] is True
    body = client.post(
        "/submissions", json={"session_id": "session-abcdefgh", "sample_invoice_id": "inv-001"}
    ).json()
    assert body["status"] == "Pending"  # the separate worker job would process it
    assert client.get(f"/submissions/{body['submission_id']}").json()["status"] == "Pending"
