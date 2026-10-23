"""Send a correctly signed payment; secret never appears in the request body."""
import argparse
import asyncio
import json
from uuid import uuid4

import httpx

from app.config import Settings
from app.security import sign


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", type=int, default=1)
    parser.add_argument("--tokens", type=int, default=100)
    parser.add_argument("--payment-id", default=None)
    parser.add_argument("--duplicates", type=int, default=1)
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()
    body = json.dumps({"payment_id": args.payment_id or str(uuid4()), "user_id": args.user,
                       "tokens": args.tokens, "status": "succeeded"}, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "X-Signature": sign(body, Settings().webhook_secret)}
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30) as client:
        results = await asyncio.gather(*(client.post("/webhooks/payments", content=body, headers=headers)
                                         for _ in range(args.duplicates)))
        for result in results:
            result.raise_for_status()
            print(result.json())


if __name__ == "__main__":
    asyncio.run(main())
