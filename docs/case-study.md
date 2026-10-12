# Case study — InvoiceIQ, an AI copilot for invoice checking

*A portfolio project on Azure. All invoices, companies and people are fictional. Every
measurement below comes from [production-verification.md](evidence/production-verification.md).*

**Evidence labels used here:** **verified in Azure** (observed in the production subscription),
**verified locally** (automated tests only), **simulated** (the real code with in-memory
stand-ins, as in the public demo).

## 1. Business problem

Accounts-payable teams check every supplier invoice before payment: arithmetic, VAT, approval
level and mandatory fields. The work is repetitive and errors are costly. Language models are
useful for explaining issues and drafting supplier emails, but they are unreliable at arithmetic
and can invent facts. The goal: an assistant that **never decides whether an invoice is
correct** — it only explains decisions made by deterministic code.

## 2. Requirements

**Functional**

- Validate invoices against a purchasing policy: line totals, subtotal, VAT, total, VAT rate,
  currency, required fields and approval thresholds (12 rules).
- Explain the findings in plain language, citing the policy sections they rely on.
- Process incoming invoices asynchronously, as a real AP inbox would.
- Offer a public demo that a visitor can use in a minute, without uploading anything.

**Non-functional**

- Exact, auditable arithmetic; the AI cannot change an amount or a status.
- Near-zero cost while idle; a hard cap on AI spend.
- No secrets; least-privilege access; nothing a public visitor does may write to Azure storage
  or queues.
- Every claim backed by a test or a documented measurement.

## 3. Architecture and technology choices

- **One container on Azure Container Apps** (0–1 replicas): Streamlit UI on the public port,
  FastAPI on localhost only. Python 3.12, Pydantic, `Decimal`.
- **Validation engine** in pure Python; **BM25** retrieval over ~20 policy sections (no vector
  database); **Azure OpenAI** behind a provider interface, with an offline **mock** provider.
- **Usage limiter** on Azure Table Storage (ETag-conditioned reserve-and-settle), shared across
  replicas and durable across scale-to-zero.
- **Event-driven pipeline:** Blob Storage → Event Grid (system topic, managed-identity delivery)
  → Service Bus **Basic** queue → Container Apps **Job** started from zero by KEDA → results in
  Table Storage.
- **Terraform** (AzureRM + AzAPI) for all infrastructure; **GitHub Actions** for lint, tests with
  the Azurite emulator, Terraform plan tests and Docker smoke tests; images published manually,
  multi-arch, pinned by digest.

## 4. Five design decisions and their trade-offs

1. **Deterministic validation, AI for language only.** The model receives verified findings as
   labelled evidence and its citations are checked after generation; the "verified facts" panel
   is built by Python. *Trade-off:* the AI cannot catch issues the rules do not encode — by
   design, since a missed rule is easier to audit than a hallucinated one.
2. **At-least-once delivery with idempotent processing, instead of paid duplicate detection.**
   Service Bus Basic has no fixed charge but no duplicate detection. The worker claims each
   submission with an ETag-guarded lease and stores the result before completing the message,
   so a redelivered message finds the existing result. *Trade-off:* more application logic, and
   correctness depends on tests: duplicates, lease takeover, retries and dead-lettering are
   **verified locally**, not exercised in Azure.
3. **Scale to zero everywhere.** Web app and worker cost nothing while idle. *Trade-off:* cold
   starts — about 28 s for the web app and 24.5–35.7 s from upload to result in the pipeline,
   almost all of it worker start-up (verified in Azure).
4. **A simulated public demo instead of public uploads.** The *Try processing* page runs ten
   predefined scenarios through the real processor with in-memory stand-ins, behind its own
   flag (`DEMO_MODE_ENABLED`), independent of live processing (`ASYNC_PROCESSING_ENABLED`, off in
   public). Tests fail if demo code constructs an Azure client. *Trade-off:* demo timings are
   milliseconds and say nothing about Azure, so the page shows a measured Azure run beside every
   result, labelled as historical.
5. **Fail closed on cost.** Each AI call reserves an upper-bound cost before calling the model
   and settles with the reported usage; if the limiter is unreachable or prices are missing, the
   app switches to a read-only mode that still shows validation results. *Trade-off:* the budget
   may be reached slightly early, but it cannot be overspent.

## 5. Security and identity model

- **No secrets:** storage shared keys, Service Bus local auth and Azure OpenAI API keys are all
  disabled; every service call uses a managed identity. GitHub deployment uses OIDC.
- **Separate identities, each role scoped to one resource:** the web app (AI inference, its
  tables, the upload container); the worker (read uploads, its table, receive from the queue —
  no AI role); the KEDA scaler (queue metrics only, not available to container code); and Event
  Grid's own identity for delivery and dead-lettering.
- **Finding during deployment:** Event Grid rejected a dead-letter role scoped to a single
  container. A controlled comparison showed that only a storage-account scope works, so
  dead-letters go to a **dedicated** storage account rather than widening access on the main one.
- **Input handling:** Pydantic validation and size limits; invoice-supplied text is always
  rendered escaped (security regression tests); the API is reachable only from inside the
  container.

## 6. Verified results

| What | Result | Label |
|---|---|---|
| End-to-end event-driven processing | 6 test submissions; upload → result 24.5–35.7 s; message queued ≈ 0.8 s after upload; 0 duplicates, retries and dead-letters observed | Verified in Azure |
| Concurrency | 3 simultaneous submissions started 3 executions (the configured maximum); one processed all three, two exited idle | Verified in Azure |
| Web app cold start | 24.6 / 33.1 / 26.4 s (average 28.0 s); scale to zero ≈ 6 min after an idle pause | Verified in Azure |
| Azure OpenAI answers with citations | 5 requests in an IP-restricted test window | Verified in Azure (controlled) |
| Public demo deployment | One web-app update from a reviewed plan; one scenario smoke-tested; Service Bus, Event Grid and Azure OpenAI counters unchanged | Verified in Azure |
| Duplicates, lease takeover, retries, dead-lettering, lock renewal | Deterministic tests with Service Bus semantics | Verified locally |
| Overall test suite | 527 tests (95% line coverage), 24 Terraform plan tests | Verified locally |

## 7. Cost-aware design

- No fixed monthly charges in the event-processing path at this scale; estimated **DKK 0.1–0.2
  per month while idle** (Azure list prices, Sweden Central). Actual billing has not been
  reviewed yet.
- Test runs were estimated at under DKK 0.01 (E1) and under DKK 0.02 (E2); the Azure OpenAI test
  cost about **USD 0.005**.
- The public app uses the mock AI provider, so visitors cause no model spend. A resource-group
  budget alert (DKK 75/month) notifies; it is not a spending cap.

## 8. Limitations and next steps

**Limitations**

- Live AI and live asynchronous processing are off for the public; each was verified only in
  short, controlled tests.
- Failure paths (retries, lease takeover, dead-lettering, lifecycle deletion) and sustained load
  have not been exercised in Azure.
- The demo's daily allowances are in memory: best-effort, per replica, reset on scale-to-zero.
- No authentication; fictional data only.

**Next steps**

- Exercise the failure paths in Azure with controlled fault injection.
- Let each worker execution take a single message, so bursts do not start idle executions.
- Add authentication, then allow protected uploads through the existing pipeline.
- Azure Monitor dashboards and alerts; optionally Azure AI Search behind the retriever interface.
