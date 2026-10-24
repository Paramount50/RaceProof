import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ReviewPanel } from './ReviewPanel'
import type { ReviewState } from './types'

const handlers = {
  onReviewerChange: () => undefined,
  onCommentChange: () => undefined,
  onDecision: () => undefined,
}

const pending: ReviewState = {
  run_id: 'a1b2c3d4e5f6',
  deterministic_status: 'VERIFIED_CANDIDATE',
  eligible: true,
  reason: 'candidate passed every required deterministic verification gate',
  report_sha256: 'a'.repeat(64),
  candidate_diff_sha256: 'b'.repeat(64),
  review: null,
}

describe('human review panel', () => {
  it('keeps deterministic verification separate from pending review controls', () => {
    const html = renderToStaticMarkup(<ReviewPanel state={pending} loading={false} submitting={false} error={null} reviewer="reviewer" comment="" {...handlers}/>)
    expect(html).toContain('VERIFIED CANDIDATE')
    expect(html).toContain('Approve candidate')
    expect(html).toContain('Reject candidate')
    expect(html).toContain('does not apply a patch')
  })

  it('renders an immutable decision without active controls', () => {
    const state: ReviewState = {
      ...pending,
      eligible: false,
      review: {
        schema_version: 1,
        run_id: pending.run_id,
        decision: 'APPROVED',
        reviewer: 'reviewer',
        comment: 'Evidence accepted.',
        decided_at: '2026-10-24T08:00:00Z',
        report_sha256: pending.report_sha256,
        candidate_diff_sha256: pending.candidate_diff_sha256!,
      },
    }
    const html = renderToStaticMarkup(<ReviewPanel state={state} loading={false} submitting={false} error={null} reviewer="reviewer" comment="" {...handlers}/>)
    expect(html).toContain('Evidence accepted.')
    expect(html).not.toContain('Approve candidate')
    expect(html).not.toContain('Reject candidate')
  })
})
