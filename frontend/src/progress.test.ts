import { describe, expect, it } from 'vitest'
import { deriveStageStates } from './progress'
import type { RunEvent } from './types'

const event = (sequence: number, stage: string, status: string): RunEvent => ({
  sequence, timestamp: '', kind: 'stage', stage, actor: null,
  action: 'stage_status', detail: { status },
})

describe('deriveStageStates', () => {
  it('does not mark apply or verify successful because repair retried', () => {
    const states = deriveStageStates(['repair', 'apply_patch', 'verify', 'finalize'], [
      event(1, 'repair', 'SUCCEEDED'),
      event(2, 'apply_patch', 'FAILED'),
      event(3, 'repair', 'RETRYING'),
      event(4, 'verify', 'SKIPPED'),
      event(5, 'finalize', 'SUCCEEDED'),
    ])
    expect(states.apply_patch).toBe('FAILED')
    expect(states.repair).toBe('RETRYING')
    expect(states.verify).toBe('SKIPPED')
    expect(states.finalize).toBe('SUCCEEDED')
  })
})
