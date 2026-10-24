import io
import json
from types import SimpleNamespace

import pytest

from raceproof.model import _invoke_agent
from raceproof.providers import _openrouter_catalog, validate_openrouter_free_model
from raceproof.usage import (
    BudgetExceededError,
    ModelAuthenticationError,
    ModelConfigurationError,
    ModelQuotaExceededError,
    ModelSharedPoolRateLimitError,
    ProvidersExhaustedError,
    UsageLimits,
    UsageTracker,
    classify_model_error,
)


def catalog_entry(
    *,
    model_id: str = "example/code:free",
    prompt: str = "0",
    completion: str = "0",
    parameters: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": model_id,
        "name": "Example Code Free",
        "context_length": 131072,
        "pricing": {"prompt": prompt, "completion": completion},
        "supported_parameters": parameters or ["tools", "structured_outputs"],
    }


def install_catalog(monkeypatch, entries: list[dict[str, object]]) -> None:
    payload = io.BytesIO(json.dumps({"data": entries}).encode())
    monkeypatch.setattr("raceproof.providers.urlopen", lambda *args, **kwargs: payload)
    _openrouter_catalog.cache_clear()
    validate_openrouter_free_model.cache_clear()


def test_openrouter_catalog_accepts_only_verified_free_capable_model(monkeypatch) -> None:
    install_catalog(monkeypatch, [catalog_entry()])
    metadata = validate_openrouter_free_model("example/code:free")
    assert metadata.verified_free
    assert metadata.supports_tools
    assert metadata.supports_structured_output


@pytest.mark.parametrize(
    "entry,error",
    [
        (catalog_entry(prompt="0.1"), "not all free"),
        (catalog_entry(parameters=["structured_outputs"]), "tool support"),
        (catalog_entry(parameters=["tools"]), "structured output"),
    ],
)
def test_openrouter_catalog_fails_closed(monkeypatch, entry, error: str) -> None:
    install_catalog(monkeypatch, [entry])
    with pytest.raises(ModelConfigurationError, match=error):
        validate_openrouter_free_model("example/code:free")


def fake_usage_response():
    return SimpleNamespace(
        generations=[
            [
                SimpleNamespace(
                    message=SimpleNamespace(
                        usage_metadata={
                            "input_tokens": 8,
                            "output_tokens": 4,
                            "total_tokens": 12,
                        }
                    )
                )
            ]
        ]
    )


class ProviderAgent:
    def __init__(self, provider: str, error: Exception | None = None) -> None:
        self.provider = provider
        self.error = error

    def invoke(self, payload, config):
        callback = config["callbacks"][0]
        callback.on_chat_model_start({}, [[]])
        if self.error:
            callback.on_llm_error(self.error)
            raise self.error
        callback.on_llm_end(fake_usage_response())
        return {"provider": self.provider}


@pytest.mark.parametrize("agent_name", ["investigation", "repair"])
def test_gemini_quota_falls_back_without_resetting_global_budget(
    monkeypatch, agent_name: str
) -> None:
    monkeypatch.setattr("raceproof.model.validate_openrouter_free_model", lambda model: object())
    tracker = UsageTracker(UsageLimits(5, 3, 2, 10_000))
    result = _invoke_agent(
        agent_name=agent_name,
        model_name="gemini-primary",
        fallback_model=None,
        fallback_enabled=False,
        tracker=tracker,
        max_retries=0,
        build=lambda model, provider: ProviderAgent(
            provider,
            ModelQuotaExceededError("provider quota exhausted") if provider == "gemini" else None,
        ),
        payload={"messages": []},
        operation="plan_experiment" if agent_name == "investigation" else "propose_patch",
        openrouter_models=["example/code:free"],
        openrouter_api_key="not-sent",
        auto_fallback=True,
    )
    assert result == {"provider": "openrouter"}
    snapshot = tracker.snapshot()
    assert snapshot.total_requests == 2
    assert [item.provider for item in snapshot.request_log] == ["gemini", "openrouter"]
    assert snapshot.remaining_request_budget == 3
    assert len(snapshot.fallback_history) == 1


@pytest.mark.parametrize(
    "error",
    [
        ModelAuthenticationError("invalid credential"),
        BudgetExceededError("investigation request budget", 1, 1),
    ],
)
def test_authentication_and_local_budget_do_not_trigger_fallback(monkeypatch, error) -> None:
    tracker = UsageTracker(UsageLimits(5, 3, 2, 10_000))
    providers: list[str] = []

    def build(model, provider):
        providers.append(provider)
        return ProviderAgent(provider, error)

    with pytest.raises(type(error)):
        _invoke_agent(
            agent_name="investigation",
            model_name="gemini-primary",
            fallback_model=None,
            fallback_enabled=False,
            tracker=tracker,
            max_retries=0,
            build=build,
            payload={"messages": []},
            operation="plan_experiment",
            openrouter_models=["example/code:free"],
            openrouter_api_key="not-sent",
            auto_fallback=True,
        )
    assert providers == ["gemini"]
    assert tracker.snapshot().fallback_history == []


def test_missing_openrouter_key_reports_providers_exhausted() -> None:
    tracker = UsageTracker(UsageLimits(5, 3, 2, 10_000))
    with pytest.raises(ProvidersExhaustedError, match="key is not configured"):
        _invoke_agent(
            agent_name="investigation",
            model_name="gemini-primary",
            fallback_model=None,
            fallback_enabled=False,
            tracker=tracker,
            max_retries=0,
            build=lambda model, provider: ProviderAgent(
                provider, ModelQuotaExceededError("quota exhausted")
            ),
            payload={"messages": []},
            operation="plan_experiment",
            openrouter_models=["example/code:free"],
            openrouter_api_key=None,
            auto_fallback=True,
        )
    assert tracker.snapshot().total_requests == 1


def test_both_provider_quotas_report_providers_exhausted(monkeypatch) -> None:
    monkeypatch.setattr("raceproof.model.validate_openrouter_free_model", lambda model: object())
    tracker = UsageTracker(UsageLimits(5, 3, 2, 10_000))
    with pytest.raises(ProvidersExhaustedError, match="example/code:free: QUOTA_EXCEEDED"):
        _invoke_agent(
            agent_name="investigation",
            model_name="gemini-primary",
            fallback_model=None,
            fallback_enabled=False,
            tracker=tracker,
            max_retries=0,
            build=lambda model, provider: ProviderAgent(
                provider, ModelQuotaExceededError(f"{provider} quota exhausted")
            ),
            payload={"messages": []},
            operation="plan_experiment",
            openrouter_models=["example/code:free"],
            openrouter_api_key="not-sent",
            auto_fallback=True,
        )
    snapshot = tracker.snapshot()
    assert snapshot.total_requests == 2
    assert snapshot.requests_by_provider == {"gemini": 1, "openrouter": 1}


class SharedPool429(Exception):
    status_code = 429

    def __init__(self, provider: str) -> None:
        self.body = {
            "error": {
                "message": "upstream shared pool is rate limited",
                "metadata": {
                    "limit_source": "upstream_provider_shared_pool",
                    "provider_name": provider,
                },
            }
        }
        super().__init__(json.dumps(self.body))


def test_shared_pool_rate_limit_is_classified_with_upstream() -> None:
    error = classify_model_error(SharedPool429("Phala"))
    assert isinstance(error, ModelSharedPoolRateLimitError)
    assert error.upstream_provider == "Phala"
    assert error.limit_source == "upstream_provider_shared_pool"
    assert not error.transient


def test_shared_pool_failure_moves_to_next_free_model_without_retry(monkeypatch) -> None:
    monkeypatch.setattr("raceproof.model.validate_openrouter_free_model", lambda model: object())
    tracker = UsageTracker(UsageLimits(8, 6, 2, 10_000))

    def build(model, provider):
        if provider == "gemini":
            return ProviderAgent(provider, ModelQuotaExceededError("quota exhausted"))
        if model == "example/limited:free":
            return ProviderAgent(provider, SharedPool429("Phala"))
        return ProviderAgent(provider)

    result = _invoke_agent(
        agent_name="investigation",
        model_name="gemini-primary",
        fallback_model=None,
        fallback_enabled=False,
        tracker=tracker,
        max_retries=2,
        build=build,
        payload={"messages": []},
        operation="plan_experiment",
        openrouter_models=["example/limited:free", "example/available:free"],
        openrouter_api_key="not-sent",
        auto_fallback=True,
    )

    assert result == {"provider": "openrouter"}
    snapshot = tracker.snapshot()
    assert [item.model for item in snapshot.request_log] == [
        "gemini-primary",
        "example/limited:free",
        "example/available:free",
    ]
    assert snapshot.requests_by_model["example/limited:free"] == 1
    assert snapshot.request_log[1].upstream_provider == "Phala"
    assert snapshot.fallback_history[-1].to_model == "example/available:free"


def test_multiple_unavailable_free_models_end_providers_exhausted(monkeypatch) -> None:
    monkeypatch.setattr("raceproof.model.validate_openrouter_free_model", lambda model: object())
    tracker = UsageTracker(UsageLimits(8, 6, 2, 10_000))
    with pytest.raises(ProvidersExhaustedError, match="all configured OpenRouter free models"):
        _invoke_agent(
            agent_name="investigation",
            model_name="gemini-primary",
            fallback_model=None,
            fallback_enabled=False,
            tracker=tracker,
            max_retries=2,
            build=lambda model, provider: ProviderAgent(
                provider,
                ModelQuotaExceededError("quota exhausted")
                if provider == "gemini"
                else SharedPool429("Phala" if model.endswith("one:free") else "Together"),
            ),
            payload={"messages": []},
            operation="plan_experiment",
            openrouter_models=["example/one:free", "example/two:free"],
            openrouter_api_key="not-sent",
            auto_fallback=True,
        )
    assert tracker.snapshot().total_requests == 3


def test_global_budget_stops_model_fallback_sequence(monkeypatch) -> None:
    monkeypatch.setattr("raceproof.model.validate_openrouter_free_model", lambda model: object())
    tracker = UsageTracker(UsageLimits(2, 2, 1, 10_000))
    with pytest.raises(BudgetExceededError, match="global request budget"):
        _invoke_agent(
            agent_name="investigation",
            model_name="gemini-primary",
            fallback_model=None,
            fallback_enabled=False,
            tracker=tracker,
            max_retries=0,
            build=lambda model, provider: ProviderAgent(
                provider,
                ModelQuotaExceededError("quota exhausted")
                if provider == "gemini"
                else SharedPool429("Phala"),
            ),
            payload={"messages": []},
            operation="plan_experiment",
            openrouter_models=["example/one:free", "example/two:free"],
            openrouter_api_key="not-sent",
            auto_fallback=True,
        )
    assert tracker.snapshot().total_requests == 2
