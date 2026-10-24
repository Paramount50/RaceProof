from pathlib import Path

import pytest

from raceproof.events import EventRecorder
from raceproof.scheduler import ControlledScheduler
from raceproof.target import TargetRegistry, load_adapter

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target_id", ["inventory_oversell", "booking_double_reservation"])
def test_same_scheduler_reproduces_both_target_races(postgres_engine, target_id: str) -> None:
    root = Path(__file__).resolve().parents[1]
    target = TargetRegistry(root / "examples").get(target_id)
    result = ControlledScheduler(postgres_engine, EventRecorder(), timeout_seconds=3).execute(
        load_adapter(target),
        target.config.actors,
        target.config.reproduction_schedule,
        exact=True,
    )
    assert result.completed
    assert result.exact
    assert result.reproduced_failure
    assert result.invariant is not None and not result.invariant.passed
    assert list(result.actor_outcomes.values()).count("accepted") == 2


def test_target_reset_isolates_each_reproduction(postgres_engine) -> None:
    root = Path(__file__).resolve().parents[1]
    target = TargetRegistry(root / "examples").get("inventory_oversell")
    adapter = load_adapter(target)
    scheduler = ControlledScheduler(postgres_engine, EventRecorder(), timeout_seconds=3)
    first = scheduler.execute(
        adapter, target.config.actors, target.config.reproduction_schedule, exact=True
    )
    second = scheduler.execute(
        adapter, target.config.actors, target.config.reproduction_schedule, exact=True
    )
    assert first.invariant and second.invariant
    assert first.invariant.evidence == second.invariant.evidence
