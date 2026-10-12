# Manual test: Event Grid → Service Bus Basic

Proves, in an isolated throwaway resource group, that a Blob Storage `BlobCreated` event is
delivered by Event Grid — **with managed identity** — to a **Service Bus Basic** queue, using
the same subscription shape as `infra/terraform/event_processing.tf` (subject filter,
`data.api = PutBlob`, `CorrelationId` from `data.clientRequestId`).

It never touches the InvoiceIQ deployment: separate resource group, storage account and
Terraform state (local to this directory, git-ignored).

**Last run: 2026-10-11 — PASS.** Delivered in ~4 s; filter, correlation ID and redelivery verified.
Raw SDK `delivery_count` was 0 on the first delivery and 1 after an abandon (the worker adds 1).
The Service Bus `message_id` is an internal ID, not the Event Grid event ID.

**Dead-letter scope test — 2026-10-11:** with managed-identity dead-lettering, Event Grid
refuses to create the subscription unless the topic identity's *Storage Blob Data Contributor*
role is on the **storage account** that receives dead-letters:

| Run | Role scope (age at creation) | Subscription | Delivery |
|---|---|---|---|
| Test 1 | dead-letter **container** (21 min) | **Failed** — `Managed Identity Authorization Error`: "Managed identity does not have authorization to deliver to the deadletter endpoint." | — |
| Test 2 | dead-letter **account** (10 min) | **Succeeded** | Delivered in ~2.5 s; filter and correlation ID verified |

Everything else was identical. This matches Microsoft's documentation ("Storage Blob Data
Contributor role on the storage account used for dead-lettering") and is why production puts
dead-letters in a dedicated storage account.

## Creates (15 Terraform resources, all in `rg-invoiceiq-egtest-<random>`)

Resource group · storage account (shared keys off, private container `invoice-uploads`) ·
dedicated dead-letter storage account + private container `eventgrid-deadletter` · Service Bus
**Basic** namespace (local auth off) + queue · Event Grid system topic (system identity) +
subscription (managed-identity delivery and dead-lettering) · role assignments: topic → *Service
Bus Data Sender* (queue) and *Storage Blob Data Contributor* (dead-letter account or container,
see `deadletter_role_scope`); you → *Storage Blob Data Contributor* (`invoice-uploads`) and
*Service Bus Data Receiver* (queue) · random suffix.

## Prerequisites

* `az login` to the target subscription, with rights to create role assignments
  (Owner or User Access Administrator).
* Resource providers registered: `Microsoft.EventGrid`, `Microsoft.ServiceBus`,
  `Microsoft.Storage` (`az provider register -n Microsoft.EventGrid --wait`).
* Terraform ≥ 1.9 and `uv` (the probe uses the project's Azure SDKs).

## Cost

Well under DKK 0.01 per run: no fixed charges (Basic has no base fee; storage accounts have none),
a handful of Service Bus operations, two blob writes, Event Grid operations within the monthly free
allowance. Destroy
the resources right after the run.

## Run

Two stages, so role propagation can never be mistaken for a scope limitation:

```bash
cd infra/manual-tests/eventgrid-servicebus-basic
terraform init
SUB=$(az account show --query id -o tsv)
SCOPE=account   # or: container (expected to fail at stage 2)

# Stage 1: everything except the event subscription (14 to add).
terraform apply -var subscription_id=$SUB -var deadletter_role_scope=$SCOPE -var create_subscription=false
# Wait at least 10 minutes for the role assignments to propagate.

# Stage 2: the event subscription (1 to add).
terraform apply -var subscription_id=$SUB -var deadletter_role_scope=$SCOPE -var create_subscription=true
uv run python probe.py "$(terraform output -raw blob_account_url)" "$(terraform output -raw servicebus_fqdn)"
```

Switching `SCOPE` on an existing deployment replaces only the dead-letter role assignment;
wait 10 minutes again before stage 2.

The probe uploads `submissions/<id>.json` (must be delivered) and `other/<id>.json` (must be
filtered out), receives the message, abandons it once, receives it again, completes it, and checks
that nothing else arrived.

**Success looks like:** `received after` a few seconds · `eventType` BlobCreated · `data.api`
PutBlob · `correlation … OK` · `extra messages (expected 0): 0` · `PROBE COMPLETE`. Event Grid
metrics on the system topic: PublishSuccessCount 2, MatchedEventCount 1, DeliverySuccessCount 1,
no failures or dead-letters.

## Clean up

```bash
terraform destroy -var subscription_id=$(az account show --query id -o tsv)
az group list --query "[?contains(name,'egtest')].name" -o tsv   # expect nothing
```

If destroy fails: `az group delete -n rg-invoiceiq-egtest-<suffix> --yes`.

## Interpreting failures

| Symptom | Category | Meaning / next step |
|---|---|---|
| `terraform apply` fails creating the **event subscription** with an error naming the SKU/tier | **Tier limitation** | Basic is not accepted as a destination. Destroy; decide between Standard (+~DKK 64.5/month), an Azure Storage queue destination, or direct API → Service Bus. Do not switch silently. |
| Subscription created, probe gets no message; `DeliveryAttemptFailCount` > 0 or dead-letters | **RBAC** (most often propagation) | Wait a few minutes; Event Grid retries for the configured TTL. Persisting: check the topic's *Data Sender* role on the queue. |
| Probe fails on upload or receive with 403 / `AuthorizationFailed` | **RBAC for the tester** | Your data roles have not propagated yet; wait and re-run the probe (it creates new test blobs — note them). |
| `MatchedEventCount` 0 while `PublishSuccessCount` > 0 | **Configuration** | Subject/advanced filter does not match the uploaded blob path or API. |
| Subscription creation fails with "not have authorization to deliver to the deadletter endpoint" although the role is older than ~10 minutes | **Dead-letter role scope** | The dead-letter role must be on the storage *account*, not a container (see the results table). |
| `azurerm_servicebus_queue` update stays "Still modifying…" for many minutes | **Provider wait** | Azure applies queue updates at once; the provider's readiness wait can hang on Basic queues. Keep the queue configuration identical to what Azure stores (e.g. `PT1M`, not `PT60S`) so no update is planned. |
| Provider/registration errors at plan or apply | **Prerequisite** | Register the missing resource provider. |
| `extra messages` > 0 | **Filter or duplicate delivery** | Inspect the extra events' subjects; Event Grid is at-least-once, so an occasional duplicate is legal — the worker is idempotent. |
