from types import SimpleNamespace

from raceproof.scheduler import benchmark_schedule


def test_benchmark_excludes_warmup_and_keeps_engine_alive(monkeypatch) -> None:
    calls = []

    class FakeScheduler:
        def __init__(self, engine, recorder, timeout_seconds):
            calls.append(("init", engine, timeout_seconds))

        def execute(self, adapter, actors, schedule, *, exact):
            return SimpleNamespace(
                elapsed_ms=10 + len(calls),
                actor_outcomes={"a": "accepted", "b": "rejected"},
                workers_terminated=True,
                completed=True,
                error=None,
            )

    class FakeEngine:
        def dispose(self):
            raise AssertionError("benchmark must not dispose a shared engine per sample")

    monkeypatch.setattr("raceproof.scheduler.ControlledScheduler", FakeScheduler)
    result = benchmark_schedule(
        FakeEngine(),
        object(),
        [SimpleNamespace(id="a"), SimpleNamespace(id="b")],
        SimpleNamespace(name="benchmark"),
        runs=3,
        warmup_runs=1,
    )

    assert result.runs == 3
    assert result.warmup_runs == 1
    assert len(result.latency_samples_ms) == 3
    assert result.median_latency_ms is not None
    assert len(calls) == 4
