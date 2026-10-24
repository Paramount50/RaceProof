import time
from pathlib import Path

import pytest
from sqlalchemy import text

from raceproof.contracts import ActorSpec, InvariantResult, ScheduleAction, ScheduleSpec
from raceproof.database import actor_session, create_database_engine
from raceproof.events import EventRecorder
from raceproof.scheduler import ControlledScheduler
from raceproof.target import TargetAdapter, TargetRegistry, load_adapter

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


class LockProbeAdapter(TargetAdapter):
    def reset(self, engine) -> None:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS raceproof_lock_probe"))
            connection.execute(
                text("CREATE TABLE raceproof_lock_probe (id integer PRIMARY KEY, value integer)")
            )
            connection.execute(text("INSERT INTO raceproof_lock_probe VALUES (1, 0)"))

    def run_actor(self, engine, actor, checkpoint, recorder) -> str:
        with actor_session(engine, actor.id) as session:
            checkpoint(actor.id, "before_operation")
            session.execute(text("SELECT id FROM raceproof_lock_probe WHERE id = 1 FOR UPDATE"))
            time.sleep(0.35)
            session.execute(
                text("UPDATE raceproof_lock_probe SET value = value + 1 WHERE id = 1")
            )
        return "accepted"

    def evaluate_invariant(self, engine) -> InvariantResult:
        with engine.connect() as connection:
            value = connection.scalar(text("SELECT value FROM raceproof_lock_probe WHERE id = 1"))
        return InvariantResult(
            name="lock_probe",
            passed=value == 2,
            assertion="both serialized updates commit",
            evidence={"value": value},
            explanation="The row lock serializes both updates.",
        )

    def schema_ddl(self) -> str:
        return "CREATE TABLE raceproof_lock_probe (id integer PRIMARY KEY, value integer);"


def test_lock_observer_records_wait_edges_and_release_evidence(database_url) -> None:
    root = Path(__file__).resolve().parents[1]
    target = TargetRegistry(root / "examples").get("inventory_oversell")
    adapter = LockProbeAdapter(target.repository_root, target.config)
    recorder = EventRecorder()
    engine = create_database_engine(database_url, recorder)
    try:
        result = ControlledScheduler(engine, recorder, timeout_seconds=2).execute(
            adapter,
            [ActorSpec(id="A", input={}), ActorSpec(id="B", input={})],
            ScheduleSpec(
                name="parallel_lock_probe",
                description="Both actors contend for one row lock.",
                actions=[
                    ScheduleAction(
                        kind="wait_checkpoint",
                        actors=["A", "B"],
                        checkpoint="before_operation",
                    ),
                    ScheduleAction(kind="release_all", actors=["A", "B"]),
                    ScheduleAction(kind="wait_completion", actors=["A", "B"]),
                ],
            ),
            exact=False,
        )
    finally:
        engine.dispose()

    assert result.completed
    assert result.lock_observation_status == "available"
    assert result.lock_observation_error is None
    assert result.lock_waits
    assert result.wait_for_edges
    assert any(wait.release_points for wait in result.lock_waits)
    assert all(edge.waiting_actor in {"A", "B"} for edge in result.wait_for_edges)
    assert all(edge.blocking_actor in {"A", "B"} for edge in result.wait_for_edges)
    assert not any(event.actor == "__lock_observer__" for event in result.events)
