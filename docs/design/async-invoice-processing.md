# Asynchronous invoice processing (Phase B — local implementation)

Upload → private Blob Storage → Event Grid `BlobCreated` → Service Bus (Basic) queue →
Container Apps event-triggered Job → existing validation engine → Table Storage → Streamlit.
Off by default (`ASYNC_PROCESSING_ENABLED=false`). Nothing here calls Azure OpenAI.

## Status lifecycle

```
Pending ──claim──▶ Processing ──▶ Completed     (findings stored)
   ▲                   │      ├─▶ Failed        (invoice unusable — final, never retried)
   └──transient error──┘      └─▶ DeadLettered  (retries exhausted — final)
```

Terminal states never change. Every transition is a compare-and-set on the row's ETag.
The API additionally reports **Stalled** for a non-terminal row with no active lease and no
progress for `SUBMISSION_STALLED_AFTER_SECONDS` (default 180 s) — typically a message the broker
dead-lettered after the worker crashed repeatedly.

## Ordering that makes the pipeline race-free

1. API checks size/JSON syntax, takes one unit of the **upload** allowance, then creates the
   **Pending row before the blob exists** — every event refers to a known submission.
2. Blob upload is create-only (`If-None-Match: *`): one blob, one `BlobCreated` event. The
   correlation ID is sent as `x-ms-client-request-id` and arrives as `data.clientRequestId`.
3. The worker **persists the terminal row first, then completes the message**. A crash in
   between causes a redelivery, which sees the terminal row and completes as a duplicate.

## Concurrent workers, leases and locks

* A worker claims a row by writing `Processing` + `lease_owner` + `lease_expires_at`
  (lease = message lock duration, 60 s).
* If another worker holds an **unexpired** lease, the worker **does not complete its message**.
  It waits (renewing its message lock) for up to `contention_wait_seconds`:
  terminal row → complete as duplicate; lease expired → take over; otherwise **abandon** so
  Service Bus redelivers later.
* A crashed worker's lease simply expires; the next delivery takes over (`attempts + 1`).
* If the final write fails with an ETag conflict (our lease expired and someone took over), the
  worker re-reads: terminal → complete as duplicate; otherwise abandon.
* Long work: before persisting, the worker renews the message lock and extends the lease
  (compare-and-set) whenever less than `renew_margin_seconds` remain. A run stops taking new
  messages when less than one lease + margin of its time budget is left (budget 240 s < job
  replica timeout 300 s). A lock lost after persisting is harmless (duplicate on redelivery).

## Error policy

| Problem | Where | Result |
|---|---|---|
| Empty, not UTF-8, not JSON, not an object, `NaN`/`Infinity` | API, synchronous | HTTP 422 `invalid_json`; nothing stored; allowance not used |
| Larger than 64 KB | API, synchronous | HTTP 413 `too_large`; nothing stored |
| Valid JSON, invalid invoice schema | Worker | `Failed` / `invalid_schema` with field-level details (values never echoed); message completed |
| Blob missing (retention) or oversized | Worker | `Failed` / `blob_not_found` · `too_large`; message completed |
| Storage/queue errors, permission denied, unexpected bug | Worker | Row back to `Pending` with `transient_error`, backoff, abandon → retry. On delivery `WORKER_MAX_DELIVERY_COUNT`: row `DeadLettered`, message dead-lettered (`processing-failed`) |
| Not a BlobCreated event for our container/account/blob pattern | Worker | Dead-lettered (`unexpected-event`); storage untouched |
| Event for a submission without a row | Worker | Dead-lettered (`unknown-submission`) |
| Blob upload fails in the API | API | Row `Failed` / `upload_failed`; HTTP 503; no event |

## Allowances

Uploads have their own daily allowance (`SUBMISSION_SESSION_DAILY_LIMIT`,
`SUBMISSION_GLOBAL_DAILY_LIMIT`), stored as counter rows in the `invoicejobs` table — completely
separate from the AI chat allowance (`aiusage` table, `SESSION_REQUEST_LIMIT`, …). Uploading never
uses AI questions and an exhausted AI allowance never blocks uploads (tested).

## Retention and cleanup

* **Blobs:** deleted after 7 days by a storage lifecycle policy (Terraform, Phase C) — guaranteed
  by the platform.
* **Status rows:** cleanup is **opportunistic, not guaranteed**: each worker run deletes at most
  50 expired rows and ignores failures. If no job runs, rows remain. The API hides rows past
  `expires_at` regardless, so expired data is never served.

## Local development

`SUBMISSION_BACKEND=local` (default) runs an in-process simulation: in-memory blob store and
status store, an Event Grid–shaped event, an in-memory queue with Service Bus peek-lock semantics,
and the real `InvoiceProcessor` on a background thread.

```
ASYNC_PROCESSING_ENABLED=true uv run uvicorn invoiceiq.api.main:app_factory --factory
uv run streamlit run src/invoiceiq/frontend/app.py        # "Process Invoice" page appears
```

`SUBMISSION_BACKEND=azure` with the Azurite connection strings exercises the real Blob and Table
code paths locally (the worker then needs a Service Bus queue).

## Not yet verified (requires Azure — Phase D/E)

* Event Grid delivering to a **Service Bus Basic** queue with managed identity.
* The KEDA `azure-servicebus` scaler authenticating with a managed identity, and the role it
  needs (KEDA documents *Manage* rights → likely *Azure Service Bus Data Owner* on the queue).
* How the broker numbers deliveries. The Python SDK exposes the raw AMQP `delivery-count`
  (defined as *prior* attempts, 0 on first delivery), which the adapter normalises to 1-based.
  If the broker already counts from 1, the worker dead-letters one attempt early (status still
  accurate); if the opposite, the broker dead-letters first and the row shows as Stalled.
* Real cold-start latency of the job (expected 20–40 s from upload to Completed).
