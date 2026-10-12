# Plan-only tests: independent web/worker images and safe defaults. Mocked providers.
#   terraform test
#
# These checks see the real defaults: production values live in production.tfvars, which
# `terraform test` does not load (it auto-loads only terraform.tfvars and *.auto.tfvars).

mock_provider "azurerm" {}
mock_provider "random" {}
mock_provider "azapi" {}

variables {
  container_image          = "ghcr.io/example/invoiceiq@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  openai_model_name        = "example-model"
  openai_model_version     = "2025-01-01"
  ai_price_input_per_1m    = "0.40"
  ai_price_output_per_1m   = "1.60"
  budget_alert_email       = null
  event_processing_enabled = true
  # ai_provider, demo_mode_enabled and async_processing_enabled are deliberately not set here.
}

run "defaults_never_enable_live_ai_demo_or_async" {
  command = plan

  assert {
    condition     = var.ai_provider == "mock"
    error_message = "ai_provider must default to mock: live Azure OpenAI is opt-in only."
  }

  assert {
    condition = alltrue([
      { for e in azurerm_container_app.app.template[0].container[0].env : e.name => e.value }["AI_PROVIDER"] == "mock",
      { for e in azurerm_container_app.app.template[0].container[0].env : e.name => e.value }["DEMO_MODE_ENABLED"] == "false",
      { for e in azurerm_container_app.app.template[0].container[0].env : e.name => e.value }["ASYNC_PROCESSING_ENABLED"] == "false",
    ])
    error_message = "By default the app must use the mock AI, with the demo and live processing off."
  }
}

run "worker_image_defaults_to_the_web_image" {
  command = plan

  assert {
    condition     = azapi_resource.worker_job[0].body.properties.template.containers[0].image == var.container_image
    error_message = "Without worker_container_image the worker must keep using container_image (backward compatible)."
  }
}

run "web_image_update_leaves_the_worker_image_alone" {
  command = plan

  variables {
    container_image        = "ghcr.io/example/invoiceiq@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    worker_container_image = "ghcr.io/example/invoiceiq@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }

  assert {
    condition     = azurerm_container_app.app.template[0].container[0].image == "ghcr.io/example/invoiceiq@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    error_message = "The web app must use container_image."
  }

  assert {
    condition     = azapi_resource.worker_job[0].body.properties.template.containers[0].image == "ghcr.io/example/invoiceiq@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    error_message = "Changing container_image must not change a pinned worker image."
  }
}

run "worker_image_can_be_updated_independently" {
  command = plan

  variables {
    worker_container_image = "ghcr.io/example/invoiceiq@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  }

  assert {
    condition = alltrue([
      azapi_resource.worker_job[0].body.properties.template.containers[0].image == "ghcr.io/example/invoiceiq@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      azurerm_container_app.app.template[0].container[0].image == var.container_image,
    ])
    error_message = "worker_container_image must change only the worker's image."
  }

  # Nothing else about the worker changes with its image.
  assert {
    condition = alltrue([
      azapi_resource.worker_job[0].body.properties.template.containers[0].command == ["python", "-m", "invoiceiq.worker"],
      azapi_resource.worker_job[0].body.properties.template.containers[0].resources.cpu == 0.25,
      azapi_resource.worker_job[0].body.properties.template.containers[0].resources.memory == "0.5Gi",
      length(azapi_resource.worker_job[0].body.properties.template.containers[0].env) == 14,
    ])
    error_message = "The worker's command, size and settings must not depend on its image."
  }
}

run "rejects_latest_worker_image" {
  command = plan

  variables {
    worker_container_image = "ghcr.io/example/invoiceiq:latest"
  }

  expect_failures = [var.worker_container_image]
}

# The intended production rollout: new web image, previous worker image, demo on, live off.
run "production_rollout_configuration" {
  command = plan

  variables {
    container_image          = "ghcr.io/example/invoiceiq@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    worker_container_image   = "ghcr.io/example/invoiceiq@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    ai_provider              = "mock"
    demo_mode_enabled        = true
    async_processing_enabled = false
  }

  assert {
    condition = alltrue([
      { for e in azurerm_container_app.app.template[0].container[0].env : e.name => e.value }["AI_PROVIDER"] == "mock",
      { for e in azurerm_container_app.app.template[0].container[0].env : e.name => e.value }["DEMO_MODE_ENABLED"] == "true",
      { for e in azurerm_container_app.app.template[0].container[0].env : e.name => e.value }["ASYNC_PROCESSING_ENABLED"] == "false",
      azapi_resource.worker_job[0].body.properties.template.containers[0].image != azurerm_container_app.app.template[0].container[0].image,
    ])
    error_message = "Production: mock AI, demo on, live processing off, worker image pinned separately."
  }
}
