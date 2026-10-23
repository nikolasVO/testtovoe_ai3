"""Optional local failure lab. Every run gets a fresh user and isolated provider scope."""
import asyncio
import json
import secrets
from typing import Literal
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Request

from app import readmodel
from app.domain import DomainError
from app.http_security import require_admin
from app.schemas import Generate, Payment, StrictModel
from app.security import sign
from providers.schemas import Behavior

router = APIRouter(prefix="/demo/runs", tags=["Local failure lab"])


class DemoRequest(StrictModel):
    scenario: Literal["success", "fallback", "duplicate_payment", "duplicate_refund", "late",
                      "moderation", "payload", "silent", "sync_timeout"] = "fallback"


def plan(scenario: str, timeout: float):
    alpha = Behavior(delay_min=2, delay_max=2)
    beta = Behavior(delay_min=2, delay_max=2)
    model, refs = "image-fast", []
    if scenario == "fallback":
        alpha.sync_error = "503"
    elif scenario == "duplicate_refund":
        beta.failure_rate, beta.duplicates = 1, 10
        refs = [f"https://refs.example.test/{i}.png" for i in range(5)]
    elif scenario == "late":
        beta.late_delay = timeout + 3
        refs = [f"https://refs.example.test/{i}.png" for i in range(5)]
    elif scenario == "silent":
        beta.drop_webhook = True
        refs = [f"https://refs.example.test/{i}.png" for i in range(5)]
    elif scenario == "moderation":
        model = "video-long"
        beta.failure_rate, beta.error_type = 1, "moderation"
    elif scenario == "payload":
        alpha.failure_rate, alpha.error_type = 1, "payload"
    elif scenario == "sync_timeout":
        alpha.sync_error = "timeout"
    return alpha, beta, model, refs


@router.post("", status_code=202)
async def start(body: DemoRequest, request: Request):
    require_admin(request)
    state = request.app.state
    settings = state.settings
    run_id = uuid4()
    # Stay below JavaScript's 2**53-1 integer limit for this optional browser demo.
    user_id = secrets.randbits(48) + 10000
    scope = str(run_id)
    alpha, beta, model, refs = plan(body.scenario, settings.provider_timeout)
    try:
        for name, behavior in (("alpha", alpha), ("beta", beta)):
            response = await state.client.post(
                f"{settings.provider_url(name)}/admin/config", params={"scope": scope},
                headers={"X-Admin-Key": settings.admin_key}, json=behavior.model_dump(), timeout=10,
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise DomainError(502, "Demo providers are unavailable; check compose services") from exc
    payment = Payment(payment_id=f"demo:{run_id}", user_id=user_id, tokens=100, status="succeeded")
    if body.scenario == "duplicate_payment":
        raw = json.dumps(payment.model_dump(), separators=(",", ":")).encode()
        responses = await asyncio.gather(*(
            state.client.post(
                f"{settings.public_base_url.rstrip('/')}/webhooks/payments", content=raw,
                headers={"Content-Type": "application/json", "X-Signature": sign(raw, settings.webhook_secret)},
                timeout=20,
            ) for _ in range(20)
        ))
        if any(r.status_code != 200 for r in responses):
            raise DomainError(502, "One or more duplicate-payment requests failed")
        task_id = None
    else:
        await state.service.payment(payment)
        accepted = await state.service.generate(
            Generate(user_id=user_id, model_slug=model, prompt="Failure lab: a quiet mountain lake", ref_images=refs),
            key=f"demo:{run_id}", request_id=request.state.request_id, scenario_key=scope,
        )
        task_id = accepted["task_id"]
        await state.dispatcher.dispatch_one(accepted["attempt_id"])
    async with state.pool.acquire() as conn:
        await conn.execute("INSERT INTO demo_runs(id,user_id,task_id,scenario) VALUES($1,$2,$3,$4)",
                           run_id, user_id, task_id, body.scenario)
    return {"run_id": run_id, "user_id": user_id, "task_id": task_id, "scenario": body.scenario,
            "provider_timeout": settings.provider_timeout}


@router.get("/{run_id}")
async def inspect(run_id: UUID, request: Request):
    require_admin(request)
    state = request.app.state
    async with state.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM demo_runs WHERE id=$1", run_id)
    if row is None:
        raise DomainError(404, "Run not found")
    task = await readmodel.task(state.pool, row["task_id"]) if row["task_id"] else None
    account = await readmodel.balance(state.pool, row["user_id"])
    counts = {kind: sum(r["kind"] == kind for r in account["ledger"])
              for kind in ("payment", "charge", "refund")}
    finished = task is None or task["status"] != "active"
    if row["scenario"] == "late" and task:
        finished = any(e["kind"] == "ignored_late" for e in task["events"])
    return {"run": dict(row), "task": task, "account": account, "counts": counts, "finished": finished}
