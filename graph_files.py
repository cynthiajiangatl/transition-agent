"""
graph_files.py
==============
Downloads SharePoint / OneDrive file content via the Microsoft Graph ``/shares``
API, using a delegated token. This is separate from the Work IQ token: backing up
file *content* needs Graph file-read scopes, which Work IQ's WorkIQAgent.Ask scope
does not grant.

Token acquisition mirrors workiq_client.WorkIQAuth (silent -> WAM broker ->
device-code), but requests Files.Read.All + Sites.Read.All. The first backup in a
session may require a one-time consent.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
import urllib.error
from typing import Optional

import config

GRAPH = "https://graph.microsoft.com/v1.0"
# Some information-protection APIs (the sensitivity-label catalog) are beta-only.
GRAPH_BETA = "https://graph.microsoft.com/beta"
# A real User-Agent is required: the information-protection endpoints sit behind a
# WAF/gateway that 403s the default "Python-urllib" agent.
GRAPH_USER_AGENT = "TransitionAgent/2.0"
GRAPH_SCOPES = ["Files.Read.All", "Sites.Read.All"]


class GraphError(RuntimeError):
    pass


class GraphAuth:
    """Delegated Graph token using the same MSAL/cache setup as Work IQ."""

    def __init__(self):
        self._token: Optional[str] = config.GRAPH_TOKEN
        # reuse the Work IQ auth plumbing (encrypted cache, broker, device-code)
        from workiq_client import WorkIQAuth

        self._base = WorkIQAuth(client_id=config.WORKIQ_CLIENT_ID, authority=config.WORKIQ_AUTHORITY)

    def get_token(self, allow_interactive: bool = True) -> str:
        if self._token:
            return self._token
        import msal

        app = self._base._build_app(broker=False)
        for acct in app.get_accounts():
            res = app.acquire_token_silent(GRAPH_SCOPES, account=acct)
            if res and "access_token" in res:
                self._token = res["access_token"]
                return self._token

        if not allow_interactive:
            raise GraphError("graph_consent_required")

        result = None
        try:
            bapp = self._base._build_app(broker=True)
            accts = bapp.get_accounts()
            if accts:
                result = bapp.acquire_token_silent(GRAPH_SCOPES, account=accts[0])
            if not result or "access_token" not in result:
                result = bapp.acquire_token_interactive(
                    GRAPH_SCOPES,
                    parent_window_handle=msal.PublicClientApplication.CONSOLE_WINDOW_HANDLE,
                )
        except Exception:  # noqa: BLE001
            result = None

        if not result or "access_token" not in result:
            flow = app.initiate_device_flow(scopes=GRAPH_SCOPES)
            if "user_code" not in flow:
                raise GraphError(f"Failed to start device flow: {flow}")
            print("\n[Graph sign-in for file backup] " + flow["message"] + "\n", flush=True)
            result = app.acquire_token_by_device_flow(flow)

        if not result or "access_token" not in result:
            raise GraphError(
                f"Could not acquire a Graph token: {result.get('error') if result else 'unknown'}"
            )
        self._token = result["access_token"]
        return self._token


def _encode_share_url(url: str) -> str:
    """Graph shares API: base64url of the URL, prefixed 'u!', no trailing '='."""
    b64 = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")
    return "u!" + b64


def _graph_get(path: str, token: str, raw: bool = False, base: str = GRAPH):
    req = urllib.request.Request(
        base + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "*/*", "User-Agent": GRAPH_USER_AGENT},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
            ctype = r.headers.get("Content-Type", "")
            return (data, ctype) if raw else (__import__("json").loads(data.decode()), ctype)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise GraphError(f"Graph {e.code} on {path}: {detail}") from e


def _graph_post(path: str, token: str, body: Optional[dict] = None, base: str = GRAPH) -> dict:
    payload = json.dumps(body if body is not None else {}).encode()
    req = urllib.request.Request(
        base + path,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": GRAPH_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw.decode()) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise GraphError(f"Graph {e.code} on {path}: {detail}") from e


# --------------------------------------------------------------------------- #
# Sensitivity labels (Microsoft Purview) — used to flag files containing PII.
# --------------------------------------------------------------------------- #
def sensitivity_label_catalog(token: str, emit=None) -> dict:
    """Return ``{sensitivityLabelId: displayName}`` for labels available to the
    signed-in user. Tries the security/informationProtection sensitivity-label list
    first, then the older policy-labels endpoint. Returns ``{}`` when the
    ``InformationProtectionPolicy.Read`` permission isn't granted, so callers can
    degrade gracefully to "no label"."""
    catalog: dict[str, str] = {}

    def _log(msg: str) -> None:
        if emit:
            emit(msg)

    def _absorb(node) -> None:
        if not isinstance(node, dict):
            return
        lid = node.get("id")
        name = node.get("displayName") or node.get("name")
        if lid and name:
            catalog[str(lid)] = str(name)
        _absorb(node.get("parent"))  # sub-labels nest their parent

    for base, path in (
        (GRAPH_BETA, "/me/security/informationProtection/sensitivityLabels"),
        (GRAPH_BETA, "/me/informationProtection/policy/labels"),
    ):
        try:
            data, _ = _graph_get(path, token, base=base)
        except GraphError as e:
            _log(f"    - label catalog {path} error: {e}")
            continue
        vals = (data.get("value") if isinstance(data, dict) else []) or []
        for lbl in vals:
            _absorb(lbl)
        if catalog:
            break
    return catalog


def _resolve_drive_ids(share_url: str, token: str) -> tuple[Optional[str], Optional[str]]:
    """Resolve a file's web/sharing URL to ``(driveId, itemId)`` via the /shares API."""
    sid = _encode_share_url(share_url)
    item, _ = _graph_get(f"/shares/{sid}/driveItem?$select=id,parentReference", token)
    drive_id = (item.get("parentReference") or {}).get("driveId")
    return drive_id, item.get("id")


def extract_sensitivity_label_ids(drive_id: str, item_id: str, token: str) -> list[str]:
    """POST driveItem: extractSensitivityLabels (v1.0); return the applied label IDs."""
    result = _graph_post(f"/drives/{drive_id}/items/{item_id}/extractSensitivityLabels", token)
    # The API returns labels at the top level ({"labels": [...]}); some docs show a
    # {"value": {"labels": [...]}} envelope, so accept both.
    labels = None
    if isinstance(result, dict):
        labels = result.get("labels")
        if labels is None and isinstance(result.get("value"), dict):
            labels = result["value"].get("labels")
    ids: list[str] = []
    for lbl in labels or []:
        if isinstance(lbl, dict) and lbl.get("sensitivityLabelId"):
            ids.append(str(lbl["sensitivityLabelId"]))
    return ids


def file_sensitivity_label(share_url: str, token: str, catalog: dict) -> str:
    """Return the display name of a file's sensitivity label ("" if none/unknown).
    ``catalog`` maps label IDs to display names (see ``sensitivity_label_catalog``)."""
    drive_id, item_id = _resolve_drive_ids(share_url, token)
    if not (drive_id and item_id):
        return ""
    for lid in extract_sensitivity_label_ids(drive_id, item_id, token):
        name = catalog.get(lid)
        if name:
            return name
    return ""


def resolve_item(share_url: str, token: str) -> dict:
    """Resolve a sharing/web URL to a driveItem (name, size, mimeType, ids)."""
    sid = _encode_share_url(share_url)
    item, _ = _graph_get(f"/shares/{sid}/driveItem", token)
    return item


def download_shared(share_url: str, token: str, max_bytes: int = 0) -> tuple[bytes, str, dict]:
    """Return (content_bytes, content_type, item_metadata) for a shared file URL."""
    item = resolve_item(share_url, token)
    if "folder" in item:
        raise GraphError("URL points to a folder, not a file")
    if max_bytes and item.get("size", 0) > max_bytes:
        raise GraphError(f"File exceeds max backup size ({item.get('size')} > {max_bytes} bytes)")
    sid = _encode_share_url(share_url)
    content, ctype = _graph_get(f"/shares/{sid}/driveItem/content", token, raw=True)
    meta = {
        "name": item.get("name"),
        "size": item.get("size"),
        "mimeType": item.get("file", {}).get("mimeType") or ctype,
        "webUrl": item.get("webUrl"),
        "id": item.get("id"),
    }
    return content, meta["mimeType"], meta
