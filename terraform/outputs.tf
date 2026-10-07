output "resource_group_id" {
  description = "Resource Group ID"
  value       = azurerm_resource_group.main.id
}

output "resource_group_name" {
  description = "Resource Group name"
  value       = azurerm_resource_group.main.name
}

output "container_app_fqdn" {
  description = "Container App fully qualified domain name"
  value       = azurerm_container_app.aurora.ingress[0].fqdn
}

output "container_app_id" {
  description = "Container App resource ID"
  value       = azurerm_container_app.aurora.id
}

output "container_app_url" {
  description = "Container App public URL"
  value       = "https://${azurerm_container_app.aurora.ingress[0].fqdn}"
}

output "container_registry_login_server" {
  description = "Container Registry login server"
  value       = azurerm_container_registry.aurora.login_server
}

output "container_registry_id" {
  description = "Container Registry resource ID"
  value       = azurerm_container_registry.aurora.id
}

output "key_vault_uri" {
  description = "Key Vault URI"
  value       = azurerm_key_vault.aurora.vault_uri
}

output "key_vault_id" {
  description = "Key Vault resource ID"
  value       = azurerm_key_vault.aurora.id
}

output "managed_identity_id" {
  description = "Managed Identity resource ID"
  value       = azurerm_user_assigned_identity.aurora.id
}

output "managed_identity_client_id" {
  description = "Managed Identity client ID"
  value       = azurerm_user_assigned_identity.aurora.client_id
}

output "managed_identity_principal_id" {
  description = "Managed Identity principal ID (object ID)"
  value       = azurerm_user_assigned_identity.aurora.principal_id
}

output "app_insights_instrumentation_key" {
  description = "Application Insights instrumentation key"
  value       = azurerm_application_insights.aurora.instrumentation_key
  sensitive   = true
}

output "app_insights_connection_string" {
  description = "Application Insights connection string"
  value       = azurerm_application_insights.aurora.connection_string
  sensitive   = true
}

output "app_insights_id" {
  description = "Application Insights resource ID"
  value       = azurerm_application_insights.aurora.id
}

output "storage_account_id" {
  description = "Storage Account resource ID"
  value       = azurerm_storage_account.aurora.id
}

output "storage_account_primary_endpoint" {
  description = "Storage Account primary file endpoint"
  value       = azurerm_storage_account.aurora.primary_file_endpoint
}

output "storage_share_id" {
  description = "Storage file share resource ID"
  value       = azurerm_storage_share.aurora_data.id
}

output "log_analytics_workspace_id" {
  description = "Log Analytics Workspace resource ID"
  value       = azurerm_log_analytics_workspace.aurora.id
}

output "log_analytics_workspace_customer_id" {
  description = "Log Analytics Workspace customer ID"
  value       = azurerm_log_analytics_workspace.aurora.workspace_id
  sensitive   = true
}

output "container_app_environment_id" {
  description = "Container Apps Environment resource ID"
  value       = azurerm_container_app_environment.aurora.id
}

output "deployment_info" {
  description = "Deployment summary information"
  value = {
    deployed_at = timestamp()
    environment = var.environment
    region      = var.location
    app_url     = "https://${azurerm_container_app.aurora.ingress[0].fqdn}"
    acr_login   = "az acr login --name ${azurerm_container_registry.aurora.name}"
  }
}
