from pathlib import Path

import pytest

from raceproof.contracts import FileReplacement
from raceproof.patching import PatchGuard, WorkspaceManager, replacement_diff

GUARD = PatchGuard(
    {"src/inventory_app/operations.py"},
    ("tests", "raceproof.json", "raceproof_adapter.py"),
)


def test_patch_guard_rejects_protected_test_file() -> None:
    diff = """diff --git a/tests/test_oracle.py b/tests/test_oracle.py
--- a/tests/test_oracle.py
+++ b/tests/test_oracle.py
@@ -1 +1 @@
-assert failed
+assert passed
"""
    result = GUARD.validate(diff)
    assert not result.accepted
    assert any("not editable" in error for error in result.errors)


def test_patch_guard_rejects_generated_sleeps() -> None:
    diff = """diff --git a/src/inventory_app/operations.py b/src/inventory_app/operations.py
--- a/src/inventory_app/operations.py
+++ b/src/inventory_app/operations.py
@@ -1 +1,2 @@
 from __future__ import annotations
+time.sleep(1)
"""
    result = GUARD.validate(diff)
    assert not result.accepted
    assert "forbidden addition: time.sleep(" in result.errors


def test_workspace_keeps_external_baseline_unchanged(tmp_path: Path) -> None:
    baseline = tmp_path / "target-app"
    baseline.mkdir()
    (baseline / "source.py").write_text("original", encoding="utf-8")
    manager = WorkspaceManager(baseline, tmp_path / "engine-artifacts")
    candidate = manager.create_candidate("run-one", 1)
    (candidate / "source.py").write_text("candidate", encoding="utf-8")
    assert (baseline / "source.py").read_text(encoding="utf-8") == "original"


def test_workspaces_are_isolated_by_run_and_attempt(tmp_path: Path) -> None:
    baseline = tmp_path / "target-app"
    baseline.mkdir()
    (baseline / "source.py").write_text("original", encoding="utf-8")
    manager = WorkspaceManager(baseline, tmp_path / "engine-artifacts")
    first = manager.create_candidate("run-one", 1)
    second = manager.create_candidate("run-two", 1)
    (first / "source.py").write_text("first candidate", encoding="utf-8")
    assert (second / "source.py").read_text(encoding="utf-8") == "original"
    assert (baseline / "source.py").read_text(encoding="utf-8") == "original"
    assert first != second


def test_structured_replacement_generates_reviewable_diff() -> None:
    diff = replacement_diff(
        {"src/inventory_app/operations.py": "value = 1\n"},
        [FileReplacement(path="src/inventory_app/operations.py", content="value = 2\n")],
    )
    assert diff.startswith("diff --git a/src/inventory_app/operations.py")
    assert "-value = 1" in diff
    assert "+value = 2" in diff
    assert GUARD.validate(diff).accepted


def test_patch_apply_changes_nested_candidate_not_parent_repository(tmp_path: Path) -> None:
    parent = tmp_path / "repository"
    parent.mkdir()
    candidate = parent / "artifacts/workspaces/run/attempt-1/candidate"
    source = candidate / "src/inventory_app/operations.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    diff = replacement_diff(
        {"src/inventory_app/operations.py": "value = 1\n"},
        [FileReplacement(path="src/inventory_app/operations.py", content="value = 2\n")],
    )
    result = GUARD.apply(candidate, diff)
    assert result.accepted, result.errors
    assert source.read_text(encoding="utf-8") == "value = 2\n"


def test_structured_replacement_rejects_unsupplied_path() -> None:
    with pytest.raises(ValueError, match="not supplied editable source"):
        replacement_diff(
            {"src/inventory_app/operations.py": "value = 1\n"},
            [FileReplacement(path="tests/test_oracle.py", content="pass\n")],
        )


def test_non_applicable_generated_patch_fails_without_modifying_candidate(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    source = candidate / "src/inventory_app/operations.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    diff = """diff --git a/src/inventory_app/operations.py b/src/inventory_app/operations.py
--- a/src/inventory_app/operations.py
+++ b/src/inventory_app/operations.py
@@ -1 +1 @@
-value = 999
+value = 2
"""
    result = GUARD.apply(candidate, diff)
    assert not result.accepted
    assert any("git apply --check failed" in error for error in result.errors)
    assert source.read_text(encoding="utf-8") == "value = 1\n"
