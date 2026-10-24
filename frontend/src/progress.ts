import type { RunEvent } from './types'

export type StageState = 'NOT_STARTED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'RETRYING' | 'SKIPPED'

export function deriveStageStates(stages: readonly string[], events: RunEvent[]) {
  const result = Object.fromEntries(stages.map(stage => [stage, 'NOT_STARTED'])) as Record<string, StageState>
  for (const event of events.filter(item => item.kind === 'stage')) {
    const candidate = event.detail?.status
    result[event.stage] = typeof candidate === 'string' && ['NOT_STARTED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'RETRYING', 'SKIPPED'].includes(candidate)
      ? candidate as StageState
      : 'RUNNING'
  }
  return result
}
