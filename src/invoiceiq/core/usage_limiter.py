"""Usage limits that protect the public demo from unexpected AI cost.

Per UTC day the limiter enforces:

* a per-session request limit,
* a global request limit, and
* a global *estimated-cost* budget.

Before every AI call it **reserves** the request plus an upper-bound cost estimate; afterwards it
**settles** the reservation with the actual token usage. Ambiguous outcomes (timeouts, server
errors, missing usage data) are charged at the reserved estimate, so uncertainty always
over-counts, never under-counts.

Concurrency: stores expose optimistic concurrency (read with a version tag, write only if the
tag is unchanged — Azure Table Storage ETags). The reservation algorithm lives here, once, and
retries on conflicts. A store error or exhausted retries raises `LimiterUnavailableError`; the
caller must then refuse the AI call (fail closed).

Stores:

* `InMemoryUsageStore` — development and tests; lost on restart.
* `SqliteUsageStore` — local development; survives restarts on a persistent disk, single machine.
* `AzureTableUsageStore` (invoiceiq.storage) — production; shared by all replicas and durable.

Only stores with `shared_durable = True` may protect live AI calls in production.
"""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

# --- Store primitives ------------------------------------------------------------------------


@dataclass(frozen=True)
class Counters:
    """Usage counters for one key (the global total or one session) on one UTC day."""

    requests: int = 0
    reserved_cost_micro: int = 0  # cost of reservations not yet settled (micro-currency units)
    spent_cost_micro: int = 0  # settled cost: actual where known, else the reserved estimate
    estimated_tokens: int = 0  # sum of pre-call token estimates
    actual_prompt_tokens: int = 0  # provider-reported usage
    actual_completion_tokens: int = 0
    unreconciled: int = 0  # requests settled at their estimate (ambiguous outcome)


COUNTER_FIELDS = tuple(Counters.__dataclass_fields__)


@dataclass(frozen=True)
class Versioned:
    counters: Counters
    etag: str | None  # None: the record does not exist yet


@dataclass(frozen=True)
class CounterWrite:
    key: str
    counters: Counters
    etag: str | None  # expected current version; None means "create, must not exist"


class UsageStoreError(Exception):
    """The store could not be read or written (network, auth, contention...)."""


class UsageStore(Protocol):
    shared_durable: bool

    def read(self, day: str, keys: Sequence[str]) -> dict[str, Versioned]: ...

    def commit(self, day: str, writes: Sequence[CounterWrite]) -> bool:
        """Apply all writes atomically if every etag still matches. False on conflict."""
        ...

    def try_consume(self, day: str, key: str, limit: int) -> int | None: ...

    def get_count(self, day: str, key: str) -> int: ...


class CasUsageStore(ABC):
    """Base class: implement `read`/`commit`; request-count helpers are built on top."""

    shared_durable = False
    max_cas_attempts = 200

    @abstractmethod
    def read(self, day: str, keys: Sequence[str]) -> dict[str, Versioned]: ...

    @abstractmethod
    def commit(self, day: str, writes: Sequence[CounterWrite]) -> bool: ...

    def try_consume(self, day: str, key: str, limit: int) -> int | None:
        """Atomically add one request if below `limit`. Returns the new count, or None."""
        if limit <= 0:
            return None
        for _ in range(self.max_cas_attempts):
            current = self.read(day, [key])[key]
            if current.counters.requests >= limit:
                return None
            updated = replace(current.counters, requests=current.counters.requests + 1)
            if self.commit(day, [CounterWrite(key, updated, current.etag)]):
                return updated.requests
            time.sleep(random.uniform(0, 0.002))
        raise UsageStoreError("too much contention")

    def get_count(self, day: str, key: str) -> int:
        return self.read(day, [key])[key].counters.requests


class InMemoryUsageStore(CasUsageStore):
    shared_durable = False

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest_day = ""
        self._data: dict[tuple[str, str], tuple[Counters, int]] = {}

    def read(self, day: str, keys: Sequence[str]) -> dict[str, Versioned]:
        with self._lock:
            result = {}
            for key in keys:
                item = self._data.get((day, key))
                result[key] = (
                    Versioned(item[0], str(item[1])) if item else Versioned(Counters(), None)
                )
            return result

    def commit(self, day: str, writes: Sequence[CounterWrite]) -> bool:
        with self._lock:
            for w in writes:
                current = self._data.get((day, w.key))
                if (str(current[1]) if current else None) != w.etag:
                    return False
            for w in writes:
                current = self._data.get((day, w.key))
                self._data[(day, w.key)] = (w.counters, current[1] + 1 if current else 1)
            if day > self._latest_day:  # drop previous days (late settlements for them are fine)
                self._latest_day = day
                self._data = {k: v for k, v in self._data.items() if k[0] >= day}
            return True


class SqliteUsageStore(CasUsageStore):
    """Local development store. Each commit is one IMMEDIATE transaction with version checks."""

    shared_durable = False

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        with closing(self._connect()) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS usage_counters_v2 ("
                " day TEXT NOT NULL, key TEXT NOT NULL, version INTEGER NOT NULL,"
                " data TEXT NOT NULL, PRIMARY KEY (day, key))"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def read(self, day: str, keys: Sequence[str]) -> dict[str, Versioned]:
        with closing(self._connect()) as conn:
            rows = {
                key: (version, data)
                for key, version, data in conn.execute(
                    f"SELECT key, version, data FROM usage_counters_v2 WHERE day = ? "
                    f"AND key IN ({','.join('?' * len(keys))})",
                    (day, *keys),
                )
            }
        return {
            k: Versioned(Counters(**json.loads(rows[k][1])), str(rows[k][0]))
            if k in rows
            else Versioned(Counters(), None)
            for k in keys
        }

    def commit(self, day: str, writes: Sequence[CounterWrite]) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for w in writes:
                row = conn.execute(
                    "SELECT version FROM usage_counters_v2 WHERE day = ? AND key = ?", (day, w.key)
                ).fetchone()
                if (str(row[0]) if row else None) != w.etag:
                    conn.execute("ROLLBACK")
                    return False
            for w in writes:
                conn.execute(
                    "INSERT INTO usage_counters_v2 (day, key, version, data) VALUES (?, ?, 1, ?) "
                    "ON CONFLICT(day, key) DO UPDATE SET "
                    "version = version + 1, data = excluded.data",
                    (day, w.key, json.dumps(asdict(w.counters))),
                )
            conn.execute("DELETE FROM usage_counters_v2 WHERE day < ?", (day,))
            conn.execute("COMMIT")
            return True
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


class UnavailableUsageStore(CasUsageStore):
    """Placeholder when the configured store cannot be used: every operation fails closed."""

    shared_durable = True  # stands in for a durable store, so production does not fall back

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def read(self, day: str, keys: Sequence[str]) -> dict[str, Versioned]:
        raise UsageStoreError(self.reason)

    def commit(self, day: str, writes: Sequence[CounterWrite]) -> bool:
        raise UsageStoreError(self.reason)


# --- Limiter ---------------------------------------------------------------------------------


class LimitReason(StrEnum):
    ALLOWED = "allowed"
    SESSION_LIMIT_REACHED = "session_limit_reached"
    DAILY_LIMIT_REACHED = "daily_limit_reached"
    DAILY_BUDGET_REACHED = "daily_budget_reached"


class SettleOutcome(StrEnum):
    SUCCEEDED = "succeeded"  # charge actual cost if reported, else the estimate
    NOT_BILLED = "not_billed"  # certainly no model usage (no call made, or rejected auth)
    AMBIGUOUS = "ambiguous"  # timeout, server error, unknown failure: charge the estimate


@dataclass(frozen=True)
class UsageEstimate:
    tokens: int = 0
    cost_micro: int = 0


@dataclass(frozen=True)
class Reservation:
    day: str
    session_key: str
    estimate: UsageEstimate


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    reason: LimitReason
    session_remaining: int
    global_remaining: int
    reservation: Reservation | None = None


class LimiterUnavailableError(Exception):
    """Usage could not be checked or recorded. Callers must not make the AI call."""


def _utc_day() -> str:
    return datetime.now(UTC).date().isoformat()


def _session_key(session_id: str) -> str:
    # Hashed so raw session identifiers are never stored.
    return "session-" + hashlib.sha256(session_id.encode()).hexdigest()[:32]


class UsageLimiter:
    GLOBAL_KEY = "global"

    def __init__(
        self,
        store: UsageStore,
        *,
        session_limit: int,
        global_daily_limit: int,
        daily_cost_limit_micro: int | None = None,
        clock: Callable[[], str] = _utc_day,
        max_attempts: int = 10,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._store = store
        self._session_limit = session_limit
        self._global_limit = global_daily_limit
        self._cost_limit = daily_cost_limit_micro
        self._clock = clock
        self._max_attempts = max_attempts
        self._sleep = sleep

    @property
    def store(self) -> UsageStore:
        return self._store

    def _read(self, day: str, keys: Sequence[str]) -> dict[str, Versioned]:
        try:
            return self._store.read(day, keys)
        except Exception as exc:
            raise LimiterUnavailableError(str(exc)) from exc

    def _commit(self, day: str, writes: Sequence[CounterWrite]) -> bool:
        try:
            return self._store.commit(day, writes)
        except Exception as exc:
            raise LimiterUnavailableError(str(exc)) from exc

    def _backoff(self, attempt: int) -> None:
        self._sleep(random.uniform(0, min(0.4, 0.02 * 2**attempt)))

    def snapshot(self) -> Counters:
        """Today's global counters."""
        day = self._clock()
        return self._read(day, [self.GLOBAL_KEY])[self.GLOBAL_KEY].counters

    def remaining(self, session_id: str | None = None) -> tuple[int | None, int]:
        day = self._clock()
        keys = [self.GLOBAL_KEY] + ([_session_key(session_id)] if session_id else [])
        current = self._read(day, keys)
        global_left = max(0, self._global_limit - current[self.GLOBAL_KEY].counters.requests)
        if session_id is None:
            return None, global_left
        used = current[_session_key(session_id)].counters.requests
        return max(0, self._session_limit - used), global_left

    def reserve(self, session_id: str, estimate: UsageEstimate | None = None) -> LimitDecision:
        """Atomically reserve one request (and its estimated cost) for the session."""
        estimate = estimate or UsageEstimate()
        day = self._clock()
        session_key = _session_key(session_id)
        for attempt in range(self._max_attempts):
            current = self._read(day, [self.GLOBAL_KEY, session_key])
            g, s = current[self.GLOBAL_KEY], current[session_key]
            global_left = max(0, self._global_limit - g.counters.requests)
            session_left = max(0, self._session_limit - s.counters.requests)
            # Global limits are checked first so an exhausted day does not burn session allowance.
            if global_left <= 0:
                return LimitDecision(False, LimitReason.DAILY_LIMIT_REACHED, session_left, 0)
            if self._cost_limit is not None and (
                g.counters.spent_cost_micro + g.counters.reserved_cost_micro + estimate.cost_micro
                > self._cost_limit
            ):
                return LimitDecision(
                    False, LimitReason.DAILY_BUDGET_REACHED, session_left, global_left
                )
            if session_left <= 0:
                return LimitDecision(False, LimitReason.SESSION_LIMIT_REACHED, 0, global_left)

            new_global = replace(
                g.counters,
                requests=g.counters.requests + 1,
                reserved_cost_micro=g.counters.reserved_cost_micro + estimate.cost_micro,
                estimated_tokens=g.counters.estimated_tokens + estimate.tokens,
            )
            new_session = replace(s.counters, requests=s.counters.requests + 1)
            if self._commit(
                day,
                [
                    CounterWrite(self.GLOBAL_KEY, new_global, g.etag),
                    CounterWrite(session_key, new_session, s.etag),
                ],
            ):
                return LimitDecision(
                    True,
                    LimitReason.ALLOWED,
                    session_left - 1,
                    global_left - 1,
                    Reservation(day, session_key, estimate),
                )
            self._backoff(attempt)  # another request won the race; re-read and retry
        raise LimiterUnavailableError("could not reserve usage: too much contention")

    def try_acquire(self, session_id: str) -> LimitDecision:
        """Reserve one request with no cost estimate (request counting only)."""
        return self.reserve(session_id)

    def settle(
        self,
        reservation: Reservation,
        outcome: SettleOutcome,
        *,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        actual_cost_micro: int | None = None,
    ) -> None:
        """Release the reservation and record what was actually used.

        The request itself stays counted in every case.
        """
        est = reservation.estimate
        has_actual = outcome == SettleOutcome.SUCCEEDED and actual_cost_micro is not None
        if outcome == SettleOutcome.NOT_BILLED:
            charge = 0
        elif has_actual:
            charge = actual_cost_micro or 0
        else:
            charge = est.cost_micro  # ambiguous: assume the worst case we reserved
        for attempt in range(self._max_attempts):
            current = self._read(reservation.day, [self.GLOBAL_KEY])[self.GLOBAL_KEY]
            c = current.counters
            updated = replace(
                c,
                reserved_cost_micro=max(0, c.reserved_cost_micro - est.cost_micro),
                spent_cost_micro=c.spent_cost_micro + charge,
                actual_prompt_tokens=c.actual_prompt_tokens + (prompt_tokens or 0),
                actual_completion_tokens=c.actual_completion_tokens + (completion_tokens or 0),
                unreconciled=c.unreconciled
                + (0 if has_actual or outcome == SettleOutcome.NOT_BILLED else 1),
            )
            if self._commit(
                reservation.day, [CounterWrite(self.GLOBAL_KEY, updated, current.etag)]
            ):
                return
            self._backoff(attempt)
        raise LimiterUnavailableError("could not settle usage: too much contention")
