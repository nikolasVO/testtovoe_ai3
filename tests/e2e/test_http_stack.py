"""Real TCP requests, four API processes, separate Alpha/Beta containers and PostgreSQL."""
import asyncio
import secrets
import time
from uuid import uuid4

import pytest

pytestmark = pytest.mark.e2e


async def test_four_distinct_api_worker_processes_respond(live):
    pids = set()
    until = time.monotonic() + 15
    while len(pids) < 4 and time.monotonic() < until:
        responses = await asyncio.gather(*(live.client.get("/health/live") for _ in range(40)))
        for response in responses:
            response.raise_for_status()
            pids.add(response.json()["worker_pid"])
    assert len(pids) >= 4, f"Expected four processes; saw {pids}"


async def test_twenty_simultaneous_signed_payments_over_http(live):
    user = secrets.randbits(48) + 10000
    payment = {"payment_id": str(uuid4()), "user_id": user, "tokens": 17, "status": "succeeded"}
    responses = await asyncio.gather(*(live.signed("/webhooks/payments", payment) for _ in range(20)))
    assert all(r.status_code == 200 for r in responses)
    assert sum(r.json()["credited"] for r in responses) == 1
    account = (await live.client.get(f"/users/{user}/balance")).json()
    assert account["balance"] == account["ledger_sum"] == 17
    assert len(account["ledger"]) == 1


async def test_ten_actual_provider_callbacks_refund_only_once(live):
    result = await live.run("duplicate_refund")
    await live.wait_delivered(result["task"]["attempts"][0])
    result = (await live.client.get(f"/demo/runs/{result['run']['id']}")).json()
    assert result["task"]["status"] == "failed"
    assert result["counts"] == {"payment": 1, "charge": 1, "refund": 1}
    assert result["account"]["balance"] == result["account"]["ledger_sum"] == 100


async def test_actual_alpha_503_falls_back_to_actual_beta(live):
    result = await live.run("fallback")
    assert result["task"]["status"] == "completed"
    assert [(a["provider"], a["status"]) for a in result["task"]["attempts"]] == [("alpha", "failed"), ("beta", "completed")]
    assert result["counts"]["refund"] == 0 and result["account"]["balance"] == 98


async def test_actual_late_success_after_watchdog_is_ignored(live):
    result = await live.run("late")
    await live.wait_delivered(result["task"]["attempts"][0])
    assert result["task"]["status"] == "failed" and result["task"]["result_url"] is None
    assert any(e["kind"] == "ignored_late" for e in result["task"]["events"])
    assert result["account"]["balance"] == 100 and result["counts"]["refund"] == 1


@pytest.mark.parametrize("scenario,error", [("moderation", "moderation"), ("payload", "payload"), ("silent", "technical")])
async def test_real_failure_modes(scenario, error, live):
    result = await live.run(scenario)
    assert result["task"]["status"] == "failed" and result["task"]["error_type"] == error
    assert len(result["task"]["attempts"]) == 1
    assert result["account"]["balance"] == 100 and result["counts"]["refund"] == 1


async def test_real_post_timeout_then_late_old_callback(live):
    result = await live.run("sync_timeout")
    assert result["task"]["status"] == "completed"
    attempts = result["task"]["attempts"]
    assert len(attempts) == 2 and attempts[0]["error_type"] == "technical"
    await live.wait_delivered(attempts[0])
    final = (await live.client.get(f"/tasks/{result['task']['task_id']}")).json()
    assert final["status"] == "completed" and final["result_url"] == result["task"]["result_url"]
    assert any(e["kind"] == "ignored_terminal" for e in final["events"])


async def test_demo_scopes_do_not_change_other_runs(live):
    fallback, payload = await asyncio.gather(live.run("fallback"), live.run("payload"))
    assert fallback["task"]["status"] == "completed" and payload["task"]["status"] == "failed"
    assert fallback["run"]["user_id"] != payload["run"]["user_id"]


async def test_optional_generate_idempotency_key_is_shared_across_workers(live):
    user = secrets.randbits(48) + 10000
    response = await live.signed("/webhooks/payments", {"payment_id": str(uuid4()), "user_id": user, "tokens": 100, "status": "succeeded"})
    response.raise_for_status()
    key = str(uuid4())
    body = {"user_id": user, "model_slug": "image-fast", "prompt": "one logical request", "ref_images": []}
    responses = await asyncio.gather(*(live.client.post("/generate", json=body, headers={"Idempotency-Key": key}) for _ in range(20)))
    assert all(r.status_code == 202 for r in responses)
    assert len({r.json()["task_id"] for r in responses}) == 1
    account = (await live.client.get(f"/users/{user}/balance")).json()
    assert account["balance"] == 98 and sum(r["kind"] == "charge" for r in account["ledger"]) == 1


async def test_metrics_report_global_consistent_state(live):
    response = await live.client.get("/metrics")
    assert response.status_code == 200
    assert "generation_balance_mismatches 0" in response.text
    assert "generation_task_duration_seconds_bucket" in response.text
