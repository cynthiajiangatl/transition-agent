"""
worker.py — refresh worker for the queue+worker scale-out model.

Runs as a separate process/container (no web server). It blocks on the Redis
refresh queue, and for each job re-acquires the user's delegated tokens from the
shared MSAL cache (in Redis, keyed by the job's sid), runs the handover
collection, and publishes progress/state back to Redis so any web replica can
serve /api/refresh/status.

Scale throughput two ways:
  * WORKER_CONCURRENCY threads per replica (each independently drains the queue), and
  * more worker replicas (KEDA autoscales on the Redis queue length).

Start with:  python worker.py
Requires Redis (config.USE_REDIS). Without it, there is nothing to consume.
"""

from __future__ import annotations

import logging
import threading

import config

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
for _noisy in ("azure", "azure.cosmos", "azure.core.pipeline.policies.http_logging_policy",
               "azure.identity", "urllib3", "msal"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("transition-agent.worker")


def _consume(worker_id: int, stop: threading.Event) -> None:
    import redis_store
    import refresh_jobs

    log.info("worker thread %d consuming %s", worker_id, config.REDIS_QUEUE)
    while not stop.is_set():
        try:
            payload = redis_store.dequeue(timeout=5)
        except Exception:  # noqa: BLE001 — a transient Redis blip must not kill the loop
            log.exception("worker %d: dequeue failed; retrying", worker_id)
            stop.wait(1)
            continue
        if not payload:
            continue

        key = payload.get("key")
        sid = payload.get("sid")
        identity = payload.get("identity")
        transport = payload.get("transport")
        log.info("worker %d: running refresh for %s", worker_id, key)
        try:
            refresh_jobs.run(sid, identity, transport, lambda state, k=key: redis_store.set_job(k, state))
        except Exception:  # noqa: BLE001 — run() records its own errors; guard the loop regardless
            log.exception("worker %d: refresh crashed for %s", worker_id, key)


def main() -> None:
    if not config.USE_REDIS:
        raise SystemExit("worker.py requires Redis (set REDIS_URL or REDIS_HOST).")

    n = max(1, config.WORKER_CONCURRENCY)
    log.info("refresh worker starting: %d thread(s)", n)
    stop = threading.Event()
    threads = [threading.Thread(target=_consume, args=(i, stop), daemon=True, name=f"refresh-{i}") for i in range(n)]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=1)
    except KeyboardInterrupt:
        log.info("shutting down worker...")
        stop.set()


if __name__ == "__main__":
    main()
