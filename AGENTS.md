# Repository Guidance

- Preserve the boundary: models propose; deterministic PostgreSQL execution and approved invariants decide.
- Keep exactly two primary LLM agents: Investigation Agent and Repair Agent. LangGraph is the orchestrator; do not add a supervisor LLM.
- Keep agent tool permissions distinct and communicate through the structured investigation artifact.
- Never report reproduction without a failed executable invariant from real PostgreSQL.
- Never report a verified candidate unless every named gate passed.
- Treat only paths explicitly listed in a validated target contract as model-editable.
- Keep example applications outside `src/raceproof`; core code must not import their domain models or operations.
- Do not let generated patches alter tests, fixtures, scheduler, invariant, configuration, or Git metadata.
- Every repair attempt starts from the unchanged baseline. Instrument only a disposable run copy.
- When transaction structure changes, relocate by semantic SQL role or return `UNVERIFIABLE_SCHEDULE`; missing checkpoints are not success.
- Cap repair attempts and return exact failed evidence with `UNRESOLVED`.
- Use separate SQLAlchemy sessions for concurrent actors.
- Run unit tests, PostgreSQL integration tests, and the frontend build before claiming completion.
- Avoid em dashes in documentation and user-facing copy.

