from types import SimpleNamespace

import pytest

from raceproof.model import _invoke_agent, _parse_repair_response
from raceproof.usage import (
    ModelConfigurationError,
    ProvidersExhaustedError,
    StructuredOutputError,
    UsageLimits,
    UsageTracker,
)


def _tracker() -> UsageTracker:
    return UsageTracker(UsageLimits(10, 4, 6, 20_000, 4_000))


def test_repair_response_parser_distinguishes_malformed_and_schema_invalid() -> None:
    with pytest.raises(StructuredOutputError, match="malformed JSON"):
        _parse_repair_response("{not-json")
    with pytest.raises(StructuredOutputError, match="schema-invalid"):
        _parse_repair_response({"diagnosis": "description only"})


def test_structured_output_failure_is_retried_and_counted() -> None:
    tracker = _tracker()
    calls = 0

    def invoke(config):
        nonlocal calls
        calls += 1
        callback = config["callbacks"][0]
        callback.on_chat_model_start({}, [[]])
        if calls == 1:
            error = StructuredOutputError("schema-invalid repair response")
            callback.on_llm_error(error)
            raise error
        callback.on_llm_end(SimpleNamespace(generations=[]))
        return {"ok": True}

    result = tracker.invoke_with_retry(
        agent="repair",
        model="gemini-repair",
        invoke=invoke,
        max_retries=1,
        operation="propose_patch",
    )
    assert result == {"ok": True}
    snapshot = tracker.snapshot()
    assert calls == 2
    assert snapshot.total_requests == 2
    assert [item.retry_index for item in snapshot.request_log] == [0, 1]


def test_fallback_configuration_failure_is_not_reported_as_request(monkeypatch) -> None:
    tracker = _tracker()
    monkeypatch.setattr("raceproof.model.validate_openrouter_free_model", lambda _model: None)

    def build(model: str, provider: str):
        if provider == "openrouter":
            raise ModelConfigurationError("fallback dependency unavailable")
        return SimpleNamespace(invoke=invoke)

    def invoke(*_args, **_kwargs):
        raise StructuredOutputError("malformed JSON repair response")

    with pytest.raises(ProvidersExhaustedError, match="all configured OpenRouter"):
        _invoke_agent(
            agent_name="repair",
            model_name="gemini-repair",
            fallback_model=None,
            fallback_enabled=False,
            tracker=tracker,
            max_retries=0,
            build=build,
            payload=[],
            operation="propose_patch",
            openrouter_models=["example/model:free"],
            openrouter_api_key="configured",
            auto_fallback=True,
            parse_result=lambda value: value,
        )
    snapshot = tracker.snapshot()
    assert snapshot.requests_by_provider == {}
    assert len(snapshot.fallback_history) == 1
    assert snapshot.fallback_invocations == 0
