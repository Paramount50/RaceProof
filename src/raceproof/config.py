from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RACEPROOF_",
        env_file=PROJECT_ROOT / ".env",
        extra="ignore",
    )

    database_url: str = (
        "postgresql+psycopg://raceproof:raceproof_dev_only@localhost:55432/raceproof"
    )
    investigation_model: str = "gemini-3.5-flash-lite"
    repair_model: str = "gemini-3.1-flash-lite"
    fallback_model: str = "gemini-3.8-flash"
    enable_model_fallback: bool = False
    provider_order: str = "gemini,openrouter"
    auto_fallback: bool = True
    gemini_investigation_model: str | None = None
    gemini_repair_model: str | None = None
    openrouter_investigation_models: str = (
        "nvidia/nemotron-3.5-super-vl:free,"
        "dots-studio/dots-3-note-preview:free,"
        "nvidia/nemotron-3-super-120b-a12b:free,"
        "xingchen-agi/xing4.0-29b-a4b:free"
    )
    openrouter_repair_models: str = (
        "nvidia/nemotron-3.5-super-vl:free,"
        "dots-studio/dots-3-note-preview:free,"
        "nvidia/nemotron-3-super-120b-a12b:free,"
        "xingchen-agi/xing4.0-29b-a4b:free"
    )
    openrouter_free_only: bool = True
    allow_paid_models: bool = False
    model_name: str | None = None
    model_max_retries: int = Field(default=2, ge=0, le=5)
    max_retry_wait_seconds: float = Field(default=3.0, ge=0, le=10)
    max_fallback_duration_seconds: float = Field(default=120.0, gt=0, le=600)
    max_model_requests: int = Field(default=14, ge=1, le=100)
    max_investigation_requests: int = Field(default=6, ge=1, le=50)
    max_repair_requests: int = Field(default=8, ge=1, le=50)
    min_repair_request_reserve: int = Field(default=6, ge=1, le=50)
    max_total_tokens: int = Field(default=60_000, ge=1_000, le=2_000_000)
    max_request_input_tokens: int = Field(default=24_000, ge=1_000, le=200_000)
    max_repair_context_tokens: int = Field(default=12_000, ge=1_000, le=100_000)
    model_pricing_json: str | None = None
    model_pricing_verified: bool = False
    allow_nonlocal_database: bool = False
    gemini_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    )
    openrouter_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="OPENROUTER_API_KEY",
    )
    langsmith_tracing: bool = Field(
        default=False,
        validation_alias=AliasChoices("LANGSMITH_TRACING", "RACEPROOF_LANGSMITH_TRACING"),
    )
    langsmith_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("LANGSMITH_API_KEY", "RACEPROOF_LANGSMITH_API_KEY"),
    )
    langsmith_project: str = Field(
        default="raceproof-local-dev",
        validation_alias=AliasChoices("LANGSMITH_PROJECT", "RACEPROOF_LANGSMITH_PROJECT"),
    )
    langsmith_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LANGSMITH_ENDPOINT", "RACEPROOF_LANGSMITH_ENDPOINT"),
    )
    langsmith_hide_inputs: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "LANGSMITH_HIDE_INPUTS", "RACEPROOF_LANGSMITH_HIDE_INPUTS"
        ),
    )
    langsmith_hide_outputs: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "LANGSMITH_HIDE_OUTPUTS", "RACEPROOF_LANGSMITH_HIDE_OUTPUTS"
        ),
    )
    max_repair_attempts: int = Field(default=3, ge=1, le=5)
    max_investigation_attempts: int = Field(default=3, ge=1, le=5)
    schedule_timeout_seconds: float = Field(default=8.0, gt=0, le=60)
    benchmark_runs: int = Field(default=20, ge=3, le=200)
    performance_regression_limit: float = Field(default=2.5, ge=1.0, le=10.0)
    artifact_dir: Path = Path("artifacts")
    external_upload_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    external_extracted_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    external_max_files: int = Field(default=1000, ge=1, le=10_000)
    external_source_max_bytes: int = Field(default=1024 * 1024, ge=1024)

    @model_validator(mode="after")
    def validate_budget_reservation(self) -> Settings:
        providers = self.provider_sequence
        if self.auto_fallback and providers != ["gemini", "openrouter"]:
            raise ValueError(
                "automatic fallback requires RACEPROOF_PROVIDER_ORDER=gemini,openrouter"
            )
        if not self.openrouter_free_only or self.allow_paid_models:
            raise ValueError(
                "RaceProof fails closed: OpenRouter must be free-only and paid models disabled"
            )
        for role, models in (
            ("investigation", self.openrouter_investigation_model_list),
            ("repair", self.openrouter_repair_model_list),
        ):
            if not models:
                raise ValueError(f"OpenRouter {role} fallback model list cannot be empty")
            if any(not model.endswith(":free") for model in models):
                raise ValueError(
                    f"OpenRouter {role} fallback list may contain only explicit :free model IDs"
                )
        if self.min_repair_request_reserve > self.max_repair_requests:
            raise ValueError(
                "RACEPROOF_MIN_REPAIR_REQUEST_RESERVE cannot exceed RACEPROOF_MAX_REPAIR_REQUESTS"
            )
        required = self.max_investigation_requests + self.min_repair_request_reserve
        if self.max_model_requests < required:
            raise ValueError(
                "RACEPROOF_MAX_MODEL_REQUESTS must be at least "
                "RACEPROOF_MAX_INVESTIGATION_REQUESTS + "
                f"RACEPROOF_MIN_REPAIR_REQUEST_RESERVE ({required}) so investigation cannot "
                "consume the Repair Agent reserve"
            )
        if self.max_repair_context_tokens > self.max_request_input_tokens:
            raise ValueError(
                "RACEPROOF_MAX_REPAIR_CONTEXT_TOKENS cannot exceed "
                "RACEPROOF_MAX_REQUEST_INPUT_TOKENS"
            )
        return self

    @property
    def resolved_investigation_model(self) -> str:
        return self.model_name or self.gemini_investigation_model or self.investigation_model

    @property
    def resolved_repair_model(self) -> str:
        return self.model_name or self.gemini_repair_model or self.repair_model

    @property
    def gemini_api_key_value(self) -> str | None:
        value = self.gemini_api_key.get_secret_value() if self.gemini_api_key else None
        return None if value in {None, "", "placeholder"} else value

    @property
    def openrouter_api_key_value(self) -> str | None:
        value = self.openrouter_api_key.get_secret_value() if self.openrouter_api_key else None
        return None if value in {None, "", "placeholder"} else value

    @property
    def langsmith_api_key_value(self) -> str | None:
        value = self.langsmith_api_key.get_secret_value() if self.langsmith_api_key else None
        return None if value in {None, "", "placeholder"} else value

    @staticmethod
    def _model_list(value: str) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))

    @property
    def openrouter_investigation_model_list(self) -> list[str]:
        return self._model_list(self.openrouter_investigation_models)

    @property
    def openrouter_repair_model_list(self) -> list[str]:
        return self._model_list(self.openrouter_repair_models)

    @property
    def provider_sequence(self) -> list[str]:
        providers = [
            item.strip().lower() for item in self.provider_order.split(",") if item.strip()
        ]
        if not providers or any(item not in {"gemini", "openrouter"} for item in providers):
            raise ValueError("RACEPROOF_PROVIDER_ORDER may contain only gemini and openrouter")
        return providers


@lru_cache
def get_settings() -> Settings:
    return Settings()
