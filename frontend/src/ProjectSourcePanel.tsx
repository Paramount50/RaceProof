import { useEffect, useState } from 'react'
import { Code2, FileArchive, FolderOpen, Trash2, X } from 'lucide-react'
import type { ExternalProject } from './types'

type SourceMode = 'examples' | 'upload'

type Props = {
  project: ExternalProject | null
  loading: boolean
  error: string | null
  onUpload: (files: File[], name: string | null) => Promise<void>
  onRemove: () => Promise<boolean>
}

export function ProjectSourcePanel({ project, loading, error, onUpload, onRemove }: Props) {
  const [mode, setMode] = useState<SourceMode>(project?.source ?? 'examples')
  const [projectName, setProjectName] = useState('')
  const [files, setFiles] = useState<File[]>([])

  useEffect(() => {
    if (project) setMode(project.source)
  }, [project])

  const backToExamples = async () => {
    if (project && !await onRemove()) return
    setMode('examples')
    setFiles([])
  }

  return <section className="panel source-panel">
    <div className="panel-head"><div><span className="kicker">APPLICATION SOURCE</span><h2>Choose what RaceProof inspects</h2></div><FolderOpen size={20}/></div>
    <div className="source-tabs">
      <button className={mode === 'examples' ? 'active' : ''} onClick={() => { void backToExamples() }} disabled={loading}><Code2 size={15}/>Built-in examples</button>
      <button className={mode === 'upload' ? 'active' : ''} onClick={() => setMode('upload')} disabled={loading || Boolean(project)}><FileArchive size={15}/>Upload files</button>
    </div>
    {error && <div className="error-banner"><X size={15}/><span>{error}</span></div>}
    {!project && mode === 'examples' && <div className="source-copy"><strong>Booking Double Reservation and Inventory Oversell</strong><p>The validated built-in targets remain active. Select either example from the header and run the existing workflow.</p></div>}
    {!project && mode === 'upload' && <div className="source-form">
      <label>Project name<input value={projectName} onChange={event => setProjectName(event.target.value)} placeholder="Optional display name" /></label>
      <label>Python files or ZIP archive<input type="file" accept=".py,.zip" multiple onChange={event => setFiles(Array.from(event.target.files ?? []))} /></label>
      <small>{files.length ? `${files.length} file${files.length === 1 ? '' : 's'} selected` : 'No files selected'}</small>
      <button className="source-primary" disabled={loading || !files.length} onClick={() => { void onUpload(files, projectName.trim() || null) }}>Upload and inspect</button>
    </div>}
    {project && <div className="active-project">
      <div className="active-project-head"><div><span>ISOLATED UPLOAD</span><strong>{project.name}</strong><small>{project.root_path}</small></div><button className="remove-project" onClick={() => { void backToExamples() }} disabled={loading}><Trash2 size={14}/>Remove uploaded files</button></div>
      <div className="project-metrics"><div><span>Python files</span><strong>{project.supported_files.length}</strong></div><div><span>Suspicious operations</span><strong>{project.findings.length}</strong></div><div><span>SQLAlchemy</span><strong>{project.sqlalchemy_detected ? 'Detected' : 'Not detected'}</strong></div><div><span>PostgreSQL config</span><strong>{project.postgresql_configuration_detected ? 'Source evidence' : 'Not detected'}</strong></div></div>
      <div className="inspection-state"><strong>Inspection only</strong><p>Static findings are hypotheses. This project cannot run until an adapter, approved invariant, schedule, actor inputs, and a disposable experiment database are validated.</p></div>
      <div className="finding-list">{project.findings.slice(0, 20).map((finding, index) => <div className="finding" key={`${finding.file}-${finding.line_start}-${index}`}><strong>{finding.function ?? 'module'} · {finding.file}:{finding.line_start}</strong><span>{finding.signals.join(' · ')}</span><p>{finding.explanation}</p></div>)}{!project.findings.length && <p className="evidence-empty">No suspicious operation matched the bounded static patterns. This does not prove the code is race-free.</p>}</div>
      <div className="missing-requirements"><strong>Required before controlled execution</strong>{project.missing_requirements.map(item => <span key={item}>{item}</span>)}</div>
      <button className="back-examples" onClick={() => { void backToExamples() }} disabled={loading}>Back to examples</button>
    </div>}
  </section>
}
