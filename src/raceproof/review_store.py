from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from raceproof.contracts import REQUIRED_VERIFICATION_GATES, FinalStatus, RunReport
from raceproof.report_store import ReportStore

MAX_REVIEW_BYTES = 64 * 1024
SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ReviewDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ReviewSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ReviewDecision
    reviewer: str = Field(default="human-reviewer", min_length=1, max_length=200)
    comment: str | None = Field(default=None, max_length=4000)
    expected_report_sha256: str = Field(pattern=SHA256_PATTERN)
    expected_candidate_diff_sha256: str = Field(pattern=SHA256_PATTERN)

    @field_validator("reviewer")
    @classmethod
    def reviewer_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reviewer must not be blank")
        return stripped

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class ReviewRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, le=1)
    run_id: str
    decision: ReviewDecision
    reviewer: str
    comment: str | None = None
    decided_at: datetime
    report_sha256: str = Field(pattern=SHA256_PATTERN)
    candidate_diff_sha256: str = Field(pattern=SHA256_PATTERN)


class ReviewState(BaseModel):
    run_id: str
    deterministic_status: FinalStatus
    eligible: bool
    reason: str
    report_sha256: str
    candidate_diff_sha256: str | None
    review: ReviewRecord | None = None


class ReviewStoreError(RuntimeError):
    pass


class ReviewReportNotFound(ReviewStoreError):
    pass


class InvalidReviewArtifact(ReviewStoreError):
    pass


class ReviewIneligible(ReviewStoreError):
    pass


class ReviewHashMismatch(ReviewStoreError):
    pass


class ReviewAlreadyExists(ReviewStoreError):
    pass


class ReviewStorageError(ReviewStoreError):
    pass


class ReviewStore:
    def __init__(self, engine_root: Path, artifact_dir: Path) -> None:
        self.report_store = ReportStore(engine_root, artifact_dir)

    def review_path(self, run_id: str) -> Path:
        report_path = self.report_store.report_path(run_id)
        path = (report_path.parent / "review.json").resolve()
        if not path.is_relative_to(self.report_store.runs_root):
            raise InvalidReviewArtifact("review path escapes the configured artifact directory")
        return path

    def load_review(self, run_id: str) -> ReviewRecord | None:
        path = self.review_path(run_id)
        if not path.is_file():
            return None
        if path.stat().st_size > MAX_REVIEW_BYTES:
            raise InvalidReviewArtifact("review artifact exceeds the configured size limit")
        try:
            review = ReviewRecord.model_validate_json(path.read_bytes())
        except (OSError, ValidationError) as exc:
            raise InvalidReviewArtifact("review artifact failed validation") from exc
        if review.run_id != run_id:
            raise InvalidReviewArtifact("review artifact run ID does not match its directory")
        return review

    @staticmethod
    def _hashes(report: RunReport, raw_report: bytes) -> tuple[str, str | None]:
        report_hash = hashlib.sha256(raw_report).hexdigest()
        candidate_hash = (
            hashlib.sha256(report.final_diff.encode("utf-8")).hexdigest()
            if report.final_diff and report.final_diff.strip()
            else None
        )
        return report_hash, candidate_hash

    @staticmethod
    def _eligibility(report: RunReport) -> tuple[bool, str]:
        if report.status != FinalStatus.VERIFIED_CANDIDATE:
            return False, "human review requires a persisted VERIFIED_CANDIDATE"
        if not report.final_diff or not report.final_diff.strip():
            return False, "human review requires a non-empty candidate diff"
        for attempt in report.attempts:
            verification = attempt.verification
            if (
                attempt.patch_validation is not None
                and attempt.patch_validation.accepted
                and verification is not None
                and verification.passed
                and REQUIRED_VERIFICATION_GATES.issubset(verification.gates)
                and all(verification.gates[name] for name in REQUIRED_VERIFICATION_GATES)
            ):
                return True, "candidate passed every required deterministic verification gate"
        return False, "human review requires every deterministic verification gate to pass"

    def state(self, run_id: str) -> ReviewState:
        loaded = self.report_store.load_with_bytes(run_id)
        if loaded is None:
            raise ReviewReportNotFound("persisted run report was not found")
        report, raw_report = loaded
        report_hash, candidate_hash = self._hashes(report, raw_report)
        eligible, reason = self._eligibility(report)
        review = self.load_review(run_id)
        if review is not None:
            if not hmac.compare_digest(review.report_sha256, report_hash) or (
                candidate_hash is None
                or not hmac.compare_digest(review.candidate_diff_sha256, candidate_hash)
            ):
                raise InvalidReviewArtifact(
                    "review artifact does not match the current persisted report"
                )
            eligible = False
            reason = "an immutable human review decision already exists"
        return ReviewState(
            run_id=run_id,
            deterministic_status=report.status,
            eligible=eligible,
            reason=reason,
            report_sha256=report_hash,
            candidate_diff_sha256=candidate_hash,
            review=review,
        )

    def create(self, run_id: str, submission: ReviewSubmission) -> ReviewRecord:
        state = self.state(run_id)
        if state.review is not None:
            raise ReviewAlreadyExists("an immutable human review decision already exists")
        if not state.eligible or state.candidate_diff_sha256 is None:
            raise ReviewIneligible(state.reason)
        if not hmac.compare_digest(submission.expected_report_sha256, state.report_sha256):
            raise ReviewHashMismatch("persisted report SHA-256 is stale")
        if not hmac.compare_digest(
            submission.expected_candidate_diff_sha256, state.candidate_diff_sha256
        ):
            raise ReviewHashMismatch("candidate diff SHA-256 is stale")

        review = ReviewRecord(
            run_id=run_id,
            decision=submission.decision,
            reviewer=submission.reviewer,
            comment=submission.comment,
            decided_at=datetime.now(UTC),
            report_sha256=state.report_sha256,
            candidate_diff_sha256=state.candidate_diff_sha256,
        )
        path = self.review_path(run_id)
        temporary = path.parent / f".review-{uuid.uuid4().hex}.tmp"
        payload = review.model_dump_json(indent=2).encode("utf-8")
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise ReviewAlreadyExists(
                    "an immutable human review decision already exists"
                ) from exc
            except OSError as exc:
                raise ReviewStorageError("review artifact could not be created atomically") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return review
