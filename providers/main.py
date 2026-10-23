import asyncio
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.db import make_pool
from app.domain import DomainError
from app.http_security import require_admin
from app.logging import setup_logging
from app.runtime import worker_loop
from providers.schemas import Behavior, TaskRequest
from providers.service import FakeProvider


def create_provider(settings: Settings | None = None, *, pool=None, client=None):
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        setup_logging()
        actual_pool = pool or await make_pool(settings)
        actual_client = client or httpx.AsyncClient()
        app.state.settings, app.state.pool = settings, actual_pool
        app.state.provider = FakeProvider(actual_pool, settings, actual_client)
        worker = None
        if settings.run_background:
            worker = asyncio.create_task(worker_loop(settings.provider_name, app.state.provider.deliver_one, settings.poll_interval))
        try:
            yield
        finally:
            if worker:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            if client is None:
                await actual_client.aclose()
            if pool is None:
                await actual_pool.close()

    app = FastAPI(title=f"Fake provider: {settings.provider_name}", lifespan=lifespan)
    app.state.settings = settings

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    @app.post("/tasks", status_code=202)
    async def create_task(body: TaskRequest, request: Request,
                          idempotency_key: Annotated[str, Header(min_length=1, max_length=200)]):
        external_id, behavior = await request.app.state.provider.accept(body, idempotency_key)
        if behavior.sync_error == "timeout":
            await asyncio.sleep(settings.http_timeout + 2)
        return {"external_id": external_id}

    @app.post("/admin/config")
    async def set_config(body: Behavior, request: Request, scope: str = Query(default="default", min_length=1, max_length=200)):
        require_admin(request)
        await request.app.state.provider.configure(scope, body)
        return {"provider": settings.provider_name, "scope": scope, "config": body}

    @app.get("/admin/jobs/{key}")
    async def get_job(key: str, request: Request):
        require_admin(request)
        async with request.app.state.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM fake_jobs WHERE provider=$1 AND idempotency_key=$2",
                                      settings.provider_name, key)
        if row is None:
            raise DomainError(404, "Job not found")
        return dict(row)

    @app.get("/health")
    async def health(request: Request):
        async with request.app.state.pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ok", "provider": settings.provider_name}

    return app


app = create_provider()
