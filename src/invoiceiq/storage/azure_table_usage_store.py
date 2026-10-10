"""Azure Table Storage implementation of the usage store (production).

Data model (table `aiusage` by default):

    PartitionKey = UTC date, e.g. "2026-10-10"   → one partition per day; a new day starts at zero
    RowKey       = "global" | "session-<sha256 prefix>"
    properties   = the `Counters` fields, stored as Edm.Int64

Concurrency: every write carries the ETag that was read (`If-Match`). Writes that touch the
global and the session counter are submitted as one entity-group transaction (same partition),
so they succeed or fail together. A 412 (ETag changed) or 409 (row created concurrently) means
another request won the race; `commit` returns False and the limiter re-reads and retries.

Authentication: Microsoft Entra ID via managed identity (DefaultAzureCredential) in Azure. A
connection string is accepted only for local development against Azurite and is refused when
APP_ENV=production.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Sequence
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from invoiceiq.core.usage_limiter import (
    COUNTER_FIELDS,
    CasUsageStore,
    Counters,
    CounterWrite,
    UsageStoreError,
    Versioned,
)

if TYPE_CHECKING:
    from azure.data.tables import TableClient

    from invoiceiq.configuration import Settings

logger = logging.getLogger(__name__)

# Fail fast: a slow limiter must not hold a user request for long.
CLIENT_OPTIONS: dict[str, Any] = {
    "connection_timeout": 3,
    "read_timeout": 5,
    "retry_total": 1,
    "retry_backoff_factor": 0.2,
}


def _escape(value: str) -> str:
    return value.replace("'", "''")


class AzureTableUsageStore(CasUsageStore):
    shared_durable = True

    def __init__(self, table: TableClient) -> None:
        self._table = table

    @classmethod
    def from_settings(cls, settings: Settings) -> AzureTableUsageStore:
        from azure.data.tables import TableClient

        from invoiceiq.configuration import Environment

        name = settings.azure_table_name
        if settings.azure_table_connection_string is not None:
            if settings.app_env == Environment.PRODUCTION:
                raise UsageStoreError(
                    "AZURE_TABLE_CONNECTION_STRING is for local Azurite only; production must "
                    "use AZURE_TABLE_ENDPOINT with managed identity"
                )
            conn = settings.azure_table_connection_string.get_secret_value()
            return cls(TableClient.from_connection_string(conn, name, **CLIENT_OPTIONS))
        if not settings.azure_table_endpoint:
            raise UsageStoreError("AZURE_TABLE_ENDPOINT is not configured")
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
    def _to_counters(entity: Any) -> Counters:
        values = {}
        for name in COUNTER_FIELDS:
            raw = entity.get(name, 0)
            values[name] = int(getattr(raw, "value", raw) or 0)
        return Counters(**values)

    def _entity(self, day: str, write: CounterWrite) -> dict[str, Any]:
        from azure.data.tables import EdmType, EntityProperty

        entity: dict[str, Any] = {"PartitionKey": day, "RowKey": write.key}
        for name, value in asdict(write.counters).items():
            entity[name] = EntityProperty(value, EdmType.INT64)
        return entity

    def read(self, day: str, keys: Sequence[str]) -> dict[str, Versioned]:
        from azure.core.exceptions import AzureError

        rows = " or ".join(f"RowKey eq '{_escape(k)}'" for k in keys)
        query = f"PartitionKey eq '{_escape(day)}' and ({rows})"
        try:
            found = {
                e["RowKey"]: Versioned(self._to_counters(e), e.metadata["etag"])
                for e in self._table.query_entities(query)
            }
        except AzureError as exc:
            raise UsageStoreError(f"table read failed: {type(exc).__name__}") from exc
        return {k: found.get(k, Versioned(Counters(), None)) for k in keys}

    def commit(self, day: str, writes: Sequence[CounterWrite]) -> bool:
        from azure.core import MatchConditions
        from azure.core.exceptions import AzureError, HttpResponseError
        from azure.data.tables import TableTransactionError, UpdateMode

        operations: list[Any] = []
        for write in writes:
            entity = self._entity(day, write)
            if write.etag is None:
                operations.append(("create", entity))
            else:
                operations.append(
                    (
                        "update",
                        entity,
                        {
                            "mode": UpdateMode.REPLACE,
                            "etag": write.etag,
                            "match_condition": MatchConditions.IfNotModified,
                        },
                    )
                )
        try:
            if len(operations) == 1:
                op = operations[0]
                if op[0] == "create":
                    self._table.create_entity(op[1])
                else:
                    self._table.update_entity(op[1], **op[2])
            else:
                self._table.submit_transaction(operations)
            return True
        except (TableTransactionError, HttpResponseError) as exc:
            if exc.status_code in (409, 412):
                return False  # concurrency conflict: someone else updated first
            raise UsageStoreError(f"table write failed: HTTP {exc.status_code}") from exc
        except AzureError as exc:
            raise UsageStoreError(f"table write failed: {type(exc).__name__}") from exc
