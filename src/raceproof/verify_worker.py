from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import os
import subprocess
import sys
import threading
from pathlib import Path

from raceproof.contracts import ScheduleStatus, VerificationReport
from raceproof.database import create_database_engine
from raceproof.events import EventRecorder
from raceproof.scheduler import ControlledScheduler, benchmark_schedule, outcomes_match
from raceproof.target import load_adapter, load_target_config


def _resolve_operation(callable_path: str):
    module_name, separator, attribute_path = callable_path.partition(":")
    if not separator:
        raise ValueError(f"operation callable must use module:attribute syntax: {callable_path}")
    module = importlib.import_module(module_name)
    operation = module
    for part in attribute_path.split("."):
        operation = getattr(operation, part)
    return module, operation


def _execute_schedule(
    database_url: str,
    adapter,
    actors,
    schedule,
    operation,
    *,
    exact: bool,
):
    recorder = EventRecorder()
    engine = create_database_engine(database_url, recorder)
    invocation_count = 0
    invocation_lock = threading.Lock()

    def profile(frame, event, _arg):
        nonlocal invocation_count
        if event == "call" and frame.f_code is operation.__code__:
            with invocation_lock:
                invocation_count += 1
        return profile

    threading.setprofile(profile)
    connections_released = False
    try:
        result = ControlledScheduler(engine, recorder).execute(
            adapter, actors, schedule, exact=exact
        )
    finally:
        threading.setprofile(None)
        checked_out = getattr(engine.pool, "checkedout", lambda: 0)()
        connections_released = checked_out == 0
        engine.dispose()
    return result, invocation_count, connections_released


def _run_regression(root: Path, target) -> subprocess.CompletedProcess[str]:
    command = target.config.verification.regression
    argv = list(command.argv)
    if argv[0] in {"python", "python3"}:
        argv[0] = sys.executable
    python_paths = [str(root / path) for path in target.config.python_paths]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([*python_paths, os.environ.get("PYTHONPATH", "")]),
    }
    return subprocess.run(
        argv,
        cwd=root / command.cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=command.timeout_seconds,
        shell=False,
        check=False,
    )


def verify(
    database_url: str,
    root: Path,
    config_relative_path: str,
    baseline_p95_ms: float | None = None,
) -> VerificationReport:
    target = load_target_config(root / config_relative_path, allowed_roots=[root])
    adapter = load_adapter(target)
    config = target.config
    operation_module, operation = _resolve_operation(config.operation.callable)
    operation_module_file = Path(operation_module.__file__ or "").resolve()
    candidate_source_executed = operation_module_file.is_relative_to(root.resolve())
    operation_source = inspect.getsource(operation)
    operation_source_sha256 = hashlib.sha256(operation_source.encode()).hexdigest()
    selected_results = []
    all_results = []
    lock_evidence_results = []
    attempted_modes: list[str] = []
    operation_invocations = 0
    replay_workers_terminated = True
    replay_isolation = True
    replay_connections_released = True
    for _ in range(config.verification.replay_count):
        selected = None
        for index, schedule in enumerate(config.verification_schedules):
            result, invocations, connections_released = _execute_schedule(
                database_url,
                adapter,
                config.actors,
                schedule,
                operation,
                exact=index == 0,
            )
            operation_invocations += invocations
            all_results.append(result)
            attempted_modes.append(f"{schedule.name}:{result.status.value}")
            replay_workers_terminated = (
                replay_workers_terminated and result.workers_terminated
            )
            replay_connections_released = (
                replay_connections_released and connections_released
            )
            replay_isolation = replay_workers_terminated and replay_connections_released
            if not replay_isolation:
                break
            if result.status == ScheduleStatus.COMPLETED:
                selected = result
                break
        if selected is not None:
            selected_results.append(selected)
        if not replay_isolation:
            break

    schedule_valid = len(selected_results) == config.verification.replay_count
    invariants_pass = schedule_valid and all(
        result.invariant is not None and result.invariant.passed for result in selected_results
    )
    outcomes_valid = schedule_valid and all(
        outcomes_match(result.actor_outcomes, config.verification.allowed_outcomes)
        for result in selected_results
    )
    completed = schedule_valid and all(result.completed for result in selected_results)
    sql_statements = [
        str(event.detail.get("statement", ""))
        for result in all_results
        for event in result.events
        if event.kind.value == "sql"
    ]
    locking_sql = [statement for statement in sql_statements if "FOR UPDATE" in statement.upper()]
    lock_before_availability_checks = []
    lock_before_transaction_end = []
    for result in all_results:
        for actor in {event.actor for event in result.events if event.actor}:
            actor_events = [event for event in result.events if event.actor == actor]
            lock_indexes = [
                index
                for index, event in enumerate(actor_events)
                if event.kind.value == "sql"
                and "FOR UPDATE" in str(event.detail.get("statement", "")).upper()
            ]
            availability_indexes = [
                index
                for index, event in enumerate(actor_events)
                if event.kind.value == "sql"
                and "FROM BOOKINGS" in str(event.detail.get("statement", "")).upper()
                and "SELECT" in str(event.detail.get("statement", "")).upper()
            ]
            commit_indexes = [
                index
                for index, event in enumerate(actor_events)
                if event.kind.value == "transaction" and event.action == "COMMIT"
            ]
            if lock_indexes and availability_indexes:
                lock_before_availability_checks.append(
                    any(index > min(lock_indexes) for index in availability_indexes)
                )
            if lock_indexes and commit_indexes:
                lock_before_transaction_end.append(min(lock_indexes) < max(commit_indexes))

    if replay_isolation:
        regression = _run_regression(root, target)
        engine = create_database_engine(database_url)
        try:
            performance = benchmark_schedule(
                engine,
                adapter,
                config.actors,
                config.verification_schedules[-1],
                config.verification.benchmark_runs,
            )
        finally:
            engine.dispose()
    else:
        regression = None
        performance = None

    if replay_isolation:
        diagnostic_schedule = config.verification_schedules[-1]
        diagnostic_result, _, _ = _execute_schedule(
            database_url,
            adapter,
            config.actors,
            diagnostic_schedule,
            operation,
            exact=False,
        )
        lock_evidence_results.append(diagnostic_result)

    performance_acceptable = (
        performance is not None
        and performance.runs > 0
        and (
            baseline_p95_ms is None
            or performance.p95_latency_ms
            <= baseline_p95_ms * config.verification.performance_regression_limit
        )
    )
    gates = {
        "candidate_source_executed": candidate_source_executed,
        "candidate_operation_invoked": operation_invocations > 0,
        "replay_isolation": replay_isolation,
        "schedule_semantically_valid": schedule_valid,
        "all_replays_completed": completed,
        "business_invariant": invariants_pass,
        "allowed_outcomes": outcomes_valid,
        "regression_tests": regression is not None and regression.returncode == 0,
        "no_transaction_errors": performance is not None and performance.errors == 0,
        "benchmark_schedules_completed": (
            performance is not None and performance.schedule_failures == 0
        ),
        "no_deadlocks": performance is not None and performance.deadlocks == 0,
        "performance_within_configured_limit": performance_acceptable,
    }
    replay_modes = sorted({"exact" if result.exact else "adapted" for result in selected_results})
    return VerificationReport(
        passed=all(gates.values()),
        replay_mode="+".join(replay_modes) if replay_modes else "unverifiable",
        gates=gates,
        schedule_results=selected_results,
        lock_evidence_results=lock_evidence_results,
        regression_output=(
            (regression.stdout + regression.stderr)[-8000:]
            if regression is not None
            else "Skipped because replay worker cleanup failed."
        ),
        performance=performance,
        limitations=[
            f"Target: {config.name}.",
            f"Executed operation module: {operation_module_file}.",
            "Finite replays cover only the developer-declared schedules.",
            "A missing original checkpoint triggers a declared adapted schedule, never a pass.",
            "Performance values are local observations, not production capacity claims.",
            "Schedule attempts: " + ", ".join(attempted_modes),
            (
                "Candidate p95 was compared with the same local baseline and configured multiplier."
                if baseline_p95_ms is not None
                else "No comparative baseline was supplied; the performance gate was informational."
            ),
        ],
        execution_evidence={
            "operation_callable": config.operation.callable,
            "operation_module_file": str(operation_module_file),
            "operation_source_sha256": operation_source_sha256,
            "operation_file_sha256": hashlib.sha256(operation_module_file.read_bytes()).hexdigest(),
            "operation_invocations": operation_invocations,
            "locking_sql_observed": bool(locking_sql),
            "locking_sql": locking_sql[:20],
            "lock_before_availability_check": (
                all(lock_before_availability_checks)
                if lock_before_availability_checks
                else None
            ),
            "lock_precedes_transaction_commit": (
                all(lock_before_transaction_end) if lock_before_transaction_end else None
            ),
            "replay_workers_terminated": replay_workers_terminated,
            "replay_connections_released": replay_connections_released,
            "replay_engines_disposed": True,
            "performance_comparison": (
                {
                    "baseline_p95_ms": baseline_p95_ms,
                    "candidate_p95_ms": performance.p95_latency_ms if performance else None,
                    "regression_ratio": (
                        performance.p95_latency_ms / baseline_p95_ms
                        if performance is not None and baseline_p95_ms
                        else None
                    ),
                    "configured_limit": config.verification.performance_regression_limit,
                    "candidate_samples": performance.latency_samples_ms if performance else [],
                    "candidate_median_ms": performance.median_latency_ms if performance else None,
                }
                if performance is not None
                else None
            ),
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--config", default="raceproof.json")
    parser.add_argument("--baseline-p95-ms", type=float)
    args = parser.parse_args()
    report = verify(
        args.database_url,
        args.root.resolve(),
        args.config,
        args.baseline_p95_ms,
    )
    print(report.model_dump_json())
    return 0 if report.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
