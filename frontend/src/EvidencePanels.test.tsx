import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { DatabaseBehaviorComparison, LangSmithPanel, LockWaitPanel, StageDurationPanel, orderedScheduleEvents } from './EvidencePanels'
import type { RunEvent, ScheduleResult } from './types'

const event = (sequence: number, kind: RunEvent['kind'], actor: string | null, action: string): RunEvent => ({
  sequence, timestamp: '2026-10-24T00:00:00Z', kind, stage: 'execute', actor, action, detail: {},
})

const result = (overrides: Partial<ScheduleResult> = {}): ScheduleResult => ({
  schedule_name: 'probe', status: 'COMPLETED', exact: false, completed: true,
  reproduced_failure: false, actor_outcomes: { A: 'accepted', B: 'rejected' }, events: [],
  invariant: { name: 'exclusive', passed: true, assertion: 'count <= 1', evidence: { booking_count: 1 }, explanation: 'safe' },
  elapsed_ms: 20, error: null, workers_terminated: true, lock_observation_status: 'available',
  lock_observation_error: null, lock_waits: [], wait_for_edges: [], deadlock_cycle: false,
  ...overrides,
})

describe('evidence panels', () => {
  it('orders only schedule events', () => {
    expect(orderedScheduleEvents([
      event(3, 'sql', 'A', 'sql'), event(2, 'checkpoint', 'B', 'after_read'), event(1, 'transaction', 'A', 'BEGIN'),
    ]).map(item => item.sequence)).toEqual([1, 2])
  })

  it('renders before and after invariant evidence', () => {
    const html = renderToStaticMarkup(<DatabaseBehaviorComparison before={result({ invariant: { ...result().invariant!, passed: false } })} after={result()}/>)
    expect(html).toContain('Invariant failed')
    expect(html).toContain('Invariant passed')
    expect(html).toContain('booking count: 1')
  })

  it('renders wait edges, release evidence, and unavailable state honestly', () => {
    const observed = renderToStaticMarkup(<LockWaitPanel result={result({
      wait_for_edges: [{ waiting_actor: 'B', blocking_actor: 'A', lock_type: 'transactionid', lock_mode: 'ShareLock', relation: null }],
      lock_waits: [{ waiting_actor: 'B', blocking_actors: ['A'], lock_type: 'transactionid', lock_mode: 'ShareLock', relation: null, duration_ms: 42, release_points: ['A:COMMIT#9'] }],
    })}/>)
    expect(observed).toContain('B')
    expect(observed).toContain('waited for')
    expect(observed).toContain('A:COMMIT#9')
    const unavailable = renderToStaticMarkup(<LockWaitPanel result={result({ lock_observation_status: 'unavailable' })}/>)
    expect(unavailable).toContain('Verification gates were evaluated independently')
  })

  it('formats stage durations', () => {
    const html = renderToStaticMarkup(<StageDurationPanel timings={{ inspect: 15, verify: 1250 }}/>)
    expect(html).toContain('15 ms')
    expect(html).toContain('1.25 s')
  })

  it('shows a trace link only when a reference exists', () => {
    const status = { enabled: true, configured: true, project: 'raceproof-local-dev', reason: 'metadata-only tracing is enabled' }
    expect(renderToStaticMarkup(<LangSmithPanel status={status}/>)).not.toContain('Open established trace')
    expect(renderToStaticMarkup(<LangSmithPanel status={status} traceUrl="https://smith.langchain.com/o/example"/>)).toContain('Open established trace')
  })
})
