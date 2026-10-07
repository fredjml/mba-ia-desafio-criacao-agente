// Aurora Virtual Assistant - Bicep Parameters
// v1.0.0-production

@description('Resource naming prefix')
param resourcePrefix string = 'aurora'

@description('Azure region')
param location string = 'eastus'

@description('Environment (dev, staging, prod)')
param environment string = 'prod'

@description('Container image repository URI')
param containerImageUri string = 'aurora:latest'

@description('Container CPU allocation')
param containerCpu string = '1.0'

@description('Container memory allocation (Gi)')
param containerMemory string = '2.0'

@description('Container port number')
param containerPort int = 8000

@description('Minimum number of replicas')
param minReplicas int = 1

@description('Maximum number of replicas')
param maxReplicas int = 3

@description('Application log level')
param logLevel string = 'INFO'
