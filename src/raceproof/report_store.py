from __future__ import annotations

import re
from pathlib import Path

from pydantic import ValidationError

from raceproof.contracts import RunReport

RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")
MAX_REPORT_BYTES = 5 * 1024 * 1024


class InvalidRunId(ValueError):
    pass


class InvalidReportArtifact(ValueError):
    pass


class ReportStore:
    def __init__(self, engine_root: Path, artifact_dir: Path) -> None:
        self.engine_root = engine_root.resolve()
        artifact_root = artifact_dir if artifact_dir.is_absolute() else self.engine_root / artifact_dir
        self.runs_root = (artifact_root / "runs").resolve()

    def report_path(self, run_id: str) -> Path:
        if RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise InvalidRunId("run ID must contain exactly 12 lowercase hexadecimal characters")
        path = (self.runs_root / run_id / "report.json").resolve()
        if not path.is_relative_to(self.runs_root):
            raise InvalidReportArtifact("report path escapes the configured artifact directory")
        return path

    def load(self, run_id: str) -> RunReport | None:
        loaded = self.load_with_bytes(run_id)
        return loaded[0] if loaded is not None else None

    def load_with_bytes(self, run_id: str) -> tuple[RunReport, bytes] | None:
        path = self.report_path(run_id)
        if not path.is_file():
            return None
        if path.stat().st_size > MAX_REPORT_BYTES:
            raise InvalidReportArtifact("report artifact exceeds the configured size limit")
        try:
            raw_report = path.read_bytes()
            report = RunReport.model_validate_json(raw_report)
        except (OSError, UnicodeError, ValidationError) as exc:
            raise InvalidReportArtifact("report artifact failed validation") from exc
        if report.run_id != run_id:
            raise InvalidReportArtifact("report artifact run ID does not match its directory")
        return report, raw_report
