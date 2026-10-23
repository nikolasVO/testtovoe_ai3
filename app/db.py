import json

from app.config import Settings


async def make_pool(settings: Settings):
    # Lazy import also allows pure unit tests without a PostgreSQL driver installed.
    import asyncpg

    async def init(conn):
        for typ in ("json", "jsonb"):
            await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

    return await asyncpg.create_pool(
        settings.database_url, min_size=settings.pool_min, max_size=settings.pool_max,
        command_timeout=30, init=init,
        server_settings={"application_name": "generation-orchestrator"},
    )
