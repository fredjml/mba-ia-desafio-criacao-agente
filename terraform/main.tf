terraform {
  required_version = ">= 1.5"
  
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.85"
    }
  }

  # Uncomment for remote state storage
  # backend "azurerm" {
  #   resource_group_name  = "terraform-state"
  #   storage_account_name = "tfstate"
  #   container_name       = "state"
  #   key                  = "aurora-prod.tfstate"
  # }
}

provider "azurerm" {
  features {}
}

data "azurerm_client_config" "current" {}

# Resource Group
resource "azurerm_resource_group" "main" {
  name     = "${var.resource_prefix}-rg-${var.environment}"
  location = var.location

  tags = {
    Project     = "Aurora Virtual Assistant"
    Version     = "v1.0.0"
    Environment = var.environment
    ManagedBy   = "Terraform"
  }
}

# Managed Identity
resource "azurerm_user_assigned_identity" "aurora" {
  name                = "${var.resource_prefix}-mi-${var.environment}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location

  tags = azurerm_resource_group.main.tags
}

# Log Analytics Workspace
resource "azurerm_log_analytics_workspace" "aurora" {
  name                = "${var.resource_prefix}-la-${var.environment}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "PerGB2018"
  retention_in_days   = 30

  tags = azurerm_resource_group.main.tags
}

# Application Insights
resource "azurerm_application_insights" "aurora" {
  name                = "${var.resource_prefix}-ai-${var.environment}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  application_type    = "web"
  workspace_id        = azurerm_log_analytics_workspace.aurora.id

  tags = azurerm_resource_group.main.tags
}

# Container Registry
resource "azurerm_container_registry" "aurora" {
  name                = replace("${var.resource_prefix}acr", "-", "")
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "Standard"

  admin_enabled = false

  tags = azurerm_resource_group.main.tags
}

# Role Assignment: Managed Identity -> ACR Pull
resource "azurerm_role_assignment" "acr_pull" {
  scope              = azurerm_container_registry.aurora.id
  role_definition_id = "/subscriptions/${data.azurerm_client_config.current.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/7f951dda-4ed3-4680-a7ca-43fe172d538d"
  principal_id       = azurerm_user_assigned_identity.aurora.principal_id
}

# Storage Account
resource "azurerm_storage_account" "aurora" {
  name                     = replace("${var.resource_prefix}st", "-", "")
  resource_group_name      = azurerm_resource_group.main.name
  location                 = azurerm_resource_group.main.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  https_traffic_only_enabled = true
  min_tls_version          = "TLS1_2"

  tags = azurerm_resource_group.main.tags
}

# Storage File Share
resource "azurerm_storage_share" "aurora_data" {
  name                 = "aurora-data"
  storage_account_name = azurerm_storage_account.aurora.name
  quota                = 10

  depends_on = [azurerm_storage_account.aurora]
}

# Key Vault
resource "azurerm_key_vault" "aurora" {
  name                       = "${var.resource_prefix}-kv-${substr(azurerm_resource_group.main.id, -8, 8)}"
  resource_group_name        = azurerm_resource_group.main.name
  location                   = azurerm_resource_group.main.location
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  enabled_for_deployment     = true
  soft_delete_retention_days = 90
  purge_protection_enabled   = true

  access_policy {
    tenant_id = data.azurerm_client_config.current.tenant_id
    object_id = azurerm_user_assigned_identity.aurora.principal_id

    secret_permissions = [
      "Get",
      "List"
    ]
  }

  tags = azurerm_resource_group.main.tags
}

# Key Vault Secret: LLM API Key
resource "azurerm_key_vault_secret" "llm_api_key" {
  name         = "llm-api-key"
  value        = var.llm_api_key
  key_vault_id = azurerm_key_vault.aurora.id

  depends_on = [azurerm_key_vault.aurora]
}

# Container Apps Environment
resource "azurerm_container_app_environment" "aurora" {
  name                = "${var.resource_prefix}-aca-env-${var.environment}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location

  log_analytics_workspace_id = azurerm_log_analytics_workspace.aurora.id

  tags = azurerm_resource_group.main.tags
}

# Container App
resource "azurerm_container_app" "aurora" {
  name                         = "${var.resource_prefix}-api-${var.environment}"
  container_app_environment_id = azurerm_container_app_environment.aurora.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.aurora.id]
  }

  registry {
    server   = azurerm_container_registry.aurora.login_server
    identity = azurerm_user_assigned_identity.aurora.id
  }

  ingress {
    allow_insecure_connections = false
    external_enabled           = true
    target_port                = var.container_port
    transport                  = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    container {
      name   = "aurora-api"
      image  = var.container_image_uri != "" ? var.container_image_uri : "${azurerm_container_registry.aurora.login_server}aurora:latest"
      cpu    = var.container_cpu
      memory = "${var.container_memory}Gi"

      port {
        container_port = var.container_port
      }

      env {
        name  = "DATABASE_PATH"
        value = "/mnt/data/aurora.db"
      }

      env {
        name  = "LOG_LEVEL"
        value = var.log_level
      }

      env {
        name  = "ENVIRONMENT"
        value = var.environment
      }

      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = azurerm_application_insights.aurora.connection_string
      }

      env {
        name        = "LLM_API_KEY"
        secret_name = "llm-api-key"
      }

      volume_mounts {
        name = "aurora-data"
        path = "/mnt/data"
      }
    }

    min_replicas = var.min_replicas
    max_replicas = var.max_replicas

    volume {
      name         = "aurora-data"
      storage_name = azurerm_container_app_environment_storage.aurora.name
      storage_type = "AzureFile"
    }
  }

  secret {
    name                    = "llm-api-key"
    key_vault_secret_id     = "${azurerm_key_vault_secret.llm_api_key.id}/versions/${azurerm_key_vault_secret.llm_api_key.version}"
    identity                = azurerm_user_assigned_identity.aurora.id
  }

  tags = azurerm_resource_group.main.tags

  depends_on = [
    azurerm_container_app_environment_storage.aurora,
    azurerm_key_vault_secret.llm_api_key,
    azurerm_role_assignment.acr_pull
  ]
}

# Container App Environment Storage
resource "azurerm_container_app_environment_storage" "aurora" {
  name                         = "aurorastorage"
  container_app_environment_id = azurerm_container_app_environment.aurora.id
  account_name                 = azurerm_storage_account.aurora.name
  share_name                   = azurerm_storage_share.aurora_data.name
  access_key                   = azurerm_storage_account.aurora.primary_access_key
  access_mode                  = "ReadWrite"
}
