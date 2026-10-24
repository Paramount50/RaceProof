from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from raceproof.config import get_settings
from raceproof.database import create_database_engine
from raceproof.events import EventRecorder
from raceproof.model import GeminiInvestigationAgent, GeminiRepairAgent
from raceproof.model_validation import validate_model_capability
from raceproof.providers import validate_openrouter_free_model
from raceproof.runtime import build_usage_tracker, model_status
from raceproof.scheduler import ControlledScheduler
from raceproof.target import TargetRegistry, load_adapter, load_target_config
from raceproof.workflow import RaceProofRunner


def main() -> int:
    parser = argparse.ArgumentParser(prog="raceproof")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("targets", help="list bundled target applications")
    subparsers.add_parser("model-status", help="show safe local model configuration")
    validate = subparsers.add_parser(
        "validate-models", help="make opt-in live provider capability probes"
    )
    validate.add_argument("--provider", choices=["gemini", "openrouter", "all"], default="gemini")
    validate.add_argument(
        "--model",
        action="append",
        help="probe only this exact model ID; repeat to test multiple OpenRouter candidates",
    )
    metadata = subparsers.add_parser(
        "validate-openrouter-metadata",
        help="check live OpenRouter model capabilities and zero pricing without generation",
    )
    metadata.add_argument("--model")
    reproduce = subparsers.add_parser(
        "reproduce", help="run only the controlled PostgreSQL reproduction"
    )
    reproduce.add_argument("--config", type=Path, required=True)
    run = subparsers.add_parser("run", help="run the bounded LangGraph workflow")
    run.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    settings = get_settings()
    root = Path.cwd().resolve()
    if args.command == "targets":
        for target in TargetRegistry(root / "examples").list():
            print(f"{target.config.id}\t{target.config.name}\t{target.config.operation.callable}")
        return 0
    if args.command == "model-status":
        print(json.dumps(model_status(settings), indent=2))
        return 0
    if args.command == "validate-openrouter-metadata":
        models = (
            [args.model]
            if args.model
            else [
                *settings.openrouter_investigation_model_list,
                *settings.openrouter_repair_model_list,
            ]
        )
        try:
            results = [
                validate_openrouter_free_model(model).model_dump(mode="json")
                for model in dict.fromkeys(models)
            ]
        except Exception as exc:  # noqa: BLE001 - CLI returns configuration evidence
            print(json.dumps({"passed": False, "error": str(exc)}, indent=2))
            return 2
        print(json.dumps({"passed": True, "models": results}, indent=2))
        return 0
    if args.command == "validate-models":
        tracker = build_usage_tracker(settings)
        results = []
        if args.provider in {"gemini", "all"}:
            results.extend(
                [
                    validate_model_capability(
                        settings.resolved_investigation_model,
                        "investigation",
                        tracker,
                        settings.model_max_retries,
                        settings.gemini_api_key_value,
                        "gemini",
                    ),
                    validate_model_capability(
                        settings.resolved_repair_model,
                        "repair",
                        tracker,
                        settings.model_max_retries,
                        settings.gemini_api_key_value,
                        "gemini",
                    ),
                ]
            )
        if args.provider in {"openrouter", "all"}:
            candidates = args.model or list(
                dict.fromkeys(
                    settings.openrouter_investigation_model_list
                    + settings.openrouter_repair_model_list
                )
            )
            for index, model in enumerate(candidates):
                results.append(
                    validate_model_capability(
                        model,
                        "investigation" if index % 2 == 0 else "repair",
                        tracker,
                        0,
                        settings.openrouter_api_key_value,
                        "openrouter",
                    )
                )
        print(
            json.dumps(
                {
                    "results": [item.model_dump(mode="json") for item in results],
                    "usage": tracker.snapshot().model_dump(mode="json"),
                },
                indent=2,
            )
        )
        return 0 if all(item.passed for item in results) else 2
    target = load_target_config(args.config)
    if args.command == "reproduce":
        recorder = EventRecorder()
        database_url = os.environ.get(target.config.database_url_env, settings.database_url)
        engine = create_database_engine(database_url, recorder)
        try:
            adapter = load_adapter(target)
            result = ControlledScheduler(
                engine, recorder, settings.schedule_timeout_seconds
            ).execute(
                adapter,
                target.config.actors,
                target.config.reproduction_schedule,
                exact=True,
            )
        finally:
            engine.dispose()
        print(result.model_dump_json(indent=2))
        return 0 if result.invariant and not result.invariant.passed else 2
    tracker = build_usage_tracker(settings)
    common = {
        "usage_tracker": tracker,
        "fallback_model": settings.fallback_model,
        "fallback_enabled": settings.enable_model_fallback,
        "max_retries": settings.model_max_retries,
        "api_key": settings.gemini_api_key_value,
        "openrouter_api_key": settings.openrouter_api_key_value,
        "auto_fallback": settings.auto_fallback,
        "max_retry_wait_seconds": settings.max_retry_wait_seconds,
        "max_fallback_duration_seconds": settings.max_fallback_duration_seconds,
    }
    investigation_agent = GeminiInvestigationAgent(
        settings.resolved_investigation_model,
        openrouter_models=settings.openrouter_investigation_model_list,
        **common,
    )
    repair_agent = GeminiRepairAgent(
        settings.resolved_repair_model,
        openrouter_models=settings.openrouter_repair_model_list,
        max_repair_context_tokens=settings.max_repair_context_tokens,
        **common,
    )
    report = RaceProofRunner(
        root, target, settings, investigation_agent, repair_agent, usage_tracker=tracker
    ).run()
    print(report.model_dump_json(indent=2))
    return (
        0 if report.status.value in {"REPRODUCED", "VERIFIED_CANDIDATE", "MODEL_UNAVAILABLE"} else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
