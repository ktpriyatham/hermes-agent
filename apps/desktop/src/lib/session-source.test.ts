import { describe, expect, it } from 'vitest'

import { isKanbanWorkerSession, partitionKanbanWorkerSessions } from './session-source'

function session(overrides: Record<string, unknown> = {}) {
  return {
    cwd: '/Users/example/project',
    preview: 'ordinary conversation',
    source: 'desktop',
    title: 'Project chat',
    ...overrides
  }
}

describe('isKanbanWorkerSession', () => {
  it('classifies future dispatcher sessions by their dedicated source', () => {
    expect(isKanbanWorkerSession(session({ source: 'kanban' }))).toBe(true)
  })

  it('classifies existing dispatcher sessions by their exact worker prompt', () => {
    expect(isKanbanWorkerSession(session({ preview: 'work kanban task t_a7db34ac', title: null }))).toBe(true)
    expect(isKanbanWorkerSession(session({ preview: null, title: 'work kanban task t_cb1baf0d' }))).toBe(true)
  })

  it('does not classify ordinary Kanban conversations or similarly named work', () => {
    expect(isKanbanWorkerSession(session({ title: 'Kanban planning' }))).toBe(false)
    expect(isKanbanWorkerSession(session({ preview: 'work on kanban task categories' }))).toBe(false)
  })
})

describe('partitionKanbanWorkerSessions', () => {
  it('removes worker runs from conversations while preserving order', () => {
    const result = partitionKanbanWorkerSessions([
      { id: 'run-old', preview: 'work kanban task t_a7db34ac', source: 'desktop', title: null },
      { id: 'chat', preview: 'ordinary conversation', source: 'desktop', title: null },
      { id: 'run-new', preview: 'worker output', source: 'kanban', title: null }
    ])

    expect(result.conversations.map(item => item.id)).toEqual(['chat'])
    expect(result.kanbanRuns.map(item => item.id)).toEqual(['run-old', 'run-new'])
  })
})
