"""Prometheus exposition backed by shared DB state, not per-worker in-memory counters."""


async def render_metrics(pool) -> str:
    lines = []

    def family(name, kind, help_text):
        lines.extend([f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"])

    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        family("generation_tasks", "gauge", "Tasks by current state across all workers")
        states = {r["status"]: r["n"] for r in await conn.fetch("SELECT status,count(*) n FROM tasks GROUP BY status")}
        for status in ("active", "completed", "failed"):
            lines.append(f'generation_tasks{{status="{status}"}} {states.get(status, 0)}')
        family("generation_events_total", "counter", "Committed domain events")
        for row in await conn.fetch("SELECT kind,count(*) n FROM task_events GROUP BY kind ORDER BY kind"):
            lines.append(f'generation_events_total{{kind="{row["kind"]}"}} {row["n"]}')
        family("generation_tokens_moved_total", "counter", "Absolute token movements by ledger kind")
        for row in await conn.fetch("SELECT kind,sum(abs(delta)) n FROM ledger GROUP BY kind ORDER BY kind"):
            lines.append(f'generation_tokens_moved_total{{kind="{row["kind"]}"}} {row["n"]}')
        family("generation_provider_errors_total", "counter", "Failed and timed-out attempts")
        for row in await conn.fetch("""SELECT provider,error_type,count(*) n FROM attempts
                                     WHERE error_type IS NOT NULL GROUP BY provider,error_type"""):
            lines.append(f'generation_provider_errors_total{{provider="{row["provider"]}",error_type="{row["error_type"]}"}} {row["n"]}')
        family("generation_outbox_pending", "gauge", "Durable jobs awaiting acknowledged dispatch")
        pending = await conn.fetchval("SELECT count(*) FROM outbox WHERE state='pending'")
        lines.append(f"generation_outbox_pending {pending}")
        family("generation_balance_mismatches", "gauge", "Must remain zero")
        bad = await conn.fetchval("""SELECT count(*) FROM users u WHERE balance <>
                                  (SELECT COALESCE(sum(delta),0) FROM ledger l WHERE l.user_id=u.id)""")
        lines.append(f"generation_balance_mismatches {bad}")
        family("generation_task_duration_seconds", "histogram", "Committed end-to-end terminal task latency")
        # Aggregate in SQL; no per-task labels and no unbounded result list in Python.
        for bound in (0.5, 1, 2, 5, 10, 30, 60, 120):
            count = await conn.fetchval("""SELECT count(*) FROM tasks WHERE finished_at IS NOT NULL
                                         AND extract(epoch FROM finished_at-created_at)::double precision<=$1::double precision""", bound)
            lines.append(f'generation_task_duration_seconds_bucket{{le="{bound}"}} {count}')
        stats = await conn.fetchrow("""SELECT count(*) n,
                                    COALESCE(sum(extract(epoch FROM finished_at-created_at)),0) s
                                    FROM tasks WHERE finished_at IS NOT NULL""")
        lines.extend([f'generation_task_duration_seconds_bucket{{le="+Inf"}} {stats["n"]}',
                      f'generation_task_duration_seconds_count {stats["n"]}',
                      f'generation_task_duration_seconds_sum {stats["s"]}'])
    return "\n".join(lines) + "\n"
