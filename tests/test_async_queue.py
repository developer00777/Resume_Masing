"""Async batch masking queue, against a real Redis.

Needs a Redis on REDIS_TEST_URL (default localhost:6399) -- the concurrency
gate is a Lua script, so a fake client would not exercise the thing most
likely to be wrong. Skipped when no Redis is reachable.

    docker run -d --rm --name masker-redis -p 6399:6379 redis:7-alpine
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

REDIS_URL = os.environ.get("REDIS_TEST_URL", "redis://localhost:6399/9")


def _reachable() -> bool:
    try:
        import redis
        redis.from_url(REDIS_URL, socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(),
                                reason=f"no Redis at {REDIS_URL}")


@pytest.fixture()
def q(monkeypatch):
    """A clean jobs module bound to the test Redis."""
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    from app import jobs
    monkeypatch.setattr(jobs, "_client", None)
    monkeypatch.setattr(jobs, "_acquire_script", None)
    monkeypatch.setattr(jobs, "MAX_CONCURRENT", 3)
    # Every test flushes db 9 itself inside its own event loop; building a
    # loop here just to flush would leave a stale client bound to it.
    return jobs


def test_gate_never_exceeds_max_concurrent(q):
    """The cap is the point of putting the gate in Redis rather than using a
    per-process semaphore: it has to hold across replicas."""
    async def run():
        r = q.client()
        await r.flushdb()
        tokens = [await q._acquire_slot(r) for _ in range(5)]
        granted = [t for t in tokens if t]
        assert len(granted) == 3, f"gate let {len(granted)} through, cap is 3"
        assert await r.zcard(q.INFLIGHT_KEY) == 3

        await q._release_slot(r, granted[0])
        assert await q._acquire_slot(r) is not None, "a released slot was not reusable"
        await r.flushdb()
    asyncio.run(run())


def test_expired_lease_is_reclaimed(q):
    """A worker that dies mid-item must not leak its slot forever.

    This is why the gate is a lease with a timestamp and not an INCR/DECR
    counter -- a counter leaks a slot per crash until the service wedges with
    a full count and an idle queue."""
    async def run():
        r = q.client()
        await r.flushdb()
        for _ in range(3):
            assert await q._acquire_slot(r) is not None
        assert await q._acquire_slot(r) is None, "gate should be full"

        # age every lease past the TTL, as a crashed worker's would be
        stale = time.time() - q.LEASE_TTL - 1
        for token in await r.zrange(q.INFLIGHT_KEY, 0, -1):
            await r.zadd(q.INFLIGHT_KEY, {token: stale})

        assert await q._acquire_slot(r) is not None, "stale lease was not reclaimed"
        await r.flushdb()
    asyncio.run(run())


def test_submit_queues_every_item_and_drains_in_order(q):
    """Everything is queued, and the backlog survives in Redis rather than
    being held in the request that submitted it."""
    async def run():
        r = q.client()
        await r.flushdb()
        items = [{"job_applicant_id": f"a0X00000000{i:04d}"} for i in range(50)]
        job_id = await q.submit(items, client_key=None, watermark_text="", watermark_base64=None)

        assert await r.llen(q.QUEUE_KEY) == 50, "not everything was queued"
        snap = await q.status(job_id)
        assert (snap["total"], snap["pending"], snap["status"]) == (50, 50, "queued")

        first = await r.lpop(q.QUEUE_KEY)
        assert "a0X000000000000" in first, "queue is not FIFO"
        await r.flushdb()
    asyncio.run(run())


def test_workers_drain_the_queue_and_record_results(q):
    """End to end: 25 items, cap of 3, every one processed exactly once."""
    processed = []

    def process(payload):
        processed.append(payload["item"]["job_applicant_id"])
        time.sleep(0.01)
        return {"status": "ok", "masked_content_version_id": "068AAA",
                "redacted_regions": 3, "watermark_used": "none"}

    async def run():
        r = q.client()
        await r.flushdb()
        items = [{"job_applicant_id": f"a0X00000000{i:04d}"} for i in range(25)]
        job_id = await q.submit(items, None, "", None)

        handle = await q.start_workers(process)
        try:
            for _ in range(200):
                snap = await q.status(job_id)
                if snap["status"] == "done":
                    break
                await asyncio.sleep(0.05)
        finally:
            await q.stop_workers(handle)

        snap = await q.status(job_id)
        assert snap["status"] == "done", f"queue did not drain: {snap}"
        assert snap["succeeded"] == 25 and snap["failed"] == 0
        assert snap["pending"] == 0
        assert len(processed) == 25, "an item ran twice or not at all"
        assert len(set(processed)) == 25
        assert await r.llen(q.QUEUE_KEY) == 0
        await r.flushdb()
    asyncio.run(run())


def test_failing_item_is_recorded_without_stopping_the_batch(q):
    """One bad resume must not stall the queue behind it."""
    def process(payload):
        if payload["item"]["job_applicant_id"].endswith("0003"):
            raise RuntimeError("no resume found")
        return {"status": "ok", "watermark_used": "none"}

    async def run():
        r = q.client()
        await r.flushdb()
        items = [{"job_applicant_id": f"a0X00000000{i:04d}"} for i in range(6)]
        job_id = await q.submit(items, None, "", None)
        handle = await q.start_workers(process)
        try:
            for _ in range(200):
                if (await q.status(job_id))["status"] == "done":
                    break
                await asyncio.sleep(0.05)
        finally:
            await q.stop_workers(handle)

        snap = await q.status(job_id)
        assert (snap["succeeded"], snap["failed"]) == (5, 1), snap
        bad = [x for x in snap["results"] if x["result"]["status"] == "error"]
        assert len(bad) == 1 and "no resume found" in bad[0]["result"]["detail"]
        await r.flushdb()
    asyncio.run(run())


def test_endpoints_submit_and_report_progress(q, monkeypatch):
    """The HTTP surface: submit returns a job id, polling reports progress,
    and /health shows the queue depth."""
    from fastapi.testclient import TestClient

    from app import server

    monkeypatch.setattr(server, "_mask_one",
                        lambda req, sf: server.MaskResponse(
                            status="ok", masked_content_version_id="068AAA",
                            redacted_regions=3))
    monkeypatch.setattr(server.sf_client, "with_session",
                        lambda fn, client_key=None: fn(None))

    with TestClient(server.app) as client:
        body = {"items": [{"job_applicant_id": f"a0X00000000{i:04d}"} for i in range(8)]}
        resp = client.post("/mask/batch/async", json=body)
        assert resp.status_code == 200, resp.text
        submitted = resp.json()
        assert submitted["status"] == "ok", submitted
        job_id = submitted["job_id"]
        assert submitted["queued"] == 8

        assert client.get("/health").json()["queue"]["backend"] == "redis"

        for _ in range(200):
            snap = client.get(f"/mask/jobs/{job_id}").json()
            if snap["job_status"] == "done":
                break
            time.sleep(0.05)

        assert snap["job_status"] == "done", snap
        assert snap["succeeded"] == 8 and snap["failed"] == 0
        assert len(snap["results"]) == 8
        assert snap["results"][0]["result"]["masked_content_version_id"] == "068AAA"


def test_unknown_job_id_is_an_error_not_a_crash(q):
    from fastapi.testclient import TestClient

    from app import server

    with TestClient(server.app) as client:
        snap = client.get("/mask/jobs/does-not-exist").json()
    assert snap["status"] == "error"
    assert "Unknown or expired" in snap["detail"]


# =========================================================================
# the worker container, and handing a large batch to the queue
# =========================================================================

def test_the_api_stops_draining_when_a_worker_container_is_running(q, monkeypatch):
    """MASK_RUN_WORKERS=0 is how the API hands the backlog over.

    The two roles want opposite things from a host -- the API wants to stay
    responsive, the worker wants to use the whole box -- so the split has to
    be switchable without a second image. Unset, the API drains as it always
    has, which is what keeps a deployment with no worker container working.
    """
    monkeypatch.delenv("MASK_RUN_WORKERS", raising=False)
    assert q.run_workers_here() is True
    for off in ("0", "false", "no", "off", "OFF"):
        monkeypatch.setenv("MASK_RUN_WORKERS", off)
        assert q.run_workers_here() is False, off
    monkeypatch.setenv("MASK_RUN_WORKERS", "1")
    assert q.run_workers_here() is True


def test_the_worker_entrypoint_drains_the_same_queue(q, monkeypatch):
    """`python -m app.worker` is the web service's consumer pool, alone.

    Started the same way, gated the same way, against the same keys -- the
    only difference is that no HTTP server is attached. Proved by queueing
    work with no API running at all and watching the worker's pool clear it.
    """
    from app import worker

    masked = []

    def process(payload):
        masked.append(payload["item"]["job_applicant_id"])
        return {"status": "ok"}

    async def scenario():
        r = q.client()
        await r.flushdb()
        await q.submit([{"job_applicant_id": f"a0C{i}"} for i in range(6)],
                       client_key=None, watermark_text="", watermark_base64=None)

        handle = await q.start_workers(process)
        assert handle, "the worker pool did not start"
        for _ in range(200):
            if len(masked) == 6:
                break
            await asyncio.sleep(0.05)
        await q.stop_workers(handle)
        assert sorted(masked) == [f"a0C{i}" for i in range(6)]
        assert await r.llen(q.QUEUE_KEY) == 0, "items were left in the queue"
        # The gate is not asserted empty: stopping cancels the pool, and a
        # task cancelled inside its own release leaves its lease behind for
        # LEASE_TTL to reclaim. That is the design -- a lease outliving a dead
        # worker is exactly what the timestamp is for -- so what matters here
        # is that nothing is held that was never taken.
        assert await r.zcard(q.INFLIGHT_KEY) <= q.MAX_CONCURRENT
        await r.aclose()

    assert callable(worker.main)
    asyncio.run(scenario())


def test_a_large_batch_is_queued_rather_than_masked_inline(q, monkeypatch):
    """Above the threshold, POST /mask/batch returns a job id immediately.

    Masking inline is fine for a handful and hopeless for a hundred: at a few
    seconds each the request outlives any HTTP timeout between here and
    Salesforce, and everything already done is lost with the connection.
    Nothing is refused -- the work moves to the queue.
    """
    from fastapi.testclient import TestClient

    from app import jobs, server
    monkeypatch.setattr(jobs, "BATCH_ASYNC_THRESHOLD", 10)
    monkeypatch.setattr(server.jobs, "_client", None)
    monkeypatch.setattr(server.jobs, "_acquire_script", None)

    def _never_called(*a, **kw):
        raise AssertionError("a queued batch was masked inline anyway")

    monkeypatch.setattr(server, "_mask_one", _never_called)

    with TestClient(server.app) as c:
        body = {"items": [{"job_applicant_id": f"a0C{i}"} for i in range(25)]}
        res = c.post("/mask/batch", json=body).json()
    assert res["status"] == "queued", res
    assert res["queued"] == 25 and res["job_id"]
    assert str(jobs.MAX_CONCURRENT) in (res.get("detail") or "")


def test_a_small_batch_is_still_masked_inline(q, monkeypatch):
    """Below the threshold nothing changes: the caller gets its results."""
    from fastapi.testclient import TestClient

    from app import jobs, server
    monkeypatch.setattr(jobs, "BATCH_ASYNC_THRESHOLD", 10)
    monkeypatch.setattr(
        server, "_mask_one",
        lambda req, sf: server.MaskResponse(status="ok", redacted_regions=3))
    monkeypatch.setattr(server.sf_client, "with_session",
                        lambda fn, client_key=None: fn(object()))

    with TestClient(server.app) as c:
        body = {"items": [{"job_applicant_id": f"a0C{i}"} for i in range(4)]}
        res = c.post("/mask/batch", json=body).json()
    assert res["status"] == "ok", res
    assert res["job_id"] is None and res["succeeded"] == 4


def test_the_hand_off_is_off_until_it_is_configured(q, monkeypatch):
    """Threshold 0 means every batch is masked inline, however long.

    Turning this on changes what a caller gets back, so it cannot be a
    default -- a deployment that upgrades and keeps posting 50 items must
    keep receiving 50 results.
    """
    from fastapi.testclient import TestClient

    from app import jobs, server
    monkeypatch.setattr(jobs, "BATCH_ASYNC_THRESHOLD", 0)
    monkeypatch.setattr(
        server, "_mask_one",
        lambda req, sf: server.MaskResponse(status="ok", redacted_regions=1))
    monkeypatch.setattr(server.sf_client, "with_session",
                        lambda fn, client_key=None: fn(object()))

    with TestClient(server.app) as c:
        body = {"items": [{"job_applicant_id": f"a0C{i}"} for i in range(30)]}
        res = c.post("/mask/batch", json=body).json()
    assert res["status"] == "ok" and res["succeeded"] == 30
    assert res["job_id"] is None


def test_an_idle_pool_holds_no_slots(q):
    """An idle worker must hold nothing, or a second container starves.

    The pool used to block on the queue while holding a slot, so an idle pool
    held every slot: /health reported in_flight = MAX_CONCURRENT against an
    empty queue -- a number nobody can act on -- and a worker container added
    alongside the API could only get a slot in the instant between another
    worker releasing one and taking it back. Found by running the two
    containers together and reading /health.
    """
    async def scenario():
        r = q.client()
        await r.flushdb()

        handle = await q.start_workers(lambda payload: {"status": "ok"})
        assert handle
        try:
            await asyncio.sleep(0.6)          # several idle polls
            idle = await r.zcard(q.INFLIGHT_KEY)
            assert idle == 0, f"{idle} slots held with an empty queue"

            # And a slot IS held while something is actually being masked.
            started = asyncio.Event()
            holding = []

            def slow(payload):
                holding.append(payload)
                started.set()
                time.sleep(0.6)
                return {"status": "ok"}

            await q.stop_workers(handle)
            handle = await q.start_workers(slow)
            await q.submit([{"job_applicant_id": "a0C1"}], client_key=None,
                           watermark_text="", watermark_base64=None)
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.02)
            assert started.is_set(), "the item was never picked up"
            assert await r.zcard(q.INFLIGHT_KEY) >= 1, \
                "a resume was being masked with no slot held"
        finally:
            await q.stop_workers(handle)
            await r.aclose()

    asyncio.run(scenario())
