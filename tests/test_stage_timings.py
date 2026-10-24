from datetime import UTC, datetime, timedelta

from raceproof.contracts import EventKind, RunEvent
from raceproof.workflow import stage_timings_ms


def stage_event(sequence: int, stage: str, status: str, timestamp: datetime) -> RunEvent:
    return RunEvent(
        sequence=sequence,
        timestamp=timestamp,
        kind=EventKind.STAGE,
        stage=stage,
        action="stage_status",
        detail={"status": status},
    )


def test_stage_timings_accumulate_retries_and_close_running_stage() -> None:
    start = datetime(2026, 10, 24, tzinfo=UTC)
    events = [
        stage_event(1, "repair", "RUNNING", start),
        stage_event(2, "repair", "RETRYING", start + timedelta(milliseconds=12)),
        stage_event(3, "repair", "RUNNING", start + timedelta(milliseconds=20)),
        stage_event(4, "repair", "SUCCEEDED", start + timedelta(milliseconds=35)),
        stage_event(5, "finalize", "RUNNING", start + timedelta(milliseconds=40)),
    ]

    assert stage_timings_ms(events, start + timedelta(milliseconds=50)) == {
        "repair": 27.0,
        "finalize": 10.0,
    }
