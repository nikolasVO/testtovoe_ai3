from uuid import UUID

from app.domain import DomainError


async def balance(pool, user_id: int, limit: int = 50):
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        user = await conn.fetchrow("SELECT * FROM users WHERE id=$1", user_id)
        if user is None:
            raise DomainError(404, "User not found")
        ledger_sum = await conn.fetchval("SELECT COALESCE(sum(delta),0) FROM ledger WHERE user_id=$1", user_id)
        rows = await conn.fetch("SELECT * FROM ledger WHERE user_id=$1 ORDER BY id DESC LIMIT $2", user_id, limit)
        return {"user_id": user_id, "balance": user["balance"], "ledger_sum": int(ledger_sum),
                "consistent": user["balance"] == ledger_sum, "ledger": [dict(r) for r in rows]}


async def task(pool, task_id: UUID):
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        row = await conn.fetchrow("SELECT * FROM tasks WHERE id=$1", task_id)
        if row is None:
            raise DomainError(404, "Task not found")
        attempts = [dict(r) for r in await conn.fetch(
            "SELECT * FROM attempts WHERE task_id=$1 ORDER BY leg_index", task_id,
        )]
        events = [dict(r) for r in await conn.fetch(
            "SELECT * FROM task_events WHERE task_id=$1 ORDER BY id", task_id,
        )]
        current = row["legs"][row["current_index"]] if row["current_index"] >= 0 else None
        return {"task_id": row["id"], "user_id": row["user_id"], "model_slug": row["model_slug"],
                "status": row["status"], "price": row["price"], "result_url": row["result_url"],
                "error_type": row["error_type"], "current_leg": current,
                "current_leg_index": row["current_index"], "created_at": row["created_at"],
                "finished_at": row["finished_at"], "request_id": row["request_id"],
                "attempts": attempts, "events": events}
