from fastapi.testclient import TestClient

from raceproof.api import app
from raceproof.config import get_settings
from raceproof.security import redact_sensitive


def test_api_lists_generic_target_metadata() -> None:
    response = TestClient(app).get("/api/targets")
    assert response.status_code == 200
    targets = response.json()
    assert {target["id"] for target in targets} == {
        "inventory_oversell",
        "booking_double_reservation",
    }
    assert all(target["actors"] == ["A", "B"] for target in targets)


def test_api_rejects_unknown_target_without_starting_run() -> None:
    response = TestClient(app).post("/api/runs", json={"target_id": "not_registered"})
    assert response.status_code == 422
    assert "unknown target" in response.json()["detail"]


def test_api_model_status_never_exposes_key(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-value")
    monkeypatch.setenv("OPENROUTER_API_KEY", "other-super-secret-value")
    get_settings.cache_clear()
    response = TestClient(app).get("/api/model-status")
    assert response.status_code == 200
    assert response.json()["key_configured"] is True
    assert "super-secret-value" not in response.text
    assert "other-super-secret-value" not in response.text
    get_settings.cache_clear()


def test_dotenv_loaded_secret_can_be_explicitly_redacted() -> None:
    assert redact_sensitive("provider error secret-value", "secret-value") == (
        "provider error [REDACTED]"
    )
