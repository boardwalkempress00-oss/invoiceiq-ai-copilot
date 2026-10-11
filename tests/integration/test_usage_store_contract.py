"""Usage-store contract tests: every store must behave identically.

The Azure Table store runs against Azurite (local emulator) when it is reachable:

    docker run -d -p 127.0.0.1:10002:10002 mcr.microsoft.com/azure-storage/azurite:3.35.0 \\
        azurite-table --tableHost 0.0.0.0 --skipApiVersionCheck --loose

Otherwise those cases are skipped. Set AZURITE_REQUIRED=1 (as CI does) to fail instead of skip.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from collections import Counter as Tally
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from invoiceiq.core.usage_limiter import (
    Counters,
    CounterWrite,
    InMemoryUsageStore,
    LimiterUnavailableError,
    LimitReason,
    SettleOutcome,
    SqliteUsageStore,
    UsageEstimate,
    UsageLimiter,
    UsageStore,
)

AZURITE_HOST, AZURITE_PORT = "127.0.0.1", 10002
# Azurite's publicly documented development account key — not a secret.
AZURITE_CONNECTION_STRING = os.getenv(
    "AZURITE_CONNECTION_STRING",
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/"
    "KBHBeksoGMGw==;TableEndpoint=http://127.0.0.1:10002/devstoreaccount1;",
)


def azurite_available() -> bool:
    try:
        with socket.create_connection((AZURITE_HOST, AZURITE_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def require_azurite() -> None:
    if not azurite_available():
        if os.getenv("AZURITE_REQUIRED") == "1":
            pytest.fail("Azurite is required but not reachable on 127.0.0.1:10002")
        pytest.skip("Azurite not running")


def table_store_factory() -> Callable[[], UsageStore]:
    """Each call returns a *separate client* on the same table, like separate replicas."""
    from azure.data.tables import TableClient

    from invoiceiq.storage.azure_table_usage_store import CLIENT_OPTIONS, AzureTableUsageStore

    name = "usage" + uuid.uuid4().hex[:12]

    def make() -> UsageStore:
        client = TableClient.from_connection_string(
            AZURITE_CONNECTION_STRING, name, **CLIENT_OPTIONS
        )
        return AzureTableUsageStore(client)

    first = make()
    first.ensure_table()  # type: ignore[attr-defined]
    return make


@pytest.fixture(params=["memory", "sqlite", "azure_table"])
def store_factory(request, tmp_path: Path) -> Iterator[Callable[[], UsageStore]]:
    """Factory producing store instances that share the same underlying data."""
    if request.param == "memory":
        shared = InMemoryUsageStore()
        yield lambda: shared  # memory cannot be shared across instances
    elif request.param == "sqlite":
        path = tmp_path / "usage.sqlite3"
        yield lambda: SqliteUsageStore(path)
    else:
        require_azurite()
        yield table_store_factory()


class Clock:
    def __init__(self, day: str = "2026-10-10") -> None:
        self.day = day

    def __call__(self) -> str:
        return self.day


def limiter(store: UsageStore, **overrides) -> UsageLimiter:
    options = {"session_limit": 100, "global_daily_limit": 1_000,
               "daily_cost_limit_micro": None, "clock": Clock(), "max_attempts": 60}  # fmt: skip
    options.update(overrides)
    return UsageLimiter(store, **options)


EST = UsageEstimate(tokens=2_000, cost_micro=1_000)


# --- Store primitives ------------------------------------------------------------------------


def test_read_missing_returns_empty_counters(store_factory):
    result = store_factory().read("2026-10-10", ["global", "session-x"])
    assert result["global"].counters == Counters() and result["global"].etag is None


def test_commit_create_then_conditional_update(store_factory):
    store = store_factory()
    assert store.commit("d1", [CounterWrite("global", Counters(requests=1), None)])
    current = store.read("d1", ["global"])["global"]
    assert current.counters.requests == 1 and current.etag
    assert store.commit("d1", [CounterWrite("global", Counters(requests=2), current.etag)])
    # Stale ETag → conflict, nothing written.
    assert not store.commit("d1", [CounterWrite("global", Counters(requests=99), current.etag)])
    # Create of an existing row → conflict.
    assert not store.commit("d1", [CounterWrite("global", Counters(requests=99), None)])
    assert store.read("d1", ["global"])["global"].counters.requests == 2


def test_multi_row_commit_is_atomic(store_factory):
    store = store_factory()
    assert store.commit("d1", [CounterWrite("global", Counters(requests=1), None)])
    stale = store.read("d1", ["global"])["global"].etag
    fresh = store.read("d1", ["global"])["global"]
    assert store.commit("d1", [CounterWrite("global", Counters(requests=1), fresh.etag)])
    # Session row is new (ok) but the global ETag is stale → the whole commit must fail.
    assert not store.commit(
        "d1",
        [
            CounterWrite("global", Counters(requests=5), stale),
            CounterWrite("session-a", Counters(requests=1), None),
        ],
    )
    after = store.read("d1", ["global", "session-a"])
    assert after["global"].counters.requests == 1
    assert after["session-a"].etag is None


def test_large_counters_round_trip(store_factory):
    store = store_factory()
    big = Counters(requests=3, spent_cost_micro=5_000_000_000, estimated_tokens=2**40)
    assert store.commit("d1", [CounterWrite("global", big, None)])
    assert store.read("d1", ["global"])["global"].counters == big


# --- Reservation and reconciliation ----------------------------------------------------------


def test_success_reconciles_estimate_with_actual(store_factory):
    lim = limiter(store_factory())
    decision = lim.reserve("s1", EST)
    assert decision.allowed and decision.reservation
    reserved = lim.snapshot()
    assert (reserved.reserved_cost_micro, reserved.estimated_tokens) == (1_000, 2_000)
    lim.settle(decision.reservation, SettleOutcome.SUCCEEDED, prompt_tokens=700,
               completion_tokens=150, actual_cost_micro=300)  # fmt: skip
    c = lim.snapshot()
    assert c.reserved_cost_micro == 0 and c.spent_cost_micro == 300
    assert (c.actual_prompt_tokens, c.actual_completion_tokens) == (700, 150)
    assert c.estimated_tokens == 2_000  # estimates and actuals are tracked separately
    assert c.unreconciled == 0 and c.requests == 1


@pytest.mark.parametrize(
    ("outcome", "actual", "spent", "unreconciled"),
    [
        (SettleOutcome.AMBIGUOUS, None, 1_000, 1),  # timeout/5xx: charge the estimate
        (SettleOutcome.SUCCEEDED, None, 1_000, 1),  # no usage reported: charge the estimate
        (SettleOutcome.NOT_BILLED, None, 0, 0),  # certainly no model usage
    ],
)
def test_conservative_settlement(store_factory, outcome, actual, spent, unreconciled):
    lim = limiter(store_factory())
    reservation = lim.reserve("s1", EST).reservation
    lim.settle(reservation, outcome, actual_cost_micro=actual)
    c = lim.snapshot()
    assert (c.spent_cost_micro, c.reserved_cost_micro, c.unreconciled) == (spent, 0, unreconciled)
    assert c.requests == 1  # the attempt always counts against the request limits


def test_daily_cost_budget_counts_reserved_and_spent(store_factory):
    lim = limiter(store_factory(), daily_cost_limit_micro=2_500)
    first, second = lim.reserve("a", EST), lim.reserve("b", EST)
    assert first.allowed and second.allowed
    third = lim.reserve("c", EST)
    assert not third.allowed and third.reason == LimitReason.DAILY_BUDGET_REACHED
    # Settling the first at a lower actual cost frees budget: 100 + 1000 + 1000 <= 2500.
    lim.settle(first.reservation, SettleOutcome.SUCCEEDED, actual_cost_micro=100)
    assert lim.reserve("c", EST).allowed


def test_denied_reservation_consumes_nothing(store_factory):
    lim = limiter(store_factory(), global_daily_limit=1)
    assert lim.reserve("a", EST).allowed
    assert lim.reserve("b", EST).reason == LimitReason.DAILY_LIMIT_REACHED
    assert lim.remaining("b") == (100, 0)
    assert lim.snapshot().reserved_cost_micro == 1_000


def test_utc_day_rollover_and_late_settlement(store_factory):
    clock = Clock("2026-10-10")
    lim = limiter(store_factory(), global_daily_limit=1, clock=clock)
    late = lim.reserve("a", EST).reservation
    assert not lim.reserve("a", EST).allowed
    clock.day = "2026-10-11"
    assert lim.reserve("a", EST).allowed  # new day, fresh limits
    lim.settle(late, SettleOutcome.SUCCEEDED, actual_cost_micro=10)  # yesterday's call finishes
    today = lim.snapshot()
    assert (
        today.requests == 1 and today.reserved_cost_micro == 1_000 and today.spent_cost_micro == 0
    )


# --- Concurrency ("replicas" = separate store instances / clients) ---------------------------


def run_threads(count: int, target: Callable[[int], None]) -> None:
    threads = [threading.Thread(target=target, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_concurrent_reservations_never_exceed_global_limit(store_factory):
    results: Tally[str] = Tally()
    reservations = []
    lock = threading.Lock()
    limiters = [limiter(store_factory(), global_daily_limit=30) for _ in range(4)]

    def worker(i: int) -> None:
        lim = limiters[i % len(limiters)]
        for j in range(3):
            try:
                decision = lim.reserve(f"session-{i}-{j}", EST)
            except LimiterUnavailableError:
                outcome = "unavailable"
            else:
                outcome = decision.reason.value
                if decision.allowed:
                    with lock:
                        reservations.append((lim, decision.reservation))
            with lock:
                results[outcome] += 1

    run_threads(16, worker)  # 48 attempts for 30 slots
    assert results["allowed"] == 30
    assert results["allowed"] + results["daily_limit_reached"] + results["unavailable"] == 48
    snapshot = limiters[0].snapshot()
    assert snapshot.requests == 30 and snapshot.reserved_cost_micro == 30_000

    # Concurrent settlement must also lose nothing.
    def settle(i: int) -> None:
        for lim, reservation in reservations[i::8]:
            lim.settle(reservation, SettleOutcome.SUCCEEDED, actual_cost_micro=10)

    run_threads(8, settle)
    final = limiters[0].snapshot()
    assert final.reserved_cost_micro == 0 and final.spent_cost_micro == 300


def test_concurrent_requests_from_one_session_respect_session_limit(store_factory):
    allowed = []
    limiters = [limiter(store_factory(), session_limit=3) for _ in range(3)]

    def worker(i: int) -> None:
        if limiters[i % 3].reserve("same-session", EST).allowed:
            allowed.append(i)

    run_threads(10, worker)
    assert len(allowed) == 3
    assert limiters[0].remaining("same-session")[0] == 0


# --- Failure handling ------------------------------------------------------------------------


def test_unreachable_table_storage_fails_closed_quickly():
    from azure.data.tables import TableClient

    from invoiceiq.storage.azure_table_usage_store import CLIENT_OPTIONS, AzureTableUsageStore

    with socket.socket() as s:  # find a closed local port
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    conn = AZURITE_CONNECTION_STRING.replace("10002", str(port))
    store = AzureTableUsageStore(TableClient.from_connection_string(conn, "x", **CLIENT_OPTIONS))
    started = time.monotonic()
    with pytest.raises(LimiterUnavailableError):
        limiter(store).reserve("s1", EST)
    assert time.monotonic() - started < 10


def test_concurrent_reservations_never_exceed_the_monetary_budget(store_factory):
    """Many sessions race for a cost budget that fits exactly 12 reservations."""
    est = UsageEstimate(tokens=2_900, cost_micro=2_200)  # ≈ a live gpt-4.1-mini question
    budget = 12 * est.cost_micro
    limiters = [
        limiter(store_factory(), daily_cost_limit_micro=budget, global_daily_limit=1_000)
        for _ in range(4)
    ]
    granted: list = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        lim = limiters[i % len(limiters)]
        for j in range(3):
            try:
                decision = lim.reserve(f"budget-{i}-{j}", est)
            except LimiterUnavailableError:
                continue
            if decision.allowed:
                with lock:
                    granted.append((lim, decision.reservation))
            else:
                assert decision.reason == LimitReason.DAILY_BUDGET_REACHED

    run_threads(16, worker)  # 48 attempts for 12 budget slots
    assert len(granted) == 12
    reserved = limiters[0].snapshot()
    assert reserved.reserved_cost_micro == budget and reserved.spent_cost_micro == 0

    # Settle concurrently at an actual cost lower than the estimate: reservations are released
    # and spend reconciles to exactly the sum of actual costs.
    def settle(i: int) -> None:
        for lim, reservation in granted[i::6]:
            lim.settle(reservation, SettleOutcome.SUCCEEDED, prompt_tokens=1_400,
                       completion_tokens=350, actual_cost_micro=1_300)  # fmt: skip

    run_threads(6, settle)
    final = limiters[0].snapshot()
    assert final.reserved_cost_micro == 0
    assert final.spent_cost_micro == 12 * 1_300
    assert (final.actual_prompt_tokens, final.actual_completion_tokens) == (12 * 1_400, 12 * 350)
    assert final.unreconciled == 0
    # Freed budget is usable again: 12 × 1,300 spent leaves room for 5 more reservations.
    assert limiters[1].reserve("after-settle", est).allowed
