from pathlib import Path

import pytest
from pydantic import ValidationError

from raceproof.config import Settings
from raceproof.runtime import model_status


def test_global_budget_must_preserve_repair_reserve() -> None:
    with pytest.raises(ValidationError, match="Repair Agent reserve"):
        Settings(
            _env_file=None,
            max_model_requests=12,
            max_investigation_requests=12,
            max_repair_requests=8,
            min_repair_request_reserve=6,
        )


@pytest.mark.parametrize("investigation", [5, 12, 20])
def test_effective_budget_accepts_configurable_investigation_limits(
    investigation: int,
) -> None:
    settings = Settings(
        _env_file=None,
        max_model_requests=investigation + 8,
        max_investigation_requests=investigation,
        max_repair_requests=8,
        min_repair_request_reserve=6,
    )
    status = model_status(settings)
    assert status["budgets"]["max_investigation_requests"] == investigation
    assert status["budgets"]["max_model_requests"] == investigation + 8


def test_process_environment_overrides_dotenv_and_is_reported(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("RACEPROOF_MAX_MODEL_REQUESTS=14\n", encoding="utf-8")
    monkeypatch.setenv("RACEPROOF_MAX_MODEL_REQUESTS", "22")
    settings = Settings(
        _env_file=env_file,
        max_investigation_requests=6,
        max_repair_requests=8,
        min_repair_request_reserve=6,
    )
    assert settings.max_model_requests == 22
    assert (
        model_status(settings)["configuration"]["sources"]["RACEPROOF_MAX_MODEL_REQUESTS"]
        == "process_environment"
    )


def test_openrouter_fallback_lists_reject_non_free_models() -> None:
    with pytest.raises(ValidationError, match="only explicit :free model IDs"):
        Settings(
            _env_file=None,
            openrouter_investigation_models="paid/model",
        )
