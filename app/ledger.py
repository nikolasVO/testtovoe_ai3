from uuid import UUID


async def post_entry(conn, *, user_id: int, delta: int, kind: str, key: str,
                     task_id: UUID | None = None, payment_id: str | None = None) -> bool:
    """Caller owns the user lock. AFTER INSERT is the only balance writer."""
    inserted = await conn.fetchval(
        """INSERT INTO ledger(user_id,delta,kind,operation_key,task_id,payment_id)
           VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING RETURNING id""",
        user_id, delta, kind, key, task_id, payment_id,
    )
    return inserted is not None
