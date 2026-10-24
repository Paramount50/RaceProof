export type RunEvent = {
  sequence: number
  timestamp: string
  kind: 'agent' | 'stage' | 'transaction' | 'checkpoint' | 'sql' | 'invariant' | 'repair' | 'verification' | 'error' | 'usage'
  stage: string
  actor: string | null
  action: string
  detail: Record<string, unknown>
}

export type InvariantResult = {
  name: string
  passed: boolean
  assertion: string
  evidence: Record<string, unknown>
  explanation: string
}

export type Target = {
  id: string
  name: string
  description: string
  operation: string
  actors: string[]
  invariant: { name: string; description: string; approved: true }
}

export type ScheduleResult = {
  schedule_name: string
  status: string
  terminal_reason?: string
  exact: boolean
  completed: boolean
  reproduced_failure: boolean
  actor_outcomes: Record<string, string>
  events: RunEvent[]
  invariant: InvariantResult | null
  elapsed_ms: number
  error: string | null
  workers_terminated: boolean
  lock_observation_status: 'not_requested' | 'available' | 'unavailable'
  lock_observation_error: string | null
  lock_waits: Array<{
    waiting_actor: string
    blocking_actors: string[]
    lock_type: string
    lock_mode: string
    relation: string | null
    duration_ms: number
    release_points: string[]
  }>
  wait_for_edges: Array<{
    waiting_actor: string
    blocking_actor: string
    lock_type: string
    lock_mode: string
    relation: string | null
  }>
  deadlock_cycle: boolean
}

export type Verification = {
  passed: boolean
  replay_mode: string
  gates: Record<string, boolean>
  schedule_results: ScheduleResult[]
  lock_evidence_results: ScheduleResult[]
  performance?: {
    runs: number
    mean_latency_ms: number
    median_latency_ms?: number | null
    p95_latency_ms: number
    throughput_per_second: number
    deadlocks: number
    latency_samples_ms?: number[]
    warmup_runs?: number
  }
  execution_evidence?: {
    performance_comparison?: {
      baseline_p95_ms: number | null
      candidate_p95_ms: number | null
      regression_ratio: number | null
      configured_limit: number
      candidate_samples: number[]
      candidate_median_ms: number | null
    }
  }
}

export type RunReport = {
  run_id: string
  status: string
  terminal_reason?: string
  target_id: string
  target_name: string
  target_operation: string
  invariant_name: string
  invariant_description: string
  investigation_attempts: Array<{
    number: number
    decision: {
      suspected_source: string
      concurrency_pattern: string
      reasoning_summary: string
      experiment_name: string
      confidence: number
      supporting_evidence: string[]
    }
    result?: ScheduleResult
    failure_reason?: string
  }>
  investigation_artifact: {
    confirmed_vulnerability: boolean
    hypothesized_root_cause: string
    evidence_references: string[]
  } | null
  baseline: ScheduleResult | null
  final_diff: string | null
  stage_timings_ms: Record<string, number>
  attempts: Array<{
    number: number
    proposal?: {
      strategy: string
      diagnosis: string
      unified_diff: string
      file_replacements: Array<{ path: string; content: string }>
      cannot_repair: boolean
    }
    generated_diff?: string
    patch_validation?: { accepted: boolean; errors: string[]; changed_paths: string[] }
    verification?: Verification
    failure_reason?: string
    artifact_paths: Record<string, string>
  }>
  limitations: string[]
  investigation_model: string
  repair_model: string
  fallback_model: string | null
  fallback_enabled: boolean
  provider_order: string[]
  auto_fallback: boolean
  openrouter_investigation_models: string[]
  openrouter_repair_models: string[]
  usage: UsageSnapshot
}

export type UsageSnapshot = {
  total_requests: number
  requests_by_agent: Record<string, number>
  requests_by_model: Record<string, number>
  requests_by_provider: Record<string, number>
  usage_by_provider: Record<string, { requests: number; failed_requests: number; input_tokens: number; output_tokens: number; total_tokens: number; unknown_token_responses: number }>
  input_tokens: number
  output_tokens: number
  total_tokens: number
  unknown_token_responses: number
  failed_requests: number
  rate_limit_errors: number
  fallback_invocations: number
  remaining_request_budget: number
  remaining_investigation_budget: number
  remaining_repair_budget: number
  remaining_token_budget: number
  budget_exhausted: boolean
  warnings: string[]
  approximate_cost_usd: number | null
  cost_is_partial: boolean
  configured_limits: Record<string, number>
  budget_exhausted_reason: string | null
  request_log: Array<{
    request_number: number
    agent_request_number: number
    agent: string
    operation: string
    provider: string
    model: string
    retry_index: number
    status: string
    total_tokens: number | null
    error_code: string | null
    upstream_provider: string | null
    limit_source: string | null
    failure_reason: string | null
    message_count: number | null
    context_characters: number | null
    estimated_input_tokens: number | null
  }>
  fallback_history: Array<{
    agent: string
    operation: string
    from_provider: string
    from_model: string
    to_provider: string
    to_model: string
    reason_code: string
    reason: string
    upstream_provider: string | null
    limit_source: string | null
  }>
}

export type ModelStatus = {
  key_configured: boolean
  investigation_model: string
  repair_model: string
  fallback_model: string
  fallback_enabled: boolean
  budgets: Record<string, number>
  configuration: {
    env_file: string
    process_environment_overrides_dotenv: boolean
    restart_fastapi_after_change: boolean
    sources: Record<string, string>
  }
  providers: {
    order: string[]
    auto_fallback: boolean
    gemini: { key_configured: boolean; investigation_model: string; repair_model: string }
    openrouter: { key_configured: boolean; investigation_models: string[]; repair_models: string[]; free_only: boolean; paid_models_allowed: boolean }
  }
}
