# Asynchronous invoice processing (Milestone 2 MVP).
#
#   FastAPI ──upload──▶ Blob container "invoice-uploads" (private, existing storage account)
#        └──status────▶ Table "invoicejobs"
#   Blob ──BlobCreated──▶ Event Grid system topic ──(managed identity)──▶ Service Bus Basic queue
#   Queue ──KEDA scale rule (scaler identity)──▶ Container Apps Job "worker" (worker identity)
#
# Everything here is created only when `event_processing_enabled = true` (default false), so the
# existing deployment's plan is unchanged until that is switched on deliberately. Even then the
# app keeps ASYNC_PROCESSING_ENABLED=false until `async_processing_enabled = true`.
#
# Before the first apply with event_processing_enabled = true, the Microsoft.EventGrid and
# Microsoft.ServiceBus resource providers must be registered (resource_provider_registrations =
# "none" means Terraform never registers them itself).
#
# Not verified until deployed (Phase D): Event Grid delivery to a *Basic* queue with managed
# identity, and the KEDA azure-servicebus scaler with a managed identity (KEDA documents that
# reading queue metrics needs "Manage" rights — hence Data Owner on the queue for the scaler).

locals {
  ep = var.event_processing_enabled

  upload_container          = "invoice-uploads"
  deadletter_container      = "eventgrid-deadletter"
  submissions_table         = "invoicejobs"
  queue_name                = "invoice-events"
  queue_max_delivery        = 5
  queue_lock_seconds        = 60  # message lock = worker lease
  job_replica_timeout       = 300 # seconds; the worker's run budget must stay below it
  worker_run_budget         = 240
  submission_max_bytes      = 65536
  blob_subject_prefix       = "/blobServices/default/containers/${local.upload_container}/blobs/submissions/"
  servicebus_namespace      = "sb-${local.base}"
  servicebus_namespace_fqdn = "${local.servicebus_namespace}.servicebus.windows.net"

  # The worker's image defaults to the web app's, so existing configurations plan unchanged.
  worker_image = coalesce(var.worker_container_image, var.container_image)

  # Added to the web app only when the infrastructure exists. ASYNC_PROCESSING_ENABLED stays
  # "false" until var.async_processing_enabled is set.
  async_app_env = local.ep ? {
    ASYNC_PROCESSING_ENABLED       = tostring(var.async_processing_enabled)
    SUBMISSION_BACKEND             = "azure"
    AZURE_BLOB_ENDPOINT            = azurerm_storage_account.usage.primary_blob_endpoint
    UPLOAD_CONTAINER               = local.upload_container
    SUBMISSIONS_TABLE              = local.submissions_table
    SUBMISSION_MAX_BYTES           = tostring(local.submission_max_bytes)
    SUBMISSION_SESSION_DAILY_LIMIT = tostring(var.submission_session_daily_limit)
    SUBMISSION_GLOBAL_DAILY_LIMIT  = tostring(var.submission_global_daily_limit)
    SUBMISSION_RETENTION_DAYS      = tostring(var.submission_retention_days)
  } : {}

  # The worker gets no AI settings at all (and its identity has no Azure OpenAI role).
  worker_env = local.ep ? {
    APP_ENV                     = "production"
    LOG_LEVEL                   = "INFO"
    AZURE_CLIENT_ID             = azurerm_user_assigned_identity.worker[0].client_id
    AZURE_BLOB_ENDPOINT         = azurerm_storage_account.usage.primary_blob_endpoint
    AZURE_TABLE_ENDPOINT        = azurerm_storage_account.usage.primary_table_endpoint
    UPLOAD_CONTAINER            = local.upload_container
    SUBMISSIONS_TABLE           = local.submissions_table
    SUBMISSION_MAX_BYTES        = tostring(local.submission_max_bytes)
    SERVICEBUS_NAMESPACE        = local.servicebus_namespace_fqdn
    SERVICEBUS_QUEUE            = local.queue_name
    WORKER_MAX_DELIVERY_COUNT   = tostring(local.queue_max_delivery)
    WORKER_LEASE_SECONDS        = tostring(local.queue_lock_seconds)
    WORKER_RUN_BUDGET_SECONDS   = tostring(local.worker_run_budget)
    WORKER_MAX_MESSAGES_PER_RUN = "20"
  } : {}
}

# --- Storage: private container, status table, retention ------------------------------------

# `storage_account_id` makes the provider use Azure Resource Manager only (no shared-key
# data-plane call), which matters because shared-key access is disabled on this account.
resource "azurerm_storage_container" "invoice_uploads" {
  count                 = local.ep ? 1 : 0
  name                  = local.upload_container
  storage_account_id    = azurerm_storage_account.usage.id
  container_access_type = "private"
}

# LEGACY (unused): the first dead-letter container, in the main account. Event Grid refused it as
# a managed-identity dead-letter destination with a container-scoped role (it requires the role on
# the storage account — verified in infra/manual-tests). Kept so this change destroys nothing;
# remove it and its role assignment in a separately approved clean-up.
resource "azurerm_storage_container" "eventgrid_deadletter" {
  count                 = local.ep ? 1 : 0
  name                  = local.deadletter_container
  storage_account_id    = azurerm_storage_account.usage.id
  container_access_type = "private"
}

# Dedicated dead-letter account: Event Grid needs Storage Blob Data Contributor on the *account*
# that receives dead-letters, so dead-letters get their own account. Event Grid's identity then has
# no access to the main account (uploads, usage table).
resource "azurerm_storage_account" "deadletter" {
  count                           = local.ep ? 1 : 0
  name                            = "stdl${var.name_prefix}${local.suffix}"
  location                        = azurerm_resource_group.main.location
  resource_group_name             = azurerm_resource_group.main.name
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  account_kind                    = "StorageV2"
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  shared_access_key_enabled       = false # Entra ID only
  default_to_oauth_authentication = true
  allow_nested_items_to_be_public = false
  local_user_enabled              = false
  tags                            = var.tags
}

resource "azurerm_storage_container" "deadletter" {
  count                 = local.ep ? 1 : 0
  name                  = local.deadletter_container
  storage_account_id    = azurerm_storage_account.deadletter[0].id
  container_access_type = "private"
}

resource "azurerm_storage_management_policy" "deadletter_retention" {
  count              = local.ep ? 1 : 0
  storage_account_id = azurerm_storage_account.deadletter[0].id

  rule {
    name    = "delete-eventgrid-deadletters"
    enabled = true
    filters {
      blob_types   = ["blockBlob"]
      prefix_match = ["${local.deadletter_container}/"]
    }
    actions {
      base_blob {
        delete_after_days_since_creation_greater_than = var.submission_retention_days
      }
    }
  }

  depends_on = [azurerm_storage_container.deadletter]
}

# Same pattern as the usage table: management plane only, no stored access policies.
resource "azapi_resource" "jobs_table" {
  count     = local.ep ? 1 : 0
  type      = "Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01"
  name      = local.submissions_table
  parent_id = "${azurerm_storage_account.usage.id}/tableServices/default"
  body = {
    properties = {
      signedIdentifiers = []
    }
  }
}

# Guaranteed deletion of uploads and Event Grid dead-letters (the lifecycle engine runs about once
# a day, so a blob can outlive its retention by up to ~1 day). Status rows are cleaned up
# opportunistically by the worker and hidden by the API after expiry.
resource "azurerm_storage_management_policy" "retention" {
  count              = local.ep ? 1 : 0
  storage_account_id = azurerm_storage_account.usage.id

  rule {
    name    = "delete-invoice-uploads"
    enabled = true
    filters {
      blob_types   = ["blockBlob"]
      prefix_match = ["${local.upload_container}/submissions/"]
    }
    actions {
      base_blob {
        delete_after_days_since_creation_greater_than = var.submission_retention_days
      }
    }
  }

  rule {
    name    = "delete-eventgrid-deadletters"
    enabled = true
    filters {
      blob_types   = ["blockBlob"]
      prefix_match = ["${local.deadletter_container}/"]
    }
    actions {
      base_blob {
        delete_after_days_since_creation_greater_than = var.submission_retention_days
      }
    }
  }

  depends_on = [azurerm_storage_container.invoice_uploads, azurerm_storage_container.eventgrid_deadletter]
}

# --- Service Bus (Basic: queues + dead-letter queue, no fixed monthly charge) -----------------

resource "azurerm_servicebus_namespace" "main" {
  count                         = local.ep ? 1 : 0
  name                          = local.servicebus_namespace
  location                      = azurerm_resource_group.main.location
  resource_group_name           = azurerm_resource_group.main.name
  sku                           = "Basic"
  local_auth_enabled            = false # Entra ID only: no SAS keys or connection strings
  minimum_tls_version           = "1.2"
  public_network_access_enabled = true # no virtual network in this design; access is RBAC-only
  tags                          = var.tags
}

resource "azurerm_servicebus_queue" "invoice_events" {
  count                                = local.ep ? 1 : 0
  name                                 = local.queue_name
  namespace_id                         = azurerm_servicebus_namespace.main[0].id
  max_delivery_count                   = local.queue_max_delivery
  lock_duration                        = "PT1M" # = queue_lock_seconds; Azure normalises "PT60S" to "PT1M"
  default_message_ttl                  = "P1D"
  dead_lettering_on_message_expiration = true
  max_size_in_megabytes                = 1024
  # Standard-tier features stay off (not available on Basic): duplicates are handled by the
  # worker's idempotent processing instead.
  requires_duplicate_detection = false
  requires_session             = false
  partitioning_enabled         = false
}

# --- Event Grid: BlobCreated → Service Bus, delivered with the topic's managed identity --------

resource "azurerm_eventgrid_system_topic" "storage" {
  count               = local.ep ? 1 : 0
  name                = "egst-${local.base}"
  location            = azurerm_storage_account.usage.location
  resource_group_name = azurerm_resource_group.main.name
  source_resource_id  = azurerm_storage_account.usage.id
  topic_type          = "Microsoft.Storage.StorageAccounts"
  tags                = var.tags

  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_eventgrid_system_topic_event_subscription" "blob_created" {
  count                         = local.ep ? 1 : 0
  name                          = "invoice-blob-created"
  system_topic                  = azurerm_eventgrid_system_topic.storage[0].name
  resource_group_name           = azurerm_resource_group.main.name
  service_bus_queue_endpoint_id = azurerm_servicebus_queue.invoice_events[0].id
  event_delivery_schema         = "EventGridSchema"
  included_event_types          = ["Microsoft.Storage.BlobCreated"]

  subject_filter {
    subject_begins_with = local.blob_subject_prefix
    subject_ends_with   = ".json"
    case_sensitive      = true
  }

  # The API uploads ≤ 64 KB in one Put Blob call; ignore any other way of creating blobs.
  advanced_filter {
    string_in {
      key    = "data.api"
      values = ["PutBlob"]
    }
  }

  delivery_identity {
    type = "SystemAssigned"
  }

  # Carry the API's correlation ID (sent as x-ms-client-request-id) into the message.
  delivery_property {
    header_name  = "CorrelationId"
    type         = "Dynamic"
    source_field = "data.clientRequestId"
  }

  retry_policy {
    max_delivery_attempts = 30
    event_time_to_live    = 1440 # minutes
  }

  dead_letter_identity {
    type = "SystemAssigned"
  }

  storage_blob_dead_letter_destination {
    storage_account_id          = azurerm_storage_account.deadletter[0].id
    storage_blob_container_name = azurerm_storage_container.deadletter[0].name
  }

  # Undeliverable events are retried for 24 h, so RBAC propagation delay right after creation
  # cannot lose events; the roles are still created first.
  depends_on = [
    azurerm_role_assignment.eventgrid_queue_sender,
    azurerm_role_assignment.eventgrid_deadletter_account,
  ]
}

# --- Identities (separate per responsibility) --------------------------------------------------

resource "azurerm_user_assigned_identity" "worker" {
  count               = local.ep ? 1 : 0
  name                = "id-${local.base}-worker"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = var.tags
}

# Used only by the platform's KEDA scale rule; not available to container code (lifecycle None).
resource "azurerm_user_assigned_identity" "scaler" {
  count               = local.ep ? 1 : 0
  name                = "id-${local.base}-scaler"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = var.tags
}

# --- Role assignments (each scoped to a single container, table or queue) ----------------------

resource "azurerm_role_assignment" "app_uploads_writer" {
  count                = local.ep ? 1 : 0
  scope                = azurerm_storage_container.invoice_uploads[0].id
  role_definition_name = "Storage Blob Data Contributor" # no built-in write-only role
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "app_jobs_table" {
  count                = local.ep ? 1 : 0
  scope                = azapi_resource.jobs_table[0].id
  role_definition_name = "Storage Table Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "worker_uploads_reader" {
  count                = local.ep ? 1 : 0
  scope                = azurerm_storage_container.invoice_uploads[0].id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_user_assigned_identity.worker[0].principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "worker_jobs_table" {
  count                = local.ep ? 1 : 0
  scope                = azapi_resource.jobs_table[0].id
  role_definition_name = "Storage Table Data Contributor"
  principal_id         = azurerm_user_assigned_identity.worker[0].principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "worker_queue_receiver" {
  count                = local.ep ? 1 : 0
  scope                = azurerm_servicebus_queue.invoice_events[0].id
  role_definition_name = "Azure Service Bus Data Receiver"
  principal_id         = azurerm_user_assigned_identity.worker[0].principal_id
  principal_type       = "ServicePrincipal"
}

# KEDA reads queue metrics through management operations ("Manage" rights).
resource "azurerm_role_assignment" "scaler_queue_owner" {
  count                = local.ep ? 1 : 0
  scope                = azurerm_servicebus_queue.invoice_events[0].id
  role_definition_name = "Azure Service Bus Data Owner"
  principal_id         = azurerm_user_assigned_identity.scaler[0].principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "eventgrid_queue_sender" {
  count                = local.ep ? 1 : 0
  scope                = azurerm_servicebus_queue.invoice_events[0].id
  role_definition_name = "Azure Service Bus Data Sender"
  principal_id         = azurerm_eventgrid_system_topic.storage[0].identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

# LEGACY (unused, see the legacy container above).
resource "azurerm_role_assignment" "eventgrid_deadletter_writer" {
  count                = local.ep ? 1 : 0
  scope                = azurerm_storage_container.eventgrid_deadletter[0].id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_eventgrid_system_topic.storage[0].identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

# Account scope is what Event Grid validates for managed-identity dead-lettering; the account
# holds nothing but dead-letters.
resource "azurerm_role_assignment" "eventgrid_deadletter_account" {
  count                = local.ep ? 1 : 0
  scope                = azurerm_storage_account.deadletter[0].id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_eventgrid_system_topic.storage[0].identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

# --- Worker: event-triggered Container Apps Job ------------------------------------------------

# AzAPI rather than azurerm_container_app_job: the provider has no `identitySettings`, which is
# what keeps the scaler identity away from container code (lifecycle "None"). One resource owns
# the whole definition, so no later azurerm update can silently reset it.
resource "azapi_resource" "worker_job" {
  count     = local.ep ? 1 : 0
  type      = "Microsoft.App/jobs@2025-01-01"
  name      = "caj-${local.base}-worker"
  parent_id = azurerm_resource_group.main.id
  location  = azurerm_resource_group.main.location
  tags      = var.tags

  identity {
    type = "UserAssigned"
    identity_ids = [
      azurerm_user_assigned_identity.worker[0].id,
      azurerm_user_assigned_identity.scaler[0].id,
    ]
  }

  body = {
    properties = {
      environmentId       = azurerm_container_app_environment.main.id
      workloadProfileName = "Consumption"
      configuration = {
        triggerType       = "Event"
        replicaTimeout    = local.job_replica_timeout
        replicaRetryLimit = 0 # Service Bus redelivery is the retry mechanism
        # Azure returns these IDs with a lower-case "resourcegroups" segment; sending the same
        # form avoids a permanent (case-only) plan difference.
        identitySettings = [
          { identity = replace(azurerm_user_assigned_identity.scaler[0].id, "resourceGroups/", "resourcegroups/"), lifecycle = "None" },
          { identity = replace(azurerm_user_assigned_identity.worker[0].id, "resourceGroups/", "resourcegroups/"), lifecycle = "Main" },
        ]
        eventTriggerConfig = {
          parallelism            = 1
          replicaCompletionCount = 1
          scale = {
            minExecutions   = 0 # nothing runs (or is billed) while the queue is empty
            maxExecutions   = var.worker_max_executions
            pollingInterval = var.worker_polling_interval_seconds
            rules = [
              {
                name = "servicebus-queue"
                type = "azure-servicebus"
                metadata = {
                  namespace    = local.servicebus_namespace
                  queueName    = local.queue_name
                  messageCount = "1"
                }
                identity = azurerm_user_assigned_identity.scaler[0].id
              },
            ]
          }
        }
      }
      template = {
        containers = [
          {
            name    = "worker"
            image   = local.worker_image
            command = ["python", "-m", "invoiceiq.worker"]
            resources = {
              cpu    = 0.25
              memory = "0.5Gi"
            }
            env = [for name, value in local.worker_env : { name = name, value = value }]
          },
        ]
      }
    }
  }

  depends_on = [
    azurerm_role_assignment.worker_uploads_reader,
    azurerm_role_assignment.worker_jobs_table,
    azurerm_role_assignment.worker_queue_receiver,
    azurerm_role_assignment.scaler_queue_owner,
  ]
}
