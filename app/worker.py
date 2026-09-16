"""Queue worker: drains the masking backlog, serves no HTTP.

    python -m app.worker

Same image as the web service, different command. On Railway that means a
second service built from this repo with its start command overridden; the
Dockerfile needs no branch for it, because the only difference is which
process the container runs.

Why a separate container at all, when app/server.py already starts the same
consumer pool at startup? Because the two jobs want opposite things from a
host:

  * the web service should stay responsive. Masking is CPU- and memory-heavy
    -- PyMuPDF rasterising, LibreOffice converting -- and a burst of it inside
    the web container makes every HTTP request queue behind work that has
    nothing to do with that request, health checks included.
  * the worker should scale with the backlog. Draining faster means running
    more of it, and that is a reason to add replicas of something that is not
    also holding the API up.

Splitting them makes each of those independent, and costs nothing to run: the
gate that bounds concurrency lives in Redis (see app/jobs.py), so N workers
and the web service together still mask at most MASK_MAX_CONCURRENT resumes
at a time. Nothing needs to know how many containers there are.

With MASK_RUN_WORKERS=0 set on the web service, the split is complete and the
API does no masking off the queue at all. Left unset, the web service keeps
draining as it always has, so deploying this worker changes nothing until the
web service is told to stop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from app import jobs
from app.server import _queued_item_worker

log = logging.getLogger("mask.worker")


async def _run() -> int:
    """Drain until the queue is empty and a signal says stop."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not jobs.configured():
        log.error("REDIS_URL is not set: there is no queue to drain. "
                  "The worker needs the same Redis as the web service.")
        return 2
    if not await jobs.ping():
        log.error("REDIS_URL is set but Redis is unreachable.")
        return 2

    handle = await jobs.start_workers(_queued_item_worker)
    if not handle:
        log.error("Could not start the consumer pool.")
        return 2
    log.info("draining %s, up to %d at a time (shared across every replica)",
             jobs.QUEUE_KEY, jobs.MAX_CONCURRENT)

    # Railway stops a container with SIGTERM. Finishing the item in hand
    # rather than dropping it is the difference between a redeploy costing
    # nothing and a redeploy losing whatever was in flight -- and an item
    # abandoned mid-flight holds its Redis lease until LEASE_TTL expires,
    # which narrows the gate for everyone else in the meantime.
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stopping.set())   # Windows

    await stopping.wait()
    log.info("stopping: finishing the items already in hand")
    await jobs.stop_workers(handle)
    log.info("stopped")
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
