"""Parse and verify Event Grid `Microsoft.Storage.BlobCreated` events (Event Grid schema).

Event Grid delivers each event to Service Bus as one message whose body is the event JSON. The
worker trusts nothing in it: the event must be a BlobCreated event for the configured container,
the blob name must be one the API generates (`submissions/<32 hex>.json`), and — when configured —
the blob URL must point at the expected storage account. Anything else is rejected and
dead-lettered without touching storage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlsplit

from invoiceiq.processing.models import submission_id_from_blob

BLOB_CREATED = "Microsoft.Storage.BlobCreated"
ACCEPTED_APIS = frozenset({"PutBlob", "PutBlockList"})
MAX_EVENT_BYTES = 64 * 1024


class InvalidEventError(ValueError):
    """The message is not a BlobCreated event this worker should process."""


@dataclass(frozen=True)
class BlobCreatedEvent:
    event_id: str
    event_time: datetime | None
    submission_id: str
    blob_name: str
    api: str
    content_length: int | None
    etag: str | None
    client_request_id: str | None


def _str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_blob_created(
    body: bytes | str, *, container: str, expected_host: str | None = None
) -> BlobCreatedEvent:
    raw = body.encode("utf-8") if isinstance(body, str) else body
    if len(raw) > MAX_EVENT_BYTES:
        raise InvalidEventError("event too large")
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise InvalidEventError("message body is not JSON") from exc
    # Service Bus delivery carries one event; accept a one-element array defensively.
    if isinstance(payload, list) and len(payload) == 1:
        payload = payload[0]
    if not isinstance(payload, dict):
        raise InvalidEventError("event is not a JSON object")

    if payload.get("eventType") != BLOB_CREATED:
        raise InvalidEventError("unexpected event type")
    event_id = _str(payload, "id")
    if event_id is None:
        raise InvalidEventError("event id missing")

    prefix = f"/blobServices/default/containers/{container}/blobs/"
    subject = _str(payload, "subject") or ""
    if not subject.startswith(prefix):
        raise InvalidEventError("event is for another container")
    blob_name = subject[len(prefix) :]
    submission_id = submission_id_from_blob(blob_name)
    if submission_id is None:
        raise InvalidEventError("unexpected blob name")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise InvalidEventError("event data missing")
    api = _str(data, "api") or ""
    if api not in ACCEPTED_APIS:
        raise InvalidEventError("unexpected storage operation")

    url = _str(data, "url")
    if url is None:
        raise InvalidEventError("blob url missing")
    parts = urlsplit(url)
    if expected_host is not None and parts.netloc.lower() != expected_host.lower():
        raise InvalidEventError("event is for another storage account")
    # Path is /<container>/<blob> (Azure) or /<account>/<container>/<blob> (Azurite).
    if not unquote(parts.path).endswith(f"/{container}/{blob_name}"):
        raise InvalidEventError("blob url does not match subject")

    length = data.get("contentLength")
    return BlobCreatedEvent(
        event_id=event_id,
        event_time=_time(payload.get("eventTime")),
        submission_id=submission_id,
        blob_name=blob_name,
        api=api,
        content_length=length if isinstance(length, int) and length >= 0 else None,
        etag=_str(data, "eTag"),
        client_request_id=_str(data, "clientRequestId"),
    )


def blob_created_event(
    *,
    event_id: str,
    account_url: str,
    container: str,
    blob_name: str,
    content_length: int,
    etag: str,
    client_request_id: str,
    event_time: datetime,
    topic: str = "/subscriptions/local/resourceGroups/local/providers/"
    "Microsoft.Storage/storageAccounts/local",
) -> dict[str, Any]:
    """Build an event shaped exactly like Event Grid's (local simulation and tests)."""
    return {
        "topic": topic,
        "subject": f"/blobServices/default/containers/{container}/blobs/{blob_name}",
        "eventType": BLOB_CREATED,
        "id": event_id,
        "data": {
            "api": "PutBlob",
            "clientRequestId": client_request_id,
            "requestId": event_id,
            "eTag": etag,
            "contentType": "application/json",
            "contentLength": content_length,
            "blobType": "BlockBlob",
            "url": f"{account_url.rstrip('/')}/{container}/{blob_name}",
            "sequencer": "0000000000000000000000000000000000000000000000",
            "storageDiagnostics": {"batchId": "local"},
        },
        "dataVersion": "",
        "metadataVersion": "1",
        "eventTime": event_time.isoformat().replace("+00:00", "Z"),
    }
