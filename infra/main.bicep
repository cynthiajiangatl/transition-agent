// =============================================================================
// Transition Agent — Azure App Service (Linux container) infrastructure
// =============================================================================
// Provisions:
//   - Azure Container Registry (ACR) to hold the app image
//   - Linux App Service Plan
//   - Web App for Containers with a SYSTEM-ASSIGNED managed identity
//   - AcrPull role assignment so the web app pulls its image via managed identity
//   - App settings (environment variables) for the Flask/Waitress app
//
// Cosmos DB and Storage are EXISTING resources and are referenced by app
// settings only. Grant the web app's managed identity data-plane access to them
// AFTER deployment (see DEPLOYMENT.md) — those grants are cross-resource and are
// scripted separately so this template stays deployable on its own.
// =============================================================================

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Base name used to derive resource names (lowercase letters/numbers).')
@minLength(3)
@maxLength(20)
param appName string = 'transitionagent'

@description('Globally-unique ACR name (alphanumeric only). Defaults to a stable hash.')
param acrName string = 'acr${uniqueString(resourceGroup().id, appName)}'

@description('App Service Plan SKU. B1 for dev; P1v3 or higher for production.')
@allowed([ 'B1', 'B2', 'P0v3', 'P1v3', 'P2v3' ])
param appServicePlanSku string = 'B1'

@description('Container image repository:tag inside the ACR.')
param imageName string = 'transition-agent:latest'

// --- application configuration (non-secret) --------------------------------- #
@description('Attempt the Work IQ collector. Set false for tenants without Work IQ (Graph-only).')
param workiqEnabled string = 'true'

@description('Work IQ REST endpoint.')
param workiqRestEndpoint string = 'https://workiq.svc.cloud.microsoft/rest'

@description('Work IQ delegated scope.')
param workiqScope string = 'fdcc1f02-fc51-4226-8753-f668596af7f7/WorkIQAgent.Ask'

@description('Existing Cosmos DB account endpoint.')
param cosmosEndpoint string = 'https://caigcosmoscj1.documents.azure.com:443/'

@description('Cosmos database name.')
param cosmosDatabase string = 'transition-agent'

@description('Cosmos container name.')
param cosmosContainer string = 'handovers'

@description('Existing Storage account blob endpoint for file backup.')
#disable-next-line no-hardcoded-env-urls
param storageAccountUrl string = 'https://transitionagentstorecj1.blob.core.windows.net'

@description('Blob container for backups.')
param backupContainer string = 'handover-backups'

@description('Entra ID app registration (confidential client) id.')
param aadClientId string = '00815db6-c56b-4e2e-a023-9ae44dd7f08b'

@description('Entra ID tenant id for sign-in.')
param aadTenantId string = subscription().tenantId

@description('Delegated Graph/Work IQ scopes requested at sign-in.')
param aadLoginScopes string = 'User.Read User.Read.All People.Read Files.Read.All Sites.Read.All Calendars.Read Mail.Read Tasks.Read Team.ReadBasic.All GroupMember.Read.All'

@description('Enable Purview sensitivity-label / PII detection on Graph files.')
param graphDetectPii string = 'true'

@description('Maximum rows kept per section of the brief, applied by both the Work IQ and Graph collectors. Keys omitted here fall back to the defaults in config.py.')
param sectionLimits object = {
  MAX_CONTACTS: '20'
  MAX_PROJECTS: '20'
  MAX_IMPORTANT_FILES: '20'
  MAX_RECURRING_PROCESSES: '20'
  MAX_OUTSTANDING_ITEMS: '20'
  MAX_ACCESS_TRANSFERS: '20'
}

@description('Log verbosity.')
param logLevel string = 'INFO'

// --- secrets (pass at deploy time; never commit) ---------------------------- #
@description('Entra app client secret.')
@secure()
param aadClientSecret string

@description('Flask session signing key (stable random hex).')
@secure()
param secretKey string

// --- data-plane auth mode --------------------------------------------------- #
@description('Use keys instead of managed identity for Cosmos/Storage (required if they are in a different tenant).')
param useKeyBasedDataAccess bool = false

@description('Cosmos account key (only when useKeyBasedDataAccess = true).')
@secure()
param cosmosKey string = ''

@description('Storage connection string (only when useKeyBasedDataAccess = true).')
@secure()
param storageConnectionString string = ''

// --------------------------------------------------------------------------- //
var planName = '${appName}-plan'
var webAppName = '${appName}-${uniqueString(resourceGroup().id)}'
var acrPullRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d') // AcrPull

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false // pull via managed identity, not admin creds
  }
}

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: planName
  location: location
  sku: { name: appServicePlanSku }
  kind: 'linux'
  properties: {
    reserved: true // required for Linux
  }
}

resource webApp 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  kind: 'app,linux,container'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'DOCKER|${acr.properties.loginServer}/${imageName}'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      http20Enabled: true
      healthCheckPath: '/healthz'
      acrUseManagedIdentityCreds: true // pull image with the system-assigned identity
    }
  }
}

// App settings kept in one config resource, built via union() so key-based mode
// can swap the Cosmos/Storage auth settings without duplicating the whole map.
var baseSettings = {
  WEBSITES_PORT: '3000'
  WEBSITES_ENABLE_APP_SERVICE_STORAGE: 'false'
  DOCKER_REGISTRY_SERVER_URL: 'https://${acr.properties.loginServer}'
  HOST: '0.0.0.0'
  PORT: '3000'
  WSGI_THREADS: '16'
  MAX_CONCURRENT_REFRESHES: '8'
  TRUST_PROXY: 'true'
  ENABLE_REFRESH: 'true'
  AUTH_ENABLED: 'true'
  LOG_LEVEL: logLevel
  GRAPH_DETECT_PII: graphDetectPii
  WORKIQ_ENABLED: workiqEnabled
  WORKIQ_REST_ENDPOINT: workiqRestEndpoint
  WORKIQ_SCOPE: workiqScope
  COSMOS_ENDPOINT: cosmosEndpoint
  COSMOS_DATABASE: cosmosDatabase
  COSMOS_CONTAINER: cosmosContainer
  AZURE_STORAGE_ACCOUNT_URL: storageAccountUrl
  AZURE_BACKUP_CONTAINER: backupContainer
  AAD_CLIENT_ID: aadClientId
  AAD_CLIENT_SECRET: aadClientSecret
  AAD_TENANT_ID: aadTenantId
  AAD_LOGIN_SCOPES: aadLoginScopes
  AAD_REDIRECT_URI: 'https://${webApp.properties.defaultHostName}/auth/callback'
  AAD_POST_LOGOUT_REDIRECT: 'https://${webApp.properties.defaultHostName}/login'
  SECRET_KEY: secretKey
}
var miDataSettings = {
  COSMOS_USE_MANAGED_IDENTITY: 'true'
  AZURE_STORAGE_USE_MANAGED_IDENTITY: 'true'
}
var keyDataSettings = {
  COSMOS_USE_MANAGED_IDENTITY: 'false'
  COSMOS_KEY: cosmosKey
  AZURE_STORAGE_USE_MANAGED_IDENTITY: 'false'
  AZURE_STORAGE_CONNECTION_STRING: storageConnectionString
}

resource appSettings 'Microsoft.Web/sites/config@2023-12-01' = {
  parent: webApp
  name: 'appsettings'
  properties: union(baseSettings, sectionLimits, useKeyBasedDataAccess ? keyDataSettings : miDataSettings)
}

// Let the web app's managed identity pull images from the ACR.
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, webApp.id, acrPullRoleId)
  scope: acr
  properties: {
    principalId: webApp.identity.principalId
    roleDefinitionId: acrPullRoleId
    principalType: 'ServicePrincipal'
  }
}

output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output webAppName string = webApp.name
output webAppHostName string = webApp.properties.defaultHostName
output webAppUrl string = 'https://${webApp.properties.defaultHostName}'
output webAppPrincipalId string = webApp.identity.principalId
output redirectUri string = 'https://${webApp.properties.defaultHostName}/auth/callback'
