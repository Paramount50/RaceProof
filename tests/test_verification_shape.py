from pathlib import Path

from raceproof.contracts import (
    ActorSpec,
    InvariantResult,
    ScheduleAction,
    ScheduleSpec,
    ScheduleStatus,
)
from raceproof.events import EventRecorder
from raceproof.scheduler import ControlledScheduler
from raceproof.target import TargetAdapter, TargetRegistry


class MissingCheckpointAdapter(TargetAdapter):
    def reset(self, engine) -> None:
        pass

    def run_actor(self, engine, actor, checkpoint, recorder) -> str:
        checkpoint(actor.id, "different_checkpoint")
        return "accepted"

    def evaluate_invariant(self, engine) -> InvariantResult:
        return InvariantResult(
            name="test",
            passed=True,
            assertion="true",
            evidence={},
            explanation="test",
        )

    def schema_ddl(self) -> str:
        return ""


def test_missing_checkpoint_is_invalid_not_success() -> None:
    root = Path(__file__).resolve().parents[1]
    target = TargetRegistry(root / "examples").get("inventory_oversell")
    adapter = MissingCheckpointAdapter(target.repository_root, target.config)
    schedule = ScheduleSpec(
        name="missing",
        description="requires a checkpoint the operation never emits",
        actions=[
            ScheduleAction(kind="wait_checkpoint", actors=["A", "B"], checkpoint="after_read")
        ],
    )
    result = ControlledScheduler(None, EventRecorder(), timeout_seconds=0.05).execute(
        adapter,
        [ActorSpec(id="A", input={}), ActorSpec(id="B", input={})],
        schedule,
        exact=True,
    )
    assert result.status == ScheduleStatus.INVALID
    assert not result.completed
    assert not result.reproduced_failure
