"""Azure Table Storage implementation of the submission store (production).

Data model (table `invoicejobs` by default — separate from the AI usage table):

    PartitionKey = "sub"             RowKey = <submission id>       → one status row per upload
    PartitionKey = "quota-<UTC day>" RowKey = "global" | "upload-session-<hash>"
                                                                    → upload allowance counters

Status rows are replaced with `If-Match: <etag>` (compare-and-set); creation uses insert, which
fails if the row exists. The two quota counters of a day share a partition and are updated in one
entity-group transaction, so they move together. Timestamps are stored as ISO-8601 UTC strings
(fixed width, so string order is time order). The result JSON is split over several properties
because a single string property is limited to 64 KiB.

Authentication: Microsoft Entra ID via managed identity in Azure. A connection string is accepted
only for local development against Azurite and is refused when APP_ENV=production.
"""

from __future__ import annotations

import contextlib
import json
import logging
import random
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from invoiceiq.processing.models import (
    ErrorCode,
    SubmissionRecord,
    SubmissionSource,
    SubmissionStatus,
)
from invoiceiq.processing.store import (
    ConcurrencyConflict,
    QuotaDecision,
    SubmissionExistsError,
    SubmissionStoreError,
    Versioned,
    decide_quota,
)

if TYPE_CHECKING:
    from azure.data.tables import TableClient

    from invoiceiq.configuration import Settings

logger = logging.getLogger(__name__)

STATUS_PARTITION = "sub"
QUOTA_PREFIX = "quota-"
RESULT_CHUNK_CHARS = 30_000  # < 32 Ki UTF-16 code units per string property
MAX_RESULT_CHUNKS = 20
QUOTA_MAX_ATTEMPTS = 25
CLIENT_OPTIONS: dict[str, Any] = {
    "connection_timeout": 3,
    "read_timeout": 5,
    "retry_total": 1,
    "retry_backoff_factor": 0.2,
}
_TIME_FIELDS = (
    "created_at",
    "updated_at",
    "expires_at",
    "lease_expires_at",
    "queued_at",
    "started_at",
    "finished_at",
)
_TEXT_FIELDS = (
    "session_key",
    "correlation_id",
    "blob_name",
    "sha256",
    "sample_invoice_id",
    "lease_owner",
    "invoice_id",
    "invoice_number",
    "error_message",
    "engine_version",
    "event_id",
    "message_id",
)


def _ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _escape(value: str) -> str:
    return value.replace("'", "''")


def to_entity(record: SubmissionRecord) -> dict[str, Any]:
    entity: dict[str, Any] = {
        "PartitionKey": STATUS_PARTITION,
        "RowKey": record.submission_id,
        "status": record.status.value,
        "source": record.source.value,
        "size_bytes": record.size_bytes,
        "attempts": record.attempts,
        "error_code": record.error_code.value if record.error_code else None,
        "error_details": json.dumps(list(record.error_details)) if record.error_details else None,
    }
    for name in _TIME_FIELDS:
        entity[name] = _ts(getattr(record, name))
    for name in _TEXT_FIELDS:
        entity[name] = getattr(record, name)
    result = record.result_json or ""
    chunks = [result[i : i + RESULT_CHUNK_CHARS] for i in range(0, len(result), RESULT_CHUNK_CHARS)]
    if len(chunks) > MAX_RESULT_CHUNKS:
        raise SubmissionStoreError("validation result too large to store")
    entity["result_chunks"] = len(chunks)
    for i, chunk in enumerate(chunks):
        entity[f"result_{i}"] = chunk
    # Table storage has no null: omit unset properties (a Replace removes old ones).
    return {k: v for k, v in entity.items() if v is not None}


def from_entity(entity: Any) -> SubmissionRecord:
    chunks = int(entity.get("result_chunks") or 0)
    result = "".join(entity.get(f"result_{i}", "") for i in range(chunks)) or None
    details = entity.get("error_details")
    times = {name: _parse_ts(entity.get(name)) for name in _TIME_FIELDS}
    return SubmissionRecord(
        submission_id=entity["RowKey"],
        status=SubmissionStatus(entity["status"]),
        source=SubmissionSource(entity["source"]),
        session_key=entity["session_key"],
        correlation_id=entity["correlation_id"],
        blob_name=entity["blob_name"],
        size_bytes=int(entity.get("size_bytes") or 0),
        sha256=entity["sha256"],
        created_at=times["created_at"],  # type: ignore[arg-type]
        updated_at=times["updated_at"],  # type: ignore[arg-type]
        expires_at=times["expires_at"],  # type: ignore[arg-type]
        sample_invoice_id=entity.get("sample_invoice_id"),
        attempts=int(entity.get("attempts") or 0),
        lease_owner=entity.get("lease_owner"),
        lease_expires_at=times["lease_expires_at"],
        queued_at=times["queued_at"],
        started_at=times["started_at"],
        finished_at=times["finished_at"],
        invoice_id=entity.get("invoice_id"),
        invoice_number=entity.get("invoice_number"),
        error_code=ErrorCode(entity["error_code"]) if entity.get("error_code") else None,
        error_message=entity.get("error_message"),
        error_details=tuple(json.loads(details)) if details else (),
        result_json=result,
        engine_version=entity.get("engine_version"),
        event_id=entity.get("event_id"),
        message_id=entity.get("message_id"),
    )


class AzureTableSubmissionStore:
    def __init__(self, table: TableClient) -> None:
        self._table = table

    @classmethod
    def from_settings(cls, settings: Settings) -> AzureTableSubmissionStore:
        from azure.data.tables import TableClient

        from invoiceiq.configuration import Environment

        name = settings.submissions_table
        if settings.azure_table_connection_string is not None:
            if settings.app_env == Environment.PRODUCTION:
                raise SubmissionStoreError(
                    "AZURE_TABLE_CONNECTION_STRING is for local Azurite only; production must "
                    "use AZURE_TABLE_ENDPOINT with managed identity"
                )
            conn = settings.azure_table_connection_string.get_secret_value()
            return cls(TableClient.from_connection_string(conn, name, **CLIENT_OPTIONS))
        if not settings.azure_table_endpoint:
            raise SubmissionStoreError("AZURE_TABLE_ENDPOINT is not configured")
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        return cls(
            TableClient(
                endpoint=settings.azure_table_endpoint,
                table_name=name,
                credential=credential,
                **CLIENT_OPTIONS,
            )
        )

    def ensure_table(self) -> None:
        """Create the table if missing (local/Azurite). In Azure, Terraform creates it."""
        from azure.core.exceptions import ResourceExistsError

        with contextlib.suppress(ResourceExistsError):
            self._table.create_table()

    @staticmethod
    def _etag(metadata: Any) -> str:
        return str(metadata.get("etag"))

    def create(self, record: SubmissionRecord) -> Versioned:
        from azure.core.exceptions import AzureError, ResourceExistsError

        try:
            meta = self._table.create_entity(to_entity(record))
        except ResourceExistsError as exc:
            raise SubmissionExistsError(record.submission_id) from exc
        except AzureError as exc:
            raise SubmissionStoreError(type(exc).__name__) from exc
        return Versioned(record, self._etag(meta))

    def get(self, submission_id: str) -> Versioned | None:
        from azure.core.exceptions import AzureError, ResourceNotFoundError

        try:
            entity = self._table.get_entity(STATUS_PARTITION, submission_id)
        except ResourceNotFoundError:
            return None
        except AzureError as exc:
            raise SubmissionStoreError(type(exc).__name__) from exc
        return Versioned(from_entity(entity), entity.metadata["etag"])

    def replace(self, record: SubmissionRecord, etag: str) -> Versioned:
        from azure.core import MatchConditions
        from azure.core.exceptions import (
            AzureError,
            ResourceModifiedError,
            ResourceNotFoundError,
        )
        from azure.data.tables import UpdateMode

        try:
            meta = self._table.update_entity(
                to_entity(record),
                mode=UpdateMode.REPLACE,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except (ResourceModifiedError, ResourceNotFoundError) as exc:
            raise ConcurrencyConflict(record.submission_id) from exc
        except AzureError as exc:
            if getattr(exc, "status_code", None) == 412:
                raise ConcurrencyConflict(record.submission_id) from exc
            raise SubmissionStoreError(type(exc).__name__) from exc
        return Versioned(record, self._etag(meta))

    def list_for_session(self, session_key: str, limit: int) -> list[SubmissionRecord]:
        from azure.core.exceptions import AzureError

        query = f"PartitionKey eq '{STATUS_PARTITION}' and session_key eq '{_escape(session_key)}'"
        try:
            rows = [from_entity(e) for e in self._table.query_entities(query)]
        except AzureError as exc:
            raise SubmissionStoreError(type(exc).__name__) from exc
        return sorted(rows, key=lambda r: r.created_at, reverse=True)[:limit]

    def delete_expired(self, now: datetime, limit: int) -> int:
        from azure.core.exceptions import AzureError, ResourceNotFoundError

        cutoff = _ts(now)
        query = f"PartitionKey eq '{STATUS_PARTITION}' and expires_at lt '{cutoff}'"
        deleted = 0
        try:
            for entity in self._table.query_entities(query, select=["PartitionKey", "RowKey"]):
                if deleted >= limit:
                    break
                with contextlib.suppress(ResourceNotFoundError):
                    self._table.delete_entity(entity["PartitionKey"], entity["RowKey"])
                deleted += 1
        except AzureError as exc:
            raise SubmissionStoreError(type(exc).__name__) from exc
        return deleted

    def reserve_upload(
        self, day: str, session_key: str, session_limit: int, global_limit: int
    ) -> QuotaDecision:
        from azure.core import MatchConditions
        from azure.core.exceptions import AzureError
        from azure.data.tables import TableTransactionError, UpdateMode

        partition = f"{QUOTA_PREFIX}{day}"
        for attempt in range(QUOTA_MAX_ATTEMPTS):
            if attempt:  # lost a race: back off briefly with jitter before re-reading
                time.sleep(random.uniform(0.01, 0.05) * min(attempt, 5))
            try:
                rows = {
                    e["RowKey"]: e
                    for e in self._table.query_entities(
                        f"PartitionKey eq '{partition}' and "
                        f"(RowKey eq 'global' or RowKey eq '{_escape(session_key)}')"
                    )
                }
            except AzureError as exc:
                raise SubmissionStoreError(type(exc).__name__) from exc
            g, s = rows.get("global"), rows.get(session_key)
            used_g = int(g["uploads"]) if g else 0
            used_s = int(s["uploads"]) if s else 0
            decision = decide_quota(used_s, used_g, session_limit, global_limit)
            if not decision.allowed:
                return decision
            ops: list[tuple[Any, ...]] = []
            for key, row, used in (("global", g, used_g), (session_key, s, used_s)):
                entity = {"PartitionKey": partition, "RowKey": key, "uploads": used + 1}
                if row is None:
                    ops.append(("create", entity))
                else:
                    ops.append(
                        (
                            "update",
                            entity,
                            {
                                "mode": UpdateMode.REPLACE,
                                "etag": row.metadata["etag"],
                                "match_condition": MatchConditions.IfNotModified,
                            },
                        )
                    )
            try:
                self._table.submit_transaction(ops)
                return decision
            except TableTransactionError:
                continue  # a concurrent upload won the race; re-read and retry
            except AzureError as exc:
                raise SubmissionStoreError(type(exc).__name__) from exc
        raise SubmissionStoreError("upload allowance busy; try again")
