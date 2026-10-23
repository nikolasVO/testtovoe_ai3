from app.domain import is_current


class Watchdog:
    def __init__(self, service):
        self.service, self.pool = service, service.pool

    async def tick(self) -> bool:
        async with self.pool.acquire() as conn, conn.transaction():
            # Lock USERS, not attempts, to preserve the common lock order under all races.
            user_id = await conn.fetchval(
                """SELECT u.id FROM users u WHERE EXISTS (
                     SELECT 1 FROM tasks t JOIN attempts a ON a.task_id=t.id
                     WHERE t.user_id=u.id AND t.status='active'
                       AND a.leg_index=t.current_index AND a.status IN ('sending','waiting')
                       AND a.deadline<=clock_timestamp())
                   ORDER BY u.id LIMIT 1 FOR UPDATE OF u SKIP LOCKED""",
            )
            if user_id is None:
                return False
            task = await conn.fetchrow(
                """SELECT t.* FROM tasks t WHERE t.user_id=$1 AND t.status='active' AND EXISTS (
                     SELECT 1 FROM attempts a WHERE a.task_id=t.id AND a.leg_index=t.current_index
                       AND a.status IN ('sending','waiting') AND a.deadline<=clock_timestamp())
                   ORDER BY t.id LIMIT 1 FOR UPDATE OF t""", user_id,
            )
            if task is None:
                return False
            attempt = await conn.fetchrow(
                """SELECT *, deadline<=clock_timestamp() AS expired FROM attempts
                   WHERE task_id=$1 AND leg_index=$2 FOR UPDATE""", task["id"], task["current_index"],
            )
            if attempt["expired"] and is_current(task, attempt):
                return await self.service.fail_locked(
                    conn, task, attempt, "technical", "provider_timeout", timed_out=True,
                )
            return False
