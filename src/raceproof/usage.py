from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from langchain_core.callbacks import BaseCallbackHandler
from pydantic import BaseModel, Field

T = TypeVar("T")
logger = logging.getLogger("raceproof.model_requests")


class ModelRuntimeError(RuntimeError):
    code = "MODEL_ERROR"
    transient = False


class BudgetExceededError(ModelRuntimeError):
    code = "BUDGET_EXCEEDED"

    def __init__(self, limit_kind: str, used: int, configured: int) -> None:
        self.limit_kind = limit_kind
        self.used = used
        self.configured = configured
        super().__init__(
            f"local {limit_kind} exhausted: used {used} of {configured}; "
            "change the matching RACEPROOF budget setting and restart FastAPI"
        )


class ModelAuthenticationError(ModelRuntimeError):
    code = "AUTHENTICATION_ERROR"


class ModelUnavailableError(ModelRuntimeError):
    code = "MODEL_UNAVAILABLE"


class ModelConfigurationError(ModelRuntimeError):
    code = "MODEL_CONFIGURATION_ERROR"


class ProvidersExhaustedError(ModelRuntimeError):
    code = "PROVIDERS_EXHAUSTED"


class ModelRateLimitError(ModelRuntimeError):
    code = "RATE_LIMITED"
    transient = True


class ModelSharedPoolRateLimitError(ModelRateLimitError):
    code = "UPSTREAM_SHARED_POOL_RATE_LIMITED"
    transient = False

    def __init__(
        self,
        message: str,
        *,
        upstream_provider: str | None = None,
        limit_source: str | None = None,
    ) -> None:
        self.upstream_provider = upstream_provider
        self.limit_source = limit_source
        super().__init__(message)


class ModelQuotaExceededError(ModelRuntimeError):
    code = "QUOTA_EXCEEDED"


class ModelTransientError(ModelRuntimeError):
    code = "PROVIDER_TRANSIENT_ERROR"
    transient = True


class StructuredOutputError(ModelRuntimeError):
    code = "STRUCTURED_OUTPUT_ERROR"


class ContextLimitExceededError(BudgetExceededError):
    code = "CONTEXT_LIMIT_EXCEEDED"


class AgentUsage(BaseModel):
    requests: int = 0
    failed_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    unknown_token_responses: int = 0


class ModelRequestRecord(BaseModel):
    request_number: int
    agent_request_number: int
    agent: str
    operation: str
    provider: str
    model: str
    retry_index: int = 0
    fallback: bool = False
    status: str = "started"
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    error_code: str | None = None
    upstream_provider: str | None = None
    limit_source: str | None = None
    failure_reason: str | None = None
    message_count: int | None = None
    context_characters: int | None = None
    estimated_input_tokens: int | None = None


class ModelFallbackEvent(BaseModel):
    agent: str
    operation: str
    from_provider: str
    from_model: str
    to_provider: str
    to_model: str
    reason_code: str
    reason: str
    upstream_provider: str | None = None
    limit_source: str | None = None


class UsageSnapshot(BaseModel):
    total_requests: int = 0
    requests_by_agent: dict[str, int] = Field(default_factory=dict)
    requests_by_model: dict[str, int] = Field(default_factory=dict)
    requests_by_provider: dict[str, int] = Field(default_factory=dict)
    usage_by_agent: dict[str, AgentUsage] = Field(default_factory=dict)
    usage_by_model: dict[str, AgentUsage] = Field(default_factory=dict)
    usage_by_provider: dict[str, AgentUsage] = Field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    unknown_token_responses: int = 0
    failed_requests: int = 0
    rate_limit_errors: int = 0
    fallback_invocations: int = 0
    remaining_request_budget: int = 0
    remaining_investigation_budget: int = 0
    remaining_repair_budget: int = 0
    remaining_token_budget: int = 0
    budget_exhausted: bool = False
    warnings: list[str] = Field(default_factory=list)
    approximate_cost_usd: float | None = None
    cost_is_partial: bool = False
    configured_limits: dict[str, int] = Field(default_factory=dict)
    budget_exhausted_reason: str | None = None
    request_log: list[ModelRequestRecord] = Field(default_factory=list)
    fallback_history: list[ModelFallbackEvent] = Field(default_factory=list)


@dataclass(frozen=True)
class UsageLimits:
    max_model_requests: int = 12
    max_investigation_requests: int = 5
    max_repair_requests: int = 7
    max_total_tokens: int = 60_000
    max_request_input_tokens: int = 24_000


def classify_model_error(exc: BaseException) -> ModelRuntimeError:
    if isinstance(exc, ModelRuntimeError):
        return exc
    message = str(exc).lower()
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    status = status or getattr(response, "status_code", None)
    if status in {401, 403} or any(
        marker in message
        for marker in (
            "api key not valid",
            "api_key_invalid",
            "unauthenticated",
            "permission denied",
        )
    ):
        return ModelAuthenticationError(str(exc))
    if status == 404 or any(
        marker in message
        for marker in ("model not found", "unknown model", "invalid model", "not supported for")
    ):
        error = ModelConfigurationError(str(exc))
        details = _provider_error_details(exc)
        error.upstream_provider = details["upstream_provider"]
        error.limit_source = details["limit_source"]
        return error
    if status == 429 or "429" in message or "resource_exhausted" in message:
        details = _provider_error_details(exc)
        if details["limit_source"] == "upstream_provider_shared_pool" or (
            "upstream_provider_shared_pool" in message
        ):
            return ModelSharedPoolRateLimitError(
                str(exc),
                upstream_provider=details["upstream_provider"],
                limit_source=details["limit_source"] or "upstream_provider_shared_pool",
            )
        if any(
            marker in message
            for marker in ("per day", "daily", "quota exhausted", "billing", "project quota")
        ):
            return ModelQuotaExceededError(str(exc))
        return ModelRateLimitError(str(exc))
    if status is not None and int(status) >= 500:
        return ModelTransientError(str(exc))
    if any(marker in message for marker in ("timeout", "timed out", "connection", "temporarily")):
        return ModelTransientError(str(exc))
    if any(
        marker in message
        for marker in ("structured_response", "validation error", "tool call", "malformed")
    ):
        return StructuredOutputError(str(exc))
    return ModelRuntimeError(str(exc))


def _provider_error_details(exc: BaseException) -> dict[str, str | None]:
    values: list[object] = [getattr(exc, "body", None)]
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            values.append(response.json())
        except Exception as response_error:  # noqa: BLE001 - provider responses vary by SDK
            logger.debug("provider error response body is not JSON: %s", response_error)
    upstream_provider: str | None = None
    limit_source: str | None = None

    def inspect(value: object) -> None:
        nonlocal upstream_provider, limit_source
        if isinstance(value, dict):
            provider = value.get("provider_name") or value.get("provider")
            source = value.get("limit_source")
            if isinstance(provider, str):
                upstream_provider = provider
            if isinstance(source, str):
                limit_source = source
            for nested in value.values():
                inspect(nested)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested)

    for value in values:
        inspect(value)
    text = str(exc)
    if upstream_provider is None:
        match = re.search(r"provider_name['\"=: ]+([A-Za-z0-9_. -]+)", text)
        if match:
            upstream_provider = match.group(1).strip(" '\"")
    if limit_source is None and "upstream_provider_shared_pool" in text.lower():
        limit_source = "upstream_provider_shared_pool"
    return {"upstream_provider": upstream_provider, "limit_source": limit_source}


def retry_after_seconds(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


class _UsageCallback(BaseCallbackHandler):
    def __init__(
        self,
        tracker: UsageTracker,
        agent: str,
        operation: str,
        provider: str,
        model: str,
        fallback: bool,
        retry_index: int,
        message_count: int | None,
        context_characters: int | None,
        estimated_input_tokens: int | None,
    ) -> None:
        self.raise_error = True
        self.tracker = tracker
        self.agent = agent
        self.operation = operation
        self.provider = provider
        self.model = model
        self.fallback = fallback
        self.retry_index = retry_index
        self.message_count = message_count
        self.context_characters = context_characters
        self.estimated_input_tokens = estimated_input_tokens
        self.request_id: int | None = None

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        **kwargs: Any,
    ) -> None:
        self.request_id = self.tracker.start_request(
            self.agent,
            self.model,
            self.fallback,
            operation=self.operation,
            provider=self.provider,
            retry_index=self.retry_index,
            message_count=self.message_count,
            context_characters=self.context_characters,
            estimated_input_tokens=self.estimated_input_tokens,
        )

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        usage = _extract_usage(response)
        self.tracker.finish_request(
            self.agent, self.model, usage, self.request_id, provider=self.provider
        )

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        self.tracker.fail_request(
            self.agent,
            self.model,
            classify_model_error(error),
            self.request_id,
            provider=self.provider,
        )


def _extract_usage(response: Any) -> tuple[int, int, int] | None:
    generations = getattr(response, "generations", []) or []
    for group in generations:
        for generation in group:
            message = getattr(generation, "message", None)
            metadata = getattr(message, "usage_metadata", None) or {}
            if metadata:
                input_tokens = metadata.get("input_tokens")
                output_tokens = metadata.get("output_tokens")
                total_tokens = metadata.get("total_tokens")
                if input_tokens is not None and output_tokens is not None:
                    total = (
                        total_tokens if total_tokens is not None else input_tokens + output_tokens
                    )
                    return int(input_tokens), int(output_tokens), int(total)
            response_metadata = getattr(message, "response_metadata", None) or {}
            token_usage = response_metadata.get("token_usage") or response_metadata.get(
                "usage_metadata"
            )
            if token_usage:
                input_tokens = token_usage.get("prompt_token_count") or token_usage.get(
                    "input_tokens"
                )
                output_tokens = token_usage.get("candidates_token_count") or token_usage.get(
                    "output_tokens"
                )
                total_tokens = token_usage.get("total_token_count") or token_usage.get(
                    "total_tokens"
                )
                if input_tokens is not None and output_tokens is not None:
                    total = (
                        total_tokens if total_tokens is not None else input_tokens + output_tokens
                    )
                    return int(input_tokens), int(output_tokens), int(total)
    return None


class UsageTracker:
    def __init__(
        self,
        limits: UsageLimits,
        *,
        pricing_json: str | None = None,
        pricing_verified: bool = False,
        event_listener: Callable[[str, ModelRequestRecord], None] | None = None,
    ) -> None:
        self.limits = limits
        self._lock = threading.Lock()
        self._requests_by_agent: Counter[str] = Counter()
        self._requests_by_model: Counter[str] = Counter()
        self._requests_by_provider: Counter[str] = Counter()
        self._usage_by_agent: defaultdict[str, AgentUsage] = defaultdict(AgentUsage)
        self._usage_by_model: defaultdict[str, AgentUsage] = defaultdict(AgentUsage)
        self._usage_by_provider: defaultdict[str, AgentUsage] = defaultdict(AgentUsage)
        self._failed_requests = 0
        self._rate_limit_errors = 0
        self._fallback_invocations = 0
        self._warnings: list[str] = []
        self._budget_exhausted = False
        self._budget_exhausted_reason: str | None = None
        self._budget_error: BudgetExceededError | None = None
        self._request_log: list[ModelRequestRecord] = []
        self._fallback_history: list[ModelFallbackEvent] = []
        self._pricing = self._parse_pricing(pricing_json) if pricing_verified else {}
        self._pricing_verified = pricing_verified and bool(self._pricing)
        self._event_listener = event_listener

    def set_event_listener(
        self, listener: Callable[[str, ModelRequestRecord], None] | None
    ) -> None:
        self._event_listener = listener

    def _emit(self, action: str, record: ModelRequestRecord) -> None:
        if self._event_listener is None:
            return
        try:
            self._event_listener(action, record.model_copy())
        except Exception:  # pragma: no cover - observability must not break model execution
            logger.exception("model request event listener failed")

    @staticmethod
    def _parse_pricing(value: str | None) -> dict[str, dict[str, float]]:
        if not value:
            return {}
        try:
            raw = json.loads(value)
            return {
                str(model): {
                    "input_per_million": float(prices["input_per_million"]),
                    "output_per_million": float(prices["output_per_million"]),
                }
                for model, prices in raw.items()
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return {}

    def callback(
        self,
        agent: str,
        model: str,
        *,
        operation: str = "unspecified",
        provider: str = "gemini",
        fallback: bool = False,
        retry_index: int = 0,
        message_count: int | None = None,
        context_characters: int | None = None,
        estimated_input_tokens: int | None = None,
    ) -> BaseCallbackHandler:
        return _UsageCallback(
            self,
            agent,
            operation,
            provider,
            model,
            fallback,
            retry_index,
            message_count,
            context_characters,
            estimated_input_tokens,
        )

    def ensure_request_affordable(self, estimated_input_tokens: int) -> None:
        if estimated_input_tokens > self.limits.max_request_input_tokens:
            raise ContextLimitExceededError(
                "request input context",
                estimated_input_tokens,
                self.limits.max_request_input_tokens,
            )
        current = sum(item.total_tokens for item in self._usage_by_agent.values())
        remaining = self.limits.max_total_tokens - current
        if estimated_input_tokens > remaining:
            self._exhaust("remaining provider-reported token budget", current, self.limits.max_total_tokens)

    def _exhaust(self, kind: str, used: int, configured: int) -> None:
        error = BudgetExceededError(kind, used, configured)
        self._budget_exhausted = True
        self._budget_exhausted_reason = str(error)
        self._budget_error = error
        raise error

    def start_request(
        self,
        agent: str,
        model: str,
        fallback: bool,
        *,
        operation: str = "unspecified",
        provider: str = "gemini",
        retry_index: int = 0,
        message_count: int | None = None,
        context_characters: int | None = None,
        estimated_input_tokens: int | None = None,
    ) -> int:
        with self._lock:
            total = sum(self._requests_by_agent.values())
            agent_limit = (
                self.limits.max_investigation_requests
                if agent == "investigation"
                else self.limits.max_repair_requests
            )
            if total >= self.limits.max_model_requests:
                self._exhaust("global request budget", total, self.limits.max_model_requests)
            if self._requests_by_agent[agent] >= agent_limit:
                self._exhaust(
                    f"{agent} request budget", self._requests_by_agent[agent], agent_limit
                )
            current_tokens = sum(item.total_tokens for item in self._usage_by_agent.values())
            if current_tokens >= self.limits.max_total_tokens:
                self._exhaust(
                    "provider-reported token budget",
                    current_tokens,
                    self.limits.max_total_tokens,
                )
            self._requests_by_agent[agent] += 1
            self._requests_by_model[model] += 1
            self._requests_by_provider[provider] += 1
            self._usage_by_agent[agent].requests += 1
            self._usage_by_model[model].requests += 1
            self._usage_by_provider[provider].requests += 1
            if fallback:
                self._fallback_invocations += 1
            record = ModelRequestRecord(
                request_number=total + 1,
                agent_request_number=self._requests_by_agent[agent],
                agent=agent,
                operation=operation,
                provider=provider,
                model=model,
                retry_index=retry_index,
                fallback=fallback,
                message_count=message_count,
                context_characters=context_characters,
                estimated_input_tokens=estimated_input_tokens,
            )
            self._request_log.append(record)
            logger.info(
                "model request start request=%s agent=%s agent_request=%s operation=%s "
                "provider=%s model=%s retry=%s",
                record.request_number,
                agent,
                record.agent_request_number,
                operation,
                provider,
                model,
                retry_index,
            )
            self._emit("started", record)
            return record.request_number

    def finish_request(
        self,
        agent: str,
        model: str,
        usage: tuple[int, int, int] | None,
        request_id: int | None = None,
        *,
        provider: str = "gemini",
    ) -> None:
        with self._lock:
            record = self._record(request_id)
            if record:
                record.status = "succeeded"
            if usage is None:
                self._usage_by_agent[agent].unknown_token_responses += 1
                self._usage_by_model[model].unknown_token_responses += 1
                warning = f"Token usage metadata unavailable for {agent} request on {model}."
                if warning not in self._warnings:
                    self._warnings.append(warning)
                logger.info(
                    "model request end request=%s agent=%s model=%s tokens=unknown",
                    request_id,
                    agent,
                    model,
                )
                if record:
                    self._emit("completed", record)
                return
            input_tokens, output_tokens, total_tokens = usage
            for item in (
                self._usage_by_agent[agent],
                self._usage_by_model[model],
                self._usage_by_provider[provider],
            ):
                item.input_tokens += input_tokens
                item.output_tokens += output_tokens
                item.total_tokens += total_tokens
            if record:
                record.input_tokens = input_tokens
                record.output_tokens = output_tokens
                record.total_tokens = total_tokens
            logger.info(
                "model request end request=%s agent=%s model=%s input_tokens=%s "
                "output_tokens=%s total_tokens=%s",
                request_id,
                agent,
                model,
                input_tokens,
                output_tokens,
                total_tokens,
            )
            if record:
                self._emit("completed", record)
            if (
                sum(item.total_tokens for item in self._usage_by_agent.values())
                > self.limits.max_total_tokens
            ):
                self._budget_exhausted = True
                self._budget_exhausted_reason = (
                    f"local provider-reported token budget exceeded after response: used "
                    f"{sum(item.total_tokens for item in self._usage_by_agent.values())} of "
                    f"{self.limits.max_total_tokens}"
                )
                self._budget_error = BudgetExceededError(
                    "provider-reported token budget",
                    sum(item.total_tokens for item in self._usage_by_agent.values()),
                    self.limits.max_total_tokens,
                )

    def _record(self, request_id: int | None) -> ModelRequestRecord | None:
        if request_id is None or request_id < 1 or request_id > len(self._request_log):
            return None
        return self._request_log[request_id - 1]

    def fail_request(
        self,
        agent: str,
        model: str,
        error: ModelRuntimeError,
        request_id: int | None = None,
        *,
        provider: str = "gemini",
    ) -> None:
        with self._lock:
            self._failed_requests += 1
            self._usage_by_agent[agent].failed_requests += 1
            self._usage_by_model[model].failed_requests += 1
            self._usage_by_provider[provider].failed_requests += 1
            if isinstance(error, ModelRateLimitError):
                self._rate_limit_errors += 1
            record = self._record(request_id)
            if record:
                record.status = "failed"
                record.error_code = error.code
                record.failure_reason = str(error)[:500]
                record.upstream_provider = getattr(error, "upstream_provider", None)
                record.limit_source = getattr(error, "limit_source", None)
            warning = f"{error.code}: {error}"
            if warning not in self._warnings:
                self._warnings.append(warning[:500])
            logger.warning(
                "model request failed request=%s agent=%s model=%s code=%s",
                request_id,
                agent,
                model,
                error.code,
            )
            if record:
                self._emit("failed", record)

    def record_agent_failure(self, error: ModelRuntimeError) -> None:
        with self._lock:
            warning = f"{error.code}: {error}"
            if warning not in self._warnings:
                self._warnings.append(warning[:500])

    def record_provider_fallback(
        self,
        agent: str,
        from_provider: str,
        from_model: str,
        to_provider: str,
        to_model: str,
        reason: ModelRuntimeError,
        *,
        operation: str,
    ) -> None:
        with self._lock:
            message = (
                f"{from_provider}/{from_model} {reason.code} for {agent}; switching "
                f"{operation} to {to_provider}/{to_model}"
            )
            event = ModelFallbackEvent(
                agent=agent,
                operation=operation,
                from_provider=from_provider,
                from_model=from_model,
                to_provider=to_provider,
                to_model=to_model,
                reason_code=reason.code,
                reason=str(reason)[:500],
                upstream_provider=getattr(reason, "upstream_provider", None),
                limit_source=getattr(reason, "limit_source", None),
            )
            self._fallback_history.append(event)
            self._warnings.append(f"PROVIDER_FALLBACK: {message}")
            logger.warning(message)

    def ensure_within_budget(self) -> None:
        with self._lock:
            if self._budget_exhausted:
                raise self._budget_error or BudgetExceededError("unknown budget", 0, 0)

    def invoke_with_retry(
        self,
        *,
        agent: str,
        model: str,
        invoke: Callable[[dict[str, Any]], T],
        max_retries: int,
        fallback: bool = False,
        operation: str = "unspecified",
        provider: str = "gemini",
        max_retry_wait_seconds: float = 3.0,
        message_count: int | None = None,
        context_characters: int | None = None,
        estimated_input_tokens: int | None = None,
    ) -> T:
        if estimated_input_tokens is not None:
            self.ensure_request_affordable(estimated_input_tokens)
        for attempt in range(max_retries + 1):
            callback = self.callback(
                agent,
                model,
                operation=operation,
                provider=provider,
                fallback=fallback,
                retry_index=attempt,
                message_count=message_count,
                context_characters=context_characters,
                estimated_input_tokens=estimated_input_tokens,
            )
            try:
                result = invoke({"callbacks": [callback]})
                self.ensure_within_budget()
                return result
            except BudgetExceededError:
                raise
            except Exception as exc:
                error = classify_model_error(exc)
                self.record_agent_failure(error)
                if not error.transient or attempt >= max_retries:
                    raise error from exc
                delay = retry_after_seconds(exc)
                if delay is None:
                    delay = min(2**attempt, 8)
                time.sleep(min(delay, max_retry_wait_seconds))
        raise AssertionError("unreachable retry loop")

    def snapshot(self) -> UsageSnapshot:
        with self._lock:
            total_requests = sum(self._requests_by_agent.values())
            input_tokens = sum(item.input_tokens for item in self._usage_by_agent.values())
            output_tokens = sum(item.output_tokens for item in self._usage_by_agent.values())
            total_tokens = sum(item.total_tokens for item in self._usage_by_agent.values())
            unknown = sum(item.unknown_token_responses for item in self._usage_by_agent.values())
            cost: float | None = None
            cost_partial = False
            if self._pricing_verified:
                cost = 0.0
                for model, usage in self._usage_by_model.items():
                    prices = self._pricing.get(model)
                    if prices is None:
                        cost_partial = True
                        continue
                    cost += usage.input_tokens / 1_000_000 * prices["input_per_million"]
                    cost += usage.output_tokens / 1_000_000 * prices["output_per_million"]
                cost_partial = cost_partial or unknown > 0
            return UsageSnapshot(
                total_requests=total_requests,
                requests_by_agent=dict(self._requests_by_agent),
                requests_by_model=dict(self._requests_by_model),
                requests_by_provider=dict(self._requests_by_provider),
                usage_by_agent={
                    key: value.model_copy() for key, value in self._usage_by_agent.items()
                },
                usage_by_model={
                    key: value.model_copy() for key, value in self._usage_by_model.items()
                },
                usage_by_provider={
                    key: value.model_copy() for key, value in self._usage_by_provider.items()
                },
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                unknown_token_responses=unknown,
                failed_requests=self._failed_requests,
                rate_limit_errors=self._rate_limit_errors,
                fallback_invocations=self._fallback_invocations,
                remaining_request_budget=max(0, self.limits.max_model_requests - total_requests),
                remaining_investigation_budget=max(
                    0,
                    self.limits.max_investigation_requests
                    - self._requests_by_agent["investigation"],
                ),
                remaining_repair_budget=max(
                    0, self.limits.max_repair_requests - self._requests_by_agent["repair"]
                ),
                remaining_token_budget=max(0, self.limits.max_total_tokens - total_tokens),
                budget_exhausted=self._budget_exhausted,
                warnings=list(self._warnings),
                approximate_cost_usd=cost,
                cost_is_partial=cost_partial,
                configured_limits={
                    "max_model_requests": self.limits.max_model_requests,
                    "max_investigation_requests": self.limits.max_investigation_requests,
                    "max_repair_requests": self.limits.max_repair_requests,
                    "max_total_tokens": self.limits.max_total_tokens,
                    "max_request_input_tokens": self.limits.max_request_input_tokens,
                },
                budget_exhausted_reason=self._budget_exhausted_reason,
                request_log=[item.model_copy() for item in self._request_log],
                fallback_history=[item.model_copy() for item in self._fallback_history],
            )
