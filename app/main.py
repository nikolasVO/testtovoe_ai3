import asyncio
import hashlib
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from app import readmodel
from app.config import Settings, get_settings
from app.db import make_pool
from app.dispatch import Dispatcher
from app.domain import DomainError
from app.http_security import signed_payload
from app.logging import setup_logging
from app.metrics import render_metrics
from app.runtime import worker_loop
from app.schemas import Generate, Payment, ProviderWebhook
from app.service import GenerationService
from app.watchdog import Watchdog

log = logging.getLogger(__name__)
WEB = Path(__file__).resolve().parent.parent / "web"


def create_app(settings: Settings | None = None, *, pool=None, client=None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        setup_logging()
        actual_pool = pool or await make_pool(settings)
        actual_client = client or httpx.AsyncClient()
        app.state.pool, app.state.client = actual_pool, actual_client
        app.state.settings = settings
        app.state.expected_revision = ScriptDirectory.from_config(Config(str(WEB.parent / "alembic.ini"))).get_current_head()
        app.state.service = GenerationService(actual_pool, settings)
        app.state.dispatcher = Dispatcher(app.state.service, actual_client)
        app.state.watchdog = Watchdog(app.state.service)
        workers = []
        if settings.run_background:
            workers = [asyncio.create_task(worker_loop("dispatch", app.state.dispatcher.dispatch_one, settings.poll_interval)),
                       asyncio.create_task(worker_loop("watchdog", app.state.watchdog.tick, settings.poll_interval))]
        try:
            yield
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            if client is None:
                await actual_client.aclose()
            if pool is None:
                await actual_pool.close()

    app = FastAPI(title="Generation Orchestrator", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    @app.middleware("http")
    async def trace_request(request: Request, call_next):
        supplied = request.headers.get("X-Request-Id", "")
        request.state.request_id = supplied if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", supplied) else str(uuid4())
        started = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        response.headers["X-Worker-Pid"] = str(os.getpid())
        log.info("http_request", extra={"request_id": request.state.request_id,
                 "status_code": response.status_code, "duration_ms": round((time.monotonic()-started)*1000, 2)})
        return response

    @app.post("/generate", status_code=202)
    async def generate(body: Generate, request: Request,
                       idempotency_key: Annotated[str | None, Header(min_length=1, max_length=200)] = None):
        result = await request.app.state.service.generate(body, idempotency_key, request.state.request_id)
        if not result["replayed"]:
            # Best-effort immediate dispatch. The committed outbox is the recovery mechanism.
            try:
                await request.app.state.dispatcher.dispatch_one(result["attempt_id"])
            except Exception:
                log.exception("inline_dispatch_deferred", extra={"task_id": result["task_id"]})
        return {"task_id": result["task_id"], "replayed": result["replayed"]}

    @app.post("/webhooks/payments")
    async def payment(request: Request):
        _, payload = await signed_payload(request, Payment)
        return await request.app.state.service.payment(payload)

    @app.post("/webhooks/providers/{name}")
    async def provider_webhook(name: str, request: Request):
        raw, payload = await signed_payload(request, ProviderWebhook)
        return await request.app.state.service.provider_webhook(name, payload, hashlib.sha256(raw).hexdigest())

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: UUID, request: Request):
        return await readmodel.task(request.app.state.pool, task_id)

    @app.get("/users/{user_id}/balance")
    async def get_balance(user_id: int, request: Request, limit: int = Query(default=50, ge=1, le=200)):
        return await readmodel.balance(request.app.state.pool, user_id, limit)

    @app.get("/health/live")
    async def live():
        return {"status": "ok", "worker_pid": os.getpid()}

    @app.get("/health/ready")
    async def ready(request: Request):
        async with request.app.state.pool.acquire() as conn:
            revision = await conn.fetchval("SELECT version_num FROM alembic_version")
        if revision != request.app.state.expected_revision:
            return JSONResponse(status_code=503, content={"status": "migrations_required"})
        return {"status": "ok", "revision": revision, "worker_pid": os.getpid()}

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics(request: Request):
        return PlainTextResponse(await render_metrics(request.app.state.pool),
                                 media_type="text/plain; version=0.0.4")

    if settings.demo_enabled:
        from app.demo import router as demo_router
        app.include_router(demo_router)

        @app.get("/demo", include_in_schema=False)
        async def demo_page():
            return FileResponse(WEB / "index.html")

        @app.get("/demo/assets/{name}", include_in_schema=False)
        async def demo_asset(name: str):
            if name not in ("app.js", "style.css"):
                raise DomainError(404, "Asset not found")
            return FileResponse(WEB / name)

    return app


app = create_app()
