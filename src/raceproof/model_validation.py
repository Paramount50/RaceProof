from __future__ import annotations

from typing import Literal

from langchain.agents import create_agent
from langchain.tools import tool
from pydantic import BaseModel

from raceproof.model import _availability, _called_tools, _gemini, _openrouter
from raceproof.providers import validate_openrouter_free_model
from raceproof.security import redact_sensitive
from raceproof.usage import StructuredOutputError, UsageTracker


class CapabilityResponse(BaseModel):
    model_role: Literal["investigation", "repair"]
    tool_value: str
    structured_output_ok: Literal[True]


class CapabilityResult(BaseModel):
    role: str
    model: str
    provider: str = "gemini"
    passed: bool
    tool_calling: bool = False
    structured_output: bool = False
    upstream_provider: str | None = None
    limit_source: str | None = None
    error: str | None = None


def validate_model_capability(
    model: str,
    role: Literal["investigation", "repair"],
    tracker: UsageTracker,
    max_retries: int,
    api_key: str | None = None,
    provider: Literal["gemini", "openrouter"] = "gemini",
) -> CapabilityResult:
    if provider == "gemini":
        available, reason = _availability(model, api_key)
        if not available:
            return CapabilityResult(
                role=role, model=model, provider=provider, passed=False, error=reason
            )
    elif not api_key or api_key == "placeholder":
        return CapabilityResult(
            role=role,
            model=model,
            provider=provider,
            passed=False,
            error="OPENROUTER_API_KEY is not configured",
        )

    @tool
    def capability_probe() -> str:
        """Return the required value for this model capability check."""
        return "raceproof-capability-ok"

    try:
        if provider == "openrouter":
            validate_openrouter_free_model(model)
        chat_model = (
            _openrouter(model, api_key)
            if provider == "openrouter" and api_key
            else _gemini(model, api_key)
        )
        agent = create_agent(
            chat_model,
            [capability_probe],
            system_prompt=(
                "Call capability_probe exactly once, then return the requested structured "
                "response. Do not answer from memory."
            ),
            response_format=CapabilityResponse,
            name=f"{role}_capability_probe",
        )
        result = tracker.invoke_with_retry(
            agent=role,
            model=model,
            invoke=lambda config: agent.invoke(
                {"messages": [{"role": "user", "content": f"Validate the {role} model role."}]},
                config=config,
            ),
            max_retries=max_retries,
            provider=provider,
            operation="capability_probe",
        )
        response = CapabilityResponse.model_validate(result["structured_response"])
        tool_ok = any(call.get("name") == "capability_probe" for call in _called_tools(result))
        structured_ok = response.tool_value == "raceproof-capability-ok"
        if not tool_ok or not structured_ok:
            raise StructuredOutputError("model did not complete the required tool and schema probe")
        return CapabilityResult(
            role=role,
            model=model,
            provider=provider,
            passed=True,
            tool_calling=True,
            structured_output=True,
        )
    except Exception as exc:  # noqa: BLE001 - validation returns structured failure evidence
        request_log = tracker.snapshot().request_log
        record = request_log[-1] if request_log and request_log[-1].model == model else None
        return CapabilityResult(
            role=role,
            model=model,
            provider=provider,
            passed=False,
            upstream_provider=record.upstream_provider if record else None,
            limit_source=record.limit_source if record else None,
            error=redact_sensitive(exc),
        )
