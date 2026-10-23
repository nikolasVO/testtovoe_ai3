import hmac

from fastapi import Request
from pydantic import ValidationError

from app.domain import DomainError
from app.security import verify


async def signed_payload(request: Request, schema):
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 2_000_000:
            raise DomainError(413, "Webhook is too large")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not verify(raw, request.headers.get("X-Signature"), request.app.state.settings.webhook_secret):
        raise DomainError(401, "Invalid signature")
    try:
        return raw, schema.model_validate_json(raw)
    except ValidationError as exc:
        raise DomainError(422, "Invalid webhook payload") from exc


def require_admin(request: Request):
    supplied = request.headers.get("X-Admin-Key", "")
    expected = request.app.state.settings.admin_key
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise DomainError(401, "Invalid admin key")
