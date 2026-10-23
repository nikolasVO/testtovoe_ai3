import asyncio
import json
import os
import time
from dataclasses import dataclass

import httpx
import pytest

from app.config import Settings
from app.security import sign


@dataclass
class Live:
    client: httpx.AsyncClient
    settings: Settings

    async def signed(self, path, payload):
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return await self.client.post(path, content=raw,
            headers={"Content-Type": "application/json", "X-Signature": sign(raw, self.settings.webhook_secret)})

    async def run(self, scenario):
        response = await self.client.post("/demo/runs", json={"scenario": scenario})
        response.raise_for_status()
        run = response.json()
        deadline = time.monotonic() + 2*run["provider_timeout"] + 45
        while time.monotonic() < deadline:
            response = await self.client.get(f"/demo/runs/{run['run_id']}")
            response.raise_for_status()
            data = response.json()
            if data["finished"]:
                return data
            await asyncio.sleep(0.2)
        raise AssertionError(f"Scenario did not finish: {run}")

    async def wait_delivered(self, attempt):
        url = self.settings.provider_url(attempt["provider"])
        deadline = time.monotonic() + self.settings.provider_timeout + 20
        while time.monotonic() < deadline:
            response = await self.client.get(f"{url}/admin/jobs/{attempt['id']}")
            response.raise_for_status()
            if response.json()["delivered_at"] is not None:
                return
            await asyncio.sleep(0.2)
        raise AssertionError("Simulator has not received acknowledgements for all duplicate callbacks")


@pytest.fixture
async def live():
    base = os.getenv("E2E_BASE_URL")
    if not base:
        pytest.skip("Set E2E_BASE_URL or run make e2e against the real four-worker stack")
    settings = Settings()
    async with httpx.AsyncClient(base_url=base, headers={"X-Admin-Key": settings.admin_key},
                                 timeout=40, limits=httpx.Limits(max_connections=100, max_keepalive_connections=0)) as client:
        response = await client.get("/health/ready")
        response.raise_for_status()
        yield Live(client, settings)
