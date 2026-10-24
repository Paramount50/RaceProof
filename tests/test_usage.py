from types import SimpleNamespace

import pytest

from raceproof.usage import (
    BudgetExceededError,
    ContextLimitExceededError,
    ModelAuthenticationError,
    ModelQuotaExceededError,
    ModelRateLimitError,
    ModelTransientError,
    UsageLimits,
    UsageTracker,
    _extract_usage,
    classify_model_error,
)


def test_request_budgets_are_enforced_before_provider_call() -> None:
    tracker = UsageTracker(UsageLimits(2, 1, 1, 1000))
    tracker.start_request("investigation", "model-a", False)
    with pytest.raises(BudgetExceededError, match="investigation"):
        tracker.start_request("investigation", "model-a", False)
    tracker.start_request("repair", "model-b", True)
    with pytest.raises(BudgetExceededError, match="global"):
        tracker.start_request("repair", "model-b", False)
    snapshot = tracker.snapshot()
    assert snapshot.total_requests == 2
    assert snapshot.fallback_invocations == 1
    assert "global request budget" in snapshot.budget_exhausted_reason
    assert "used 2 of 2" in snapshot.budget_exhausted_reason
    assert "RACEPROOF" in snapshot.budget_exhausted_reason


def test_global_budget_exhaustion_during_repair_is_reported_as_local() -> None:
    tracker = UsageTracker(UsageLimits(2, 1, 2, 1000))
    tracker.start_request("investigation", "investigator", False)
    tracker.start_request("repair", "repairer", False)
    with pytest.raises(BudgetExceededError) as raised:
        tracker.start_request("repair", "repairer", False)
    assert raised.value.limit_kind == "global request budget"
    assert tracker.snapshot().requests_by_agent == {"investigation": 1, "repair": 1}


@pytest.mark.parametrize("limit", [5, 12, 20])
def test_investigation_limit_is_independently_configurable(limit: int) -> None:
    tracker = UsageTracker(UsageLimits(limit + 8, limit, 8, 100_000))
    for _ in range(limit):
        tracker.start_request("investigation", "model-a", False)
    with pytest.raises(BudgetExceededError) as raised:
        tracker.start_request("investigation", "model-a", False)
    assert raised.value.limit_kind == "investigation request budget"
    assert raised.value.used == limit
    assert tracker.snapshot().remaining_repair_budget == 8


def test_request_log_tracks_operation_provider_retry_and_tokens() -> None:
    tracker = UsageTracker(UsageLimits())
    request_id = tracker.start_request(
        "investigation",
        "model-a",
        False,
        operation="plan_experiment",
        provider="gemini",
        retry_index=1,
    )
    tracker.finish_request("investigation", "model-a", (10, 4, 14), request_id)
    record = tracker.snapshot().request_log[0]
    assert record.operation == "plan_experiment"
    assert record.provider == "gemini"
    assert record.retry_index == 1
    assert record.status == "succeeded"
    assert record.total_tokens == 14


def test_bounded_retry_is_counted_as_a_real_request(monkeypatch) -> None:
    monkeypatch.setattr("raceproof.usage.time.sleep", lambda seconds: None)
    tracker = UsageTracker(UsageLimits())
    calls = 0

    def invoke(config):
        nonlocal calls
        calls += 1
        callback = config["callbacks"][0]
        callback.on_chat_model_start({}, [[]])
        if calls == 1:
            error = ModelTransientError("temporary provider outage")
            callback.on_llm_error(error)
            raise error
        callback.on_llm_end(SimpleNamespace(generations=[]))
        return "ok"

    assert (
        tracker.invoke_with_retry(
            agent="investigation",
            model="model-a",
            invoke=invoke,
            max_retries=1,
            operation="plan_experiment",
        )
        == "ok"
    )
    snapshot = tracker.snapshot()
    assert snapshot.total_requests == 2
    assert [item.retry_index for item in snapshot.request_log] == [0, 1]


def test_retry_after_is_honored_with_hackathon_wait_cap(monkeypatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr("raceproof.usage.time.sleep", waits.append)
    tracker = UsageTracker(UsageLimits())
    calls = 0

    class RateLimited(Exception):
        response = SimpleNamespace(status_code=429, headers={"Retry-After": "30"})

    def invoke(config):
        nonlocal calls
        calls += 1
        callback = config["callbacks"][0]
        callback.on_chat_model_start({}, [[]])
        if calls == 1:
            error = RateLimited("429 retry later")
            callback.on_llm_error(error)
            raise error
        callback.on_llm_end(SimpleNamespace(generations=[]))
        return "ok"

    assert tracker.invoke_with_retry(
        agent="investigation",
        model="model-a",
        invoke=invoke,
        max_retries=1,
        max_retry_wait_seconds=3,
    ) == "ok"
    assert waits == [3]


def test_preflight_context_limit_stops_before_provider_request() -> None:
    tracker = UsageTracker(UsageLimits(10, 4, 6, 20_000, 1_000))
    called = False

    def invoke(config):
        nonlocal called
        called = True

    with pytest.raises(ContextLimitExceededError, match="request input context"):
        tracker.invoke_with_retry(
            agent="repair",
            model="model-a",
            invoke=invoke,
            max_retries=0,
            estimated_input_tokens=1_001,
        )
    assert not called
    assert tracker.snapshot().total_requests == 0


def test_provider_reported_tokens_and_unknown_usage_are_explicit() -> None:
    tracker = UsageTracker(UsageLimits())
    tracker.start_request("repair", "model-b", False)
    tracker.finish_request("repair", "model-b", (11, 7, 18))
    tracker.start_request("repair", "model-b", False)
    tracker.finish_request("repair", "model-b", None)
    snapshot = tracker.snapshot()
    assert snapshot.total_tokens == 18
    assert snapshot.unknown_token_responses == 1
    assert snapshot.approximate_cost_usd is None


def test_usage_extraction_reads_langchain_metadata() -> None:
    response = SimpleNamespace(
        generations=[
            [
                SimpleNamespace(
                    message=SimpleNamespace(
                        usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}
                    )
                )
            ]
        ]
    )
    assert _extract_usage(response) == (3, 2, 5)


@pytest.mark.parametrize(
    "message,expected",
    [
        ("API key not valid", ModelAuthenticationError),
        ("429 RESOURCE_EXHAUSTED retry later", ModelRateLimitError),
        ("429 project quota exhausted for daily requests", ModelQuotaExceededError),
    ],
)
def test_provider_failures_are_classified(message: str, expected: type[Exception]) -> None:
    assert isinstance(classify_model_error(RuntimeError(message)), expected)
