export type RunStatus = string

const terminalStatuses = new Set([
  'READY',
  'MODEL_UNAVAILABLE',
  'VERIFIED_CANDIDATE',
  'UNRESOLVED',
  'UNCONFIRMED',
  'EXECUTION_ERROR',
  'UNVERIFIABLE_SCHEDULE',
  'BUDGET_EXCEEDED',
  'AUTHENTICATION_ERROR',
  'RATE_LIMITED',
  'QUOTA_EXCEEDED',
  'PROVIDERS_EXHAUSTED',
  'MODEL_CONFIGURATION_ERROR',
])

export function normalizeStatus(value: unknown, fallback = 'UNKNOWN'): RunStatus {
  return typeof value === 'string' && value.trim() ? value : fallback
}

export function statusTone(value: unknown): 'good' | 'bad' | 'warn' {
  const status = normalizeStatus(value)
  return status.includes('VERIFIED')
    ? 'good'
    : status.includes('ERROR') || status === 'UNRESOLVED'
      ? 'bad'
      : 'warn'
}

export function statusLabel(value: unknown): string {
  return normalizeStatus(value).replaceAll('_', ' ')
}

export function isTerminalStatus(value: unknown): boolean {
  return terminalStatuses.has(normalizeStatus(value))
}
