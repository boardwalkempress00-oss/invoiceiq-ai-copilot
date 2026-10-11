"""The in-memory queue reproduces the Service Bus peek-lock semantics the worker relies on."""

from __future__ import annotations

from datetime import timedelta

import pytest

from invoiceiq.processing.messages import InMemoryQueue, MessageLockLostError


@pytest.fixture
def queue(clock):
    return InMemoryQueue(lock_duration=timedelta(seconds=60), max_delivery_count=3, clock=clock)


def test_peek_lock_hides_message_until_lock_expires(queue, clock):
    queue.send("a", message_id="m1")
    first = queue.receive(5, 0)
    assert [m.delivery_count for m in first] == [1]
    assert queue.receive(5, 0) == []  # locked
    clock.advance(61)
    again = queue.receive(5, 0)
    assert again[0].delivery_count == 2 and again[0].lock_token != first[0].lock_token


def test_settling_with_an_expired_lock_fails(queue, clock):
    queue.send("a", message_id="m1")
    message = queue.receive(1, 0)[0]
    clock.advance(61)
    with pytest.raises(MessageLockLostError):
        queue.complete(message)
    # The message is still there and can be redelivered.
    assert queue.receive(1, 0)[0].delivery_count == 2


def test_stale_lock_token_cannot_settle_a_redelivered_message(queue, clock):
    queue.send("a", message_id="m1")
    old = queue.receive(1, 0)[0]
    clock.advance(61)
    new = queue.receive(1, 0)[0]
    with pytest.raises(MessageLockLostError):
        queue.complete(old)
    queue.complete(new)
    assert queue.completed == ["m1"] and queue.active_count() == 0


def test_abandon_releases_immediately_and_counts_a_delivery(queue):
    queue.send("a", message_id="m1")
    queue.abandon(queue.receive(1, 0)[0])
    assert queue.receive(1, 0)[0].delivery_count == 2


def test_renew_lock_extends_it(queue, clock):
    queue.send("a", message_id="m1")
    message = queue.receive(1, 0)[0]
    clock.advance(50)
    queue.renew_lock(message)
    clock.advance(50)
    queue.complete(message)  # still held


def test_max_delivery_count_moves_message_to_dead_letter_queue(queue):
    queue.send("a", message_id="m1")
    for _ in range(3):
        queue.abandon(queue.receive(1, 0)[0])
    assert queue.receive(1, 0) == []
    assert [e.dead_letter_reason for e in queue.dead_letters] == ["MaxDeliveryCountExceeded"]


def test_explicit_dead_letter_keeps_reason(queue):
    queue.send("a", message_id="m1")
    queue.dead_letter(queue.receive(1, 0)[0], "unexpected-event", "bad")
    entry = queue.dead_letters[0]
    assert (entry.dead_letter_reason, entry.dead_letter_description) == ("unexpected-event", "bad")
