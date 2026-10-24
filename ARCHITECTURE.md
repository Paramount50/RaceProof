# RaceProof Architecture

## Boundary and trust model

RaceProof is an engine plus a validated application contract. Domain behavior stays with the target application.

| Capability | Owner | Pass or fail authority |
| --- | --- | --- |
| Hypothesis formation and experiment selection | Investigation Agent plus static evidence | None |
| Candidate diagnosis and source authorship | Repair Agent | None |
| Target operation and state reset | Application adapter | Execution only |
| Checkpoint schedule | Validated target contract and deterministic engine | Yes |
| Business invariant | Developer-approved application adapter | Yes |
| Patch paths and syntax | Deterministic guard | Yes |
| Regression and performance gates | Declared command and measured execution | Yes |
| Candidate promotion | Aggregate gates plus human review | Yes |

The engine reports `SUSPECTED`, `REPRODUCED`, `VERIFIED_CANDIDATE`, `UNRESOLVED`, `MODEL_UNAVAILABLE`, `UNVERIFIABLE_SCHEDULE`, or `EXECUTION_ERROR`.

## Package structure

```text
src/raceproof/
  contracts.py       validated engine and target schemas
  target.py          safe loader, registry, and adapter interface
  inspection.py      bounded generic Python AST signals
  scheduler.py       declarative actor and checkpoint executor
  database.py        SQLAlchemy engine, sessions, SQL attribution
  workflow.py        bounded LangGraph control flow
  patching.py        isolated workspaces and diff containment
  verification.py    child-process boundary
  verify_worker.py   deterministic candidate gates
  api.py             generic target and run endpoints

examples/
  inventory_app/     application, adapter, contract, tests
  booking_app/       application, adapter, contract, tests
```

No module under `src/raceproof` imports either example's models or operation.

## Validated target loading

`TargetConfig` is a versioned Pydantic schema. It requires at least two unique actors, an explicitly approved invariant, a reproduction schedule, optional investigation controls, one or more verification schedules, allowed outcomes, and a shell-free regression command. Every schedule actor must exist.

`load_target_config` resolves the repository and checks all declared paths. Absolute paths and `..` traversal are rejected. Required adapters, source files, and editable files must exist. API-discovered targets must remain under the configured examples registry. CLI users explicitly select a config.

`load_adapter` imports a single application-owned bridge through a unique module name. The bridge must implement `TargetAdapter`. It receives the resolved repository and immutable validated config.

## Application adapter

The adapter has four responsibilities:

1. Reset only its disposable schema and seed deterministic state.
2. Execute one actor operation in an independent SQLAlchemy transaction.
3. Evaluate the named approved invariant and return generic evidence.
4. Provide schema DDL for the failure bundle.

The contract supplies actor inputs, so the engine never assumes product IDs, resource IDs, order tables, stock fields, or result column names.

## Scheduler semantics

`ControlledScheduler.execute` derives gates from `ScheduleSpec` actions:

- `wait_checkpoint`
- `release`
- `wait_completion`
- `release_all`

Actor threads call the adapter with a checkpoint callback. Each actor owns a separate transaction and session. The engine records checkpoint, transaction, SQL, invariant, and error events.

The exact reproduction schedules for both examples hold A and B after their application read, release A through commit, then release B. This produces stale write behavior for inventory and a double insert for booking.

If a candidate removes or relocates a checkpoint, the original schedule becomes `INVALID` or `TIMED_OUT`. Verification may then try the next schedule only if that schedule was already declared by the developer. Current targets declare an adapted parallel-start schedule at the complete-operation boundary. No source classifier silently invents a schedule.

## Static inspection

`inspection.py` searches Python functions for generic concurrency-risk signals such as row reads, check-then-act branches, writes derived from observations, dependent inserts, and transactional flushes. A high-signal match produces only `SUSPECTED`. Runtime PostgreSQL execution and an invariant failure are required for `REPRODUCED`.

## Investigation Agent

The Investigation Agent is a LangChain tool-calling agent with its own system prompt and `InvestigationDecision` structured output. Its tools can read approved source, inspect schema DDL, list transaction boundaries, list synchronization checkpoints, list approved experiments, and select one controlled experiment. It cannot read arbitrary files, generate a patch, or claim execution results.

LangGraph executes the selected schedule outside the agent. If the invariant does not fail, the exact `ScheduleResult` is appended to the next `InvestigationContext`. The agent can revise its hypothesis until `max_investigation_attempts` is reached. Failure to confirm ends as `UNCONFIRMED`.

## Structured handoff

A confirmed PostgreSQL invariant violation produces `InvestigationArtifact`. It contains the target operation, relevant source spans, reproduced schedule, actual SQL events, violated invariant, final evidence state, hypothesized root cause, and evidence references. The Repair Agent receives this bounded artifact, not the Investigation Agent's message history.

If the Repair Agent identifies a specific evidence gap, `PatchProposal` can carry `request_additional_investigation` plus a bounded evidence request. LangGraph routes back to the Investigation Agent only while its independent attempt budget remains. The request becomes tool-readable context; it does not create an unstructured agent conversation.

## Repair Agent and Gemini

The two agents have separate model identities and one shared usage ledger. Every underlying chat-model request, including internal tool loops, is counted through a LangChain callback. The ledger enforces global and per-agent request limits before dispatch and records provider-reported input and output tokens. Provider retries are bounded and exponential. Authentication, unavailable model, rate, daily quota, structured output, and local budget failures remain distinct terminal evidence.

Gemini is primary and OpenRouter is the only automatic provider fallback. Each agent has an ordered list of explicit free OpenRouter models. A confirmed upstream shared-pool rate limit advances immediately to the next catalog-verified model. Other transient failures use bounded retries and honor `Retry-After` only up to the configured hackathon wait cap. The structured workflow state, completed PostgreSQL experiments, patch attempts, and global usage ledger stay unchanged. Authentication errors, local request limits, local token limits, PostgreSQL errors, and verification failures never trigger provider fallback. Invalid or paid model entries are rejected. LangGraph remains the sole orchestrator; fallback does not introduce another agent.

The Investigation Agent exposes one consolidated planning tool and one latest-experiment observation tool. A normal tool loop therefore uses two provider requests for planning and two for post-experiment interpretation. The Repair Agent exposes one consolidated bounded workspace tool and normally uses two provider requests per proposal. Every internal request is recorded with agent, operation, provider, model, retry index, status, and provider-reported tokens.

The Repair Agent is a second LangChain tool-calling agent with a different system prompt, tools, and `PatchProposal` output. After reproduction, it receives:

- target ID and importable operation;
- configured source files and schema DDL;
- static findings;
- the exact schedule result, SQL-attributed events, actor outcomes, and invariant evidence;
- the approved invariant description and allowed outcome counts;
- editable and protected paths;
- prior failed patch and verification attempts.

Its tools read the handoff, editable source, schema, patch policy, and verification feedback. They do not expose arbitrary files or pass authority. `PatchProposal` contains diagnosis, strategy, assumptions, complete unified diff, expected SQL shape, optional schedule hint, residual risks, and `cannot_repair`.

## Patch containment and isolation

```text
target repository baseline, unchanged
  engine artifacts/workspaces/attempt-N/candidate
  engine artifacts/workspaces/attempt-N/run
```

Every attempt starts from a fresh baseline copy. `PatchGuard` enforces the target's editable allowlist and protected prefixes, rejects traversal and selected dangerous additions, caps changed lines, runs `git apply --check`, parses Python, and compiles the target `src` tree. Verification runs against a second disposable copy in a child process. The final diff is recomputed from baseline and candidate.

## Verification gates

For each replay, the worker attempts configured schedules in order. It selects the first schedule that completes. A completed exact replay that still violates the invariant is a failed candidate and does not fall through to an easier schedule.

All named gates must pass:

- a declared schedule completes for every replay;
- every approved invariant passes;
- actor outcomes equal the configured multiset;
- the target-owned regression command passes;
- the adapted benchmark observes no transaction errors or deadlocks;
- candidate p95 stays within the configured local multiplier when a baseline exists.

The worker is generic. It loads the copied contract and adapter and has no domain-specific source classification.

## LangGraph and retry behavior

```text
START -> load -> inspect -> Investigation Agent -> execute -> observe
                            ^                            |
                            +------- not confirmed -----+
                                                         |
                                                confirmed handoff
                                                         v
                                    Repair Agent -> apply -> verify
                                         ^                    |
                                         +---- failed gates ---+
```

Conditional edges enforce a confirmed failure before handoff, patch acceptance before verification, success before promotion, and separate investigation and repair attempt caps. Every node appends JSONL state evidence. Failure after a cap ends as `UNCONFIRMED`, `UNRESOLVED`, or `UNVERIFIABLE_SCHEDULE` with exact evidence.

## Why exactly two agents

The previous prototype had only one Repair Agent after deterministic suspicion and reproduction. The new architecture makes hypothesis formation and experiment selection genuinely LLM-powered while preserving a hard evidence boundary. Investigation and repair require different context, tools, and outputs, so separating them reduces permissions and context growth. A third supervisor agent is unnecessary because LangGraph routes typed state and deterministic results directly.

## API and UI

FastAPI exposes registered target metadata and accepts `target_id` rather than an inventory scenario. Background runs stream ordered Server-Sent Events. The React dashboard derives its title, operation, actors, invariant, evidence fields, and transaction lanes from target metadata and the report.

## Security and production gaps

Candidate code is untrusted. A child process, copied workspace, lexical path controls, and timeouts provide containment but not a security sandbox. Production execution needs an ephemeral container or VM with restricted CPU, memory, filesystem, network, and database grants.

The target adapter can destructively reset its tables. RaceProof therefore rejects non-PostgreSQL URLs and rejects non-local hosts unless `RACEPROOF_ALLOW_NONLOCAL_DATABASE=true` is explicitly set for an isolated test database. The API redacts configured keys and URL passwords from surfaced errors. Production still needs authentication, authorization, durable queues, artifact retention, a stronger secret store, and a persistent LangGraph checkpointer.
