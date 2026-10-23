from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app
from app.schemas import Payment, ProviderWebhook
from app.security import sign, verify
from providers.schemas import Behavior


@pytest.mark.parametrize("signature", [None, "", "f"*63, "f"*65, "z"*64, "я"*64])
def test_invalid_hmac(signature):
    assert not verify(b'{"x":1}', signature, "secret")


def test_hmac_is_over_exact_bytes():
    raw = b'{"x":1}'
    signature = sign(raw, "secret")
    assert verify(raw, signature, "secret")
    assert verify(raw, signature.upper(), "secret")
    assert not verify(b'{ "x": 1 }', signature, "secret")
    assert not verify(raw, signature, "another secret")


@pytest.mark.parametrize("tokens", [0, -1, 1.5, "10", True, 1000000001])
def test_tokens_are_strict_positive_bounded_integers(tokens):
    with pytest.raises(ValidationError):
        Payment(payment_id="p", user_id=1, tokens=tokens, status="succeeded")


@pytest.mark.parametrize("payload", [
    {"status": "completed"}, {"status": "failed"},
    {"status": "completed", "result_url": "https://a.test/x", "error_type": "technical"},
    {"status": "failed", "result_url": "https://a.test/x", "error_type": "technical"},
    {"status": "completed", "result_url": "javascript:alert(1)"},
    {"status": "failed", "error_type": "unknown"},
])
def test_invalid_provider_outcomes(payload):
    with pytest.raises(ValidationError):
        ProviderWebhook(attempt_id=uuid4(), external_id="external", **payload)


@pytest.mark.parametrize("overrides", [{"http_timeout": 5, "lease_seconds": 5},
                                        {"provider_timeout": 0}, {"pool_min": 10, "pool_max": 2}])
def test_invalid_runtime_settings(overrides):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


@pytest.mark.parametrize("overrides", [{"failure_rate": 1.1}, {"delay_min": 20, "delay_max": 2},
                                        {"duplicates": 0}, {"late_delay": -1}])
def test_invalid_simulator_settings(overrides):
    with pytest.raises(ValidationError):
        Behavior(**overrides)


class NeverPool:
    def acquire(self):
        raise AssertionError("This request must not reach the database")


@pytest.fixture
def no_db_api():
    settings = Settings(_env_file=None, run_background=False, demo_enabled=True)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(202)))
    app = create_app(settings, pool=NeverPool(), client=client)
    with TestClient(app) as api:
        yield api


def test_unauthenticated_payment_is_rejected_before_database(no_db_api):
    response = no_db_api.post("/webhooks/payments", json={"payment_id": "p", "user_id": 1, "tokens": 10, "status": "succeeded"})
    assert response.status_code == 401


def test_valid_signature_invalid_payment_status_rejected_before_database(no_db_api):
    raw = b'{"payment_id":"p","user_id":1,"tokens":10,"status":"unknown"}'
    response = no_db_api.post("/webhooks/payments", content=raw,
                              headers={"X-Signature": sign(raw, Settings().webhook_secret)})
    assert response.status_code == 422


def test_large_webhook_rejected_before_database(no_db_api):
    assert no_db_api.post("/webhooks/payments", content=b"x"*2000001).status_code == 413


def test_malformed_generation_rejected_before_database(no_db_api):
    assert no_db_api.post("/generate", json={"user_id": 1, "model_slug": "image-fast", "prompt": "", "ref_images": []}).status_code == 422


def test_demo_admin_key_required(no_db_api):
    assert no_db_api.post("/demo/runs", json={"scenario": "fallback"}).status_code == 401


def test_demo_and_openapi_are_available_without_db_queries(no_db_api):
    assert "Failure Lab" in no_db_api.get("/demo").text
    assert no_db_api.get("/demo/assets/app.js").status_code == 200
    paths = no_db_api.get("/openapi.json").json()["paths"]
    assert {"/generate", "/webhooks/payments", "/webhooks/providers/{name}", "/tasks/{task_id}", "/users/{user_id}/balance"} <= paths.keys()


def test_live_health_contains_worker_and_request_id(no_db_api):
    response = no_db_api.get("/health/live", headers={"X-Request-Id": "review-123"})
    assert response.status_code == 200
    assert response.headers["X-Request-Id"] == "review-123"
    assert int(response.headers["X-Worker-Pid"]) > 0
