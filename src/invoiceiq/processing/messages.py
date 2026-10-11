"""Queue access for the worker: a small receive/settle interface over Azure Service Bus.

Messages are received in peek-lock mode. A message is removed only by `complete` or
`dead_letter`; `abandon` (or simply letting the lock expire) makes it available again with a
higher delivery count. When the delivery count passes the queue's maximum, Service Bus moves the
message to the dead-letter queue itself.

`InMemoryQueue` reproduces exactly these semantics — lock expiry, lock tokens, delivery counts,
automatic and explicit dead-lettering — so the worker's at-least-once handling can be tested
deterministically with a controllable clock.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from invoiceiq.processing.models import utcnow

if TYPE_CHECKING:
    from invoiceiq.configuration import Settings


class MessageSourceError(RuntimeError):
    """The queue could not be reached or refused the operation."""


class MessageLockLostError(MessageSourceError):
    """The lock expired (or was renewed by nobody) before the message was settled."""


@dataclass(frozen=True)
class QueueMessage:
    body: bytes
    message_id: str | None
    correlation_id: str | None
    delivery_count: int  # 1 on the first delivery
    enqueued_time: datetime | None
    sequence_number: int | None
    locked_until: datetime | None
    lock_token: str
    raw: Any = field(default=None, compare=False, repr=False)


class MessageSource(Protocol):
    def receive(self, max_messages: int, max_wait_seconds: float) -> list[QueueMessage]: ...

    def complete(self, message: QueueMessage) -> None: ...

    def abandon(self, message: QueueMessage) -> None: ...

    def dead_letter(self, message: QueueMessage, reason: str, description: str) -> None: ...

    def renew_lock(self, message: QueueMessage) -> datetime: ...


@dataclass
class _Entry:
    body: bytes
    message_id: str
    correlation_id: str | None
    sequence_number: int
    enqueued_time: datetime
    delivery_count: int = 0
    lock_token: str | None = None
    locked_until: datetime | None = None
    dead_letter_reason: str | None = None
    dead_letter_description: str | None = None


class InMemoryQueue:
    """Thread-safe peek-lock queue with Service Bus semantics, for tests and local development."""

    def __init__(
        self,
        *,
        lock_duration: timedelta = timedelta(seconds=60),
        max_delivery_count: int = 5,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.lock_duration = lock_duration
        self.max_delivery_count = max_delivery_count
        self._clock = clock
        self._active: list[_Entry] = []
        self.dead_letters: list[_Entry] = []
        self.completed: list[str] = []
        self._lock = threading.Condition()
        self._seq = itertools.count(1)
        self._tokens = itertools.count(1)
        self.fail_next: list[Exception] = []  # test hook for receive/settle

    def send(
        self, body: bytes | str, *, message_id: str, correlation_id: str | None = None
    ) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        with self._lock:
            self._active.append(
                _Entry(data, message_id, correlation_id, next(self._seq), self._clock())
            )
            self._lock.notify_all()

    def _available(self, now: datetime) -> _Entry | None:
        for entry in list(self._active):
            if entry.locked_until is not None and entry.locked_until > now:
                continue
            if entry.delivery_count >= self.max_delivery_count:
                # A delivery beyond the maximum is not made: the broker dead-letters instead.
                self._active.remove(entry)
                entry.dead_letter_reason = "MaxDeliveryCountExceeded"
                entry.dead_letter_description = "Message could not be consumed after retries."
                entry.lock_token = entry.locked_until = None
                self.dead_letters.append(entry)
                continue
            return entry
        return None

    def receive(self, max_messages: int, max_wait_seconds: float) -> list[QueueMessage]:
        with self._lock:
            if self.fail_next:
                raise self.fail_next.pop(0)
            out: list[QueueMessage] = []
            now = self._clock()
            while len(out) < max_messages:
                entry = self._available(now)
                if entry is None:
                    break
                entry.delivery_count += 1
                entry.lock_token = f"lock-{next(self._tokens)}"
                entry.locked_until = now + self.lock_duration
                out.append(self._message(entry))
            return out

    @staticmethod
    def _message(entry: _Entry) -> QueueMessage:
        assert entry.lock_token is not None
        return QueueMessage(
            body=entry.body,
            message_id=entry.message_id,
            correlation_id=entry.correlation_id,
            delivery_count=entry.delivery_count,
            enqueued_time=entry.enqueued_time,
            sequence_number=entry.sequence_number,
            locked_until=entry.locked_until,
            lock_token=entry.lock_token,
        )

    def _held(self, message: QueueMessage) -> _Entry:
        """The entry, if this message still holds a valid lock; otherwise MessageLockLostError."""
        if self.fail_next:
            raise self.fail_next.pop(0)
        now = self._clock()
        for entry in self._active:
            if (
                entry.lock_token == message.lock_token
                and entry.locked_until is not None
                and entry.locked_until > now
            ):
                return entry
        raise MessageLockLostError(message.message_id or "?")

    def complete(self, message: QueueMessage) -> None:
        with self._lock:
            entry = self._held(message)
            self._active.remove(entry)
            self.completed.append(entry.message_id)

    def abandon(self, message: QueueMessage) -> None:
        with self._lock:
            entry = self._held(message)
            entry.lock_token = entry.locked_until = None
            self._lock.notify_all()

    def dead_letter(self, message: QueueMessage, reason: str, description: str) -> None:
        with self._lock:
            entry = self._held(message)
            self._active.remove(entry)
            entry.dead_letter_reason, entry.dead_letter_description = reason, description
            entry.lock_token = entry.locked_until = None
            self.dead_letters.append(entry)

    def renew_lock(self, message: QueueMessage) -> datetime:
        with self._lock:
            entry = self._held(message)
            entry.locked_until = self._clock() + self.lock_duration
            return entry.locked_until

    # -- inspection -----------------------------------------------------------------------------

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def wait_for_message(self, timeout: float) -> bool:
        """Block until a message may be available (local simulation's worker thread)."""
        with self._lock:
            if self._available(self._clock()) is not None:
                return True
            self._lock.wait(timeout)
            return self._available(self._clock()) is not None


class AzureServiceBusSource:
    """Peek-lock receiver for one Service Bus queue (managed identity in Azure)."""

    def __init__(self, client: Any, queue_name: str) -> None:
        from azure.servicebus import ServiceBusReceiveMode

        self._client = client
        self._receiver = client.get_queue_receiver(
            queue_name, receive_mode=ServiceBusReceiveMode.PEEK_LOCK, prefetch_count=0
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> AzureServiceBusSource:
        from azure.servicebus import ServiceBusClient

        from invoiceiq.configuration import Environment

        if settings.servicebus_connection_string is not None:
            if settings.app_env == Environment.PRODUCTION:
                raise MessageSourceError(
                    "SERVICEBUS_CONNECTION_STRING is for the local emulator only; production "
                    "must use SERVICEBUS_NAMESPACE with managed identity"
                )
            conn = settings.servicebus_connection_string.get_secret_value()
            return cls(ServiceBusClient.from_connection_string(conn), settings.servicebus_queue)
        if not settings.servicebus_namespace:
            raise MessageSourceError("SERVICEBUS_NAMESPACE is not configured")
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        client = ServiceBusClient(settings.servicebus_namespace, credential)
        return cls(client, settings.servicebus_queue)

    def close(self) -> None:
        self._receiver.close()
        self._client.close()

    @staticmethod
    def _wrap(raw: Any) -> QueueMessage:
        body = b"".join(bytes(part) for part in raw.body)
        return QueueMessage(
            body=body,
            message_id=raw.message_id,
            correlation_id=raw.correlation_id,
            # The SDK returns the AMQP header field, defined as the number of *prior* delivery
            # attempts (0 on the first delivery). Normalise to "1 on the first delivery". If the
            # broker turns out to count from 1 already (to verify in Azure), the only effect is
            # that the worker dead-letters one attempt early — with an accurate status.
            delivery_count=int(raw.delivery_count or 0) + 1,
            enqueued_time=raw.enqueued_time_utc,
            sequence_number=raw.sequence_number,
            locked_until=raw.locked_until_utc,
            lock_token=str(raw.lock_token),
            raw=raw,
        )

    def _call(self, fn: Callable[[], Any]) -> Any:
        from azure.servicebus.exceptions import MessageLockLostError as SdkLockLost
        from azure.servicebus.exceptions import ServiceBusError

        try:
            return fn()
        except SdkLockLost as exc:
            raise MessageLockLostError(type(exc).__name__) from exc
        except ServiceBusError as exc:
            raise MessageSourceError(type(exc).__name__) from exc

    def receive(self, max_messages: int, max_wait_seconds: float) -> list[QueueMessage]:
        raws = self._call(
            lambda: self._receiver.receive_messages(
                max_message_count=max_messages, max_wait_time=max_wait_seconds
            )
        )
        return [self._wrap(raw) for raw in raws]

    def complete(self, message: QueueMessage) -> None:
        self._call(lambda: self._receiver.complete_message(message.raw))

    def abandon(self, message: QueueMessage) -> None:
        self._call(lambda: self._receiver.abandon_message(message.raw))

    def dead_letter(self, message: QueueMessage, reason: str, description: str) -> None:
        self._call(
            lambda: self._receiver.dead_letter_message(
                message.raw, reason=reason, error_description=description
            )
        )

    def renew_lock(self, message: QueueMessage) -> datetime:
        return self._call(lambda: self._receiver.renew_message_lock(message.raw))  # type: ignore[no-any-return]
