from pathlib import Path

import pytest

from raceproof.contracts import FinalStatus, RunReport
from raceproof.report_store import InvalidReportArtifact, InvalidRunId, ReportStore


def make_report(run_id: str) -> RunReport:
    return RunReport(
        run_id=run_id,
        target_id="inventory_oversell",
        target_name="Inventory oversell",
        target_operation="inventory_app.operations:purchase_inventory",
        invariant_name="inventory_conservation",
        invariant_description="Initial stock equals final stock plus committed orders.",
        status=FinalStatus.INTERRUPTED,
        terminal_reason="Run interrupted by operator",
    )


def write_report(root: Path, report: RunReport) -> Path:
    path = root / "artifacts" / "runs" / report.run_id / "report.json"
    path.parent.mkdir(parents=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def test_report_store_loads_valid_report_without_modifying_it(tmp_path: Path) -> None:
    report = make_report("a1b2c3d4e5f6")
    path = write_report(tmp_path, report)
    before = path.read_bytes()

    loaded = ReportStore(tmp_path, Path("artifacts")).load(report.run_id)

    assert loaded == report
    assert path.read_bytes() == before


@pytest.mark.parametrize("run_id", ["../report", "ABCDEF123456", "abc", "a" * 13])
def test_report_store_rejects_invalid_run_ids(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(InvalidRunId):
        ReportStore(tmp_path, Path("artifacts")).load(run_id)


def test_report_store_rejects_mismatched_report_identity(tmp_path: Path) -> None:
    path = write_report(tmp_path, make_report("a1b2c3d4e5f6"))
    destination = tmp_path / "artifacts" / "runs" / "111111111111" / "report.json"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(path.read_bytes())

    with pytest.raises(InvalidReportArtifact):
        ReportStore(tmp_path, Path("artifacts")).load("111111111111")
