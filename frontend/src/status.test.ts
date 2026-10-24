import { describe, expect, it } from 'vitest'
import { isTerminalStatus, normalizeStatus, statusLabel, statusTone } from './status'

describe('run status handling', () => {
  it('uses a safe fallback for missing and unexpected values', () => {
    expect(normalizeStatus(undefined)).toBe('UNKNOWN')
    expect(normalizeStatus(null)).toBe('UNKNOWN')
    expect(statusLabel({})).toBe('UNKNOWN')
    expect(statusTone(undefined)).toBe('warn')
  })

  it('preserves terminal and failure semantics', () => {
    expect(statusTone('VERIFIED_CANDIDATE')).toBe('good')
    expect(statusTone('EXECUTION_ERROR')).toBe('bad')
    expect(statusTone('UNRESOLVED')).toBe('bad')
    expect(isTerminalStatus('VERIFIED_CANDIDATE')).toBe(true)
    expect(isTerminalStatus('RUNNING')).toBe(false)
  })
})
