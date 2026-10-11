output "app_url" {
  description = "Public HTTPS URL of the Streamlit app."
  value       = "https://${azurerm_container_app.app.ingress[0].fqdn}"
}

output "resource_group_name" {
  value = azurerm_resource_group.main.name
}

output "container_app_name" {
  value = azurerm_container_app.app.name
}

output "openai_endpoint" {
  value = azurerm_cognitive_account.openai.endpoint
}

output "usage_table_endpoint" {
  value = azurerm_storage_account.usage.primary_table_endpoint
}

output "app_identity_client_id" {
  description = "Client ID of the app's managed identity (not a secret)."
  value       = azurerm_user_assigned_identity.app.client_id
}

output "github_deployer_client_id" {
  description = "Set as the AZURE_CLIENT_ID repository variable for the deploy workflow."
  value       = var.github_repository == null ? null : azurerm_user_assigned_identity.github_deployer[0].client_id
}

output "servicebus_namespace" {
  description = "Service Bus namespace for asynchronous processing (null when disabled)."
  value       = var.event_processing_enabled ? azurerm_servicebus_namespace.main[0].name : null
}

output "worker_job_name" {
  description = "Container Apps Job that processes uploaded invoices (null when disabled)."
  value       = var.event_processing_enabled ? azapi_resource.worker_job[0].name : null
}
