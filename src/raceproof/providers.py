from __future__ import annotations

import json
from functools import lru_cache
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field

from raceproof.usage import ModelConfigurationError

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


class OpenRouterModelMetadata(BaseModel):
    id: str
    name: str
    context_length: int
    prompt_price: float
    completion_price: float
    supported_parameters: list[str] = Field(default_factory=list)
    verified_free: bool
    supports_tools: bool
    supports_structured_output: bool


@lru_cache(maxsize=1)
def _openrouter_catalog() -> dict[str, dict[str, object]]:
    request = Request(
        OPENROUTER_MODELS_URL,
        headers={"User-Agent": "RaceProof/0.1 model-metadata-validation"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except Exception as exc:
        raise ModelConfigurationError(
            f"OpenRouter pricing metadata could not be verified: {type(exc).__name__}: {exc}"
        ) from exc
    return {
        str(item["id"]): item
        for item in payload.get("data", [])
        if isinstance(item, dict) and item.get("id")
    }


@lru_cache(maxsize=32)
def validate_openrouter_free_model(model_id: str) -> OpenRouterModelMetadata:
    raw = _openrouter_catalog().get(model_id)
    if raw is None:
        raise ModelConfigurationError(f"OpenRouter model is not in the live catalog: {model_id}")
    pricing = raw.get("pricing") or {}
    try:
        prompt_price = float(pricing["prompt"])
        completion_price = float(pricing["completion"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelConfigurationError(
            f"OpenRouter pricing is missing or invalid for {model_id}"
        ) from exc
    parameters = list(raw.get("supported_parameters") or [])
    metadata = OpenRouterModelMetadata(
        id=model_id,
        name=str(raw.get("name") or model_id),
        context_length=int(raw.get("context_length") or 0),
        prompt_price=prompt_price,
        completion_price=completion_price,
        supported_parameters=parameters,
        verified_free=(model_id.endswith(":free") and prompt_price == 0 and completion_price == 0),
        supports_tools="tools" in parameters,
        supports_structured_output=bool(
            {"structured_outputs", "response_format"}.intersection(parameters)
        ),
    )
    failures = []
    if not metadata.verified_free:
        failures.append("model ID and live input/output pricing are not all free")
    if not metadata.supports_tools:
        failures.append("live catalog does not declare tool support")
    if not metadata.supports_structured_output:
        failures.append("live catalog does not declare structured output support")
    if metadata.context_length < 32_768:
        failures.append("context length is below 32768")
    if failures:
        raise ModelConfigurationError(
            f"OpenRouter model {model_id} rejected: " + "; ".join(failures)
        )
    return metadata
