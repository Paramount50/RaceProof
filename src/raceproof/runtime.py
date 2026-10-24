from __future__ import annotations

import os

from raceproof.config import Settings
from raceproof.usage import UsageLimits, UsageTracker


def build_usage_tracker(settings: Settings) -> UsageTracker:
    return UsageTracker(
        UsageLimits(
            max_model_requests=settings.max_model_requests,
            max_investigation_requests=settings.max_investigation_requests,
            max_repair_requests=settings.max_repair_requests,
            max_total_tokens=settings.max_total_tokens,
            max_request_input_tokens=settings.max_request_input_tokens,
        ),
        pricing_json=settings.model_pricing_json,
        pricing_verified=settings.model_pricing_verified,
    )


def model_status(settings: Settings) -> dict[str, object]:
    key = settings.gemini_api_key_value
    openrouter_key = settings.openrouter_api_key_value
    env_file = Settings.model_config["env_file"]
    dotenv_keys: set[str] = set()
    if env_file and env_file.exists():
        dotenv_keys = {
            line.split("=", 1)[0].strip()
            for line in env_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#") and "=" in line
        }
    budget_names = (
        "RACEPROOF_MAX_MODEL_REQUESTS",
        "RACEPROOF_MAX_INVESTIGATION_REQUESTS",
        "RACEPROOF_MAX_REPAIR_REQUESTS",
        "RACEPROOF_MIN_REPAIR_REQUEST_RESERVE",
        "RACEPROOF_MAX_TOTAL_TOKENS",
        "RACEPROOF_MODEL_MAX_RETRIES",
        "RACEPROOF_MAX_RETRY_WAIT_SECONDS",
        "RACEPROOF_MAX_REQUEST_INPUT_TOKENS",
        "RACEPROOF_MAX_REPAIR_CONTEXT_TOKENS",
    )
    return {
        "key_configured": bool(key and key != "placeholder"),
        "providers": {
            "order": settings.provider_sequence,
            "auto_fallback": settings.auto_fallback,
            "gemini": {
                "key_configured": bool(key and key != "placeholder"),
                "investigation_model": settings.resolved_investigation_model,
                "repair_model": settings.resolved_repair_model,
            },
            "openrouter": {
                "key_configured": bool(openrouter_key and openrouter_key != "placeholder"),
                "investigation_models": settings.openrouter_investigation_model_list,
                "repair_models": settings.openrouter_repair_model_list,
                "free_only": settings.openrouter_free_only,
                "paid_models_allowed": settings.allow_paid_models,
            },
        },
        "investigation_model": settings.resolved_investigation_model,
        "repair_model": settings.resolved_repair_model,
        "fallback_model": settings.fallback_model,
        "fallback_enabled": settings.enable_model_fallback,
        "budgets": {
            "max_model_requests": settings.max_model_requests,
            "max_investigation_requests": settings.max_investigation_requests,
            "max_repair_requests": settings.max_repair_requests,
            "min_repair_request_reserve": settings.min_repair_request_reserve,
            "max_total_tokens": settings.max_total_tokens,
            "model_max_retries": settings.model_max_retries,
            "max_retry_wait_seconds": settings.max_retry_wait_seconds,
            "max_request_input_tokens": settings.max_request_input_tokens,
            "max_repair_context_tokens": settings.max_repair_context_tokens,
        },
        "configuration": {
            "env_file": str(env_file),
            "env_file_exists": bool(env_file and env_file.exists()),
            "process_environment_overrides_dotenv": True,
            "restart_fastapi_after_change": True,
            "sources": {
                name: "process_environment"
                if name in os.environ
                else ("project_dotenv" if name in dotenv_keys else "default")
                for name in budget_names
            },
        },
    }
