"""A shared state machine for callbacks, synchronous failures and watchdog timeouts."""
from uuid import UUID, uuid4

from app.config import Settings
from app.domain import DomainError, fingerprint, is_current, next_leg
from app.ledger import post_entry
from app.schemas import Generate, Payment, ProviderWebhook
from app.store import event, lock_attempt, lock_user


class GenerationService:
    def __init__(self, pool, settings: Settings):
        self.pool, self.settings = pool, settings

    async def generate(self, body: Generate, key: str | None = None,
                       request_id: str | None = None, scenario_key: str | None = None):
        digest = fingerprint(body.model_dump(mode="json"))
        async with self.pool.acquire() as conn, conn.transaction():
            user = await lock_user(conn, body.user_id)
            if key:
                existing = await conn.fetchrow(
                    "SELECT * FROM tasks WHERE user_id=$1 AND request_key=$2", body.user_id, key,
                )
                if existing:
                    if existing["request_hash"] != digest:
                        raise DomainError(409, "Idempotency-Key was used with another request")
                    return {"task_id": existing["id"], "replayed": True}
            model = await conn.fetchrow("SELECT * FROM models WHERE slug=$1", body.model_slug)
            if model is None:
                raise DomainError(404, "Model not found")
            legs = [dict(r) for r in await conn.fetch(
                """SELECT provider,priority,max_ref_images,skip_on_moderation
                   FROM legs WHERE model_slug=$1 ORDER BY priority""", body.model_slug,
            )]
            first = next_leg(legs, -1, len(body.ref_images), None)
            if first is None:
                raise DomainError(400, "No provider leg accepts this many reference images")
            if user["balance"] < model["price"]:
                raise DomainError(402, "Insufficient tokens")
            active = await conn.fetchval(
                "SELECT count(*) FROM tasks WHERE user_id=$1 AND status='active'", body.user_id,
            )
            if active >= 3:
                raise DomainError(429, "At most 3 active tasks per user")
            task_id = uuid4()
            task = await conn.fetchrow(
                """INSERT INTO tasks(id,user_id,model_slug,price,prompt,ref_images,legs,
                   request_key,request_hash,request_id,scenario_key)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING *""",
                task_id, body.user_id, body.model_slug, model["price"], body.prompt,
                [str(x) for x in body.ref_images], legs, key, digest, request_id, scenario_key,
            )
            await post_entry(conn, user_id=body.user_id, delta=-model["price"],
                             kind="charge", key=f"charge:{task_id}", task_id=task_id)
            await event(conn, task_id, "charged", tokens=model["price"], request_id=request_id)
            attempt_id = await self.enqueue(conn, task, first)
            return {"task_id": task_id, "attempt_id": attempt_id, "replayed": False}

    async def enqueue(self, conn, task, index: int) -> UUID:
        attempt_id = uuid4()
        leg = task["legs"][index]
        await conn.execute("UPDATE tasks SET current_index=$2 WHERE id=$1", task["id"], index)
        await conn.execute(
            """INSERT INTO attempts(id,task_id,leg_index,provider,status)
               VALUES($1,$2,$3,$4,'queued')""", attempt_id, task["id"], index, leg["provider"],
        )
        await conn.execute("INSERT INTO outbox(attempt_id) VALUES($1)", attempt_id)
        await event(conn, task["id"], "attempt_queued", attempt_id,
                    provider=leg["provider"], leg_index=index)
        return attempt_id

    async def fail_locked(self, conn, task, attempt, error: str, detail: str,
                          timed_out: bool = False) -> bool:
        """Requires user/task/attempt locks. A stale event cannot advance a newer attempt."""
        if not is_current(task, attempt):
            return False
        status = "timed_out" if timed_out else "failed"
        await conn.execute(
            """UPDATE attempts SET status=$2,error_type=$3,error_detail=$4,
               finished_at=clock_timestamp() WHERE id=$1""",
            attempt["id"], status, error, detail[:500],
        )
        await conn.execute("UPDATE outbox SET state='done' WHERE attempt_id=$1", attempt["id"])
        await event(conn, task["id"], status, attempt["id"], error_type=error, detail=detail[:500])
        index = next_leg(task["legs"], attempt["leg_index"], len(task["ref_images"]), error)
        if index is not None:
            await self.enqueue(conn, task, index)
            await event(conn, task["id"], "fallback", attempt["id"], next_leg_index=index)
        else:
            await conn.execute(
                """UPDATE tasks SET status='failed',error_type=$2,
                   finished_at=clock_timestamp() WHERE id=$1""", task["id"], error,
            )
            refunded = await post_entry(
                conn, user_id=task["user_id"], delta=task["price"], kind="refund",
                key=f"refund:{task['id']}", task_id=task["id"],
            )
            if refunded:
                await event(conn, task["id"], "refunded", attempt["id"], tokens=task["price"])
        return True

    async def provider_webhook(self, provider: str, body: ProviderWebhook, body_hash: str):
        if provider not in ("alpha", "beta"):
            raise DomainError(404, "Unknown provider")
        async with self.pool.acquire() as conn, conn.transaction():
            task, attempt = await lock_attempt(conn, body.attempt_id)
            if attempt["provider"] != provider:
                raise DomainError(409, "Provider does not own this attempt")
            if attempt["external_id"] and attempt["external_id"] != body.external_id:
                raise DomainError(409, "external_id does not match the attempt")
            receipt = await conn.fetchval(
                "SELECT disposition FROM webhook_receipts WHERE provider=$1 AND body_hash=$2",
                provider, body_hash,
            )
            if receipt:
                return {"disposition": "duplicate", "original": receipt}
            # The callback may beat the HTTP 202. Correlate with our durable attempt ID.
            if attempt["external_id"] is None:
                owner = await conn.fetchval(
                    "SELECT id FROM attempts WHERE provider=$1 AND external_id=$2",
                    provider, body.external_id,
                )
                if owner is not None and owner != attempt["id"]:
                    raise DomainError(409, "external_id belongs to another attempt")
                await conn.execute("UPDATE attempts SET external_id=$2 WHERE id=$1",
                                   attempt["id"], body.external_id)
            if is_current(task, attempt) and attempt["expired"]:
                await self.fail_locked(conn, task, attempt, "technical", "provider_timeout", True)
                disposition = "ignored_late"
            elif not is_current(task, attempt):
                disposition = "ignored_late" if attempt["status"] == "timed_out" else "ignored_terminal"
            elif attempt["status"] == "queued":
                raise DomainError(409, "Attempt has not been dispatched")
            elif body.status == "completed":
                await conn.execute(
                    "UPDATE attempts SET status='completed',finished_at=clock_timestamp() WHERE id=$1",
                    attempt["id"],
                )
                await conn.execute(
                    """UPDATE tasks SET status='completed',result_url=$2,
                       finished_at=clock_timestamp() WHERE id=$1""", task["id"], str(body.result_url),
                )
                await conn.execute("UPDATE outbox SET state='done' WHERE attempt_id=$1", attempt["id"])
                disposition = "applied"
                await event(conn, task["id"], "completed", attempt["id"])
            else:
                await self.fail_locked(conn, task, attempt, body.error_type, "provider_webhook")
                disposition = "applied"
            await conn.execute(
                """INSERT INTO webhook_receipts(provider,body_hash,attempt_id,disposition)
                   VALUES($1,$2,$3,$4)""", provider, body_hash, attempt["id"], disposition,
            )
            if disposition.startswith("ignored"):
                # Do not expose a refunded attempt's result URL, even via its event history.
                await event(conn, task["id"], disposition, attempt["id"], status=body.status)
            return {"disposition": disposition}

    async def payment(self, body: Payment):
        async with self.pool.acquire() as conn, conn.transaction():
            # An authenticated payment also provisions a user; registration is out of scope.
            await conn.execute("INSERT INTO users(id) VALUES($1) ON CONFLICT DO NOTHING", body.user_id)
            await lock_user(conn, body.user_id)
            await conn.execute(
                """INSERT INTO payments(payment_id,user_id,tokens,status) VALUES($1,$2,$3,'pending')
                   ON CONFLICT DO NOTHING""", body.payment_id, body.user_id, body.tokens,
            )
            payment = await conn.fetchrow(
                "SELECT * FROM payments WHERE payment_id=$1 FOR UPDATE", body.payment_id,
            )
            if payment["user_id"] != body.user_id or payment["tokens"] != body.tokens:
                raise DomainError(409, "payment_id was already used for another user or amount")
            if payment["status"] == "succeeded":
                return {"credited": False, "status": "succeeded", "duplicate": True}
            rank = {"pending": 0, "failed": 1, "succeeded": 2}
            new_status = max((payment["status"], body.status), key=rank.__getitem__)
            await conn.execute(
                "UPDATE payments SET status=$2,updated_at=clock_timestamp() WHERE payment_id=$1",
                body.payment_id, new_status,
            )
            credited = False
            if new_status == "succeeded":
                credited = await post_entry(
                    conn, user_id=body.user_id, delta=body.tokens, kind="payment",
                    key=f"payment:{body.payment_id}", payment_id=body.payment_id,
                )
            return {"credited": credited, "status": new_status, "duplicate": False}
