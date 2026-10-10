# Plan-only tests with mocked providers: no Azure credentials, no API calls, nothing created.
#   terraform test

mock_provider "azurerm" {}
mock_provider "random" {}
mock_provider "azapi" {}

variables {
  # Explicit, so a local terraform.tfvars cannot turn on the one-time table import in tests.
  usage_table_import_id  = null
  container_image        = "ghcr.io/example/invoiceiq:0123abc"
  openai_model_name      = "example-model"
  openai_model_version   = "2025-01-01"
  ai_price_input_per_1m  = "0.40"
  ai_price_output_per_1m = "1.60"
}

run "cost_and_scaling_settings" {
  command = plan

  # Explicit, so a local terraform.tfvars (which `terraform test` also loads) cannot change it.
  variables {
    budget_alert_email = null
  }

  assert {
    condition     = azurerm_container_app.app.template[0].min_replicas == 0 && azurerm_container_app.app.template[0].max_replicas == 1
    error_message = "The app must scale to zero and never above one replica."
  }

  assert {
    condition     = azurerm_container_app.app.template[0].container[0].cpu == 0.5 && azurerm_container_app.app.template[0].container[0].memory == "1Gi"
    error_message = "Container size must be 0.5 vCPU / 1 GiB."
  }

  assert {
    condition = length(azurerm_container_app_environment.main.workload_profile) == 1 && alltrue([
      for p in azurerm_container_app_environment.main.workload_profile : p.workload_profile_type == "Consumption"
    ])
    error_message = "Only the serverless Consumption profile is allowed (Dedicated profiles add a fixed fee)."
  }

  assert {
    condition     = azurerm_container_app.app.workload_profile_name == "Consumption"
    error_message = "The app must run on the Consumption profile."
  }

  assert {
    condition     = azurerm_log_analytics_workspace.main.retention_in_days == 30 && azurerm_log_analytics_workspace.main.daily_quota_gb == 0.1
    error_message = "Log retention and daily ingestion cap must stay minimal."
  }

  assert {
    condition     = azurerm_storage_account.usage.account_replication_type == "LRS" && azurerm_storage_account.usage.account_tier == "Standard"
    error_message = "Usage storage must be the cheapest tier (Standard LRS)."
  }

  assert {
    condition     = azurerm_cognitive_deployment.chat.version_upgrade_option == "NoAutoUpgrade"
    error_message = "Model upgrades must be deliberate."
  }

  assert {
    condition     = length(azurerm_consumption_budget_resource_group.main) == 0
    error_message = "Budget alert must be optional."
  }
}

run "ingress_and_identity_security" {
  command = plan

  assert {
    condition     = azurerm_container_app.app.ingress[0].target_port == 8501 && azurerm_container_app.app.ingress[0].external_enabled
    error_message = "Only Streamlit (8501) is exposed publicly."
  }

  assert {
    condition     = azurerm_container_app.app.ingress[0].allow_insecure_connections == false
    error_message = "Ingress must be HTTPS only."
  }

  assert {
    condition     = azurerm_cognitive_account.openai.local_auth_enabled == false
    error_message = "Azure OpenAI API keys must be disabled (managed identity only)."
  }

  assert {
    condition     = azurerm_storage_account.usage.shared_access_key_enabled == false && azurerm_storage_account.usage.allow_nested_items_to_be_public == false
    error_message = "Storage must not allow shared keys or public access."
  }

  assert {
    condition     = azurerm_role_assignment.app_openai_user.role_definition_name == "Cognitive Services OpenAI User"
    error_message = "The app identity must have inference-only access to Azure OpenAI."
  }

  assert {
    condition     = azurerm_role_assignment.app_table_data.role_definition_name == "Storage Table Data Contributor"
    error_message = "The app identity needs table data access only."
  }

  assert {
    condition     = azapi_resource.usage_table.type == "Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01" && azapi_resource.usage_table.name == "aiusage"
    error_message = "The usage table must be created through Azure Resource Manager (management plane)."
  }

  assert {
    condition     = length(azapi_resource.usage_table.body.properties.signedIdentifiers) == 0
    error_message = "The usage table must not define stored access policies (ACLs); access is RBAC only."
  }

  assert {
    condition = contains([
      for e in azurerm_container_app.app.template[0].container[0].env : "${e.name}=${e.value}"
    ], "AZURE_TABLE_NAME=aiusage")
    error_message = "The app must use the aiusage table."
  }

  assert {
    condition = alltrue([
      for e in azurerm_container_app.app.template[0].container[0].env :
      !can(regex("(?i)(key|secret|password|connection_string|token)", e.name))
    ])
    error_message = "No credential-like environment variables may be passed to the app."
  }

  assert {
    condition     = length(azurerm_container_app.app.secret) == 0 && length(azurerm_container_app.app.registry) == 0
    error_message = "The app must not hold secrets or registry credentials."
  }

  assert {
    condition = contains([
      for e in azurerm_container_app.app.template[0].container[0].env : "${e.name}=${e.value}"
    ], "USAGE_STORE=azure_table")
    error_message = "Production must use the persistent Azure Table usage store."
  }
}

run "budget_alert_when_email_given" {
  command = plan

  variables {
    budget_alert_email = "alerts@example.com"
  }

  assert {
    condition     = length(azurerm_consumption_budget_resource_group.main) == 1
    error_message = "Budget alert should be created when an email is provided."
  }
}

run "rejects_latest_tag" {
  command = plan

  variables {
    container_image = "ghcr.io/example/invoiceiq:latest"
  }

  expect_failures = [var.container_image]
}

run "rejects_zero_prices" {
  command = plan

  variables {
    ai_price_input_per_1m = "0"
  }

  expect_failures = [var.ai_price_input_per_1m]
}

run "github_oidc_identity_is_scoped" {
  command = plan

  variables {
    github_repository = "example/invoiceiq"
  }

  assert {
    condition     = azurerm_federated_identity_credential.github_environment[0].subject == "repo:example/invoiceiq:environment:production"
    error_message = "Federation must trust only the protected production environment of this repo."
  }

  assert {
    condition     = azurerm_role_assignment.github_rg_reader[0].role_definition_name == "Reader"
    error_message = "The deployer gets read-only access to the resource group."
  }
}

run "no_github_identity_by_default" {
  command = plan

  assert {
    condition     = length(azurerm_user_assigned_identity.github_deployer) == 0
    error_message = "No deploy identity unless a repository is configured."
  }
}

run "rejects_provisioned_throughput" {
  command = plan

  variables {
    openai_deployment_sku = "ProvisionedManaged"
  }

  expect_failures = [var.openai_deployment_sku]
}

run "rejects_malformed_table_import_id" {
  command = plan

  variables {
    usage_table_import_id = "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/st/tableServices/default/tables/other"
  }

  expect_failures = [var.usage_table_import_id]
}
