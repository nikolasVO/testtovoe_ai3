"""Test-only subprocess with barriers. No fault injection endpoints exist in the app."""
import asyncio
import os
import sys
from uuid import UUID

import httpx

import app.service as service_module
from app.config import Settings
from app.db import make_pool
from app.dispatch import Dispatcher
from app.schemas import Payment
from app.service import GenerationService
from providers.schemas import TaskRequest
from providers.service import FakeProvider


async def barrier():
    print("READY_TO_KILL", flush=True)
    await asyncio.sleep(3600)


async def main():
    mode = sys.argv[1]
    settings = Settings(_env_file=None, database_url=os.environ["TEST_DATABASE_URL"],
                        run_background=False, provider_timeout=60, http_timeout=1, lease_seconds=3,
                        public_base_url="http://api", alpha_url="http://alpha", beta_url="http://beta")
    pool = await make_pool(settings)
    service = GenerationService(pool, settings)
    try:
        if mode in ("before_commit", "after_commit"):
            if mode == "before_commit":
                original = service_module.post_entry
                async def interrupted(*args, **kwargs):
                    inserted = await original(*args, **kwargs)
                    if kwargs["kind"] == "payment":
                        await barrier()
                    return inserted
                service_module.post_entry = interrupted
            await service.payment(Payment(payment_id="crash-payment", user_id=1, tokens=7, status="succeeded"))
            await barrier()
        elif mode == "after_accept":
            async with httpx.AsyncClient() as callback_client:
                provider = FakeProvider(pool, settings, callback_client)
                async def transport(request):
                    body = TaskRequest.model_validate_json(request.content)
                    external_id, _ = await provider.accept(body, request.headers["Idempotency-Key"])
                    await barrier()  # Provider committed; dispatcher has not persisted the acknowledgement.
                    return httpx.Response(202, json={"external_id": external_id})
                async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
                    await Dispatcher(service, client).dispatch_one(UUID(os.environ["CRASH_ATTEMPT_ID"]))
        else:
            raise ValueError(mode)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
