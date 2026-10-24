# Reusable Platform Delivery Plan

## Outcome target

The demo must reliably load two external applications, reproduce both defects on real PostgreSQL, show generic traces and failed invariants, and run model-authored candidates through protected deterministic verification. If LLM access is unavailable, use the deterministic reproduction command for evidence and show that the autonomous workflow stops honestly as `MODEL_UNAVAILABLE`.

Model completion criteria include separate Investigation and Repair model configuration, opt-in live capability validation, provider-request accounting across internal tool loops, conservative request and token budgets, bounded retries, explicit fallback telemetry, and structured terminal provider failures. A local test suite never substitutes for `raceproof validate-models` when claiming live model compatibility.

## Work split

Developer A owns PostgreSQL, scheduling, invariant, candidate execution, and tests. Developer B owns both distinct LangChain agents, their LangGraph handoff, FastAPI events, dashboard, and integration. Both review the security boundary and final demo script.

| Time | Developer A | Developer B | Go/no-go evidence |
| --- | --- | --- | --- |
| 0:00 to 0:30 | Target schema and adapters | Versioned contract and registry | Both contracts validate |
| 0:30 to 1:30 | Declarative two-session scheduler | Generic SSE service and target UI | Both targets fail their own invariant |
| 1:30 to 2:15 | Reset and executable oracles | Dynamic transaction lanes and evidence | UI changes entirely from target metadata |
| 2:15 to 3:15 | Candidate workspace and subprocess verifier | Investigation tools, hypothesis schema, and handoff | Failed control experiment replans; failure creates artifact |
| 3:15 to 4:15 | Exact/adapted replay and gate aggregation | Repair tools, diff schema, and repair loop | Failed candidate evidence reaches the next repair attempt |
| 4:15 to 5:00 | Integration and failure tests | Diff, before/after, limitation UI | Full local test suite passes |
| 5:00 to 6:00 | Harden timeouts and demo data | Polish, responsive layout, rehearsal | Fresh setup and one recorded end-to-end run |

## Essential

- Real PostgreSQL and separate transactions.
- Controlled A/B schedule.
- Approved executable invariant.
- Honest statuses and bounded attempts.
- Actual model-authored diff path.
- Protected verification files and clean candidate workspace.
- Minimal live dashboard.

## Optional after the core works

- SQLite graph checkpointer.
- Additional adjacent schedules and actor counts.
- Lock-wait introspection from `pg_stat_activity`.
- Third idempotency scenario with a unique constraint.
- Containerized candidate sandbox.
- Side-by-side syntax-highlighted diff.

## Highest risks

1. A target adapter commits outside the intended transaction boundary.
2. A candidate removes the checkpoint and creates a false pass.
3. SQLAlchemy module/session state is imported from the baseline instead of the candidate.
4. LLM output is not a complete applicable diff.
5. Candidate tests mutate the same database concurrently.
6. Demo time is spent on UI before the database evidence is repeatable.

## Fallbacks

- If the model provider is unavailable, demonstrate deterministic reproduction with the CLI, then show that the autonomous workflow stops as `MODEL_UNAVAILABLE`. Do not replay a canned hypothesis or fix as AI output.
- If adapted scheduling cannot classify the candidate, return `UNVERIFIABLE_SCHEDULE` and show the missing semantic anchor.
- If Docker is unavailable, start an isolated local PostgreSQL cluster bound to loopback.
- If SSE reconnect behavior becomes unstable, poll `GET /api/runs/{id}` while keeping the same real event list.
- If performance samples are noisy, display measurements without a comparative pass claim.

