"""
refresh_jobs.py
===============
Background handover refresh with two interchangeable execution backends:

  * **Queue + worker (Redis)** — when ``config.USE_REDIS`` is set, ``start_refresh``
    enqueues a job and returns immediately. A separate worker pool (worker.py)
    drains the queue and runs the collection, publishing progress/state to Redis
    so *any* web replica can serve ``/api/refresh/status``. This is what lets the
    web tier stay stateless and scale horizontally.

  * **In-process thread (fallback)** — without Redis the refresh runs in a daemon
    thread inside the web process, bounded by a semaphore. This keeps local dev
    (and single-replica deployments) working with no extra infrastructure.

Both backends share the same execution core (`_run`) and the same credential/mode
resolution (`resolve_credentials`), so a refresh behaves identically either way.
The job-status shape is stable: {running, log[], error, finishedAt, upn}.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

import auth
import config
import storage

log = logging.getLogger("transition-agent")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def idle_job() -> dict:
    return {"running": False, "log": [], "error": None, "finishedAt": None, "upn": None}


def refresh_key(identity: dict | None) -> str:
    """Per-user key for a refresh job (a shared key when auth is disabled locally)."""
    if identity and identity.get("upn"):
        return storage.slugify(identity["upn"])
    return "default"


# --------------------------------------------------------------------------- #
# credential + collection-mode resolution (shared by both backends)
# --------------------------------------------------------------------------- #
def resolve_credentials(sid: str | None) -> tuple[str | None, str | None, str]:
    """Return (workiq_token, graph_token, mode) for the user identified by ``sid``.

    Prefers Work IQ; falls back to Microsoft Graph when Work IQ is disabled or not
    available in the tenant. Raises auth.AuthError if neither can be acquired.
    Runs on whichever process executes the refresh (worker or web thread), so it
    only uses sid-based token acquisition — never the Flask session.
    """
    if not config.AUTH_ENABLED:
        return None, None, "workiq"

    token = None
    graph_token = None
    mode = "workiq"
    workiq_err = None

    if config.WORKIQ_ENABLED:
        try:
            token = auth.get_workiq_token_for(sid)
            mode = "workiq"
            # Best-effort Graph token so the Work IQ collector can resolve Purview
            # sensitivity labels (Work IQ's answer omits them).
            try:
                graph_token = auth.get_graph_token_for(sid, config.GRAPH_COLLECT_SCOPES + config.GRAPH_LABEL_SCOPES)
            except auth.AuthError:
                try:
                    graph_token = auth.get_graph_token_for(sid, config.GRAPH_COLLECT_SCOPES)
                except auth.AuthError:
                    graph_token = None
        except auth.AuthError as e:
            workiq_err = e

    if not config.WORKIQ_ENABLED or workiq_err is not None:
        if workiq_err is not None:
            log.info("Work IQ not available (%s); falling back to Microsoft Graph", workiq_err)
        else:
            log.info("Work IQ disabled (WORKIQ_ENABLED=false); using the Microsoft Graph collector")
        try:
            graph_token = auth.get_graph_token_for(sid, config.GRAPH_COLLECT_SCOPES + config.GRAPH_LABEL_SCOPES)
        except auth.AuthError:
            graph_token = auth.get_graph_token_for(sid, config.GRAPH_COLLECT_SCOPES)  # may raise -> caller records it
        mode = "graph"

    return token, graph_token, mode


# --------------------------------------------------------------------------- #
# execution core (used by the worker and the in-process thread)
# --------------------------------------------------------------------------- #
def run(sid: str | None, identity: dict | None, transport: str | None, publish) -> None:
    """Resolve credentials, run the collection, and persist the brief.

    ``publish(state: dict)`` is called with the evolving job state so the caller
    can surface progress (write to Redis, or update an in-process dict).
    """
    job = idle_job()

    def emit():
        publish(dict(job))

    try:
        token, graph_token, mode = resolve_credentials(sid)
    except auth.AuthError as e:
        job.update(running=False, error=f"Authentication failed: {e}", log=[f"ERROR: {e}"], finishedAt=_now())
        emit()
        return

    label = "Microsoft Graph" if mode == "graph" else "Work IQ"
    job.update(running=True, log=[f"Starting {label} collection..."], error=None, finishedAt=None, upn=None)
    emit()

    def progress(msg: str):
        job["log"].append(msg)
        emit()

    try:
        if mode == "graph":
            import graph_collect

            upn, data = graph_collect.collect(graph_token, identity=identity, progress=progress)
        else:
            from collect import collect

            upn, data = collect(transport, progress=progress, token=token, identity=identity, graph_token=graph_token)
        storage.save(upn, data)
        job["upn"] = storage.slugify(upn)
        job["log"].append("Refresh complete.")
        log.info("refresh complete for %s via %s", upn, mode)
    except Exception as e:  # noqa: BLE001 — one failed run must not crash the worker
        job["error"] = str(e)
        job["log"].append(f"ERROR: {e}")
        log.exception("refresh failed")
    finally:
        job["running"] = False
        job["finishedAt"] = _now()
        emit()


# --------------------------------------------------------------------------- #
# in-process backend (no Redis): daemon thread + bounded semaphore
# --------------------------------------------------------------------------- #
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_slots = threading.BoundedSemaphore(config.MAX_CONCURRENT_REFRESHES)


def _prune(max_age_seconds: int = 3600) -> None:
    now = datetime.now(timezone.utc)
    stale = []
    for key, job in _jobs.items():
        if job.get("running") or not job.get("finishedAt"):
            continue
        try:
            age = (now - datetime.fromisoformat(job["finishedAt"]).astimezone(timezone.utc)).total_seconds()
        except ValueError:
            continue
        if age > max_age_seconds:
            stale.append(key)
    for key in stale:
        _jobs.pop(key, None)


def _start_inprocess(key: str, sid: str | None, identity: dict | None, transport: str | None):
    with _jobs_lock:
        _prune()
        existing = _jobs.get(key)
        if existing and existing.get("running"):
            return False, 409, "A refresh is already running for you"
    if not _slots.acquire(blocking=False):
        return False, 429, "Server is busy; please retry the refresh shortly"

    starting = idle_job()
    starting.update(running=True, log=["Starting..."])
    with _jobs_lock:
        _jobs[key] = starting

    def publish(state: dict):
        with _jobs_lock:
            _jobs[key] = state

    def target():
        try:
            run(sid, identity, transport, publish)
        finally:
            _slots.release()

    threading.Thread(target=target, daemon=True).start()
    return True, 200, None


# --------------------------------------------------------------------------- #
# public API used by app.py
# --------------------------------------------------------------------------- #
def start_refresh(key: str, sid: str | None, identity: dict | None, transport: str | None):
    """Kick off a refresh. Returns (ok, http_status, error_message)."""
    if not config.USE_REDIS:
        return _start_inprocess(key, sid, identity, transport)

    import redis_store

    existing = redis_store.get_job(key)
    if existing and existing.get("running"):
        return False, 409, "A refresh is already running for you"

    queued = idle_job()
    queued.update(running=True, log=["Queued for a worker..."])
    redis_store.set_job(key, queued)
    redis_store.enqueue({"key": key, "sid": sid, "identity": identity, "transport": transport})
    return True, 200, None


def get_status(key: str) -> dict:
    if config.USE_REDIS:
        import redis_store

        return redis_store.get_job(key) or idle_job()
    with _jobs_lock:
        job = _jobs.get(key)
        return dict(job) if job else idle_job()
