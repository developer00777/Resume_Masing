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
