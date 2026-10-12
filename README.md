# InvoiceIQ — AI Finance Operations Copilot

**An AI assistant for accounts payable that never does the maths.** Python validates every
invoice with exact decimal arithmetic against a purchasing policy; a language model then explains
the verified results, cites its evidence, and drafts next steps for a person to review.

> **Portfolio project.** All invoices, companies, people and policies are **fictional**. The
> rules (25% VAT, a DKK 20,000 approval threshold, …) are demo rules, not accounting, tax or
> legal advice.

![Dashboard](docs/screenshots/dashboard.png)

**Status:** deployed on Azure Container Apps (Sweden Central). The public app runs with the
offline mock AI provider; live Azure OpenAI and the event-driven invoice pipeline are built,
deployed and **verified in controlled tests**, and switched off for public visitors. Evidence:
[docs/evidence/production-verification.md](docs/evidence/production-verification.md).

### Try it

**Public app:** <https://ca-invoiceiq-dbmr0.bravewater-5e6a1283.swedencentral.azurecontainerapps.io>

What to expect in the public app today:

- **The first visit can take about 30 seconds.** The app scales to zero when idle (measured
  cold start: 28 s on average), so it costs nothing while nobody is using it.
- **AI answers come from the offline mock model.** Validation, policy citations and evidence are
  real; the wording is generated without a live model, so it costs nothing.
- **No uploads.** You can explore the three fictional sample invoices; uploading your own files is
  disabled for public visitors.
- **The event-driven pipeline is switched off for visitors.** Its measured behaviour in Azure
  (24.5–35.7 s from upload to result, worker starting from zero) is documented in the evidence
  file above.
- **Start here page and processing demo (in development, not yet deployed).** The next release
  adds a Start here page and a *simulated* processing demo: ten fictional scenarios run through
  the real processing and validation code inside the web app, with in-memory stand-ins for
  Blob Storage, Event Grid, Service Bus and Table Storage. Demo results are labelled
  *simulated* and shown next to — never mixed with — the verified Azure reference run. The demo
  uses no Azure storage or messaging, and visitors cannot upload anything.

---

## Contents

[The problem](#the-business-problem) · [What it does](#what-it-does) ·
[Architecture](#architecture) · [Tech stack](#technology-stack) ·
[Implemented vs planned](#implemented-vs-planned) · [Run locally](#run-locally) ·
[Docker](#docker) · [Deploy to Azure](#deploy-to-azure-terraform) ·
[Security and cost controls](#security-and-cost-controls) · [Testing](#testing) ·
[Design decisions](#key-design-decisions-and-trade-offs) · [Limitations](#known-limitations) ·
[Event-driven processing](#event-driven-processing) · [Evidence](docs/evidence/production-verification.md)

## The business problem

Accounts-payable teams check every supplier invoice before paying it: do the line items add up,
is the VAT right, was the purchase approved at the right level, are mandatory fields present?
The checks are repetitive, and mistakes are expensive — overpayments, missing approvals, audit
findings.

Large language models are good at *explaining* such issues and drafting supplier emails, but
they are **unreliable at arithmetic** and can invent facts. InvoiceIQ splits the job:

| Responsibility | Done by | Why |
|---|---|---|
| Totals, VAT, rounding, thresholds, required fields | Deterministic Python (`Decimal`) | Exact, testable, auditable |
| Finding relevant policy text | BM25 keyword retrieval | Cheap, transparent, citable section IDs |
| Explanations, recommendations, email drafts | Azure OpenAI | Natural language over **verified** evidence only |

## What it does

- **Dashboard** — three fictional invoices with their status: *Passed*, *Flagged* or
  *Requires Review*.
- **Invoice Analysis** — line items, stated vs. recalculated totals, findings grouped by root
  cause (e.g. one VAT error that also makes the total wrong), and the policy text behind each.
- **AI Chat** — suggested questions, per-invoice chat history, cited evidence, and a separate
  "verified facts" panel produced by the validation engine, never by the model. Clear labels show
  whether an answer came from Azure OpenAI, the offline mock, or read-only mode.
- **Process Invoice** — submit a fictional invoice and follow it through Blob Storage, Event Grid,
  Service Bus and a background worker. Deployed and verified, but **off by default** in public
  (see [Event-driven processing](#event-driven-processing)).
- **Architecture** — what is deployed, how each part was verified, measured numbers and
  security boundaries.
- **Observability** — real per-session latency, AI latency, token usage and estimated cost.

| Invoice Analysis | AI Chat |
|---|---|
| ![Invoice analysis](docs/screenshots/invoice-analysis.png) | ![AI chat](docs/screenshots/ai-chat.png) |

| Architecture | Observability |
|---|---|
| ![Architecture](docs/screenshots/architecture.png) | ![Observability](docs/screenshots/observability.png) |

The three sample invoices:

| Invoice | Scenario | Result |
|---|---|---|
| NOS-2026-0142 | Correct arithmetic, below the approval threshold | ✓ Passed |
| FBC-2026-0187 | DKK 42,100 net, above the DKK 20,000 threshold, no manager approval | ⚑ Flagged (`APR-001`) |
| KFS-2026-0203 | VAT stated as DKK 3,312.50; 25% of DKK 12,450.00 is DKK 3,112.50 | ! Requires Review (`CALC-003`, `CALC-004`) |

## Architecture

```mermaid
flowchart LR
    user([Visitor]) -- HTTPS --> ui

    subgraph container["Azure Container App · one container · 0–1 replicas"]
        ui["Streamlit UI<br/>:8501 (public)"] -- "HTTP 127.0.0.1" --> api["FastAPI<br/>:8000 (private)"]
        api --> val["Validation engine<br/>Python Decimal"]
        api --> rag["BM25 policy retrieval"]
        api --> lim["Usage limiter<br/>reserve → settle"]
    end

    api -- "managed identity" --> aoai["Azure OpenAI<br/>(mock in production)"]
    lim -- "managed identity · ETags" --> tbl[("Azure Table Storage<br/>daily quotas")]
    container -. "stdout JSON logs" .-> law["Log Analytics"]

    subgraph ep["Event-driven processing · deployed, off by default"]
        blob[("Blob container<br/>invoice-uploads")] -- "BlobCreated" --> eg["Event Grid"]
        eg -- "managed identity" --> sb["Service Bus Basic<br/>queue + dead-letter"]
        sb -- "KEDA" --> job["Container Apps Job<br/>worker (0–3)"]
        job --> jobs[("Table invoicejobs<br/>status + results")]
    end
    api -- "upload" --> blob
    api -- "status" --> jobs
```

**A chat question, step by step:** the API loads the invoice and validates it → retrieves the
policy sections cited by findings plus BM25 matches → **reserves** one request and an
upper-bound cost estimate in Table Storage (atomically, with ETags) → sends instructions,
evidence and the question to the model as separate messages → removes any citation not present
in the evidence → **settles** the reservation with actual token usage → returns the answer
together with verified facts built by Python.

If AI is disabled, over budget, or the limiter is unreachable, the app **fails closed** into a
read-only mode that still shows the verified validation results.

## Technology stack

| Area | Choice |
|---|---|
| Language / packaging | Python 3.12, `uv` (lockfile), `src/` layout |
| Backend | FastAPI, Pydantic v2 |
| Frontend | Streamlit (multipage, light enterprise theme) |
| AI | Azure OpenAI via the official OpenAI SDK (GA v1 endpoint), Entra ID auth; offline mock provider |
| Retrieval | BM25, implemented in ~100 lines (no vector DB) |
| Usage limiting | Azure Table Storage with ETag optimistic concurrency; SQLite/in-memory for local dev |
| Event-driven processing | Blob Storage, Event Grid (system topic, managed-identity delivery), Service Bus **Basic**, Container Apps Job scaled by KEDA |
| Container | Multi-stage Dockerfile, `tini` + Python supervisor, non-root |
| Infrastructure | Terraform (AzureRM 4.81 + AzAPI 2.13): Container Apps and Jobs, Azure OpenAI, Storage, Event Grid, Service Bus, Log Analytics, managed identities |
| CI/CD | GitHub Actions: ruff, pytest + Azurite, Terraform test, Docker smoke test; manual multi-arch image publishing; OIDC deploy (disabled) |
| Quality | pytest (480 tests, 95% coverage), ruff, `terraform test` (15 runs, mocked providers) |

## Status and evidence

**Verified in Azure** = observed in the production subscription; **verified locally** = covered by
automated tests; **planned** = not built. Details and measurements:
[docs/evidence/production-verification.md](docs/evidence/production-verification.md).

| Component | Status |
|---|---|
| Deterministic validation engine (12 rules) | ✅ Verified in Azure |
| BM25 policy retrieval with stable citations | ✅ Verified in Azure |
| FastAPI backend and Streamlit UI | ✅ Verified in Azure (Container Apps, scale to zero) |
| Mock AI provider | ✅ Verified in Azure — production default |
| Azure OpenAI provider (Entra ID, timeouts, bounded retries) | ✅ Verified in Azure in a controlled, IP-restricted test (5 requests); production stays in mock mode |
| Persistent usage limiter on Azure Table Storage | ✅ Verified in Azure |
| Event-driven processing (Blob → Event Grid → Service Bus Basic → Container Apps Job) | ✅ Verified in Azure — 6 test submissions; **off by default** |
| Retries, lease takeover, lock renewal, dead-lettering | 🧪 Verified locally (automated tests); not exercised in Azure |
| Terraform (incl. event-processing resources) | ✅ Applied; plan reports no drift |
| CI, image publishing (GHCR, amd64 + arm64) | ✅ In use |
| Deploy workflow (OIDC) | ✅ Written — **disabled** until credentials are reviewed |
| Public demo of invoice processing (simulated scenario gallery, `DEMO_MODE_ENABLED`) | 🧪 Built and tested locally; not yet deployed |
| Azure AI Search, Document Intelligence, authentication | ⏳ Planned |

## Run locally

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/). No Azure account needed — the mock
AI provider is the default.

```bash
uv sync
uv run pytest

# Terminal 1 — FastAPI (API docs at http://127.0.0.1:8000/docs)
uv run uvicorn invoiceiq.api.main:app_factory --factory --reload

# Terminal 2 — Streamlit (http://localhost:8501)
uv run streamlit run src/invoiceiq/frontend/app.py
```

Configuration is environment-driven; see [.env.example](.env.example). To use a real Azure
OpenAI deployment locally, set `AI_PROVIDER=azure_openai`, `AZURE_OPENAI_ENDPOINT` and
`AZURE_OPENAI_DEPLOYMENT`, and sign in with `az login` (Entra ID; no API key needed).

## Docker

```bash
docker compose up --build        # → http://localhost:8501
```

- One image runs both services under a small supervisor: FastAPI on `127.0.0.1:8000` (not
  reachable from outside the container), Streamlit on `0.0.0.0:8501`.
- Runs as non-root user `10001`; Compose also uses a read-only filesystem, dropped
  capabilities and `no-new-privileges`.
- Health check covers both services; SIGTERM shuts both down gracefully (≈1 s).
- No credentials in the image. ~174 MB compressed (most of it is Streamlit's pandas/pyarrow).

Persistent limiter against the local storage emulator:

```bash
docker compose --profile azurite up --build
# with USAGE_STORE=azure_table and AZURE_TABLE_CONNECTION_STRING in .env (see .env.example)
```

## Deploy to Azure (Terraform)

> Deploying creates billable resources. Read
> [docs/deployment-readiness.md](docs/deployment-readiness.md) first.

Terraform in [infra/terraform](infra/terraform) creates: resource group, Log Analytics
workspace, a Container Apps environment with only the serverless *Consumption* profile, the container app (0–1 replicas,
0.5 vCPU / 1 GiB, HTTPS ingress on 8501), Azure OpenAI account + model deployment (API keys
disabled), storage account (shared keys disabled) + `aiusage` table created through Azure Resource
Manager via AzAPI (no shared-key ACL call), a user-assigned managed
identity with two least-privilege role assignments, and optionally a budget alert and a GitHub
OIDC deploy identity.

1. **Choose a model and check availability and quota** — never assume them:
   ```bash
   az cognitiveservices model list -l swedencentral \
     --query "[?model.name=='<model>'].{version:model.version, skus:model.skus[].name}" -o table
   az cognitiveservices usage list -l swedencentral -o table
   ```
2. **Publish the image** with the manual *Publish image* workflow (GHCR, amd64 + arm64, full-SHA tag,
   only for commits whose CI passed) and copy the digest from the run summary.
3. `cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars` and fill in the
   image, model, version and **real token prices** (Terraform refuses zero prices — the daily cost
   budget depends on them). `ai_provider` defaults to `mock`; set `azure_openai` to opt in to live
   answers. `worker_container_image` (optional, defaults to `container_image`) lets the web app and
   the worker job be rolled out independently.
4. ```bash
   cd infra/terraform
   terraform init
   terraform plan -out tfplan     # review every resource
   terraform apply tfplan         # only after review
   ```
5. Optional: set `github_repository` to create the OIDC deploy identity, then configure the
   repository variables listed at the top of [.github/workflows/deploy.yml](.github/workflows/deploy.yml).

Offline checks (no Azure credentials): `terraform validate` and `terraform test` (plan tests with
mocked providers).

**Event-driven processing** is created only with `event_processing_enabled = true` (requires the
`Microsoft.EventGrid` and `Microsoft.ServiceBus` resource providers), and exposed in the app only
with `async_processing_enabled = true`. Event Grid validates its managed identity when the
subscription is created, so on a first deployment create the dead-letter role assignment, wait
about 10 minutes, then create the subscription. A reusable compatibility test lives in
[infra/manual-tests/eventgrid-servicebus-basic](infra/manual-tests/eventgrid-servicebus-basic).

## Security and cost controls

**AI usage (public demo protection)**

- Per-session limit (default 10 questions/day), global daily request limit (200) and a daily
  **estimated-cost budget** (default USD 1.00).
- Each call **reserves** an upper-bound estimate (prompt ÷ 3 characters per token + the full
  output allowance) before calling the model and **settles** with the provider-reported usage
  afterwards. Timeouts, server errors and missing usage data are charged at the estimate.
- Counters live in Azure Table Storage, shared by all replicas and durable across scale-to-zero;
  ETag-conditioned transactions make concurrent reservations safe.
- **Fail closed:** in production, live AI is refused unless the persistent limiter is reachable
  and model prices are configured. Output tokens, input size, question length and request
  duration are all bounded.
- Azure OpenAI's tokens-per-minute quota is a *throughput* limit, not a spending cap; the
  application budget is what bounds cost. A free Azure budget alert can notify (it does not stop
  spending).

**Application security**

- No secrets anywhere: managed identity for Azure OpenAI and Table Storage; API keys and storage
  shared keys are disabled in Terraform. GitHub deploys use OIDC federation.
- Least privilege: every role assignment is scoped to a single resource. The web app, the worker
  and the KEDA scaler have **separate** managed identities; the worker has no Azure OpenAI role,
  and the scaler identity cannot be used by container code. Event Grid delivers and dead-letters
  with its own identity (dead-letters go to a dedicated storage account).
- Prompt-injection resistance: instructions, evidence (JSON, labelled as data) and the question
  are separate messages; look-alike tags are stripped; citations are verified after generation;
  the facts panel never comes from the model.
- Safe errors: no stack traces or submitted values in responses; correlation IDs link the UI to
  structured JSON logs; prompts and questions are never logged.
- Invoice upload (event-driven mode only, off in public): JSON only, ≤ 64 KB, schema-validated,
  separate daily allowance, private storage, 7-day retention; invoice-supplied text is rendered as
  escaped text, never as Markdown or HTML (security regression tests).
- Other input is validated with Pydantic and length limits.

## Testing

| Suite | Tests | What it covers |
|---|---:|---|
| Unit | 178 | Every validation rule incl. boundaries and rounding, BM25, prompts, citations, providers (fake SDK client), cost accounting, supervisor |
| Processing | 99 | Event parsing, Service Bus peek-lock semantics, leases, duplicates, lock expiry, crash recovery, retries, dead-lettering, concurrency, delivery-count normalisation |
| API | 49 | All endpoints incl. submissions, error envelopes, correlation IDs, read-only modes, isolation from the AI allowance |
| Frontend | 100 | Presentation logic, API client, every Streamlit page via `AppTest`, escaping of untrusted invoice text |
| Integration | 54 | Usage and submission stores and Blob Storage on **Azurite**: ETags, atomic commits, concurrency, end-to-end worker path |
| Terraform | 15 | Plan tests with mocked providers: scaling, keys disabled, least-privilege role scopes, Service Bus Basic, private containers, platform-only scaler identity, feature flags |

```bash
uv run pytest --cov=invoiceiq        # 480 passed, 95% line coverage
# Azurite tests run when the emulator is up (CI requires it):
docker run -d -p 127.0.0.1:10000:10000 -p 127.0.0.1:10002:10002 \
  mcr.microsoft.com/azure-storage/azurite:3.35.0 \
  azurite --blobHost 0.0.0.0 --tableHost 0.0.0.0 --skipApiVersionCheck --loose
cd infra/terraform && terraform init -backend=false && terraform test
```

No automated test calls Azure or a live model. Production behaviour was verified separately,
in controlled and documented runs — see the [evidence](docs/evidence/production-verification.md).

## Key design decisions and trade-offs

- **One container for both processes** keeps hosting at one scale-to-zero app. The trade-off is
  coupled scaling and a custom supervisor; splitting them is easy later because the UI only talks
  HTTP to the API.
- **BM25 instead of embeddings:** the policy is ~20 short sections; keyword retrieval is free,
  deterministic and easy to explain. The `PolicyRetriever` interface allows Azure AI Search later.
- **Findings cascade by design:** expected amounts always derive from quantity × unit price, so a
  wrong line also shows up in the subtotal, VAT and total. The UI groups these and points at the
  earliest failing step.
- **Approval thresholds use the recalculated net amount**, so a supplier cannot dodge approval by
  understating the subtotal.
- **Conservative cost accounting** over-counts when the outcome is ambiguous; the demo may go
  read-only slightly early, but it cannot overspend.

## Known limitations

- The public app runs with the mock provider. Live Azure OpenAI was verified only in a short,
  IP-restricted test (5 requests).
- Event-driven processing is verified with 6 test submissions but switched off for the public.
  Retries, lease takeover and dead-lettering are covered by automated tests, not yet exercised in
  Azure. Processing from upload to result takes 24.5–35.7 s, almost all of it the worker
  starting from zero.
- The processing demo's daily allowances live in memory: best-effort, reset whenever the app
  scales to zero, and per replica. They protect memory and CPU, not a budget — the demo makes no
  paid calls.
- Per-session limits are per browser session; reloading starts a new session. The **global**
  request and cost caps are the hard limits.
- Streamlit holds a WebSocket per open tab, so an idle tab would keep the replica running. An
  **idle pause** (browser-side, configurable) pauses sessions after 15 min without interaction or
  5 min in a hidden tab, with a 60 s warning; chat history is restored on Resume in the same tab.
  Verified on Azure: the app scaled to zero ~6 min after a pause.
- Waking from zero takes ~25–35 s on Azure (measured average 28 s: replica scheduling, image
  pull, container creation, ingress routing); the app itself starts in ~2–3 s. Resume shows a "Starting InvoiceIQ" screen meanwhile; a first
  visit to the URL shows the browser's own loading indicator.
- Observability is per browser session; Azure Monitor dashboards are not yet built.
- Mock answers are templates; only the live model writes free-form text.
- Fictional data only; no authentication, no public uploads, no real financial integrations.

## Event-driven processing

Chat and analysis are **synchronous**. Real accounts-payable teams receive invoices continuously,
so ingestion can also run **asynchronously** — deployed on Azure and verified, off by default:

```mermaid
flowchart LR
    upload["Process Invoice<br/>(API records Pending)"] --> blob[("Blob Storage<br/>private")]
    blob -- "BlobCreated" --> eg["Event Grid<br/>managed identity"]
    eg --> sb["Service Bus Basic queue"]
    eg -. "undeliverable" .-> egdl[("Dead-letter<br/>storage account")]
    sb -- "KEDA, from zero" --> worker["Container Apps Job<br/>same validation engine"]
    sb -. "after 5 deliveries" .-> dlq["Dead-letter queue"]
    worker --> jobs[("Table Storage<br/>status + results")]
    ui["Streamlit / API"] --> jobs
```

- **At-least-once delivery, exactly-once results.** The worker claims each submission with a
  lease (ETag compare-and-set), never completes a message while another worker holds the lease,
  and stores the result **before** completing the message. Duplicates are recognised; transient
  failures retry and then dead-letter with a visible status.
- **Measured on Azure** (6 test submissions): upload to result **24.5–35.7 s**; message queued
  ≈ 0.8 s after upload; validation < 0.1 s. Almost all of the time is the worker starting from
  zero — the trade-off for paying nothing while idle.
- **Concurrency:** three simultaneous submissions started three overlapping executions (the
  configured maximum); the first one drained all three messages, the other two found nothing.
  Correct, slightly wasteful under bursts.
- **Cost:** no fixed monthly charges (Service Bus Basic, storage, Event Grid at this scale);
  estimated DKK 0.1–0.2/month while idle.

Full write-up, including the Event Grid dead-letter authorization finding:
[docs/evidence/production-verification.md](docs/evidence/production-verification.md) and
[docs/design/async-invoice-processing.md](docs/design/async-invoice-processing.md).

### Processing modes and feature flags

| Setting | Effect |
|---|---|
| neither flag (default) | No processing endpoints; `/submissions` and `/demo` answer 404 `feature_disabled` |
| `DEMO_MODE_ENABLED=true` | **Simulated** demo: `/demo/scenarios`, `/demo/runs`, `/demo/reference` and the *Try processing* page. Catalog scenarios only, in memory, bounded (200 runs, 1 h expiry). Never constructs an Azure client. Its daily allowances are **process-local and best-effort**: they reset when the replica restarts or scales to zero and are not shared between replicas. |
| `ASYNC_PROCESSING_ENABLED=true` | Live processing: `/submissions` and the *Process Invoice* page. With `SUBMISSION_BACKEND=azure` this uses Blob Storage, Service Bus and Table Storage — reserved for protected, non-public use. |

The two flags are independent; the public configuration is demo on, live processing off.

## Repository layout

```
src/invoiceiq/
  core/        validation engine, invoice service, BM25 retriever, usage limiter (no web/AI imports)
  ai/          evidence, prompts, providers (mock + Azure OpenAI), chat orchestration
  processing/  submissions, Event Grid parsing, Service Bus access, idempotent worker logic,
               simulated public demo (demo.py)
  worker.py    entry point of the Container Apps Job (no AI imports)
  api/         FastAPI app, routes, schemas, middleware
  frontend/    Streamlit app, views/, API client, presentation helpers
  storage/     Azure Table Storage usage store
  runtime/     container supervisor and health check
data/          fictional invoices, purchasing policy, demo scenarios, verified reference run
infra/terraform/   Azure infrastructure + plan tests
infra/manual-tests/  isolated Event Grid → Service Bus compatibility test
docs/          design notes, production evidence, deployment readiness review, screenshots
```
