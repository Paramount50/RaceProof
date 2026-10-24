import { describe, expect, it } from 'vitest'
import { normalizeUsage } from './usage'

describe('normalizeUsage', () => {
  it('rejects model request SSE details that are not usage snapshots', () => {
    expect(normalizeUsage({ request_number: 1, model: 'example' })).toBeUndefined()
  })

  it('fills missing arrays in partial usage snapshots', () => {
    const usage = normalizeUsage({ total_requests: 1, total_tokens: 12 })
    expect(usage?.request_log).toEqual([])
    expect(usage?.fallback_history).toEqual([])
    expect(usage?.requests_by_agent).toEqual({})
  })
})
