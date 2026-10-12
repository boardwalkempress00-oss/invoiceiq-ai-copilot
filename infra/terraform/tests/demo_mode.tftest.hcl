# Plan-only tests for the public processing demo flag. Mocked providers: nothing is created.
#   terraform test

mock_provider "azurerm" {}
mock_provider "random" {}
mock_provider "azapi" {}

variables {
  container_image        = "ghcr.io/example/invoiceiq@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  openai_model_name      = "example-model"
  openai_model_version   = "2025-01-01"
  ai_price_input_per_1m  = "0.40"
  ai_price_output_per_1m = "1.60"
  budget_alert_email     = null
  # Pinned like production; `terraform test` also loads a local terraform.tfvars.
  ai_provider              = "mock"
  event_processing_enabled = true
  async_processing_enabled = false
}

run "demo_is_off_by_default" {
  command = plan

  variables {
    demo_mode_enabled = false
  }

  assert {
    condition     = { for e in azurerm_container_app.app.template[0].container[0].env : e.name => e.value }["DEMO_MODE_ENABLED"] == "false"
    error_message = "DEMO_MODE_ENABLED must default to false."
  }
}

run "demo_flag_changes_only_the_demo_setting" {
  command = plan

  variables {
    demo_mode_enabled = true
  }

  assert {
    condition = alltrue([
      { for e in azurerm_container_app.app.template[0].container[0].env : e.name => e.value }["DEMO_MODE_ENABLED"] == "true",
      { for e in azurerm_container_app.app.template[0].container[0].env : e.name => e.value }["ASYNC_PROCESSING_ENABLED"] == "false",
      { for e in azurerm_container_app.app.template[0].container[0].env : e.name => e.value }["AI_PROVIDER"] == "mock",
    ])
    error_message = "The demo flag must not enable live processing or change the AI provider."
  }

  assert {
    condition = alltrue([
      azurerm_container_app.app.template[0].min_replicas == 0,
      azurerm_container_app.app.template[0].max_replicas == 1,
      azurerm_container_app.app.ingress[0].target_port == 8501,
      azurerm_container_app.app.ingress[0].external_enabled,
      azurerm_container_app.app.identity[0].type == "UserAssigned",
    ])
    error_message = "The demo flag must not change scaling, ingress or identity."
  }

  assert {
    condition = alltrue([
      length(azurerm_servicebus_namespace.main) == 1,
      length(azurerm_eventgrid_system_topic_event_subscription.blob_created) == 1,
      length(azapi_resource.worker_job) == 1,
    ])
    error_message = "The demo flag must leave the event-processing infrastructure as it is."
  }
}

run "demo_default_is_false_in_the_variable" {
  command = plan

  assert {
    condition     = var.demo_mode_enabled == false
    error_message = "demo_mode_enabled must default to false."
  }
}
