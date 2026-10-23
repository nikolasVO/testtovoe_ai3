"""The four acceptance tests from the assignment, against real PostgreSQL."""
import asyncio

import pytest

pytestmark = pytest.mark.integration


async def test_20_concurrent_payment_webhooks_credit_once(env):
    payment = {"payment_id": "payment-20", "user_id": 1, "tokens": 17, "status": "succeeded"}
    responses = await asyncio.gather(*(env.signed("/webhooks/payments", payment) for _ in range(20)))
    assert all(r.status_code == 200 for r in responses)
    assert sum(r.json()["credited"] for r in responses) == 1
    account = (await env.api.get("/users/1/balance")).json()
    assert account["balance"] == account["ledger_sum"] == 117
    assert sum(r["payment_id"] == "payment-20" for r in account["ledger"]) == 1
    await env.assert_invariants()


async def test_10_concurrent_failures_on_last_leg_refund_once(env):
    task = await env.generate(refs=5)  # alpha cannot accept five references: beta is the only leg.
    await env.dispatcher.dispatch_one(task["attempt_id"])
    attempt = (await env.attempts(task["task_id"]))[0]
    responses = await asyncio.gather(*(env.callback(attempt) for _ in range(10)))
    assert all(r.status_code == 200 for r in responses)
    state = (await env.api.get(f"/tasks/{task['task_id']}")).json()
    account = (await env.api.get("/users/1/balance")).json()
    assert state["status"] == "failed" and state["result_url"] is None
    assert account["balance"] == 100
    assert sum(r["kind"] == "refund" for r in account["ledger"]) == 1
    await env.assert_invariants()


async def test_first_leg_fails_second_completes_with_two_attempts(env):
    task = await env.generate()
    await env.dispatcher.dispatch_one(task["attempt_id"])
    first = (await env.attempts(task["task_id"]))[0]
    assert (await env.callback(first)).status_code == 200
    await env.dispatcher.dispatch_one()
    second = (await env.attempts(task["task_id"]))[1]
    assert (await env.callback(second, "completed")).status_code == 200
    state = (await env.api.get(f"/tasks/{task['task_id']}")).json()
    assert state["status"] == "completed"
    assert [(a["provider"], a["status"]) for a in state["attempts"]] == [("alpha", "failed"), ("beta", "completed")]
    assert (await env.api.get("/users/1/balance")).json()["balance"] == 98
    await env.assert_invariants()


async def test_late_completed_after_watchdog_does_not_resurrect_refunded_task(env):
    task = await env.generate(refs=5)
    await env.dispatcher.dispatch_one(task["attempt_id"])
    attempt = (await env.attempts(task["task_id"]))[0]
    await env.expire(attempt["id"])
    assert await env.watchdog.tick()
    response = await env.callback(attempt, "completed")
    assert response.status_code == 200 and response.json()["disposition"] == "ignored_late"
    state = (await env.api.get(f"/tasks/{task['task_id']}")).json()
    assert state["status"] == "failed" and state["result_url"] is None
    assert "https://files.test/result.png" not in str(state)
    assert (await env.api.get("/users/1/balance")).json()["balance"] == 100
    await env.assert_invariants()
