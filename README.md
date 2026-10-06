# Transition Agent

A Python web app that acts as a transition / handover agent when an employee leaves.
It assembles everything a successor needs — role, contacts, projects, files, recurring
processes, open items, and access to transfer — into one editable, printable brief, with
workplace data sourced from **Microsoft Work IQ** and **Microsoft Graph** (each section
routed to its most accurate source).

> Work IQ API docs:
> https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/rest/overview

The app calls the Work IQ **REST API** directly over HTTPS (no CLI). It acquires a
delegated Entra token with MSAL and asks Work IQ natural-language questions, parsing
the grounded answers into a structured brief.

For accuracy and cost, the collector **routes each section to its best source**: the
factual/directory sections (employee & manager, key contacts, access transfers) come
from **Microsoft Graph** — authoritative and free of Copilot credits — while Work IQ
handles the synthesis sections (projects, recurring processes). Two sections are
**hybrid** — important files and outstanding hand-off items — combining Work IQ's
reasoning over mail/chats with Graph's factual signals (open To Do tasks, action-oriented
mail, recent files). When the Work IQ resource isn't provisioned in the tenant at all,
it **falls back to Microsoft Graph** for every section, inferring them from factual
Graph signals — profile/manager, relevant people, calendar events, recent mail, To Do
tasks, recent files, and Teams / group memberships.

## What it extracts (7 sections)

Role / department / org · key contacts (name, email, relationship) · active
projects (role, status, pending) · important files (+ links) · recurring processes ·
outstanding hand-off items (owner, due date) · access transfers (Azure / GitHub /
SharePoint / Teams).

## Architecture

```
config.py            All settings from env vars (no hardcoded users/paths/secrets)
auth.py              Entra ID web sign-in (MSAL auth-code flow); mints per-user
                       Work IQ + Graph tokens. Token cache is shared in Redis (or
                       in process memory locally); sid-based token acquisition lets
                       the worker mint tokens off-request
workiq_client.py     Work IQ REST API client + MSAL delegated auth
                       - OS-encrypted token cache (DPAPI / Keychain / libsecret)
                       - identity from the token (per-employee attribution)
collect.py           Per-section source routing -> per-employee brief: factual sections
                       (employee/manager, contacts, access transfers) from Microsoft Graph;
                       synthesis sections (projects, recurring processes) from Work IQ;
                       outstanding items and the important-files list merged from BOTH
                       (de-duplicated), with Purview sensitivity labels resolved for PII flags
graph_collect.py     Microsoft Graph collector — sources the factual/hybrid sections for
                       per-section routing and infers all 7 sections for full fallback,
                       from calendar/mail/To Do/files/Teams/group signals; merges into the
                       existing brief instead of overwriting
storage.py           Per-employee store (Azure Cosmos DB or local JSON), atomic
                       writes, process lock, legacy migration
redis_store.py       Shared Redis layer: MSAL token cache, refresh job status, and
                       the refresh queue - the backbone of stateless scale-out
refresh_jobs.py      Background refresh: enqueues onto Redis for a worker pool when
                       Redis is configured, else runs on a bounded in-process thread
worker.py            Refresh worker entrypoint (python worker.py): drains the Redis
                       queue, mints the user's tokens from the shared cache, runs the
                       collection, and publishes progress back to Redis
graph_files.py       Microsoft Graph /shares file download + sensitivity-label lookup
                       (driveItem extractSensitivityLabels + label catalog) for PII detection
blob_backup.py       Azure Blob Storage upload (connection string or Entra RBAC)
app.py               Flask app: security headers, health, multi-user API, live refresh
                       (delegated to refresh_jobs), file backup
serve.py             Production WSGI entrypoint (Waitress; dynamic thread pool with
                       tunable connection limit / channel timeout)
public/              Dashboard UI (edit mode, user selector, refresh, export, print)
scripts/             Tooling, e.g. generate_architecture_visio.py (builds the
                       editable Visio diagram architecture.vsdx)
Dockerfile           Container image (+ .dockerignore, .env.example)
```

Backend, Work IQ calls, and data pipeline are **all Python**; only the browser dashboard
is HTML/CSS/JS. An editable Visio diagram of these components lives at
`architecture.vsdx` (regenerate with `python scripts/generate_architecture_visio.py`).

## Quick start (local, single user)

```bash
pip install -r requirements.txt
python serve.py            # production server (Waitress) on http://localhost:3000
# or: python app.py        # Flask dev server
```

First refresh triggers a one-time sign-in (silent via the WAM broker on Windows,
otherwise device-code). The token is cached **OS-encrypted** for subsequent silent runs.

## Enterprise deployment

### Auth model (important)

Work IQ uses **Entra ID delegated** auth — application-only is **not** supported, so the
app always acts *as a signed-in user*. Two deployment shapes follow from this:

1. **Per-user (default).** The departing employee signs in with **Microsoft Entra ID**
   (OAuth2 authorization-code flow); the app calls Work IQ with that user's delegated
   token and stores the resulting brief keyed by their UPN. In tenants where Work IQ
   isn't provisioned, the app falls back to **Microsoft Graph** and infers all sections
   from Graph signals (set `AAD_LOGIN_SCOPES` to the Graph scopes so sign-in still
   succeeds).
2. **Central portal.** Host behind a reverse proxy; each employee signs in and the service
   collects their own data. Briefs are multi-employee (`storage.py` keys them by UPN,
   `/api/users` lists them, the UI has an employee selector) and persisted in **Azure
   Cosmos DB**. App-only/background collection is not possible with Work IQ.

### How Work IQ and Microsoft Graph work together

There are two collectors — `collect.py` (Work IQ, with per-section routing to Graph) and
`graph_collect.py` (Graph only) — and every refresh picks one **mode** at collection time
(in the worker, or the in-process thread when Redis isn't configured), surfaced in the
refresh progress log:

| Mode | Chosen when | Collector |
|---|---|---|
| `workiq` | `WORKIQ_ENABLED=true` **and** a delegated Work IQ token can be minted for the signed-in user | `collect.py` — Work IQ for the synthesis segments, Microsoft Graph for the factual ones |
| `graph` | `WORKIQ_ENABLED=false`, **or** the Work IQ token fails (resource not provisioned / not consented) | `graph_collect.py` — all seven segments inferred from Graph |

If neither token can be acquired the endpoint returns `401`. In `workiq` mode the app also
tries — **best effort** — to mint a Graph token (collect scopes + `InformationProtectionPolicy.Read`,
falling back to the collect scopes alone). Every "Graph" cell in the `workiq` column below
applies **only when that token was obtained**; without it all seven segments come from Work
IQ alone and no sensitivity labels are resolved.

#### Per-segment routing

The routing sets live at the top of `collect.py` (`GRAPH_SECTIONS`, `HYBRID_SECTIONS`);
everything else is asked of Work IQ.

| Page segment (JSON key) | `workiq` mode | `graph` mode | Cap |
|---|---|---|---|
| Employee & manager (`employee`) | **Graph only** — Work IQ is not asked. Graph's fields overlay the token-seeded identity one key at a time (non-empty wins) | Graph | n/a |
| Key Contacts (`contacts`) | **Graph only** — replaces the section (only when Graph returns people, so a failed call never wipes it) | Graph | `MAX_CONTACTS` |
| Active Projects (`projects`) | **Work IQ only** — Graph's inferred projects are discarded in this mode | Graph-inferred | `MAX_PROJECTS` |
| Important Files (`importantFiles`) | **Hybrid** — Work IQ's ranked picks first, then Graph adds the files Work IQ missed (de-dup by `name`); Purview labels are then resolved **once** across the merged list | Graph only, labels resolved inline | `MAX_IMPORTANT_FILES` |
| Recurring Processes (`recurringProcesses`) | **Work IQ only** | Graph-inferred | `MAX_RECURRING_PROCESSES` |
| Outstanding Items (`outstandingItems`) | **Hybrid** — union of Work IQ's synthesis (wins de-dup on `description` + `source`) and Graph's To Do / mail signals | Graph only | `MAX_OUTSTANDING_ITEMS` |
| Access & Systems to Transfer (`accessTransfers`) | **Graph only** — Work IQ is not asked | Graph | `MAX_ACCESS_TRANSFERS` |

The caps are single values applied by **both** collectors (`config.SECTION_LIMITS`), so a
segment holds the same number of rows whichever source produced it — set them in `.env`
(see [Configuration](#configuration)).

So a `workiq` refresh spends Copilot credits on four questions only — projects, recurring
processes, important files, outstanding items — and they run **concurrently**
(`WORKIQ_MAX_WORKERS`, per-section `WORKIQ_SECTION_TIMEOUT`). Each is a strict
"reply with only JSON" prompt; an answer that fails to parse leaves the existing snapshot
for that segment untouched.

#### Graph signals per segment

| Segment | Microsoft Graph source |
|---|---|
| Employee / manager | `/me`, `/me/manager` (manager needs `User.Read.All`) |
| Key contacts | `/me/people` (top 15) |
| Active projects | clustered topics across `/me/calendarView`, `/me/messages`, files, To Do |
| Important files | live OneDrive walk (`/me/drive/root/children`, incl. subfolders) + `/me/drive/recent` + `/me/insights/used` / `/me/insights/trending`, filtered to documents; each file's Purview label resolved via `driveItem: extractSensitivityLabels` |
| Recurring processes | recurring / series events in `/me/calendarView` |
| Outstanding items | open `/me/todo` tasks + action-oriented `/me/messages` |
| Access transfers | `/me/joinedTeams`, `/me/memberOf`, SharePoint sites + Azure / GitHub mentions |

The projects, recurring-process, outstanding-item and access-transfer segments are
**heuristic inferences** whenever they come from Graph, so a reviewer should vet them
before finalizing. Both collectors tolerate missing permissions — a blocked endpoint is
skipped and logged into the refresh progress, not fatal.

### Configuration

Everything is environment-driven (see `.env.example`). Notable knobs:

| Variable | Purpose |
|---|---|
| `AAD_CLIENT_ID` / `AAD_CLIENT_SECRET` | Web app's Entra ID registration (confidential client) used for sign-in. Must have the delegated Work IQ permission (`WorkIQAgent.Ask`) granted. |
| `AAD_TENANT_ID` | Tenant for the sign-in authority (default `organizations`) |
| `AAD_REDIRECT_URI` | Must match the redirect URI registered on the app (e.g. `https://<host>/auth/callback`) |
| `AAD_POST_LOGOUT_REDIRECT` | Optional URL to return to after Entra ID sign-out |
| `AAD_LOGIN_SCOPES` | Scopes requested at sign-in (defaults to the Work IQ scope). In a tenant without Work IQ, set the Graph scopes `User.Read User.Read.All People.Read Files.Read.All Sites.Read.All Calendars.Read Mail.Read Tasks.Read Team.ReadBasic.All GroupMember.Read.All` so sign-in succeeds and the Graph routing/fallback/backup work |
| `AUTH_ENABLED` | Force-enable/disable sign-in (defaults on when `AAD_CLIENT_ID`+secret are set) |
| `COSMOS_ENDPOINT` | Azure Cosmos DB account URI; enables the Cosmos store (else local JSON files) |
| `COSMOS_KEY` | Optional account key; omit to authenticate with an Entra identity |
| `COSMOS_DATABASE` / `COSMOS_CONTAINER` | Database + container names (defaults `transition-agent` / `handovers`, partition key `/id`) |
| `COSMOS_USE_MANAGED_IDENTITY` | When no key is set, use the app's managed identity (default `true`); set `false` for local dev to use `DefaultAzureCredential` (`az login`) |
| `COSMOS_MANAGED_IDENTITY_CLIENT_ID` | Client id of a user-assigned managed identity (blank = system-assigned) |
| `WORKIQ_REST_ENDPOINT` | Work IQ REST API base (default `https://workiq.svc.cloud.microsoft/rest`; use `.../rest/beta` for the beta surface) |
| `WORKIQ_SCOPE` | Delegated Work IQ scope requested at sign-in (default `…/WorkIQAgent.Ask`) |
| `WORKIQ_SECTION_TIMEOUT` / `WORKIQ_TZ` | Per-section timeout (seconds) and IANA timezone sent to Work IQ |
| `MAX_CONTACTS` / `MAX_PROJECTS` / `MAX_IMPORTANT_FILES` / `MAX_RECURRING_PROCESSES` / `MAX_OUTSTANDING_ITEMS` / `MAX_ACCESS_TRANSFERS` | Maximum rows kept per segment of the brief (default 20 each, minimum 1). Applied by both collectors, so the cap is the same in `workiq` and `graph` mode. Raising `MAX_IMPORTANT_FILES` slows a refresh when `GRAPH_DETECT_PII=true` — each extra file costs two more Graph calls |
| `HOST` / `PORT` | Server bind address and port |
| `WSGI_THREADS` | Waitress worker threads (unset auto-sizes from CPU, min 8; set explicitly in a container with a fractional vCPU) |
| `WSGI_CONNECTION_LIMIT` / `WSGI_CHANNEL_TIMEOUT` | Max connections accepted before queueing, and how long a slow/idle connection may hold a thread |
| `MAX_CONCURRENT_REFRESHES` | Heavy refreshes allowed at once per process (in-process mode); excess callers get HTTP 429. In Redis mode the worker replicas set the ceiling |
| `SECRET_KEY` | Set a stable value in production (required so sessions survive restarts) |
| `ENABLE_REFRESH` | `false` to serve read-only (no live Work IQ calls) |
| `TRUST_PROXY` | `true` behind nginx / App Service / Container Apps for correct client IP & scheme; also enables HSTS and the `Secure` session cookie |
| `SESSION_COOKIE_SECURE` / `SESSION_LIFETIME_MINUTES` | Session cookie hardening. `Secure` defaults to `TRUST_PROXY` (on behind HTTPS, off for local http) and the session lifetime defaults to 480 minutes |
| `WORKIQ_MAX_WORKERS` | Parallel section questions (default 4; higher risks throttling) |
| `WORKIQ_ENABLED` | Attempt the Work IQ collector (default `true`). Set `false` in tenants **not** onboarded to Work IQ so refresh uses the Microsoft Graph collector directly instead of empty Work IQ results |
| `GRAPH_DETECT_PII` | Sensitivity-label / PII detection on Graph-sourced files (default `true`); `false` skips the extra label-resolution calls |
| `REDIS_URL` **or** `REDIS_HOST` / `REDIS_PORT` / `REDIS_SSL` / `REDIS_PASSWORD` | Enable **stateless scale-out**: move the MSAL token cache, refresh status, and the refresh queue into Redis. Unset = in-process state + background-thread refresh (local / single-node) |
| `REDIS_QUEUE` / `REDIS_JOB_TTL` / `REDIS_CACHE_TTL` | Refresh-queue list name, how long finished job status is kept (s), and token-cache TTL (s) |
| `WORKER_CONCURRENCY` | Refresh worker threads per worker replica (`worker.py`) |
| `LOG_LEVEL` | Structured log verbosity |

> **Entra ID setup:** register a web app in Entra ID, add a client secret, set the redirect
> URI to `https://<host>/auth/callback`, and grant the delegated **Work IQ** permission
> (`WorkIQAgent.Ask`) with admin consent. The Microsoft Graph collector and file
> backup need these delegated Microsoft Graph permissions: **User.Read, User.Read.All,
> People.Read, Calendars.Read, Mail.Read, Tasks.Read, Team.ReadBasic.All,
> GroupMember.Read.All, Files.Read.All, Sites.Read.All, InformationProtectionPolicy.Read**
> (`User.Read.All` is required to read the employee's manager via `/me/manager` — plain
> `User.Read` returns a 403 `Authorization_RequestDenied` in many tenants — and needs
> **admin consent**; `InformationProtectionPolicy.Read` enables sensitivity-label / PII
> detection; plus the standard `openid`, `profile`, `offline_access`).
> Grant admin consent so the app can mint Work IQ and Graph tokens for the user silently.
> **Cosmos setup:** create the
> database/container (partition key `/id`) ahead of time, or grant the app's identity the
> *Cosmos DB Built-in Data Contributor* role when using managed identity.

### Container

```bash
docker build -t transition-agent .
docker run -p 3000:3000 \
  -e AAD_CLIENT_ID=<id> -e AAD_CLIENT_SECRET=<secret> -e AAD_TENANT_ID=<tenant> \
  -e AAD_REDIRECT_URI=https://<host>/auth/callback \
  -e SECRET_KEY=<random-hex> \
  transition-agent
```

Sign-in happens in the browser via Entra ID (OAuth authorization-code) — no WAM/device-code
needed. Set a stable `SECRET_KEY` so sessions survive restarts; briefs persist in Azure
Cosmos DB (configure the `COSMOS_*` variables).

### Deploying to Azure

The same image runs on either host — see [DEPLOYMENT.md](DEPLOYMENT.md) for the full runbook:

| Host | IaC | Notes |
|---|---|---|
| **Azure Container Apps** | `infra/aca.bicep` | Consumption-based, needs no App Service VM quota. User-assigned managed identity for ACR pull + Cosmos/Storage |
| **Azure App Service** (Linux container) | `infra/main.bicep` | Needs dedicated App Service VM quota (B1+); system-assigned managed identity |

Either way the image is built server-side with `az acr build` (no local Docker). The app
**scales horizontally** — the web tier autoscales on HTTP concurrency and heavy refreshes
run off-request. Two scaling modes are supported (see [DEPLOYMENT.md](DEPLOYMENT.md)):

- **In-process (default, no extra infra).** The refresh runs on a background thread; the
  per-user token cache and refresh state stay in memory, so the Container App uses
  **sticky sessions** to keep each user on one replica.
- **Stateless (Redis).** Set `REDIS_*`: the MSAL token cache, refresh status, and the
  refresh queue move to Redis, and a separate **worker** Container App (`python worker.py`)
  drains the queue and autoscales on its depth — no sticky sessions needed.

### Production hardening built in

- **Waitress** WSGI server (not the Flask dev server).
- **Security headers** on every response: CSP, `X-Content-Type-Options`, `X-Frame-Options:
  DENY`, `Referrer-Policy`, `Strict-Transport-Security` (when served over TLS), and
  `Cache-Control: no-store` on API responses.
- **Hardened session cookie**: `HttpOnly`, `SameSite=Lax` (Lax, not Strict, so the Entra
  auth-code redirect still carries it), and `Secure` whenever the app is behind an HTTPS
  ingress.
- **No secrets in the image**: `.dockerignore` excludes `.env`, `.secret_key.txt`, the token
  cache, `data/` and the IaC folders — the `Dockerfile` uses `COPY . .`, so anything not
  excluded would be readable by anyone who can pull the image.
- **Non-root container**: the image drops privileges to `appuser`.
- **Liveness never depends on Cosmos**: `/healthz` reports `store: ok|unavailable` but still
  returns 200, so a store outage degrades the app instead of triggering a restart loop.
- **Request limits**: JSON body capped at `MAX_CONTENT_LENGTH` (8 MB default).
- **OS-encrypted token cache** via msal-extensions (DPAPI on Windows, Keychain on macOS,
  libsecret on Linux); a stale/foreign cache self-heals instead of failing.
- **Atomic, locked writes** to per-employee files (temp + `os.replace`) so concurrent
  edits/refreshes never corrupt data.
- **Health check** at `/healthz`; structured logging; graceful per-section error handling
  with retry/backoff on throttling (`429`), `5xx`, and timeouts.
- **Resilient refresh**: one slow/failed section never aborts the whole run.

## API

| Method & path | Purpose |
|---|---|
| `GET /healthz` | Liveness + version + user count |
| `GET /api/me` | Current sign-in state (`authenticated`, `authEnabled`, `workiqEnabled`, `user`) |
| `GET /api/users` | List stored employee briefs |
| `GET /api/handover[?user=<slug>]` | Read a brief (defaults to the signed-in user, else most recent) |
| `PUT /api/handover` | Save edits (subject inferred from payload) |
| `POST /api/refresh` | Start a live refresh for the signed-in user. Returns `{ok, started}` and runs it on a worker (Redis mode) or a background thread. `409` if one is already running for you; `429` if the in-process concurrency cap is hit |
| `GET /api/refresh/status` | Poll refresh progress (per-segment log), errors and result — served from Redis (or in-process state), so it works across replicas |
| `GET /api/backup/config` | Whether Blob backup is configured |
| `POST /api/backup` | Back up selected files to Azure Blob Storage |

## Editable mode

**✎ Edit** turns every field into an inline input; **＋ Add** / **✕** add or remove rows;
**Save changes** persists via `PUT /api/handover`; **⬇ Export JSON** and **Print / PDF**
for sharing. Multiple employees appear in a header **selector**. Items you edit or add are
flagged internally so a later refresh **never overwrites your edits** (see below).

## Important files — open & back up

The **Important Files** list is assembled from **both** collectors: Work IQ contributes the
files it judges most relevant to the handover (including SharePoint documents), and Microsoft
Graph adds any recent OneDrive documents Work IQ didn't return — de-duplicated by name, so no
file is fetched twice. Every file (from either source) then has its Microsoft Purview
sensitivity label resolved **once** via Graph, so PII is flagged consistently. In a
Work-IQ-less tenant the list comes entirely from the Graph collector.

In the **Important Files** section each file name is a **hyperlink to its stored
location** (SharePoint/OneDrive) that opens on click. Each file also has a **selection
checkbox**; pick some (or **Select all**) and click **☁ Back up selected to Blob
Storage** to copy them to Azure Blob Storage for safekeeping.

How the backup works:

1. The backend downloads each selected file's content via the Microsoft Graph `/shares`
   API using a **delegated Graph token** (`Files.Read.All`, `Sites.Read.All`). This is a
   *second* token, separate from the Work IQ token — backing up file content needs Graph
   file-read scopes that `WorkIQAgent.Ask` doesn't grant, so the first backup in a session
   prompts a one-time consent (silent thereafter).
2. It uploads each file to the configured container under
   `handover-backups/<employee-upn>/<timestamp>/<filename>`.

Configure storage with **either** a connection string **or** an account URL + Entra RBAC.
With an account URL the app writes using its **managed identity** by default (assign it
"Storage Blob Data Contributor"); set `AZURE_STORAGE_USE_MANAGED_IDENTITY=false` for local
dev to fall back to `DefaultAzureCredential` (`az login`):

```bash
# option A: connection string
AZURE_STORAGE_CONNECTION_STRING="..."
# option B: account URL + RBAC (assign "Storage Blob Data Contributor")
AZURE_STORAGE_ACCOUNT_URL=https://<account>.blob.core.windows.net
AZURE_BACKUP_CONTAINER=handover-backups        # created on first use
# AZURE_STORAGE_USE_MANAGED_IDENTITY=false      # local dev (uses az login)
```

If storage isn't configured the backup button is disabled with a hint; the file
hyperlinks still work. Endpoints: `GET /api/backup/config`, `POST /api/backup`.

## Refresh, merge & edit preservation

- **Auto-generate on first sign-in.** When a signed-in user has no stored brief yet, the
  dashboard runs one refresh automatically and saves it to Cosmos. If a brief already
  exists, it loads from Cosmos and does **not** auto-refresh.
- **Merge, don't overwrite.** A refresh loads the existing brief as its base and merges the
  fresh results per segment, de-duplicated by key and capped at the configured per-section
  limit (`MAX_CONTACTS`, `MAX_PROJECTS`, …): fresh data updates auto-generated rows and adds
  new ones; prior rows are kept.
- **Edits are protected in `workiq` mode.** Rows you edit or add are marked (`_edited`) and
  are ordered first in `collect.py`'s merge, so a refresh never replaces them (the same
  applies to edited `employee` fields). `graph_collect.py` de-duplicates fresh-first, so in
  `graph` mode an edited row whose key still matches a regenerated item can be superseded.
- If a segment comes back empty (e.g. no Copilot credits, or a blocked Graph endpoint), the
  existing snapshot for that segment is kept.

## Refresh performance

The Work IQ section questions run **concurrently** (one shared token, independent REST
requests), cutting a full refresh from ~3 minutes to ~70–90 seconds. With Graph routing
active, only the synthesis and hybrid sections go to Work IQ (projects, recurring
processes, important files, outstanding items) — the factual sections come from Graph — so
fewer Copilot credits are spent. Concurrency is bounded (`WORKIQ_MAX_WORKERS`, default 4)
to stay under Work IQ throttling.

## Security & privacy notes

- The token cache and per-employee briefs are **gitignored** — briefs contain private
  M365 data and the cache holds a credential.
- Work IQ enforces the user's Microsoft 365 permissions, sensitivity labels, and
  compliance policies automatically.
- Work IQ synthesizes some fields (status, suggested owners, priorities) — the departing
  employee should review the brief before finalizing.
- Graph-sourced segments are **heuristic** where they're generated rather than read from
  the directory: access transfers are inferred from Teams/group/file/mail signals in *both*
  modes, and in `graph` mode projects, recurring processes and outstanding items are
  inferred too. Review those before finalizing.
- **PII flagging:** important files whose Microsoft Purview sensitivity-label display name
  contains "PII" are marked with a red ✓ PII badge. Labels are always resolved via Microsoft
  Graph — `driveItem: extractSensitivityLabels` plus the label catalog
  (`/me/security/informationProtection/sensitivityLabels`) — because Work IQ's conversational
  answer doesn't include Purview labels. This needs the delegated
  **`InformationProtectionPolicy.Read`** permission consented on the app registration, and a
  real `User-Agent` header (the information-protection endpoints sit behind a WAF that 403s the
  default `python-urllib` agent). Without the permission, collection still runs but files carry
  no label (set `GRAPH_DETECT_PII=false` to skip label resolution entirely).
