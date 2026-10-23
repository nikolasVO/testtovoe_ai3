"""Random sequences exercise the real service/database, not a copy of its ledger algorithm."""
from uuid import uuid4

import pytest

from app.domain import DomainError
from app.schemas import Payment

hypothesis = pytest.importorskip("hypothesis")
st = pytest.importorskip("hypothesis.strategies")
HealthCheck, given, settings = hypothesis.HealthCheck, hypothesis.given, hypothesis.settings

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(st.sampled_from(["pay", "generate", "complete", "fail", "timeout", "duplicate"]), min_size=1, max_size=30))
async def test_financial_invariants_after_random_real_operations(env, operations):
    # Function-scoped fixtures are reused across Hypothesis examples; each example is isolated by user.
    user = uuid4().int % (2**48) + 10000
    payment = Payment(payment_id=str(uuid4()), user_id=user, tokens=50, status="succeeded")
    await env.service.payment(payment)
    tasks = []
    for operation in operations:
        if operation == "pay":
            await env.service.payment(payment)  # the same payment must never be credited twice
        elif operation == "generate":
            try:
                task = await env.generate(refs=5, user=user)
            except DomainError as error:
                assert error.status in (402, 429)
            else:
                tasks.append(task)
                await env.dispatcher.dispatch_one(task["attempt_id"])
        elif tasks:
            task = tasks[-1]
            attempt = (await env.attempts(task["task_id"]))[0]
            if operation == "timeout":
                await env.expire(attempt["id"])
                await env.watchdog.tick()
            else:
                response = await env.callback(attempt, "completed" if operation == "complete" else "failed")
                assert response.status_code == 200
        await env.assert_invariants()
