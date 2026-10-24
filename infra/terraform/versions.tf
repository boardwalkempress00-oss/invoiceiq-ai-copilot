terraform {
  required_version = ">= 1.9"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.10"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.13"
    }
  }

  # State is local by default (single maintainer). State contains resource IDs and the Azure
  # OpenAI account's (disabled) access keys, so keep it out of git (see .gitignore). For a team,
  # configure a remote backend, e.g. an azurerm backend in a separately managed storage account.
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id # null → falls back to ARM_SUBSCRIPTION_ID
  # Use Entra ID (not shared keys) for any storage data-plane calls the provider makes.
  storage_use_azuread = true
  # Never register resource providers implicitly (that is a subscription-wide change, and the
  # provider would do it even during `plan`). Required providers are registered deliberately:
  # Microsoft.App, Microsoft.CognitiveServices, Microsoft.Storage, Microsoft.OperationalInsights,
  # Microsoft.ManagedIdentity, Microsoft.Authorization and Microsoft.Consumption — plus
  # Microsoft.ServiceBus and Microsoft.EventGrid when event_processing_enabled = true.
  resource_provider_registrations = "none"
}

# AzAPI calls Azure Resource Manager directly. Used for the usage table, which must be created
# through the management plane only (see main.tf). Same subscription, same registration policy.
provider "azapi" {
  subscription_id            = var.subscription_id
  skip_provider_registration = true
}
