"""Global write lock order: user -> task -> attempt -> outbox."""
import logging
from uuid import UUID

from app.domain import DomainError

log = logging.getLogger(__name__)


async def event(conn, task_id: UUID, kind: str, attempt_id: UUID | None = None, **details):
    await conn.execute(
        "INSERT INTO task_events(task_id,attempt_id,kind,details) VALUES($1,$2,$3,$4)",
        task_id, attempt_id, kind, details,
    )
    # A diagnostic log is not evidence of commit; task_events is the transactional audit trail.
    log.info(kind, extra={"task_id": task_id, "attempt_id": attempt_id})


async def lock_user(conn, user_id: int):
    user = await conn.fetchrow("SELECT * FROM users WHERE id=$1 FOR UPDATE", user_id)
    if user is None:
        raise DomainError(404, "User not found")
    return user


async def lock_task(conn, task_id: UUID):
    user_id = await conn.fetchval("SELECT user_id FROM tasks WHERE id=$1", task_id)
    if user_id is None:
        raise DomainError(404, "Task not found")
    await lock_user(conn, user_id)
    return await conn.fetchrow("SELECT * FROM tasks WHERE id=$1 FOR UPDATE", task_id)


async def lock_attempt(conn, attempt_id: UUID):
    task_id = await conn.fetchval("SELECT task_id FROM attempts WHERE id=$1", attempt_id)
    if task_id is None:
        raise DomainError(404, "Attempt not found")
    task = await lock_task(conn, task_id)
    attempt = await conn.fetchrow(
        "SELECT *, deadline <= clock_timestamp() AS expired FROM attempts WHERE id=$1 FOR UPDATE",
        attempt_id,
    )
    return task, attempt
