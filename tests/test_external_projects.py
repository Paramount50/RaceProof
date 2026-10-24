from __future__ import annotations

import stat
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from raceproof.api import app
from raceproof.config import Settings
from raceproof.external_projects import ExternalProjectError, ExternalProjectManager

SUSPICIOUS_SOURCE = """from sqlalchemy.orm import Session

def reserve(session: Session, item_id: int):
    item = session.get(Item, item_id)
    if item.stock > 0:
        item.stock -= 1
        session.commit()
"""


def manager_for(tmp_path: Path, allowed_root: Path | None = None) -> ExternalProjectManager:
    return ExternalProjectManager(
        tmp_path,
        Settings(
            _env_file=None,
            artifact_dir=tmp_path / "artifacts",
            external_project_roots=str(allowed_root) if allowed_root else "",
            external_upload_max_bytes=1024 * 1024,
            external_extracted_max_bytes=2 * 1024 * 1024,
            external_source_max_bytes=256 * 1024,
        ),
    )


def zip_payload(entries: dict[str, str]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


def test_local_project_requires_configured_authorized_root_and_only_scans(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    project_root = allowed / "demo"
    project_root.mkdir(parents=True)
    source = project_root / "operations.py"
    source.write_text("raise RuntimeError('must not execute')\n" + SUSPICIOUS_SOURCE, encoding="utf-8")
    source_before = source.read_bytes()
    ignored = project_root / ".venv"
    ignored.mkdir()
    (ignored / "ignored.py").write_text(SUSPICIOUS_SOURCE, encoding="utf-8")

    project = manager_for(tmp_path, allowed).connect_local(str(project_root))

    assert project.source == "local"
    assert project.supported_files == ["operations.py"]
    assert project.findings[0].function == "reserve"
    assert project.ready_for_experiment is False
    assert source.read_bytes() == source_before


def test_local_project_rejects_path_outside_authorized_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (outside / "app.py").write_text(SUSPICIOUS_SOURCE, encoding="utf-8")

    with pytest.raises(ExternalProjectError, match="outside"):
        manager_for(tmp_path, allowed).connect_local(str(outside))


def test_upload_imports_python_and_zip_into_isolated_workspace(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)

    project = manager.import_uploads(
        [
            ("standalone.py", b"value = 1\n"),
            ("project.zip", zip_payload({"src/operations.py": SUSPICIOUS_SOURCE})),
        ],
        "Uploaded demo",
    )

    assert project.source == "upload"
    assert project.name == "Uploaded demo"
    assert project.supported_files == ["src/operations.py", "standalone.py"]
    assert project.findings[0].function == "reserve"
    assert Path(project.root_path).is_relative_to(manager.upload_root)


@pytest.mark.parametrize("entry", ["../escape.py", "/absolute.py", "C:/drive.py"])
def test_upload_rejects_zip_path_escape(tmp_path: Path, entry: str) -> None:
    with pytest.raises(ExternalProjectError, match="escapes"):
        manager_for(tmp_path).import_uploads([("unsafe.zip", zip_payload({entry: "x = 1"}))])


def test_upload_rejects_zip_symlink(tmp_path: Path) -> None:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        entry = zipfile.ZipInfo("linked.py")
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(entry, "target.py")

    with pytest.raises(ExternalProjectError, match="symlink"):
        manager_for(tmp_path).import_uploads([("unsafe.zip", output.getvalue())])


def test_project_upload_api_returns_inspection_only_state(tmp_path: Path, monkeypatch) -> None:
    manager = manager_for(tmp_path)
    monkeypatch.setattr("raceproof.api.project_manager", manager)

    response = TestClient(app).post(
        "/api/projects/upload",
        files={"files": ("operation.py", SUSPICIOUS_SOURCE, "text/x-python")},
        data={"project_name": "API upload"},
    )

    assert response.status_code == 201
    assert response.json()["onboarding_state"] == "INSPECTION_ONLY"
    assert response.json()["ready_for_experiment"] is False
