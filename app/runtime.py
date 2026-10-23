import asyncio
import logging

log = logging.getLogger(__name__)


async def worker_loop(name, work, interval: float):
    while True:
        try:
            worked = await work()
            await asyncio.sleep(0 if worked else interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Keep the worker alive; failed transactions roll back and leases can be reclaimed.
            log.exception("worker_iteration_failed", extra={"provider": name})
            await asyncio.sleep(interval)
