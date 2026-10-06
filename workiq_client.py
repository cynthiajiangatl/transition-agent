"""
workiq_client.py
================
Python client for the **Microsoft Work IQ REST API** (Copilot Chat).

This talks DIRECTLY to the Work IQ REST API over HTTPS — it does not shell out to
the Work IQ CLI. It acquires a delegated Microsoft Entra token with MSAL, then
creates a Copilot conversation and POSTs chat messages, exactly as documented at
https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/rest/overview

REST contract (Work IQ Gateway, path prefix ``/rest``):

    create conversation : POST {rest}/conversations                → { "id": ... }
    chat (synchronous)  : POST {rest}/conversations/{id}/chat       → { "messages": [...] }
        request body    : { "message": {"text": ...},
                            "locationHint": {"timeZone": "<IANA>"} }
        response        : the last item in ``messages`` holds the assistant
                          answer in ``text`` (plus optional ``attributions``).

Auth contract (delegated, per-user):

    resource / audience : fdcc1f02-fc51-4226-8753-f668596af7f7  (api://workiq.svc.cloud.microsoft)
    delegated scope     : fdcc1f02-fc51-4226-8753-f668596af7f7/WorkIQAgent.Ask
    authorization server: https://login.microsoftonline.com/organizations
    rest endpoint       : https://workiq.svc.cloud.microsoft/rest

Work IQ requires delegated (per-user) auth; application-only is not supported.
Token acquisition order:
    1. WORKIQ_TOKEN env var (a pre-acquired bearer token)
    2. MSAL silent (from the on-disk token cache)
    3. MSAL broker / WAM (single sign-on with the signed-in Windows account)
    4. MSAL device-code flow (prints a code to complete in a browser)
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.request
import urllib.error
from typing import Optional

import config

# Work IQ REST API constants (all overridable via config / environment)
REST_ENDPOINT = config.WORKIQ_REST_ENDPOINT.rstrip("/")
WORKIQ_RESOURCE = config.WORKIQ_RESOURCE
WORKIQ_SCOPE = config.WORKIQ_SCOPE
DEFAULT_CLIENT_ID = config.WORKIQ_CLIENT_ID
AUTHORITY = config.WORKIQ_AUTHORITY
CACHE_PATH = config.TOKEN_CACHE_PATH


class WorkIQError(RuntimeError):
    pass


def _decode_claims(token: str) -> dict:
    """Decode (without verifying) the JWT payload to read identity claims from a
    token we just acquired for ourselves."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------------------- #
# Token acquisition (MSAL, delegated) — no CLI involved.
# --------------------------------------------------------------------------- #
class WorkIQAuth:
    def __init__(
        self,
        client_id: str = DEFAULT_CLIENT_ID,
        authority: str = AUTHORITY,
        token: Optional[str] = None,
    ):
        self.client_id = client_id
        self.authority = authority
        # An explicit per-user token (e.g. minted by the web sign-in for the
        # signed-in user) takes precedence over any env-provided token.
        self._token: Optional[str] = token or config.WORKIQ_TOKEN
        self._cache = None
        self._persistence = None

    def _load_cache(self):
        """Prefer an OS-encrypted persistence (DPAPI on Windows, Keychain on macOS,
        libsecret on Linux) via msal-extensions; fall back to a plain serialized
        cache only if the secure backend is unavailable. A stale/incompatible cache
        file is discarded and recreated rather than failing the run."""
        if self._cache is not None:
            return self._cache
        try:
            from msal_extensions import build_encrypted_persistence, PersistedTokenCache

            self._persistence = build_encrypted_persistence(CACHE_PATH)
            # validate we can actually decrypt; clear a stale/foreign cache if not
            if os.path.exists(CACHE_PATH):
                try:
                    self._persistence.load()
                except Exception:  # noqa: BLE001 — incompatible/old cache, reset it
                    try:
                        os.remove(CACHE_PATH)
                    except OSError:
                        pass
                    self._persistence = build_encrypted_persistence(CACHE_PATH)
            self._cache = PersistedTokenCache(self._persistence)
            return self._cache
        except Exception:  # noqa: BLE001 — secure backend unavailable
            import msal

            self._cache = msal.SerializableTokenCache()
            if os.path.exists(CACHE_PATH):
                try:
                    self._cache.deserialize(open(CACHE_PATH, "r", encoding="utf-8").read())
                except (OSError, ValueError):
                    pass
            return self._cache

    def _save_cache(self):
        # PersistedTokenCache writes through the OS-encrypted persistence on its own.
        # Only a plain (non-persisted) SerializableTokenCache needs a manual flush.
        # NOTE: PersistedTokenCache subclasses SerializableTokenCache, so we must gate
        # on self._persistence, not on isinstance, to avoid clobbering the encrypted file.
        if self._persistence is not None:
            return
        import msal

        if isinstance(self._cache, msal.SerializableTokenCache) and self._cache.has_state_changed:
            try:
                with open(CACHE_PATH, "w", encoding="utf-8") as f:
                    f.write(self._cache.serialize())
            except OSError:
                pass

    def _build_app(self, broker: bool):
        import msal

        kwargs = dict(authority=self.authority, token_cache=self._load_cache())
        if broker:
            kwargs["enable_broker_on_windows"] = True
        return msal.PublicClientApplication(self.client_id, **kwargs)

    def get_identity(self) -> dict:
        """Return {upn, name, oid, tid} for the signed-in user from the token."""
        claims = _decode_claims(self.get_token())
        return {
            "upn": claims.get("upn") or claims.get("preferred_username") or claims.get("unique_name"),
            "name": claims.get("name"),
            "oid": claims.get("oid"),
            "tid": claims.get("tid"),
        }

    def get_token(self) -> str:
        if self._token:
            return self._token

        import msal

        # 2) silent from cache (non-broker app shares the serialized cache)
        app = self._build_app(broker=False)
        for acct in app.get_accounts():
            res = app.acquire_token_silent([WORKIQ_SCOPE], account=acct)
            if res and "access_token" in res:
                self._save_cache()
                self._token = res["access_token"]
                return self._token

        # 3) broker / WAM single sign-on (silent on a machine where the user is signed in)
        result = None
        try:
            bapp = self._build_app(broker=True)
            accts = bapp.get_accounts()
            if accts:
                result = bapp.acquire_token_silent([WORKIQ_SCOPE], account=accts[0])
            if not result or "access_token" not in result:
                result = bapp.acquire_token_interactive(
                    [WORKIQ_SCOPE],
                    parent_window_handle=msal.PublicClientApplication.CONSOLE_WINDOW_HANDLE,
                )
        except Exception:  # noqa: BLE001 — broker unavailable, fall through to device code
            result = None

        # 4) device-code flow
        if not result or "access_token" not in result:
            flow = app.initiate_device_flow(scopes=[WORKIQ_SCOPE])
            if "user_code" not in flow:
                raise WorkIQError(f"Failed to start device flow: {flow}")
            print("\n[Work IQ sign-in] " + flow["message"] + "\n", flush=True)
            result = app.acquire_token_by_device_flow(flow)

        if not result or "access_token" not in result:
            raise WorkIQError(
                f"Could not acquire a Work IQ token: {result.get('error') if result else 'unknown'} "
                f"{result.get('error_description', '') if result else ''}"
            )
        self._save_cache()
        self._token = result["access_token"]
        return self._token


# --------------------------------------------------------------------------- #
# REST HTTP client (the documented Work IQ Copilot Chat API)
# --------------------------------------------------------------------------- #
class WorkIQRestClient:
    """Calls the Work IQ API via the REST Copilot Chat HTTPS endpoints."""

    def __init__(
        self,
        auth: Optional[WorkIQAuth] = None,
        endpoint: str = REST_ENDPOINT,
        tz: str = config.TZ,
        token: Optional[str] = None,
    ):
        self.auth = auth or WorkIQAuth(token=token)
        self.endpoint = endpoint.rstrip("/")
        self.tz = tz
        # conversation id threaded across stateful ask() turns (multi-turn)
        self.conversation_id: Optional[str] = None

    # context manager parity with the rest of the codebase
    def start(self) -> None:
        self.auth.get_token()

    def close(self) -> None:
        pass

    def __enter__(self) -> "WorkIQRestClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def ask(self, question: str, timeout: int = 150) -> str:
        """Stateful ask — threads the conversation id across turns (multi-turn)."""
        if not self.conversation_id:
            self.conversation_id = self._create_conversation(timeout=timeout)
        return self._chat(self.conversation_id, question, timeout=timeout)

    def ask_once(self, question: str, timeout: int = 150) -> str:
        """Stateless, thread-safe ask — a fresh conversation with no shared state.
        Use this for concurrent / parallel section questions."""
        conversation_id = self._create_conversation(timeout=timeout)
        return self._chat(conversation_id, question, timeout=timeout)

    # ----------------------------------------------------------------------- #
    # REST plumbing
    # ----------------------------------------------------------------------- #
    def _create_conversation(self, timeout: int = 150) -> str:
        payload = self._request("POST", f"{self.endpoint}/conversations", {}, timeout=timeout)
        conversation_id = payload.get("id")
        if not conversation_id:
            raise WorkIQError(f"No conversation id in Work IQ response: {payload}")
        return conversation_id

    def _chat(self, conversation_id: str, question: str, timeout: int = 150) -> str:
        body = {
            "message": {"text": question},
            "locationHint": {"timeZone": self.tz},
        }
        payload = self._request(
            "POST", f"{self.endpoint}/conversations/{conversation_id}/chat", body, timeout=timeout
        )
        return self._extract_answer(payload)

    @staticmethod
    def _extract_answer(payload: dict) -> str:
        """The synchronous /chat response echoes the conversation; the last message
        holds the assistant's answer in ``text``."""
        messages = payload.get("messages") or []
        for msg in reversed(messages):
            if isinstance(msg, dict) and isinstance(msg.get("text"), str):
                return msg["text"].strip()
        return ""

    def _request(self, method: str, url: str, body: Optional[dict], timeout: int = 150, _attempt: int = 0) -> dict:
        token = self.auth.get_token()
        data = json.dumps(body if body is not None else {}).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                # A real User-Agent is required: the Work IQ gateway/WAF drops the
                # default "python-urllib/x.y" agent, surfacing as a connection reset
                # ("remote end closed connection without response").
                "User-Agent": "TransitionAgent/2.0",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            # transient throttling/unavailability -> exponential backoff retry
            if e.code in (429, 500, 502, 503, 504) and _attempt < 4:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                delay = float(retry_after) if (retry_after and retry_after.isdigit()) else (2 ** _attempt)
                time.sleep(delay)
                return self._request(method, url, body, timeout=timeout, _attempt=_attempt + 1)
            detail = e.read().decode(errors="replace")[:400]
            raise WorkIQError(f"REST HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # includes socket read timeouts and transient connection drops
            if _attempt < 4:
                time.sleep(2 ** _attempt)
                return self._request(method, url, body, timeout=timeout, _attempt=_attempt + 1)
            raise WorkIQError(f"REST connection error: {e}") from e


def get_client(transport: Optional[str] = None, token: Optional[str] = None):
    """Factory. The only transport is the Work IQ REST API ('rest').

    ``token`` is an optional pre-acquired delegated bearer token for the user the
    collection runs on behalf of (used by the web sign-in flow)."""
    transport = (transport or config.WORKIQ_TRANSPORT or "rest").lower()
    if transport in ("rest", "api", "http"):
        return WorkIQRestClient(token=token)
    raise WorkIQError(f"Unknown transport: {transport!r}")
