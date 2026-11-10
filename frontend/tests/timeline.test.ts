import { describe, expect, it } from 'vitest'
import type { Message } from '../src/api/types'
import { groupTimeline, latestWorkLine, type WorkGroup } from '../src/features/chat/timeline'

const thought = (seq: number, content = ''): Message => ({ seq, role: 'assistant', content, reasoning: [{ channel: 'text', text: 'thinking' }] })
const tool = (seq: number): Message => ({ seq, role: 'tool', tool_name: 'read_file', arguments: '{}' })

describe('work grouping', () => {
  it('keeps body text outside and reasoning before it, grouping tools and empty-body assistants', () => {
    const rows = groupTimeline([
      { seq: 1, role: 'user', content: 'question' }, thought(2, '我先检查相关文件。'),
      tool(3), { seq: 4, role: 'assistant', content: '' }, tool(5), thought(6, 'result'),
    ], { firstSeq: 2, thinkingSeq: null, toolSeq: null })
    expect(rows.map(row => [row.kind, row.seq])).toEqual([
      ['message', 1], ['work', 2], ['message', 2], ['work', 3], ['message', 6],
    ])
    expect(rows[3]).toMatchObject({ active: false, items: [{ kind: 'tool' }, { kind: 'tool' }, { kind: 'thinking' }] })
  })
  it('only activates an unclosed group belonging to the current run', () => {
    const messages = [thought(1), tool(2)]
    expect(groupTimeline(messages, { firstSeq: 1, thinkingSeq: null, toolSeq: 2 })[0]).toMatchObject({ active: true })
    expect(groupTimeline(messages, { firstSeq: 3, thinkingSeq: 3, toolSeq: null })[0]).toMatchObject({ active: false })
    expect(groupTimeline(messages, null)[0]).toMatchObject({ active: false })
  })
  it('handles leading partial pages and user boundaries without empty groups', () => {
    const rows = groupTimeline([tool(5), thought(6), { seq: 7, role: 'user', content: 'next' },
      { seq: 8, role: 'assistant', content: '', reasoning: [] }, thought(9)], null)
    expect(rows.map(row => [row.kind, row.seq])).toEqual([['work', 5], ['message', 7], ['work', 9]])
    expect(groupTimeline([{ seq: 1, role: 'assistant', content: 'simple answer' }], null)).toHaveLength(1)
  })
})

it('previews only the latest complete nonempty line, including a completed final tail', () => {
  const group: WorkGroup = { kind: 'work', key: 'work:1', seq: 2, active: true, items: [
    { kind: 'thinking', active: true, message: { seq: 2, role: 'assistant', content: '',
      reasoning: [{ channel: 'text', text: '第一行\r\n第二行\n\n未完成' }] } },
  ] }
  expect(latestWorkLine(group)).toBe('第二行')
  group.items[0].active = false
  expect(latestWorkLine(group)).toBe('未完成')
  group.items.push({ kind: 'tool', active: true, message: { seq: 3, role: 'tool', tool_name: 'read_file', arguments: '{}' } })
  expect(latestWorkLine(group)).toBe('read_file')
  group.items.push({ kind: 'thinking', active: true, message: { seq: 4, role: 'assistant', content: '',
    reasoning: [{ channel: 'text', text: '新的半行' }] } })
  expect(latestWorkLine(group)).toBe('read_file')
})

it('does not preview an initial partial line and accepts the end of an earlier reasoning channel', () => {
  const message: Message = { seq: 1, role: 'assistant', content: '', reasoning: [{ channel: 'text', text: 'partial' }] }
  const group = groupTimeline([message], { firstSeq: 1, thinkingSeq: 1, toolSeq: null })[0] as WorkGroup
  expect(latestWorkLine(group)).toBeNull()
  message.reasoning!.push({ channel: 'summary', text: 'new partial' })
  expect(latestWorkLine(group)).toBe('partial')
})
