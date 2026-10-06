"""
storage.py
==========
Per-employee handover storage.

The primary backend is **Azure Cosmos DB** (one document per departing employee,
keyed by a slug of their UPN). When no Cosmos endpoint is configured the module
falls back to atomic per-employee JSON files so local development still works.

Backend selection (see config.USE_COSMOS):
    COSMOS_ENDPOINT set -> Cosmos DB
    otherwise           -> local JSON files under config.DATA_DIR
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Optional

import config

_lock = threading.Lock()


def slugify(upn: str) -> str:
    """Turn a UPN/email into a safe document id / filename stem."""
    s = (upn or "unknown").strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "_", s)
    return s.strip("._-") or "unknown"


# --------------------------------------------------------------------------- #
# Cosmos DB backend
# --------------------------------------------------------------------------- #
_container = None
_container_lock = threading.Lock()


def _cosmos_credential():
    """Credential for Cosmos DB.

    Uses the app's managed identity by default so database writes never depend on
    the signed-in user having data-plane access to the account. Falls back to
    DefaultAzureCredential when managed identity is disabled (local dev / az login).
    The identity needs the "Cosmos DB Built-in Data Contributor" role.
    """
    if config.COSMOS_USE_MANAGED_IDENTITY:
        from azure.identity import ManagedIdentityCredential

        client_id = config.COSMOS_MANAGED_IDENTITY_CLIENT_ID
        if client_id:
            return ManagedIdentityCredential(client_id=client_id)
        return ManagedIdentityCredential()

    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


def _get_container():
    """Lazily build (and cache) the Cosmos container client."""
    global _container
    if _container is not None:
        return _container
    with _container_lock:
        if _container is not None:
            return _container
        from azure.cosmos import CosmosClient, PartitionKey

        if config.COSMOS_KEY:
            client = CosmosClient(config.COSMOS_ENDPOINT, credential=config.COSMOS_KEY)
        else:
            client = CosmosClient(config.COSMOS_ENDPOINT, credential=_cosmos_credential())

        try:
            # Works with key auth or an identity that has management rights.
            db = client.create_database_if_not_exists(config.COSMOS_DATABASE)
            _container = db.create_container_if_not_exists(
                id=config.COSMOS_CONTAINER,
                partition_key=PartitionKey(path="/id"),
            )
        except Exception:  # noqa: BLE001 — data-plane-only identity: assume pre-provisioned
            db = client.get_database_client(config.COSMOS_DATABASE)
            _container = db.get_container_client(config.COSMOS_CONTAINER)
        return _container


def _strip_system(doc: dict) -> dict:
    """Drop Cosmos system fields (_rid, _etag, _ts, ...) before returning."""
    return {k: v for k, v in doc.items() if not k.startswith("_")}


def _cosmos_save(upn: str, data: dict) -> str:
    container = _get_container()
    doc = dict(data)
    slug = slugify(upn)
    doc["id"] = slug
    doc["upn"] = upn
    container.upsert_item(doc)
    return slug


def _cosmos_load(upn: str) -> Optional[dict]:
    from azure.cosmos import exceptions

    container = _get_container()
    slug = slugify(upn)
    try:
        return _strip_system(container.read_item(item=slug, partition_key=slug))
    except exceptions.CosmosResourceNotFoundError:
        return None


def _cosmos_resolve_slug(slug: str) -> Optional[dict]:
    return _cosmos_load(slug)


def _cosmos_list_users() -> list[dict]:
    container = _get_container()
    out: list[dict] = []
    for d in container.read_all_items():
        emp = d.get("employee", {})
        meta = d.get("meta", {})
        out.append(
            {
                "slug": d.get("id"),
                "upn": d.get("upn") or emp.get("upn") or emp.get("email") or d.get("id"),
                "displayName": emp.get("displayName") or d.get("id"),
                "generatedAt": meta.get("generatedAt"),
                "savedAt": meta.get("savedAt"),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Local-file backend (dev fallback)
# --------------------------------------------------------------------------- #
def _path_for(upn: str) -> Path:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config.DATA_DIR / f"{slugify(upn)}.json"


def _file_save(upn: str, data: dict) -> str:
    path = _path_for(upn)
    tmp = path.with_suffix(".json.tmp")
    with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic on the same filesystem
    return slugify(upn)


def _file_resolve_slug(slug: str) -> Optional[dict]:
    path = config.DATA_DIR / f"{slugify(slug)}.json"
    if not path.exists():
        return None
    with _lock:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None


def _file_load(upn: str) -> Optional[dict]:
    return _file_resolve_slug(slugify(upn))


def _file_list_users() -> list[dict]:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    for p in sorted(config.DATA_DIR.glob("*.json")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        emp = d.get("employee", {})
        out.append(
            {
                "slug": p.stem,
                "upn": emp.get("upn") or emp.get("email") or p.stem,
                "displayName": emp.get("displayName") or p.stem,
                "generatedAt": d.get("meta", {}).get("generatedAt"),
                "savedAt": d.get("meta", {}).get("savedAt"),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Public API (dispatches to the active backend)
# --------------------------------------------------------------------------- #
def save(upn: str, data: dict) -> str:
    """Persist a handover for ``upn``. Returns the document slug."""
    return _cosmos_save(upn, data) if config.USE_COSMOS else _file_save(upn, data)


def load(upn: str) -> Optional[dict]:
    return _cosmos_load(upn) if config.USE_COSMOS else _file_load(upn)


def resolve_slug(slug: str) -> Optional[dict]:
    """Load a brief by its slug (used by the API ?user= selector)."""
    return _cosmos_resolve_slug(slug) if config.USE_COSMOS else _file_resolve_slug(slug)


def list_users() -> list[dict]:
    """Return [{slug, upn, displayName, generatedAt, savedAt}] for every brief."""
    return _cosmos_list_users() if config.USE_COSMOS else _file_list_users()


def migrate_legacy() -> None:
    """One-time import of any local JSON handovers into the active store.

    For the Cosmos backend this uploads existing ``data/handovers/*.json`` files
    (and the old single ``data/handover.json``) so prior data is preserved. For
    the file backend it migrates only the legacy single-file layout.
    """
    legacy = config.LEGACY_DATA

    if not config.USE_COSMOS:
        if not legacy.exists():
            return
        try:
            with open(legacy, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        emp = d.get("employee", {})
        upn = emp.get("upn") or emp.get("email") or "legacy-user"
        if not _path_for(upn).exists():
            _file_save(upn, d)
        try:
            legacy.rename(legacy.with_suffix(".json.migrated"))
        except OSError:
            pass
        return

    # Cosmos: import any local files that aren't already present.
    sources: list[Path] = []
    if legacy.exists():
        sources.append(legacy)
    if config.DATA_DIR.exists():
        sources.extend(sorted(config.DATA_DIR.glob("*.json")))
    for p in sources:
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        emp = d.get("employee", {})
        upn = emp.get("upn") or emp.get("email") or p.stem
        try:
            if _cosmos_load(upn) is None:
                _cosmos_save(upn, d)
                p.rename(p.with_suffix(p.suffix + ".migrated"))
        except Exception:  # noqa: BLE001 — never block startup on migration
            pass
