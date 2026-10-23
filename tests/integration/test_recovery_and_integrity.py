import asyncio
import json

import httpx
import pytest

import app.service as service_module
from app.dispatch import Dispatcher
from app.ledger import post_entry

pytestmark = pytest.mark.integration


async def test_outbox_is_claimed_once_and_old_owner_is_fenced(env):
    task = await env.generate()
    claims = await asyncio.gather(*(env.dispatcher.claim(task["attempt_id"]) for _ in range(20)))
    assert sum(c is not None for c in claims) == 1
    old = next(c for c in claims if c)
    await env.dispatcher.prepare(old)
    first_deadline = (await env.attempts(task["task_id"]))[0]["deadline"]
    async with env.pool.acquire() as conn:
        await conn.execute("UPDATE outbox SET lease_until=clock_timestamp()-interval '1 second' WHERE attempt_id=$1", task["attempt_id"])
    new = await env.dispatcher.claim(task["attempt_id"])
    assert old[1] != new[1]
    await env.dispatcher.prepare(new)
    await env.dispatcher.finish(old, error="technical", detail="stale sender must not fail new owner")
    current = (await env.attempts(task["task_id"]))[0]
    assert current["status"] == "sending" and current["deadline"] == first_deadline
    await env.dispatcher.finish(new, external_id=f"ext-{task['attempt_id']}")
    await env.callback((await env.attempts(task["task_id"]))[0], "completed")
    assert len(await env.attempts(task["task_id"])) == 1
    await env.assert_invariants()


async def test_http_send_holds_no_database_row_lock(env):
    async def transport(request):
        async with env.pool.acquire() as conn, conn.transaction():
            # NOWAIT would fail if the dispatcher kept its user lock across HTTP.
            assert await conn.fetchval("SELECT id FROM users WHERE id=1 FOR UPDATE NOWAIT") == 1
        payload = json.loads(request.content)
        return httpx.Response(202, json={"external_id": f"ext-{payload['attempt_id']}"})
    task = await env.generate()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        await Dispatcher(env.service, client).dispatch_one(task["attempt_id"])


async def test_failure_before_admission_commit_rolls_back_charge_task_and_outbox(env, monkeypatch):
    async def fail_event(*args, **kwargs):
        raise RuntimeError("injected crash before commit")
    monkeypatch.setattr(service_module, "event", fail_event)
    with pytest.raises(RuntimeError, match="injected crash"):
        await env.generate()
    async with env.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM tasks") == 0
        assert await conn.fetchval("SELECT count(*) FROM outbox") == 0
    assert (await env.api.get("/users/1/balance")).json()["balance"] == 100
    await env.assert_invariants()


@pytest.mark.parametrize("sql", [
    "UPDATE users SET balance=101 WHERE id=1",
    "INSERT INTO users(id,balance) VALUES(2,10)",
    "UPDATE ledger SET delta=101 WHERE user_id=1",
    "DELETE FROM ledger WHERE user_id=1",
])
async def test_direct_balance_and_ledger_mutations_are_rejected(env, sql):
    with pytest.raises(Exception):
        async with env.pool.acquire() as conn, conn.transaction():
            await conn.execute(sql)
    await env.assert_invariants()
    assert (await env.api.get("/users/1/balance")).json()["balance"] == 100


async def test_active_task_cannot_commit_a_refund_without_terminal_transition(env):
    task = await env.generate()
    with pytest.raises(Exception, match="Task/ledger invariant"):
        async with env.pool.acquire() as conn, conn.transaction():
            await conn.fetchrow("SELECT * FROM users WHERE id=1 FOR UPDATE")
            await post_entry(conn, user_id=1, delta=2, kind="refund", key=f"refund:{task['task_id']}", task_id=task["task_id"])
    assert (await env.api.get("/users/1/balance")).json()["balance"] == 98
    await env.assert_invariants()


async def test_succeeded_payment_cannot_commit_without_ledger_credit(env):
    with pytest.raises(Exception, match="Payment/ledger invariant"):
        async with env.pool.acquire() as conn, conn.transaction():
            await conn.execute("INSERT INTO payments(payment_id,user_id,tokens,status) VALUES('broken',1,7,'succeeded')")
    await env.assert_invariants()


async def test_duplicate_ledger_insert_does_not_execute_balance_trigger_twice(env):
    async with env.pool.acquire() as conn, conn.transaction():
        await conn.fetchrow("SELECT * FROM users WHERE id=1 FOR UPDATE")
        assert not await post_entry(conn, user_id=1, delta=100, kind="seed", key="seed:1")
    assert (await env.api.get("/users/1/balance")).json()["balance"] == 100


async def test_invalid_hmac_has_no_side_effect(env):
    response = await env.api.post("/webhooks/payments", json={"payment_id": "bad", "user_id": 1, "tokens": 10, "status": "succeeded"}, headers={"X-Signature": "0"*64})
    assert response.status_code == 401
    assert (await env.api.get("/users/1/balance")).json()["balance"] == 100


async def test_provider_cannot_settle_someone_elses_attempt(env):
    task = await env.generate()
    await env.dispatcher.dispatch_one(task["attempt_id"])
    attempt = (await env.attempts(task["task_id"]))[0]
    response = await env.signed("/webhooks/providers/beta", {"attempt_id": str(attempt["id"]), "external_id": attempt["external_id"], "status": "completed", "result_url": "https://files.test/x"})
    assert response.status_code == 409
    assert (await env.api.get(f"/tasks/{task['task_id']}")).json()["status"] == "active"


async def test_global_metrics_reflect_committed_database_state(env):
    task = await env.generate(refs=5)
    await env.dispatcher.dispatch_one(task["attempt_id"])
    await env.callback((await env.attempts(task["task_id"]))[0])
    response = await env.api.get("/metrics")
    assert response.status_code == 200
    assert 'generation_tasks{status="failed"} 1' in response.text
    assert "generation_balance_mismatches 0" in response.text
    assert 'generation_events_total{kind="refunded"} 1' in response.text
