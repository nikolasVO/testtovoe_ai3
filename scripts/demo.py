"""python -m scripts.demo [fallback|duplicate_payment|duplicate_refund|late|...]"""
import asyncio
import json
import os
import sys
import time

import httpx

from app.config import Settings


async def main():
    settings = Settings()
    base = os.getenv("DEMO_BASE_URL", settings.public_base_url).rstrip("/")
    scenario = sys.argv[1] if len(sys.argv) > 1 else "fallback"
    async with httpx.AsyncClient(base_url=base, headers={"X-Admin-Key": settings.admin_key}, timeout=30) as client:
        response = await client.post("/demo/runs", json={"scenario": scenario})
        response.raise_for_status()
        run = response.json()
        print(json.dumps(run, ensure_ascii=False, indent=2))
        seen = set()
        deadline = time.monotonic() + 2*settings.provider_timeout + 45
        while time.monotonic() < deadline:
            response = await client.get(f"/demo/runs/{run['run_id']}")
            response.raise_for_status()
            data = response.json()
            for event in (data["task"] or {}).get("events", []):
                if event["id"] not in seen:
                    print(event["created_at"], event["kind"], event["details"], flush=True)
                    seen.add(event["id"])
            if data["finished"]:
                print(json.dumps({"balance": data["account"]["balance"], "ledger_sum": data["account"]["ledger_sum"],
                                  "counts": data["counts"], "status": (data["task"] or {}).get("status")}, indent=2))
                assert data["account"]["consistent"]
                return
            await asyncio.sleep(0.5)
        raise TimeoutError("Run still pending; inspect task history and compose logs")


if __name__ == "__main__":
    asyncio.run(main())
