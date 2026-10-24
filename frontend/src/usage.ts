import type { UsageSnapshot } from './types'

const emptyUsage: UsageSnapshot = {
  total_requests: 0, requests_by_agent: {}, requests_by_model: {}, requests_by_provider: {},
  usage_by_provider: {}, input_tokens: 0, output_tokens: 0, total_tokens: 0,
  unknown_token_responses: 0, failed_requests: 0, rate_limit_errors: 0,
  fallback_invocations: 0, remaining_request_budget: 0,
  remaining_investigation_budget: 0, remaining_repair_budget: 0,
  remaining_token_budget: 0, budget_exhausted: false, warnings: [],
  approximate_cost_usd: null, cost_is_partial: false, configured_limits: {},
  budget_exhausted_reason: null, request_log: [], fallback_history: [],
}

export function normalizeUsage(value: unknown): UsageSnapshot | undefined {
  if (!value || typeof value !== 'object') return undefined
  const raw = value as Partial<UsageSnapshot>
  if (typeof raw.total_requests !== 'number') return undefined
  return {
    ...emptyUsage, ...raw,
    requests_by_agent: raw.requests_by_agent ?? {},
    requests_by_model: raw.requests_by_model ?? {},
    requests_by_provider: raw.requests_by_provider ?? {},
    usage_by_provider: raw.usage_by_provider ?? {},
    warnings: Array.isArray(raw.warnings) ? raw.warnings : [],
    configured_limits: raw.configured_limits ?? {},
    request_log: Array.isArray(raw.request_log) ? raw.request_log : [],
    fallback_history: Array.isArray(raw.fallback_history) ? raw.fallback_history : [],
  }
}
