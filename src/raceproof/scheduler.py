from __future__ import annotations

import math
import statistics
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from raceproof.contracts import (
    ActorSpec,
    EventKind,
    LockWaitEvidence,
    PerformanceResult,
    ScheduleResult,
    ScheduleSpec,
    ScheduleStatus,
    WaitForEdge,
)
from raceproof.database import set_sql_actor
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
        capture_lock_evidence: bool = True,
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
        observer_stop = threading.Event()
        observer_available = False
        observer_error: str | None = None
        lock_samples: dict[tuple[int, tuple[int, ...], str, str, str], dict[str, Any]] = {}
        observed_edges: dict[tuple[int, int, str, str, str], None] = {}
        deadlock_cycle = False

        def actor_pids() -> dict[int, str]:
            mapping: dict[int, str] = {}
            for event in self.recorder.snapshot():
                pid = event.detail.get("backend_pid") if event.kind == EventKind.SQL else None
                if event.actor and isinstance(pid, int):
                    mapping[pid] = event.actor
            return mapping

        def contains_cycle(edges: set[tuple[int, int]]) -> bool:
            graph: dict[int, set[int]] = {}
            for source, destination in edges:
                graph.setdefault(source, set()).add(destination)

            def visit(node: int, path: set[int]) -> bool:
                if node in path:
                    return True
                return any(visit(item, {*path, node}) for item in graph.get(node, set()))

            return any(visit(node, set()) for node in graph)

        def observe_locks() -> None:
            nonlocal deadlock_cycle, observer_available, observer_error
            if self.engine is None:
                observer_error = "database engine is unavailable"
                return
            set_sql_actor("__lock_observer__")
            try:
                with self.engine.connect() as connection:
                    observer_available = True
                    while not observer_stop.is_set():
                        pids = actor_pids()
                        if not pids:
                            observer_stop.wait(0.001)
                            continue
                        try:
                            rows = connection.execute(
                                text(
                                    """
                                    SELECT activity.pid,
                                           waiting_lock.locktype,
                                           waiting_lock.mode,
                                           COALESCE(relation.relname, ''),
                                           pg_blocking_pids(activity.pid)
                                    FROM pg_stat_activity AS activity
                                    JOIN pg_locks AS waiting_lock
                                      ON waiting_lock.pid = activity.pid
                                    LEFT JOIN pg_class AS relation
                                      ON relation.oid = waiting_lock.relation
                                    WHERE activity.pid = ANY(:pids)
                                      AND NOT waiting_lock.granted
                                      AND cardinality(pg_blocking_pids(activity.pid)) > 0
                                    """
                                ),
                                {"pids": list(pids)},
                            ).all()
                        except SQLAlchemyError as exc:
                            observer_available = False
                            observer_error = type(exc).__name__
                            return
                        sampled_edges: set[tuple[int, int]] = set()
                        now = datetime.now(UTC)
                        for waiting_pid, lock_type, lock_mode, relation, blockers in rows:
                            blocker_tuple = tuple(
                                sorted(int(pid) for pid in blockers if int(pid) in pids)
                            )
                            if not blocker_tuple or int(waiting_pid) not in pids:
                                continue
                            key = (
                                int(waiting_pid),
                                blocker_tuple,
                                str(lock_type),
                                str(lock_mode),
                                str(relation),
                            )
                            sample = lock_samples.setdefault(key, {"first": now, "last": now})
                            sample["last"] = now
                            for blocker in blocker_tuple:
                                sampled_edges.add((int(waiting_pid), blocker))
                                observed_edges[
                                    (
                                        int(waiting_pid),
                                        blocker,
                                        str(lock_type),
                                        str(lock_mode),
                                        str(relation),
                                    )
                                ] = None
                        deadlock_cycle = deadlock_cycle or contains_cycle(sampled_edges)
                        observer_stop.wait(0.001)
            except SQLAlchemyError as exc:
                observer_available = False
                observer_error = type(exc).__name__
            finally:
                set_sql_actor(None)

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
        observer = None
        if capture_lock_evidence:
            observer = threading.Thread(
                target=observe_locks,
                daemon=True,
                name="raceproof-lock-observer",
            )
            observer.start()
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
        observer_stop.set()
        if observer is not None:
            observer.join(min(self.timeout_seconds, 1))
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
        pid_actors = actor_pids()
        terminal_events = {
            event.actor: event
            for event in self.recorder.snapshot()
            if event.actor and event.action in {"COMMIT", "ROLLBACK"}
        }
        lock_waits = []
        for (waiting_pid, blockers, lock_type, lock_mode, relation), sample in lock_samples.items():
            blocker_actors = sorted({pid_actors[pid] for pid in blockers if pid in pid_actors})
            release_points = [
                f"{actor}:{terminal_events[actor].action}#{terminal_events[actor].sequence}"
                for actor in blocker_actors
                if actor in terminal_events
            ]
            final_observation = max(
                [
                    terminal_events[actor].timestamp
                    for actor in blocker_actors
                    if actor in terminal_events
                ],
                default=sample["last"],
            )
            lock_waits.append(
                LockWaitEvidence(
                    waiting_actor=pid_actors.get(waiting_pid, f"pid:{waiting_pid}"),
                    blocking_actors=blocker_actors,
                    lock_type=lock_type,
                    lock_mode=lock_mode,
                    relation=relation or None,
                    duration_ms=max(
                        0.001,
                        (final_observation - sample["first"]).total_seconds() * 1000,
                    ),
                    release_points=release_points,
                )
            )
        wait_for_edges = [
            WaitForEdge(
                waiting_actor=pid_actors.get(waiting, f"pid:{waiting}"),
                blocking_actor=pid_actors.get(blocking, f"pid:{blocking}"),
                lock_type=lock_type,
                lock_mode=lock_mode,
                relation=relation or None,
            )
            for waiting, blocking, lock_type, lock_mode, relation in observed_edges
        ]
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
            lock_observation_status=(
                "available"
                if capture_lock_evidence and observer_available
                else "unavailable"
                if capture_lock_evidence
                else "not_requested"
            ),
            lock_observation_error=observer_error,
            lock_waits=lock_waits,
            wait_for_edges=wait_for_edges,
            deadlock_cycle=deadlock_cycle,
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
    warmup_runs: int = 1,
) -> PerformanceResult:
    latencies: list[float] = []
    outcome_counts: Counter[str] = Counter()
    errors = deadlocks = schedule_failures = 0
    wall_start = time.perf_counter()
    measured_start: float | None = None
    total_runs = warmup_runs + runs
    for index in range(total_runs):
        recorder = EventRecorder()
        result = ControlledScheduler(engine, recorder, timeout_seconds).execute(
            adapter, actors, schedule, exact=False, capture_lock_evidence=False
        )
        if index < warmup_runs:
            schedule_failures += int(not result.completed)
            errors += sum(value == "error" for value in result.actor_outcomes.values())
            if not result.workers_terminated:
                break
            continue
        if measured_start is None:
            measured_start = time.perf_counter()
        latencies.append(result.elapsed_ms)
        outcome_counts.update(result.actor_outcomes.values())
        errors += sum(value == "error" for value in result.actor_outcomes.values())
        schedule_failures += int(not result.completed)
        if not result.workers_terminated:
            break
        if result.error and "40P01" in result.error:
            deadlocks += 1
    total_seconds = max(time.perf_counter() - (measured_start or wall_start), 0.000001)
    if not latencies:
        return PerformanceResult(
            runs=0,
            outcomes=dict(outcome_counts),
            errors=errors,
            schedule_failures=schedule_failures,
            mean_latency_ms=0,
            p95_latency_ms=0,
            throughput_per_second=0,
            deadlocks=deadlocks,
            warmup_runs=warmup_runs,
        )
    sorted_latencies = sorted(latencies)
    p95_index = max(0, math.ceil(0.95 * len(sorted_latencies)) - 1)
    return PerformanceResult(
        runs=len(latencies),
        outcomes=dict(outcome_counts),
        errors=errors,
        schedule_failures=schedule_failures,
        mean_latency_ms=statistics.fmean(latencies),
        p95_latency_ms=sorted_latencies[p95_index],
        throughput_per_second=(len(latencies) * len(actors)) / total_seconds,
        deadlocks=deadlocks,
        median_latency_ms=statistics.median(latencies),
        latency_samples_ms=latencies,
        warmup_runs=warmup_runs,
    )
