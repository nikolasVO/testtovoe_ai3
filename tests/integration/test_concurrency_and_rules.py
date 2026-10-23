import asyncio
import json

import httpx
import pytest

from app.dispatch import Dispatcher
from app.domain import DomainError
from app.schemas import Generate, Payment

pytestmark = pytest.mark.integration


async def test_100_concurrent_generations_allow_only_three_active_tasks(env):
    results = await asyncio.gather(*(env.generate() for _ in range(100)), return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 3
    assert all(isinstance(r, dict) or isinstance(r, DomainError) and r.status == 429 for r in results)
    assert (await env.api.get("/users/1/balance")).json()["balance"] == 94
    await env.assert_invariants()


async def test_concurrent_admission_cannot_overspend(env):
    await env.service.payment(Payment(payment_id="small", user_id=2, tokens=2, status="succeeded"))
    results = await asyncio.gather(*(env.generate(user=2) for _ in range(30)), return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 1
    assert all(isinstance(r, dict) or isinstance(r, DomainError) and r.status == 402 for r in results)
    assert (await env.api.get("/users/2/balance")).json()["balance"] == 0
    await env.assert_invariants()


async def test_no_matching_leg_fails_before_charge(env):
    with pytest.raises(DomainError) as error:
        await env.generate(refs=11)
    assert error.value.status == 400
    account = (await env.api.get("/users/1/balance")).json()
    assert account["balance"] == 100 and len(account["ledger"]) == 1


@pytest.mark.parametrize("model,error", [("video-long", "moderation"), ("image-fast", "payload")])
async def test_terminal_errors_do_not_try_forbidden_next_leg(env, model, error):
    task = await env.generate(model=model)
    await env.dispatcher.dispatch_one(task["attempt_id"])
    attempt = (await env.attempts(task["task_id"]))[0]
    await env.callback(attempt, error=error)
    state = (await env.api.get(f"/tasks/{task['task_id']}")).json()
    assert state["status"] == "failed" and len(state["attempts"]) == 1
    assert (await env.api.get("/users/1/balance")).json()["balance"] == 100
    await env.assert_invariants()


async def test_moderation_can_fallback_to_non_skipping_leg(env):
    task = await env.generate()
    await env.dispatcher.dispatch_one(task["attempt_id"])
    await env.callback((await env.attempts(task["task_id"]))[0], error="moderation")
    attempts = await env.attempts(task["task_id"])
    assert len(attempts) == 2 and attempts[1]["provider"] == "beta"
    await env.assert_invariants()


@pytest.mark.parametrize("mode", ["503", "timeout", "bad_response"])
async def test_synchronous_failure_uses_same_fallback_path(env, mode):
    async def transport(request):
        payload = json.loads(request.content)
        if request.url.host == "alpha":
            if mode == "timeout":
                raise httpx.ReadTimeout("injected timeout", request=request)
            return httpx.Response(503) if mode == "503" else httpx.Response(202, json={"no_id": True})
        return httpx.Response(202, json={"external_id": f"ext-{payload['attempt_id']}"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        dispatcher = Dispatcher(env.service, client)
        task = await env.generate()
        await dispatcher.dispatch_one(task["attempt_id"])
        await dispatcher.dispatch_one()
    attempts = await env.attempts(task["task_id"])
    assert len(attempts) == 2 and attempts[0]["error_type"] == "technical"
    await env.callback(attempts[1], "completed")
    await env.assert_invariants()


async def test_four_watchdogs_do_not_duplicate_fallback_or_refund(env):
    task = await env.generate()
    await env.dispatcher.dispatch_one(task["attempt_id"])
    first = (await env.attempts(task["task_id"]))[0]
    await env.expire(first["id"])
    assert sum(await asyncio.gather(*(env.watchdog.tick() for _ in range(4)))) == 1
    attempts = await env.attempts(task["task_id"])
    assert len(attempts) == 2
    await env.dispatcher.dispatch_one(attempts[1]["id"])
    await env.expire(attempts[1]["id"])
    assert sum(await asyncio.gather(*(env.watchdog.tick() for _ in range(4)))) == 1
    account = (await env.api.get("/users/1/balance")).json()
    assert sum(r["kind"] == "refund" for r in account["ledger"]) == 1
    await env.assert_invariants()


@pytest.mark.parametrize("expired", [True, False])
async def test_watchdog_and_completed_race_has_one_financial_outcome(env, expired):
    task = await env.generate(refs=5)
    await env.dispatcher.dispatch_one(task["attempt_id"])
    attempt = (await env.attempts(task["task_id"]))[0]
    if expired:
        await env.expire(attempt["id"])
    await asyncio.gather(env.watchdog.tick(), env.callback(attempt, "completed"))
    state = (await env.api.get(f"/tasks/{task['task_id']}")).json()
    assert state["status"] == ("failed" if expired else "completed")
    await env.assert_invariants()


async def test_conflicting_terminal_events_have_a_single_winner(env):
    for _ in range(12):
        task = await env.generate(refs=5)
        await env.dispatcher.dispatch_one(task["attempt_id"])
        attempt = (await env.attempts(task["task_id"]))[0]
        responses = await asyncio.gather(env.callback(attempt, "completed"), env.callback(attempt, "failed"))
        assert all(r.status_code == 200 for r in responses)
        await env.assert_invariants()


async def test_early_callback_before_http_202_is_not_lost(env):
    async def transport(request):
        payload = json.loads(request.content)
        attempt = (await env.attempts(task["task_id"]))[0]
        assert attempt["status"] == "sending" and attempt["external_id"] is None
        assert (await env.callback(attempt, "completed")).status_code == 200
        return httpx.Response(202, json={"external_id": f"ext-{payload['attempt_id']}"})
    task = await env.generate()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        await Dispatcher(env.service, client).dispatch_one(task["attempt_id"])
    assert (await env.api.get(f"/tasks/{task['task_id']}")).json()["status"] == "completed"
    await env.assert_invariants()


async def test_same_generate_key_charges_once_under_contention(env):
    results = await asyncio.gather(*(env.generate(key="same-request") for _ in range(20)))
    assert len({r["task_id"] for r in results}) == 1
    assert sum(not r["replayed"] for r in results) == 1
    account = (await env.api.get("/users/1/balance")).json()
    assert sum(r["kind"] == "charge" for r in account["ledger"]) == 1
    with pytest.raises(DomainError) as error:
        await env.service.generate(Generate(user_id=1, model_slug="image-fast", prompt="different"), "same-request")
    assert error.value.status == 409


async def test_payment_id_reuse_with_other_amount_or_user_is_conflict(env):
    await env.service.payment(Payment(payment_id="p", user_id=1, tokens=7, status="succeeded"))
    for user, amount in [(1, 8), (2, 7)]:
        response = await env.signed("/webhooks/payments", {"payment_id": "p", "user_id": user, "tokens": amount, "status": "succeeded"})
        assert response.status_code == 409
    assert (await env.api.get("/users/1/balance")).json()["balance"] == 107
    await env.assert_invariants()


async def test_payment_statuses_are_monotonic_and_only_success_credits(env):
    for status, expected in [("pending", 100), ("failed", 100), ("succeeded", 107), ("pending", 107), ("failed", 107), ("succeeded", 107)]:
        await env.service.payment(Payment(payment_id="p", user_id=1, tokens=7, status=status))
        assert (await env.api.get("/users/1/balance")).json()["balance"] == expected
    await env.assert_invariants()


async def test_task_uses_price_and_leg_snapshot(env):
    task = await env.generate(refs=4)
    async with env.pool.acquire() as conn:
        await conn.execute("UPDATE models SET price=999 WHERE slug='image-fast'")
        await conn.execute("UPDATE legs SET max_ref_images=0 WHERE model_slug='image-fast'")
    await env.dispatcher.dispatch_one(task["attempt_id"])
    await env.callback((await env.attempts(task["task_id"]))[0])
    attempts = await env.attempts(task["task_id"])
    assert len(attempts) == 2
    await env.dispatcher.dispatch_one(attempts[1]["id"])
    await env.callback((await env.attempts(task["task_id"]))[1])
    account = (await env.api.get("/users/1/balance")).json()
    assert account["balance"] == 100
    assert next(r["delta"] for r in account["ledger"] if r["kind"] == "refund") == 2
    await env.assert_invariants()
