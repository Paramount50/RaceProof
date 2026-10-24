from __future__ import annotations

import ast
import difflib
import os
import re
import shutil
import subprocess
from pathlib import Path

from raceproof.contracts import FileReplacement, PatchValidation

DIFF_PATH = re.compile(r"^(?:\+\+\+|---)\s+(?:a/|b/)?([^\t\r\n]+)", re.MULTILINE)
FORBIDDEN_ADDITIONS = (
    "raceproof.checkpoint",
    "RACEPROOF_SCHEDULE",
    "time.sleep(",
    "subprocess.",
    "os.system(",
    "requests.",
    "httpx.",
    "socket.",
)


class WorkspaceManager:
    def __init__(self, repository_root: Path, artifact_root: Path) -> None:
        self.repository_root = repository_root.resolve()
        self.artifact_root = artifact_root.resolve()

    def create_candidate(self, run_id: str, attempt: int) -> Path:
        candidate = (
            self.artifact_root / "workspaces" / run_id / f"attempt-{attempt}" / "candidate"
        )
        if candidate.exists():
            shutil.rmtree(candidate)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        ignore = shutil.ignore_patterns(
            ".git", ".raceproof", "artifacts", "node_modules", "dist", "__pycache__"
        )
        shutil.copytree(self.repository_root, candidate, ignore=ignore)
        return candidate

    def create_run_copy(self, run_id: str, candidate: Path, attempt: int) -> Path:
        run = self.artifact_root / "workspaces" / run_id / f"attempt-{attempt}" / "run"
        if run.exists():
            shutil.rmtree(run)
        shutil.copytree(
            candidate, run, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache")
        )
        return run


class PatchGuard:
    def __init__(
        self,
        editable_paths: set[str],
        protected_prefixes: tuple[str, ...],
        max_changed_lines: int = 200,
    ) -> None:
        self.editable_paths = {Path(path).as_posix() for path in editable_paths}
        self.protected_prefixes = protected_prefixes
        self.max_changed_lines = max_changed_lines

    def validate(self, diff: str) -> PatchValidation:
        errors: list[str] = []
        paths = sorted(
            {Path(match).as_posix() for match in DIFF_PATH.findall(diff) if match != "/dev/null"}
        )
        if not diff.startswith("diff --git "):
            errors.append("proposal is not a complete unified Git diff")
        if not paths:
            errors.append("diff does not identify a changed file")
        for path in paths:
            candidate = Path(path)
            if candidate.is_absolute() or ".." in candidate.parts:
                errors.append(f"unsafe path: {path}")
            if path not in self.editable_paths:
                errors.append(f"path is not editable: {path}")
            if path.startswith(self.protected_prefixes):
                errors.append(f"protected path: {path}")
        additions = [
            line[1:]
            for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        removals = [
            line[1:]
            for line in diff.splitlines()
            if line.startswith("-") and not line.startswith("---")
        ]
        if len(additions) + len(removals) > self.max_changed_lines:
            errors.append(f"diff exceeds {self.max_changed_lines} changed lines")
        added_text = "\n".join(additions)
        for token in FORBIDDEN_ADDITIONS:
            if token in added_text:
                errors.append(f"forbidden addition: {token}")
        return PatchValidation(accepted=not errors, errors=errors, changed_paths=paths)

    def apply(self, candidate: Path, diff: str) -> PatchValidation:
        validation = self.validate(diff)
        if not validation.accepted:
            return validation
        before = {
            path: (candidate / path).read_bytes()
            for path in validation.changed_paths
            if (candidate / path).is_file()
        }
        git_env = {
            **os.environ,
            "GIT_CEILING_DIRECTORIES": str(candidate.parent.resolve()),
        }
        check = subprocess.run(
            ["git", "apply", "--check", "--ignore-space-change", "-"],
            input=diff,
            text=True,
            capture_output=True,
            cwd=candidate,
            timeout=15,
            shell=False,
            check=False,
            env=git_env,
        )
        if check.returncode != 0:
            validation.accepted = False
            validation.errors.append(f"git apply --check failed: {check.stderr.strip()}")
            return validation
        applied = subprocess.run(
            ["git", "apply", "--ignore-space-change", "-"],
            input=diff,
            text=True,
            capture_output=True,
            cwd=candidate,
            timeout=15,
            shell=False,
            check=False,
            env=git_env,
        )
        if applied.returncode != 0:
            validation.accepted = False
            validation.errors.append(f"git apply failed: {applied.stderr.strip()}")
            return validation
        unchanged = [
            path
            for path, content in before.items()
            if (candidate / path).read_bytes() == content
        ]
        if unchanged:
            validation.accepted = False
            validation.errors.append(
                "git apply reported success but changed no content in: "
                + ", ".join(unchanged)
            )
            return validation
        for path in validation.changed_paths:
            if path.endswith(".py"):
                try:
                    ast.parse((candidate / path).read_text(encoding="utf-8"))
                except SyntaxError as exc:
                    validation.accepted = False
                    validation.errors.append(f"Python syntax error in {path}: {exc}")
        compile_result = subprocess.run(
            ["python", "-m", "compileall", "-q", "src"],
            capture_output=True,
            text=True,
            cwd=candidate,
            timeout=30,
            shell=False,
            check=False,
        )
        if compile_result.returncode != 0:
            validation.accepted = False
            validation.errors.append(f"compileall failed: {compile_result.stderr.strip()}")
        return validation


def canonical_diff(baseline: Path, candidate: Path, relative_paths: list[str]) -> str:
    chunks: list[str] = []
    for relative in sorted(relative_paths):
        before = (baseline / relative).read_text(encoding="utf-8").splitlines(keepends=True)
        after = (candidate / relative).read_text(encoding="utf-8").splitlines(keepends=True)
        if before == after:
            continue
        body = "".join(
            difflib.unified_diff(before, after, fromfile=f"a/{relative}", tofile=f"b/{relative}")
        )
        chunks.append(f"diff --git a/{relative} b/{relative}\n{body}")
    return "".join(chunks)


def replacement_diff(
    source_files: dict[str, str], replacements: list[FileReplacement]
) -> str:
    chunks: list[str] = []
    seen: set[str] = set()
    for replacement in replacements:
        relative = Path(replacement.path).as_posix()
        if relative in seen:
            raise ValueError(f"duplicate replacement path: {relative}")
        if relative not in source_files:
            raise ValueError(f"replacement path is not supplied editable source: {relative}")
        seen.add(relative)
        before = source_files[relative].splitlines(keepends=True)
        content = replacement.content
        if content and not content.endswith("\n"):
            content += "\n"
        after = content.splitlines(keepends=True)
        if before == after:
            continue
        body = "".join(
            difflib.unified_diff(before, after, fromfile=f"a/{relative}", tofile=f"b/{relative}")
        )
        chunks.append(f"diff --git a/{relative} b/{relative}\n{body}")
    return "".join(chunks)
