# RaceProof Demo Guide

## Demo claim

RaceProof is a reusable, bounded concurrency investigation engine for synchronous Python, SQLAlchemy, and PostgreSQL targets. The demo proves reuse with two external applications. It does not claim universal race detection or formal race freedom.

## Five-minute path

1. Start the isolated PostgreSQL database.
2. Run `raceproof targets` to show that applications are discovered from validated contracts.
3. Reproduce inventory oversell with `raceproof reproduce --config examples/inventory_app/raceproof.json`.
4. Reproduce booking double reservation with `raceproof reproduce --config examples/booking_app/raceproof.json`.
5. Open the dashboard, switch targets, and show that operation, actors, invariant, evidence, and lanes change without frontend code changes.
6. If a Gemini key is configured, show the Investigation Agent hypothesis and handoff followed by the Repair Agent strategy. Otherwise, show honest `MODEL_UNAVAILABLE` behavior.
7. Run `raceproof model-status` before the demo. Run `raceproof validate-openrouter-metadata` for a no-generation free-tier check. Run `raceproof validate-models --provider gemini` or `--provider openrouter` only when consuming live quota is intended. Catalog capability declarations do not prove live inference.

## Evidence to emphasize

- Both defects execute on real PostgreSQL with independent actor sessions.
- The target application owns reset, transaction invocation, and invariant evaluation.
- The engine owns schedule execution, evidence capture, isolation, patch controls, retry limits, and status.
- A model proposal has no pass or fail authority.
- Missing checkpoints are schedule failures, not success.
- A candidate is copied, guarded, tested, benchmarked, and reported for review. The original target remains unchanged.
- The dashboard labels provider-reported tokens, unknown metadata, remaining local budgets, and any explicit fallback use.

## Architecture choice

Use exactly two reasoning agents. The Investigation Agent can read evidence and select experiments but cannot patch. The Repair Agent can inspect the confirmed handoff and author a diff but cannot approve it. LangGraph performs the handoff and retry routing without a supervisor LLM. PostgreSQL schedules and executable invariants remain the hard authority.

## Before presenting

```powershell
python -m ruff check src tests examples
python -m pytest -q
npm --prefix frontend run build
```

Do not present skipped PostgreSQL tests as passes. Do not call local benchmark numbers production capacity. Do not imply that `VERIFIED_CANDIDATE` authorizes automatic merge.
