"""
blob_backup.py
==============
Uploads bytes to Azure Blob Storage for handover file backups.

The source files are READ with the signed-in user's delegated identity (see
graph_files.py). Writing them to the backup storage account, however, uses the
**application's managed identity** — the departing user generally has no RBAC
on the storage account, so the app authenticates as itself.

Credentials (in priority order):
  1. AZURE_STORAGE_CONNECTION_STRING
  2. AZURE_STORAGE_ACCOUNT_URL + managed identity (system- or user-assigned).
     For local dev set AZURE_STORAGE_USE_MANAGED_IDENTITY=false to fall back to
     DefaultAzureCredential (az login, etc.).

Container is AZURE_BACKUP_CONTAINER (default "handover-backups") and is created
on first use. The managed identity needs the "Storage Blob Data Contributor"
role on the account/container.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import config


class BlobBackupError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(config.AZURE_STORAGE_CONNECTION_STRING or config.AZURE_STORAGE_ACCOUNT_URL)


def _storage_credential():
    """Credential for the backup storage account.

    Uses the app's managed identity by default so writes never depend on the
    signed-in user having access to the storage account. Falls back to
    DefaultAzureCredential when managed identity is disabled (local dev).
    """
    if config.AZURE_STORAGE_USE_MANAGED_IDENTITY:
        from azure.identity import ManagedIdentityCredential

        client_id = config.AZURE_STORAGE_MANAGED_IDENTITY_CLIENT_ID
        if client_id:
            return ManagedIdentityCredential(client_id=client_id)
        return ManagedIdentityCredential()

    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


def _service_client():
    from azure.storage.blob import BlobServiceClient

    if config.AZURE_STORAGE_CONNECTION_STRING:
        return BlobServiceClient.from_connection_string(config.AZURE_STORAGE_CONNECTION_STRING)
    if config.AZURE_STORAGE_ACCOUNT_URL:
        return BlobServiceClient(config.AZURE_STORAGE_ACCOUNT_URL, credential=_storage_credential())
    raise BlobBackupError(
        "Azure Blob Storage is not configured. Set AZURE_STORAGE_CONNECTION_STRING "
        "or AZURE_STORAGE_ACCOUNT_URL."
    )


def _container_client(container: Optional[str] = None):
    svc = _service_client()
    name = container or config.AZURE_BACKUP_CONTAINER
    cc = svc.get_container_client(name)
    try:
        cc.create_container()
    except Exception:  # noqa: BLE001 — already exists
        pass
    return cc


def upload_bytes(
    data: bytes,
    blob_name: str,
    content_type: Optional[str] = None,
    metadata: Optional[dict] = None,
    container: Optional[str] = None,
) -> dict:
    """Upload bytes; returns {blobName, url, size, container}."""
    from azure.storage.blob import ContentSettings

    cc = _container_client(container)
    settings = ContentSettings(content_type=content_type) if content_type else None
    blob = cc.get_blob_client(blob_name)
    blob.upload_blob(
        data,
        overwrite=True,
        content_settings=settings,
        metadata={k: str(v) for k, v in (metadata or {}).items()},
    )
    return {"blobName": blob_name, "url": blob.url, "size": len(data), "container": cc.container_name}


def backup_prefix(subject_upn: str) -> str:
    """Stable, sortable prefix per employee + run timestamp."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{_safe_subject(subject_upn)}/{stamp}"


def _safe_subject(subject_upn: str) -> str:
    """Filesystem/blob-safe form of a subject UPN (also the top-level prefix)."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in (subject_upn or "unknown")).strip("._-") or "unknown"


def subject_root_prefix(subject_upn: str) -> str:
    """All of a subject's backups live under this prefix (``<safe>/``)."""
    return f"{_safe_subject(subject_upn)}/"


def list_backups(subject_upn: str, container: Optional[str] = None) -> list[dict]:
    """List every backed-up blob for a subject, newest run first.

    Returns [{blob, name, size, lastModified, contentType}] where ``blob`` is the
    full blob path (needed to download) and ``name`` is the display path under the
    subject's prefix.
    """
    cc = _container_client(container)
    prefix = subject_root_prefix(subject_upn)
    out: list[dict] = []
    for b in cc.list_blobs(name_starts_with=prefix):
        settings = getattr(b, "content_settings", None)
        out.append(
            {
                "blob": b.name,
                "name": b.name[len(prefix):],
                "size": b.size,
                "lastModified": b.last_modified.isoformat() if getattr(b, "last_modified", None) else None,
                "contentType": getattr(settings, "content_type", None) if settings else None,
            }
        )
    out.sort(key=lambda x: x["blob"], reverse=True)
    return out


def open_backup_stream(subject_upn: str, blob_name: str, container: Optional[str] = None):
    """Open a download stream for one backed-up blob.

    Validates the blob really belongs to the subject's prefix (defense in depth
    against a caller passing another subject's path), then returns
    ``(chunks_iterable, content_type, size, filename)``.
    """
    prefix = subject_root_prefix(subject_upn)
    if ".." in blob_name or not blob_name.startswith(prefix):
        raise BlobBackupError("Requested blob is not within the subject's backup prefix.")
    cc = _container_client(container)
    downloader = cc.get_blob_client(blob_name).download_blob()
    props = downloader.properties
    settings = getattr(props, "content_settings", None)
    ctype = (getattr(settings, "content_type", None) if settings else None) or "application/octet-stream"
    filename = blob_name.rsplit("/", 1)[-1]
    return downloader.chunks(), ctype, props.size, filename

