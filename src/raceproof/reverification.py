from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from raceproof.config import get_settings
from raceproof.contracts import RunReport
from raceproof.patching import PatchGuard, WorkspaceManager, canonical_diff
from raceproof.target import TargetRegistry
from raceproof.verification import verify_candidate


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _baseline_p95(history_path: Path) -> float | None:
    if not history_path.is_file():
        return None
    for line in history_path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        performance = record.get("state", {}).get("baseline_performance")
        if performance and performance.get("p95_latency_ms") is not None:
            return float(performance["p95_latency_ms"])
    return None


def reverify_persisted_run(engine_root: Path, run_id: str) -> dict[str, Any]:
    engine_root = engine_root.resolve()
    settings = get_settings()
    run_root = (engine_root / settings.artifact_dir / "runs" / run_id).resolve()
    report_path = run_root / "report.json"
    if not report_path.is_file():
        raise FileNotFoundError(f"persisted report not found: {report_path}")
    original = RunReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    target = TargetRegistry(engine_root / "examples").get(original.target_id)
    database_url = os.environ.get(target.config.database_url_env, settings.database_url)
    artifact_root = (engine_root / settings.artifact_dir).resolve()
    replay_id = f"reverify-{run_id}"
    workspace = WorkspaceManager(target.repository_root, artifact_root)
    guard = PatchGuard(
        set(target.config.operation.editable_paths),
        tuple(path.rstrip("/") for path in target.config.operation.protected_paths),
    )
    baseline_hashes = {
        path: _file_hash(target.repository_root / path)
        for path in target.config.operation.source_files
    }
    baseline_p95_ms = _baseline_p95(run_root / "state-history.jsonl")
    output_root = artifact_root / "reverification" / run_id
    output_root.mkdir(parents=True, exist_ok=True)
    results = []

    for attempt in original.attempts:
        if attempt.proposal is None or not attempt.proposal.unified_diff.strip():
            results.append(
                {
                    "attempt": attempt.number,
                    "replayed": False,
                    "reason": "original executable diff is unavailable",
                }
            )
            continue
        attempt_root = output_root / f"attempt-{attempt.number}"
        attempt_root.mkdir(parents=True, exist_ok=True)
        original_diff = attempt.proposal.unified_diff
        (attempt_root / "original-generated.diff").write_text(original_diff, encoding="utf-8")
        (attempt_root / "original-verification.json").write_text(
            (
                attempt.verification.model_dump_json(indent=2)
                if attempt.verification is not None
                else "null\n"
            ),
            encoding="utf-8",
        )
        candidate = workspace.create_candidate(replay_id, attempt.number)
        validation = guard.apply(candidate, original_diff)
        (attempt_root / "patch-validation.json").write_text(
            validation.model_dump_json(indent=2), encoding="utf-8"
        )
        item: dict[str, Any] = {
            "attempt": attempt.number,
            "replayed": validation.accepted,
            "workspace": str(candidate),
            "patch_validation": validation.model_dump(mode="json"),
        }
        if validation.accepted:
            run_copy = workspace.create_run_copy(replay_id, candidate, attempt.number)
            verification = verify_candidate(
                run_copy,
                target.config_relative_path,
                database_url,
                baseline_p95_ms=baseline_p95_ms,
                timeout_seconds=max(
                    180, target.config.verification.regression.timeout_seconds + 60
                ),
            )
            applied_diff = canonical_diff(
                target.repository_root, candidate, validation.changed_paths
            )
            (attempt_root / "applied.diff").write_text(applied_diff, encoding="utf-8")
            (attempt_root / "new-verification.json").write_text(
                verification.model_dump_json(indent=2), encoding="utf-8"
            )
            item.update(
                {
                    "run_workspace": str(run_copy),
                    "exact_diff_preserved": applied_diff == original_diff,
                    "verification": verification.model_dump(mode="json"),
                }
            )
        results.append(item)

    source_unchanged = all(
        _file_hash(target.repository_root / path) == digest
        for path, digest in baseline_hashes.items()
    )
    summary = {
        "source_run_id": run_id,
        "replay_id": replay_id,
        "target_id": original.target_id,
        "baseline_p95_ms": baseline_p95_ms,
        "original_source_unchanged": source_unchanged,
        "attempts": results,
    }
    (output_root / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reverify persisted model-generated candidates without an API call."
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    result = reverify_persisted_run(args.root, args.run_id)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
