import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ProjectSourcePanel } from './ProjectSourcePanel'
import type { ExternalProject } from './types'

const handlers = {
  onConnectLocal: async () => undefined,
  onUpload: async () => undefined,
  onRemove: async () => true,
}

describe('project source selector', () => {
  it('keeps built-in examples as the default', () => {
    const html = renderToStaticMarkup(<ProjectSourcePanel project={null} loading={false} error={null} {...handlers}/>)
    expect(html).toContain('Booking Double Reservation and Inventory Oversell')
    expect(html).toContain('Built-in examples')
  })

  it('shows inspection-only evidence and removal controls for an upload', () => {
    const project: ExternalProject = {
      id: 'a1b2c3d4e5f6', name: 'Uploaded app', source: 'upload', root_path: 'isolated/import',
      files_discovered: 2, supported_files: ['operations.py'], warnings: [], sqlalchemy_detected: true,
      postgresql_configuration_detected: false, onboarding_state: 'INSPECTION_ONLY', ready_for_experiment: false,
      missing_requirements: ['Provide and validate a target adapter.'],
      findings: [{ file: 'operations.py', line_start: 4, line_end: 9, function: 'reserve', severity: 'high', signals: ['row read', 'no obvious row lock'], explanation: 'Static hypothesis.' }],
    }
    const html = renderToStaticMarkup(<ProjectSourcePanel project={project} loading={false} error={null} {...handlers}/>)
    expect(html).toContain('Remove uploaded files')
    expect(html).toContain('Inspection only')
    expect(html).toContain('reserve · operations.py:4')
    expect(html).toContain('Back to examples')
  })
})
