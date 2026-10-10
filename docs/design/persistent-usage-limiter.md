# Design: Persistent AI usage limiter (Azure Table Storage)

**Status:** Implemented (Phase 5B) and tested against Azurite. Not yet run against a real
storage account. Code: `src/invoiceiq/core/usage_limiter.py` (algorithm, memory/SQLite stores),
`src/invoiceiq/storage/azure_table_usage_store.py` (Azure Table adapter).
**Safeguard:** with `APP_ENV=production`, live Azure OpenAI calls are refused unless the usage
store is `shared_durable` and reachable, and model prices are configured.

> **Implementation notes (differences from the original plan below).** No per-reservation
> entities are written: the reservation lives in the request that made it. If the process dies
> between reserve and settle, the reserved estimate simply stays counted for that day — the safe
> direction. Rate-limited (429) calls are charged at the estimate rather than released, because
> the SDK may have retried after an earlier attempt that the service processed.

## 1. Why

The public demo must cap AI spend even when:

- Azure Container Apps scale to zero and back (the container and its memory are replaced);
- more than one replica runs at the same time;
- many visitors open sessions at once.

In-memory and local SQLite counters fail the first two conditions. A shared, durable store is
required.

**Azure OpenAI quotas are not a spending cap.** A deployment's tokens-per-minute (TPM) quota
limits *throughput*, not *total cost*: a deployment allowed 1,000 TPM can still consume
~1.4 million tokens per day. TPM is a useful secondary brake, and Azure budget alerts give
visibility, but neither replaces an application-level daily budget. Budget alerts notify; they
do not stop usage.

## 2. Requirements

| # | Requirement | How it is met |
|---|---|---|
| R1 | Atomic, concurrency-safe reservations | Optimistic concurrency with ETags (`If-Match`) and bounded retry |
| R2 | Global daily AI budget | One counter entity per UTC day: request count *and* token budget |
| R3 | Per-session request limits | One counter entity per (UTC day, session hash) |
| R4 | Separate estimated vs. actual token usage | Reserve with an *estimate* before the call; record *actual* usage after |
| R5 | UTC daily reset | Partition key is the UTC date; a new day uses new entities |
| R6 | Fail closed | Any storage error, timeout or exhausted retry → no AI call, read-only answer |
| R7 | No in-memory enforcement in production | Store is the single source of truth; no local caches of remaining quota |
| R8 | Keep SQLite/memory for local development | Same `UsageStore` interface; selected by `USAGE_STORE` |

## 3. Data model

Table `aiusage` (one storage account, Standard LRS):

| PartitionKey | RowKey | Properties |
|---|---|---|
| `2026-10-10` (UTC date) | `global` | `requests`, `reserved_tokens`, `actual_prompt_tokens`, `actual_completion_tokens`, `estimated_cost_micro_usd` |
| `2026-10-10` | `session-<sha256(session_id)[:32]>` | `requests` |
| `2026-10-10` | `reservation-<uuid>` | `estimated_tokens`, `status` (reserved/settled/released), `created_at` |

- Session IDs are hashed so raw identifiers are never stored.
- Partitioning by date keeps each day's counters in one partition, which makes entity group
  transactions possible and lets old days be deleted (or expired with a lifecycle job).
- Costs are stored as integer micro-units to avoid floating point.

## 4. Reservation protocol

```
reserve(session_id, estimated_tokens):
    for attempt in 1..MAX_ATTEMPTS (e.g. 5, with jittered backoff 50–400 ms):
        read global + session entities for today (with ETags; create if absent)
        if global.requests >= GLOBAL_LIMIT
           or global.reserved_tokens + estimated_tokens > DAILY_TOKEN_BUDGET: deny(daily_limit)
        if session.requests >= SESSION_LIMIT: deny(session_limit)
        submit ONE entity-group transaction (same partition):
            update global  (If-Match: global.etag)  requests+1, reserved_tokens+=estimated
            upsert session (If-Match: session.etag or insert-if-absent) requests+1
            insert reservation-<uuid> (status=reserved)
        on 412 Precondition Failed / 409 Conflict: retry (someone else won the race)
        on success: return reservation id
    raise LimiterUnavailable        # exhausted retries → fail closed

settle(reservation_id, actual_prompt_tokens, actual_completion_tokens, cost):
    ETag-protected update of global: reserved_tokens -= estimated,
        actual_* += actual, estimated_cost += cost; reservation.status = settled
    (best effort: on failure the reservation stays "reserved", which over-counts — the safe
     direction)
```

- **Estimate before the call:** `estimated_tokens = prompt_chars / 4 + max_output_tokens`.
  Because the output cap is included, the reservation is an upper bound for that request.
- **Actual after the call:** recorded from the provider's `usage` field. Observability shows
  both numbers; the budget check uses `reserved + actual`, never just actual.
- Entity group transactions in one partition are atomic, so the global and session counters
  can never drift apart.

## 5. Failure behaviour (fail closed)

| Situation | Result |
|---|---|
| Storage unreachable, auth failure, throttling, timeout | `LIMITER_UNAVAILABLE` → read-only answer, no AI call |
| ETag conflicts exceed retry budget | Same as above |
| AI call fails after reservation | Reservation is released only if no tokens were consumed; otherwise settled with the reserved estimate |
| `settle` fails | Reservation remains counted at its estimate (over-counts, never under-counts) |

The fail-closed path is already implemented and tested for the existing stores
(`test_limiter_failure_fails_closed`, `test_health_reports_unavailable_limiter`).

## 6. Security and cost

- Authentication: the Container App's **managed identity** with the
  *Storage Table Data Contributor* role, scoped to the storage account. No connection strings.
- Network: public endpoint with HTTPS only and shared-key access disabled is sufficient for the
  demo; private endpoints are a later hardening step (they add cost).
- Expected cost: a Standard LRS storage account with a few thousand table transactions per day
  is in the range of cents per month. Exact prices vary by region; check the Azure pricing
  calculator before provisioning.

## 7. Interface changes

The current `UsageStore.try_consume(day, key, limit)` covers request counting. The table store
needs reservation semantics, so the plan is to extend the abstraction rather than replace it:

```python
class UsageStore(Protocol):
    shared_durable: bool

    def reserve(
        self, day, session_key, *, session_limit, global_limit, token_budget, estimated_tokens
    ) -> Reservation | Denial: ...
    def settle(self, reservation, *, actual: TokenUsage, cost: Decimal) -> None: ...
    def snapshot(self, day) -> UsageSnapshot: ...  # for /health and Observability
```

Memory and SQLite stores implement the same methods (SQLite with a single transaction), so all
existing limiter tests run against every store.

## 8. Emulator limitations (Azurite)

Azurite faithfully emulates the Table API's ETag (`If-Match`) checks and entity-group
transactions, which is what the concurrency tests rely on. It does **not** reproduce:

- **Microsoft Entra ID / RBAC.** Azurite uses the shared development key; the managed-identity
  path and the *Storage Table Data Contributor* role are only exercised in Azure.
- **Throttling and latency.** Real accounts can return 503 *ServerBusy* or be slow; the store
  uses short timeouts and one retry, then fails closed.
- **Exact error payloads.** E.g. a malformed ETag returns 400 in Azurite. Any non-409/412 error is
  treated as unavailable (fail closed), so payload differences cannot over-allocate.
- **Network, firewall and `allowSharedKeyAccess=false` settings.**

## 9. Test plan

- Contract tests shared by all stores (memory, SQLite, table).
- Table store against **Azurite** (the local Azure Storage emulator) in CI — no Azure account
  needed: concurrent reservations from many threads/processes never exceed limits; ETag
  conflicts retry; day rollover; fail-closed on emulator shutdown.
- One manual smoke test against a real storage account before go-live (requires approval).
