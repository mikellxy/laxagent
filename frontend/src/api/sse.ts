import { request } from './http'
import type { StreamEvent } from './types'

export async function consumeSSE(body: ReadableStream<Uint8Array>, onEvent: (event: StreamEvent) => void, signal?: AbortSignal) {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = '', data: string[] = [], ended = false
  const abort = () => { void reader.cancel() }
  signal?.addEventListener('abort', abort, { once: true })
  function line(value: string) {
    if (value === '') {
      if (data.length) {
        const event = JSON.parse(data.join('\n')) as StreamEvent
        onEvent(event)
        if (event.type === 'sse_end') ended = true
      }
      data = []
    } else if (value.startsWith('data:')) data.push(value.slice(5).replace(/^ /, ''))
  }
  try {
    while (!ended) {
      signal?.throwIfAborted()
      const { value, done } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      // Split on LF; a CR may be split across network chunks, so strip it only from a complete line.
      let index: number
      while ((index = buffer.indexOf('\n')) >= 0) {
        line(buffer.slice(0, index).replace(/\r$/, '')); buffer = buffer.slice(index + 1)
      }
      if (done) break
    }
    signal?.throwIfAborted()
    if (!ended) throw new Error('事件连接已断开')
  } finally {
    signal?.removeEventListener('abort', abort)
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}
export async function streamRun(runId: number, onEvent: (event: StreamEvent) => void, signal: AbortSignal) {
  const response = await request(`/runs/${runId}/events`, { signal, headers: { Accept: 'text/event-stream' } })
  if (!response.body) throw new Error('事件连接不可用')
  await consumeSSE(response.body, onEvent, signal)
}
