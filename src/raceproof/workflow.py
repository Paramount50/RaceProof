from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from raceproof.config import Settings
from raceproof.contracts import (
    EventKind,
    FailureBundle,
    FinalStatus,
    InvestigationArtifact,
    InvestigationAttempt,
    InvestigationContext,
    RepairAttempt,
    RunEvent,
    RunReport,
    ScheduleSpec,
)
from raceproof.database import create_database_engine
from raceproof.events import EventRecorder
from raceproof.inspection import inspect_python_file
from raceproof.model import InvestigationModel, RepairModel
from raceproof.patching import PatchGuard, WorkspaceManager, canonical_diff, replacement_diff
from raceproof.scheduler import ControlledScheduler, benchmark_schedule
from raceproof.security import redact_sensitive
from raceproof.target import LoadedTarget, load_adapter
from raceproof.usage import (
    BudgetExceededError,
    ModelAuthenticationError,
    ModelConfigurationError,
    ModelQuotaExceededError,
    ModelRateLimitError,
    ModelRequestRecord,
    ModelRuntimeError,
    ProvidersExhaustedError,
    StructuredOutputError,
    UsageLimits,
    UsageTracker,
)
from raceproof.verification import verify_candidate


class WorkflowState(TypedDict, total=False):
    run_id: str
    status: str
    findings: list[dict[str, Any]]
    source_files: dict[str, str]
    schema_ddl: str
    investigation_attempts: list[dict[str, Any]]
    selected_experiment: str
    experiment_result: dict[str, Any]
    investigation_artifact: dict[str, Any]
    baseline: dict[str, Any]
    baseline_performance: dict[str, Any]
    attempts: list[dict[str, Any]]
    attempt_number: int
    candidate_path: str
    proposal: dict[str, Any]
    candidate_diff: str
    final_diff: str
    terminal_reason: str


def stage_timings_ms(events: list[RunEvent], finished_at: datetime) -> dict[str, float]:
    starts: dict[str, datetime] = {}
    totals: dict[str, float] = {}
    terminal_states = {"SUCCEEDED", "FAILED", "RETRYING", "SKIPPED"}
    for event in sorted(events, key=lambda item: item.sequence):
        if event.kind != EventKind.STAGE:
            continue
        status = str(event.detail.get("status", ""))
        if status == "RUNNING":
            starts.setdefault(event.stage, event.timestamp)
        elif status in terminal_states and event.stage in starts:
            elapsed = max(0.0, (event.timestamp - starts.pop(event.stage)).total_seconds() * 1000)
            totals[event.stage] = totals.get(event.stage, 0.0) + elapsed
    for stage, started_at in starts.items():
        elapsed = max(0.0, (finished_at - started_at).total_seconds() * 1000)
        totals[stage] = totals.get(stage, 0.0) + elapsed
    return {stage: round(duration, 2) for stage, duration in totals.items()}


class RaceProofRunner:
    """LangGraph orchestration for two LLM agents and deterministic execution tools."""

    def __init__(
        self,
        engine_root: Path,
        target: LoadedTarget,
        settings: Settings,
        investigation_model: InvestigationModel,
        repair_model: RepairModel,
        listener: Any | None = None,
        usage_tracker: UsageTracker | None = None,
    ) -> None:
        self.engine_root = engine_root.resolve()
        self.target = target
        self.settings = settings
        self.database_url = os.environ.get(target.config.database_url_env, settings.database_url)
        self.investigation_model = investigation_model
        self.repair_model = repair_model
        self.usage_tracker = usage_tracker or UsageTracker(UsageLimits())
        self.recorder = EventRecorder(listener)
        self.usage_tracker.set_event_listener(self._model_request_event)
        artifact_root = (self.engine_root / settings.artifact_dir).resolve()
        self.workspace = WorkspaceManager(target.repository_root, artifact_root)
        protected = tuple(
            sorted(
                {
                    Path(path).as_posix().rstrip("/")
                    for path in target.config.operation.protected_paths
                }
            )
        )
        self.guard = PatchGuard(set(target.config.operation.editable_paths), protected)
        self._history_lock = threading.Lock()
        self._latest_state: WorkflowState = {}
        self.graph = self._build_graph()

    def _attempt_artifact_dir(self, run_id: str, attempt: int) -> Path:
        path = (
            self.engine_root
            / self.settings.artifact_dir
            / "runs"
            / run_id
            / "attempts"
            / f"attempt-{attempt}"
        ).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_attempt_artifact(
        self, run_id: str, attempt: int, name: str, content: str
    ) -> str:
        path = self._attempt_artifact_dir(run_id, attempt) / name
        path.write_text(content, encoding="utf-8")
        return path.relative_to(
            (self.engine_root / self.settings.artifact_dir / "runs" / run_id).resolve()
        ).as_posix()

    def _stage(self, action: str, detail: dict[str, Any] | None = None) -> None:
        self.recorder.add(
            EventKind.STAGE,
            action,
            action,
            detail={**(detail or {}), "status": "RUNNING"},
        )

    def _stage_status(
        self, stage: str, status: str, detail: dict[str, Any] | None = None
    ) -> None:
        self.recorder.add(
            EventKind.STAGE,
            stage,
            "stage_status",
            detail={**(detail or {}), "status": status},
        )

    def _usage_event(self, stage: str) -> None:
        self.recorder.add(
            EventKind.USAGE,
            stage,
            "model_usage",
            detail=self.usage_tracker.snapshot().model_dump(mode="json"),
        )

    def _model_request_event(self, action: str, record: ModelRequestRecord) -> None:
        self.recorder.add(
            EventKind.USAGE,
            record.operation,
            f"model_request_{action}",
            actor=("Investigation Agent" if record.agent == "investigation" else "Repair Agent"),
            detail=record.model_dump(mode="json"),
        )

    def _model_failure(self, exc: Exception, prefix: str) -> WorkflowState:
        status = FinalStatus.EXECUTION_ERROR
        if isinstance(exc, BudgetExceededError):
            status = FinalStatus.BUDGET_EXCEEDED
        elif isinstance(exc, ModelAuthenticationError):
            status = FinalStatus.AUTHENTICATION_ERROR
        elif isinstance(exc, ModelRateLimitError):
            status = FinalStatus.RATE_LIMITED
        elif isinstance(exc, ModelQuotaExceededError):
            status = FinalStatus.QUOTA_EXCEEDED
        elif isinstance(exc, ProvidersExhaustedError):
            status = FinalStatus.PROVIDERS_EXHAUSTED
        elif isinstance(exc, ModelConfigurationError):
            status = FinalStatus.MODEL_CONFIGURATION_ERROR
        elif isinstance(exc, ModelRuntimeError) and exc.code == "MODEL_UNAVAILABLE":
            status = FinalStatus.MODEL_UNAVAILABLE
        reason = f"{prefix}: {type(exc).__name__}: {exc}"
        if isinstance(exc, StructuredOutputError):
            reason = f"STRUCTURED_OUTPUT_RETRIES_EXHAUSTED: {reason}"
        return {
            "status": status.value,
            "terminal_reason": redact_sensitive(
                reason,
                self.settings.gemini_api_key_value,
                self.settings.openrouter_api_key_value,
            ),
        }

    def _persist(self, run_id: str, node: str, state: WorkflowState) -> None:
        output = self.engine_root / self.settings.artifact_dir / "runs" / run_id
        output.mkdir(parents=True, exist_ok=True)
        self._latest_state = {**self._latest_state, **state, "run_id": run_id}
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "node": node,
            "state": state,
        }
        with (
            self._history_lock,
            (output / "state-history.jsonl").open("a", encoding="utf-8") as stream,
        ):
            stream.write(json.dumps(record, default=str) + "\n")

    def _experiments(self) -> list[ScheduleSpec]:
        items = [
            self.target.config.reproduction_schedule,
            *self.target.config.investigation_schedules,
        ]
        unique: dict[str, ScheduleSpec] = {}
        for item in items:
            unique.setdefault(item.name, item)
        return list(unique.values())

    def _load_target(self, state: WorkflowState) -> WorkflowState:
        self._stage("load_target", {"target_id": self.target.config.id})
        update: WorkflowState = {"status": FinalStatus.SUSPECTED.value}
        self._persist(state["run_id"], "load_target", update)
        self._stage_status("load_target", "SUCCEEDED")
        return update

    def _inspect(self, state: WorkflowState) -> WorkflowState:
        self._stage("inspect", {"target_id": self.target.config.id})
        findings = []
        source_files: dict[str, str] = {}
        for relative in self.target.config.operation.source_files:
            path = self.target.repository_root / relative
            source_files[relative] = path.read_text(encoding="utf-8")
            if path.suffix == ".py":
                findings.extend(inspect_python_file(path, self.target.repository_root))
        update: WorkflowState = {
            "findings": [finding.model_dump(mode="json") for finding in findings],
            "source_files": source_files,
            "schema_ddl": load_adapter(self.target).schema_ddl(),
        }
        self._persist(state["run_id"], "inspect", update)
        self._stage_status("inspect", "SUCCEEDED")
        return update

    def _investigation_context(self, state: WorkflowState) -> InvestigationContext:
        experiments = self._experiments()
        checkpoints = sorted(
            {
                action.checkpoint
                for experiment in experiments
                for action in experiment.actions
                if action.checkpoint
            }
        )
        return InvestigationContext(
            target_id=self.target.config.id,
            target_name=self.target.config.name,
            target_operation=self.target.config.operation.callable,
            source_files=state["source_files"],
            schema_ddl=state["schema_ddl"],
            findings=state.get("findings", []),
            transaction_boundaries=[
                "Each actor executes through TargetAdapter.run_actor.",
                "Each bundled adapter opens one independent sessionmaker.begin transaction.",
                "Commit or rollback occurs only when the adapter transaction context exits.",
            ],
            checkpoints=checkpoints,
            available_experiments=experiments,
            invariant=self.target.config.invariant,
            prior_attempts=state.get("investigation_attempts", []),
            repair_evidence_requests=[
                request
                for attempt in state.get("attempts", [])
                for request in (attempt.get("proposal") or {}).get(
                    "additional_evidence_request", []
                )
            ],
        )

    def _investigate(self, state: WorkflowState) -> WorkflowState:
        number = len(state.get("investigation_attempts", [])) + 1
        self._stage("investigate", {"agent": "Investigation Agent", "attempt": number})
        available, reason = self.investigation_model.available()
        if not available:
            update: WorkflowState = {
                "status": FinalStatus.MODEL_UNAVAILABLE.value,
                "terminal_reason": f"Investigation Agent unavailable: {reason}",
            }
            self._persist(state["run_id"], "investigation_agent", update)
            self._stage_status("investigate", "FAILED", {"reason": reason})
            return update

        try:
            decision = self.investigation_model.investigate(self._investigation_context(state))
        except Exception as exc:  # noqa: BLE001 - agent failure becomes terminal evidence
            self._usage_event("investigate")
            update = self._model_failure(exc, "Investigation Agent failed")
            self._persist(state["run_id"], "investigation_agent", update)
            self._stage_status("investigate", "FAILED", {"reason": type(exc).__name__})
            return update
        self._usage_event("investigate")
        known = {experiment.name for experiment in self._experiments()}
        failure_reason = None
        selected = decision.experiment_name
        if selected not in known:
            failure_reason = f"Investigation Agent selected unknown experiment: {selected}"
            selected = ""
        if decision.unresolved:
            failure_reason = "Investigation Agent could not justify another experiment"
            selected = ""
        attempt = InvestigationAttempt(
            number=number,
            decision=decision,
            failure_reason=failure_reason,
        )
        attempts = [
            *state.get("investigation_attempts", []),
            attempt.model_dump(mode="json"),
        ]
        self.recorder.add(
            EventKind.AGENT,
            "investigate",
            "hypothesis",
            actor="Investigation Agent",
            detail=decision.model_dump(mode="json"),
        )
        exhausted = number >= self.settings.max_investigation_attempts
        update = {
            "investigation_attempts": attempts,
            "selected_experiment": selected,
            "status": (
                FinalStatus.UNCONFIRMED.value
                if (decision.unresolved or (failure_reason and exhausted))
                else FinalStatus.SUSPECTED.value
            ),
            "terminal_reason": failure_reason or "",
        }
        self._persist(state["run_id"], "investigation_agent", update)
        self._stage_status(
            "investigate",
            "SUCCEEDED" if selected else ("FAILED" if exhausted else "RETRYING"),
            {"attempt": number},
        )
        return update

    def _route_after_investigation(
        self, state: WorkflowState
    ) -> Literal["execute_experiment", "investigate", "finalize"]:
        if state.get("status") in {
            FinalStatus.MODEL_UNAVAILABLE.value,
            FinalStatus.UNCONFIRMED.value,
            FinalStatus.EXECUTION_ERROR.value,
            FinalStatus.BUDGET_EXCEEDED.value,
            FinalStatus.AUTHENTICATION_ERROR.value,
            FinalStatus.RATE_LIMITED.value,
            FinalStatus.QUOTA_EXCEEDED.value,
            FinalStatus.PROVIDERS_EXHAUSTED.value,
            FinalStatus.MODEL_CONFIGURATION_ERROR.value,
        }:
            return "finalize"
        if state.get("selected_experiment"):
            return "execute_experiment"
        return "investigate"

    def _execute_experiment(self, state: WorkflowState) -> WorkflowState:
        schedule = next(
            item for item in self._experiments() if item.name == state["selected_experiment"]
        )
        self._stage(
            "execute_experiment",
            {"agent": "Investigation Agent", "schedule": schedule.name},
        )
        engine = create_database_engine(self.database_url, self.recorder)
        try:
            result = ControlledScheduler(
                engine, self.recorder, self.settings.schedule_timeout_seconds
            ).execute(
                load_adapter(self.target),
                self.target.config.actors,
                schedule,
                exact=True,
            )
        finally:
            engine.dispose()
        update: WorkflowState = {"experiment_result": result.model_dump(mode="json")}
        self._persist(state["run_id"], "execute_experiment", update)
        self._stage_status(
            "execute_experiment", "SUCCEEDED" if result.completed else "FAILED"
        )
        return update

    def _observe(self, state: WorkflowState) -> WorkflowState:
        self._stage("observe", {"agent": "Investigation Agent"})
        attempts = [dict(item) for item in state["investigation_attempts"]]
        attempts[-1]["result"] = state["experiment_result"]
        result = state["experiment_result"]
        reproduced = bool(result["completed"] and result["reproduced_failure"])
        update: WorkflowState = {"investigation_attempts": attempts}
        if reproduced:
            interpretation_state = dict(state)
            interpretation_state["investigation_attempts"] = attempts
            try:
                conclusion = self.investigation_model.interpret(
                    self._investigation_context(interpretation_state)
                )
            except Exception as exc:  # noqa: BLE001 - agent failure becomes terminal evidence
                self._usage_event("observe")
                update.update(self._model_failure(exc, "Investigation Agent observation failed"))
                self._persist(state["run_id"], "observe", update)
                self._stage_status("observe", "FAILED", {"reason": type(exc).__name__})
                return update
            self._usage_event("observe")
            if not conclusion.repair_ready:
                if len(attempts) < self.settings.max_investigation_attempts:
                    update.update(
                        {
                            "status": FinalStatus.SUSPECTED.value,
                            "terminal_reason": "Investigation Agent requested additional evidence",
                        }
                    )
                else:
                    update.update(
                        {
                            "status": FinalStatus.UNRESOLVED.value,
                            "terminal_reason": (
                                "Confirmed invariant failure, but investigation evidence was "
                                "insufficient for repair at the attempt cap"
                            ),
                        }
                    )
                self._persist(state["run_id"], "observe", update)
                self._stage_status("observe", "SUCCEEDED")
                return update
            artifact = InvestigationArtifact(
                target_id=self.target.config.id,
                target_operation=self.target.config.operation.callable,
                confirmed_vulnerability=True,
                relevant_source_spans=state.get("findings", []),
                reproduced_schedule=result,
                sql_trace=[
                    event for event in result["events"] if event["kind"] == EventKind.SQL.value
                ],
                violated_invariant=result["invariant"],
                final_database_state=result["invariant"]["evidence"],
                hypothesized_root_cause=conclusion.root_cause,
                execution_interpretation=conclusion.execution_interpretation,
                evidence_references=[
                    f"schedule:{result['schedule_name']}",
                    f"invariant:{result['invariant']['name']}",
                    *[
                        f"source:{finding['file']}:{finding['line_start']}"
                        for finding in state.get("findings", [])
                    ],
                    *conclusion.evidence_references,
                ],
            )
            engine = create_database_engine(self.database_url)
            try:
                performance = benchmark_schedule(
                    engine,
                    load_adapter(self.target),
                    self.target.config.actors,
                    self.target.config.verification_schedules[-1],
                    self.target.config.verification.benchmark_runs,
                    self.settings.schedule_timeout_seconds,
                )
            finally:
                engine.dispose()
            update.update(
                {
                    "status": FinalStatus.REPRODUCED.value,
                    "baseline": result,
                    "baseline_performance": performance.model_dump(mode="json"),
                    "investigation_artifact": artifact.model_dump(mode="json"),
                    "terminal_reason": "",
                }
            )
            self.recorder.add(
                EventKind.AGENT,
                "observe",
                "confirmed_handoff",
                actor="Investigation Agent",
                detail={
                    "target_id": artifact.target_id,
                    "root_cause": artifact.hypothesized_root_cause,
                    "execution_interpretation": artifact.execution_interpretation,
                    "invariant": artifact.violated_invariant.model_dump(mode="json"),
                },
            )
        elif len(attempts) >= self.settings.max_investigation_attempts:
            update.update(
                {
                    "status": FinalStatus.UNCONFIRMED.value,
                    "terminal_reason": "Investigation attempt cap reached without an invariant violation",
                }
            )
        else:
            update.update(
                {
                    "status": FinalStatus.SUSPECTED.value,
                    "terminal_reason": "Experiment did not confirm the hypothesis; replanning",
                }
            )
        self._persist(state["run_id"], "observe", update)
        self._stage_status("observe", "SUCCEEDED")
        return update

    def _route_after_observe(
        self, state: WorkflowState
    ) -> Literal["repair_agent", "investigate", "finalize"]:
        if state.get("status") == FinalStatus.REPRODUCED.value:
            return "repair_agent"
        if state.get("status") == FinalStatus.SUSPECTED.value:
            return "investigate"
        return "finalize"

    def _build_bundle(self, state: WorkflowState) -> FailureBundle:
        config = self.target.config
        return FailureBundle(
            target_id=config.id,
            target_operation=config.operation.callable,
            source_files=state["source_files"],
            schema_ddl=state["schema_ddl"],
            findings=state.get("findings", []),
            reproduced_schedule=state["baseline"],
            business_invariant=config.invariant.description,
            allowed_outcomes=config.verification.allowed_outcomes,
            editable_paths=config.operation.editable_paths,
            protected_paths=config.operation.protected_paths,
            investigation_artifact=state["investigation_artifact"],
            previous_attempts=state.get("attempts", []),
        )

    def _repair_agent(self, state: WorkflowState) -> WorkflowState:
        number = state.get("attempt_number", 0) + 1
        self._stage("repair", {"agent": "Repair Agent", "attempt": number})
        if number > 1:
            self._stage_status("repair", "RETRYING", {"attempt": number})
        available, reason = self.repair_model.available()
        if not available:
            update: WorkflowState = {
                "attempt_number": number,
                "status": FinalStatus.MODEL_UNAVAILABLE.value,
                "terminal_reason": f"Repair Agent unavailable: {reason}",
            }
            self._persist(state["run_id"], "repair_agent", update)
            self._stage_status("repair", "FAILED", {"reason": reason})
            return update
        try:
            proposal = self.repair_model.propose(self._build_bundle(state))
        except Exception as exc:  # noqa: BLE001 - agent failure becomes terminal evidence
            self._usage_event("repair")
            update = {
                "attempt_number": number,
                **self._model_failure(exc, "Repair Agent failed"),
            }
            self._persist(state["run_id"], "repair_agent", update)
            self._stage_status("repair", "FAILED", {"reason": type(exc).__name__})
            return update
        self._usage_event("repair")
        attempt = RepairAttempt(number=number, proposal=proposal)
        candidate_diff = proposal.unified_diff
        if proposal.file_replacements:
            try:
                candidate_diff = replacement_diff(
                    state["source_files"], proposal.file_replacements
                )
            except ValueError as exc:
                attempt.failure_reason = str(exc)
        attempt.generated_diff = candidate_diff or None
        attempt.artifact_paths["model_proposal"] = self._write_attempt_artifact(
            state["run_id"],
            number,
            "model-proposal.json",
            proposal.model_dump_json(indent=2),
        )
        if candidate_diff:
            attempt.artifact_paths["generated_diff"] = self._write_attempt_artifact(
                state["run_id"], number, "generated.diff", candidate_diff
            )
        if not candidate_diff.strip() and not proposal.cannot_repair:
            attempt.failure_reason = attempt.failure_reason or "proposal contains no executable edit"
        investigation_available = (
            len(state.get("investigation_attempts", [])) < self.settings.max_investigation_attempts
        )
        if proposal.request_additional_investigation and investigation_available:
            attempt.failure_reason = "Repair Agent requested additional investigation evidence"
        elif proposal.cannot_repair or proposal.request_additional_investigation:
            attempt.failure_reason = "Repair Agent reported insufficient evidence for a safe repair"
        normalized_diff = "\n".join(
            line.rstrip() for line in candidate_diff.strip().splitlines()
        )
        diff_hash = hashlib.sha256(normalized_diff.encode()).hexdigest()
        prior_hashes = {
            hashlib.sha256(
                "\n".join(
                    line.rstrip()
                    for line in (item.get("generated_diff") or "").strip().splitlines()
                ).encode()
            ).hexdigest()
            for item in state.get("attempts", [])
            if item.get("generated_diff")
        }
        if diff_hash in prior_hashes:
            attempt.failure_reason = "Repair Agent repeated an identical candidate diff"
        attempts = [*state.get("attempts", []), attempt.model_dump(mode="json")]
        self.recorder.add(
            EventKind.AGENT,
            "repair",
            "repair_strategy",
            actor="Repair Agent",
            detail={
                "attempt": number,
                "strategy": proposal.strategy,
                "diagnosis": proposal.diagnosis,
                "cannot_repair": proposal.cannot_repair,
                "request_additional_investigation": proposal.request_additional_investigation,
                "additional_evidence_request": proposal.additional_evidence_request,
            },
        )
        update: WorkflowState = {
            "attempt_number": number,
            "attempts": attempts,
            "proposal": proposal.model_dump(mode="json"),
            "candidate_diff": candidate_diff,
            "candidate_path": "",
            "status": (
                FinalStatus.SUSPECTED.value
                if proposal.request_additional_investigation and investigation_available
                else (
                    FinalStatus.UNRESOLVED.value
                    if attempt.failure_reason
                    else FinalStatus.REPRODUCED.value
                )
            ),
            "terminal_reason": attempt.failure_reason or "",
        }
        self._persist(state["run_id"], "repair_agent", update)
        self._stage_status(
            "repair", "FAILED" if attempt.failure_reason else "SUCCEEDED", {"attempt": number}
        )
        return update

    def _route_after_repair_agent(
        self, state: WorkflowState
    ) -> Literal["apply_patch", "investigate", "repair_agent", "finalize"]:
        if state.get("status") in {
            FinalStatus.MODEL_UNAVAILABLE.value,
            FinalStatus.EXECUTION_ERROR.value,
            FinalStatus.BUDGET_EXCEEDED.value,
            FinalStatus.AUTHENTICATION_ERROR.value,
            FinalStatus.RATE_LIMITED.value,
            FinalStatus.QUOTA_EXCEEDED.value,
            FinalStatus.PROVIDERS_EXHAUSTED.value,
            FinalStatus.MODEL_CONFIGURATION_ERROR.value,
        }:
            return "finalize"
        if state.get("status") == FinalStatus.SUSPECTED.value:
            return "investigate"
        if not state.get("terminal_reason"):
            return "apply_patch"
        if state.get("attempt_number", 0) < self.settings.max_repair_attempts:
            return "repair_agent"
        return "finalize"

    def _apply_patch(self, state: WorkflowState) -> WorkflowState:
        self._stage("apply_patch", {"attempt": state["attempt_number"]})
        candidate = self.workspace.create_candidate(state["run_id"], state["attempt_number"])
        validation = self.guard.apply(candidate, state["candidate_diff"])
        attempts = [dict(item) for item in state["attempts"]]
        attempts[-1]["patch_validation"] = validation.model_dump(mode="json")
        artifact_paths = dict(attempts[-1].get("artifact_paths", {}))
        artifact_paths["patch_validation"] = self._write_attempt_artifact(
            state["run_id"],
            state["attempt_number"],
            "patch-validation.json",
            validation.model_dump_json(indent=2),
        )
        if validation.accepted:
            artifact_paths["applied_diff"] = self._write_attempt_artifact(
                state["run_id"],
                state["attempt_number"],
                "applied.diff",
                state["candidate_diff"],
            )
        attempts[-1]["artifact_paths"] = artifact_paths
        reason = "" if validation.accepted else "; ".join(validation.errors)
        if reason:
            attempts[-1]["failure_reason"] = reason
        update: WorkflowState = {
            "attempts": attempts,
            "candidate_path": str(candidate) if validation.accepted else "",
            "status": FinalStatus.REPRODUCED.value
            if validation.accepted
            else FinalStatus.UNRESOLVED.value,
            "terminal_reason": reason,
        }
        self._persist(state["run_id"], "apply_patch", update)
        self._stage_status(
            "apply_patch",
            "SUCCEEDED" if validation.accepted else "FAILED",
            {"attempt": state["attempt_number"], "errors": validation.errors},
        )
        if not validation.accepted and state.get("attempt_number", 0) < self.settings.max_repair_attempts:
            self._stage_status("repair", "RETRYING", {"next_attempt": state["attempt_number"] + 1})
        return update

    def _route_after_apply(
        self, state: WorkflowState
    ) -> Literal["verify", "repair_agent", "finalize"]:
        if state.get("candidate_path"):
            return "verify"
        if state.get("attempt_number", 0) < self.settings.max_repair_attempts:
            return "repair_agent"
        return "finalize"

    def _verify(self, state: WorkflowState) -> WorkflowState:
        self._stage("verify", {"attempt": state["attempt_number"]})
        candidate = Path(state["candidate_path"])
        run_copy = self.workspace.create_run_copy(
            state["run_id"], candidate, state["attempt_number"]
        )
        report = verify_candidate(
            run_copy,
            self.target.config_relative_path,
            self.database_url,
            baseline_p95_ms=state.get("baseline_performance", {}).get("p95_latency_ms"),
            timeout_seconds=max(
                180, self.target.config.verification.regression.timeout_seconds + 60
            ),
        )
        attempts = [dict(item) for item in state["attempts"]]
        attempts[-1]["verification"] = report.model_dump(mode="json")
        artifact_paths = dict(attempts[-1].get("artifact_paths", {}))
        artifact_paths["verification"] = self._write_attempt_artifact(
            state["run_id"],
            state["attempt_number"],
            "verification.json",
            report.model_dump_json(indent=2),
        )
        attempts[-1]["artifact_paths"] = artifact_paths
        self.recorder.add(
            EventKind.AGENT,
            "verify",
            "verification_feedback",
            actor="Repair Agent",
            detail={
                "attempt": state["attempt_number"],
                "passed": report.passed,
                "gates": report.gates,
            },
        )
        if report.passed:
            diff = canonical_diff(
                self.target.repository_root,
                candidate,
                attempts[-1]["patch_validation"]["changed_paths"],
            )
            status = FinalStatus.VERIFIED_CANDIDATE.value
            reason = ""
        else:
            failed = [name for name, passed in report.gates.items() if not passed]
            reason = "failed verification gates: " + ", ".join(failed)
            if report.performance is not None and "performance_within_configured_limit" in failed:
                performance = report.performance
                baseline_p95 = state.get("baseline_performance", {}).get("p95_latency_ms")
                ratio = (
                    performance.p95_latency_ms / baseline_p95
                    if baseline_p95
                    else None
                )
                reason += (
                    f"; baseline_p95_ms={baseline_p95}; "
                    f"candidate_p95_ms={performance.p95_latency_ms}; "
                    f"regression_ratio={ratio}; samples={performance.runs}"
                )
            attempts[-1]["failure_reason"] = reason
            diff = ""
            status = (
                FinalStatus.UNVERIFIABLE_SCHEDULE.value
                if not report.gates.get("schedule_semantically_valid", False)
                else FinalStatus.UNRESOLVED.value
            )
        update: WorkflowState = {
            "attempts": attempts,
            "status": status,
            "terminal_reason": reason,
            "final_diff": diff,
        }
        self._persist(state["run_id"], "verify", update)
        self._stage_status(
            "verify", "SUCCEEDED" if report.passed else "FAILED", {"attempt": state["attempt_number"]}
        )
        return update

    def _route_after_verify(self, state: WorkflowState) -> Literal["repair_agent", "finalize"]:
        if state.get("status") == FinalStatus.VERIFIED_CANDIDATE.value:
            return "finalize"
        if state.get("attempt_number", 0) < self.settings.max_repair_attempts:
            return "repair_agent"
        return "finalize"

    def _finalize(self, state: WorkflowState) -> WorkflowState:
        self._stage("finalize", {"status": state.get("status")})
        if not any(attempt.get("verification") for attempt in state.get("attempts", [])):
            self._stage_status("verify", "SKIPPED")
        self._persist(state["run_id"], "finalize", {"status": state.get("status", "")})
        self._stage_status("finalize", "SUCCEEDED", {"result": state.get("status", "")})
        return state

    def _build_graph(self):
        graph = StateGraph(WorkflowState)
        graph.add_node("load_target", self._load_target)
        graph.add_node("inspect", self._inspect)
        graph.add_node("investigate", self._investigate)
        graph.add_node("execute_experiment", self._execute_experiment)
        graph.add_node("observe", self._observe)
        graph.add_node("repair_agent", self._repair_agent)
        graph.add_node("apply_patch", self._apply_patch)
        graph.add_node("verify", self._verify)
        graph.add_node("finalize", self._finalize)
        graph.add_edge(START, "load_target")
        graph.add_edge("load_target", "inspect")
        graph.add_edge("inspect", "investigate")
        graph.add_conditional_edges("investigate", self._route_after_investigation)
        graph.add_edge("execute_experiment", "observe")
        graph.add_conditional_edges("observe", self._route_after_observe)
        graph.add_conditional_edges("repair_agent", self._route_after_repair_agent)
        graph.add_conditional_edges("apply_patch", self._route_after_apply)
        graph.add_conditional_edges("verify", self._route_after_verify)
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=InMemorySaver())

    def _build_report(
        self,
        state: WorkflowState,
        run_id: str,
        started_at: datetime,
        status: FinalStatus | None = None,
    ) -> RunReport:
        config = self.target.config
        final_status = status or FinalStatus(state.get("status", FinalStatus.EXECUTION_ERROR.value))
        finished_at = datetime.now(UTC)
        events = self.recorder.snapshot()
        return RunReport(
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            target_id=config.id,
            target_name=config.name,
            target_operation=config.operation.callable,
            invariant_name=config.invariant.name,
            invariant_description=config.invariant.description,
            status=final_status,
            terminal_reason=state.get("terminal_reason", ""),
            findings=state.get("findings", []),
            investigation_attempts=state.get("investigation_attempts", []),
            investigation_artifact=state.get("investigation_artifact"),
            baseline=state.get("baseline"),
            attempts=state.get("attempts", []),
            final_diff=state.get("final_diff") or None,
            events=events,
            stage_timings_ms=stage_timings_ms(events, finished_at),
            limitations=[
                *config.limitations,
                "Investigation and repair reasoning require configured LLM access.",
                "Verification covers named finite schedules and is not a proof of race freedom.",
                state.get("terminal_reason", ""),
            ],
            investigation_model=self.settings.resolved_investigation_model,
            repair_model=self.settings.resolved_repair_model,
            fallback_model=self.settings.fallback_model,
            fallback_enabled=self.settings.enable_model_fallback,
            provider_order=self.settings.provider_sequence,
            auto_fallback=self.settings.auto_fallback,
            openrouter_investigation_models=self.settings.openrouter_investigation_model_list,
            openrouter_repair_models=self.settings.openrouter_repair_model_list,
            usage=self.usage_tracker.snapshot(),
        )

    def _write_report(self, report: RunReport) -> None:
        output = self.engine_root / self.settings.artifact_dir / "runs" / report.run_id
        output.mkdir(parents=True, exist_ok=True)
        (output / "report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
        if report.final_diff:
            (output / "candidate.diff").write_text(report.final_diff, encoding="utf-8")

    def run(self, run_id: str | None = None) -> RunReport:
        run_id = run_id or uuid.uuid4().hex[:12]
        started_at = datetime.now(UTC)
        initial: WorkflowState = {
            "run_id": run_id,
            "status": FinalStatus.SUSPECTED.value,
            "investigation_attempts": [],
            "attempts": [],
            "attempt_number": 0,
        }
        self._latest_state = initial
        try:
            final = self.graph.invoke(
                initial,
                config={"configurable": {"thread_id": run_id}, "recursion_limit": 40},
            )
            report = self._build_report(final, run_id, started_at)
        except KeyboardInterrupt:
            state = {
                **self._latest_state,
                "status": FinalStatus.INTERRUPTED.value,
                "terminal_reason": "Run interrupted by operator",
            }
            self._persist(run_id, "interrupted", state)
            report = self._build_report(state, run_id, started_at, FinalStatus.INTERRUPTED)
        except Exception as exc:  # noqa: BLE001 - persist handled workflow failures
            failure = self._model_failure(exc, "Workflow failed")
            state = {**self._latest_state, **failure}
            self._persist(run_id, "workflow_error", state)
            report = self._build_report(state, run_id, started_at)
        self._write_report(report)
        return report
