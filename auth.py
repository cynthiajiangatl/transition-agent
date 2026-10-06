"""
auth.py
=======
Microsoft Entra ID (Azure AD) web sign-in for the Transition Agent.

Implements the OAuth 2.0 authorization-code flow with an MSAL
``ConfidentialClientApplication``. A signed-in user's delegated token is then
used to call the Work IQ API on their behalf, so each user only ever collects
their own data.

Flow:
    1. ``/login``            -> redirect to Entra ID, requesting the Work IQ scope.
    2. ``/auth/callback``    -> redeem the code, store the user + token cache.
    3. ``get_workiq_token()``-> silently mint a Work IQ token for the current user.
    4. ``/logout``           -> clear the session and Entra ID sign-in.

The per-user MSAL token cache (which holds the refresh token) is kept server-side
in memory keyed by an opaque session id, so it never bloats the signed cookie.
"""

from __future__ import annotations

import base64
import json
import threading
import uuid
from typing import Optional

from flask import redirect, request, session

import config


class AuthError(RuntimeError):
    pass


# Server-side token-cache store. With Redis configured (config.USE_REDIS) the
# serialized MSAL cache lives in Redis, keyed by an opaque session id, so any web
# replica or the refresh worker can mint tokens for a user. Without Redis it falls
# back to this in-process dict (fine for single-replica local dev).
_caches: dict[str, str] = {}
_caches_lock = threading.Lock()


def _read_cache_blob(sid: Optional[str]) -> Optional[str]:
    if not sid:
        return None
    if config.USE_REDIS:
        import redis_store

        return redis_store.get_token_cache(sid)
    with _caches_lock:
        return _caches.get(sid)


def _write_cache_blob(sid: str, blob: str) -> None:
    if config.USE_REDIS:
        import redis_store

        redis_store.set_token_cache(sid, blob)
        return
    with _caches_lock:
        _caches[sid] = blob


def _del_cache_blob(sid: Optional[str]) -> None:
    if not sid:
        return
    if config.USE_REDIS:
        import redis_store

        redis_store.del_token_cache(sid)
        return
    with _caches_lock:
        _caches.pop(sid, None)


# --------------------------------------------------------------------------- #
# MSAL helpers
# --------------------------------------------------------------------------- #
def _build_msal_app(cache=None):
    import msal

    if not config.AAD_CLIENT_ID or not config.AAD_CLIENT_SECRET:
        raise AuthError(
            "Entra ID sign-in is not configured (set AAD_CLIENT_ID and AAD_CLIENT_SECRET)."
        )
    return msal.ConfidentialClientApplication(
        config.AAD_CLIENT_ID,
        authority=config.AAD_AUTHORITY,
        client_credential=config.AAD_CLIENT_SECRET,
        token_cache=cache,
    )


def _load_cache_for(sid: Optional[str]):
    """Build an MSAL cache from the stored blob for ``sid`` (no Flask context needed)."""
    import msal

    cache = msal.SerializableTokenCache()
    blob = _read_cache_blob(sid)
    if blob:
        cache.deserialize(blob)
    return cache


def _save_cache_for(sid: Optional[str], cache) -> None:
    if sid and cache.has_state_changed:
        _write_cache_blob(sid, cache.serialize())


def _load_cache():
    return _load_cache_for(session.get("sid"))


def _save_cache(cache) -> None:
    if not cache.has_state_changed:
        return
    sid = session.get("sid")
    if not sid:
        sid = uuid.uuid4().hex
        session["sid"] = sid
    _save_cache_for(sid, cache)


def _decode_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def current_user() -> Optional[dict]:
    """Return the signed-in user's identity ({upn, name, oid, tid}) or None."""
    return session.get("user")


def is_admin(user: Optional[dict] = None) -> bool:
    """True if ``user`` (or the signed-in user) may administer delegated access.

    Membership is by the ADMIN_APP_ROLE app role (ID-token ``roles`` claim), with
    an optional ADMIN_UPNS break-glass allow-list for bootstrapping.
    """
    user = user if user is not None else current_user()
    if not user:
        return False
    if config.ADMIN_APP_ROLE and config.ADMIN_APP_ROLE in (user.get("roles") or []):
        return True
    upn = (user.get("upn") or "").strip().lower()
    return bool(upn and upn in config.ADMIN_UPNS)


def login():
    app = _build_msal_app()
    flow = app.initiate_auth_code_flow(
        scopes=config.AAD_LOGIN_SCOPES,
        redirect_uri=config.AAD_REDIRECT_URI,
    )
    session["auth_flow"] = flow
    return redirect(flow["auth_uri"])


def callback():
    from flask import jsonify

    flow = session.get("auth_flow")
    if not flow:
        return redirect("/login")
    cache = _load_cache()
    app = _build_msal_app(cache)
    try:
        result = app.acquire_token_by_auth_code_flow(flow, request.args)
    except ValueError as e:  # state mismatch / replay
        return jsonify({"error": f"Authentication failed: {e}"}), 400
    if "error" in result:
        return jsonify({"error": result.get("error_description") or result["error"]}), 401

    claims = result.get("id_token_claims", {})
    session["user"] = {
        "upn": claims.get("preferred_username") or claims.get("upn") or claims.get("email"),
        "name": claims.get("name"),
        "oid": claims.get("oid"),
        "tid": claims.get("tid"),
        # App-role memberships (e.g. Handover.Admin) drive admin authorization.
        "roles": claims.get("roles") or [],
    }
    _save_cache(cache)
    session.pop("auth_flow", None)
    return redirect("/")


def logout():
    _del_cache_blob(session.get("sid"))
    session.clear()
    if config.AAD_POST_LOGOUT_REDIRECT:
        return redirect(
            f"{config.AAD_AUTHORITY}/oauth2/v2.0/logout"
            f"?post_logout_redirect_uri={config.AAD_POST_LOGOUT_REDIRECT}"
        )
    return redirect("/login")


def _acquire_silent(sid: Optional[str], scopes: list[str], what: str) -> str:
    """Silently mint a token for ``sid`` from its stored MSAL cache.

    Works both in a request (sid from the session) and in the worker (sid from the
    job payload), because it never reads Flask state directly. Raises AuthError if
    the user isn't signed in or the requested resource isn't consented/cached.
    """
    cache = _load_cache_for(sid)
    app = _build_msal_app(cache)
    accounts = app.get_accounts()
    if not accounts:
        raise AuthError("Not signed in.")
    result = app.acquire_token_silent(scopes, account=accounts[0])
    _save_cache_for(sid, cache)
    if not result or "access_token" not in result:
        if isinstance(result, dict):
            detail = f"{result.get('error')}: {result.get('error_description', '')}"
        else:
            detail = "no cached token for the resource (silent acquisition returned None)"
        raise AuthError(f"Could not acquire a {what} token for the signed-in user: {detail[:400]}")
    return result["access_token"]


def get_workiq_token() -> str:
    """Silently acquire a Work IQ token for the signed-in user (request context)."""
    return _acquire_silent(session.get("sid"), [config.WORKIQ_SCOPE], "Work IQ")


def get_workiq_token_for(sid: Optional[str]) -> str:
    """Silently acquire a Work IQ token for ``sid`` (used by the refresh worker)."""
    return _acquire_silent(sid, [config.WORKIQ_SCOPE], "Work IQ")


def get_graph_token(scopes: Optional[list[str]] = None) -> str:
    """Silently acquire a Microsoft Graph token for the signed-in user (request)."""
    return _acquire_silent(session.get("sid"), scopes or config.GRAPH_FILE_SCOPES, "Microsoft Graph")


def get_graph_token_for(sid: Optional[str], scopes: Optional[list[str]] = None) -> str:
    """Silently acquire a Microsoft Graph token for ``sid`` (used by the worker)."""
    return _acquire_silent(sid, scopes or config.GRAPH_FILE_SCOPES, "Microsoft Graph")
