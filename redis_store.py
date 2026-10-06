"""
redis_store.py
==============
Thin Redis layer shared by the web tier (app.py) and the refresh worker
(worker.py). It backs three pieces of state so the app can scale statelessly:

  1. MSAL token caches   -> key ``ta:cache:{sid}``   (read/written by auth.py)
  2. Refresh job status  -> key ``ta:job:{key}``     (published by the worker,
                                                       polled by any web replica)
  3. Refresh queue       -> list ``config.REDIS_QUEUE`` (RPUSH by web, BLPOP by
                                                          the worker)

A single lazily-created client is reused process-wide. ``decode_responses=True``
means everything comes back as ``str`` (MSAL cache blobs and JSON both are).

This module is only imported when ``config.USE_REDIS`` is true; without Redis the
app keeps its in-process state and never touches this file.
"""

from __future__ import annotations

import json
import threading
from typing import Optional

import config

_client = None
_client_lock = threading.Lock()

_CACHE_PREFIX = "ta:cache:"
_JOB_PREFIX = "ta:job:"


def client():
    """Return a shared Redis client, creating it on first use."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import redis

                common = dict(
                    decode_responses=True,
                    socket_timeout=15,
                    socket_connect_timeout=15,
                    health_check_interval=30,
                    retry_on_timeout=True,
                )
                if config.REDIS_URL:
                    _client = redis.Redis.from_url(config.REDIS_URL, **common)
                else:
                    _client = redis.Redis(
                        host=config.REDIS_HOST,
                        port=config.REDIS_PORT,
                        password=config.REDIS_PASSWORD or None,
                        ssl=config.REDIS_SSL,
                        **common,
                    )
    return _client


# --------------------------------------------------------------------------- #
# MSAL token cache
# --------------------------------------------------------------------------- #
def get_token_cache(sid: str) -> Optional[str]:
    return client().get(_CACHE_PREFIX + sid)


def set_token_cache(sid: str, blob: str) -> None:
    client().set(_CACHE_PREFIX + sid, blob, ex=config.REDIS_CACHE_TTL)


def del_token_cache(sid: str) -> None:
    client().delete(_CACHE_PREFIX + sid)


# --------------------------------------------------------------------------- #
# Refresh job status
# --------------------------------------------------------------------------- #
def get_job(key: str) -> Optional[dict]:
    raw = client().get(_JOB_PREFIX + key)
    return json.loads(raw) if raw else None


def set_job(key: str, state: dict) -> None:
    client().set(_JOB_PREFIX + key, json.dumps(state), ex=config.REDIS_JOB_TTL)


# --------------------------------------------------------------------------- #
# Refresh queue (a plain Redis list; KEDA autoscales the worker on its length)
# --------------------------------------------------------------------------- #
def enqueue(payload: dict) -> None:
    client().rpush(config.REDIS_QUEUE, json.dumps(payload))


def dequeue(timeout: int = 5) -> Optional[dict]:
    """Block up to ``timeout`` seconds for the next job; None if none arrived."""
    res = client().blpop(config.REDIS_QUEUE, timeout=timeout)
    if not res:
        return None
    _, raw = res
    return json.loads(raw)
