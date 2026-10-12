# Production verification — InvoiceIQ on Azure

What has actually been deployed and tested in Azure (Sweden Central), what is only covered by
automated tests, and what is still planned. Every number below was measured during the runs
described; cost figures are **estimates** from list prices unless stated otherwise.

| Label | Meaning |
|---|---|
| **Verified in Azure** | Observed in the production subscription, with logs or metrics as evidence |
| **Verified locally** | Covered by automated tests (unit, integration with Azurite, Terraform plan tests) |
| **Simulated** | Runs the real code with in-memory stand-ins (local development pipeline) |
| **Planned** | Designed, not built |

## Summary

| Capability | Status |
|---|---|
| Web app (Streamlit + FastAPI) on Container Apps, scale to zero | Verified in Azure |
| Deterministic validation, BM25 citations, mock AI, usage limiter on Table Storage | Verified in Azure |
| Azure OpenAI (gpt-4.1-mini) answers with citations | Verified in Azure — controlled test only (5 requests); production runs in mock mode |
| Upload → Blob Storage → Event Grid → Service Bus Basic → Container Apps Job → result | Verified in Azure — 6 submissions (E1, E2); switched off for the public |
| Duplicate handling, leases, retries, dead-lettering, lock renewal | Verified locally (99 processing tests incl. concurrency and failure scenarios) — **not exercised in Azure** |
| Public demo of invoice processing | Planned (simulated scenario gallery, clearly labelled) |

The public site runs with `AI_PROVIDER=mock` and `ASYNC_PROCESSING_ENABLED=false`. Live AI and
asynchronous processing were switched on only for short tests behind a temporary IP allowlist
whose enforcement was checked first (a non-allowed source received HTTP 403).

## Phase D — event-processing infrastructure (2026-10-11 → 2026-10-12)

Deployed with Terraform behind `event_processing_enabled` (default off):

- Service Bus **Basic** namespace and queue `invoice-events` (local auth disabled, TLS 1.2,
  5 deliveries, 60 s lock, 1-day TTL, dead-letter on expiry).
- Event Grid system topic on the main storage account; subscription for `BlobCreated` on
  `invoice-uploads/submissions/*.json` (`data.api = PutBlob`), delivered **with the topic's
  managed identity**, dead-lettered with the same identity.
- Private containers, table `invoicejobs`, 7-day lifecycle deletion.
- Container Apps Job `worker` (event-triggered, 0–3 executions, KEDA Service Bus rule).
- Separate managed identities for the web app, the worker and the KEDA scaler; the scaler
  identity is configured as platform-only (`identitySettings.lifecycle = None`).

**Compatibility test (isolated resource group, destroyed afterwards).** Event Grid accepted a
Service Bus **Basic** queue as a destination; a test `BlobCreated` event arrived in 4.4 s with the
correlation ID carried through; a non-matching blob produced no message. Raw SDK
`delivery_count` was 0 on the first delivery and 1 after an abandon, which confirmed the worker's
normalisation (+1).

**Finding: dead-letter authorization scope.** The first apply created everything except the
event subscription, which Azure rejected:

> Managed identity does not have authorization to deliver to the deadletter endpoint.

The role was scoped to the dead-letter **container**. A controlled test in an isolated resource
group compared two configurations that differed only in that scope, each after at least 10 minutes
of role propagation:

| Dead-letter role scope | Subscription creation |
|---|---|
| Container (role 21 min old) | Failed — same error |
| Storage account (role 10 min old) | Succeeded; delivery to Service Bus Basic verified |

Production therefore uses a **dedicated dead-letter storage account** (shared keys off, private,
7-day retention), and the topic identity's role is scoped to that account only — never to the
main account that holds uploads and the usage table. The deployment was completed in two stages
(role first, subscription after a 10-minute wait). The original container and its unused
container-scoped role remain, pending an approved clean-up.

Other issues found and fixed during Phase D:

- Two permanent no-op plan differences (Azure stores the lock duration as `PT1M`, and returns
  identity IDs with a lower-case `resourcegroups` segment); configuration now matches.
- In the isolated test, an AzureRM in-place update of a Basic queue waited ~10 minutes for
  readiness although Azure had applied it immediately. Production plans avoid queue updates.

Final state: Terraform plan **No changes**; worker idle with zero executions; no Azure OpenAI
requests.

## Phase E1 — one end-to-end submission (2026-10-12)

Fictional sample `inv-003.json` (KFS-2026-0203), uploaded through the Process Invoice page.

| Step (UTC) | Time | Since upload |
|---|---|---|
| Submission accepted (Pending), blob stored | 06:53:53 | +0.0 s |
| Message queued in Service Bus | 06:53:54 | +0.8 s |
| KEDA detects the message, creates 1 execution | 06:53:58 | +4.5 s |
| Image pulled (6.6 s), container started | 06:54:20 | +26.6 s |
| Worker claims the submission and completes | 06:54:22 | +29.3 s |

- Result: **Requires Review** — `CALC-003` (VAT 3,312.50 stated, 3,112.50 calculated) and
  `CALC-004` (total 15,762.50 stated, 15,562.50 calculated), identical to the local result.
- The API's correlation ID appeared in the worker's logs, carried through the upload's
  `client-request-id` → Event Grid → Service Bus → worker.
- Event Grid: 1 published / 1 matched / 1 delivered, 0 failures. Service Bus: 1 incoming,
  1 completed, 0 dead-lettered. One job execution (succeeded), delivery count 1, no retries.
- The result row was written before the message was completed (worker log order: claimed →
  validated → message completed).
- Azure OpenAI requests: 0.

## Phase E2 — reliability and concurrency (2026-10-12)

Five submissions, the authorised maximum.

| # | Scenario | Result | Upload → final status |
|---|---|---|---|
| 1 | Valid JSON, invalid invoice schema | **Failed `invalid_schema`**, 3 field errors; submitted values not shown | 30.4 s |
| 2 | Sample FBC-2026-0187 via the sample flow | **Flagged** (`APR-001`) | 35.7 s |
| 3 | Concurrent upload — NOS-2026-0142 | **Passed** | 25.1 s |
| 4 | Concurrent upload — FBC-2026-0187 | **Flagged** (`APR-001`) | 25.1 s |
| 5 | Concurrent upload — KFS-2026-0203 | **Requires Review** (`CALC-003`, `CALC-004`) | 24.5 s |

All results matched results computed locally beforehand.

- Each submission was claimed exactly once (attempt 1, delivery count 1); 4 completed and 1
  failed by design. No retries, duplicates or dead-letters.
- Event Grid: 5 published / 5 matched / 5 delivered. Service Bus: 5 incoming, 5 completed,
  0 abandoned, 0 dead-lettered. Storage: 5 new blobs and 5 new status rows, no deletions.
- Azure OpenAI requests: 0.

**Concurrency observation.** The three concurrent clicks (within 3 ms) led KEDA to start three
overlapping executions — exactly the configured maximum. The first execution received all three
messages and processed them within 0.2 s, because each run keeps receiving until the queue is
empty; the other two started 1.7 s and 3.4 s later, found nothing and exited cleanly. Correct
and safe, but bursts can start executions that do no work (here about 16 vCPU-seconds). Possible
tuning: let each execution take a single message, or lower the maximum.

## Latency and the cold-start trade-off

| Measurement | Result | Evidence |
|---|---|---|
| Upload → final status, worker starting from zero | 24.5 – 35.7 s (6 submissions) | E1, E2 status timelines |
| Upload → message in Service Bus | ≈ 0.8 s | E1 |
| Of which worker start-up (KEDA poll, scheduling, image pull, container creation) | ≈ 22 – 33 s | Job events and worker logs |
| Validation in the worker | < 0.1 s | Worker logs |
| Web app wake-up from zero after Resume | 24.6 / 33.1 / 26.4 s (average 28.0 s) | Three cold starts after probe tuning (2026-10-11) |
| Web app scale-to-zero after an idle pause | ≈ 6 minutes | Replica counts (2026-10-11) |

Both the web app and the worker cost nothing while idle; the price is roughly half a minute of
start-up when work arrives. An always-on replica would remove most of that delay at a recurring
cost, which this demo deliberately avoids.

## Security boundaries

| Identity | Access (each role scoped to a single resource) |
|---|---|
| Web app | Azure OpenAI inference; tables `aiusage` and `invoicejobs`; container `invoice-uploads` (read/write) |
| Worker | Container `invoice-uploads` (read); table `invoicejobs`; Service Bus queue (receive). No Azure OpenAI role |
| KEDA scaler | Service Bus queue (Data Owner, needed for metrics); not available to container code |
| Event Grid topic | Service Bus queue (send); dedicated dead-letter account; plus the unused legacy container role pending clean-up |

- No keys or connection strings: storage shared keys, Service Bus local auth and Azure OpenAI
  keys are disabled; all access uses managed identities.
- Private containers, HTTPS only, the API is not exposed outside the container.
- Submitted values are never logged; the Process Invoice page renders invoice-supplied text as
  escaped text (covered by security regression tests).

## Cost

- **Incremental monthly cost of the event-processing infrastructure:** estimated DKK 0.1–0.2
  while idle — no fixed charges (Service Bus Basic, storage accounts and Event Grid have none at
  this scale; the job costs nothing with zero executions). Estimated from Azure retail list
  prices for Sweden Central (October 2026). Actual billing has not been reviewed yet.
- **Test runs:** E1 estimated under DKK 0.01; E2 estimated under DKK 0.02 (about 45 vCPU-seconds
  of job time, within the Container Apps free grant).
- **Azure OpenAI controlled test (2026-10-11):** 5 requests, about USD 0.005 calculated from the
  reported tokens and configured prices.
- The resource-group budget (DKK 75/month) is an alert, not a spending cap.

## Not yet verified in Azure

- Retries after transient failures, lease takeover after a worker crash, lock renewal under
  slow processing, and dead-lettering (Service Bus and Event Grid) — covered by automated tests
  only.
- Lifecycle deletion of uploads and dead-letters after 7 days.
- Behaviour under sustained load or more than three simultaneous submissions.
- The Process Invoice page often shows *Pending* then the final state directly, because
  processing finishes within its 2-second refresh; the status table still records each step.
