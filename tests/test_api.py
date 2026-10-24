from fastapi.testclient import TestClient

from raceproof.api import app
from raceproof.config import get_settings
from raceproof.contracts import FinalStatus, RunReport
from raceproof.report_store import ReportStore
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


def test_api_reads_valid_persisted_report_without_active_run(tmp_path, monkeypatch) -> None:
    run_id = "a1b2c3d4e5f6"
    report = RunReport(
        run_id=run_id,
        target_id="inventory_oversell",
        target_name="Inventory oversell",
        target_operation="inventory_app.operations:purchase_inventory",
        invariant_name="inventory_conservation",
        invariant_description="Initial stock equals final stock plus committed orders.",
        status=FinalStatus.INTERRUPTED,
    )
    path = tmp_path / "artifacts" / "runs" / run_id / "report.json"
    path.parent.mkdir(parents=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    store = ReportStore(tmp_path, tmp_path / "artifacts")
    monkeypatch.setattr("raceproof.api._persisted_report", store.load)

    response = TestClient(app).get(f"/api/runs/{run_id}")
    report_response = TestClient(app).get(f"/api/runs/{run_id}/report")

    assert response.status_code == 200
    assert response.json()["source"] == "persisted"
    assert response.json()["status"] == "INTERRUPTED"
    assert report_response.status_code == 200
    assert report_response.json()["run_id"] == run_id


def test_api_does_not_accept_path_like_run_id() -> None:
    response = TestClient(app).get("/api/runs/%2E%2E%2Fsecret/report")
    assert response.status_code in {404, 422}
