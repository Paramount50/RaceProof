import { Activity, Clock, Database, ExternalLink, GitCompare, LockKeyhole } from 'lucide-react'
import type { RunEvent, ScheduleResult } from './types'

export function orderedScheduleEvents(events: RunEvent[]) {
  return [...events.filter(event => ['transaction', 'checkpoint'].includes(event.kind))]
    .sort((left, right) => left.sequence - right.sequence)
}

export function evidenceSummary(result: ScheduleResult | null | undefined) {
  if (!result?.invariant) return []
  return Object.entries(result.invariant.evidence).map(([name, value]) => ({
    name: name.replaceAll('_', ' '),
    value: String(value),
  }))
}

export function formatDuration(durationMs: number) {
  return durationMs < 1000 ? `${Math.round(durationMs)} ms` : `${(durationMs / 1000).toFixed(2)} s`
}

export function SchedulePlayer({ events, actors }: { events: RunEvent[]; actors: string[] }) {
  const scheduleEvents = orderedScheduleEvents(events)
  return <article className="panel evidence-feature schedule-player">
    <div className="panel-head"><div><span className="kicker">INTERLEAVING PLAYER</span><h2>Schedule sequence</h2></div><Activity size={20}/></div>
    <div className="schedule-sequence">
      {scheduleEvents.map(event => <div className={`schedule-step ${event.kind}`} key={event.sequence}>
        <span>#{event.sequence}</span><strong>{event.actor ?? 'engine'}</strong><small>{event.action.replaceAll('_', ' ')}</small>
      </div>)}
      {!scheduleEvents.length && <p className="evidence-empty">No schedule events recorded yet.</p>}
    </div>
    {!!actors.length && <div className="schedule-caption"><span>Actors: {actors.join(' + ')}</span></div>}
  </article>
}

export function DatabaseBehaviorComparison({ before, after }: { before?: ScheduleResult | null; after?: ScheduleResult | null }) {
  const columns = [{ label: 'Before repair', result: before }, { label: 'After repair', result: after }]
  return <article className="panel evidence-feature database-comparison">
    <div className="panel-head"><div><span className="kicker">DATABASE BEHAVIOR</span><h2>Before and after</h2></div><GitCompare size={20}/></div>
    <div className="behavior-columns">
      {columns.map(({ label, result }) => <div className="behavior-column" key={label}>
        <span>{label}</span>
        <strong>{result?.invariant ? (result.invariant.passed ? 'Invariant passed' : 'Invariant failed') : 'No result'}</strong>
        {evidenceSummary(result).slice(0, 4).map(item => <small key={item.name}>{item.name}: {item.value}</small>)}
        {result && <small>Outcomes: {Object.entries(result.actor_outcomes).map(([actor, outcome]) => `${actor}=${outcome}`).join(', ')}</small>}
      </div>)}
    </div>
  </article>
}

export function LockWaitPanel({ result }: { result?: ScheduleResult | null }) {
  const status = result?.lock_observation_status ?? 'not_requested'
  return <article className="panel evidence-feature lock-evidence">
    <div className="panel-head"><div><span className="kicker">POSTGRESQL LOCKS</span><h2>Wait relationships</h2></div><LockKeyhole size={20}/></div>
    <div className={`observation-status ${status}`}>Observation: {status.replaceAll('_', ' ')}</div>
    {result?.wait_for_edges.map((edge, index) => <div className="wait-edge" key={`${edge.waiting_actor}-${edge.blocking_actor}-${index}`}>
      <strong>{edge.waiting_actor}</strong><span>waited for</span><strong>{edge.blocking_actor}</strong><small>{edge.lock_mode} {edge.lock_type}{edge.relation ? ` on ${edge.relation}` : ''}</small>
    </div>)}
    {result?.lock_waits.map((wait, index) => <div className="lock-release" key={`${wait.waiting_actor}-${index}`}>
      <Database size={14}/><span>{wait.waiting_actor} waited {formatDuration(wait.duration_ms)}</span><small>{wait.release_points.length ? `Released at ${wait.release_points.join(', ')}` : 'Release event not observed'}</small>
    </div>)}
    {status === 'available' && !result?.wait_for_edges.length && <p className="evidence-empty">No actor wait relationship was observed.</p>}
    {status === 'unavailable' && <p className="evidence-empty">Lock evidence was unavailable. Verification gates were evaluated independently.</p>}
    {result?.deadlock_cycle && <div className="deadlock-warning">Wait-for cycle observed</div>}
  </article>
}

export function StageDurationPanel({ timings }: { timings: Record<string, number> }) {
  const entries = Object.entries(timings)
  return <article className="panel evidence-feature stage-duration-panel">
    <div className="panel-head"><div><span className="kicker">LOCAL TIMINGS</span><h2>Workflow stages</h2></div><Clock size={20}/></div>
    <div className="timing-grid">
      {entries.map(([stage, duration]) => <div key={stage}><span>{stage.replaceAll('_', ' ')}</span><strong>{formatDuration(duration)}</strong></div>)}
      {!entries.length && <p className="evidence-empty">Stage durations appear when a report is complete.</p>}
    </div>
  </article>
}

type ObservabilityStatus = {
  enabled: boolean
  configured: boolean
  project: string
  reason: string
}

export function LangSmithPanel({ status, traceUrl }: { status?: ObservabilityStatus; traceUrl?: string | null }) {
  return <article className="panel evidence-feature observability-panel">
    <div className="panel-head"><div><span className="kicker">OPTIONAL OBSERVABILITY</span><h2>LangSmith</h2></div><ExternalLink size={20}/></div>
    <div className="invariant-stats">
      <div className="metric"><span>Tracing</span><strong>{status?.enabled ? 'Enabled' : 'Disabled'}</strong></div>
      <div className="metric"><span>Configured</span><strong>{status?.configured ? 'Yes' : 'No'}</strong></div>
    </div>
    <div className="agent-summary"><strong>{status?.project ?? 'raceproof-local-dev'}</strong><p>{status?.reason ?? 'Tracing status is unavailable.'}</p><small>Metadata only. Inputs and outputs remain hidden.</small></div>
    {traceUrl && <a className="trace-link" href={traceUrl} target="_blank" rel="noreferrer">Open established trace</a>}
  </article>
}
