terraform {
  required_version = ">= 1.9"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.81"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
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
}
