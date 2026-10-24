import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from raceproof.contracts import FinalStatus, InvariantResult, PatchProposal, RunReport, TargetConfig
from raceproof.target import TargetConfigurationError, TargetRegistry, load_target_config


def test_report_requires_human_approval_by_default() -> None:
    report = RunReport(
        run_id="test-run",
        target_id="demo",
        target_name="Demo",
        target_operation="demo:operation",
        invariant_name="demo_invariant",
        invariant_description="One thing remains true.",
        status=FinalStatus.REPRODUCED,
    )
    assert report.human_approval_required is True


def test_invariant_contains_named_generic_evidence() -> None:
    result = InvariantResult(
        name="exclusive_booking",
        passed=False,
        assertion="bookings <= 1",
        evidence={"bookings": 2, "maximum": 1},
        explanation="resource was booked twice",
    )
    assert not result.passed
    assert result.evidence["bookings"] > result.evidence["maximum"]


def test_registry_loads_two_external_applications() -> None:
    root = Path(__file__).resolve().parents[1]
    targets = TargetRegistry(root / "examples").list()
    assert {target.config.id for target in targets} == {
        "inventory_oversell",
        "booking_double_reservation",
    }
    assert all("src/raceproof" not in str(target.repository_root) for target in targets)


def test_contract_requires_developer_approved_invariant() -> None:
    root = Path(__file__).resolve().parents[1]
    raw = json.loads((root / "examples/inventory_app/raceproof.json").read_text())
    raw["invariant"]["approved"] = False
    with pytest.raises(ValidationError):
        TargetConfig.model_validate(raw)


def test_contract_rejects_path_traversal(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    raw = json.loads((root / "examples/inventory_app/raceproof.json").read_text())
    raw["operation"]["editable_paths"] = ["../outside.py"]
    config = tmp_path / "raceproof.json"
    config.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(TargetConfigurationError, match="safe repository-relative"):
        load_target_config(config)


def test_contract_rejects_shell_operators() -> None:
    root = Path(__file__).resolve().parents[1]
    raw = json.loads((root / "examples/inventory_app/raceproof.json").read_text())
    raw["verification"]["regression"]["argv"] = ["pytest", "&&", "echo"]
    with pytest.raises(ValidationError, match="shell operators"):
        TargetConfig.model_validate(raw)


def test_patch_proposal_rejects_empty_executable_edits() -> None:
    with pytest.raises(ValidationError, match="requires executable edits"):
        PatchProposal(
            diagnosis="race found",
            strategy="lock parent",
            assumptions=[],
            expected_sql_shape=[],
            residual_risks=[],
        )


def test_patch_proposal_allows_explicit_cannot_repair() -> None:
    proposal = PatchProposal(
        diagnosis="insufficient source",
        strategy="stop safely",
        assumptions=[],
        expected_sql_shape=[],
        residual_risks=[],
        cannot_repair=True,
    )
    assert proposal.cannot_repair


def test_verified_status_requires_applied_and_deterministically_verified_patch() -> None:
    with pytest.raises(ValidationError, match="requires an applied executable patch"):
        RunReport(
            run_id="invalid-verified",
            target_id="demo",
            target_name="Demo",
            target_operation="demo:operation",
            invariant_name="demo_invariant",
            invariant_description="One thing remains true.",
            status=FinalStatus.VERIFIED_CANDIDATE,
            final_diff="diff --git a/src/app.py b/src/app.py\n",
        )
