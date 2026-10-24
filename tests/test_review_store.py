from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import pytest

from raceproof.contracts import (
    REQUIRED_VERIFICATION_GATES,
    FinalStatus,
    PatchValidation,
    RepairAttempt,
    RunReport,
    VerificationReport,
)
from raceproof.report_store import InvalidReportArtifact
from raceproof.review_store import (
    ReviewAlreadyExists,
    ReviewDecision,
    ReviewHashMismatch,
    ReviewIneligible,
    ReviewStore,
    ReviewSubmission,
)


def make_verified_report(run_id: str) -> RunReport:
    candidate_diff = "diff --git a/src/app.py b/src/app.py\n+atomic_update()\n"
    verification = VerificationReport(
        passed=True,
        replay_mode="exact",
        gates={name: True for name in REQUIRED_VERIFICATION_GATES},
        schedule_results=[],
        regression_output="tests passed",
        performance=None,
        limitations=[],
    )
    return RunReport(
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
                    accepted=True,
                    errors=[],
                    changed_paths=["src/app.py"],
                ),
                verification=verification,
            )
        ],
    )


def write_report(root: Path, report: RunReport) -> Path:
    path = root / "artifacts" / "runs" / report.run_id / "report.json"
    path.parent.mkdir(parents=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def submission_for(store: ReviewStore, run_id: str, decision: ReviewDecision) -> ReviewSubmission:
    state = store.state(run_id)
    assert state.candidate_diff_sha256 is not None
    return ReviewSubmission(
        decision=decision,
        reviewer="reviewer@example.test",
        comment="Reviewed deterministic evidence.",
        expected_report_sha256=state.report_sha256,
        expected_candidate_diff_sha256=state.candidate_diff_sha256,
    )


def _process_create_review(root: str, run_id: str, decision: str) -> str:
    store = ReviewStore(Path(root), Path("artifacts"))
    submission = submission_for(store, run_id, ReviewDecision(decision))
    try:
        return store.create(run_id, submission).decision.value
    except ReviewAlreadyExists:
        return "CONFLICT"


@pytest.mark.parametrize("decision", [ReviewDecision.APPROVED, ReviewDecision.REJECTED])
def test_review_store_persists_immutable_decision_without_changing_report(
    tmp_path: Path, decision: ReviewDecision
) -> None:
    run_id = "a1b2c3d4e5f6"
    report_path = write_report(tmp_path, make_verified_report(run_id))
    report_before = report_path.read_bytes()
    store = ReviewStore(tmp_path, Path("artifacts"))

    review = store.create(run_id, submission_for(store, run_id, decision))

    assert review.decision == decision
    assert report_path.read_bytes() == report_before
    restarted = ReviewStore(tmp_path, Path("artifacts")).state(run_id)
    assert restarted.review == review
    assert restarted.deterministic_status == FinalStatus.VERIFIED_CANDIDATE
    assert restarted.eligible is False
    with pytest.raises(ReviewAlreadyExists):
        store.create(run_id, submission_for(store, run_id, decision))


def test_review_store_rejects_non_verified_status(tmp_path: Path) -> None:
    run_id = "a1b2c3d4e5f6"
    report = make_verified_report(run_id).model_copy(
        update={"status": FinalStatus.INTERRUPTED, "final_diff": None, "attempts": []}
    )
    write_report(tmp_path, report)
    store = ReviewStore(tmp_path, Path("artifacts"))
    state = store.state(run_id)
    submission = ReviewSubmission(
        decision=ReviewDecision.APPROVED,
        expected_report_sha256=state.report_sha256,
        expected_candidate_diff_sha256="0" * 64,
    )

    with pytest.raises(ReviewIneligible, match="VERIFIED_CANDIDATE"):
        store.create(run_id, submission)


@pytest.mark.parametrize("stale_field", ["report", "candidate"])
def test_review_store_rejects_stale_hashes(tmp_path: Path, stale_field: str) -> None:
    run_id = "a1b2c3d4e5f6"
    write_report(tmp_path, make_verified_report(run_id))
    store = ReviewStore(tmp_path, Path("artifacts"))
    submission = submission_for(store, run_id, ReviewDecision.APPROVED)
    if stale_field == "report":
        submission = submission.model_copy(update={"expected_report_sha256": "0" * 64})
    else:
        submission = submission.model_copy(
            update={"expected_candidate_diff_sha256": "0" * 64}
        )

    with pytest.raises(ReviewHashMismatch, match="stale"):
        store.create(run_id, submission)


def test_review_store_rejects_corrupted_report(tmp_path: Path) -> None:
    run_id = "a1b2c3d4e5f6"
    path = tmp_path / "artifacts" / "runs" / run_id / "report.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not valid JSON", encoding="utf-8")

    with pytest.raises(InvalidReportArtifact):
        ReviewStore(tmp_path, Path("artifacts")).state(run_id)


@pytest.mark.parametrize("corruption", ["empty_diff", "failed_gate"])
def test_review_store_rejects_corrupted_verification_claims(
    tmp_path: Path, corruption: str
) -> None:
    report = make_verified_report("a1b2c3d4e5f6").model_dump(mode="json")
    if corruption == "empty_diff":
        report["final_diff"] = ""
    else:
        report["attempts"][0]["verification"]["gates"]["business_invariant"] = False
    path = tmp_path / "artifacts" / "runs" / report["run_id"] / "report.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(InvalidReportArtifact):
        ReviewStore(tmp_path, Path("artifacts")).state(report["run_id"])


def test_review_creation_is_atomic_across_processes(tmp_path: Path) -> None:
    run_id = "a1b2c3d4e5f6"
    write_report(tmp_path, make_verified_report(run_id))

    with ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn")) as executor:
        outcomes = list(
            executor.map(
                _process_create_review,
                [str(tmp_path), str(tmp_path)],
                [run_id, run_id],
                [ReviewDecision.APPROVED.value, ReviewDecision.REJECTED.value],
            )
        )

    assert sorted(outcomes).count("CONFLICT") == 1
    assert len({*outcomes} & {"APPROVED", "REJECTED"}) == 1
    state = ReviewStore(tmp_path, Path("artifacts")).state(run_id)
    assert state.review is not None
    assert state.review.decision.value in outcomes
