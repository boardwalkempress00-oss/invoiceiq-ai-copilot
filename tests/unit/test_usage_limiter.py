from __future__ import annotations

import threading
from pathlib import Path

import pytest

from invoiceiq.core.usage_limiter import (
    InMemoryUsageStore,
    LimitReason,
    SqliteUsageStore,
    UsageLimiter,
)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path: Path):
    if request.param == "memory":
        return InMemoryUsageStore()
    return SqliteUsageStore(tmp_path / "usage.sqlite3")


class FakeClock:
    def __init__(self, day: str = "2026-10-10") -> None:
        self.day = day

    def __call__(self) -> str:
        return self.day


def test_store_consumes_up_to_limit(store):
    assert [store.try_consume("d", "k", 3) for _ in range(4)] == [1, 2, 3, None]
    assert store.get_count("d", "k") == 3
    assert store.get_count("d", "other") == 0


def test_store_zero_limit_denies(store):
    assert store.try_consume("d", "k", 0) is None


def test_store_is_concurrency_safe(store):
    allowed: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(10):
            if store.try_consume("d", "k", 25) is not None:
                with lock:
                    allowed.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(allowed) == 25  # 80 attempts, exactly 25 succeed
    assert store.get_count("d", "k") == 25


def test_sqlite_store_survives_new_instance(tmp_path: Path):
    path = tmp_path / "usage.sqlite3"
    SqliteUsageStore(path).try_consume("d", "k", 5)
    assert SqliteUsageStore(path).get_count("d", "k") == 1


def test_stores_are_not_shared_durable():
    # Neither Milestone 1 store may be used to protect live AI in production.
    assert InMemoryUsageStore.shared_durable is False
    assert SqliteUsageStore.shared_durable is False


def test_session_limit(store):
    limiter = UsageLimiter(store, session_limit=2, global_daily_limit=100, clock=FakeClock())
    decisions = [limiter.try_acquire("session-a") for _ in range(3)]
    assert [d.allowed for d in decisions] == [True, True, False]
    assert decisions[1].session_remaining == 0
    assert decisions[2].reason == LimitReason.SESSION_LIMIT_REACHED
    # A different session still has its own allowance.
    assert limiter.try_acquire("session-b").allowed


def test_global_daily_limit(store):
    limiter = UsageLimiter(store, session_limit=10, global_daily_limit=3, clock=FakeClock())
    results = [limiter.try_acquire(f"session-{i}") for i in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert results[3].reason == LimitReason.DAILY_LIMIT_REACHED
    # The rejected session's own allowance was not consumed.
    assert limiter.remaining("session-3") == (10, 0)


def test_limits_reset_on_new_utc_day(store):
    clock = FakeClock("2026-10-10")
    limiter = UsageLimiter(store, session_limit=1, global_daily_limit=1, clock=clock)
    assert limiter.try_acquire("s").allowed
    assert not limiter.try_acquire("s").allowed
    clock.day = "2026-10-11"
    assert limiter.try_acquire("s").allowed


def test_zero_global_limit_blocks_everything(store):
    limiter = UsageLimiter(store, session_limit=5, global_daily_limit=0, clock=FakeClock())
    decision = limiter.try_acquire("s")
    assert not decision.allowed
    assert decision.reason == LimitReason.DAILY_LIMIT_REACHED
