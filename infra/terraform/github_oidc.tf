# Optional: identity for the GitHub Actions deploy workflow (OIDC federation, no client secret).
# Created only when var.github_repository is set. It may update the container app's image and
# nothing else: Contributor on the container app resource, Reader on the resource group.

resource "azurerm_user_assigned_identity" "github_deployer" {
  count               = var.github_repository == null ? 0 : 1
  name                = "id-${local.base}-deploy"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "github_environment" {
  count                     = var.github_repository == null ? 0 : 1
  name                      = "github-${var.github_environment}"
  user_assigned_identity_id = azurerm_user_assigned_identity.github_deployer[0].id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = "https://token.actions.githubusercontent.com"
  # Only workflow jobs that run in this repository's protected environment can sign in.
  subject = "repo:${var.github_repository}:environment:${var.github_environment}"
}

resource "azurerm_role_assignment" "github_app_contributor" {
  count                = var.github_repository == null ? 0 : 1
  scope                = azurerm_container_app.app.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.github_deployer[0].principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "github_rg_reader" {
  count                = var.github_repository == null ? 0 : 1
  scope                = azurerm_resource_group.main.id
  role_definition_name = "Reader"
  principal_id         = azurerm_user_assigned_identity.github_deployer[0].principal_id
  principal_type       = "ServicePrincipal"
}
