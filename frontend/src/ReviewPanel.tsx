import { Check, ShieldCheck, X } from 'lucide-react'
import type { ReviewDecision, ReviewState } from './types'

type ReviewPanelProps = {
  state: ReviewState | null
  loading: boolean
  submitting: boolean
  error: string | null
  reviewer: string
  comment: string
  onReviewerChange: (value: string) => void
  onCommentChange: (value: string) => void
  onDecision: (decision: ReviewDecision) => void
}

export function ReviewPanel({
  state,
  loading,
  submitting,
  error,
  reviewer,
  comment,
  onReviewerChange,
  onCommentChange,
  onDecision,
}: ReviewPanelProps) {
  const review = state?.review
  const disabled = !state?.eligible || submitting || !reviewer.trim()
  return <article className="panel review-panel">
    <div className="panel-head"><div><span className="kicker">SEPARATE HUMAN DECISION</span><h2>Candidate review</h2></div><ShieldCheck size={20}/></div>
    <div className="review-status-row">
      <div><span>Deterministic result</span><strong>{state?.deterministic_status.replaceAll('_', ' ') ?? 'Awaiting persisted report'}</strong></div>
      <div><span>Human decision</span><strong className={review ? review.decision.toLowerCase() : ''}>{review?.decision ?? 'PENDING'}</strong></div>
    </div>
    {loading && <p className="evidence-empty">Loading persisted review state.</p>}
    {error && <div className="error-banner"><X size={15}/><span>{error}</span></div>}
    {state && <div className="review-fingerprints">
      <small title={state.report_sha256}>Report SHA-256: {state.report_sha256.slice(0, 16)}...</small>
      <small title={state.candidate_diff_sha256 ?? ''}>Candidate SHA-256: {state.candidate_diff_sha256?.slice(0, 16) ?? 'unavailable'}{state.candidate_diff_sha256 ? '...' : ''}</small>
    </div>}
    {review ? <div className={`review-decision ${review.decision.toLowerCase()}`}>
      {review.decision === 'APPROVED' ? <Check size={18}/> : <X size={18}/>}<div><strong>{review.decision}</strong><p>{review.comment ?? 'No review comment was provided.'}</p><small>{review.reviewer} · {new Date(review.decided_at).toLocaleString()}</small></div>
    </div> : state?.eligible ? <div className="review-form">
      <label>Reviewer<input value={reviewer} maxLength={200} onChange={event => onReviewerChange(event.target.value)} /></label>
      <label>Comment<textarea value={comment} maxLength={4000} onChange={event => onCommentChange(event.target.value)} placeholder="Optional evidence-based review note" /></label>
      <div className="review-actions">
        <button className="reject-button" disabled={disabled} onClick={() => onDecision('REJECTED')}><X size={15}/>Reject candidate</button>
        <button className="approve-button" disabled={disabled} onClick={() => onDecision('APPROVED')}><Check size={15}/>Approve candidate</button>
      </div>
    </div> : state && <p className="evidence-empty">{state.reason}</p>}
    <p className="review-boundary">A review records a decision only. It does not apply a patch, merge code, or modify a production system.</p>
  </article>
}
