"""The simulator is stateful in PostgreSQL too; HTTP retries reuse the same job."""
import asyncio
import json
import logging
import random
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx

from app.domain import DomainError, fingerprint
from app.security import sign
from providers.schemas import Behavior, TaskRequest

log = logging.getLogger(__name__)


class FakeProvider:
    def __init__(self, pool, settings, client):
        self.pool, self.settings, self.client = pool, settings, client
        self.name = settings.provider_name

    async def configure(self, scope: str, config: Behavior):
        async with self.pool.acquire() as conn:
            await conn.execute("""INSERT INTO fake_configs(provider,scope,config) VALUES($1,$2,$3)
                                ON CONFLICT(provider,scope) DO UPDATE SET config=EXCLUDED.config""",
                               self.name, scope, config.model_dump())

    async def accept(self, body: TaskRequest, key: str):
        if key != str(body.attempt_id):
            raise DomainError(400, "Idempotency-Key must equal attempt_id")
        expected = f"{self.settings.public_base_url.rstrip('/')}/webhooks/providers/{self.name}"
        if str(body.callback_url) != expected:
            raise DomainError(400, "callback_url must be the configured orchestrator URL")
        payload = body.model_dump(mode="json")
        digest = fingerprint(payload)
        async with self.pool.acquire() as conn, conn.transaction():
            existing = await conn.fetchrow("SELECT * FROM fake_jobs WHERE provider=$1 AND idempotency_key=$2",
                                           self.name, key)
            if existing:
                if existing["request_hash"] != digest:
                    raise DomainError(409, "Idempotency key conflicts with earlier payload")
                return existing["external_id"], Behavior.model_validate(existing["behavior"])
            config = await conn.fetchval("SELECT config FROM fake_configs WHERE provider=$1 AND scope=$2",
                                         self.name, body.scenario_key or "default")
            behavior = Behavior.model_validate(config or {})
            if behavior.sync_error == "503":
                raise DomainError(503, "Configured synchronous failure")
            external_id = str(uuid5(NAMESPACE_URL, f"{self.name}:{key}"))
            failed = random.random() < behavior.failure_rate
            callback = {"attempt_id": str(body.attempt_id), "external_id": external_id,
                        "status": "failed" if failed else "completed"}
            if failed:
                callback["error_type"] = behavior.error_type
            else:
                callback["result_url"] = f"https://files.example.test/{self.name}/{external_id}.bin"
            delay = behavior.late_delay or random.uniform(behavior.delay_min, behavior.delay_max)
            if behavior.sync_error == "timeout":
                # Simulate the ambiguous case: accepted, but the 202 never arrives in time.
                delay = max(delay, self.settings.http_timeout + 3)
            await conn.execute("""INSERT INTO fake_jobs(provider,idempotency_key,external_id,request_hash,
                                payload,behavior,callback,due_at)
                                VALUES($1,$2,$3,$4,$5,$6,$7,clock_timestamp()+$8::double precision*interval '1 second')
                                ON CONFLICT(provider,idempotency_key) DO NOTHING""",
                               self.name, key, external_id, digest, payload, behavior.model_dump(), callback, delay)
            winner = await conn.fetchrow("SELECT * FROM fake_jobs WHERE provider=$1 AND idempotency_key=$2",
                                         self.name, key)
            if winner["request_hash"] != digest:
                raise DomainError(409, "Concurrent conflicting payload")
            return winner["external_id"], Behavior.model_validate(winner["behavior"])

    async def deliver_one(self) -> bool:
        async with self.pool.acquire() as conn, conn.transaction():
            job = await conn.fetchrow("""SELECT * FROM fake_jobs WHERE provider=$1 AND delivered_at IS NULL
                                     AND due_at<=clock_timestamp()
                                     AND NOT (behavior->>'drop_webhook')::boolean
                                     AND (lease_until IS NULL OR lease_until<=clock_timestamp())
                                     ORDER BY due_at LIMIT 1 FOR UPDATE SKIP LOCKED""", self.name)
            if job is None:
                return False
            token = uuid4()
            await conn.execute("""UPDATE fake_jobs SET lease_token=$3,
                                lease_until=clock_timestamp()+$4::double precision*interval '1 second',
                                deliveries=deliveries+1 WHERE provider=$1 AND idempotency_key=$2""",
                               self.name, job["idempotency_key"], token, self.settings.lease_seconds)
        raw = json.dumps(job["callback"], ensure_ascii=False, separators=(",", ":")).encode()
        behavior = Behavior.model_validate(job["behavior"])

        async def send_once():
            try:
                response = await self.client.post(
                    job["payload"]["callback_url"], content=raw,
                    headers={"Content-Type": "application/json", "X-Signature": sign(raw, self.settings.webhook_secret)},
                    timeout=self.settings.http_timeout,
                )
                return 200 <= response.status_code < 300
            except httpx.HTTPError:
                return False

        if behavior.concurrent_duplicates:
            results = await asyncio.gather(*(send_once() for _ in range(behavior.duplicates)))
        else:
            results = [await send_once() for _ in range(behavior.duplicates)]
        async with self.pool.acquire() as conn:
            if all(results):
                await conn.execute("""UPDATE fake_jobs SET delivered_at=clock_timestamp(),lease_until=NULL
                                    WHERE provider=$1 AND idempotency_key=$2 AND lease_token=$3""",
                                   self.name, job["idempotency_key"], token)
            else:
                await conn.execute("""UPDATE fake_jobs SET lease_until=NULL,
                                    due_at=clock_timestamp()+interval '1 second'
                                    WHERE provider=$1 AND idempotency_key=$2 AND lease_token=$3""",
                                   self.name, job["idempotency_key"], token)
        return True
