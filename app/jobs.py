"""Redis-backed asynchronous batch masking.

Masking one resume is slow and bursty: a Salesforce fetch, sometimes a
LibreOffice conversion, a redact pass, then an upload back. A caller handing
us 200 Job Applicants at once cannot wait for that on one HTTP request, and
running all 200 at once would exhaust LibreOffice processes and Salesforce API
limits. So work is queued and drained at a fixed width.

    POST /mask/batch/async   ->  {job_id}, returns immediately
    GET  /mask/jobs/{job_id} ->  progress + per-item results

Three Redis structures, and the reason each one is in Redis rather than in
process memory:

    mask:queue              LIST   the backlog. In Redis so a restart or a
                                   redeploy does not lose the remaining items.
    mask:inflight           ZSET   the concurrency gate, member = lease token,
                                   score = start time. In Redis so the cap is
                                   global: two app replicas together still run
                                   at most MAX_CONCURRENT, which a per-process
                                   semaphore could never guarantee.
    mask:job:<id>           HASH   counters and status for one submission.
    mask:job:<id>:results   LIST   one JSON result per finished item.

The gate is a lease, not a counter. A plain INCR/DECR pair leaks a slot every
time a worker dies mid-item, and after enough crashes the service would wedge
with a full counter and an idle queue. Each lease carries a timestamp instead,
and admission first drops any lease older than LEASE_TTL, so a crashed worker
returns its slot on its own.

Optional: with no REDIS_URL configured the async endpoints report that they
are disabled and the existing synchronous /mask/batch is unaffected.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any, Callable

QUEUE_KEY = "mask:queue"
INFLIGHT_KEY = "mask:inflight"
JOB_KEY = "mask:job:{}"
RESULTS_KEY = "mask:job:{}:results"

#: How many resumes may be masked at once, across every replica.
MAX_CONCURRENT = int(os.environ.get("MASK_MAX_CONCURRENT", "20"))

#: A lease older than this is assumed to belong to a dead worker and is
#: reclaimed. Must comfortably exceed the slowest single resume -- a .doc
#: needing LibreOffice plus two Salesforce round-trips -- or a slow item would
#: have its slot stolen while still working, letting concurrency drift above
#: the cap.
LEASE_TTL = int(os.environ.get("MASK_LEASE_TTL", "900"))

#: Job records are progress reporting, not a system of record: the masked PDF
#: itself already lives in Salesforce. Expired so Redis does not grow forever.
JOB_TTL = int(os.environ.get("MASK_JOB_TTL", str(7 * 24 * 3600)))

#: Seconds a worker waits on the queue before looping, so shutdown is prompt.
_POP_TIMEOUT = 5

#: Admission control, as one atomic step. Purge expired leases, then take a
#: slot only if that leaves room -- checking ZCARD and then ZADD from Python
#: would let several workers pass the check simultaneously and overshoot.
_ACQUIRE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[1])
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[2]) then
  redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
  return 1
end
return 0
"""

_client: Any = None
_acquire_script: Any = None


def configured() -> bool:
    return bool(os.environ.get("REDIS_URL"))


def client():
    """Lazily built async Redis client, or None when not configured."""
    global _client, _acquire_script
    if not configured():
        return None
    if _client is None:
        import redis.asyncio as aioredis

        _client = aioredis.from_url(
            os.environ["REDIS_URL"], encoding="utf-8", decode_responses=True)
        _acquire_script = _client.register_script(_ACQUIRE_LUA)
    return _client


async def ping() -> bool:
    r = client()
    if r is None:
        return False
    try:
        await r.ping()
        return True
    except Exception:
        return False


# --- the concurrency gate -------------------------------------------------

async def _acquire_slot(r) -> str | None:
    """Take a lease, or None if all MAX_CONCURRENT are busy."""
    token = uuid.uuid4().hex
    now = time.time()
    taken = await _acquire_script(
        keys=[INFLIGHT_KEY],
        args=[now - LEASE_TTL, MAX_CONCURRENT, now, token])
    return token if taken else None


async def _release_slot(r, token: str) -> None:
    try:
        await r.zrem(INFLIGHT_KEY, token)
    except Exception:
        pass          # the lease expires on its own; never mask a real error


# --- submitting work ------------------------------------------------------

async def submit(items: list[dict], client_key: str | None,
                 watermark_text: str, watermark_base64: str | None) -> str:
    """Queue a batch and return its job id."""
    r = client()
    job_id = uuid.uuid4().hex
    now = time.time()

    pipe = r.pipeline()
    pipe.hset(JOB_KEY.format(job_id), mapping={
        "status": "queued",
        "total": len(items),
        "succeeded": 0,
        "failed": 0,
        "created_at": now,
    })
    pipe.expire(JOB_KEY.format(job_id), JOB_TTL)
    for item in items:
        pipe.rpush(QUEUE_KEY, json.dumps({
            "job_id": job_id,
            "item": item,
            "client_key": client_key,
            "watermark_text": watermark_text,
            "watermark_base64": watermark_base64,
        }))
    await pipe.execute()
    return job_id


async def _record(r, job_id: str, job_applicant_id: str, result: dict) -> None:
    key = JOB_KEY.format(job_id)
    field = "succeeded" if result.get("status") == "ok" else "failed"
    pipe = r.pipeline()
    pipe.hincrby(key, field, 1)
    pipe.hset(key, "status", "running")
    pipe.rpush(RESULTS_KEY.format(job_id),
               json.dumps({"job_applicant_id": job_applicant_id, "result": result}))
    pipe.expire(key, JOB_TTL)
    pipe.expire(RESULTS_KEY.format(job_id), JOB_TTL)
    pipe.hgetall(key)
    out = await pipe.execute()

    meta = out[-1] or {}
    done = int(meta.get("succeeded", 0)) + int(meta.get("failed", 0))
    if done >= int(meta.get("total", 0) or 0):
        await r.hset(key, "status", "done")


async def status(job_id: str) -> dict | None:
    """Progress and results for one job, or None if unknown/expired."""
    r = client()
    meta = await r.hgetall(JOB_KEY.format(job_id))
    if not meta:
        return None
    total = int(meta.get("total", 0) or 0)
    succeeded = int(meta.get("succeeded", 0) or 0)
    failed = int(meta.get("failed", 0) or 0)
    raw = await r.lrange(RESULTS_KEY.format(job_id), 0, -1)
    return {
        "job_id": job_id,
        "status": meta.get("status", "queued"),
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "pending": max(0, total - succeeded - failed),
        "results": [json.loads(x) for x in raw],
    }


async def stats() -> dict:
    """Queue depth and current width, for /health."""
    r = client()
    if r is None:
        return {"backend": "disabled", "max_concurrent": MAX_CONCURRENT}
    try:
        queued = await r.llen(QUEUE_KEY)
        await r.zremrangebyscore(INFLIGHT_KEY, 0, time.time() - LEASE_TTL)
        inflight = await r.zcard(INFLIGHT_KEY)
        return {"backend": "redis", "queued": queued, "in_flight": inflight,
                "max_concurrent": MAX_CONCURRENT}
    except Exception as e:
        return {"backend": "redis", "error": type(e).__name__,
                "max_concurrent": MAX_CONCURRENT}


# --- draining the queue ---------------------------------------------------

async def _worker(process: Callable[[dict], dict], stop: asyncio.Event) -> None:
    """One consumer: take a slot, take an item, mask it, give the slot back.

    The slot is taken BEFORE the item. Popping first would pull work out of
    Redis and then sit on it waiting for the gate, which hides the backlog
    from /health and loses those items if the process dies.
    """
    r = client()
    while not stop.is_set():
        token = await _acquire_slot(r)
        if token is None:
            await asyncio.sleep(0.25)     # gate full; let a slot free up
            continue
        try:
            popped = await r.blpop(QUEUE_KEY, timeout=_POP_TIMEOUT)
            if not popped:
                continue                  # idle queue, not an error
            payload = json.loads(popped[1])
            item = payload["item"]
            try:
                # Masking is blocking (PyMuPDF, LibreOffice, Salesforce HTTP),
                # so it runs off the event loop.
                result = await asyncio.to_thread(process, payload)
            except Exception as e:
                result = {"status": "error", "detail": f"{type(e).__name__}: {e}"[:300]}
            await _record(r, payload["job_id"],
                          item.get("job_applicant_id", "?"), result)
        finally:
            await _release_slot(r, token)


async def start_workers(process: Callable[[dict], dict]) -> dict:
    """Start the consumer pool. Returns a handle for shutdown."""
    if not configured() or not await ping():
        return {}
    stop = asyncio.Event()
    tasks = [asyncio.create_task(_worker(process, stop))
             for _ in range(MAX_CONCURRENT)]
    return {"stop": stop, "tasks": tasks}


async def stop_workers(handle: dict) -> None:
    if not handle:
        return
    handle["stop"].set()
    for task in handle["tasks"]:
        task.cancel()
    await asyncio.gather(*handle["tasks"], return_exceptions=True)
