"""Durable outbox with expiring ownership, fencing and provider idempotency keys."""
import logging
from uuid import UUID, uuid4

import httpx

from app.domain import is_current
from app.store import event, lock_attempt

log = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, service, client: httpx.AsyncClient):
        self.service, self.client = service, client
        self.pool, self.settings = service.pool, service.settings

    async def claim(self, attempt_id: UUID | None = None):
        # Claiming an outbox row never acquires a user lock in this transaction.
        # Commit before entering the normal user -> task -> attempt -> outbox order.
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """SELECT * FROM outbox WHERE state='pending'
                   AND available_at<=clock_timestamp()
                   AND (lease_until IS NULL OR lease_until<=clock_timestamp())
                   AND ($1::uuid IS NULL OR attempt_id=$1)
                   ORDER BY available_at,attempt_id LIMIT 1 FOR UPDATE SKIP LOCKED""", attempt_id,
            )
            if row is None:
                return None
            token = uuid4()
            await conn.execute(
                """UPDATE outbox SET lease_token=$2,
                   lease_until=clock_timestamp()+$3::double precision*interval '1 second',
                   deliveries=deliveries+1 WHERE attempt_id=$1""",
                row["attempt_id"], token, self.settings.lease_seconds,
            )
            return row["attempt_id"], token

    async def prepare(self, claim):
        attempt_id, token = claim
        async with self.pool.acquire() as conn, conn.transaction():
            task, attempt = await lock_attempt(conn, attempt_id)
            job = await conn.fetchrow("SELECT * FROM outbox WHERE attempt_id=$1 FOR UPDATE", attempt_id)
            if job["state"] != "pending" or job["lease_token"] != token:
                return None
            if not is_current(task, attempt):
                await conn.execute("UPDATE outbox SET state='done' WHERE attempt_id=$1", attempt_id)
                return None
            if attempt["expired"]:
                await self.service.fail_locked(conn, task, attempt, "technical", "provider_timeout", True)
                return None
            await conn.execute(
                """UPDATE attempts SET status='sending',started_at=COALESCE(started_at,clock_timestamp()),
                   deadline=COALESCE(deadline,clock_timestamp()+$2::double precision*interval '1 second')
                   WHERE id=$1""", attempt_id, self.settings.provider_timeout,
            )
            await event(conn, task["id"], "dispatch_started", attempt_id,
                        provider=attempt["provider"], delivery=job["deliveries"])
            payload = {
                "attempt_id": str(attempt_id), "model_slug": task["model_slug"],
                "prompt": task["prompt"], "ref_images": task["ref_images"],
                "callback_url": f"{self.settings.public_base_url.rstrip('/')}/webhooks/providers/{attempt['provider']}",
                "scenario_key": task["scenario_key"],
            }
            return attempt["provider"], payload

    async def finish(self, claim, *, external_id: str | None = None,
                     error: str | None = None, detail: str = ""):
        attempt_id, token = claim
        async with self.pool.acquire() as conn, conn.transaction():
            task, attempt = await lock_attempt(conn, attempt_id)
            job = await conn.fetchrow("SELECT * FROM outbox WHERE attempt_id=$1 FOR UPDATE", attempt_id)
            if job["lease_token"] != token or job["state"] == "done":
                return
            if not is_current(task, attempt):
                await conn.execute("UPDATE outbox SET state='done' WHERE attempt_id=$1", attempt_id)
                return
            if attempt["expired"]:
                await self.service.fail_locked(conn, task, attempt, "technical", "provider_timeout", True)
            elif error:
                await self.service.fail_locked(conn, task, attempt, error, detail)
            else:
                await conn.execute(
                    "UPDATE attempts SET status='waiting',external_id=$2 WHERE id=$1",
                    attempt_id, external_id,
                )
                await conn.execute("UPDATE outbox SET state='done' WHERE attempt_id=$1", attempt_id)
                await event(conn, task["id"], "provider_accepted", attempt_id, provider=attempt["provider"])

    async def dispatch_one(self, attempt_id: UUID | None = None) -> bool:
        claim = await self.claim(attempt_id)
        if claim is None:
            return False
        prepared = await self.prepare(claim)
        if prepared is None:
            return True
        provider, payload = prepared
        # No SQL transaction / row lock is held across network I/O.
        try:
            response = await self.client.post(
                f"{self.settings.provider_url(provider)}/tasks", json=payload,
                headers={"Idempotency-Key": str(claim[0])}, timeout=self.settings.http_timeout,
            )
        except httpx.HTTPError as exc:
            await self.finish(claim, error="technical", detail=type(exc).__name__)
            return True
        if response.status_code >= 500 or response.status_code in (408, 429):
            await self.finish(claim, error="technical", detail=f"http_{response.status_code}")
        elif 400 <= response.status_code < 500:
            await self.finish(claim, error="payload", detail=f"http_{response.status_code}")
        else:
            try:
                external_id = response.json()["external_id"]
                if response.status_code != 202 or not isinstance(external_id, str) or not 1 <= len(external_id) <= 200:
                    raise ValueError("Invalid provider acknowledgement")
            except (ValueError, KeyError, TypeError):
                await self.finish(claim, error="technical", detail="invalid_provider_response")
            else:
                await self.finish(claim, external_id=external_id)
        return True
