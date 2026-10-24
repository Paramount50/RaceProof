# RaceProof

RaceProof is an experimental platform for investigating, reproducing, repairing, and verifying PostgreSQL concurrency defects in Python applications. The engine is application-neutral. A target application supplies a validated contract and a narrow adapter that describe its operation, actors, checkpoints, reset behavior, approved invariant, verification schedules, and regression command.

RaceProof has exactly two LLM reasoning agents. The Investigation Agent interprets approved source and schema evidence, forms a structured hypothesis, and selects an approved experiment. The Repair Agent receives only a confirmed handoff artifact, analyzes the failure, and proposes a complete unified diff. Neither agent can define the invariant, edit the scheduler, choose its own editable paths, or promote a patch. PostgreSQL execution and deterministic gates remain the authority.

`VERIFIED_CANDIDATE` means only that a patch passed the named finite experiments. It is not a proof of race freedom and it still requires human review.

## Included target applications

The examples are real mini-applications outside the RaceProof package:

| Target | Defect | Approved invariant | Expected repair family |
| --- | --- | --- | --- |
| `inventory_oversell` | Two purchases consume the same observed unit | Initial stock equals final stock plus committed orders | Atomic conditional update |
| `booking_double_reservation` | Two customers reserve one exclusive resource | At most one booking exists per resource | Row lock or database constraint |

Both targets run through the same loader, scheduler, LangGraph workflow, verifier, API, and dashboard. Core modules under `src/raceproof` do not import either application.

## Target contract

Each target repository contains a `raceproof.json` and adapter:

```text
target-app/
  raceproof.json
  raceproof_adapter.py
  src/target_package/
  tests/
```

The validated contract declares:

- repository-relative Python, source, editable, and protected paths;
- an importable application operation;
- actor IDs and structured inputs;
- one developer-approved invariant;
- approved investigation schedules, including a deterministic reproduction and optional controls;
- exact and adapted verification schedules;
- allowed outcome counts, replay count, regression command, and performance bounds;
- known target-specific limitations.

The adapter implements four application-owned capabilities: reset disposable state, run one actor transaction, evaluate the approved invariant, and return schema DDL. Invalid paths, unknown actors, unapproved invariants, shell operators, and missing required files are rejected before execution.

See [`examples/inventory_app/raceproof.json`](examples/inventory_app/raceproof.json) and [`examples/booking_app/raceproof.json`](examples/booking_app/raceproof.json).

To add a target, copy one example outside `src/raceproof`, replace its domain package and tests, implement the four `TargetAdapter` methods, declare only repository-relative paths, add a serial negative control and a reproducible checkpoint schedule, and encode the business rule as an approved deterministic invariant. Run `raceproof reproduce --config <path>` before enabling either LLM agent. Registration in the bundled dashboard additionally requires placing the target under `examples/<target-id>`.

## Workflow

```text
load -> inspect -> Investigation Agent -> execute experiment -> observe
                       ^                                |
                       +---- unconfirmed, replan -------+
                                                        |
                                               confirmed handoff
                                                        v
 final report <- verify <- guarded apply <- Repair Agent
                    |                            ^
                    +------ failed gates --------+
                         bounded attempts
```

The agents use distinct LangChain prompts, structured outputs, and tools. Investigation tools are read-only plus controlled experiment selection. Repair tools expose the confirmed handoff, editable source, schema, patch policy, and prior verification feedback. LangGraph owns both bounded loops and passes a compact typed artifact between them instead of an open-ended conversation.

Each repair attempt starts from the unchanged target repository. A diff can touch only declared editable paths. It is checked with `git apply --check`, parsed, compiled, and executed in a disposable copy. If the original checkpoint no longer exists, the exact schedule is marked invalid or timed out and the verifier tries only a developer-declared adapted schedule. A missing checkpoint never becomes a pass.

## Requirements and setup

- Python 3.11 or newer
- Node.js 20 or newer
- Git
- PostgreSQL 15 or newer in an isolated disposable database
- A Gemini API key only for live investigation and repair generation

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,gemini]"
Copy-Item .env.example .env
docker compose up -d postgres
```

Open `W:\AgentOpsDemo\.env` and replace only the placeholder in this line:

```dotenv
GEMINI_API_KEY=placeholder
OPENROUTER_API_KEY=placeholder
```

Keep `.env` local. It is ignored by Git. Restart the backend after changing it, then run `raceproof model-status` for a no-network detection check and `raceproof validate-models` for the opt-in live capability check. Never paste a real key into source, frontend variables, logs, reports, or chat.

If Docker is unavailable on Windows and PostgreSQL 17 is installed:

```powershell
.\scripts\start_local_postgres.ps1
$env:RACEPROOF_DATABASE_URL='postgresql+psycopg://raceproof@127.0.0.1:55432/raceproof'
```

Never point RaceProof at a production database. Target adapters are explicitly authorized to reset their own tables.

## CLI

List bundled targets:

```powershell
raceproof targets
```

Reproduce either defect without an API key:

```powershell
raceproof reproduce --config examples/inventory_app/raceproof.json
raceproof reproduce --config examples/booking_app/raceproof.json
```

Inspect the configured roles without making a provider request:

```powershell
raceproof model-status
```

After placing the key in `.env`, validate both exact model IDs, tool calling, and structured output with an opt-in live request:

```powershell
raceproof validate-models --provider gemini
```

Run the bounded workflow:

```powershell
raceproof run --config examples/inventory_app/raceproof.json
```

`GOOGLE_API_KEY` is also accepted. Without a key, the autonomous workflow loads and inspects the target, then terminates honestly as `MODEL_UNAVAILABLE` before claiming LLM investigation. Deterministic reproduction remains available through `raceproof reproduce`.

The primary Investigation Agent uses `gemini-3.5-flash-lite`, and the primary Repair Agent uses `gemini-3.1-flash-lite`. On a confirmed Gemini provider quota or availability failure, the current operation walks an ordered OpenRouter free-model list. Each candidate must have an explicit `:free` ID and live catalog evidence for zero input and output pricing, tools, structured output or response formatting, and at least 32,768 context tokens. Paid models and unverified pricing fail closed. Catalog support is only eligibility evidence and does not prove that live tool calling or structured output will succeed.

An OpenRouter 429 with `limit_source=upstream_provider_shared_pool` skips that model immediately instead of retrying the same unavailable shared pool. Other transient retries honor `Retry-After` up to `RACEPROOF_MAX_RETRY_WAIT_SECONDS`, which defaults to three seconds. Model switches keep the same LangGraph operation, PostgreSQL evidence, patch state, and global usage ledger.

RaceProof local budgets are separate from provider quotas. Defaults are 14 total requests, 6 Investigation requests, 8 Repair requests, a 6-request Repair reserve, and 60,000 provider-reported tokens. The measured normal path is two model turns to load evidence and plan, two to interpret the PostgreSQL result, and two for the first repair proposal. The previous nine-tool Investigation interface could consume all five Investigation requests before observation. The consolidated evidence tools remove those repeated fetches. A provider switch continues the same global ledger and never bypasses `BUDGET_EXCEEDED`.

The Repair Agent receives one compact deterministic evidence message and makes one structured model request per proposal. Full execution histories and duplicated SQL traces are excluded. RaceProof records message count, character count, and approximate input tokens without logging source content. `RACEPROOF_MAX_REPAIR_CONTEXT_TOKENS` bounds the serialized repair evidence, while `RACEPROOF_MAX_REQUEST_INPUT_TOKENS` rejects an oversized request before provider dispatch. The agent prefers structured complete-file replacements for explicitly editable paths. RaceProof converts those replacements into a reviewable Git diff deterministically, rejects empty, contradictory, duplicate, or unchanged proposals, and then applies the existing protected-path, syntax, regression, schedule, invariant, and performance gates.

Dashboard stages use explicit `NOT_STARTED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `RETRYING`, and `SKIPPED` backend outcomes. Entering a later stage never implies that an earlier stage succeeded.

`RACEPROOF_MAX_MODEL_REQUESTS` must be at least `RACEPROOF_MAX_INVESTIGATION_REQUESTS + RACEPROOF_MIN_REPAIR_REQUEST_RESERVE`. Changing only the global limit does not change the per-agent limit. Process environment variables override `.env`. Restart FastAPI after changing either source, then use `raceproof model-status` to inspect the effective values and their source.

Validate OpenRouter pricing and capabilities without generating output:

```powershell
raceproof validate-openrouter-metadata
```

After configuring a key, run the opt-in live capability probe. Each unique configured free model is tested once, with no automatic retry:

```powershell
raceproof validate-models --provider openrouter
```

## API and dashboard

```powershell
uvicorn raceproof.api:app --reload
npm --prefix frontend install
npm --prefix frontend run dev
```

The dashboard fetches generic target metadata and sends only a selected `target_id` when starting a run.

- `GET /api/targets`: list validated registered targets.
- `GET /api/health`: report service and PostgreSQL status.
- `GET /api/model-status`: report model IDs, local budgets, fallback state, and only whether a key is configured.
- `POST /api/runs`: start `{ "target_id": "inventory_oversell" }`.
- `GET /api/runs/{run_id}`: status, events, and report.
- `GET /api/runs/{run_id}/events`: Server-Sent Events stream.
- `GET /api/runs/{run_id}/report`: completed structured evidence.

## Verification and tests

```powershell
python -m ruff check src tests examples
pytest -q -m "not integration"
pytest -q
npm --prefix frontend run build
```

The integration suite uses real PostgreSQL. It proves that both external applications reproduce distinct invariant failures through the same engine, candidate workspaces leave source repositories unchanged, missing checkpoints are not accepted, and a generated atomic-update test candidate passes all declared gates.

## Artifacts

Runs write to the engine artifact root:

- `artifacts/runs/<run_id>/state-history.jsonl`
- `artifacts/runs/<run_id>/report.json`
- `artifacts/runs/<run_id>/candidate.diff` only for a verified candidate
- `artifacts/runs/<run_id>/attempts/attempt-N/model-proposal.json`
- `artifacts/runs/<run_id>/attempts/attempt-N/generated.diff`
- `artifacts/runs/<run_id>/attempts/attempt-N/patch-validation.json`
- `artifacts/runs/<run_id>/attempts/attempt-N/applied.diff` only after guarded application
- `artifacts/runs/<run_id>/attempts/attempt-N/verification.json` after verification
- `artifacts/workspaces/<run_id>/attempt-N/candidate`
- `artifacts/workspaces/<run_id>/attempt-N/run`

The proposal artifact preserves the exact structured model output, including complete-file
replacements. `generated.diff` is the reviewable baseline comparison for those exact model-authored
contents. Description-only responses fail schema validation and never reach patch application.

## Honest limitations

- The current inspector handles synchronous Python and common SQLAlchemy read, branch, update, insert, and flush shapes.
- The two bundled targets demonstrate reuse, not universal concurrency analysis.
- Automated tests use structured model doubles and do not establish successful live Gemini reasoning.
- The live `validate-models` command is opt-in, consumes provider quota, and was not run unless a real key was configured.
- Verification covers configured finite schedules and outcomes, not all possible interleavings.
- Candidate code runs in a child process and disposable workspace, but not an OS sandbox. Use a restricted container or VM for untrusted generated code.
- The API run store and LangGraph checkpointer are in memory. JSONL and reports are durable, but active runs are not restartable.
- Patch filtering is defense in depth and cannot prove arbitrary generated code is benign.
- Performance results are local observations, not production capacity claims.

See [ARCHITECTURE.md](ARCHITECTURE.md) and [EXPERIMENTS.md](EXPERIMENTS.md) for boundaries and evidence.
