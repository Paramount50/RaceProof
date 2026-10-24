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


def manager_for(tmp_path: Path) -> ExternalProjectManager:
    return ExternalProjectManager(
        tmp_path,
        Settings(
            _env_file=None,
            artifact_dir=tmp_path / "artifacts",
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
        data={
            "project_name": "API upload",
            "invariant_description": "At most one active booking exists per resource.",
        },
    )

    assert response.status_code == 201
    assert response.json()["onboarding_state"] == "INSPECTION_ONLY"
    assert response.json()["ready_for_experiment"] is False
    assert response.json()["invariant_description"] == (
        "At most one active booking exists per resource."
    )


def test_project_upload_api_requires_business_invariant(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("raceproof.api.project_manager", manager_for(tmp_path))
    response = TestClient(app).post(
        "/api/projects/upload",
        files={"files": ("operation.py", SUSPICIOUS_SOURCE, "text/x-python")},
    )

    assert response.status_code == 422
    assert "business invariant" in response.json()["detail"]


def test_remove_upload_deletes_only_owned_workspace_and_returns_to_examples(
    tmp_path: Path,
) -> None:
    manager = manager_for(tmp_path)
    unrelated = tmp_path / "artifacts" / "unrelated.txt"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("keep", encoding="utf-8")
    project = manager.import_uploads([("operation.py", SUSPICIOUS_SOURCE.encode())])
    workspace = Path(project.root_path)

    result = manager.remove(project.id)

    assert result["workspace_removed"] is True
    assert not workspace.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert manager.active() is None


def test_remove_refuses_project_with_active_run(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    project = manager.import_uploads([("operation.py", SUSPICIOUS_SOURCE.encode())])
    manager.mark_run_active(project.id, "a1b2c3d4e5f6")

    with pytest.raises(ExternalProjectError, match="active"):
        manager.remove(project.id)

    assert Path(project.root_path).is_dir()


def test_new_import_requires_removing_active_project_first(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    first = manager.import_uploads([("first.py", b"value = 1\n")])

    with pytest.raises(ExternalProjectError, match="remove the active"):
        manager.import_uploads([("second.py", b"value = 2\n")])

    assert manager.active() == first
    assert sorted(path.name for path in manager.upload_root.iterdir()) == [first.id]


def test_remove_project_api_clears_active_selection(tmp_path: Path, monkeypatch) -> None:
    manager = manager_for(tmp_path)
    project = manager.import_uploads([("operation.py", SUSPICIOUS_SOURCE.encode())])
    monkeypatch.setattr("raceproof.api.project_manager", manager)

    response = TestClient(app).delete(f"/api/projects/{project.id}")

    assert response.status_code == 200
    assert response.json()["active_project"] is None
    assert TestClient(app).get("/api/projects/active").json() is None
