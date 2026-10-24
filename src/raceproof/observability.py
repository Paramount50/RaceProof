from __future__ import annotations

import os
from collections.abc import Callable
from types import TracebackType
from typing import Any, Self

from raceproof.config import Settings
from raceproof.contracts import RunReport


def langsmith_status(settings: Settings) -> dict[str, object]:
    configured = bool(settings.langsmith_api_key_value)
    privacy_safe = settings.langsmith_hide_inputs and settings.langsmith_hide_outputs
    enabled = settings.langsmith_tracing and configured and privacy_safe
    if not settings.langsmith_tracing:
        reason = "tracing is disabled"
    elif not configured:
        reason = "LANGSMITH_API_KEY is not configured"
    elif not privacy_safe:
        reason = "metadata-only tracing requires hidden inputs and outputs"
    else:
        reason = "metadata-only tracing is enabled"
    return {
        "enabled": enabled,
        "configured": configured,
        "project": settings.langsmith_project,
        "endpoint_configured": bool(settings.langsmith_endpoint),
        "hide_inputs": settings.langsmith_hide_inputs,
        "hide_outputs": settings.langsmith_hide_outputs,
        "reason": reason,
    }


def configure_langsmith_environment(settings: Settings) -> dict[str, object]:
    status = langsmith_status(settings)
    os.environ["LANGSMITH_TRACING"] = "true" if status["enabled"] else "false"
    if status["enabled"]:
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key_value or ""
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
        os.environ["LANGSMITH_HIDE_INPUTS"] = "true"
        os.environ["LANGSMITH_HIDE_OUTPUTS"] = "true"
        if settings.langsmith_endpoint:
            os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
    return status


class TraceSession:
    def __init__(
        self,
        settings: Settings,
        run_id: str,
        target_id: str,
        trace_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings
        self.run_id = run_id
        self.target_id = target_id
        self.status = configure_langsmith_environment(settings)
        self.trace_factory = trace_factory
        self._context: Any | None = None
        self._run: Any | None = None
        self.trace_url: str | None = None

    def __enter__(self) -> Self:
        if not self.status["enabled"]:
            return self
        try:
            factory = self.trace_factory
            if factory is None:
                from langsmith.run_helpers import trace

                factory = trace
            self._context = factory(
                "raceproof.run",
                run_type="chain",
                inputs={"run_id": self.run_id, "target_id": self.target_id},
                project_name=self.settings.langsmith_project,
                tags=["raceproof", self.target_id],
                metadata={
                    "raceproof_run_id": self.run_id,
                    "target_id": self.target_id,
                    "content_policy": "metadata_only",
                },
            )
            self._run = self._context.__enter__()
            self.trace_url = self._safe_trace_url()
        except Exception:  # noqa: BLE001 - observability must never fail a run
            self._context = None
            self._run = None
            self.trace_url = None
            self.status = {**self.status, "enabled": False, "reason": "trace initialization failed"}
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self._context is not None:
            try:
                self._context.__exit__(exc_type, exc, traceback)
            except Exception:  # noqa: BLE001 - tracing failure cannot change workflow outcome
                self.trace_url = None
                self.status = {**self.status, "enabled": False, "reason": "trace export failed"}
        return False

    def _safe_trace_url(self) -> str | None:
        if self._run is None or not hasattr(self._run, "get_url"):
            return None
        try:
            value = self._run.get_url()
        except Exception:  # noqa: BLE001 - URL discovery is optional
            return None
        return value if isinstance(value, str) and value.startswith("https://") else None

    def record_report(self, report: RunReport) -> None:
        if self._run is None or not hasattr(self._run, "add_metadata"):
            return
        verification = next(
            (
                attempt.verification
                for attempt in reversed(report.attempts)
                if attempt.verification is not None
            ),
            None,
        )
        metadata = {
            "final_status": report.status.value,
            "investigation_model": report.investigation_model,
            "repair_model": report.repair_model,
            "model_requests": report.usage.total_requests,
            "reported_tokens": report.usage.total_tokens,
            "fallback_invocations": report.usage.fallback_invocations,
            "repair_attempts": len(report.attempts),
            "patch_generated": bool(report.final_diff),
            "verification_passed": bool(verification and verification.passed),
            "verification_gates": verification.gates if verification else {},
            "structured_output_failures": sum(
                item.error_code == "STRUCTURED_OUTPUT_ERROR" for item in report.usage.request_log
            ),
            "stage_timings_ms": report.stage_timings_ms,
        }
        try:
            self._run.add_metadata(metadata)
        except Exception:  # noqa: BLE001 - tracing failure cannot change workflow outcome
            self.trace_url = None
            self.status = {**self.status, "enabled": False, "reason": "trace metadata failed"}


class LangSmithObservability:
    def __init__(
        self, settings: Settings, trace_factory: Callable[..., Any] | None = None
    ) -> None:
        self.settings = settings
        self.trace_factory = trace_factory

    @property
    def status(self) -> dict[str, object]:
        return langsmith_status(self.settings)

    def trace_run(self, run_id: str, target_id: str) -> TraceSession:
        return TraceSession(self.settings, run_id, target_id, self.trace_factory)
