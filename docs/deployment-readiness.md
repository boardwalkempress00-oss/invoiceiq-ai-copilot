# Deployment readiness review — InvoiceIQ Milestone 1

*Prepared 2026-10-10, before any Azure resource exists. Nothing has been provisioned, deployed or
billed. Prices: Azure Retail Prices API, region Sweden Central, USD, retrieved 2026-10-10 —
re-check before applying; your invoice is in your billing currency and may differ.*

## Verdict

**Ready for an approved `terraform plan`, with three items to settle first** (see the end).
All automated checks pass. The two paths that only real Azure can exercise — managed identity
calls to Azure OpenAI and to Table Storage — have been tested against a fake SDK client and the
Azurite emulator, not against Azure.

## 1. Resources Terraform will create

| # | Resource (Terraform type) | Name pattern | Notes |
|---|---|---|---|
| 1 | `random_string` | — | 5-char suffix for globally unique names (local only, not an Azure resource) |
| 2 | Resource group | `rg-invoiceiq-<sfx>` | |
| 3 | Log Analytics workspace (PerGB2018) | `log-invoiceiq-<sfx>` | 30-day retention, **0.1 GB/day ingestion cap** |
| 4 | User-assigned managed identity | `id-invoiceiq-<sfx>` | The app's only identity |
| 5 | Azure OpenAI account (S0, keys disabled) | `oai-invoiceiq-<sfx>` | Custom subdomain for Entra ID |
| 6 | Azure OpenAI model deployment | `chat` | Pay-per-token only (GlobalStandard default); model/version you choose; 10K TPM default |
| 7 | Role assignment | — | *Cognitive Services OpenAI User* → identity, on the AI account |
| 8 | Storage account (Standard LRS, StorageV2) | `stinvoiceiq<sfx>` | Shared keys disabled, TLS 1.2, no public blobs |
| 9 | Storage table | `aiusage` | Usage-limiter counters |
| 10 | Role assignment | — | *Storage Table Data Contributor* → identity, on that one table |
| 11 | Container Apps environment (consumption-only) | `cae-invoiceiq-<sfx>` | No workload profiles, no VNet, no private endpoints |
| 12 | Container app | `ca-invoiceiq-<sfx>` | 0–1 replicas, 0.5 vCPU / 1 GiB, HTTPS ingress → 8501 |
| 13 | Consumption budget *(optional)* | `budget-invoiceiq-<sfx>` | Only if `budget_alert_email` is set; e-mail alerts at 50% actual / 100% forecast |
| 14–17 | GitHub deploy identity, federated credential, 2 role assignments *(optional)* | `id-invoiceiq-<sfx>-deploy` | Only if `github_repository` is set; *Contributor* on the container app, *Reader* on the RG |

Not created: container registry (image comes from public GHCR), Key Vault (no secrets exist),
VNet, private endpoints, Application Insights, Azure AI Search.

**Who can apply:** the person running Terraform needs rights to create role assignments
(*Owner*, or *Contributor* + *Role Based Access Control Administrator*) on the subscription or
resource group.

## 2. Resources with fixed monthly costs

**None in this configuration** — every resource is billed by usage. Verified points:

- Container Apps: Microsoft's billing documentation states the plan-management charge applies
  only with a **dedicated workload profile**, or features such as **private endpoints** or
  **planned maintenance**. None is configured (a Terraform test blocks workload profiles). If one
  were added, the "Environment Management Hour" meter is **$0.13/hour ≈ $95/month**.
- Azure OpenAI S0 account and Standard/GlobalStandard deployments have no hosting fee.
  Provisioned (PTU) and fine-tuned deployments do — both are prevented (Terraform validation /
  not used).
- Managed identities, role assignments and budgets are free.

**Caveat — subscription-level Microsoft Defender plans.** If Defender for Cloud plans (e.g.
Defender for Storage, for AI services) are enabled on the subscription, they automatically cover
new resources and **can add per-resource monthly fees**. Check *Defender for Cloud → Environment
settings* before applying.

## 3. Consumption-based resources (Sweden Central list prices)

| Service | Meter | Price | Free allowance |
|---|---|---|---|
| Container Apps | vCPU active | $0.000024 / vCPU-second | 180,000 vCPU-s / month **per subscription** |
| | Memory | $0.000003 / GiB-second | 360,000 GiB-s / month per subscription |
| | Requests | $0.40 / million | 2M / month per subscription; health probes not billed |
| | Scaled to zero | $0 | — |
| Log Analytics | Analytics ingestion | $2.99 / GB | First 5 GB / month (per billing account) |
| | Retention beyond included | $0.13 / GB-month | 30 days configured |
| Table Storage (LRS) | Data stored | $0.045 / GB-month | — |
| | Operations | $0.00036 / 10,000 | — |
| Azure OpenAI (example: gpt-4.1-mini, Global) | Input / output tokens | $0.40 / $1.60 per 1M | — |

**Derived:** one running replica (0.5 vCPU + 1 GiB, active rate) costs **$0.054/hour**. The free
grant covers **100 running hours/month**, if no other container app in the subscription uses it.
With `min_replicas = 0`, any running replica is above the minimum, so it is always billed at the
**active** rate (there is no idle discount).

## 4. Potential unexpected charges

| Risk | Why | Bound / mitigation |
|---|---|---|
| **Open browser tabs** keep the replica running | Streamlit holds a WebSocket; the app cannot scale to zero while one is open | 1 replica max → at most $39.42/month (24/7), $34.02 after the free grant. Budget alert recommended |
| Bots and scanners wake the app | Any public URL is probed; each wake runs ≥ 5 min (cooldown) | ~60 h/month assumed → $0 within the grant, $3.24 without |
| Free grants already used | Grants are per subscription (Container Apps) or per billing account (Log Analytics) | Check other workloads in the subscription |
| Defender for Cloud plans | Auto-enrolment of new storage/AI resources | Check before apply (see §2) |
| Plan-management fee | Adding a workload profile, private endpoint or maintenance window | Not configured; Terraform test guards profiles |
| Log ingestion | Verbose logs | 0.1 GB/day cap → ≤ $9.27/month even with no free tier; logs stop for the day when hit |
| Wrong or outdated model prices | The AI budget is computed from configured prices | Terraform refuses zero prices; verify against the pricing page whenever the model changes |
| Expensive model or deployment type | Choosing a larger model | PTU blocked; budget still caps *estimated* spend per day |
| HTTP request flood | $0.40/M after 2M free | 10M requests ≈ $3.20; one replica limits throughput |
| Egress | First 100 GB/month free | Text-only app; negligible |

## 5. Are all public AI usage limits enforced?

**Yes for the hard limits, with two honest caveats.**

| Limit | Enforced where | Status |
|---|---|---|
| Global daily request cap (200) | Azure Table Storage, ETag-protected, before every AI call | ✅ Implemented; tested on Azurite incl. 20-thread / 4-client concurrency (132 conflicts retried, 0 over-allocations) |
| Global daily **estimated-cost** budget ($1.00) | Same reservation; settled with actual usage | ✅ Implemented; ambiguous outcomes charged at the estimate |
| Per-session cap (10) | Same store | ⚠️ **Soft**: a new browser session (page reload) gets a new allowance. The global caps are the real protection |
| Max output tokens (700), input size (12k chars evidence, 500-char question, bounded history) | Application | ✅ |
| Max request duration | 15 s timeout × 3 attempts + backoff (≈ 60 s worst case) | ✅ |
| Fail closed | No AI call if the limiter is unreachable, prices are zero, or the store is not shared-durable | ✅ Tested (incl. production refusing the dev connection string) |
| UTC daily reset | Table partition per UTC date | ✅ |

Caveats: (1) the pre-call estimate uses ~3 characters per token, which is conservative for
English but could under-estimate unusual input; output is hard-capped, and actual usage
replaces the estimate after each call. (2) None of this has run against a real storage account
yet. Azurite does not emulate Entra ID/RBAC, throttling or latency (details in
[design/persistent-usage-limiter.md](design/persistent-usage-limiter.md)).

**Worst-case AI spend** with defaults: largest possible request ≈ 8,100 prompt + 700 output
tokens ≈ $0.0044 → 200/day ≈ **$0.87/day (≈ $26/month)**; the $1.00/day budget is a second,
independent cap. TPM quota is *not* relied on for cost.

## 6. Security risks

| Risk | Severity | Mitigation in place | Residual / recommendation |
|---|---|---|---|
| Public, unauthenticated app | Medium | By design (recruiter demo); fictional data only; no uploads | Add Entra ID auth or Easy Auth if real data is ever used |
| Cost abuse via AI | Medium | Global request + cost caps in durable storage, fail closed | Set a budget alert e-mail |
| Cost abuse via compute (bots, open tabs) | Low–Medium | 1 replica max, scale to zero | No per-IP rate limiting at ingress; consider Azure Front Door/WAF only if abused (adds cost) |
| Prompt injection | Low | Separate instruction/evidence/question messages, tag stripping, citation verification, facts never from the model, no tools or actions | Model can still produce misleading prose; labelled as AI-generated |
| Credential leakage | Low | No keys exist: managed identity, AI keys and storage shared keys disabled, OIDC for CI | Terraform **state** contains the (disabled) AI account keys and resource IDs — keep state private |
| Public endpoints for AI and storage | Low | Entra ID required; TLS 1.2; no anonymous access | Private endpoints would add the plan-management charge and networking cost |
| Supply chain | Low–Medium | Locked dependencies (`uv.lock`), provider lockfile, pinned base/uv/Azurite images, Dependabot | GitHub Actions pinned to major tags, not commit SHAs; no image vulnerability scan yet (e.g. Trivy in CI) |
| Container hardening | Low | Non-root, tini, slim image, no shell in the entrypoint | Read-only root filesystem and dropped capabilities are applied in Compose only, not configured for Container Apps |
| Error/log disclosure | Low | No stack traces or inputs in responses; prompts never logged | — |
| Deploy pipeline | Low | Disabled by default; manual dispatch + typed confirmation + protected environment; identity limited to one container app | Configure required reviewers on the `production` environment |

## 7. Terraform validation results

| Check | Result |
|---|---|
| Terraform CLI | 1.15.8 |
| Providers (locked, linux_amd64 + darwin hashes) | azurerm **4.81.0**, random 3.9.1 |
| `terraform fmt -check -recursive` | ✅ pass |
| `terraform validate` | ✅ "The configuration is valid." |
| `terraform test` (mocked providers, plan only) | ✅ **8 passed, 0 failed** — replicas 0–1, 0.5 vCPU/1 GiB, consumption-only env, log caps, Standard LRS, HTTPS-only ingress on 8501, AI keys and storage keys disabled, least-privilege roles, no secrets/registry credentials, `USAGE_STORE=azure_table`, OIDC subject scoped to the protected environment, rejects `:latest`, zero prices and provisioned throughput |
| Negative check | Changing `min_replicas` to 1 makes the test fail as intended |
| Not yet done | A real `terraform plan` (needs Azure credentials and your approval). It will surface region/model availability, quota and name collisions |

## 8. Docker test results

| Check | Result |
|---|---|
| Build (multi-stage, uv, Python 3.12 slim-trixie) | ✅ |
| Image size | 174 MB compressed (≈ 420 MB virtualenv; ~85% is Streamlit's pandas/pyarrow/numpy) |
| Health check (both services) | ✅ healthy |
| User | uid/gid 10001 (non-root) |
| Bindings inside the container | FastAPI `127.0.0.1:8000`, Streamlit `0.0.0.0:8501`; only 8501 published |
| Credentials in image/env | None (only the base image's public `GPG_KEY` ID) |
| All five pages via headless Chrome | ✅ Dashboard, Invoice Analysis, AI Chat (with answer), Architecture, Observability — no page errors |
| Graceful shutdown (SIGTERM) | ✅ ≈ 1 s, exit code 0, frontend stopped before backend |
| Fail-fast | ✅ A failing frontend at startup stops the backend and exits non-zero |
| Production image + Azurite | ✅ `APP_ENV=production` refuses the dev connection string (fail closed); development mode counts usage in Azurite |

## 9. Tests and coverage

- **282 Python tests passed** (166 unit, 29 API, 47 frontend, 40 integration incl. Azurite),
  **95% line coverage**; plus **8 Terraform tests**. `ruff format` / `ruff check` clean;
  `actionlint` clean on both workflows.
- Streamlit page scripts run under `AppTest` but are not visible to coverage, so they are not
  included in the 95%.
- Test changes in Phase 5: two fixtures/tests were updated because requirements changed — the
  `BrokenStore` fixture now also fails the new `read`/`commit` operations, and the production
  live-AI test now configures prices (a new rule). All other existing tests are unchanged.

## 10. Estimated monthly cost

Assumptions: Sweden Central list prices above; gpt-4.1-mini Global as the example model;
a typical question ≈ 1,800 input + 350 output tokens (≈ $0.0013); a visitor session keeps the
app running ≈ 15 minutes including the scale-down cooldown; no other workloads use the free
grants unless stated.

| Scenario | Container Apps | Azure OpenAI | Log Analytics | Table Storage | **Total** |
|---|---|---|---|---|---|
| **Idle** (no visitors, no bots) | $0 (scaled to zero) | $0 | ≈ $0 | < $0.01 | **≈ $0.00** |
| Idle with typical bot traffic (~60 running h) | $0 (within grant) – $3.24 | $0 | ≈ $0 | < $0.01 | **$0 – $3.25** |
| **100 visitors**, 5 questions each (~25 running h, 500 questions) | $0 (within grant) – $1.35 | ≈ $0.64 | ≈ $0 (within 5 GB) | < $0.01 | **≈ $0.65 – $2.00** |
| Ceiling under sustained abuse (tab open 24/7 + AI caps hit daily + log cap hit daily, no free tiers) | $39.42 | ≤ $26 | ≤ $9.27 | < $0.10 | **≈ $75 / month max** |

Realistic expectation for a portfolio demo: **under $5/month**. The ceiling is what the
configured caps allow in the worst case, not a forecast.

## Before approving `terraform apply`

1. **Choose the model** and confirm availability, version and quota in your region
   (`az cognitiveservices model list` / `usage list`), then set its real token prices.
2. **Check Defender for Cloud** plans on the subscription.
3. **Set `budget_alert_email`** (free) and decide whether to start with `ai_provider = "mock"`
   (zero AI cost) for the first public week.

Then: publish the image to GHCR (public), run `terraform plan`, review it together, and apply only
with explicit approval.

**Sources:** Azure Retail Prices API (`prices.azure.com`, Sweden Central, USD, 2026-10-10);
[Billing in Azure Container Apps](https://learn.microsoft.com/en-us/azure/container-apps/billing)
(Microsoft Learn, updated 2026-03-25).
