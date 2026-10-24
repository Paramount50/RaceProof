from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from raceproof.api import app
from raceproof.contracts import (
    REQUIRED_VERIFICATION_GATES,
    FinalStatus,
    PatchValidation,
    RepairAttempt,
    RunReport,
    VerificationReport,
)
from raceproof.review_store import ReviewStore


def write_verified_report(root: Path, run_id: str) -> Path:
    candidate_diff = "diff --git a/src/app.py b/src/app.py\n+atomic_update()\n"
    report = RunReport(
        run_id=run_id,
        target_id="inventory_oversell",
        target_name="Inventory oversell",
        target_operation="inventory_app.operations:purchase_inventory",
        invariant_name="inventory_conservation",
        invariant_description="Initial stock equals final stock plus committed orders.",
        status=FinalStatus.VERIFIED_CANDIDATE,
        final_diff=candidate_diff,
        attempts=[
            RepairAttempt(
                number=1,
                generated_diff=candidate_diff,
                patch_validation=PatchValidation(
                    accepted=True, errors=[], changed_paths=["src/app.py"]
                ),
                verification=VerificationReport(
                    passed=True,
                    replay_mode="exact",
                    gates={name: True for name in REQUIRED_VERIFICATION_GATES},
                    schedule_results=[],
                    regression_output="tests passed",
                    performance=None,
                    limitations=[],
                ),
            )
        ],
    )
    path = root / "artifacts" / "runs" / run_id / "report.json"
    path.parent.mkdir(parents=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def test_review_api_gets_state_and_creates_decision(tmp_path: Path, monkeypatch) -> None:
    run_id = "a1b2c3d4e5f6"
    report_path = write_verified_report(tmp_path, run_id)
    report_before = report_path.read_bytes()
    monkeypatch.setattr(
        "raceproof.api._review_store", lambda: ReviewStore(tmp_path, Path("artifacts"))
    )
    client = TestClient(app)

    state_response = client.get(f"/api/runs/{run_id}/review")
    state = state_response.json()
    decision_response = client.post(
        f"/api/runs/{run_id}/review",
        json={
            "decision": "APPROVED",
            "reviewer": "dashboard-reviewer",
            "comment": "Evidence accepted.",
            "expected_report_sha256": state["report_sha256"],
            "expected_candidate_diff_sha256": state["candidate_diff_sha256"],
        },
    )

    assert state_response.status_code == 200
    assert state["eligible"] is True
    assert decision_response.status_code == 201
    assert decision_response.json()["decision"] == "APPROVED"
    assert report_path.read_bytes() == report_before
    restarted_state = client.get(f"/api/runs/{run_id}/review").json()
    assert restarted_state["deterministic_status"] == "VERIFIED_CANDIDATE"
    assert restarted_state["review"]["decision"] == "APPROVED"
    assert restarted_state["eligible"] is False


def test_review_api_maps_stale_hash_and_corrupt_report_to_conflict(
    tmp_path: Path, monkeypatch
) -> None:
    run_id = "a1b2c3d4e5f6"
    report_path = write_verified_report(tmp_path, run_id)
    monkeypatch.setattr(
        "raceproof.api._review_store", lambda: ReviewStore(tmp_path, Path("artifacts"))
    )
    client = TestClient(app)
    state = client.get(f"/api/runs/{run_id}/review").json()

    stale = client.post(
        f"/api/runs/{run_id}/review",
        json={
            "decision": "REJECTED",
            "expected_report_sha256": "0" * 64,
            "expected_candidate_diff_sha256": state["candidate_diff_sha256"],
        },
    )
    report_path.write_text("{corrupted", encoding="utf-8")
    corrupted = client.get(f"/api/runs/{run_id}/review")

    assert stale.status_code == 409
    assert "stale" in stale.json()["detail"]
    assert corrupted.status_code == 409
    assert "validation" in corrupted.json()["detail"]


def test_review_api_rejects_non_verified_persisted_run(tmp_path: Path, monkeypatch) -> None:
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
    monkeypatch.setattr(
        "raceproof.api._review_store", lambda: ReviewStore(tmp_path, Path("artifacts"))
    )
    client = TestClient(app)
    state = client.get(f"/api/runs/{run_id}/review").json()

    response = client.post(
        f"/api/runs/{run_id}/review",
        json={
            "decision": "APPROVED",
            "expected_report_sha256": state["report_sha256"],
            "expected_candidate_diff_sha256": "0" * 64,
        },
    )

    assert response.status_code == 409
    assert "VERIFIED_CANDIDATE" in response.json()["detail"]


def test_review_api_concurrent_requests_create_only_one_decision(
    tmp_path: Path, monkeypatch
) -> None:
    run_id = "a1b2c3d4e5f6"
    write_verified_report(tmp_path, run_id)
    monkeypatch.setattr(
        "raceproof.api._review_store", lambda: ReviewStore(tmp_path, Path("artifacts"))
    )
    state = TestClient(app).get(f"/api/runs/{run_id}/review").json()

    def submit(decision: str) -> int:
        return TestClient(app).post(
            f"/api/runs/{run_id}/review",
            json={
                "decision": decision,
                "expected_report_sha256": state["report_sha256"],
                "expected_candidate_diff_sha256": state["candidate_diff_sha256"],
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(submit, ["APPROVED", "REJECTED"]))

    assert sorted(statuses) == [201, 409]
