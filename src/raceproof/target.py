from __future__ import annotations

import importlib.util
import json
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import Engine

from raceproof.contracts import ActorSpec, InvariantResult, TargetConfig
from raceproof.events import EventRecorder

Checkpoint = Callable[[str, str], None]


class TargetConfigurationError(ValueError):
    pass


class TargetAdapter(ABC):
    """Application-owned bridge used by the deterministic RaceProof engine."""

    def __init__(self, repository_root: Path, config: TargetConfig) -> None:
        self.repository_root = repository_root
        self.config = config

    @abstractmethod
    def reset(self, engine: Engine) -> None:
        """Reset only the disposable target schema and seed known state."""

    @abstractmethod
    def run_actor(
        self,
        engine: Engine,
        actor: ActorSpec,
        checkpoint: Checkpoint,
        recorder: EventRecorder,
    ) -> str:
        """Execute one complete transaction and return a declared outcome label."""

    @abstractmethod
    def evaluate_invariant(self, engine: Engine) -> InvariantResult:
        """Evaluate the developer-approved invariant without model judgment."""

    @abstractmethod
    def schema_ddl(self) -> str:
        """Return schema evidence supplied to both reasoning agents."""


@dataclass(frozen=True)
class LoadedTarget:
    config: TargetConfig
    config_path: Path
    repository_root: Path

    @property
    def config_relative_path(self) -> str:
        return self.config_path.relative_to(self.repository_root).as_posix()


def _safe_relative_path(value: str, label: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise TargetConfigurationError(f"{label} must be a safe repository-relative path: {value}")
    return path


def load_target_config(
    config_path: Path,
    *,
    allowed_roots: list[Path] | None = None,
) -> LoadedTarget:
    resolved_config = config_path.resolve()
    if not resolved_config.is_file():
        raise TargetConfigurationError(f"target config does not exist: {resolved_config}")
    try:
        raw = json.loads(resolved_config.read_text(encoding="utf-8"))
        config = TargetConfig.model_validate(raw)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise TargetConfigurationError(f"invalid target config {resolved_config}: {exc}") from exc

    repository_value = _safe_relative_path(config.repository, "repository")
    repository_root = (resolved_config.parent / repository_value).resolve()
    if not repository_root.is_dir():
        raise TargetConfigurationError(f"target repository does not exist: {repository_root}")
    if not resolved_config.is_relative_to(repository_root):
        raise TargetConfigurationError("target config must be stored inside its repository")
    if allowed_roots:
        roots = [root.resolve() for root in allowed_roots]
        if not any(
            repository_root == root or repository_root.is_relative_to(root) for root in roots
        ):
            raise TargetConfigurationError("target repository is outside the approved roots")

    checked_paths = [
        config.adapter_path,
        *config.python_paths,
        *config.operation.source_files,
        *config.operation.editable_paths,
        *config.operation.protected_paths,
        config.verification.regression.cwd,
    ]
    for value in checked_paths:
        relative = _safe_relative_path(value, "configured path")
        resolved = (repository_root / relative).resolve()
        if not resolved.is_relative_to(repository_root):
            raise TargetConfigurationError(f"configured path escapes repository: {value}")
    for required in [config.adapter_path, *config.operation.source_files]:
        if not (repository_root / required).is_file():
            raise TargetConfigurationError(f"required target file does not exist: {required}")
    for editable in config.operation.editable_paths:
        if not (repository_root / editable).is_file():
            raise TargetConfigurationError(f"editable target file does not exist: {editable}")

    return LoadedTarget(config=config, config_path=resolved_config, repository_root=repository_root)


def load_adapter(target: LoadedTarget) -> TargetAdapter:
    adapter_file = target.repository_root / target.config.adapter_path
    source_paths = [target.repository_root / path for path in target.config.python_paths]
    added = [str(target.repository_root), *(str(path) for path in source_paths)]
    module_name = "raceproof_target_" + sha256(str(adapter_file).encode()).hexdigest()[:16]
    original_path = list(sys.path)
    try:
        sys.path[:0] = added
        spec = importlib.util.spec_from_file_location(module_name, adapter_file)
        if spec is None or spec.loader is None:
            raise TargetConfigurationError(f"cannot load adapter module: {adapter_file}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        factory = getattr(module, target.config.adapter_factory, None)
        if not callable(factory):
            raise TargetConfigurationError(
                f"adapter factory {target.config.adapter_factory!r} is missing or not callable"
            )
        adapter = factory(target.repository_root, target.config)
        if not isinstance(adapter, TargetAdapter):
            raise TargetConfigurationError("adapter factory must return a TargetAdapter instance")
        return adapter
    finally:
        sys.path[:] = original_path


class TargetRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def list(self) -> list[LoadedTarget]:
        targets = [
            load_target_config(path, allowed_roots=[self.root])
            for path in sorted(self.root.glob("*/raceproof.json"))
        ]
        ids = [target.config.id for target in targets]
        if len(ids) != len(set(ids)):
            raise TargetConfigurationError("target registry contains duplicate IDs")
        return targets

    def get(self, target_id: str) -> LoadedTarget:
        for target in self.list():
            if target.config.id == target_id:
                return target
        raise TargetConfigurationError(f"unknown target: {target_id}")
