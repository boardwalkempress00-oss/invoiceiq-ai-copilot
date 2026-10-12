# --- Subscription and placement --------------------------------------------------------------

variable "subscription_id" {
  description = "Azure subscription ID. Leave null to use ARM_SUBSCRIPTION_ID."
  type        = string
  default     = null
}

variable "location" {
  description = "Region for the app, logs and storage."
  type        = string
  default     = "swedencentral"
}

variable "openai_location" {
  description = "Region for Azure OpenAI. Model availability differs by region; null = var.location."
  type        = string
  default     = null
}

variable "name_prefix" {
  description = "Short lowercase prefix for resource names."
  type        = string
  default     = "invoiceiq"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{2,11}$", var.name_prefix))
    error_message = "name_prefix must be 3-12 lowercase letters/digits, starting with a letter."
  }
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default = {
    project     = "invoiceiq"
    environment = "demo"
    managed_by  = "terraform"
  }
}

# --- Container -------------------------------------------------------------------------------

variable "container_image" {
  description = "Public container image, e.g. ghcr.io/<owner>/invoiceiq-ai-copilot@sha256:<digest>. Pin a digest or SHA tag."
  type        = string

  validation {
    condition     = !endswith(var.container_image, ":latest")
    error_message = "Pin an immutable tag or digest instead of :latest."
  }
}

variable "min_replicas" {
  description = "0 = scale to zero when idle (no compute cost while idle)."
  type        = number
  default     = 0
}

variable "max_replicas" {
  description = "Upper bound on replicas; 1 keeps cost and the attack surface small."
  type        = number
  default     = 1
}

variable "http_concurrent_requests" {
  description = "Concurrent HTTP requests per replica before scaling out (bounded by max_replicas)."
  type        = number
  default     = 20
}

variable "scale_to_zero_cooldown_seconds" {
  description = "Idle time before scaling to zero."
  type        = number
  default     = 300
}

# --- Logging ---------------------------------------------------------------------------------

variable "log_retention_days" {
  description = "Log Analytics retention (30 is the minimum for the PerGB2018 tier)."
  type        = number
  default     = 30
}

variable "log_daily_quota_gb" {
  description = "Hard daily ingestion cap. Ingestion stops for the day when reached."
  type        = number
  default     = 0.1
}

# --- Azure OpenAI ----------------------------------------------------------------------------

variable "openai_model_name" {
  description = "Model to deploy. Check availability: az cognitiveservices model list -l <region>."
  type        = string
}

variable "openai_model_version" {
  description = "Model version (from the same model list)."
  type        = string
}

variable "openai_deployment_name" {
  description = "Deployment name used by the app (AZURE_OPENAI_DEPLOYMENT)."
  type        = string
  default     = "chat"
}

variable "openai_deployment_sku" {
  description = "Pay-per-token deployment type: GlobalStandard, DataZoneStandard or Standard."
  type        = string
  default     = "GlobalStandard"

  validation {
    # Provisioned (PTU) deployments bill per hour whether used or not — never for this demo.
    condition     = contains(["GlobalStandard", "DataZoneStandard", "Standard"], var.openai_deployment_sku)
    error_message = "Only pay-per-token deployment types are allowed (no provisioned throughput)."
  }
}

variable "openai_capacity_k_tpm" {
  description = "Rate limit in thousands of tokens per minute. Throughput cap only — NOT a spending cap."
  type        = number
  default     = 10
}

# --- Application limits ----------------------------------------------------------------------

variable "ai_price_input_per_1m" {
  description = "Input price per 1M tokens for the chosen model/region (from the Azure pricing page)."
  type        = string

  validation {
    condition     = tonumber(var.ai_price_input_per_1m) > 0
    error_message = "Set the real input price; the daily cost budget cannot work with a zero price."
  }
}

variable "ai_price_output_per_1m" {
  description = "Output price per 1M tokens for the chosen model/region."
  type        = string

  validation {
    condition     = tonumber(var.ai_price_output_per_1m) > 0
    error_message = "Set the real output price; the daily cost budget cannot work with a zero price."
  }
}

variable "ai_price_currency" {
  type    = string
  default = "USD"
}

variable "global_daily_cost_limit" {
  description = "Daily budget for estimated AI cost (in ai_price_currency)."
  type        = string
  default     = "1.00"
}

variable "global_daily_request_limit" {
  type    = number
  default = 200
}

variable "session_request_limit" {
  type    = number
  default = 10
}

variable "ai_provider" {
  description = "azure_openai for live answers; mock for a zero-cost public demo."
  type        = string
  default     = "azure_openai"

  validation {
    condition     = contains(["azure_openai", "mock", "disabled"], var.ai_provider)
    error_message = "ai_provider must be azure_openai, mock or disabled."
  }
}

# --- Cost alert ------------------------------------------------------------------------------

variable "budget_alert_email" {
  description = "Email for the resource-group budget alert (free). Null disables it."
  type        = string
  default     = null
}

variable "monthly_budget_amount" {
  description = "Monthly budget for the alert, in the billing currency. Alerts only; it does not stop spending."
  type        = number
  default     = 10
}

variable "budget_start_date" {
  description = "First day of the budget period (YYYY-MM-01T00:00:00Z), must not be in the past month."
  type        = string
  default     = "2026-11-01T00:00:00Z"
}

# --- GitHub Actions deployment (optional) ----------------------------------------------------

variable "github_repository" {
  description = "owner/repo allowed to deploy via OIDC. Null = no deploy identity is created."
  type        = string
  default     = null
}

variable "github_environment" {
  description = "GitHub environment whose jobs may deploy (protect it with required reviewers)."
  type        = string
  default     = "production"
}

# --- Public processing demo ----------------------------------------------------------------------

variable "demo_mode_enabled" {
  description = "Show the simulated processing demo (DEMO_MODE_ENABLED): predefined fictional scenarios processed in memory. Uses no Azure storage or messaging and is independent of async_processing_enabled."
  type        = bool
  default     = false
}

# --- Asynchronous invoice processing (see event_processing.tf) ---------------------------------

variable "event_processing_enabled" {
  description = "Create the asynchronous processing infrastructure (Blob container, status table, Event Grid, Service Bus Basic, worker job). Requires Microsoft.EventGrid and Microsoft.ServiceBus to be registered."
  type        = bool
  default     = false
}

variable "async_processing_enabled" {
  description = "Expose invoice upload in the app (ASYNC_PROCESSING_ENABLED). Requires event_processing_enabled."
  type        = bool
  default     = false

  validation {
    condition     = !var.async_processing_enabled || var.event_processing_enabled
    error_message = "async_processing_enabled requires event_processing_enabled = true."
  }
}

variable "submission_session_daily_limit" {
  description = "Invoice uploads per browser session per UTC day (separate from the AI allowance)."
  type        = number
  default     = 10

  validation {
    condition     = var.submission_session_daily_limit >= 0 && var.submission_session_daily_limit <= 100
    error_message = "submission_session_daily_limit must be between 0 and 100."
  }
}

variable "submission_global_daily_limit" {
  description = "Invoice uploads for the whole demo per UTC day."
  type        = number
  default     = 200

  validation {
    condition     = var.submission_global_daily_limit >= 0 && var.submission_global_daily_limit <= 2000
    error_message = "submission_global_daily_limit must be between 0 and 2000."
  }
}

variable "submission_retention_days" {
  description = "Days after which uploaded invoices (and Event Grid dead-letters) are deleted."
  type        = number
  default     = 7

  validation {
    condition     = var.submission_retention_days >= 1 && var.submission_retention_days <= 30
    error_message = "submission_retention_days must be between 1 and 30."
  }
}

variable "worker_polling_interval_seconds" {
  description = "How often KEDA checks the queue. Shorter = faster pickup, more (cheap) queue reads."
  type        = number
  default     = 10

  validation {
    condition     = var.worker_polling_interval_seconds >= 5 && var.worker_polling_interval_seconds <= 60
    error_message = "worker_polling_interval_seconds must be between 5 and 60."
  }
}

variable "worker_max_executions" {
  description = "Maximum concurrent worker job executions."
  type        = number
  default     = 3

  validation {
    condition     = var.worker_max_executions >= 1 && var.worker_max_executions <= 5
    error_message = "worker_max_executions must be between 1 and 5."
  }
}
