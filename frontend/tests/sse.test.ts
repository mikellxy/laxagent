import { expect, it, vi } from 'vitest'
import { consumeSSE } from '../src/api/sse'

it('decodes split UTF-8 and CRLF frames, ignoring heartbeats and keeping same-seq deltas', async () => {
  const events = [
    { type: 'message_chunk', msg_seq: 2, content: '你好' },
    { type: 'message_chunk', msg_seq: 2, content: '世界' },
    { type: 'sse_end', msg_seq: null, content: {} },
  ]
  const bytes = new TextEncoder().encode(': heartbeat\r\n\r\n' + events.map(e => `id: 1\r\nevent: ${e.type}\r\ndata: ${JSON.stringify(e)}\r\n\r\n`).join(''))
  const stream = new ReadableStream<Uint8Array>({ start(c) { for (const byte of bytes) c.enqueue(new Uint8Array([byte])); c.close() } })
  const seen = vi.fn()
  await consumeSSE(stream, seen)
  expect(seen.mock.calls.map(c => c[0])).toEqual(events)
})
it('detects a connection ending without sse_end', async () => {
  await expect(consumeSSE(new ReadableStream({ start(c) { c.close() } }), () => {})).rejects.toThrow('事件连接已断开')
})
