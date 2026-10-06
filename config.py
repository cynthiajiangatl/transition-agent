"""
config.py
=========
Central configuration for the Transition Agent, sourced entirely from environment
variables with safe defaults. Nothing here is tied to a specific user or machine,
so the same build runs on a developer laptop, a container, or an app service.

Load order: a local ``.env`` file (if present) is read first, then real
environment variables take precedence.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def _load_dotenv() -> None:
    """Minimal .env loader (no third-party dependency)."""
    path = Path(__file__).resolve().parent / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'\"")
        os.environ.setdefault(key, val)


_load_dotenv()


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _limit(name: str, default: int) -> int:
    """A per-section item cap; never below 1, so a bad value can't empty a section."""
    return max(1, _int(name, default))


BASE_DIR = Path(__file__).resolve().parent

# --- Work IQ REST API ------------------------------------------------------ #
# WORKIQ_REST_ENDPOINT is the base up to and including the REST prefix; the client
# appends /conversations and /conversations/{id}/chat.
# Use ".../rest" for GA/prod or ".../rest/beta" for the beta surface.
WORKIQ_REST_ENDPOINT = os.environ.get("WORKIQ_REST_ENDPOINT", "https://workiq.svc.cloud.microsoft/rest")
WORKIQ_RESOURCE = os.environ.get("WORKIQ_RESOURCE", "fdcc1f02-fc51-4226-8753-f668596af7f7")
WORKIQ_SCOPE = os.environ.get("WORKIQ_SCOPE", f"{WORKIQ_RESOURCE}/WorkIQAgent.Ask")
WORKIQ_CLIENT_ID = os.environ.get("WORKIQ_CLIENT_ID", "ba081686-5d24-4bc6-a0d6-d034ecffed87")
# Use /organizations by default; enterprises can pin their own tenant id here.
WORKIQ_AUTHORITY = os.environ.get("WORKIQ_AUTHORITY", "https://login.microsoftonline.com/organizations")
WORKIQ_TOKEN = os.environ.get("WORKIQ_TOKEN")  # optional pre-acquired bearer token

WORKIQ_TRANSPORT = os.environ.get("WORKIQ_TRANSPORT", "rest")
# Whether to attempt the Work IQ collector at all. Set false in tenants that are
# NOT onboarded to Work IQ: otherwise, if the app can silently mint a Work IQ
# token (e.g. WorkIQAgent.Ask is consented), the refresh would pick Work IQ and
# get empty results instead of falling back to the Microsoft Graph collector.
WORKIQ_ENABLED = _bool("WORKIQ_ENABLED", True)

# --- collection tuning ----------------------------------------------------- #
MAX_WORKERS = _int("WORKIQ_MAX_WORKERS", 4)
SECTION_TIMEOUT = _int("WORKIQ_SECTION_TIMEOUT", 180)
TZ = os.environ.get("WORKIQ_TZ", "America/New_York")
TZ_OFFSET = _int("WORKIQ_TZ_OFFSET", -240)

# --- per-section item caps ------------------------------------------------- #
# Maximum rows kept per handover section. Applied by BOTH collectors — the Work
# IQ merge in collect.py and the Microsoft Graph generator in graph_collect.py —
# so a section holds the same number of rows whichever source produced it.
# Raising MAX_IMPORTANT_FILES also raises refresh time when GRAPH_DETECT_PII is
# on: each extra file costs two more Graph calls to resolve its Purview label.
MAX_CONTACTS = _limit("MAX_CONTACTS", 20)
MAX_PROJECTS = _limit("MAX_PROJECTS", 20)
MAX_IMPORTANT_FILES = _limit("MAX_IMPORTANT_FILES", 20)
MAX_RECURRING_PROCESSES = _limit("MAX_RECURRING_PROCESSES", 20)
MAX_OUTSTANDING_ITEMS = _limit("MAX_OUTSTANDING_ITEMS", 20)
MAX_ACCESS_TRANSFERS = _limit("MAX_ACCESS_TRANSFERS", 20)

SECTION_LIMITS = {
    "contacts": MAX_CONTACTS,
    "projects": MAX_PROJECTS,
    "importantFiles": MAX_IMPORTANT_FILES,
    "recurringProcesses": MAX_RECURRING_PROCESSES,
    "outstandingItems": MAX_OUTSTANDING_ITEMS,
    "accessTransfers": MAX_ACCESS_TRANSFERS,
}

# --- storage --------------------------------------------------------------- #
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data" / "handovers"))
LEGACY_DATA = BASE_DIR / "data" / "handover.json"  # migrated on first run if present
TOKEN_CACHE_PATH = os.environ.get("WORKIQ_TOKEN_CACHE", str(BASE_DIR / ".token_cache.bin"))

# --- Azure Cosmos DB (primary handover store) ------------------------------ #
# When COSMOS_ENDPOINT is set, handovers are read/written from Cosmos DB instead
# of local JSON files. Authentication uses the account key if COSMOS_KEY is set,
# otherwise Microsoft Entra (DefaultAzureCredential / managed identity).
COSMOS_ENDPOINT = os.environ.get("COSMOS_ENDPOINT")
COSMOS_KEY = os.environ.get("COSMOS_KEY")  # optional; prefer managed identity
COSMOS_DATABASE = os.environ.get("COSMOS_DATABASE", "transition-agent")
COSMOS_CONTAINER = os.environ.get("COSMOS_CONTAINER", "handovers")
# Use Cosmos when an endpoint is configured; fall back to local files for dev.
USE_COSMOS = bool(COSMOS_ENDPOINT)
# When no key is supplied, authenticate to Cosmos with the app's MANAGED IDENTITY
# (the signed-in user typically has no data-plane access to the database). Set the
# client id for a user-assigned managed identity; leave blank for system-assigned.
# Disable to use DefaultAzureCredential (e.g. az login) for local development.
COSMOS_USE_MANAGED_IDENTITY = _bool("COSMOS_USE_MANAGED_IDENTITY", True)
COSMOS_MANAGED_IDENTITY_CLIENT_ID = os.environ.get("COSMOS_MANAGED_IDENTITY_CLIENT_ID")

# --- Microsoft Entra ID web sign-in ---------------------------------------- #
# The web app's own confidential-client registration. It must have the delegated
# Work IQ permission (WorkIQAgent.Ask) granted so the signed-in user's token can
# call the Work IQ API on their behalf.
AAD_CLIENT_ID = os.environ.get("AAD_CLIENT_ID") or os.environ.get("AZURE_CLIENT_ID")
AAD_CLIENT_SECRET = os.environ.get("AAD_CLIENT_SECRET") or os.environ.get("AZURE_CLIENT_SECRET")
AAD_TENANT_ID = os.environ.get("AAD_TENANT_ID") or os.environ.get("AZURE_TENANT_ID", "organizations")
AAD_AUTHORITY = os.environ.get("AAD_AUTHORITY", f"https://login.microsoftonline.com/{AAD_TENANT_ID}")
AAD_REDIRECT_URI = os.environ.get("AAD_REDIRECT_URI", "http://localhost:3000/auth/callback")
AAD_POST_LOGOUT_REDIRECT = os.environ.get("AAD_POST_LOGOUT_REDIRECT")
# Scopes requested at sign-in. Defaults to the Work IQ delegated scope so we
# obtain consent and a refresh token usable to silently mint Work IQ tokens for
# this user. Override via AAD_LOGIN_SCOPES (space/comma separated) — e.g. in a
# tenant where Work IQ isn't provisioned, set it to "User.Read" so sign-in still
# succeeds (Work IQ/Graph tokens are then acquired silently when available).
_login_scopes_raw = os.environ.get("AAD_LOGIN_SCOPES")
if _login_scopes_raw:
    AAD_LOGIN_SCOPES = [s for s in re.split(r"[ ,]+", _login_scopes_raw.strip()) if s]
else:
    AAD_LOGIN_SCOPES = [WORKIQ_SCOPE]
# Microsoft Graph delegated scopes used to read file content for backup, minted
# silently for the signed-in user from the same login (no separate sign-in).
GRAPH_FILE_SCOPES = ["Files.Read.All", "Sites.Read.All"]
# Microsoft Graph delegated scopes used by the Graph fallback collector (when the
# Work IQ resource is not available in the tenant). Reads profile, manager,
# people, files, calendar, mail, tasks, Teams, and group memberships so it can
# infer handover sections from factual Graph signals.
GRAPH_COLLECT_SCOPES = [
    "User.Read",
    # Reading the signed-in user's manager (/me/manager) returns 403
    # Authorization_RequestDenied with only User.Read in many tenants; the
    # directory-read scope below is required. Delegated User.Read.All needs
    # admin consent (grant once in the app registration).
    "User.Read.All",
    "People.Read",
    "Sites.Read.All",
    "Files.Read.All",
    "Calendars.Read",
    "Mail.Read",
    "Tasks.Read",
    "Team.ReadBasic.All",
    "GroupMember.Read.All",
]
# Optional delegated scope that lets the Graph fallback resolve a file's sensitivity
# label to its display name (used to flag PII). Requested on top of the collect
# scopes; if the app registration hasn't consented to it, collection still runs and
# files simply carry no sensitivity label.
GRAPH_LABEL_SCOPES = ["InformationProtectionPolicy.Read"]
# Toggle sensitivity-label / PII detection in the Graph fallback collector. Each
# labelled file costs two extra Graph calls, so ops can disable it if needed.
GRAPH_DETECT_PII = _bool("GRAPH_DETECT_PII", True)
# Enforce sign-in only when a registration is configured (keeps local dev usable).
AUTH_ENABLED = _bool("AUTH_ENABLED", bool(AAD_CLIENT_ID and AAD_CLIENT_SECRET))

# --- delegated access: administrators + grants ----------------------------- #
# An administrator may grant a successor read access to a departed employee's
# handover document and backed-up files. Admins are identified by the ADMIN_APP_ROLE
# Entra app role, surfaced in the ID token "roles" claim (assign users to the role
# on the app registration's Enterprise Application). ADMIN_UPNS is an optional
# break-glass allow-list (comma/space separated) to bootstrap the first admin.
ADMIN_APP_ROLE = os.environ.get("ADMIN_APP_ROLE", "Handover.Admin")
ADMIN_UPNS = {
    u.strip().lower()
    for u in re.split(r"[ ,]+", os.environ.get("ADMIN_UPNS", "").strip())
    if u.strip()
}
# Grants persist alongside handovers: a Cosmos container when Cosmos is configured,
# else local JSON files (dev fallback, mirroring storage.py).
COSMOS_GRANTS_CONTAINER = os.environ.get("COSMOS_GRANTS_CONTAINER", "handover-grants")
GRANTS_DIR = Path(os.environ.get("GRANTS_DIR", BASE_DIR / "data" / "grants"))
# Default and maximum lifetime of a grant, in days.
GRANT_DEFAULT_DAYS = _int("GRANT_DEFAULT_DAYS", 90)
GRANT_MAX_DAYS = _int("GRANT_MAX_DAYS", 365)

# --- web server ------------------------------------------------------------ #
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = _int("PORT", 3000)
# Waitress serves each request on a worker thread. Handling here is I/O-bound
# (Work IQ / Graph / Cosmos calls), so we oversubscribe CPUs: the default scales
# with the host core count and can be overridden with WSGI_THREADS. In a container
# with a fractional CPU limit, set WSGI_THREADS explicitly (cpu_count() reports the
# node's cores, not the cgroup quota).
_CPU_COUNT = os.cpu_count() or 2
WSGI_THREADS = _int("WSGI_THREADS", min(64, max(8, _CPU_COUNT * 4)))
# How many client connections Waitress accepts before queueing, and how long a
# slow/idle connection may hold a slot. Raise the limit for many concurrent users.
WSGI_CONNECTION_LIMIT = _int("WSGI_CONNECTION_LIMIT", 1000)
WSGI_CHANNEL_TIMEOUT = _int("WSGI_CHANNEL_TIMEOUT", 300)
# Heavy live refreshes run off the request threads. This caps how many run at once
# *per replica* so a burst of users can't exhaust memory or overrun the upstream
# APIs; excess callers get HTTP 429 and retry. Scale out (more replicas) to raise
# the effective ceiling. Each refresh still fans out to WORKIQ_MAX_WORKERS threads.
MAX_CONCURRENT_REFRESHES = _int("MAX_CONCURRENT_REFRESHES", max(4, _CPU_COUNT * 2))
MAX_CONTENT_LENGTH = _int("MAX_CONTENT_LENGTH", 8 * 1024 * 1024)  # 8 MB cap on PUTs

# --- Redis: shared state + refresh queue (stateless scale-out) -------------- #
# When Redis is configured, the MSAL token cache, refresh job status, and the
# refresh queue all live in Redis, so the web tier is stateless (any replica can
# serve any user) and a separate worker pool (worker.py) drains the queue. Leave
# Redis unset for local dev: state stays in-process and refreshes run in a
# background thread inside the web process (single-replica behavior).
# Provide either REDIS_URL (e.g. rediss://:<key>@host:6380/0) or discrete host/port.
REDIS_URL = os.environ.get("REDIS_URL", "").strip()
REDIS_HOST = os.environ.get("REDIS_HOST", "").strip()
REDIS_PORT = _int("REDIS_PORT", 6380)
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")
REDIS_SSL = _bool("REDIS_SSL", True)
USE_REDIS = bool(REDIS_URL or REDIS_HOST)
REDIS_QUEUE = os.environ.get("REDIS_QUEUE", "ta:refresh:queue")
REDIS_JOB_TTL = _int("REDIS_JOB_TTL", 3600)                    # job status kept 1h past finish
REDIS_CACHE_TTL = _int("REDIS_CACHE_TTL", 60 * 60 * 24 * 7)    # token cache kept 7 days
# Refresh worker threads per worker replica; scale further by adding worker replicas.
WORKER_CONCURRENCY = _int("WORKER_CONCURRENCY", 4)
SECRET_KEY = os.environ.get("SECRET_KEY", os.urandom(32).hex())
ENABLE_REFRESH = _bool("ENABLE_REFRESH", True)  # allow disabling live refresh in shared deployments
TRUST_PROXY = _bool("TRUST_PROXY", False)        # honor X-Forwarded-* behind a reverse proxy
# Mark the session cookie Secure whenever the app is served over TLS. Defaults to
# on behind a proxy (App Service / Container Apps terminate TLS); set false only
# for plain-HTTP local development, where a Secure cookie would never be sent.
SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", TRUST_PROXY)
SESSION_LIFETIME_MINUTES = _int("SESSION_LIFETIME_MINUTES", 480)

# --- observability --------------------------------------------------------- #
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
PUBLIC_DIR = BASE_DIR / "public"

# --- file backup (Azure Blob + Microsoft Graph download) ------------------- #
AZURE_STORAGE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
AZURE_STORAGE_ACCOUNT_URL = os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
AZURE_BACKUP_CONTAINER = os.environ.get("AZURE_BACKUP_CONTAINER", "handover-backups")
# The storage account is written to with the app's own **managed identity**, not
# the signed-in user (who typically has no access to the storage account). The
# user's delegated identity is only used to READ the source files via Graph.
# Set the client id for a user-assigned managed identity; leave blank for the
# system-assigned one. Disable to use DefaultAzureCredential (e.g. az login) for
# local development.
AZURE_STORAGE_USE_MANAGED_IDENTITY = _bool("AZURE_STORAGE_USE_MANAGED_IDENTITY", True)
AZURE_STORAGE_MANAGED_IDENTITY_CLIENT_ID = os.environ.get("AZURE_STORAGE_MANAGED_IDENTITY_CLIENT_ID")
# largest single file to back up (bytes); 0 = no limit
BACKUP_MAX_FILE_BYTES = _int("BACKUP_MAX_FILE_BYTES", 250 * 1024 * 1024)
# optional pre-acquired Graph token for file download (skips interactive sign-in)
GRAPH_TOKEN = os.environ.get("GRAPH_TOKEN")
# allow interactive sign-in (WAM/device-code) when backing up; set false on headless servers
BACKUP_ALLOW_INTERACTIVE = _bool("BACKUP_ALLOW_INTERACTIVE", True)
