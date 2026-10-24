import { useEffect, useMemo, useState } from 'react'
import {
  Activity, AlertTriangle, ArrowRight, Check, ChevronRight, CircleDot,
  Code2, Database, GitCompare, Loader2, LockKeyhole, Play, ShieldCheck,
  TerminalSquare, X, Gauge,
} from 'lucide-react'
import type { InvariantResult, ModelStatus, ReviewDecision, ReviewState, RunEvent, RunReport, Target } from './types'
import { normalizeUsage } from './usage'
import { deriveStageStates } from './progress'
import { describeRepairAttempt } from './repairView'
import { isTerminalStatus, normalizeStatus, statusLabel, statusTone } from './status'
import { DatabaseBehaviorComparison, LangSmithPanel, LockWaitPanel, SchedulePlayer, StageDurationPanel } from './EvidencePanels'
import { ReviewPanel } from './ReviewPanel'

const API = import.meta.env.VITE_API_URL ?? ''
const stages = ['load_target', 'inspect', 'investigate', 'execute_experiment', 'observe', 'repair', 'apply_patch', 'verify', 'finalize']

function StatusPill({ status }: { status: unknown }) {
  return <span className={`status-pill ${statusTone(status)}`}><CircleDot size={12} />{statusLabel(status)}</span>
}

function Metric({ label, value, accent = false }: { label: string; value: string | number; accent?: boolean }) {
  return <div className={`metric ${accent ? 'accent' : ''}`}><span>{label}</span><strong>{value}</strong></div>
}

function App() {
  const [runId, setRunId] = useState<string | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [report, setReport] = useState<RunReport | null>(null)
  const [status, setStatus] = useState('READY')
  const [health, setHealth] = useState<boolean | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [targets, setTargets] = useState<Target[]>([])
  const [targetId, setTargetId] = useState('')
  const [modelStatus, setModelStatus] = useState<ModelStatus | null>(null)
  const [reviewState, setReviewState] = useState<ReviewState | null>(null)
  const [reviewLoading, setReviewLoading] = useState(false)
  const [reviewSubmitting, setReviewSubmitting] = useState(false)
  const [reviewError, setReviewError] = useState<string | null>(null)
  const [reviewer, setReviewer] = useState('human-reviewer')
  const [reviewComment, setReviewComment] = useState('')

  useEffect(() => {
    fetch(`${API}/api/health`).then(r => r.json()).then(data => setHealth(data.postgresql)).catch(() => setHealth(false))
    fetch(`${API}/api/model-status`).then(r => r.json()).then(setModelStatus).catch(() => setModelStatus(null))
    fetch(`${API}/api/targets`).then(r => r.json()).then((data: Target[]) => {
      setTargets(data); setTargetId(current => current || data[0]?.id || '')
    }).catch(() => setError('Could not load target applications.'))
  }, [])

  async function loadReview(id: string) {
    setReviewLoading(true); setReviewError(null)
    try {
      const response = await fetch(`${API}/api/runs/${id}/review`)
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? 'Could not load review state.')
      setReviewState(data as ReviewState)
    } finally {
      setReviewLoading(false)
    }
  }

  async function submitReview(decision: ReviewDecision) {
    if (!runId || !reviewState?.candidate_diff_sha256) return
    const confirmed = window.confirm(`Record an immutable ${decision.toLowerCase()} decision for this candidate?`)
    if (!confirmed) return
    setReviewSubmitting(true); setReviewError(null)
    try {
      const response = await fetch(`${API}/api/runs/${runId}/review`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          decision,
          reviewer,
          comment: reviewComment,
          expected_report_sha256: reviewState.report_sha256,
          expected_candidate_diff_sha256: reviewState.candidate_diff_sha256,
        }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? 'Could not record review decision.')
      await loadReview(runId)
    } catch (reviewFailure) {
      setReviewError(reviewFailure instanceof Error ? reviewFailure.message : 'Could not record review decision.')
    } finally {
      setReviewSubmitting(false)
    }
  }

  async function startRun() {
    setError(null); setEvents([]); setReport(null); setReviewState(null); setReviewError(null); setReviewComment(''); setStatus('QUEUED')
    const response = await fetch(`${API}/api/runs`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_id: targetId }),
    })
    if (!response.ok) throw new Error(await response.text())
    const data = await response.json(); setRunId(data.run_id)
  }

  useEffect(() => {
    if (!runId) return
    const source = new EventSource(`${API}/api/runs/${runId}/events`)
    source.onmessage = event => {
      const item = JSON.parse(event.data) as RunEvent
      setEvents(previous => previous.some(existing => existing.sequence === item.sequence) ? previous : [...previous, item])
      if (item.kind === 'stage') setStatus(normalizeStatus(item.detail?.status, item.stage.toUpperCase()))
    }
    source.addEventListener('done', async () => {
      source.close()
      const response = await fetch(`${API}/api/runs/${runId}`)
      const data = await response.json()
      setStatus(normalizeStatus(data.status)); setReport(data.report); setError(data.error)
      if (data.report) loadReview(runId).catch(reviewFailure => setReviewError(reviewFailure instanceof Error ? reviewFailure.message : 'Could not load review state.'))
    })
    source.onerror = () => { source.close(); setError('Event stream disconnected.') }
    return () => source.close()
  }, [runId])

  const stageStates = useMemo(() => deriveStageStates(stages, events), [events])
  const transactions = events.filter(event => ['transaction', 'checkpoint'].includes(event.kind))
  const sqlCount = events.filter(event => event.kind === 'sql').length
  const baseline = report?.baseline
  const invariantEvent = [...events].reverse().find(event => event.kind === 'invariant')
  const invariant = baseline?.invariant ?? (invariantEvent?.detail as unknown as InvariantResult | undefined)
  const verification = report?.attempts?.at(-1)?.verification
  const verifiedSchedule = verification?.schedule_results?.[0]
  const lockEvidence = verification?.lock_evidence_results?.find(result => result.lock_waits.length || result.wait_for_edges.length)
    ?? verification?.lock_evidence_results?.[0]
    ?? verifiedSchedule
  const investigation = report?.investigation_attempts?.at(-1)
  const repairAttempt = report?.attempts?.at(-1)
  const repairView = describeRepairAttempt(repairAttempt, report?.status ?? status)
  const investigationEvents = events.filter(event => event.kind === 'agent' && event.actor === 'Investigation Agent')
  const repairEvents = events.filter(event => event.kind === 'agent' && event.actor === 'Repair Agent')
  const liveUsageEvent = [...events].reverse().find(event => event.kind === 'usage' && event.action === 'model_usage')
  const liveUsage = normalizeUsage(liveUsageEvent?.detail)
  const usage = normalizeUsage(report?.usage) ?? liveUsage
  const activeRequest = usage?.request_log?.at(-1)
  const target = targets.find(item => item.id === targetId)
  const actors = target?.actors ?? []
  const evidence = Object.entries(invariant?.evidence ?? {}).slice(0, 3)
  const busy = !isTerminalStatus(status)

  return <main>
    <header className="topbar">
      <div className="brand"><div className="brand-mark"><LockKeyhole size={18} /></div><div><strong>RaceProof</strong><span>Concurrency lab</span></div></div>
      <div className="connection"><span className={`pulse ${health ? 'online' : ''}`} /> PostgreSQL {health === null ? 'checking' : health ? 'connected' : 'offline'}</div>
      <div className="header-actions"><select value={targetId} onChange={event => setTargetId(event.target.value)} disabled={busy}>{targets.map(item => <option value={item.id} key={item.id}>{item.name}</option>)}</select><StatusPill status={status} /><button className="run-button" onClick={() => startRun().catch(e => setError(e.message))} disabled={busy || health === false || !targetId}>{busy ? <Loader2 size={16} className="spin" /> : <Play size={15} fill="currentColor" />}Run investigation</button></div>
    </header>

    <section className="hero">
      <div><div className="eyebrow">LIVE EXPERIMENT · {target?.name.toUpperCase() ?? 'SELECT A TARGET'}</div><h1>Make the invisible<br/><em>interleaving</em> visible.</h1><p>{target?.description ?? 'Controlled PostgreSQL transactions. Executable business invariants. Model-authored patches behind deterministic gates.'}</p></div>
      <div className="hero-side"><Metric label="Operation" value={target?.operation.split(':').at(-1) ?? '-'}/><Metric label="Actors" value={actors.join(' + ') || '-'}/><Metric label="SQL events" value={sqlCount.toString().padStart(2, '0')} accent/></div>
    </section>

    {error && <div className="error-banner"><AlertTriangle size={17}/><span>{error}</span></div>}

    <section className="stage-rail">
      {stages.map((stage, index) => {
        const stageState = stageStates[stage]
        return <div className={`stage ${stageState === 'RUNNING' || stageState === 'RETRYING' ? 'active' : ''} ${stageState === 'SUCCEEDED' ? 'done' : ''} ${stageState.toLowerCase()}`} key={stage}>
          <div className="stage-dot">{stageState === 'SUCCEEDED' ? <Check size={12}/> : stageState === 'FAILED' ? <X size={12}/> : stageState === 'RUNNING' || stageState === 'RETRYING' ? <Loader2 size={12} className="spin"/> : index + 1}</div><span>{stage}<small>{stageState.replaceAll('_', ' ')}</small></span>{index < stages.length - 1 && <div className="stage-line"/>}
        </div>
      })}
    </section>

    <section className="workspace-grid">
      <article className="panel transaction-panel">
        <div className="panel-head"><div><span className="kicker">CONTROLLED SCHEDULE</span><h2>Transaction timeline</h2></div><Database size={20}/></div>
        <div className="lanes">
          {actors.map(actor => <div className="lane" key={actor}>
            <div className={`actor actor-${actor.toLowerCase()}`}>{actor}</div>
            <div className="lane-track">
              {transactions.filter(event => event.actor === actor).map(event => <div className={`tx-event ${event.kind}`} key={event.sequence}>
                <span>{event.action.replaceAll('_', ' ')}</span><small>#{event.sequence}</small>
              </div>)}
              {!transactions.some(event => event.actor === actor) && <div className="lane-empty">Waiting for transaction events</div>}
            </div>
          </div>)}
        </div>
        <div className="schedule-caption"><Activity size={15}/><span>Every interleaving is driven by the selected target's declarative checkpoint schedule.</span></div>
      </article>

      <article className="panel invariant-panel">
        <div className="panel-head"><div><span className="kicker">DETERMINISTIC ORACLE</span><h2>Business invariant</h2></div><ShieldCheck size={20}/></div>
        <code>{invariant?.assertion ?? target?.invariant.description ?? 'Waiting for the approved target invariant'}</code>
        <div className={`verdict ${invariant ? (invariant.passed ? 'pass' : 'fail') : ''}`}>
          {invariant ? (invariant.passed ? <Check/> : <X/>) : <CircleDot/>}
          <div><span>INVARIANT</span><strong>{invariant ? (invariant.passed ? 'PASSED' : 'FAILED') : 'PENDING'}</strong></div>
        </div>
        <div className="invariant-stats">{evidence.map(([label, value], index) => <Metric key={label} label={label.replaceAll('_', ' ')} value={String(value)} accent={index === 1}/>)}</div>
      </article>

      <article className="panel activity-panel">
        <div className="panel-head"><div><span className="kicker">LLM AGENT 1</span><h2>Investigation Agent</h2></div><TerminalSquare size={20}/></div>
        <div className="activity-list">
          {investigationEvents.slice(-6).map(event => <div className="activity-row" key={event.sequence}>
            <span className={`activity-icon ${event.kind}`}><ChevronRight size={13}/></span><div><strong>{event.action.replaceAll('_', ' ')}</strong><small>{String(event.detail.reasoning_summary ?? event.detail.root_cause ?? event.detail.schedule ?? new Date(event.timestamp).toLocaleTimeString())}</small></div>
          </div>)}
          {investigation && <div className="agent-summary"><strong>{investigation.decision.concurrency_pattern}</strong><p>{investigation.decision.reasoning_summary}</p><small>Experiment: {investigation.decision.experiment_name} · confidence {Math.round(investigation.decision.confidence * 100)}%</small></div>}
          {!investigationEvents.length && !investigation && <div className="empty-state"><Activity size={24}/><p>Waiting for a structured LLM hypothesis.</p></div>}
        </div>
      </article>

      <article className="panel activity-panel">
        <div className="panel-head"><div><span className="kicker">LOCAL COST CONTROL</span><h2>Model usage</h2></div><Gauge size={20}/></div>
        <div className="invariant-stats">
          <Metric label="Requests" value={usage?.total_requests ?? 0} accent/>
          <Metric label="Reported tokens" value={usage ? `${usage.total_tokens}${usage.unknown_token_responses ? '+' : ''}` : '0'}/>
          <Metric label="Requests left" value={usage?.remaining_request_budget ?? modelStatus?.budgets.max_model_requests ?? '-'}/>
        </div>
        <div className="agent-summary">
          <strong>Active provider: {activeRequest?.provider ?? 'none yet'} · {activeRequest?.model ?? 'waiting'}</strong>
          <p>Gemini primary: {report?.investigation_model ?? modelStatus?.investigation_model ?? 'loading'} · OpenRouter fallback order: {modelStatus?.providers.openrouter.investigation_models.join(' → ') ?? 'not configured'}</p>
          <small>Investigation {usage?.requests_by_agent.investigation ?? 0}/{usage?.configured_limits.max_investigation_requests ?? modelStatus?.budgets.max_investigation_requests ?? '-'} · Repair {usage?.requests_by_agent.repair ?? 0}/{usage?.configured_limits.max_repair_requests ?? modelStatus?.budgets.max_repair_requests ?? '-'} · Global {usage?.total_requests ?? 0}/{usage?.configured_limits.max_model_requests ?? modelStatus?.budgets.max_model_requests ?? '-'}</small>
          <small>Gemini requests {usage?.requests_by_provider.gemini ?? 0} · OpenRouter requests {usage?.requests_by_provider.openrouter ?? 0}</small>
          <small>Repair reserve {modelStatus?.budgets.min_repair_request_reserve ?? '-'} · FastAPI restart required after configuration changes</small>
          <small>Provider retries per operation {modelStatus?.budgets.model_max_retries ?? '-'} · every retry consumes the same global ledger</small>
        </div>
        {usage?.budget_exhausted_reason && <div className="error-banner"><AlertTriangle size={15}/><span>Local budget: {usage.budget_exhausted_reason}</span></div>}
        {status === 'QUOTA_EXCEEDED' && <div className="error-banner"><AlertTriangle size={15}/><span>Provider quota exhausted. This is separate from RaceProof's local safety budget.</span></div>}
        {usage?.fallback_history.map((event, index) => <div className="schedule-caption" key={`${event.from_model}-${event.to_model}-${index}`}><AlertTriangle size={15}/><span>{event.from_provider}/{event.from_model} → {event.to_provider}/{event.to_model}: {event.reason_code}{event.upstream_provider ? ` from ${event.upstream_provider}` : ''}</span></div>)}
        {usage?.request_log.slice(-4).map(item => <div className="activity-row" key={item.request_number}><span className="activity-icon"><ChevronRight size={13}/></span><div><strong>#{item.request_number} {item.agent} · {item.operation}</strong><small>{item.provider} · {item.model} · {item.status} · {item.total_tokens ?? 'unknown'} tokens{item.estimated_input_tokens != null ? ` · ~${item.estimated_input_tokens} input preflight` : ''}{item.message_count != null ? ` · ${item.message_count} messages` : ''}{item.retry_index ? ` · retry ${item.retry_index}` : ''}{item.upstream_provider ? ` · upstream ${item.upstream_provider}` : ''}{item.limit_source ? ` · ${item.limit_source}` : ''}</small></div></div>)}
        <div className="schedule-caption"><AlertTriangle size={15}/><span>{usage?.unknown_token_responses ? 'Some provider token metadata is unavailable. Token totals are lower bounds.' : usage?.approximate_cost_usd != null ? `Verified configured estimate: $${usage.approximate_cost_usd.toFixed(6)}${usage.cost_is_partial ? ' partial' : ''}` : 'Dollar estimates stay hidden until verified pricing is configured.'}</span></div>
      </article>

      <article className="panel activity-panel">
        <div className="panel-head"><div><span className="kicker">LLM AGENT 2</span><h2>Repair Agent</h2></div><Code2 size={20}/></div>
        <div className="activity-list">
          {repairEvents.slice(-6).map(event => <div className="activity-row" key={event.sequence}>
            <span className={`activity-icon ${event.kind}`}><ChevronRight size={13}/></span><div><strong>{event.action.replaceAll('_', ' ')}</strong><small>{String(event.detail.strategy ?? (event.detail.passed === true ? 'verification passed' : event.detail.passed === false ? 'verification failed' : new Date(event.timestamp).toLocaleTimeString()))}</small></div>
          </div>)}
          {repairAttempt?.proposal && <div className="agent-summary">
            <strong>{repairAttempt.proposal.strategy}</strong>
            <p>{repairAttempt.proposal.diagnosis}</p>
            <small>Files: {repairView.files.join(', ') || 'none'}</small>
            <small>Patch: {repairView.application}</small>
            <small>Verification: {repairView.verification}</small>
            <small>Candidate status: {repairView.candidateStatus.replaceAll('_', ' ')}</small>
              {report?.terminal_reason && <small>Termination: {report.terminal_reason}</small>}
              {repairAttempt.failure_reason && <small>Feedback: {repairAttempt.failure_reason}</small>}
          </div>}
          {!repairEvents.length && !repairAttempt && <div className="empty-state"><Code2 size={24}/><p>Repair starts only after a confirmed invariant violation.</p></div>}
        </div>
      </article>

      <article className="panel evidence-panel">
        <div className="panel-head"><div><span className="kicker">BEFORE / AFTER</span><h2>Verification evidence</h2></div><GitCompare size={20}/></div>
        <div className="comparison">
          <div className="compare-card before"><span>BEFORE</span><strong>{baseline?.invariant ? (baseline.invariant.passed ? 'Invariant passed' : 'Invariant failed') : 'Awaiting baseline'}</strong><small>{baseline?.invariant ? `${baseline.schedule_name} on PostgreSQL` : 'No evidence yet'}</small></div>
          <ArrowRight size={20}/>
          <div className={`compare-card after ${verification?.passed ? 'verified' : ''}`}><span>AFTER</span><strong>{verification?.passed ? 'All declared gates passed' : 'Not verified'}</strong><small>{verification ? `${verification.replay_mode} replay` : 'Candidate required'}</small></div>
        </div>
        {verification && <div className="gate-grid">{Object.entries(verification.gates).map(([name, passed]) => <div key={name} className={passed ? 'gate pass' : 'gate fail'}>{passed ? <Check size={13}/> : <X size={13}/>}<span>{name.replaceAll('_', ' ')}</span></div>)}</div>}
        {verification?.performance && <div className="agent-summary">
          <small>Performance: {verification.performance.runs} samples, median {verification.performance.median_latency_ms?.toFixed(2) ?? 'n/a'} ms, p95 {verification.performance.p95_latency_ms.toFixed(2)} ms</small>
          {verification.execution_evidence?.performance_comparison && <small>Baseline p95 {verification.execution_evidence.performance_comparison.baseline_p95_ms?.toFixed(2) ?? 'n/a'} ms · configured limit {verification.execution_evidence.performance_comparison.configured_limit}x · observed ratio {verification.execution_evidence.performance_comparison.regression_ratio?.toFixed(2) ?? 'n/a'}x</small>}
        </div>}
      </article>
    </section>

    <section className="evidence-feature-grid">
      <SchedulePlayer events={baseline?.events ?? events} actors={actors}/>
      <DatabaseBehaviorComparison before={baseline} after={verifiedSchedule}/>
      <LockWaitPanel result={lockEvidence}/>
      <StageDurationPanel timings={report?.stage_timings_ms ?? {}}/>
      <LangSmithPanel status={report?.observability ?? modelStatus?.observability?.langsmith} traceUrl={report?.trace_url}/>
    </section>

    <section className="panel diff-panel">
      <div className="panel-head"><div><span className="kicker">REVIEWABLE BEFORE / AFTER DIFF</span><h2>Model-generated candidate changes</h2></div><Code2 size={20}/></div>
      <pre>{repairView.diff || report?.final_diff || 'No executable candidate diff was generated. Description-only repairs are rejected.'}</pre>
    </section>

    <section className="review-section">
      <ReviewPanel state={reviewState} loading={reviewLoading} submitting={reviewSubmitting} error={reviewError} reviewer={reviewer} comment={reviewComment} onReviewerChange={setReviewer} onCommentChange={setReviewComment} onDecision={decision => { void submitReview(decision) }}/>
    </section>

    <footer><span>RaceProof experimental prototype</span><span>Finite tests establish observed evidence, not formal race freedom.</span>{runId && <span>RUN {runId}</span>}</footer>
  </main>
}

export default App
