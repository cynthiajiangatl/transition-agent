// =============================================================================
// Transition Agent — Azure Container Apps infrastructure
// =============================================================================
// Alternative to main.bicep (App Service), used because this subscription has no
// dedicated App Service VM quota. Provisions:
//   - Log Analytics workspace (Container Apps environment logs)
//   - User-assigned managed identity: pulls the image AND is the app's identity
//     for Cosmos/Storage data-plane access
//   - AcrPull role assignment on the (pre-existing) registry
//   - Container Apps managed environment (consumption)
//   - Container App with external ingress on port 3000
//
// The ACR is NOT provisioned here - create it and push the image first, because
// the Container App resolves the image at creation time.
//
// A USER-assigned identity is used (not system-assigned) because the image pull
// identity must exist and hold AcrPull BEFORE the container app is created.
//
// Cosmos DB and Storage are EXISTING resources referenced by env vars only.
// Grant this identity data-plane access after deployment (see DEPLOYMENT.md).
// =============================================================================

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Base name used to derive resource names (lowercase letters/numbers).')
@minLength(3)
@maxLength(20)
param appName string = 'transitionagent'

@description('Name of an EXISTING Azure Container Registry holding the image. Create it before deploying; this template only references it.')
param acrName string

@description('Container image repository:tag inside the ACR. Must be pushed BEFORE deploying.')
param imageName string = 'transition-agent:latest'

@description('Container Apps environment name. Override when a previous environment of the same name is still deleting.')
param environmentName string = '${appName}-env'

@description('Resource ID of an EXISTING user-assigned managed identity to run as. Leave empty to create a new one per deployment. A stable identity keeps the Cosmos/Storage grants valid across redeploys and teardowns.')
param existingIdentityResourceId string = ''

@description('vCPU allocated to the container.')
param containerCpu string = '0.5'

@description('Memory allocated to the container. Must pair with containerCpu (0.5 vCPU -> 1Gi).')
param containerMemory string = '1Gi'

@description('Web server worker threads per replica (Waitress). Requests are I/O-bound, so this can safely exceed the vCPU count.')
param wsgiThreads string = '16'

@description('Max concurrent live refreshes per replica before excess callers get HTTP 429.')
param maxConcurrentRefreshes string = '8'

@description('Minimum number of replicas kept warm.')
param minReplicas int = 1

@description('Maximum number of replicas the app may scale out to under load.')
param maxReplicas int = 10

@description('KEDA HTTP scale trigger: concurrent requests per replica before another replica is added.')
param httpConcurrentRequests int = 40

// --- Redis + refresh worker (stateless scale-out) --------------------------- #
@description('Redis host for the shared token cache, job status, and refresh queue. Leave empty to run in in-process mode (single web replica, no worker).')
param redisHost string = ''

@description('Redis port (Azure Cache for Redis / Azure Managed Redis use 6380 for TLS).')
param redisPort int = 6380

@description('Connect to Redis over TLS.')
param redisEnableTls bool = true

@description('Redis list name used as the refresh queue.')
param redisQueueName string = 'ta:refresh:queue'

@description('Refresh worker threads per worker replica.')
param workerConcurrency int = 4

@description('Minimum worker replicas. Set 0 to scale to zero when the queue is empty (adds cold-start latency to the first refresh).')
param workerMinReplicas int = 1

@description('Maximum worker replicas under queue load.')
param workerMaxReplicas int = 10

@description('KEDA Redis scaler: queue length per worker replica before scaling out.')
param workerQueueLength int = 5

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

@description('Entra app role (in the ID-token "roles" claim) whose members may administer delegated access. Assign users to this role on the app registration\'s Enterprise Application.')
param adminAppRole string = 'Handover.Admin'

@description('Cosmos container holding delegated-access grants.')
param cosmosGrantsContainer string = 'handover-grants'

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

@description('Log Analytics retention (days).')
param logRetentionDays int = 30

// --- secrets (pass at deploy time; never commit) ---------------------------- #
@description('Entra app client secret.')
@secure()
param aadClientSecret string

@description('Flask session signing key (stable random hex).')
@secure()
param secretKey string

@description('Redis access key / password. Required only when redisHost is set.')
@secure()
param redisPassword string = ''

// --------------------------------------------------------------------------- //
var workspaceName = '${appName}-logs'
var identityName = '${appName}-id'
var acrPullRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d') // AcrPull

// The registry is REFERENCED, never managed here. The Container App needs the image
// to exist before it is created, so a template that also owned the registry could
// reset it during deployment and destroy the very image it is about to pull.
// Create it out-of-band first: az acr create -n <name> -g <rg> --sku Basic
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: acrName
}

// A new identity gets a NEW principal id on every deployment, so the Cosmos/Storage
// grants must be re-issued each time. Pass existingIdentityResourceId to run as a
// long-lived identity instead and grant those roles once.
var useExistingIdentity = !empty(existingIdentityResourceId)

resource newIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = if (!useExistingIdentity) {
  name: identityName
  location: location
}

resource byoIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = if (useExistingIdentity) {
  name: last(split(existingIdentityResourceId, '/'))
  scope: resourceGroup(split(existingIdentityResourceId, '/')[2], split(existingIdentityResourceId, '/')[4])
}

var identityResourceId = useExistingIdentity ? existingIdentityResourceId : newIdentity.id
var identityPrincipalId = useExistingIdentity ? byoIdentity.properties.principalId : newIdentity.properties.principalId
var identityClientId = useExistingIdentity ? byoIdentity.properties.clientId : newIdentity.properties.clientId

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, identityResourceId, acrPullRoleId)
  scope: acr
  properties: {
    principalId: identityPrincipalId
    roleDefinitionId: acrPullRoleId
    principalType: 'ServicePrincipal'
  }
}

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: logRetentionDays
  }
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: workspace.properties.customerId
        sharedKey: workspace.listKeys().primarySharedKey
      }
    }
  }
}

// The ingress hostname is derived from the environment's default domain, which is
// known before the app exists — so the app can be told its own redirect URI here.
var appFqdn = '${appName}.${environment.properties.defaultDomain}'
var appUrl = 'https://${appFqdn}'

var baseEnv = [
  { name: 'HOST', value: '0.0.0.0' }
  { name: 'PORT', value: '3000' }
  { name: 'WSGI_THREADS', value: wsgiThreads }
  { name: 'MAX_CONCURRENT_REFRESHES', value: maxConcurrentRefreshes }
  { name: 'TRUST_PROXY', value: 'true' }
  { name: 'ENABLE_REFRESH', value: 'true' }
  { name: 'AUTH_ENABLED', value: 'true' }
  { name: 'LOG_LEVEL', value: logLevel }
  { name: 'GRAPH_DETECT_PII', value: graphDetectPii }
  { name: 'WORKIQ_ENABLED', value: workiqEnabled }
  { name: 'WORKIQ_REST_ENDPOINT', value: workiqRestEndpoint }
  { name: 'WORKIQ_SCOPE', value: workiqScope }
  { name: 'COSMOS_ENDPOINT', value: cosmosEndpoint }
  { name: 'COSMOS_DATABASE', value: cosmosDatabase }
  { name: 'COSMOS_CONTAINER', value: cosmosContainer }
  { name: 'COSMOS_GRANTS_CONTAINER', value: cosmosGrantsContainer }
  { name: 'COSMOS_USE_MANAGED_IDENTITY', value: 'true' }
  { name: 'COSMOS_MANAGED_IDENTITY_CLIENT_ID', value: identityClientId }
  { name: 'AZURE_STORAGE_ACCOUNT_URL', value: storageAccountUrl }
  { name: 'AZURE_BACKUP_CONTAINER', value: backupContainer }
  { name: 'AZURE_STORAGE_USE_MANAGED_IDENTITY', value: 'true' }
  { name: 'AZURE_STORAGE_MANAGED_IDENTITY_CLIENT_ID', value: identityClientId }
  { name: 'AAD_CLIENT_ID', value: aadClientId }
  { name: 'AAD_TENANT_ID', value: aadTenantId }
  { name: 'AAD_LOGIN_SCOPES', value: aadLoginScopes }
  { name: 'ADMIN_APP_ROLE', value: adminAppRole }
  { name: 'AAD_REDIRECT_URI', value: '${appUrl}/auth/callback' }
  { name: 'AAD_POST_LOGOUT_REDIRECT', value: '${appUrl}/login' }
  { name: 'AAD_CLIENT_SECRET', secretRef: 'aad-client-secret' }
  { name: 'SECRET_KEY', secretRef: 'app-secret-key' }
]
var limitEnv = [for limit in items(sectionLimits): { name: limit.key, value: limit.value }]

// When Redis is configured the web tier is stateless and a worker pool runs
// refreshes; otherwise the app keeps state in-process and refreshes on a thread.
var useRedis = !empty(redisHost)
var redisEnv = useRedis ? [
  { name: 'REDIS_HOST', value: redisHost }
  { name: 'REDIS_PORT', value: string(redisPort) }
  { name: 'REDIS_SSL', value: string(redisEnableTls) }
  { name: 'REDIS_QUEUE', value: redisQueueName }
  { name: 'REDIS_PASSWORD', secretRef: 'redis-password' }
] : []
var appSecrets = concat([
  { name: 'aad-client-secret', value: aadClientSecret }
  { name: 'app-secret-key', value: secretKey }
], useRedis ? [ { name: 'redis-password', value: redisPassword } ] : [])

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityResourceId}': {}
    }
  }
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 3000
        transport: 'auto'
        allowInsecure: false
        // Pin each client to one replica so the in-process MSAL token cache and
        // that user's refresh state stay consistent across their requests.
        stickySessions: {
          affinity: 'sticky'
        }
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: identityResourceId
        }
      ]
      secrets: appSecrets
    }
    template: {
      containers: [
        {
          name: 'transition-agent'
          image: '${acr.properties.loginServer}/${imageName}'
          resources: {
            cpu: json(containerCpu)
            memory: containerMemory
          }
          env: concat(baseEnv, limitEnv, redisEnv)
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 3000 }
              initialDelaySeconds: 20
              periodSeconds: 30
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: { path: '/healthz', port: 3000 }
              initialDelaySeconds: 5
              periodSeconds: 10
              failureThreshold: 3
            }
          ]
        }
      ]
      // Horizontal autoscale on HTTP concurrency. app.py keeps the MSAL token
      // cache and each user's refresh state in process memory, so sticky sessions
      // (set on ingress above) keep a user on one replica while new users spread
      // across replicas. A replica restart drops that user's in-memory session
      // (they re-authenticate / re-run a refresh).
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                concurrentRequests: string(httpConcurrentRequests)
              }
            }
          }
        ]
      }
    }
  }
  dependsOn: [
    acrPull
  ]
}

// Refresh worker: same image, run with `python worker.py`, no ingress. It drains
// the Redis refresh queue and autoscales on queue length (KEDA), independently of
// the web tier. Deployed only when Redis is configured.
resource workerApp 'Microsoft.App/containerApps@2024-03-01' = if (useRedis) {
  name: '${appName}-worker'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityResourceId}': {}
    }
  }
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: acr.properties.loginServer
          identity: identityResourceId
        }
      ]
      secrets: appSecrets
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: '${acr.properties.loginServer}/${imageName}'
          resources: {
            cpu: json(containerCpu)
            memory: containerMemory
          }
          command: [ 'python', 'worker.py' ]
          env: concat(baseEnv, limitEnv, redisEnv, [
            { name: 'WORKER_CONCURRENCY', value: string(workerConcurrency) }
          ])
        }
      ]
      scale: {
        minReplicas: workerMinReplicas
        maxReplicas: workerMaxReplicas
        rules: [
          {
            name: 'redis-queue-depth'
            custom: {
              type: 'redis'
              metadata: {
                address: '${redisHost}:${redisPort}'
                listName: redisQueueName
                listLength: string(workerQueueLength)
                enableTLS: string(redisEnableTls)
                databaseIndex: '0'
              }
              auth: [
                {
                  secretRef: 'redis-password'
                  triggerParameter: 'password'
                }
              ]
            }
          }
        ]
      }
    }
  }
  dependsOn: [
    acrPull
  ]
}

output workerAppName string = useRedis ? workerApp.name : ''
output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output containerAppName string = containerApp.name
output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output appUrl string = appUrl
output redirectUri string = '${appUrl}/auth/callback'
output identityPrincipalId string = identityPrincipalId
output identityClientId string = identityClientId
output environmentName string = environment.name
