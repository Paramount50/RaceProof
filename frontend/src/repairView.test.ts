import { describe, expect, it } from 'vitest'
import { describeRepairAttempt } from './repairView'
import type { RunReport } from './types'

describe('describeRepairAttempt', () => {
  it('shows the real generated diff and patch application result', () => {
    const attempt: RunReport['attempts'][number] = {
      number: 1,
      proposal: {
        strategy: 'model strategy', diagnosis: 'model diagnosis', unified_diff: '',
        file_replacements: [], cannot_repair: false,
      },
      generated_diff: 'diff --git a/src/app.py b/src/app.py\n-old\n+new\n',
      patch_validation: { accepted: true, errors: [], changed_paths: ['src/app.py'] },
      artifact_paths: {},
    }
    const view = describeRepairAttempt(attempt, 'VERIFY')
    expect(view.diff).toContain('+new')
    expect(view.files).toEqual(['src/app.py'])
    expect(view.application).toBe('Applied to isolated candidate')
    expect(view.verification).toBe('Not executed')
  })
})
