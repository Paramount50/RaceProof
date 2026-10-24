from __future__ import annotations

import math
import statistics
import threading
import time
from collections import Counter

from sqlalchemy import Engine

from raceproof.contracts import (
    ActorSpec,
    EventKind,
    PerformanceResult,
    ScheduleResult,
    ScheduleSpec,
    ScheduleStatus,
)
from raceproof.events import EventRecorder
from raceproof.target import TargetAdapter


class ControlledScheduler:
    """Execute a declared checkpoint schedule against an application adapter."""

    def __init__(self, engine: Engine, recorder: EventRecorder, timeout_seconds: float = 8) -> None:
        self.engine = engine
        self.recorder = recorder
        self.timeout_seconds = timeout_seconds

    def execute(
        self,
        adapter: TargetAdapter,
        actors: list[ActorSpec],
        schedule: ScheduleSpec,
        *,
        exact: bool,
    ) -> ScheduleResult:
        started = time.perf_counter()
        adapter.reset(self.engine)
        blocking_checkpoints = {
            (actor, action.checkpoint)
            for action in schedule.actions
            if action.kind == "wait_checkpoint" and action.checkpoint
            for actor in action.actors
        }
        reached = {key: threading.Event() for key in blocking_checkpoints}
        release = {key: threading.Event() for key in blocking_checkpoints}
        done = {actor.id: threading.Event() for actor in actors}
        outcomes: dict[str, str] = {}
        errors: list[str] = []
        state_lock = threading.Lock()
        invalid = False
        timed_out = False

        def checkpoint(actor_id: str, name: str) -> None:
            self.recorder.add(EventKind.CHECKPOINT, "execute", name, actor_id)
            key = (actor_id, name)
            if key in reached:
                reached[key].set()
                if not release[key].wait(self.timeout_seconds):
                    raise TimeoutError(f"{actor_id} timed out waiting for release at {name}")

        def worker(actor: ActorSpec) -> None:
            try:
                self.recorder.add(EventKind.TRANSACTION, "execute", "BEGIN", actor.id)
                outcome = adapter.run_actor(self.engine, actor, checkpoint, self.recorder)
                with state_lock:
                    outcomes[actor.id] = outcome
                self.recorder.add(EventKind.TRANSACTION, "execute", "COMMIT", actor.id)
            except Exception as exc:  # noqa: BLE001 - target failures become evidence
                with state_lock:
                    outcomes[actor.id] = "error"
                    errors.append(f"{actor.id}: {type(exc).__name__}: {exc}")
                self.recorder.add(
                    EventKind.ERROR,
                    "execute",
                    "ROLLBACK",
                    actor.id,
                    {"error": f"{type(exc).__name__}: {exc}"},
                )
            finally:
                done[actor.id].set()

        threads = {
            actor.id: threading.Thread(
                target=worker,
                args=(actor,),
                daemon=True,
                name=f"raceproof-actor-{actor.id}",
            )
            for actor in actors
        }
        for thread in threads.values():
            thread.start()

        def wait_event(event: threading.Event, actor_id: str, purpose: str) -> bool:
            deadline = time.monotonic() + self.timeout_seconds
            while time.monotonic() < deadline:
                if event.wait(0.02):
                    return True
                if purpose.startswith("checkpoint") and done[actor_id].is_set():
                    return False
            return False

        for action in schedule.actions:
            if invalid or timed_out:
                break
            if action.kind == "wait_checkpoint":
                for actor_id in action.actors:
                    key = (actor_id, action.checkpoint)
                    event = reached.get(key)
                    if event is None or not wait_event(
                        event, actor_id, f"checkpoint {action.checkpoint}"
                    ):
                        invalid = True
                        errors.append(
                            f"required checkpoint {action.checkpoint} was not reached by {actor_id}"
                        )
                        break
            elif action.kind == "release":
                for actor_id in action.actors:
                    key = (actor_id, action.checkpoint)
                    event = release.get(key)
                    if event is None:
                        invalid = True
                        errors.append(
                            f"schedule attempted to release undeclared checkpoint "
                            f"{action.checkpoint} for {actor_id}"
                        )
                        break
                    event.set()
            elif action.kind == "wait_completion":
                for actor_id in action.actors:
                    if not wait_event(done[actor_id], actor_id, "completion"):
                        timed_out = True
                        errors.append(f"actor {actor_id} did not complete before timeout")
                        break
            elif action.kind == "release_all":
                for event in release.values():
                    event.set()

        for event in release.values():
            event.set()
        for thread in threads.values():
            thread.join(self.timeout_seconds)
        workers_terminated = not any(thread.is_alive() for thread in threads.values())
        if not workers_terminated:
            timed_out = True
            errors.append("one or more actor threads remained alive after timeout")

        invariant = adapter.evaluate_invariant(self.engine) if workers_terminated else None
        if invariant is not None:
            self.recorder.add(
                EventKind.INVARIANT,
                "evaluate",
                "PASS" if invariant.passed else "FAIL",
                detail=invariant.model_dump(),
            )
        completed = (
            not invalid
            and not timed_out
            and len(outcomes) == len(actors)
            and "error" not in outcomes.values()
        )
        if invalid:
            status = ScheduleStatus.INVALID
        elif timed_out:
            status = ScheduleStatus.TIMED_OUT
        elif "error" in outcomes.values():
            status = ScheduleStatus.EXECUTION_ERROR
        else:
            status = ScheduleStatus.COMPLETED
        return ScheduleResult(
            schedule_name=schedule.name,
            status=status,
            completed=completed,
            exact=exact,
            reproduced_failure=completed and invariant is not None and not invariant.passed,
            actor_outcomes=outcomes,
            events=self.recorder.snapshot(),
            invariant=invariant,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            error="; ".join(errors) or None,
            workers_terminated=workers_terminated,
        )


def outcomes_match(actual: dict[str, str], expected: dict[str, int]) -> bool:
    return Counter(actual.values()) == Counter(expected)


def benchmark_schedule(
    engine: Engine,
    adapter: TargetAdapter,
    actors: list[ActorSpec],
    schedule: ScheduleSpec,
    runs: int = 20,
    timeout_seconds: float = 8,
) -> PerformanceResult:
    latencies: list[float] = []
    outcome_counts: Counter[str] = Counter()
    errors = deadlocks = schedule_failures = 0
    wall_start = time.perf_counter()
    for _ in range(runs):
        recorder = EventRecorder()
        try:
            result = ControlledScheduler(engine, recorder, timeout_seconds).execute(
                adapter, actors, schedule, exact=False
            )
        finally:
            engine.dispose()
        latencies.append(result.elapsed_ms)
        outcome_counts.update(result.actor_outcomes.values())
        errors += sum(value == "error" for value in result.actor_outcomes.values())
        schedule_failures += int(not result.completed)
        if not result.workers_terminated:
            break
        if result.error and "40P01" in result.error:
            deadlocks += 1
    total_seconds = max(time.perf_counter() - wall_start, 0.000001)
    sorted_latencies = sorted(latencies)
    p95_index = max(0, math.ceil(0.95 * len(sorted_latencies)) - 1)
    return PerformanceResult(
        runs=runs,
        outcomes=dict(outcome_counts),
        errors=errors,
        schedule_failures=schedule_failures,
        mean_latency_ms=statistics.fmean(latencies),
        p95_latency_ms=sorted_latencies[p95_index],
        throughput_per_second=(runs * len(actors)) / total_seconds,
        deadlocks=deadlocks,
    )
