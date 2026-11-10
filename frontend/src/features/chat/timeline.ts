import type { Message, TextMessage, ToolMessage } from '../../api/types'
import type { Activity } from './conversation'

export type ProcessItem = { kind: 'thinking'; message: TextMessage; active: boolean }
  | { kind: 'tool'; message: ToolMessage; active: boolean }
export interface WorkGroup { kind: 'work'; key: string; seq: number; items: ProcessItem[]; active: boolean }
export type TimelineRow = WorkGroup | { kind: 'message'; key: string; seq: number; message: TextMessage }

/** Only preview newline-terminated text while a reasoning item is streaming. */
export function latestWorkLine(group: WorkGroup): string | null {
  for (const item of [...group.items].reverse()) {
    if (item.kind === 'tool') {
      if (item.message.tool_name.trim()) return item.message.tool_name.trim()
      continue
    }
    const parts = item.message.reasoning ?? []
    for (let index = parts.length - 1; index >= 0; index--) {
      const lines = parts[index].text.split(/\r\n|[\r\n]/)
      if (item.active && index === parts.length - 1) lines.pop()
      for (let line = lines.length - 1; line >= 0; line--) {
        if (lines[line].trim()) return lines[line].trim()
      }
    }
  }
  return null
}

/** Body text and user messages delimit work; empty assistant messages do not. */
export function groupTimeline(messages: Message[], activity: Activity | null): TimelineRow[] {
  const rows: TimelineRow[] = []
  let group: WorkGroup | null = null
  let boundary = 'start'
  function process(item: ProcessItem) {
    if (!group) {
      group = { kind: 'work', key: `work:${boundary}`, seq: item.message.seq, items: [], active: false }
      rows.push(group)
    }
    group.items.push(item)
  }
  for (const message of messages) {
    if (message.role === 'tool') {
      process({ kind: 'tool', message, active: activity?.toolSeq === message.seq })
    } else {
      if (message.role === 'assistant' && message.reasoning?.some(part => part.text)) {
        process({ kind: 'thinking', message, active: activity?.thinkingSeq === message.seq })
      }
      if (message.role === 'user' || message.content) {
        rows.push({ kind: 'message', key: `message:${message.seq}`, seq: message.seq, message })
        boundary = String(message.seq)
        group = null
      }
    }
  }
  const tail = rows.at(-1)
  if (tail?.kind === 'work' && activity) tail.active = tail.items.some(item => item.message.seq >= activity.firstSeq)
  return rows
}
