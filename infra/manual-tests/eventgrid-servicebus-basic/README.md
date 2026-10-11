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

## Creates (11 Terraform resources, all in `rg-invoiceiq-egtest-<random>`)

Resource group · storage account (shared keys off, private container `invoice-uploads`) ·
Service Bus **Basic** namespace (local auth off) + queue · Event Grid system topic (system
identity) + subscription · role assignments: topic → *Service Bus Data Sender* (queue); you →
*Storage Blob Data Contributor* (container) and *Service Bus Data Receiver* (queue) · random suffix.

## Prerequisites

* `az login` to the target subscription, with rights to create role assignments
  (Owner or User Access Administrator).
* Resource providers registered: `Microsoft.EventGrid`, `Microsoft.ServiceBus`,
  `Microsoft.Storage` (`az provider register -n Microsoft.EventGrid --wait`).
* Terraform ≥ 1.9 and `uv` (the probe uses the project's Azure SDKs).

## Cost

Well under DKK 0.01 per run: no fixed charges (Basic has no base fee), a handful of Service Bus
operations, two blob writes, Event Grid operations within the monthly free allowance. Destroy
the resources right after the run.

## Run

```bash
cd infra/manual-tests/eventgrid-servicebus-basic
terraform init
terraform plan -var subscription_id=$(az account show --query id -o tsv) -out=compat.tfplan
#   expect: 11 to add, 0 to change, 0 to destroy
terraform apply compat.tfplan && rm compat.tfplan
sleep 120   # role assignments propagate
uv run python probe.py "$(terraform output -raw blob_account_url)" "$(terraform output -raw servicebus_fqdn)"
```

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
| Provider/registration errors at plan or apply | **Prerequisite** | Register the missing resource provider. |
| `extra messages` > 0 | **Filter or duplicate delivery** | Inspect the extra events' subjects; Event Grid is at-least-once, so an occasional duplicate is legal — the worker is idempotent. |
