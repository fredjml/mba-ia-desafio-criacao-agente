variable "resource_prefix" {
  description = "Prefix for all resource names"
  type        = string
  default     = "aurora"

  validation {
    condition     = can(regex("^[a-z0-9-]{1,20}$", var.resource_prefix))
    error_message = "Resource prefix must be lowercase alphanumeric with hyphens, max 20 characters."
  }
}

variable "location" {
  description = "Azure region for deployment"
  type        = string
  default     = "eastus"

  validation {
    condition     = contains(["eastus", "eastus2", "westus", "westus2", "centralus", "northcentralus", "southcentralus", "westcentralus", "canadacentral", "brazilsouth", "northeurope", "westeurope", "germanywestcentral", "switzerlandnorth", "uksouth", "ukwest"], var.location)
    error_message = "Location must be a valid Azure region."
  }
}

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be dev, staging, or prod."
  }
}

variable "container_image_uri" {
  description = "Full URI of the container image to deploy"
  type        = string
  default     = ""
}

variable "container_cpu" {
  description = "CPU cores for the container (must be 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0)"
  type        = string
  default     = "1.0"

  validation {
    condition     = contains(["0.25", "0.5", "0.75", "1.0", "1.25", "1.5", "1.75", "2.0"], var.container_cpu)
    error_message = "Container CPU must be one of: 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0"
  }
}

variable "container_memory" {
  description = "Memory in GiB for the container (must be between 0.5 and 4.0)"
  type        = string
  default     = "2.0"

  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]+)?$", var.container_memory)) && tonumber(var.container_memory) >= 0.5 && tonumber(var.container_memory) <= 4.0
    error_message = "Container memory must be between 0.5 and 4.0 GiB."
  }
}

variable "container_port" {
  description = "Container port number"
  type        = number
  default     = 8000

  validation {
    condition     = var.container_port > 0 && var.container_port < 65536
    error_message = "Container port must be between 1 and 65535."
  }
}

variable "min_replicas" {
  description = "Minimum number of container replicas"
  type        = number
  default     = 1

  validation {
    condition     = var.min_replicas >= 1 && var.min_replicas <= 30
    error_message = "Min replicas must be between 1 and 30."
  }
}

variable "max_replicas" {
  description = "Maximum number of container replicas"
  type        = number
  default     = 3

  validation {
    condition     = var.max_replicas >= 1 && var.max_replicas <= 30
    error_message = "Max replicas must be between 1 and 30."
  }
}

variable "log_level" {
  description = "Application log level (DEBUG, INFO, WARNING, ERROR)"
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR"], var.log_level)
    error_message = "Log level must be DEBUG, INFO, WARNING, or ERROR."
  }
}

variable "llm_api_key" {
  description = "LLM API key (stored in Key Vault)"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_LLM_API_KEY"
}
