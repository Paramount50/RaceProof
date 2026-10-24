from __future__ import annotations

import os
import shutil
import stat
import threading
import uuid
import zipfile
from enum import StrEnum
from io import BytesIO
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field

from raceproof.config import Settings
from raceproof.contracts import StaticFinding
from raceproof.inspection import inspect_python_file

IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".raceproof",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "artifacts",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "venv",
    }
)


class ProjectSource(StrEnum):
    UPLOAD = "upload"


class ExternalProject(BaseModel):
    id: str
    name: str
    invariant_description: str | None = None
    source: ProjectSource
    root_path: str
    files_discovered: int
    supported_files: list[str]
    findings: list[StaticFinding]
    warnings: list[str] = Field(default_factory=list)
    sqlalchemy_detected: bool = False
    postgresql_configuration_detected: bool = False
    onboarding_state: str = "INSPECTION_ONLY"
    ready_for_experiment: bool = False
    missing_requirements: list[str] = Field(default_factory=list)


class ExternalProjectError(ValueError):
    pass


class ProjectNotFound(ExternalProjectError):
    pass


class ProjectBusy(ExternalProjectError):
    pass


class ProjectCleanupError(ExternalProjectError):
    pass


class ExternalProjectManager:
    def __init__(self, engine_root: Path, settings: Settings) -> None:
        self.engine_root = engine_root.resolve()
        artifact_root = (
            settings.artifact_dir
            if settings.artifact_dir.is_absolute()
            else self.engine_root / settings.artifact_dir
        )
        self.upload_root = (artifact_root / "imports").resolve()
        self.upload_max_bytes = settings.external_upload_max_bytes
        self.extracted_max_bytes = settings.external_extracted_max_bytes
        self.max_files = settings.external_max_files
        self.source_max_bytes = settings.external_source_max_bytes
        self._projects: dict[str, ExternalProject] = {}
        self._active_project_id: str | None = None
        self._active_runs: dict[str, set[str]] = {}
        self._lock = threading.RLock()

    def active(self) -> ExternalProject | None:
        with self._lock:
            if self._active_project_id is None:
                return None
            return self._projects.get(self._active_project_id)

    def get(self, project_id: str) -> ExternalProject:
        with self._lock:
            project = self._projects.get(project_id)
        if project is None:
            raise ProjectNotFound("external project was not found")
        return project

    def mark_run_active(self, project_id: str, run_id: str) -> None:
        self.get(project_id)
        with self._lock:
            self._active_runs.setdefault(project_id, set()).add(run_id)

    def mark_run_finished(self, project_id: str, run_id: str) -> None:
        with self._lock:
            self._active_runs.get(project_id, set()).discard(run_id)

    def remove(self, project_id: str) -> dict[str, object]:
        with self._lock:
            project = self._projects.get(project_id)
            if project is None:
                raise ProjectNotFound("external project was not found")
            active_runs = self._active_runs.get(project_id, set())
            if active_runs:
                raise ProjectBusy("project cannot be removed while an experiment is active")
            workspace = Path(project.root_path).resolve()
            if workspace.name != project.id:
                raise ProjectCleanupError("uploaded workspace identity is invalid")
            try:
                self._remove_owned_workspace(workspace)
            except OSError as exc:
                raise ProjectCleanupError("uploaded workspace cleanup failed") from exc
            self._projects.pop(project_id, None)
            self._active_runs.pop(project_id, None)
            if self._active_project_id == project_id:
                self._active_project_id = None
            return {
                "removed_project_id": project_id,
                "source": project.source.value,
                "workspace_removed": True,
                "active_project": None,
            }

    def import_uploads(
        self,
        uploads: list[tuple[str, bytes]],
        name: str | None = None,
        invariant_description: str | None = None,
    ) -> ExternalProject:
        if not uploads:
            raise ExternalProjectError("at least one Python file or ZIP archive is required")
        total_upload = sum(len(payload) for _, payload in uploads)
        if total_upload > self.upload_max_bytes:
            raise ExternalProjectError("upload exceeds the configured compressed-size limit")
        project_id = uuid.uuid4().hex[:12]
        self.upload_root.mkdir(parents=True, exist_ok=True)
        temporary = (self.upload_root / f".{project_id}.tmp").resolve()
        destination = (self.upload_root / project_id).resolve()
        self._require_upload_path(temporary)
        self._require_upload_path(destination)
        temporary.mkdir()
        written: set[str] = set()
        published = False
        try:
            for filename, payload in uploads:
                suffix = Path(filename).suffix.lower()
                if suffix == ".py":
                    safe_name = self._safe_upload_filename(filename)
                    self._write_uploaded_source(temporary, safe_name, payload, written)
                elif suffix == ".zip":
                    self._extract_zip(temporary, payload, written)
                else:
                    raise ExternalProjectError("only .py files and ZIP archives are supported")
            if not written:
                raise ExternalProjectError("upload did not contain supported Python source files")
            temporary.rename(destination)
            published = True
            project = self._inspect(
                destination,
                ProjectSource.UPLOAD,
                (name or Path(uploads[0][0]).stem or "uploaded-project").strip(),
                project_id=project_id,
                invariant_description=invariant_description,
            )
            self._activate(project)
        except Exception:
            self._remove_owned_workspace(temporary)
            if published:
                self._remove_owned_workspace(destination)
            raise
        return project

    def _extract_zip(self, root: Path, payload: bytes, written: set[str]) -> None:
        try:
            archive = zipfile.ZipFile(BytesIO(payload))
        except zipfile.BadZipFile as exc:
            raise ExternalProjectError("uploaded ZIP archive is invalid") from exc
        total_extracted = 0
        with archive:
            infos = archive.infolist()
            if len(infos) > self.max_files:
                raise ExternalProjectError("ZIP archive contains too many entries")
            for info in infos:
                relative = self._safe_archive_path(info)
                if relative is None or info.is_dir():
                    continue
                if relative.suffix.lower() != ".py":
                    continue
                total_extracted += info.file_size
                if total_extracted > self.extracted_max_bytes:
                    raise ExternalProjectError("ZIP archive exceeds the extracted-size limit")
                if info.file_size > self.source_max_bytes:
                    raise ExternalProjectError("ZIP archive contains an oversized Python file")
                if info.flag_bits & 0x1:
                    raise ExternalProjectError("encrypted ZIP entries are not supported")
                if info.file_size > 1024 * 1024 and info.file_size > max(info.compress_size, 1) * 100:
                    raise ExternalProjectError("ZIP archive contains a suspicious compression ratio")
                data = archive.read(info)
                self._write_uploaded_source(root, relative.as_posix(), data, written)

    @staticmethod
    def _safe_archive_path(info: zipfile.ZipInfo) -> PurePosixPath | None:
        if "\\" in info.filename or "\x00" in info.filename:
            raise ExternalProjectError("ZIP entry contains an unsafe path")
        relative = PurePosixPath(info.filename)
        if relative.is_absolute() or ".." in relative.parts or any(
            ":" in part for part in relative.parts
        ):
            raise ExternalProjectError("ZIP entry escapes the import workspace")
        if any(part.lower() in IGNORED_DIRECTORIES for part in relative.parts):
            return None
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise ExternalProjectError("ZIP symlink and special-file entries are not supported")
        return relative

    def _write_uploaded_source(
        self, root: Path, relative_value: str, payload: bytes, written: set[str]
    ) -> None:
        if len(payload) > self.source_max_bytes:
            raise ExternalProjectError("uploaded Python file exceeds the per-file size limit")
        relative = PurePosixPath(relative_value)
        normalized = relative.as_posix()
        if normalized in written:
            raise ExternalProjectError("upload contains duplicate Python file paths")
        destination = (root / Path(*relative.parts)).resolve()
        if not destination.is_relative_to(root):
            raise ExternalProjectError("uploaded file escapes the import workspace")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        written.add(normalized)

    @staticmethod
    def _safe_upload_filename(filename: str) -> str:
        normalized = filename.replace("\\", "/")
        relative = PurePosixPath(normalized)
        if relative.name != normalized or relative.suffix.lower() != ".py":
            raise ExternalProjectError("uploaded Python filename must not contain a path")
        return relative.name

    def _inspect(
        self,
        root: Path,
        source: ProjectSource,
        name: str,
        *,
        project_id: str | None = None,
        invariant_description: str | None = None,
    ) -> ExternalProject:
        supported: list[str] = []
        findings: list[StaticFinding] = []
        warnings: list[str] = []
        discovered = 0
        total_source_bytes = 0
        sqlalchemy_detected = False
        postgres_detected = False
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            directories[:] = sorted(
                directory
                for directory in directories
                if directory.lower() not in IGNORED_DIRECTORIES
                and not (current_path / directory).is_symlink()
            )
            for filename in sorted(files):
                path = current_path / filename
                if path.is_symlink():
                    warnings.append(f"Skipped symbolic link: {path.relative_to(root).as_posix()}")
                    continue
                resolved = path.resolve()
                if not resolved.is_relative_to(root):
                    warnings.append(f"Skipped path outside project root: {filename}")
                    continue
                discovered += 1
                if discovered > self.max_files:
                    raise ExternalProjectError("project contains too many files to inspect safely")
                if path.suffix.lower() != ".py":
                    continue
                size = path.stat().st_size
                if size > self.source_max_bytes:
                    warnings.append(f"Skipped oversized Python file: {path.relative_to(root).as_posix()}")
                    continue
                total_source_bytes += size
                if total_source_bytes > self.extracted_max_bytes:
                    raise ExternalProjectError("Python source exceeds the configured inspection limit")
                relative = path.relative_to(root).as_posix()
                try:
                    source_text = path.read_text(encoding="utf-8")
                    file_findings = inspect_python_file(path, root)
                except (OSError, UnicodeError, SyntaxError) as exc:
                    warnings.append(f"Skipped {relative}: {type(exc).__name__}")
                    continue
                supported.append(relative)
                findings.extend(file_findings)
                lowered = source_text.lower()
                sqlalchemy_detected |= "sqlalchemy" in lowered
                postgres_detected |= any(
                    marker in lowered
                    for marker in ("postgresql", "database_url", "create_engine")
                )
        if not supported:
            raise ExternalProjectError("project does not contain readable Python source files")
        return ExternalProject(
            id=project_id or uuid.uuid4().hex[:12],
            name=name[:120] or "external-project",
            invariant_description=invariant_description,
            source=source,
            root_path=str(root),
            files_discovered=discovered,
            supported_files=sorted(supported),
            findings=sorted(findings, key=lambda item: (item.file, item.line_start)),
            warnings=warnings,
            sqlalchemy_detected=sqlalchemy_detected,
            postgresql_configuration_detected=postgres_detected,
            missing_requirements=[
                "Select a suspicious operation.",
                "Provide and validate a target adapter.",
                "Approve actor inputs, a business invariant, and a schedule.",
                "Authorize a separate disposable PostgreSQL experiment database.",
            ],
        )

    def _activate(self, project: ExternalProject) -> None:
        with self._lock:
            if self._active_project_id is not None:
                raise ExternalProjectError(
                    "remove the active external project before selecting another one"
                )
            self._projects[project.id] = project
            self._active_project_id = project.id

    def _require_upload_path(self, path: Path) -> None:
        if path == self.upload_root or not path.is_relative_to(self.upload_root):
            raise ExternalProjectError("upload workspace path escaped its configured root")

    def _remove_owned_workspace(self, path: Path) -> None:
        resolved = path.resolve()
        self._require_upload_path(resolved)
        if resolved.exists():
            shutil.rmtree(resolved)
