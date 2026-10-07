// Aurora Virtual Assistant - Azure Infrastructure (Bicep)
// v1.0.0-production
// Components: ACR, ACA Environment, Container App, Key Vault, Storage, Managed Identity

metadata description = 'Aurora Virtual Assistant v1.0.0 Azure Infrastructure'

@minLength(1)
@maxLength(64)
@description('Name prefix for all resources')
param resourcePrefix string = 'aurora'

@description('Azure region for deployment')
param location string = resourceGroup().location

@description('Environment name (dev, staging, prod)')
param environment string = 'prod'

@description('Container image URI')
param containerImageUri string = ''

@description('CPU cores for container app')
param containerCpu string = '1.0'

@description('Memory for container app (Gi)')
param containerMemory string = '2.0'

@description('Container port')
param containerPort int = 8000

@description('Minimum replicas')
param minReplicas int = 1

@description('Maximum replicas')
param maxReplicas int = 3

@description('Application log level (DEBUG, INFO, WARNING, ERROR)')
param logLevel string = 'INFO'

// Variables
var uniqueSuffix = uniqueString(resourceGroup().id)
var acrName = '${replace(resourcePrefix, '-', '')}acr${uniqueSuffix}'
var acaEnvName = '${resourcePrefix}-aca-env-${environment}'
var containerAppName = '${resourcePrefix}-api-${environment}'
var keyVaultName = '${resourcePrefix}-kv-${uniqueSuffix}'
var storageAccountName = '${replace(resourcePrefix, '-', '')}st${uniqueSuffix}'
var managedIdentityName = '${resourcePrefix}-mi-${environment}'
var appInsightsName = '${resourcePrefix}-ai-${environment}'
var logAnalyticsName = '${resourcePrefix}-la-${environment}'

// Managed Identity
resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: managedIdentityName
  location: location
}

// Log Analytics
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
  }
}

// Application Insights
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// Container Registry
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: {
    name: 'Standard'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
    anonymousPullEnabled: false
  }
}

// Storage Account (for persistent SQLite)
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

// Storage file share (for database persistence)
resource fileShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-01-01' = {
  name: '${storageAccount.name}/default/aurora-data'
  properties: {
    accessTier: 'TransactionOptimized'
    shareQuota: 10
  }
}

// Key Vault
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    accessPolicies: [
      {
        tenantId: subscription().tenantId
        objectId: managedIdentity.properties.principalId
        permissions: {
          secrets: ['get', 'list']
          certificates: ['get', 'list']
        }
      }
    ]
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
  }
}

// Key Vault secret for LLM API key (placeholder)
resource llmApiKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'llm-api-key'
  properties: {
    value: 'PLACEHOLDER_LLM_API_KEY'
  }
}

// Azure Container Apps Environment
resource acaEnvironment 'Microsoft.App/managedEnvironments@2023-11-02-preview' = {
  name: acaEnvName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// Container App
resource containerApp 'Microsoft.App/containerApps@2023-11-02-preview' = {
  name: containerAppName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: acaEnvironment.id
    configuration: {
      ingress: {
        external: true
        targetPort: containerPort
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: managedIdentity.id
        }
      ]
      secrets: [
        {
          name: 'llm-api-key'
          keyVaultUrl: '${keyVault.properties.vaultUri}secrets/llm-api-key'
          identity: managedIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'aurora-api'
          image: !empty(containerImageUri) ? containerImageUri : '${acr.properties.loginServer}aurora:latest'
          resources: {
            cpu: json(containerCpu)
            memory: '${containerMemory}Gi'
          }
          ports: [
            {
              containerPort: containerPort
              protocol: 'TCP'
            }
          ]
          env: [
            {
              name: 'DATABASE_PATH'
              value: '/mnt/data/aurora.db'
            }
            {
              name: 'LOG_LEVEL'
              value: logLevel
            }
            {
              name: 'ENVIRONMENT'
              value: environment
            }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: appInsights.properties.ConnectionString
            }
            {
              name: 'LLM_API_KEY'
              secretRef: 'llm-api-key'
            }
          ]
          volumeMounts: [
            {
              mountPath: '/mnt/data'
              volumeName: 'aurora-storage'
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-scaling'
            http: {
              metadata: {
                concurrentRequests: '100'
              }
            }
          }
        ]
      }
      volumes: [
        {
          name: 'aurora-storage'
          storageName: 'aurora-data'
          storageType: 'AzureFile'
        }
      ]
    }
  }
}

// Grant ACR pull permission to Managed Identity
resource acrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, managedIdentity.id, 'acrpull')
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Outputs
output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output containerRegistryLoginServer string = acr.properties.loginServer
output keyVaultUri string = keyVault.properties.vaultUri
output managedIdentityId string = managedIdentity.id
output managedIdentityClientId string = managedIdentity.properties.clientId
output appInsightsInstrumentationKey string = appInsights.properties.InstrumentationKey
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output storageAccountName string = storageAccount.name
output storageAccountEndpoint string = storageAccount.properties.primaryEndpoints.file
