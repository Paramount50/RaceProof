from __future__ import annotations

import json
import os
from typing import Any

from raceproof.config import Settings
from raceproof.contracts import FinalStatus, RunReport
from raceproof.observability import LangSmithObservability, configure_langsmith_environment
from raceproof.usage import UsageLimits, UsageTracker


class FakeRun:
    def __init__(self) -> None:
        self.metadata: dict[str, Any] = {}

    def get_url(self) -> str:
        return "https://smith.langchain.com/o/example/r/example"

    def add_metadata(self, metadata: dict[str, Any]) -> None:
        self.metadata.update(metadata)


class FakeTraceContext:
    def __init__(self, run: FakeRun, fail_exit: bool = False) -> None:
        self.run = run
        self.fail_exit = fail_exit

    def __enter__(self) -> FakeRun:
        return self.run

    def __exit__(self, *_args) -> None:
        if self.fail_exit:
            raise RuntimeError("trace backend unavailable")


def test_langsmith_is_disabled_by_default_and_without_a_key() -> None:
    default = LangSmithObservability(Settings(_env_file=None)).status
    missing_key = LangSmithObservability(
        Settings(_env_file=None, LANGSMITH_TRACING=True)
    ).status
    assert default["enabled"] is False
    assert missing_key["enabled"] is False
    assert missing_key["configured"] is False


def test_metadata_only_trace_hides_content_and_preserves_usage(monkeypatch) -> None:
    for name in (
        "LANGSMITH_TRACING",
        "LANGSMITH_API_KEY",
        "LANGSMITH_PROJECT",
        "LANGSMITH_HIDE_INPUTS",
        "LANGSMITH_HIDE_OUTPUTS",
    ):
        monkeypatch.delenv(name, raising=False)
    captured: dict[str, Any] = {}
    run = FakeRun()

    def factory(*args, **kwargs):
        captured.update({"args": args, "kwargs": kwargs})
        return FakeTraceContext(run)

    settings = Settings(
        _env_file=None,
        LANGSMITH_TRACING=True,
        LANGSMITH_API_KEY="trace-secret",
        LANGSMITH_PROJECT="raceproof-tests",
    )
    tracker = UsageTracker(UsageLimits())
    before = tracker.snapshot()
    observability = LangSmithObservability(settings, factory)
    with observability.trace_run("a1b2c3d4e5f6", "inventory_oversell") as session:
        report = RunReport(
            run_id="a1b2c3d4e5f6",
            target_id="inventory_oversell",
            target_name="Inventory oversell",
            target_operation="inventory_app.operations:purchase_inventory",
            invariant_name="inventory_conservation",
            invariant_description="Initial stock equals final stock plus committed orders.",
            status=FinalStatus.MODEL_UNAVAILABLE,
            investigation_model="gemini-test",
            repair_model="gemini-test",
        )
        session.record_report(report)

    exported = json.dumps({"captured": captured, "metadata": run.metadata})
    assert "trace-secret" not in exported
    assert "raw source content" not in exported
    assert captured["kwargs"]["inputs"] == {
        "run_id": "a1b2c3d4e5f6",
        "target_id": "inventory_oversell",
    }
    assert run.metadata["final_status"] == "MODEL_UNAVAILABLE"
    assert tracker.snapshot() == before


def test_privacy_flags_are_forced_and_trace_failures_are_non_fatal(monkeypatch) -> None:
    for name in (
        "LANGSMITH_TRACING",
        "LANGSMITH_API_KEY",
        "LANGSMITH_HIDE_INPUTS",
        "LANGSMITH_HIDE_OUTPUTS",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(
        _env_file=None,
        LANGSMITH_TRACING=True,
        LANGSMITH_API_KEY="trace-secret",
    )
    status = configure_langsmith_environment(settings)
    assert status["enabled"] is True
    assert os.environ["LANGSMITH_HIDE_INPUTS"] == "true"
    assert os.environ["LANGSMITH_HIDE_OUTPUTS"] == "true"

    def failing_factory(*_args, **_kwargs):
        raise RuntimeError("trace backend unavailable")

    with LangSmithObservability(settings, failing_factory).trace_run(
        "a1b2c3d4e5f6", "inventory_oversell"
    ) as session:
        assert session.trace_url is None
        assert session.status["enabled"] is False


def test_trace_export_failure_clears_reference_and_is_non_fatal(monkeypatch) -> None:
    for name in (
        "LANGSMITH_TRACING",
        "LANGSMITH_API_KEY",
        "LANGSMITH_HIDE_INPUTS",
        "LANGSMITH_HIDE_OUTPUTS",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(
        _env_file=None,
        LANGSMITH_TRACING=True,
        LANGSMITH_API_KEY="trace-secret",
    )
    session = LangSmithObservability(
        settings, lambda *_args, **_kwargs: FakeTraceContext(FakeRun(), fail_exit=True)
    ).trace_run("a1b2c3d4e5f6", "inventory_oversell")

    with session:
        assert session.trace_url is not None

    assert session.trace_url is None
    assert session.status["enabled"] is False
    assert session.status["reason"] == "trace export failed"


def test_unsafe_content_visibility_disables_tracing() -> None:
    status = LangSmithObservability(
        Settings(
            _env_file=None,
            LANGSMITH_TRACING=True,
            LANGSMITH_API_KEY="trace-secret",
            LANGSMITH_HIDE_INPUTS=False,
        )
    ).status
    assert status["enabled"] is False
    assert "requires hidden inputs" in str(status["reason"])
