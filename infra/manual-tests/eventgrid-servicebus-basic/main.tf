# Event Grid → Service Bus *Basic* compatibility test (manual; see README.md).
# Isolated: own resource group, own storage account, own local state. Production untouched.
# Proves: BlobCreated → Event Grid (managed-identity delivery) → Basic queue → message received.
# Requires Microsoft.EventGrid to be registered first. Destroy afterwards (terraform destroy).
#
# Dead-letter scope test (see README): dead-letters go to a separate storage account, and
# `deadletter_role_scope` decides whether the topic identity's Storage Blob Data Contributor role
# is scoped to the dead-letter container or to the whole dead-letter account. Apply in two stages
# (create_subscription = false, wait for RBAC to propagate, then true) so a transient
# authorisation error is not mistaken for a scope limitation.

terraform {
  required_version = ">= 1.9"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.81" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "azurerm" {
  features {
    resource_group {
      prevent_deletion_if_contains_resources = false # test RG: destroy everything in it
    }
  }
  subscription_id                 = var.subscription_id
  storage_use_azuread             = true
  resource_provider_registrations = "none"
}

variable "subscription_id" {
  type    = string
  default = null
}

variable "deadletter_role_scope" {
  description = "Scope of the topic identity's dead-letter role: \"container\" or \"account\"."
  type        = string
  default     = "account"
  validation {
    condition     = contains(["container", "account"], var.deadletter_role_scope)
    error_message = "deadletter_role_scope must be container or account."
  }
}

variable "create_subscription" {
  description = "Create the event subscription (stage 2, after role propagation)."
  type        = bool
  default     = true
}

variable "location" {
  type    = string
  default = "swedencentral"
}

data "azurerm_client_config" "me" {} # the person running the test (for its two data roles)

resource "random_string" "s" {
  length  = 5
  upper   = false
  special = false
}

locals {
  base = "iqegtest${random_string.s.result}"
  tags = { project = "invoiceiq", purpose = "eventgrid-servicebus-basic-compat-test", temporary = "true" }
}

resource "azurerm_resource_group" "test" {
  name     = "rg-invoiceiq-egtest-${random_string.s.result}"
  location = var.location
  tags     = local.tags
}

resource "azurerm_storage_account" "test" {
  name                            = "st${local.base}"
  resource_group_name             = azurerm_resource_group.test.name
  location                        = azurerm_resource_group.test.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  account_kind                    = "StorageV2"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false # same posture as production
  default_to_oauth_authentication = true
  allow_nested_items_to_be_public = false
  tags                            = local.tags
}

resource "azurerm_storage_container" "uploads" {
  name                  = "invoice-uploads"
  storage_account_id    = azurerm_storage_account.test.id
  container_access_type = "private"
}

# Dedicated dead-letter account: holds nothing but Event Grid dead-letters.
resource "azurerm_storage_account" "deadletter" {
  name                            = "stdl${local.base}"
  resource_group_name             = azurerm_resource_group.test.name
  location                        = azurerm_resource_group.test.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  account_kind                    = "StorageV2"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  allow_nested_items_to_be_public = false
  tags                            = local.tags
}

resource "azurerm_storage_container" "deadletter" {
  name                  = "eventgrid-deadletter"
  storage_account_id    = azurerm_storage_account.deadletter.id
  container_access_type = "private"
}

resource "azurerm_role_assignment" "topic_deadletter_writer" {
  scope                = var.deadletter_role_scope == "account" ? azurerm_storage_account.deadletter.id : azurerm_storage_container.deadletter.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_eventgrid_system_topic.test.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_servicebus_namespace" "test" {
  name                = "sb-${local.base}"
  resource_group_name = azurerm_resource_group.test.name
  location            = azurerm_resource_group.test.location
  sku                 = "Basic" # the thing under test
  local_auth_enabled  = false
  minimum_tls_version = "1.2"
  tags                = local.tags
}

resource "azurerm_servicebus_queue" "test" {
  name                                 = "invoice-events"
  namespace_id                         = azurerm_servicebus_namespace.test.id
  max_delivery_count                   = 5
  lock_duration                        = "PT1M" # Azure normalises PT60S to PT1M
  default_message_ttl                  = "PT1H"
  dead_lettering_on_message_expiration = true
}

resource "azurerm_eventgrid_system_topic" "test" {
  name                = "egst-${local.base}"
  resource_group_name = azurerm_resource_group.test.name
  location            = azurerm_storage_account.test.location
  source_resource_id  = azurerm_storage_account.test.id
  topic_type          = "Microsoft.Storage.StorageAccounts"
  tags                = local.tags
  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_role_assignment" "topic_sender" {
  scope                = azurerm_servicebus_queue.test.id
  role_definition_name = "Azure Service Bus Data Sender"
  principal_id         = azurerm_eventgrid_system_topic.test.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

# Same subscription shape as Phase C (filters, identity delivery, CorrelationId property).
resource "azurerm_eventgrid_system_topic_event_subscription" "test" {
  count                         = var.create_subscription ? 1 : 0
  name                          = "invoice-blob-created"
  system_topic                  = azurerm_eventgrid_system_topic.test.name
  resource_group_name           = azurerm_resource_group.test.name
  service_bus_queue_endpoint_id = azurerm_servicebus_queue.test.id
  event_delivery_schema         = "EventGridSchema"
  included_event_types          = ["Microsoft.Storage.BlobCreated"]
  subject_filter {
    subject_begins_with = "/blobServices/default/containers/invoice-uploads/blobs/submissions/"
    subject_ends_with   = ".json"
    case_sensitive      = true
  }
  advanced_filter {
    string_in {
      key    = "data.api"
      values = ["PutBlob"]
    }
  }
  delivery_identity {
    type = "SystemAssigned"
  }
  delivery_property {
    header_name  = "CorrelationId"
    type         = "Dynamic"
    source_field = "data.clientRequestId"
  }
  retry_policy {
    max_delivery_attempts = 30
    event_time_to_live    = 60
  }
  dead_letter_identity {
    type = "SystemAssigned"
  }
  storage_blob_dead_letter_destination {
    storage_account_id          = azurerm_storage_account.deadletter.id
    storage_blob_container_name = azurerm_storage_container.deadletter.name
  }
  depends_on = [azurerm_role_assignment.topic_sender, azurerm_role_assignment.topic_deadletter_writer]
}

# The tester's own access, scoped to the test container and queue (deleted with the RG).
resource "azurerm_role_assignment" "me_blob_writer" {
  scope                = azurerm_storage_container.uploads.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_client_config.me.object_id
}

resource "azurerm_role_assignment" "me_queue_receiver" {
  scope                = azurerm_servicebus_queue.test.id
  role_definition_name = "Azure Service Bus Data Receiver"
  principal_id         = data.azurerm_client_config.me.object_id
}

output "resource_group" { value = azurerm_resource_group.test.name }
output "blob_account_url" { value = azurerm_storage_account.test.primary_blob_endpoint }
output "servicebus_fqdn" { value = "${azurerm_servicebus_namespace.test.name}.servicebus.windows.net" }
output "system_topic" { value = azurerm_eventgrid_system_topic.test.name }
output "deadletter_role_scope" { value = var.deadletter_role_scope }
