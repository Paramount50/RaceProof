from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from raceproof.config import Settings
from raceproof.contracts import (
    FailureBundle,
    FileReplacement,
    FinalStatus,
    InvestigationConclusion,
    InvestigationContext,
    InvestigationDecision,
    PatchProposal,
)
from raceproof.target import TargetRegistry
from raceproof.workflow import RaceProofRunner

pytestmark = pytest.mark.integration


class UnavailableAgent:
    tool_names = frozenset()

    def available(self):
        return False, "test agent intentionally unavailable"

    def investigate(self, context):  # pragma: no cover
        raise AssertionError("must not be called")

    def propose(self, bundle):  # pragma: no cover
        raise AssertionError("must not be called")


class InvestigationTestDouble:
    tool_names = frozenset({"read_approved_source", "select_controlled_experiment"})

    def __init__(self, choices: list[str] | None = None) -> None:
        self.choices = choices or ["stale_read_interleaving"]
        self.contexts: list[InvestigationContext] = []

    def available(self):
        return True, "structured investigation test double"

    def investigate(self, context: InvestigationContext) -> InvestigationDecision:
        self.contexts.append(context)
        choice = self.choices[min(len(self.contexts) - 1, len(self.choices) - 1)]
        return InvestigationDecision(
            suspected_source=next(iter(context.source_files)),
            concurrency_pattern="read check write",
            reasoning_summary="Both actors can act on the same observed state.",
            required_interleaving=["A reads", "B reads", "A commits", "B commits"],
            expected_invariant_violation=context.invariant.description,
            experiment_name=choice,
            confidence=0.9,
            supporting_evidence=[context.schema_ddl[:80], context.findings[0].explanation],
        )

    def interpret(self, context: InvestigationContext) -> InvestigationConclusion:
        latest = context.prior_attempts[-1]
        assert latest.result is not None and latest.result.reproduced_failure
        return InvestigationConclusion(
            root_cause=latest.decision.reasoning_summary,
            execution_interpretation="The actual PostgreSQL trace confirmed both conflicting commits.",
            evidence_references=[f"schedule:{latest.result.schedule_name}"],
            repair_ready=True,
        )


class GeneratedInventoryRepair:
    tool_names = frozenset({"read_investigation_handoff", "read_editable_source"})

    def __init__(self) -> None:
        self.bundles: list[FailureBundle] = []

    def available(self):
        return True, "structured repair test double"

    def propose(self, bundle: FailureBundle) -> PatchProposal:
        self.bundles.append(bundle)
        assert bundle.investigation_artifact.confirmed_vulnerability
        path = "src/inventory_app/operations.py"
        before = bundle.source_files[path]
        after = before.replace(
            "from sqlalchemy import select", "from sqlalchemy import select, update"
        )
        start = "    product = session.scalar(select(Product).where(Product.id == product_id))\n"
        end = "    return True\n"
        left = after.index(start)
        right = after.index(end, left) + len(end)
        replacement = """    checkpoint(actor_id, "before_claim")
    claim = session.execute(
        update(Product)
        .where(Product.id == product_id, Product.stock > 0)
        .values(stock=Product.stock - 1)
    )
    if claim.rowcount != 1:
        return False
    session.add(Order(product_id=product_id, actor_id=actor_id))
    session.flush()
    checkpoint(actor_id, "after_claim")
    return True
"""
        after = after[:left] + replacement + after[right:]
        body = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
        return PatchProposal(
            diagnosis="Two transactions use the same stale stock value.",
            strategy="Atomic conditional update",
            assumptions=["The product row exists."],
            unified_diff=f"diff --git a/{path} b/{path}\n{body}",
            expected_sql_shape=["UPDATE inventory_products", "stock > 0"],
            schedule_anchor_hint="before_claim",
            residual_risks=["Only the configured operation is changed."],
        )


class RejectingRepair:
    tool_names = frozenset({"read_investigation_handoff"})

    def __init__(self) -> None:
        self.calls = 0

    def available(self):
        return True, "rejecting repair double"

    def propose(self, bundle: FailureBundle) -> PatchProposal:
        self.calls += 1
        return PatchProposal(
            diagnosis="race",
            strategy="invalid test edit",
            assumptions=[],
            unified_diff=(
                "diff --git a/tests/test_domain.py b/tests/test_domain.py\n"
                "--- a/tests/test_domain.py\n+++ b/tests/test_domain.py\n"
                "@@ -1 +1 @@\n-assert True\n+assert False\n"
            ),
            expected_sql_shape=[],
            residual_risks=[],
        )


class StructuredBookingRepair:
    tool_names = frozenset({"load_repair_workspace"})

    def available(self):
        return True, "structured booking repair double"

    def propose(self, bundle: FailureBundle) -> PatchProposal:
        path = "src/booking_app/operations.py"
        before = bundle.source_files[path]
        after = before.replace(
            '    checkpoint(actor_id, "after_read")\n'
            '    session.add(Booking(resource_id=resource_id, actor_id=actor_id))\n',
            '    checkpoint(actor_id, "after_read")\n'
            '    session.scalar(\n'
            '        select(Resource).where(Resource.id == resource_id).with_for_update()\n'
            '    )\n'
            '    existing = (\n'
            '        session.scalar(\n'
            '            select(func.count()).select_from(Booking).where(\n'
            '                Booking.resource_id == resource_id\n'
            '            )\n'
            '        )\n'
            '        or 0\n'
            '    )\n'
            '    if existing:\n'
            '        return False\n'
            '    session.add(Booking(resource_id=resource_id, actor_id=actor_id))\n',
        )
        return PatchProposal(
            diagnosis="The empty booking result set cannot be locked.",
            strategy="Lock the parent resource row and re-check under that lock.",
            assumptions=["Every booking references one resource row."],
            file_replacements=[FileReplacement(path=path, content=after)],
            expected_sql_shape=["FOR UPDATE", "SELECT count"],
            residual_risks=[],
        )


class RetryAfterVerificationRepair(GeneratedInventoryRepair):
    def propose(self, bundle: FailureBundle) -> PatchProposal:
        if not self.bundles:
            self.bundles.append(bundle)
            path = "src/inventory_app/operations.py"
            before = bundle.source_files[path]
            after = before.replace(
                '"""Intentionally vulnerable read-check-write operation."""',
                '"""Purchase operation pending concurrency hardening."""',
            )
            body = "".join(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
            )
            return PatchProposal(
                diagnosis="The read-check-write race remains.",
                strategy="First insufficient candidate",
                assumptions=[],
                unified_diff=f"diff --git a/{path} b/{path}\n{body}",
                expected_sql_shape=["SELECT", "UPDATE"],
                residual_risks=["This first candidate intentionally leaves the race for testing."],
            )
        return super().propose(bundle)


class RequestThenRepair(GeneratedInventoryRepair):
    def propose(self, bundle: FailureBundle) -> PatchProposal:
        if not self.bundles:
            self.bundles.append(bundle)
            return PatchProposal(
                diagnosis="More schedule evidence is needed.",
                strategy="Request another controlled replay",
                assumptions=[],
                unified_diff="",
                expected_sql_shape=[],
                residual_risks=[],
                cannot_repair=True,
                request_additional_investigation=True,
                additional_evidence_request=["Repeat the stale-read interleaving."],
            )
        return super().propose(bundle)


def settings_for(tmp_path: Path, database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        artifact_dir=tmp_path / "artifacts",
        max_investigation_attempts=2,
        max_repair_attempts=2,
        benchmark_runs=3,
        schedule_timeout_seconds=2,
    )


def test_missing_investigation_model_stops_explicitly(tmp_path, database_url) -> None:
    root = Path(__file__).resolve().parents[1]
    target = TargetRegistry(root / "examples").get("inventory_oversell")
    report = RaceProofRunner(
        root,
        target,
        settings_for(tmp_path, database_url),
        UnavailableAgent(),
        UnavailableAgent(),
    ).run()
    assert report.status == FinalStatus.MODEL_UNAVAILABLE
    assert report.baseline is None
    assert "Investigation Agent unavailable" in report.limitations[-1]


@pytest.mark.parametrize(
    ("target_id", "experiment"),
    [
        ("inventory_oversell", "stale_read_interleaving"),
        ("booking_double_reservation", "double_reservation_interleaving"),
    ],
)
def test_same_two_agent_runner_reproduces_external_targets(
    tmp_path, database_url, target_id: str, experiment: str
) -> None:
    root = Path(__file__).resolve().parents[1]
    target = TargetRegistry(root / "examples").get(target_id)
    investigator = InvestigationTestDouble([experiment])
    report = RaceProofRunner(
        root,
        target,
        settings_for(tmp_path, database_url),
        investigator,
        UnavailableAgent(),
    ).run()
    assert report.status == FinalStatus.MODEL_UNAVAILABLE
    assert report.baseline is not None and report.baseline.reproduced_failure
    assert report.investigation_artifact is not None
    assert report.investigation_artifact.confirmed_vulnerability
    assert investigator.contexts[0].source_files
    assert investigator.contexts[0].schema_ddl


def test_failed_experiment_triggers_bounded_investigation_replan(tmp_path, database_url) -> None:
    root = Path(__file__).resolve().parents[1]
    target = TargetRegistry(root / "examples").get("inventory_oversell")
    investigator = InvestigationTestDouble(["serial_control", "stale_read_interleaving"])
    report = RaceProofRunner(
        root,
        target,
        settings_for(tmp_path, database_url),
        investigator,
        UnavailableAgent(),
    ).run()
    assert len(report.investigation_attempts) == 2
    assert report.investigation_attempts[0].result is not None
    assert not report.investigation_attempts[0].result.reproduced_failure
    assert report.investigation_attempts[1].result.reproduced_failure


def test_generated_candidate_receives_handoff_and_is_verified(tmp_path, database_url) -> None:
    root = Path(__file__).resolve().parents[1]
    target = TargetRegistry(root / "examples").get("inventory_oversell")
    before = (target.repository_root / "src/inventory_app/operations.py").read_text()
    repair = GeneratedInventoryRepair()
    report = RaceProofRunner(
        root,
        target,
        settings_for(tmp_path, database_url),
        InvestigationTestDouble(),
        repair,
    ).run()
    assert report.status == FinalStatus.VERIFIED_CANDIDATE
    assert report.final_diff and "Product.stock > 0" in report.final_diff
    assert report.attempts[0].verification is not None
    assert report.attempts[0].verification.passed
    assert report.attempts[0].verification.gates["candidate_source_executed"]
    assert repair.bundles[0].investigation_artifact.sql_trace
    assert any(event.actor == "Investigation Agent" for event in report.events)
    assert any(event.actor == "Repair Agent" for event in report.events)
    assert (target.repository_root / "src/inventory_app/operations.py").read_text() == before


def test_structured_booking_repair_locks_parent_and_passes_postgresql_verification(
    tmp_path, database_url
) -> None:
    root = Path(__file__).resolve().parents[1]
    target = TargetRegistry(root / "examples").get("booking_double_reservation")
    report = RaceProofRunner(
        root,
        target,
        settings_for(tmp_path, database_url),
        InvestigationTestDouble(["double_reservation_interleaving"]),
        StructuredBookingRepair(),
    ).run()
    assert report.status == FinalStatus.VERIFIED_CANDIDATE
    assert report.attempts[0].patch_validation is not None
    assert report.attempts[0].patch_validation.accepted
    assert report.attempts[0].verification is not None
    assert report.attempts[0].verification.passed
    assert report.attempts[0].verification.gates["candidate_source_executed"]
    assert report.attempts[0].verification.gates["candidate_operation_invoked"]
    assert report.attempts[0].verification.gates["replay_isolation"]
    execution = report.attempts[0].verification.execution_evidence
    assert execution["operation_invocations"] >= 2
    assert execution["locking_sql_observed"] is True
    assert execution["lock_before_availability_check"] is True
    assert execution["lock_precedes_transaction_commit"] is True
    assert execution["replay_workers_terminated"] is True
    assert execution["replay_connections_released"] is True
    assert "with_for_update" in (report.final_diff or "")
    assert report.attempts[0].proposal is not None
    assert report.attempts[0].proposal.file_replacements
    assert report.attempts[0].generated_diff
    artifact_root = tmp_path / "artifacts"
    candidate_source = (
        artifact_root
        / f"workspaces/{report.run_id}/attempt-1/candidate/src/booking_app/operations.py"
    )
    run_source = (
        artifact_root
        / f"workspaces/{report.run_id}/attempt-1/run/src/booking_app/operations.py"
    )
    applied_source = candidate_source.read_text(encoding="utf-8")
    assert "with_for_update" in applied_source
    assert applied_source == report.attempts[0].proposal.file_replacements[0].content
    assert applied_source == run_source.read_text(encoding="utf-8")
    for artifact in (
        "model-proposal.json",
        "generated.diff",
        "patch-validation.json",
        "applied.diff",
        "verification.json",
    ):
        assert (artifact_root / f"runs/{report.run_id}/attempts/attempt-1/{artifact}").is_file()


def test_failed_verification_is_returned_to_repair_agent_for_revision(
    tmp_path, database_url
) -> None:
    root = Path(__file__).resolve().parents[1]
    target = TargetRegistry(root / "examples").get("inventory_oversell")
    repair = RetryAfterVerificationRepair()
    report = RaceProofRunner(
        root,
        target,
        settings_for(tmp_path, database_url),
        InvestigationTestDouble(),
        repair,
    ).run()
    assert report.status == FinalStatus.VERIFIED_CANDIDATE
    assert len(report.attempts) == 2
    assert report.attempts[0].verification is not None
    assert not report.attempts[0].verification.passed
    assert report.attempts[1].verification is not None
    assert report.attempts[1].verification.passed
    assert repair.bundles[1].previous_attempts[0]["verification"]["passed"] is False


def test_repair_agent_can_request_one_bounded_additional_investigation(
    tmp_path, database_url
) -> None:
    root = Path(__file__).resolve().parents[1]
    target = TargetRegistry(root / "examples").get("inventory_oversell")
    investigator = InvestigationTestDouble()
    repair = RequestThenRepair()
    report = RaceProofRunner(
        root,
        target,
        settings_for(tmp_path, database_url),
        investigator,
        repair,
    ).run()
    assert report.status == FinalStatus.VERIFIED_CANDIDATE
    assert len(report.investigation_attempts) == 2
    assert investigator.contexts[1].repair_evidence_requests == [
        "Repeat the stale-read interleaving."
    ]


def test_repair_agent_cannot_override_patch_guard_and_retries_are_bounded(
    tmp_path, database_url
) -> None:
    root = Path(__file__).resolve().parents[1]
    target = TargetRegistry(root / "examples").get("inventory_oversell")
    repair = RejectingRepair()
    report = RaceProofRunner(
        root,
        target,
        settings_for(tmp_path, database_url),
        InvestigationTestDouble(),
        repair,
    ).run()
    assert report.status == FinalStatus.UNRESOLVED
    assert repair.calls == 2
    assert report.attempts[0].patch_validation is not None
    assert not report.attempts[0].patch_validation.accepted
    assert report.attempts[1].patch_validation is None
    assert report.attempts[1].failure_reason == "Repair Agent repeated an identical candidate diff"
    assert report.final_diff is None
    stage_states = [
        (event.stage, event.detail.get("status"))
        for event in report.events
        if event.kind == "stage" and event.action == "stage_status"
    ]
    assert ("apply_patch", "FAILED") in stage_states
    assert ("repair", "RETRYING") in stage_states
    assert ("verify", "SKIPPED") in stage_states
    assert ("finalize", "SUCCEEDED") in stage_states
