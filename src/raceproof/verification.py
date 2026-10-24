from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from raceproof.contracts import VerificationReport


def verify_candidate(
    candidate: Path,
    config_relative_path: str,
    database_url: str,
    baseline_p95_ms: float | None = None,
    timeout_seconds: int = 180,
) -> VerificationReport:
    engine_src = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(engine_src), str(candidate / "src")]),
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "raceproof.verify_worker",
            "--database-url",
            database_url,
            "--root",
            str(candidate),
            "--config",
            config_relative_path,
            *(["--baseline-p95-ms", str(baseline_p95_ms)] if baseline_p95_ms is not None else []),
        ],
        cwd=candidate,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        shell=False,
        check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"verification worker produced no report: {completed.stderr[-4000:]}")
    try:
        return VerificationReport.model_validate(json.loads(lines[-1]))
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"verification worker returned invalid JSON: {exc}; stderr={completed.stderr[-2000:]}"
        ) from exc
