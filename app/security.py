import hashlib
import hmac


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(body: bytes, signature: str | None, secret: str) -> bool:
    if not signature or len(signature) != 64:
        return False
    try:
        return hmac.compare_digest(sign(body, secret), signature.lower())
    except (TypeError, ValueError):
        return False
