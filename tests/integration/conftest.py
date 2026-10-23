import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

from app.config import Settings
from app.db import make_pool
from app.dispatch import Dispatcher
from app.main import create_app
from app.schemas import Generate
from app.security import sign
from app.service import GenerationService
from app.watchdog import Watchdog


@dataclass
class Harness:
    pool: Any
    settings: Settings
    service: GenerationService
    dispatcher: Dispatcher
    watchdog: Watchdog
    api: httpx.AsyncClient
    client: httpx.AsyncClient

    async def generate(self, refs=0, model="image-fast", user=1, key=None):
        return await self.service.generate(Generate(user_id=user, model_slug=model, prompt="test",
            ref_images=[f"https://refs.test/{i}.png" for i in range(refs)]), key)

    async def attempts(self, task_id):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM attempts WHERE task_id=$1 ORDER BY leg_index", task_id)

    async def callback(self, attempt, status="failed", error="technical"):
        body = {"attempt_id": str(attempt["id"]), "external_id": attempt["external_id"] or f"ext-{attempt['id']}", "status": status}
        if status == "completed":
            body["result_url"] = "https://files.test/result.png"
        else:
            body["error_type"] = error
        return await self.signed(f"/webhooks/providers/{attempt['provider']}", body)

    async def signed(self, path, body):
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
        return await self.api.post(path, content=raw,
            headers={"Content-Type": "application/json", "X-Signature": sign(raw, self.settings.webhook_secret)})

    async def expire(self, attempt_id):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE attempts SET deadline=clock_timestamp()-interval '1 second' WHERE id=$1", attempt_id)

    async def assert_invariants(self):
        async with self.pool.acquire() as conn:
            assert await conn.fetchval("""SELECT count(*) FROM users u WHERE balance<0 OR balance <>
                (SELECT COALESCE(sum(delta),0) FROM ledger l WHERE l.user_id=u.id)""") == 0
            assert await conn.fetchval("""SELECT count(*) FROM tasks t WHERE
                (SELECT count(*) FROM ledger l WHERE l.task_id=t.id AND l.kind='charge')<>1 OR
                (SELECT count(*) FROM ledger l WHERE l.task_id=t.id AND l.kind='refund')<>
                  CASE WHEN t.status='failed' THEN 1 ELSE 0 END OR
                (t.status='failed' AND t.result_url IS NOT NULL)""") == 0


@pytest.fixture
async def env():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL and ALLOW_TEST_DATABASE_RESET=1; use make test for isolated PostgreSQL")
    if not urlparse(url).path.rstrip("/").endswith("_test") or os.getenv("ALLOW_TEST_DATABASE_RESET") != "1":
        pytest.fail("Refusing destructive tests: database name must end in _test and reset must be explicitly allowed")
    settings = Settings(_env_file=None, database_url=url, run_background=False, demo_enabled=False,
                        public_base_url="http://api", alpha_url="http://alpha", beta_url="http://beta",
                        provider_timeout=60, http_timeout=1, lease_seconds=3, pool_max=24)
    pool = await make_pool(settings)
    async with pool.acquire() as conn, conn.transaction():
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0003", "Apply migrations first"
        await conn.execute("""TRUNCATE users, models, fake_configs, fake_jobs RESTART IDENTITY CASCADE;
            INSERT INTO models VALUES ('image-fast',2),('video-long',25);
            INSERT INTO legs(model_slug,provider,priority,max_ref_images,skip_on_moderation) VALUES
              ('image-fast','alpha',10,4,false),('image-fast','beta',20,10,false),
              ('video-long','beta',10,2,false),('video-long','alpha',20,1,true);
            INSERT INTO users(id) VALUES(1);
            INSERT INTO ledger(user_id,delta,kind,operation_key) VALUES(1,100,'seed','seed:1');""")
    def transport(request):
        payload = json.loads(request.content)
        return httpx.Response(202, json={"external_id": f"ext-{payload['attempt_id']}"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    app = create_app(settings, pool=pool, client=client)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://api") as api:
                yield Harness(pool, settings, app.state.service, app.state.dispatcher, app.state.watchdog, api, client)
    finally:
        await client.aclose()
        await pool.close()
