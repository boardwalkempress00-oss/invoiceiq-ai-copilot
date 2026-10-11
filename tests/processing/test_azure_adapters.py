"""Azure adapters without Azure: SDK objects are replaced by small stand-ins."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from azure.servicebus.exceptions import MessageLockLostError as SdkLockLost
from azure.servicebus.exceptions import ServiceBusConnectionError

from invoiceiq.configuration import Settings
from invoiceiq.processing.azure_table_store import AzureTableSubmissionStore
from invoiceiq.processing.blobs import AzureBlobStore, BlobStoreError
from invoiceiq.processing.messages import (
    AzureServiceBusSource,
    MessageLockLostError,
    MessageSourceError,
)
from invoiceiq.processing.store import SubmissionStoreError

T = datetime(2026, 10, 11, 12, tzinfo=UTC)


class FakeReceiver:
    def __init__(self, messages, fail=None):
        self.messages, self.fail, self.calls = messages, fail, []

    def receive_messages(self, max_message_count, max_wait_time):
        self.calls.append(("receive", max_message_count, max_wait_time))
        return self.messages

    def _settle(self, name, *args, **kwargs):
        self.calls.append((name, kwargs))
        if self.fail:
            raise self.fail

    def complete_message(self, m):
        self._settle("complete")

    def abandon_message(self, m):
        self._settle("abandon")

    def dead_letter_message(self, m, **kwargs):
        self._settle("dead_letter", **kwargs)

    def renew_message_lock(self, m):
        self._settle("renew")
        return T

    def close(self):
        self.calls.append(("close",))


def source_with(receiver):
    client = SimpleNamespace(get_queue_receiver=lambda *a, **k: receiver, close=lambda: None)
    return AzureServiceBusSource(client, "invoice-events")


def raw_message(delivery_count=0):
    return SimpleNamespace(
        body=iter([b'{"a":', b"1}"]),
        message_id="m-1",
        correlation_id="corr-1",
        delivery_count=delivery_count,
        enqueued_time_utc=T,
        sequence_number=7,
        locked_until_utc=T,
        lock_token="tok",
    )


def test_received_message_is_normalised():
    [message] = source_with(FakeReceiver([raw_message(delivery_count=0)])).receive(1, 5)
    assert message.body == b'{"a":1}'
    assert message.delivery_count == 1  # AMQP counts prior attempts; first delivery → 1
    assert (message.message_id, message.correlation_id, message.sequence_number) == (
        "m-1",
        "corr-1",
        7,
    )


def test_settlement_calls_and_dead_letter_reason():
    receiver = FakeReceiver([raw_message()])
    source = source_with(receiver)
    [message] = source.receive(1, 5)
    source.complete(message)
    source.dead_letter(message, "processing-failed", "BlobStoreError")
    assert source.renew_lock(message) == T
    assert receiver.calls[2] == (
        "dead_letter",
        {"reason": "processing-failed", "error_description": "BlobStoreError"},
    )


@pytest.mark.parametrize(
    ("sdk_error", "expected"),
    [(SdkLockLost(), MessageLockLostError), (ServiceBusConnectionError(), MessageSourceError)],
)
def test_sdk_errors_are_mapped(sdk_error, expected):
    receiver = FakeReceiver([raw_message()])
    source = source_with(receiver)
    [message] = source.receive(1, 5)
    receiver.fail = sdk_error
    with pytest.raises(expected):
        source.complete(message)


@pytest.mark.parametrize(
    "factory", [AzureServiceBusSource, AzureBlobStore, AzureTableSubmissionStore]
)
def test_connection_strings_are_refused_in_production(factory):
    settings = Settings(
        _env_file=None,
        app_env="production",
        servicebus_connection_string="Endpoint=sb://x/;SharedAccessKeyName=k;SharedAccessKey=v",
        azure_blob_connection_string="UseDevelopmentStorage=true",
        azure_table_connection_string="UseDevelopmentStorage=true",
    )
    with pytest.raises((MessageSourceError, BlobStoreError, SubmissionStoreError), match="local"):
        factory.from_settings(settings)


@pytest.mark.parametrize(
    ("factory", "missing"),
    [
        (AzureServiceBusSource, "SERVICEBUS_NAMESPACE"),
        (AzureBlobStore, "AZURE_BLOB_ENDPOINT"),
        (AzureTableSubmissionStore, "AZURE_TABLE_ENDPOINT"),
    ],
)
def test_missing_endpoints_are_reported(factory, missing):
    with pytest.raises((MessageSourceError, BlobStoreError, SubmissionStoreError), match=missing):
        factory.from_settings(Settings(_env_file=None))


def test_expected_blob_host_defaults_to_the_blob_endpoint():
    settings = Settings(_env_file=None, azure_blob_endpoint="https://stx.blob.core.windows.net/")
    assert settings.expected_blob_host == "stx.blob.core.windows.net"
    assert Settings(_env_file=None).expected_blob_host is None


def test_worker_exits_2_when_misconfigured(monkeypatch):
    import invoiceiq.worker as worker

    monkeypatch.setattr(worker, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(worker, "configure_logging", lambda level: None)
    assert worker.main() == 2
