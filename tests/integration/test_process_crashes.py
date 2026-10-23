import asyncio
import os
import sys

import httpx
import pytest

from app.dispatch import Dispatcher
from providers.schemas import TaskRequest
from providers.service import FakeProvider

pytestmark = [pytest.mark.integration, pytest.mark.chaos]


async def kill_at_barrier(mode, **extra):
    child_env = {**os.environ, **extra}
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "tests.integration.kill_worker", mode,
        env=child_env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        line = await asyncio.wait_for(process.stdout.readline(), timeout=20)
        if line.strip() != b"READY_TO_KILL":
            details = (await process.stderr.read()).decode()
            raise AssertionError(f"Child did not reach barrier: {line!r}\n{details}")
        process.kill()  # SIGKILL on Linux: no Python finally / cleanup / compensating handler.
        await asyncio.wait_for(process.wait(), timeout=10)
        assert process.returncode != 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.parametrize("mode,before_retry", [("before_commit", 100), ("after_commit", 107)])
async def test_sigkill_during_payment_processing_is_safe_to_retry(env, mode, before_retry):
    await kill_at_barrier(mode)
    account = (await env.api.get("/users/1/balance")).json()
    assert account["balance"] == before_retry
    payment = {"payment_id": "crash-payment", "user_id": 1, "tokens": 7, "status": "succeeded"}
    responses = await asyncio.gather(*(env.signed("/webhooks/payments", payment) for _ in range(20)))
    assert all(r.status_code == 200 for r in responses)
    account = (await env.api.get("/users/1/balance")).json()
    assert account["balance"] == 107
    assert sum(r["kind"] == "payment" for r in account["ledger"]) == 1
    await env.assert_invariants()


async def test_sigkill_after_provider_acceptance_reuses_external_job(env):
    task = await env.generate()
    await kill_at_barrier("after_accept", CRASH_ATTEMPT_ID=str(task["attempt_id"]))
    async with env.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM fake_jobs") == 1
        await conn.execute("UPDATE outbox SET lease_until=clock_timestamp()-interval '1 second' WHERE attempt_id=$1", task["attempt_id"])
    provider = FakeProvider(env.pool, env.settings, env.client)
    async def transport(request):
        external_id, _ = await provider.accept(TaskRequest.model_validate_json(request.content), request.headers["Idempotency-Key"])
        return httpx.Response(202, json={"external_id": external_id})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        await Dispatcher(env.service, client).dispatch_one(task["attempt_id"])
    async with env.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM fake_jobs") == 1
        job = await conn.fetchrow("SELECT * FROM fake_jobs")
        assert await conn.fetchval("SELECT deliveries FROM outbox WHERE attempt_id=$1", task["attempt_id"]) == 2
    response = await env.signed("/webhooks/providers/alpha", job["callback"])
    assert response.status_code == 200
    assert (await env.api.get(f"/tasks/{task['task_id']}")).json()["status"] == "completed"
    await env.assert_invariants()
