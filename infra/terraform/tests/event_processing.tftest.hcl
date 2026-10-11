# Asynchronous processing infrastructure — mocked providers: no credentials, nothing created.
#   terraform test

mock_provider "azurerm" {}
mock_provider "random" {}
mock_provider "azapi" {}

# Realistic, distinct IDs: azurerm validates ID formats even with mocked providers, and the
# role-scope assertions below need every container, table, queue and identity to differ.
override_resource {
  target = azurerm_resource_group.main
  values = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-test"
  }
}

override_resource {
  target = azurerm_log_analytics_workspace.main
  values = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-test/providers/Microsoft.OperationalInsights/workspaces/log-test"
  }
}

override_resource {
  target = azurerm_container_app_environment.main
  values = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-test/providers/Microsoft.App/managedEnvironments/cae-test"
  }
}

override_resource {
  target = azurerm_storage_account.usage
  values = {
    id                     = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-test/providers/Microsoft.Storage/storageAccounts/sttest"
    primary_blob_endpoint  = "https://sttest.blob.core.windows.net/"
    primary_table_endpoint = "https://sttest.table.core.windows.net/"
  }
}

override_resource {
  target = azurerm_cognitive_account.openai
  values = {
    id       = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-test/providers/Microsoft.CognitiveServices/accounts/oai-test"
    endpoint = "https://oai-test.openai.azure.com/"
  }
}

override_resource {
  target = azurerm_cognitive_deployment.chat
  values = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-test/providers/Microsoft.CognitiveServices/accounts/oai-test/deployments/chat"
  }
}

override_resource {
  target = azurerm_user_assigned_identity.app
  values = {
    id           = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-test/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-app"
    principal_id = "11111111-1111-1111-1111-111111111111"
    client_id    = "11111111-aaaa-1111-1111-111111111111"
  }
}

override_resource {
  target = azurerm_user_assigned_identity.worker
  values = {
    id           = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-test/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-worker"
    principal_id = "22222222-2222-2222-2222-222222222222"
    client_id    = "22222222-aaaa-2222-2222-222222222222"
  }
}

override_resource {
  target = azurerm_user_assigned_identity.scaler
  values = {
    id           = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-test/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-scaler"
    principal_id = "33333333-3333-3333-3333-333333333333"
    client_id    = "33333333-aaaa-3333-3333-333333333333"
  }
}

override_resource {
  target = azurerm_storage_container.invoice_uploads
  values = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-test/providers/Microsoft.Storage/storageAccounts/sttest/blobServices/default/containers/invoice-uploads"
  }
}

override_resource {
  target = azurerm_storage_container.eventgrid_deadletter
  values = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-test/providers/Microsoft.Storage/storageAccounts/sttest/blobServices/default/containers/eventgrid-deadletter"
  }
}

override_resource {
  target = azapi_resource.usage_table
  values = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-test/providers/Microsoft.Storage/storageAccounts/sttest/tableServices/default/tables/aiusage"
  }
}

override_resource {
  target = azapi_resource.jobs_table
  values = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-test/providers/Microsoft.Storage/storageAccounts/sttest/tableServices/default/tables/invoicejobs"
  }
}

override_resource {
  target = azurerm_servicebus_namespace.main
  values = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-test/providers/Microsoft.ServiceBus/namespaces/sb-test"
  }
}

override_resource {
  target = azurerm_servicebus_queue.invoice_events
  values = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-test/providers/Microsoft.ServiceBus/namespaces/sb-test/queues/invoice-events"
  }
}

override_resource {
  target = azurerm_eventgrid_system_topic.storage
  values = {
    id       = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-test/providers/Microsoft.EventGrid/systemTopics/egst-test"
    identity = { principal_id = "44444444-4444-4444-4444-444444444444", tenant_id = "55555555-5555-5555-5555-555555555555" }
  }
}

override_resource {
  target = azapi_resource.worker_job
  values = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-test/providers/Microsoft.App/jobs/caj-test-worker"
  }
}


variables {
  container_image        = "ghcr.io/example/invoiceiq@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  openai_model_name      = "example-model"
  openai_model_version   = "2025-01-01"
  ai_price_input_per_1m  = "0.40"
  ai_price_output_per_1m = "1.60"
  budget_alert_email     = null
  # Pinned like production: `terraform test` also loads a local terraform.tfvars, so every input
  # an assertion depends on is set here explicitly (CI has no tfvars).
  ai_provider = "mock"
}

run "disabled_by_default_creates_nothing_and_leaves_the_app_unchanged" {
  command = plan

  variables {
    event_processing_enabled = false
    async_processing_enabled = false
  }

  assert {
    condition = alltrue([
      length(azurerm_storage_container.invoice_uploads) == 0,
      length(azurerm_storage_container.eventgrid_deadletter) == 0,
      length(azapi_resource.jobs_table) == 0,
      length(azurerm_storage_management_policy.retention) == 0,
      length(azurerm_servicebus_namespace.main) == 0,
      length(azurerm_servicebus_queue.invoice_events) == 0,
      length(azurerm_eventgrid_system_topic.storage) == 0,
      length(azurerm_eventgrid_system_topic_event_subscription.blob_created) == 0,
      length(azurerm_user_assigned_identity.worker) == 0,
      length(azurerm_user_assigned_identity.scaler) == 0,
      length(azapi_resource.worker_job) == 0,
    ])
    error_message = "Nothing may be created unless event_processing_enabled = true."
  }

  assert {
    condition = length(setintersection(
      toset([for e in azurerm_container_app.app.template[0].container[0].env : e.name]),
      toset(["ASYNC_PROCESSING_ENABLED", "SUBMISSION_BACKEND", "AZURE_BLOB_ENDPOINT", "UPLOAD_CONTAINER"])
    )) == 0
    error_message = "The web app's settings must be unchanged while the feature is off."
  }
}

run "enabled_infrastructure_is_least_privilege_and_cheap" {
  command = apply # mocked: gives resources IDs so role scopes can be compared

  variables {
    event_processing_enabled = true
  }

  # --- Service Bus Basic, Entra ID only ---
  assert {
    condition     = azurerm_servicebus_namespace.main[0].sku == "Basic"
    error_message = "Service Bus must be Basic (Standard adds a fixed ~DKK 64.5/month base charge)."
  }
  assert {
    condition     = azurerm_servicebus_namespace.main[0].local_auth_enabled == false && azurerm_servicebus_namespace.main[0].minimum_tls_version == "1.2"
    error_message = "Service Bus must refuse SAS keys and require TLS 1.2."
  }
  assert {
    condition = alltrue([
      azurerm_servicebus_queue.invoice_events[0].max_delivery_count == 5,
      azurerm_servicebus_queue.invoice_events[0].lock_duration == "PT1M",
      azurerm_servicebus_queue.invoice_events[0].default_message_ttl == "P1D",
      azurerm_servicebus_queue.invoice_events[0].dead_lettering_on_message_expiration,
      !azurerm_servicebus_queue.invoice_events[0].requires_duplicate_detection,
      !azurerm_servicebus_queue.invoice_events[0].requires_session,
    ])
    error_message = "Queue settings must match the worker's retry and lease assumptions."
  }

  # --- Storage ---
  assert {
    condition     = azurerm_storage_container.invoice_uploads[0].container_access_type == "private" && azurerm_storage_container.eventgrid_deadletter[0].container_access_type == "private"
    error_message = "Containers must be private."
  }
  assert {
    condition     = azurerm_storage_container.invoice_uploads[0].storage_account_id == azurerm_storage_account.usage.id && azapi_resource.jobs_table[0].name == "invoicejobs"
    error_message = "Containers must be created through Resource Manager in the existing account; status table is invoicejobs."
  }
  assert {
    condition = alltrue([
      for r in azurerm_storage_management_policy.retention[0].rule :
      r.actions[0].base_blob[0].delete_after_days_since_creation_greater_than == 7
    ]) && toset(flatten([for r in azurerm_storage_management_policy.retention[0].rule : r.filters[0].prefix_match])) == toset(["invoice-uploads/submissions/", "eventgrid-deadletter/"])
    error_message = "Uploads and dead-letters must be deleted after 7 days, and nothing else."
  }

  # --- Event Grid ---
  assert {
    condition     = azurerm_eventgrid_system_topic.storage[0].source_resource_id == azurerm_storage_account.usage.id && azurerm_eventgrid_system_topic.storage[0].topic_type == "Microsoft.Storage.StorageAccounts"
    error_message = "The system topic must watch the existing storage account."
  }
  assert {
    condition = alltrue([
      azurerm_eventgrid_system_topic_event_subscription.blob_created[0].service_bus_queue_endpoint_id == azurerm_servicebus_queue.invoice_events[0].id,
      azurerm_eventgrid_system_topic_event_subscription.blob_created[0].included_event_types == tolist(["Microsoft.Storage.BlobCreated"]),
      azurerm_eventgrid_system_topic_event_subscription.blob_created[0].subject_filter[0].subject_begins_with == "/blobServices/default/containers/invoice-uploads/blobs/submissions/",
      azurerm_eventgrid_system_topic_event_subscription.blob_created[0].subject_filter[0].subject_ends_with == ".json",
      azurerm_eventgrid_system_topic_event_subscription.blob_created[0].advanced_filter[0].string_in[0].key == "data.api",
      azurerm_eventgrid_system_topic_event_subscription.blob_created[0].event_delivery_schema == "EventGridSchema",
    ])
    error_message = "Only BlobCreated (Put Blob) events for submissions/*.json may reach the queue."
  }
  assert {
    condition = alltrue([
      azurerm_eventgrid_system_topic_event_subscription.blob_created[0].delivery_identity[0].type == "SystemAssigned",
      azurerm_eventgrid_system_topic_event_subscription.blob_created[0].dead_letter_identity[0].type == "SystemAssigned",
      azurerm_eventgrid_system_topic_event_subscription.blob_created[0].storage_blob_dead_letter_destination[0].storage_blob_container_name == "eventgrid-deadletter",
    ])
    error_message = "Event Grid must deliver and dead-letter with its managed identity (no keys)."
  }
  assert {
    condition = anytrue([
      for p in azurerm_eventgrid_system_topic_event_subscription.blob_created[0].delivery_property :
      p.header_name == "CorrelationId" && p.type == "Dynamic" && p.source_field == "data.clientRequestId"
    ])
    error_message = "The API correlation ID must be carried into the Service Bus message."
  }

  # --- Role assignments: exact scope per identity ---
  assert {
    condition = alltrue([
      azurerm_role_assignment.app_uploads_writer[0].scope == azurerm_storage_container.invoice_uploads[0].id,
      azurerm_role_assignment.app_jobs_table[0].scope == azapi_resource.jobs_table[0].id,
      azurerm_role_assignment.worker_uploads_reader[0].scope == azurerm_storage_container.invoice_uploads[0].id,
      azurerm_role_assignment.worker_uploads_reader[0].role_definition_name == "Storage Blob Data Reader",
      azurerm_role_assignment.worker_jobs_table[0].scope == azapi_resource.jobs_table[0].id,
      azurerm_role_assignment.worker_queue_receiver[0].scope == azurerm_servicebus_queue.invoice_events[0].id,
      azurerm_role_assignment.worker_queue_receiver[0].role_definition_name == "Azure Service Bus Data Receiver",
      azurerm_role_assignment.scaler_queue_owner[0].scope == azurerm_servicebus_queue.invoice_events[0].id,
      azurerm_role_assignment.eventgrid_queue_sender[0].scope == azurerm_servicebus_queue.invoice_events[0].id,
      azurerm_role_assignment.eventgrid_queue_sender[0].role_definition_name == "Azure Service Bus Data Sender",
      azurerm_role_assignment.eventgrid_deadletter_writer[0].scope == azurerm_storage_container.eventgrid_deadletter[0].id,
    ])
    error_message = "Every role must be scoped to one container, table or queue."
  }
  assert {
    condition = alltrue([
      azurerm_role_assignment.worker_uploads_reader[0].principal_id == azurerm_user_assigned_identity.worker[0].principal_id,
      azurerm_role_assignment.scaler_queue_owner[0].principal_id == azurerm_user_assigned_identity.scaler[0].principal_id,
      azurerm_role_assignment.app_openai_user.principal_id == azurerm_user_assigned_identity.app.principal_id,
      azurerm_user_assigned_identity.worker[0].id != azurerm_user_assigned_identity.app.id,
      azurerm_user_assigned_identity.scaler[0].id != azurerm_user_assigned_identity.worker[0].id,
    ])
    error_message = "App, worker and scaler must use separate identities; only the app may use Azure OpenAI."
  }

  # --- Worker job ---
  assert {
    condition = alltrue([
      azapi_resource.worker_job[0].type == "Microsoft.App/jobs@2025-01-01",
      azapi_resource.worker_job[0].body.properties.workloadProfileName == "Consumption",
      azapi_resource.worker_job[0].body.properties.configuration.triggerType == "Event",
      azapi_resource.worker_job[0].body.properties.configuration.eventTriggerConfig.scale.minExecutions == 0,
      azapi_resource.worker_job[0].body.properties.configuration.eventTriggerConfig.scale.maxExecutions == 3,
      azapi_resource.worker_job[0].body.properties.configuration.eventTriggerConfig.scale.pollingInterval == 10,
    ])
    error_message = "The worker must be an event-triggered job on Consumption that runs nothing while idle."
  }
  assert {
    condition = alltrue([
      azapi_resource.worker_job[0].body.properties.configuration.eventTriggerConfig.scale.rules[0].type == "azure-servicebus",
      azapi_resource.worker_job[0].body.properties.configuration.eventTriggerConfig.scale.rules[0].identity == azurerm_user_assigned_identity.scaler[0].id,
      azapi_resource.worker_job[0].body.properties.configuration.eventTriggerConfig.scale.rules[0].metadata.queueName == "invoice-events",
    ])
    error_message = "The scale rule must watch the queue with the scaler identity (no connection string)."
  }
  assert {
    condition = alltrue([
      for s in azapi_resource.worker_job[0].body.properties.configuration.identitySettings :
      (lower(s.identity) == lower(azurerm_user_assigned_identity.scaler[0].id) && s.lifecycle == "None") ||
      (lower(s.identity) == lower(azurerm_user_assigned_identity.worker[0].id) && s.lifecycle == "Main")
    ]) && length(azapi_resource.worker_job[0].body.properties.configuration.identitySettings) == 2
    error_message = "The scaler identity must not be available to container code (lifecycle None)."
  }
  assert {
    condition = alltrue([
      azapi_resource.worker_job[0].body.properties.template.containers[0].command == ["python", "-m", "invoiceiq.worker"],
      azapi_resource.worker_job[0].body.properties.template.containers[0].resources.cpu == 0.25,
      azapi_resource.worker_job[0].body.properties.template.containers[0].resources.memory == "0.5Gi",
      azapi_resource.worker_job[0].body.properties.template.containers[0].image == var.container_image,
    ])
    error_message = "Worker: same image, worker command, 0.25 vCPU / 0.5 GiB."
  }
  assert {
    condition = length([
      for e in azapi_resource.worker_job[0].body.properties.template.containers[0].env : e
      if startswith(e.name, "AI_") || startswith(e.name, "AZURE_OPENAI")
    ]) == 0
    error_message = "The worker must receive no AI settings."
  }
  assert {
    condition = alltrue([
      { for e in azapi_resource.worker_job[0].body.properties.template.containers[0].env : e.name => e.value }["WORKER_MAX_DELIVERY_COUNT"] == tostring(azurerm_servicebus_queue.invoice_events[0].max_delivery_count),
      { for e in azapi_resource.worker_job[0].body.properties.template.containers[0].env : e.name => e.value }["WORKER_LEASE_SECONDS"] == "60",
      { for e in azapi_resource.worker_job[0].body.properties.template.containers[0].env : e.name => e.value }["AZURE_CLIENT_ID"] == azurerm_user_assigned_identity.worker[0].client_id,
      tonumber({ for e in azapi_resource.worker_job[0].body.properties.template.containers[0].env : e.name => e.value }["WORKER_RUN_BUDGET_SECONDS"]) < azapi_resource.worker_job[0].body.properties.configuration.replicaTimeout,
    ])
    error_message = "Worker settings must match the queue (delivery count, lock) and stay within the job timeout."
  }

  # --- Web app: infrastructure wired, feature still off ---
  assert {
    condition = alltrue([
      { for e in azurerm_container_app.app.template[0].container[0].env : e.name => e.value }["ASYNC_PROCESSING_ENABLED"] == "false",
      { for e in azurerm_container_app.app.template[0].container[0].env : e.name => e.value }["SUBMISSION_BACKEND"] == "azure",
      { for e in azurerm_container_app.app.template[0].container[0].env : e.name => e.value }["SUBMISSIONS_TABLE"] == "invoicejobs",
      { for e in azurerm_container_app.app.template[0].container[0].env : e.name => e.value }["AI_PROVIDER"] == "mock",
    ])
    error_message = "Creating the infrastructure must not switch the feature (or live AI) on."
  }
}

run "feature_flag_reaches_the_app_only_when_requested" {
  command = plan

  variables {
    event_processing_enabled = true
    async_processing_enabled = true
  }

  assert {
    condition     = { for e in azurerm_container_app.app.template[0].container[0].env : e.name => e.value }["ASYNC_PROCESSING_ENABLED"] == "true"
    error_message = "async_processing_enabled must set ASYNC_PROCESSING_ENABLED=true."
  }
}

run "feature_requires_the_infrastructure" {
  command = plan

  variables {
    event_processing_enabled = false
    async_processing_enabled = true
  }

  expect_failures = [var.async_processing_enabled]
}

run "rejects_unbounded_retention" {
  command = plan

  variables {
    submission_retention_days = 0
  }

  expect_failures = [var.submission_retention_days]
}

run "rejects_too_many_worker_executions" {
  command = plan

  variables {
    worker_max_executions = 50
  }

  expect_failures = [var.worker_max_executions]
}
