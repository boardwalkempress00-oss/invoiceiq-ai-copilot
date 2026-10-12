# InvoiceIQ — Milestone 1 infrastructure.
# One public container app (Streamlit + private FastAPI), Azure OpenAI, a table for the usage
# limiter and Log Analytics. Everything authenticates with one user-assigned managed identity;
# no API keys or connection strings are created or passed to the app.

locals {
  openai_location = coalesce(var.openai_location, var.location)
  suffix          = random_string.suffix.result
  base            = "${var.name_prefix}-${local.suffix}"
}

resource "random_string" "suffix" {
  length  = 5
  upper   = false
  special = false
}

resource "azurerm_resource_group" "main" {
  name     = "rg-${local.base}"
  location = var.location
  tags     = var.tags
}

# --- Observability ---------------------------------------------------------------------------

resource "azurerm_log_analytics_workspace" "main" {
  name                = "log-${local.base}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  daily_quota_gb      = var.log_daily_quota_gb
  tags                = var.tags
}

# --- Identity --------------------------------------------------------------------------------

resource "azurerm_user_assigned_identity" "app" {
  name                = "id-${local.base}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = var.tags
}

# --- Azure OpenAI ----------------------------------------------------------------------------

resource "azurerm_cognitive_account" "openai" {
  name                  = "oai-${local.base}"
  location              = local.openai_location
  resource_group_name   = azurerm_resource_group.main.name
  kind                  = "OpenAI"
  sku_name              = "S0"
  custom_subdomain_name = "oai-${local.base}" # required for Entra ID token authentication
  local_auth_enabled    = false               # API keys disabled: managed identity only
  tags                  = var.tags
}

resource "azurerm_cognitive_deployment" "chat" {
  name                 = var.openai_deployment_name
  cognitive_account_id = azurerm_cognitive_account.openai.id

  model {
    format  = "OpenAI"
    name    = var.openai_model_name
    version = var.openai_model_version
  }

  sku {
    name     = var.openai_deployment_sku
    capacity = var.openai_capacity_k_tpm
  }

  version_upgrade_option = "NoAutoUpgrade" # model changes are deliberate, not automatic
}

resource "azurerm_role_assignment" "app_openai_user" {
  scope                = azurerm_cognitive_account.openai.id
  role_definition_name = "Cognitive Services OpenAI User" # inference only, no management rights
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

# --- Usage limiter storage -------------------------------------------------------------------

resource "azurerm_storage_account" "usage" {
  name                            = "st${var.name_prefix}${local.suffix}"
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

# Created through Azure Resource Manager (management plane) only. `azurerm_storage_table` also
# calls the data-plane "Set Table ACL" operation, which the provider signs with a shared key —
# refused here because shared-key access is disabled. The empty body sets no stored access
# policies (ACLs); access is granted solely through the RBAC role assignment below.
resource "azapi_resource" "usage_table" {
  type      = "Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01"
  name      = "aiusage"
  parent_id = "${azurerm_storage_account.usage.id}/tableServices/default"
  body = {
    properties = {
      signedIdentifiers = [] # explicitly no stored access policies
    }
  }
}

resource "azurerm_role_assignment" "app_table_data" {
  scope                = azapi_resource.usage_table.id # this table only
  role_definition_name = "Storage Table Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

# --- Container Apps --------------------------------------------------------------------------

resource "azurerm_container_app_environment" "main" {
  name                       = "cae-${local.base}"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  logs_destination           = "log-analytics"
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  tags                       = var.tags

  # Azure creates new environments with the serverless "Consumption" workload profile. It is
  # billed per use with no management fee (that fee applies only to Dedicated profiles, which
  # must never be added here — see tests). Declared so Terraform does not try to remove it.
  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }
}

locals {
  app_env = {
    APP_ENV                    = "production"
    LOG_LEVEL                  = "INFO"
    AI_PROVIDER                = var.ai_provider
    AZURE_OPENAI_ENDPOINT      = azurerm_cognitive_account.openai.endpoint
    AZURE_OPENAI_DEPLOYMENT    = azurerm_cognitive_deployment.chat.name
    AZURE_CLIENT_ID            = azurerm_user_assigned_identity.app.client_id # selects the identity
    USAGE_STORE                = "azure_table"
    AZURE_TABLE_ENDPOINT       = azurerm_storage_account.usage.primary_table_endpoint
    AZURE_TABLE_NAME           = azapi_resource.usage_table.name
    AI_PRICE_INPUT_PER_1M      = var.ai_price_input_per_1m
    AI_PRICE_OUTPUT_PER_1M     = var.ai_price_output_per_1m
    AI_PRICE_CURRENCY          = var.ai_price_currency
    GLOBAL_DAILY_COST_LIMIT    = var.global_daily_cost_limit
    GLOBAL_DAILY_REQUEST_LIMIT = tostring(var.global_daily_request_limit)
    SESSION_REQUEST_LIMIT      = tostring(var.session_request_limit)
    # Simulated, in-memory processing demo. Independent of ASYNC_PROCESSING_ENABLED and never
    # uses Azure storage or messaging.
    DEMO_MODE_ENABLED = tostring(var.demo_mode_enabled)
  }
}

resource "azurerm_container_app" "app" {
  name                         = "ca-${local.base}"
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"
  max_inactive_revisions       = 2
  tags                         = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  ingress {
    external_enabled           = true
    target_port                = 8501   # Streamlit; FastAPI is bound to 127.0.0.1 inside the container
    transport                  = "auto" # HTTP/1.1 + WebSockets (Streamlit needs WebSockets)
    allow_insecure_connections = false  # HTTPS only

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas                     = var.min_replicas
    max_replicas                     = var.max_replicas
    cooldown_period_in_seconds       = var.scale_to_zero_cooldown_seconds
    termination_grace_period_seconds = 30

    http_scale_rule {
      name                = "http"
      concurrent_requests = tostring(var.http_concurrent_requests)
    }

    container {
      name   = "invoiceiq"
      image  = var.container_image
      cpu    = 0.5
      memory = "1Gi"

      dynamic "env" {
        for_each = merge(local.app_env, local.async_app_env) # async_app_env is {} by default
        content {
          name  = env.key
          value = env.value
        }
      }

      startup_probe {
        transport               = "HTTP"
        port                    = 8501
        path                    = "/_stcore/health"
        initial_delay           = 1
        interval_seconds        = 1
        failure_count_threshold = 60 # same ~60 s startup tolerance; detects readiness within ~1 s
      }

      liveness_probe {
        transport               = "HTTP"
        port                    = 8501
        path                    = "/_stcore/health"
        interval_seconds        = 30
        failure_count_threshold = 3
      }

      readiness_probe {
        transport               = "HTTP"
        port                    = 8501
        path                    = "/_stcore/health"
        interval_seconds        = 2
        failure_count_threshold = 15 # same 30 s tolerance before "not ready" (ACA may restart)
        # Provider default is 3, i.e. ~20-25 s of extra delay before a new replica gets traffic.
        success_count_threshold = 1
      }
    }
  }

  # Roles must exist before the first revision starts, or the first requests fail closed.
  depends_on = [
    azurerm_role_assignment.app_openai_user,
    azurerm_role_assignment.app_table_data,
    azurerm_role_assignment.app_uploads_writer, # empty unless event_processing_enabled
    azurerm_role_assignment.app_jobs_table,
  ]
}

# --- Cost alert (free; notifies, does not stop spending) -------------------------------------

resource "azurerm_consumption_budget_resource_group" "main" {
  count             = var.budget_alert_email == null ? 0 : 1
  name              = "budget-${local.base}"
  resource_group_id = azurerm_resource_group.main.id
  amount            = var.monthly_budget_amount
  time_grain        = "Monthly"

  time_period {
    start_date = var.budget_start_date
  }

  notification {
    enabled        = true
    operator       = "GreaterThan"
    threshold      = 50
    threshold_type = "Actual"
    contact_emails = [var.budget_alert_email]
  }

  notification {
    enabled        = true
    operator       = "GreaterThan"
    threshold      = 100
    threshold_type = "Forecasted"
    contact_emails = [var.budget_alert_email]
  }
}
