"""Invoice upload storage (private Blob Storage container).

Uploads are create-only (`If-None-Match: *`): a blob is never overwritten, so it produces exactly
one BlobCreated event. The API's correlation ID is sent as the client request ID, which Event Grid
copies into the event (`data.clientRequestId`) and from there into the Service Bus message.

Authentication: Microsoft Entra ID (managed identity) in Azure. A connection string is accepted
only for local development against Azurite and is refused when APP_ENV=production.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from azure.storage.blob import ContainerClient

    from invoiceiq.configuration import Settings

# Fail fast: a slow upload must not hold a user request for long.
CLIENT_OPTIONS: dict[str, Any] = {
    "connection_timeout": 3,
    "read_timeout": 10,
    "retry_total": 2,
    "retry_backoff_factor": 0.4,
}


class BlobStoreError(RuntimeError):
    """Storage unreachable, refused or failed (retryable from the worker's point of view)."""


class BlobNotFoundError(BlobStoreError):
    pass


class BlobAlreadyExistsError(BlobStoreError):
    pass


class BlobTooLargeError(BlobStoreError):
    pass


class BlobStore(Protocol):
    def upload_new(
        self,
        name: str,
        data: bytes,
        *,
        content_type: str,
        metadata: dict[str, str],
        request_id: str,
    ) -> str: ...

    def download(self, name: str, *, max_bytes: int) -> bytes: ...


class InMemoryBlobStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.request_ids: dict[str, str] = {}
        self._lock = threading.Lock()
        self._version = 0
        self.fail_next: list[Exception] = []  # test hook

    def upload_new(
        self,
        name: str,
        data: bytes,
        *,
        content_type: str,
        metadata: dict[str, str],
        request_id: str,
    ) -> str:
        with self._lock:
            if self.fail_next:
                raise self.fail_next.pop(0)
            if name in self.blobs:
                raise BlobAlreadyExistsError(name)
            self._version += 1
            self.blobs[name] = bytes(data)
            self.metadata[name] = dict(metadata)
            self.request_ids[name] = request_id
            return f'"0x{self._version:X}"'

    def download(self, name: str, *, max_bytes: int) -> bytes:
        with self._lock:
            if self.fail_next:
                raise self.fail_next.pop(0)
            if name not in self.blobs:
                raise BlobNotFoundError(name)
            data = self.blobs[name]
        if len(data) > max_bytes:
            raise BlobTooLargeError(name)
        return data


class AzureBlobStore:
    def __init__(self, container: ContainerClient) -> None:
        self._container = container

    @classmethod
    def from_settings(cls, settings: Settings) -> AzureBlobStore:
        from azure.storage.blob import ContainerClient

        from invoiceiq.configuration import Environment

        container = settings.upload_container
        if settings.azure_blob_connection_string is not None:
            if settings.app_env == Environment.PRODUCTION:
                raise BlobStoreError(
                    "AZURE_BLOB_CONNECTION_STRING is for local Azurite only; production must use "
                    "AZURE_BLOB_ENDPOINT with managed identity"
                )
            conn = settings.azure_blob_connection_string.get_secret_value()
            return cls(ContainerClient.from_connection_string(conn, container, **CLIENT_OPTIONS))
        if not settings.azure_blob_endpoint:
            raise BlobStoreError("AZURE_BLOB_ENDPOINT is not configured")
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        return cls(
            ContainerClient(
                account_url=settings.azure_blob_endpoint,
                container_name=container,
                credential=credential,
                **CLIENT_OPTIONS,
            )
        )

    def ensure_container(self) -> None:
        """Create the container if missing (local/Azurite). In Azure, Terraform creates it."""
        import contextlib

        from azure.core.exceptions import ResourceExistsError

        with contextlib.suppress(ResourceExistsError):
            self._container.create_container()

    def upload_new(
        self,
        name: str,
        data: bytes,
        *,
        content_type: str,
        metadata: dict[str, str],
        request_id: str,
    ) -> str:
        from azure.core.exceptions import AzureError, ResourceExistsError
        from azure.storage.blob import ContentSettings

        try:
            props = self._container.get_blob_client(name).upload_blob(
                data,
                overwrite=False,  # If-None-Match: * — never replace an existing blob
                content_settings=ContentSettings(content_type=content_type),
                metadata=metadata,
                client_request_id=request_id,  # x-ms-client-request-id → data.clientRequestId
            )
        except ResourceExistsError as exc:
            raise BlobAlreadyExistsError(name) from exc
        except AzureError as exc:
            raise BlobStoreError(type(exc).__name__) from exc
        return str(props.get("etag", ""))

    def download(self, name: str, *, max_bytes: int) -> bytes:
        from azure.core.exceptions import AzureError, HttpResponseError, ResourceNotFoundError

        try:
            # Ask for one byte more than allowed: a longer answer means the blob is too large,
            # and an oversized blob is never read in full.
            downloader = self._container.download_blob(name, offset=0, length=max_bytes + 1)
            data = downloader.readall()
        except ResourceNotFoundError as exc:
            raise BlobNotFoundError(name) from exc
        except HttpResponseError as exc:
            if exc.status_code == 416:  # range not satisfiable: the blob is empty
                return b""
            raise BlobStoreError(type(exc).__name__) from exc
        except AzureError as exc:
            raise BlobStoreError(type(exc).__name__) from exc
        if len(data) > max_bytes:
            raise BlobTooLargeError(name)
        return data
