# One-time state migration (2026-10-10).
#
# The first apply created the `aiusage` table with `azurerm_storage_table`, then failed on that
# resource's shared-key ACL call. The table itself is fine, so it is adopted into
# `azapi_resource.usage_table` instead of being recreated.
#
# The import runs only when `usage_table_import_id` is set (in the local, git-ignored
# terraform.tfvars), so fresh deployments and CI tests import nothing and no subscription ID is
# committed. Unset the variable and delete this file once the import has been applied.

variable "usage_table_import_id" {
  description = "Exact ARM resource ID of an existing aiusage table to adopt; null = no import."
  type        = string
  default     = null

  validation {
    condition     = var.usage_table_import_id == null || can(regex("^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\\.Storage/storageAccounts/[^/]+/tableServices/default/tables/aiusage$", var.usage_table_import_id))
    error_message = "Must be the full ARM ID of the aiusage table."
  }
}

import {
  for_each = var.usage_table_import_id == null ? toset([]) : toset([var.usage_table_import_id])
  to       = azapi_resource.usage_table
  # Same API version as the resource definition, so the import introduces no diff.
  id = "${each.value}?api-version=2023-05-01"
}
