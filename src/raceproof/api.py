from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text

from raceproof.config import get_settings
from raceproof.database import create_database_engine
from raceproof.external_projects import (
    ExternalProject,
    ExternalProjectError,
    ExternalProjectManager,
)
from raceproof.report_store import InvalidReportArtifact, InvalidRunId, ReportStore
from raceproof.review_store import (
    InvalidReviewArtifact,
    ReviewAlreadyExists,
    ReviewHashMismatch,
    ReviewIneligible,
    ReviewReportNotFound,
    ReviewState,
    ReviewStorageError,
    ReviewStore,
    ReviewSubmission,
)
from raceproof.runtime import model_status
from raceproof.security import redact_sensitive
from raceproof.service import RunService
from raceproof.target import TargetConfigurationError, TargetRegistry


class StartRunRequest(BaseModel):
    target_id: str


class ConnectLocalProjectRequest(BaseModel):
    path: str
    authorized: Literal[True]
    name: str | None = None


repository_root = Path(__file__).resolve().parents[2]
service = RunService(repository_root)
registry = TargetRegistry(repository_root / "examples")
project_manager = ExternalProjectManager(repository_root, get_settings())
app = FastAPI(title="RaceProof API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _persisted_report(run_id: str):
    try:
        return ReportStore(repository_root, get_settings().artifact_dir).load(run_id)
    except (InvalidRunId, InvalidReportArtifact):
        return None


def _review_store() -> ReviewStore:
    return ReviewStore(repository_root, get_settings().artifact_dir)


def _raise_review_http_error(exc: Exception) -> None:
    if isinstance(exc, InvalidRunId):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if isinstance(exc, ReviewReportNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, (InvalidReportArtifact, InvalidReviewArtifact)):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, (ReviewAlreadyExists, ReviewHashMismatch, ReviewIneligible)):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ReviewStorageError):
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    raise exc


@app.get("/api/health")
def health() -> dict[str, object]:
    database = False
    error = None
    try:
        engine = create_database_engine(get_settings().database_url)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        engine.dispose()
        database = True
    except Exception as exc:  # noqa: BLE001 - health must report any driver failure
        error = redact_sensitive(f"{type(exc).__name__}: {exc}")
    return {"service": "ok", "postgresql": database, "database_error": error}


@app.get("/api/model-status")
def get_model_status() -> dict[str, object]:
    return model_status(get_settings())


@app.post("/api/runs", status_code=202)
def start_run(request: StartRunRequest) -> dict[str, str]:
    try:
        target = registry.get(request.target_id)
    except TargetConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    run_id = uuid.uuid4().hex[:12]
    service.start(run_id, target)
    return {"run_id": run_id, "status": "queued"}


@app.get("/api/targets")
def list_targets() -> list[dict[str, object]]:
    return [
        {
            "id": target.config.id,
            "name": target.config.name,
            "description": target.config.description,
            "operation": target.config.operation.callable,
            "actors": [actor.id for actor in target.config.actors],
            "invariant": target.config.invariant.model_dump(mode="json"),
        }
        for target in registry.list()
    ]


@app.get("/api/projects/active", response_model=ExternalProject | None)
def get_active_project() -> ExternalProject | None:
    return project_manager.active()


@app.post("/api/projects/local", response_model=ExternalProject, status_code=201)
def connect_local_project(request: ConnectLocalProjectRequest) -> ExternalProject:
    try:
        return project_manager.connect_local(request.path, request.name)
    except ExternalProjectError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/projects/upload", response_model=ExternalProject, status_code=201)
async def upload_project(
    files: Annotated[list[UploadFile], File()],
    project_name: Annotated[str | None, Form()] = None,
) -> ExternalProject:
    if len(files) > 50:
        raise HTTPException(status_code=422, detail="too many uploaded files")
    uploads: list[tuple[str, bytes]] = []
    total = 0
    for upload in files:
        remaining = project_manager.upload_max_bytes - total
        payload = await upload.read(remaining + 1)
        total += len(payload)
        if total > project_manager.upload_max_bytes:
            raise HTTPException(status_code=413, detail="upload exceeds the configured size limit")
        uploads.append((upload.filename or "upload", payload))
    try:
        return project_manager.import_uploads(uploads, project_name)
    except ExternalProjectError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, object]:
    active = service.get(run_id)
    if active is None:
        report = _persisted_report(run_id)
        if report is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {
            "run_id": report.run_id,
            "status": report.status.value,
            "events": [event.model_dump(mode="json") for event in report.events],
            "report": report.model_dump(mode="json"),
            "error": None,
            "usage": report.usage.model_dump(mode="json"),
            "source": "persisted",
        }
    return {
        "run_id": run_id,
        "status": active.status,
        "events": [event.model_dump(mode="json") for event in active.events],
        "report": active.report.model_dump(mode="json") if active.report else None,
        "error": active.error,
        "usage": active.usage_tracker.snapshot().model_dump(mode="json")
        if active.usage_tracker
        else None,
    }


@app.get("/api/runs/{run_id}/events")
def stream_events(run_id: str) -> StreamingResponse:
    active = service.get(run_id)
    if active is None:
        report = _persisted_report(run_id)
        if report is None:
            raise HTTPException(status_code=404, detail="run not found")

        def generate_persisted():
            for event in report.events:
                yield f"data: {event.model_dump_json()}\n\n"
            yield "event: done\ndata: {}\n\n"

        return StreamingResponse(generate_persisted(), media_type="text/event-stream")

    def generate():
        for existing in active.events:
            yield f"data: {existing.model_dump_json()}\n\n"
        while True:
            event = active.updates.get()
            if event is None:
                yield "event: done\ndata: {}\n\n"
                break
            yield f"data: {event.model_dump_json()}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/runs/{run_id}/report")
def get_report(run_id: str):
    active = service.get(run_id)
    if active is None:
        report = _persisted_report(run_id)
        if report is None:
            raise HTTPException(status_code=404, detail="run not found")
        return report
    if active.report is None:
        raise HTTPException(status_code=409, detail="report is not ready")
    return active.report


@app.get("/api/runs/{run_id}/review", response_model=ReviewState)
def get_review(run_id: str) -> ReviewState:
    try:
        return _review_store().state(run_id)
    except Exception as exc:  # noqa: BLE001 - translated into bounded API errors
        _raise_review_http_error(exc)
        raise AssertionError("unreachable")


@app.post("/api/runs/{run_id}/review", status_code=201)
def create_review(run_id: str, submission: ReviewSubmission):
    try:
        return _review_store().create(run_id, submission)
    except Exception as exc:  # noqa: BLE001 - translated into bounded API errors
        _raise_review_http_error(exc)
        raise AssertionError("unreachable")
