# Deploying Transition Agent to Azure

The app ships as a **container** and can run on either of two Azure hosts. The image is
built server-side in **Azure Container Registry (ACR)** via `az acr build` in both cases —
no local Docker required.

| Path | Host | IaC | When to use |
|---|---|---|---|
| **A** | **Azure Container Apps** | [`infra/aca.bicep`](infra/aca.bicep) | Consumption-based; needs **no App Service VM quota**. Use this on sponsored / MngEnvMCAP subscriptions where dedicated plans are blocked |
| **B** | **Azure App Service** (Linux container) | [`infra/main.bicep`](infra/main.bicep) | Requires dedicated **App Service VM quota** (B1+). Fails with `Unauthorized … Current Limit (Total VMs): 0` when the subscription has none |

> Path B was attempted first on subscription `103e8828…` and failed on quota, so **Path A
> is the deployed configuration**. Both templates are kept — switch by choosing a template.

**Path A provisions** (`infra/aca.bicep`): ACR (Basic) · Log Analytics workspace ·
**user-assigned managed identity** · `AcrPull` role assignment · Container Apps managed
environment · Container App with external ingress on port 3000, secrets, and all env vars.

**Path B provisions** (`infra/main.bicep`): ACR (Basic) · Linux App Service Plan · Web App
for Containers with a **system-assigned managed identity** · `AcrPull` role assignment ·
all application settings.

Two Path A design points worth knowing:

- The identity is **user-assigned**, not system-assigned, because on Container Apps the
  image-pull identity must already hold `AcrPull` when the app is created. The same
  identity is also the app's Cosmos/Storage identity, via
  `COSMOS_MANAGED_IDENTITY_CLIENT_ID` / `AZURE_STORAGE_MANAGED_IDENTITY_CLIENT_ID`.
- The web app **autoscales** on HTTP concurrency (`minReplicas` / `maxReplicas`, default
  1–10; `httpConcurrentRequests`). It runs in one of two modes:
  - **In-process (default).** The per-user token cache and refresh state live in memory, so
    the Container App enables **sticky sessions** to keep each user pinned to one replica; a
    background thread runs the refresh. Good to a handful of replicas with no extra infra.
  - **Stateless (Redis).** Set the `redisHost` + `redisPassword` parameters: the MSAL cache,
    refresh status, and the refresh queue move to Redis, and the template also deploys a
    `<appName>-worker` Container App (`python worker.py`) that drains the queue and
    autoscales on its depth (KEDA). This removes the need for sticky sessions and lets the
    web tier scale freely. See [A3.1](#a31-optional-stateless-scale-out-with-redis--a-worker).

---

## Path A — Azure Container Apps

Prerequisites are the same as [§1 Prerequisites](#1-prerequisites) below, **except** no App
Service quota is needed. Register the providers once per subscription:

```powershell
az provider register -n Microsoft.App --wait
az provider register -n Microsoft.OperationalInsights --wait
```

### A1. Resource group and registry

The container image must exist **before** the Container App is created, so the registry
comes first. ACR names are globally unique:

```powershell
$RG   = "rg-transition-agent"
$ACR  = "acrtransitionagent$(Get-Random -Maximum 9999)"
az group create --name $RG --location eastus2
az acr create -n $ACR -g $RG --sku Basic
```

### A2. Build the image

```powershell
chcp 65001 > $null        # az acr build's log streamer crashes on cp1252 consoles
az acr build --registry $ACR --image transition-agent:latest .
```

> If the CLI still dies with `UnicodeEncodeError: 'charmap' codec can't encode…`, that is a
> **client-side log-rendering bug only** — the build continues server-side. Check it with
> `az acr task list-runs -r $ACR --top 1 -o table` and wait for `Succeeded`.

### A3. Deploy the Container Apps stack

```powershell
# 256-bit key. Do NOT use `Get-Random -Count 64` over a 16-char alphabet: it draws
# DISTINCT items, so it silently caps at 16 chars (~44 bits) instead of 64.
$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$SECRET_KEY = -join ($bytes | ForEach-Object { $_.ToString('x2') })
$CLIENT_SECRET = Read-Host "Entra app client secret" -AsSecureString
$CLIENT_SECRET_PLAIN = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
  [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($CLIENT_SECRET))

az deployment group create -g $RG --name aca `
  --template-file infra/aca.bicep `
  --parameters infra/aca.parameters.json `
  --parameters acrName=$ACR aadClientSecret=$CLIENT_SECRET_PLAIN secretKey=$SECRET_KEY
```

Save `$SECRET_KEY` — a new value on redeploy signs every user out.

### A3.1 (Optional) Stateless scale-out with Redis + a worker

By default the app runs **in-process** (sticky sessions, background-thread refresh). To make
the web tier fully stateless and run refreshes on an autoscaling worker pool, provision a
Redis and pass its host + access key. The template then also deploys a `<appName>-worker`
Container App that drains the refresh queue and scales on its depth.

```powershell
# Azure Cache for Redis (or Azure Managed Redis). Capture host + primary key:
$REDIS = "transitionagent-redis"
az redis create -n $REDIS -g $RG -l eastus2 --sku Basic --vm-size c0
$REDIS_HOST = az redis show -n $REDIS -g $RG --query hostName -o tsv
$REDIS_KEY  = az redis list-keys -n $REDIS -g $RG --query primaryKey -o tsv

az deployment group create -g $RG --name aca `
  --template-file infra/aca.bicep `
  --parameters infra/aca.parameters.json `
  --parameters acrName=$ACR aadClientSecret=$CLIENT_SECRET_PLAIN secretKey=$SECRET_KEY `
  --parameters redisHost=$REDIS_HOST redisPassword=$REDIS_KEY
```

Optional worker/scale knobs (all have defaults): `workerConcurrency` (threads per worker, 4),
`workerMinReplicas` (set `0` to scale to zero when idle), `workerMaxReplicas` (10),
`workerQueueLength` (queue depth per replica before scaling out, 5), plus the web tier's
`minReplicas` / `maxReplicas` / `httpConcurrentRequests`. The worker shares the same managed
identity as the web app, so the Cosmos/Storage grants in A5 already cover it. Verify it came
up with `az containerapp list -g $RG -o table` (you'll see `<appName>-worker`) and
`az containerapp logs show -n $($APPNAME)-worker -g $RG --follow`.

**Reuse a long-lived identity (recommended).** By default the template creates a new
user-assigned identity per deployment, which gets a **new principal id** — so the Cosmos and
Storage grants in A5 must be re-issued after every redeploy, and deleting the app orphans
the old assignments. Pass an existing identity instead and grant those roles once:

```powershell
# create once, in a resource group that survives app teardowns
az group create -n rg-transition-agent-shared -l eastus2
$IDENTITY = az identity create -n transitionagent-shared-id -g rg-transition-agent-shared --query id -o tsv

# then on every deployment
  --parameters existingIdentityResourceId=$IDENTITY
```

The template still creates the `AcrPull` assignment for it, so image pulls keep working. The
tradeoff is blast radius: every workload sharing that identity inherits its access to the
briefs in Cosmos and the backup container.

### A4. Capture the outputs

```powershell
$OUT = az deployment group show -g $RG -n aca --query properties.outputs -o json | ConvertFrom-Json
$APPURL    = $OUT.appUrl.value
$REDIRECT  = $OUT.redirectUri.value
$PRINCIPAL = $OUT.identityPrincipalId.value
$CAPP      = $OUT.containerAppName.value
"URL=$APPURL  APP=$CAPP"
```

### A5. Redirect URI and data-plane RBAC

Identical to Path B — run [§6](#6-add-the-redirect-uri-to-the-entra-app) and
[§7](#7-grant-the-managed-identity-data-plane-access-same-tenant-mi-mode) using the
`$PRINCIPAL` captured above (it is the **user-assigned** identity's principal id).

### A6. Verify

```powershell
Invoke-RestMethod "$APPURL/healthz"
az containerapp logs show -n $CAPP -g $RG --follow
```

### A7. Redeploy and tear down

```powershell
# new code -> rebuild the image, then roll a new revision
az acr build --registry $ACR --image transition-agent:latest .
az containerapp revision restart -n $CAPP -g $RG --revision (az containerapp show -n $CAPP -g $RG --query properties.latestRevisionName -o tsv)

# change an env var without redeploying the template
az containerapp update -n $CAPP -g $RG --set-env-vars MAX_PROJECTS=25

az group delete --name $RG --yes --no-wait
```

---

## Path B — Azure App Service (Linux container)

Requires dedicated App Service VM quota. Infrastructure is defined in
[`infra/main.bicep`](infra/main.bicep).

> **Scaling note.** `main.bicep` provisions the **web tier** only, and it runs in in-process
> mode by default (scale the plan / instance count for more web capacity). To use the
> stateless Redis mode on App Service, set the `REDIS_*` app settings to a Redis instance
> **and** run the refresh **worker** (`python worker.py`) as a separate host — e.g. a second
> container / App Service or a WebJob — since App Service doesn't autoscale on queue depth
> the way the ACA worker does. The Container Apps path (A) ships the worker and its
> queue-based autoscaling out of the box.

---

## 1. Prerequisites

- **Azure CLI** signed in to the target tenant/subscription:
  ```powershell
  az login --tenant 7ec2f66d-b467-4c02-a8c6-4c20748961c5
  az account set --subscription 103e8828-c726-4179-8c6a-d4950310ab64
  ```
- Permission to create resources and **assign roles** (Owner or User Access Administrator) in the subscription.
- The existing **Entra app registration** (`00815db6-c56b-4e2e-a023-9ae44dd7f08b`) — you'll add a redirect URI in step 6.
- The Cosmos **database and container** already created (`transition-agent` / `handovers`, partition key `/id`). In managed-identity mode the app holds data-plane rights only, so it cannot create them itself:
  ```powershell
  az cosmosdb sql database create -a caigcosmoscj1 -g <cosmos-rg> -n transition-agent
  az cosmosdb sql container create -a caigcosmoscj1 -g <cosmos-rg> -d transition-agent -n handovers --partition-key-path "/id"
  ```

> **⚠️ Data-plane tenancy check.** The web app's managed identity lives in the App Service's
> tenant (`7ec2f66d…`). Managed-identity access to Cosmos/Storage only works if those
> accounts are in the **same tenant**. If `caigcosmoscj1` / `transitionagentstorecj1` are in a
> different tenant, either (a) recreate them in this subscription, or (b) deploy with
> `useKeyBasedDataAccess=true` and supply `cosmosKey` + `storageConnectionString` (see step 8).

---

## 2. Set variables

```powershell
$RG        = "rg-transition-agent"
$LOCATION  = "eastus2"        # same region as Cosmos (caigcosmoscj1-eastus2)
$APPNAME   = "transitionagent"
```

## 3. Create the resource group

```powershell
az group create --name $RG --location $LOCATION
```

## 4. Deploy the infrastructure

Pass the two secrets at deploy time (never commit them). Generate a stable `SECRET_KEY`:

```powershell
$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$SECRET_KEY = -join ($bytes | ForEach-Object { $_.ToString('x2') })
$CLIENT_SECRET = Read-Host "Entra app client secret" -AsSecureString
$CLIENT_SECRET_PLAIN = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
  [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($CLIENT_SECRET))

az deployment group create `
  --resource-group $RG `
  --template-file infra/main.bicep `
  --parameters infra/main.parameters.json `
  --parameters aadClientSecret=$CLIENT_SECRET_PLAIN secretKey=$SECRET_KEY
```

Capture the outputs (ACR name, web app name, URL, redirect URI, managed-identity principal id):

```powershell
$OUT = az deployment group show -g $RG -n main --query properties.outputs -o json | ConvertFrom-Json
$ACR       = $OUT.acrName.value
$WEBAPP    = $OUT.webAppName.value
$APPURL    = $OUT.webAppUrl.value
$REDIRECT  = $OUT.redirectUri.value
$PRINCIPAL = $OUT.webAppPrincipalId.value
"ACR=$ACR  WEBAPP=$WEBAPP  URL=$APPURL"
```

## 5. Build & push the image with ACR (no local Docker)

```powershell
az acr build --registry $ACR --image transition-agent:latest .
az webapp restart --name $WEBAPP --resource-group $RG
```

`az acr build` uploads the build context, builds the Dockerfile in the cloud, and pushes
`transition-agent:latest` — which is exactly the tag the web app is configured to pull.

## 6. Add the redirect URI to the Entra app

Sign-in fails until the deployed HTTPS URL is a registered redirect URI:

```powershell
$APPID = "00815db6-c56b-4e2e-a023-9ae44dd7f08b"
# @(...) forces an array: with a single existing URI, ConvertFrom-Json returns a STRING
# and "+" would concatenate every URI into one malformed entry, dropping the originals.
$existing = @(az ad app show --id $APPID --query "web.redirectUris" -o json | ConvertFrom-Json)
$updated  = @($existing + $REDIRECT + "$APPURL/login") | Select-Object -Unique
az ad app update --id $APPID --web-redirect-uris $updated
az ad app show --id $APPID --query "web.redirectUris" -o json   # verify
```

## 7. Grant the managed identity data-plane access (same-tenant, MI mode)

**Storage — Blob Data Contributor:**
```powershell
$STORAGE_ID = az storage account show -n transitionagentstorecj1 --query id -o tsv
az role assignment create --assignee-object-id $PRINCIPAL --assignee-principal-type ServicePrincipal `
  --role "Storage Blob Data Contributor" --scope $STORAGE_ID
```

**Cosmos — Built-in Data Contributor (data-plane SQL role):**
```powershell
$COSMOS = "caigcosmoscj1"; $COSMOS_RG = "<cosmos-resource-group>"
$COSMOS_ID = az cosmosdb show -n $COSMOS -g $COSMOS_RG --query id -o tsv
# 00000000-0000-0000-0000-000000000002 = Cosmos DB Built-in Data Contributor
az cosmosdb sql role assignment create --account-name $COSMOS -g $COSMOS_RG `
  --role-definition-id "00000000-0000-0000-0000-000000000002" `
  --principal-id $PRINCIPAL --scope $COSMOS_ID
```

> These grants can take a few minutes to propagate. Until then the app may log auth errors
> reading Cosmos/Storage.

## 7b. Enable the administrator role (delegated access)

Administrators use the **/admin** page to grant a successor read access to a departed
employee's brief and backed-up files. Admins are identified by the `Handover.Admin` **app
role** in the ID-token `roles` claim — define it once, then assign people to it.

```powershell
$APPID = "00815db6-c56b-4e2e-a023-9ae44dd7f08b"

# 1. Add the app role to the registration (merges with any existing roles).
$role = @{
  allowedMemberTypes = @("User")
  description        = "Administer delegated access to departed-employee data"
  displayName        = "Handover Admin"
  id                 = [guid]::NewGuid().ToString()
  isEnabled          = $true
  value              = "Handover.Admin"
}
$existing = @(az ad app show --id $APPID --query "appRoles" -o json | ConvertFrom-Json)
$updated  = @($existing + $role)
az ad app update --id $APPID --app-roles ($updated | ConvertTo-Json -AsArray -Depth 5)

# 2. Assign an admin user to the role on the app's Enterprise Application (service principal).
$SP      = az ad sp show --id $APPID --query id -o tsv
$ROLE_ID = (az ad app show --id $APPID --query "appRoles[?value=='Handover.Admin'].id | [0]" -o tsv)
$USER_ID = az ad user show --id "admin@contoso.com" --query id -o tsv
az rest --method POST `
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP/appRoleAssignedTo" `
  --headers "Content-Type=application/json" `
  --body (@{ principalId = $USER_ID; resourceId = $SP; appRoleId = $ROLE_ID } | ConvertTo-Json)
```

Assigned users get the `Handover.Admin` role in their token at next sign-in and see the
**⚙ Admin** link. The role name is configurable via the `adminAppRole` bicep parameter /
`ADMIN_APP_ROLE` env var. To bootstrap before the role exists, set `ADMIN_UPNS` (a
comma-separated allow-list) as a temporary break-glass.

> The successor being granted access needs **no** Azure role and **no** app role — the app
> reads Cosmos/Storage with its own managed identity and enforces the grant in-app.

## 8. (Alternative) Cross-tenant — key-based data access

If Cosmos/Storage are in a different tenant, skip step 7 and redeploy step 4 with keys:

```powershell
$COSMOS_KEY = az cosmosdb keys list -n caigcosmoscj1 -g <cosmos-rg> --query primaryMasterKey -o tsv
$STORAGE_CS = az storage account show-connection-string -n transitionagentstorecj1 --query connectionString -o tsv

az deployment group create -g $RG --template-file infra/main.bicep `
  --parameters infra/main.parameters.json `
  --parameters aadClientSecret=$CLIENT_SECRET_PLAIN secretKey=$SECRET_KEY `
  --parameters useKeyBasedDataAccess=true cosmosKey=$COSMOS_KEY storageConnectionString=$STORAGE_CS
```

## 9. Verify

```powershell
Invoke-RestMethod "$APPURL/healthz"          # -> status + version + user count
Start-Process $APPURL                        # opens the dashboard; sign in with Entra
az webapp log tail --name $WEBAPP --resource-group $RG   # live container logs
```

- The health check returns JSON with the app version.
- Sign-in redirects through Entra; after consent the dashboard loads.
- First refresh collects from Work IQ (or Graph-only if `WORKIQ_ENABLED=false`).

---

## Configuration reference

App settings are applied by Bicep from `infra/main.parameters.json` + the two secrets.
To change one after deployment:

```powershell
az webapp config appsettings set -g $RG -n $WEBAPP --settings WORKIQ_ENABLED=false
az webapp restart -g $RG -n $WEBAPP
```

To change how many rows each section of the brief holds, set the `MAX_*` settings (they
apply to both the Work IQ and Graph collectors; the defaults ship in the `sectionLimits`
parameter of `infra/main.parameters.json` / `infra/aca.parameters.json`):

```powershell
# App Service (Path B)
az webapp config appsettings set -g $RG -n $WEBAPP --settings MAX_PROJECTS=25 MAX_OUTSTANDING_ITEMS=30
az webapp restart -g $RG -n $WEBAPP

# Container Apps (Path A) - updating env vars rolls a new revision automatically
az containerapp update -n $CAPP -g $RG --set-env-vars MAX_PROJECTS=25 MAX_OUTSTANDING_ITEMS=30
```

Full list of variables and their meaning is in [`.env.example`](.env.example).

## Hardening (optional)

- **Key Vault for secrets.** Store `AAD_CLIENT_SECRET` / `SECRET_KEY` in Key Vault instead of
  platform secrets. App Service: set the app settings to `@Microsoft.KeyVault(SecretUri=...)`.
  Container Apps: reference the vault directly from the secret definition —
  ```bicep
  secrets: [
    { name: 'aad-client-secret', keyVaultUrl: 'https://<vault>.vault.azure.net/secrets/aad-client-secret', identity: uami.id }
  ]
  ```
  Grant the app's identity **Key Vault Secrets User** on the vault. This keeps secrets out of
  the template, the deployment history, and the platform's own secret store.
- **Rotate on exposure.** Anything that ends up in a container image is readable by every
  principal with `AcrPull`. If a secret is ever built into an image: rotate it in Entra,
  update the app's secret, roll a revision, then delete the affected image tags with
  `az acr repository delete -n <acr> --image transition-agent:<tag>`.
- **Immutable image tags.** Deploy `transition-agent:<date>-<n>` rather than `:latest` so a
  revision always maps to a known build and rollback is possible. Optionally enable
  [ACR tag locking](https://learn.microsoft.com/azure/container-registry/container-registry-image-lock)
  to prevent overwrites.
- **Least privilege on Cosmos.** The runbook grants *Built-in Data Contributor* at account
  scope. To narrow it, assign at the container path instead:
  `--scope "/dbs/transition-agent/colls/handovers"`. Validate in a non-production account
  first — the SDK also needs account-level `readMetadata`.
- **Restrict ingress.** Container Apps supports IP restrictions
  (`az containerapp ingress access-restriction set`) and internal-only environments if the
  app should not be reachable from the public internet.
- **Scale up / out.** For App Service production, scale the plan to `P1v3`+ (the template
  exposes `appServicePlanSku`). On Container Apps the web tier autoscales on HTTP concurrency
  (`maxReplicas`, `httpConcurrentRequests`); for large fleets enable the **Redis** mode
  ([A3.1](#a31-optional-stateless-scale-out-with-redis--a-worker)) so the web tier is
  stateless and the worker Container App autoscales on queue depth. Without Redis, keep
  **sticky sessions** enabled (they are on by default) so per-user in-memory state stays
  consistent across replicas.
- **Private networking / VNet integration** if Cosmos/Storage are locked to a VNet.
- **Review the Entra app**: no unused redirect URIs, client secret expiry tracked (or move to
  certificate credentials / federated identity), and admin consent limited to the delegated
  scopes the collectors actually use.

## Redeploy the app (new image)

```powershell
az acr build --registry $ACR --image transition-agent:latest .
az webapp restart --name $WEBAPP --resource-group $RG
```

## Tear down

```powershell
az group delete --name $RG --yes --no-wait
```
