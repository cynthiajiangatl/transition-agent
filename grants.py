"""
grants.py
=========
Application-level *delegated access* grants.

A grant is a record that lets a named successor (identified by their Entra UPN,
and their object id once they've signed in) read a **departed** employee's
handover document (Cosmos) and backed-up files (Blob). It changes nothing in
Azure RBAC: the app already reads Cosmos and Blob with its own managed identity
(see storage.py / blob_backup.py), so these grants gate that access purely at the
application layer — deny-by-default, checked on every read.

Backend mirrors storage.py:
    COSMOS_ENDPOINT set -> Cosmos DB container (config.COSMOS_GRANTS_CONTAINER)
    otherwise           -> local JSON files under config.GRANTS_DIR
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import config
from storage import slugify

_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _clamp_days(days) -> int:
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = config.GRANT_DEFAULT_DAYS
    return max(1, min(days, config.GRANT_MAX_DAYS))


def _is_active(g: dict) -> bool:
    """A grant is usable only while it is neither revoked nor expired."""
    if not g or g.get("revoked"):
        return False
    exp = g.get("expiresAt")
    if exp:
        try:
            if datetime.fromisoformat(exp) <= _now():
                return False
        except ValueError:
            pass
    return True


def _matches_grantee(g: dict, user: Optional[dict]) -> bool:
    """True if ``user`` is the grant's grantee (by object id, else by UPN)."""
    if not user:
        return False
    oid = user.get("oid")
    upn = (user.get("upn") or "").strip().lower()
    if oid and g.get("granteeOid") and g["granteeOid"] == oid:
        return True
    return bool(upn and g.get("granteeUpnLower") and g["granteeUpnLower"] == upn)


def _grant_id(grantee_upn: str, subject_slug: str) -> str:
    """Deterministic id so re-granting the same pair upserts one record."""
    return f"{slugify(grantee_upn)}__{subject_slug}"


def _new_doc(subject_slug, grantee_upn, grantee_oid, granted_by, granted_by_oid, days) -> dict:
    upn = (grantee_upn or "").strip()
    return {
        "id": _grant_id(upn, subject_slug),
        "type": "grant",
        "subjectSlug": subject_slug,
        "granteeUpn": upn,
        "granteeUpnLower": upn.lower(),
        "granteeOid": grantee_oid or None,
        "grantedBy": granted_by,
        "grantedByOid": granted_by_oid,
        "grantedAt": _iso(_now()),
        "expiresAt": _iso(_now() + timedelta(days=_clamp_days(days))),
        "revoked": False,
    }


# --------------------------------------------------------------------------- #
# Cosmos backend
# --------------------------------------------------------------------------- #
_container = None
_container_lock = threading.Lock()


def _get_container():
    global _container
    if _container is not None:
        return _container
    with _container_lock:
        if _container is not None:
            return _container
        import storage
        from azure.cosmos import CosmosClient, PartitionKey

        if config.COSMOS_KEY:
            client = CosmosClient(config.COSMOS_ENDPOINT, credential=config.COSMOS_KEY)
        else:
            client = CosmosClient(config.COSMOS_ENDPOINT, credential=storage._cosmos_credential())
        try:
            db = client.create_database_if_not_exists(config.COSMOS_DATABASE)
            _container = db.create_container_if_not_exists(
                id=config.COSMOS_GRANTS_CONTAINER,
                partition_key=PartitionKey(path="/subjectSlug"),
            )
        except Exception:  # noqa: BLE001 — data-plane-only identity: assume pre-provisioned
            db = client.get_database_client(config.COSMOS_DATABASE)
            _container = db.get_container_client(config.COSMOS_GRANTS_CONTAINER)
        return _container


def _strip_system(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if not k.startswith("_")}


def _cosmos_upsert(doc: dict) -> dict:
    _get_container().upsert_item(doc)
    return doc


def _cosmos_for_subject(subject_slug: str) -> list[dict]:
    container = _get_container()
    items = container.query_items(
        query="SELECT * FROM c WHERE c.subjectSlug = @s",
        parameters=[{"name": "@s", "value": subject_slug}],
        partition_key=subject_slug,
    )
    return [_strip_system(d) for d in items]


def _cosmos_all() -> list[dict]:
    return [_strip_system(d) for d in _get_container().read_all_items()]


# --------------------------------------------------------------------------- #
# Local-file backend (dev fallback)
# --------------------------------------------------------------------------- #
def _file_path(grant_id: str) -> Path:
    config.GRANTS_DIR.mkdir(parents=True, exist_ok=True)
    return config.GRANTS_DIR / f"{grant_id}.json"


def _file_upsert(doc: dict) -> dict:
    path = _file_path(doc["id"])
    with _lock:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
    return doc


def _file_all() -> list[dict]:
    config.GRANTS_DIR.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    for p in sorted(config.GRANTS_DIR.glob("*.json")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _file_for_subject(subject_slug: str) -> list[dict]:
    return [g for g in _file_all() if g.get("subjectSlug") == subject_slug]


# --------------------------------------------------------------------------- #
# Public API (dispatches to the active backend)
# --------------------------------------------------------------------------- #
def _upsert(doc: dict) -> dict:
    return _cosmos_upsert(doc) if config.USE_COSMOS else _file_upsert(doc)


def for_subject(subject_slug: str) -> list[dict]:
    """All grants (active or not) for a departed employee, newest first."""
    rows = _cosmos_for_subject(subject_slug) if config.USE_COSMOS else _file_for_subject(subject_slug)
    return sorted(rows, key=lambda g: g.get("grantedAt") or "", reverse=True)


def list_all() -> list[dict]:
    """Every grant across all subjects, newest first (admin view)."""
    rows = _cosmos_all() if config.USE_COSMOS else _file_all()
    return sorted(rows, key=lambda g: g.get("grantedAt") or "", reverse=True)


def create(
    subject_slug: str,
    grantee_upn: str,
    granted_by: str,
    grantee_oid: Optional[str] = None,
    granted_by_oid: Optional[str] = None,
    days: Optional[int] = None,
) -> dict:
    """Create (or renew) a grant letting ``grantee_upn`` read ``subject_slug``."""
    doc = _new_doc(subject_slug, grantee_upn, grantee_oid, granted_by, granted_by_oid, days)
    return _upsert(doc)


def revoke(grant_id: str, subject_slug: str, revoked_by: Optional[str] = None) -> bool:
    """Mark a grant revoked. Returns True if a matching grant was found."""
    for g in for_subject(subject_slug):
        if g.get("id") == grant_id:
            g["revoked"] = True
            g["revokedAt"] = _iso(_now())
            if revoked_by:
                g["revokedBy"] = revoked_by
            _upsert(g)
            return True
    return False


def has_access(user: Optional[dict], subject_slug: str) -> bool:
    """True if ``user`` holds an active, unexpired grant for ``subject_slug``."""
    return any(_is_active(g) and _matches_grantee(g, user) for g in for_subject(subject_slug))


def for_grantee(user: Optional[dict]) -> list[dict]:
    """Active grants where ``user`` is the grantee, newest first."""
    return [g for g in list_all() if _is_active(g) and _matches_grantee(g, user)]


def accessible_subjects(user: Optional[dict]) -> set[str]:
    """Set of subject slugs ``user`` may read via an active grant."""
    return {
        g["subjectSlug"]
        for g in list_all()
        if g.get("subjectSlug") and _is_active(g) and _matches_grantee(g, user)
    }
