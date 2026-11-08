import { describe, expect, it, vi } from 'vitest'
import { waitFor } from '@testing-library/react'
import { Conversation, type Transport } from '../src/features/chat/conversation'
import type { Message, MessagePage, Run, SessionDetail, StreamEvent } from '../src/api/types'

const user = (seq: number, content = `user ${seq}`): Message => ({ seq, role: 'user', content })
const assistant = (seq: number, content: string): Message => ({ seq, role: 'assistant', content })
const page = (items: Message[], has_more = false): MessagePage => ({ items, has_more, next_before_seq: has_more ? items[0].seq : null })
const run = (status: Run['status'] = 'running'): Run => ({ run_id: 10, session_id: 1, model: 'p:m', status, error: null })
const detail = (next_msg_seq = 2, latest_run: Run | null = run()): SessionDetail => ({ id: 1, user_id: 'owner', name: 'session', project_id: 1, next_msg_seq, latest_run })
function deferred<T>() { let resolve!: (value: T) => void, reject!: (error: unknown) => void; const promise = new Promise<T>((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
function harness(initial = detail(), rows: Message[] = [user(1)]) {
  let receive: (event: StreamEvent) => void = () => {}
  const transport: Transport = {
    detail: vi.fn().mockResolvedValue(initial), messages: vi.fn().mockResolvedValue(page(rows)),
    chat: vi.fn().mockResolvedValue({ run_id: 10, status: 'waiting' }),
    stream: vi.fn((_id, callback, signal) => {
      receive = callback
      return new Promise<void>((_resolve, reject) => signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError'))))
    }),
  }
  const controller = new Conversation(1, transport)
  const event = (type: string, seq: number | null, content: StreamEvent['content'] = '', error: string | null = null) => receive({ type, msg_seq: seq, content, run_id: 10, role: 'assistant', error })
  return { controller, transport, event }
}

describe('conversation synchronization', () => {
  it.each(['waiting', 'running'] as const)('subscribes directly for %s without status polling', async status => {
    const h = harness(detail(2, run(status)))
    await h.controller.refresh()
    expect(h.transport.stream).toHaveBeenCalledOnce()
    expect(h.transport.detail).toHaveBeenCalledOnce()
    expect(h.transport.messages).toHaveBeenCalledWith(1, 2, expect.any(AbortSignal))
    h.controller.dispose()
  })
  it.each(['success', 'failed', null] as const)('does not subscribe for %s', async status => {
    const h = harness(detail(2, status ? { ...run(status), error: status === 'failed' ? 'failed' : null } : null))
    await h.controller.refresh()
    expect(h.transport.stream).not.toHaveBeenCalled()
    expect(h.controller.getSnapshot().messages).toEqual([user(1)])
    h.controller.dispose()
  })
  it('uses the detail watermark, appends same-seq chunks, replaces final text, and hides reasoning/results', async () => {
    const h = harness()
    await h.controller.refresh()
    h.event('message_chunk', 2, 'hel')
    await waitFor(() => expect(h.controller.getSnapshot().syncing).toBe(false))
    h.event('message_chunk', 2, 'lo')
    h.event('reasoning_chunk', 2, 'private reasoning')
    expect(h.controller.getSnapshot().messages).toEqual([user(1), assistant(2, 'hello')])
    h.event('message_end', 2, { text: 'Hello!' })
    h.event('tool_start', 3, { tool_name: 'read_file', arguments: '{"path":"hello.txt"}' })
    h.event('tool_end', 3, { tool_name: 'read_file', result: 'private output' })
    expect(h.controller.getSnapshot().messages).toEqual([user(1), assistant(2, 'Hello!'), { seq: 3, role: 'tool', tool_name: 'read_file', arguments: '{"path":"hello.txt"}' }])
    h.event('message_chunk', 1, 'replay')
    expect(h.controller.getSnapshot().messages[0]).toEqual(user(1))
    h.controller.dispose()
  })
  it('buffers events while reading a bounded snapshot and applies only events above its watermark', async () => {
    const h = harness()
    await h.controller.refresh()
    const pending = deferred<MessagePage>()
    vi.mocked(h.transport.detail).mockResolvedValue(detail(4))
    vi.mocked(h.transport.messages).mockReturnValueOnce(pending.promise)
    const syncing = h.controller.refresh(false)
    await waitFor(() => expect(h.transport.messages).toHaveBeenCalledTimes(2))
    h.event('message_chunk', 2, 'duplicate')
    h.event('message_chunk', 4, 'new')
    pending.resolve(page([user(1), assistant(2, 'complete'), user(3)]))
    await syncing
    expect(h.controller.getSnapshot().messages).toEqual([user(1), assistant(2, 'complete'), user(3), assistant(4, 'new')])
    expect(h.transport.messages).toHaveBeenLastCalledWith(1, 4, expect.any(AbortSignal))
    h.controller.dispose()
  })
  it('does not advance its boundary when the page read fails', async () => {
    const h = harness()
    await h.controller.refresh()
    const pending = deferred<MessagePage>()
    vi.mocked(h.transport.detail).mockResolvedValue(detail(4))
    vi.mocked(h.transport.messages).mockReturnValueOnce(pending.promise)
    const syncing = h.controller.refresh(false)
    h.event('message_chunk', 2, 'still visible')
    pending.reject(new Error('offline'))
    await syncing
    expect(h.controller.getSnapshot().messages).toEqual([user(1), assistant(2, 'still visible')])
    h.controller.dispose()
  })
  it('retains a 202 pending user across empty reads and never matches equal old text', async () => {
    const h = harness(detail(3, null), [user(1, 'same'), assistant(2, 'old')])
    await h.controller.refresh()
    expect(await h.controller.send('same', 'p:m')).toBe(true)
    expect(h.controller.getSnapshot().pending?.afterSeq).toBe(2)
    vi.mocked(h.transport.detail).mockResolvedValue(detail(3, run('waiting')))
    await h.controller.refresh()
    expect(h.controller.getSnapshot().pending?.content).toBe('same')
    vi.mocked(h.transport.detail).mockResolvedValue(detail(4))
    vi.mocked(h.transport.messages).mockResolvedValue(page([user(1, 'same'), assistant(2, 'old'), user(3, 'same')]))
    h.event('message_chunk', 4, 'answer')
    await waitFor(() => expect(h.controller.getSnapshot().pending).toBeNull())
    expect(h.controller.getSnapshot().messages.filter(m => m.role === 'user')).toHaveLength(2)
    expect(h.controller.getSnapshot().messages.at(-1)).toEqual(assistant(4, 'answer'))
    expect(h.transport.chat).toHaveBeenCalledOnce()
    h.controller.dispose()
  })
  it('keeps an uncommitted user as a failed local submission', async () => {
    const h = harness(detail(1, null), [])
    await h.controller.refresh(); await h.controller.send('hello', 'p:m')
    vi.mocked(h.transport.detail).mockResolvedValue(detail(1, { ...run('failed'), error: 'Workspace initialization failed' }))
    h.event('sse_end', null, {})
    await waitFor(() => expect(h.controller.getSnapshot().error).toBe('Workspace initialization failed'))
    expect(h.controller.getSnapshot().pending?.failed).toBe(true)
    expect(h.controller.getSnapshot().messages).toEqual([])
    h.controller.dispose()
  })
  it('can send a new message after a failed run', async () => {
    const h = harness(detail(2, { ...run('failed'), error: 'old failure' }))
    await h.controller.refresh()
    expect(await h.controller.send('next', 'p:m')).toBe(true)
    h.controller.dispose()
  })
  it('fills disconnect gaps across pages before discarding transient text', async () => {
    const h = harness()
    await h.controller.refresh()
    vi.mocked(h.transport.detail).mockResolvedValue(detail(101))
    const range = (start: number, end: number) => Array.from({ length: end - start + 1 }, (_, n) => user(n + start))
    vi.mocked(h.transport.messages).mockResolvedValueOnce(page(range(80, 100), true))
      .mockResolvedValueOnce(page(range(30, 79), true)).mockResolvedValueOnce(page(range(1, 29)))
    await h.controller.refresh()
    expect(h.controller.getSnapshot().messages).toHaveLength(100)
    expect(vi.mocked(h.transport.messages).mock.calls.slice(1).map(c => c[1])).toEqual([101, 80, 30])
    expect(h.controller.getSnapshot().hasMore).toBe(false)
    h.controller.dispose()
  })
  it('silently loads earlier pages without changing the durable watermark', async () => {
    const h = harness(detail(6, null), [user(4), user(5)])
    vi.mocked(h.transport.messages).mockResolvedValueOnce(page([user(4), user(5)], true))
    await h.controller.refresh()
    vi.mocked(h.transport.messages).mockResolvedValueOnce(page([user(1), user(2), user(3)]))
    await h.controller.loadOlder()
    expect(h.controller.getSnapshot().messages.map(m => m.seq)).toEqual([1, 2, 3, 4, 5])
    expect(h.transport.messages).toHaveBeenLastCalledWith(1, 4, expect.any(AbortSignal))
    h.controller.dispose()
  })
  it('ignores responses and events after the selected session is disposed', async () => {
    const h = harness()
    await h.controller.refresh()
    const previous = h.controller.getSnapshot()
    h.controller.dispose(); h.event('message_chunk', 2, 'late')
    expect(h.controller.getSnapshot()).toBe(previous)
  })
})
