import asyncio
from uuid import uuid4

import httpx
import pytest

from app.domain import DomainError
from providers.schemas import Behavior, TaskRequest
from providers.service import FakeProvider

pytestmark = pytest.mark.integration


def request(attempt_id=None, scope=None):
    return TaskRequest(attempt_id=attempt_id or uuid4(), model_slug="image-fast", prompt="test",
                       ref_images=[], callback_url="http://api/webhooks/providers/alpha", scenario_key=scope)


async def test_provider_acceptance_is_idempotent_even_concurrently(env):
    fake = FakeProvider(env.pool, env.settings, env.client)
    body = request()
    results = await asyncio.gather(*(fake.accept(body, str(body.attempt_id)) for _ in range(20)))
    assert len({external_id for external_id, _ in results}) == 1
    async with env.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM fake_jobs") == 1
    with pytest.raises(DomainError) as error:
        await fake.accept(body.model_copy(update={"prompt": "different"}), str(body.attempt_id))
    assert error.value.status == 409


async def test_provider_job_snapshots_configuration(env):
    fake = FakeProvider(env.pool, env.settings, env.client)
    await fake.configure("a", Behavior(failure_rate=1, error_type="moderation"))
    body = request(scope="a")
    first = await fake.accept(body, str(body.attempt_id))
    await fake.configure("a", Behavior(failure_rate=0))
    second = await fake.accept(body, str(body.attempt_id))
    assert first[0] == second[0] and second[1].failure_rate == 1
    async with env.pool.acquire() as conn:
        callback = await conn.fetchval("SELECT callback FROM fake_jobs")
    assert callback["error_type"] == "moderation"


async def test_provider_can_emit_simultaneous_signed_duplicates(env):
    task = await env.generate()
    claim = await env.dispatcher.claim(task["attempt_id"])
    prepared = await env.dispatcher.prepare(claim)
    received = []
    async def transport(req):
        from app.security import verify
        assert verify(req.content, req.headers["X-Signature"], env.settings.webhook_secret)
        received.append(req.content)
        return await env.api.post("/webhooks/providers/alpha", content=req.content,
                                  headers={"X-Signature": req.headers["X-Signature"]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        fake = FakeProvider(env.pool, env.settings, client)
        await fake.configure("default", Behavior(duplicates=10, delay_min=0, delay_max=0))
        await fake.accept(TaskRequest.model_validate(prepared[1]), str(task["attempt_id"]))
        assert await fake.deliver_one()
    assert len(received) == 10 and len(set(received)) == 1
    assert (await env.api.get(f"/tasks/{task['task_id']}")).json()["status"] == "completed"
    await env.assert_invariants()


async def test_drop_webhook_is_persistent_not_an_in_memory_timer(env):
    fake = FakeProvider(env.pool, env.settings, env.client)
    await fake.configure("default", Behavior(drop_webhook=True, delay_min=0, delay_max=0))
    body = request()
    await fake.accept(body, str(body.attempt_id))
    assert not await fake.deliver_one()
    restarted = FakeProvider(env.pool, env.settings, env.client)
    assert not await restarted.deliver_one()
    async with env.pool.acquire() as conn:
        assert await conn.fetchval("SELECT delivered_at FROM fake_jobs") is None


async def test_callback_target_cannot_be_arbitrary_url(env):
    fake = FakeProvider(env.pool, env.settings, env.client)
    body = request().model_copy(update={"callback_url": "http://169.254.169.254/latest/meta-data"})
    with pytest.raises(DomainError) as error:
        await fake.accept(body, str(body.attempt_id))
    assert error.value.status == 400
