import { api } from '../../api/resources'
import { ApiError, messageOf } from '../../api/errors'
import type { Answer, PendingQuestion, Question, AppendixDisplay, Message, ReasoningPart, Run, SessionDetail, StreamEvent, ToolMessage, ToolStatus } from '../../api/types'

export type Transport = Pick<typeof api, 'detail' | 'messages' | 'chat' | 'stream' | 'cancelRun' | 'resumeRun' | 'getRun' | 'answerRun'>
export interface PendingMessage { content: string; appendix: AppendixDisplay[]; afterSeq: number; runId: number | null; failed: boolean }
export interface Activity { firstSeq: number; thinkingSeq: number | null; toolSeq: number | null }
interface StreamingTool {
  key: string; parentSeq: number; seq: number | null; name: string; arguments: string
  status: ToolStatus; lastEventId?: bigint
}
export interface ConversationSnapshot {
  messages: Message[]; pending: PendingMessage | null; detail: SessionDetail | null
  generatingTools: ToolMessage[]
  busy: boolean; ready: boolean; syncing: boolean; loadingOlder: boolean; hasMore: boolean; cancelling: boolean
  error: string | null; missing: boolean
  activity: Activity | null
  resuming: boolean
  question: PendingQuestion | null; answering: boolean
}
const active = (run: Run | null | undefined) => run?.status === 'waiting' || run?.status === 'running'
const occupied = (run: Run | null | undefined) => active(run) || run?.status === 'paused'
const attempt = (run: Run | null | undefined) => run ? `${run.run_id}:${run.resume_time}` : null

/** One selected session owns one stream; durable rows and transient chunks never share storage. */
export class Conversation {
  private history = new Map<number, Message>()
  private live = new Map<number, Message>()
  private toolCalls = new Map<string, StreamingTool>()
  private boundary = 0
  private detail: SessionDetail | null = null
  private pending: PendingMessage | null = null
  private question: PendingQuestion | null = null
  private answering = false
  private pausedStreamRead: string | null = null
  private hasMore = false
  private ready = false
  private submitting = false
  private cancelling = false
  private resuming = false
  private syncFailed = false
  private syncing = false
  private loadingOlder = false
  private error: string | null = null
  private missing = false
  private disposed = false
  private lifetime = new AbortController()
  private streamAbort: AbortController | null = null
  private syncFlight: Promise<void> | null = null
  private buffered: StreamEvent[] = []
  private reconnectWanted = false
  private checkedRun: string | null = null
  private activity: Activity | null = null
  private retry: ReturnType<typeof setTimeout> | null = null
  private retryDelay = 500
  private listeners = new Set<() => void>()
  private snapshot: ConversationSnapshot = { messages: [], generatingTools: [], pending: null, detail: null, busy: false, ready: false,
    syncing: false, loadingOlder: false, hasMore: false, error: null, missing: false, cancelling: false, resuming: false, activity: null, question: null, answering: false }

  constructor(readonly sessionId: number, private transport: Transport = api) {}
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  getSnapshot = () => this.snapshot
  private emit() {
    if (this.disposed) return
    const messages = new Map([...this.history, ...this.live])
    const generatingTools: ToolMessage[] = []
    let callOrder = 0
    for (const call of this.toolCalls.values()) {
      const identity = { call_key: call.key, status: call.status, parent_seq: call.parentSeq, call_order: callOrder++ }
      if (call.seq === null) {
        generatingTools.push({ seq: call.parentSeq, role: 'tool', tool_name: call.name,
          arguments: call.arguments, ...identity })
      } else {
        const message = messages.get(call.seq)
        if (message?.role === 'tool') messages.set(call.seq, { ...message, ...identity })
      }
    }
    this.snapshot = {
      generatingTools,
      messages: [...messages.values()].sort((a, b) => a.seq - b.seq), pending: this.pending ? { ...this.pending } : null,
      detail: this.detail, busy: this.submitting || this.cancelling || this.resuming || this.answering || occupied(this.detail?.latest_run), ready: this.ready,
      cancelling: this.cancelling, resuming: this.resuming, question: this.question, answering: this.answering,
      activity: active(this.detail?.latest_run) && this.activity ? { ...this.activity } : null,
      syncing: this.syncing, loadingOlder: this.loadingOlder, hasMore: this.hasMore,
      error: this.error ?? (this.pending?.failed && this.detail?.latest_run?.status !== 'failed' ? "消息提交失败" : null), missing: this.missing,
    }
    this.listeners.forEach(listener => listener())
  }
  private reconcilePending() {
    if (this.pending && [...this.history.values()].some(m => m.role === 'user' && m.seq > this.pending!.afterSeq && m.content === this.pending!.content &&
      JSON.stringify((m.appendix ?? []).map(a => a.id)) === JSON.stringify(this.pending!.appendix.map(a => a.id)))) {
      this.pending = null
    }
  }

  refresh = (reconnect = true): Promise<void> => {
    if (this.disposed) return Promise.resolve()
    if (reconnect) {
      this.reconnectWanted = true
      this.streamAbort?.abort()
      this.buffered = []
    }
    if (this.syncFlight) return this.syncFlight
    this.syncing = true; this.emit()
    const work = async () => {
      try {
        const detail = await this.transport.detail(this.sessionId, this.lifetime.signal)
        const boundary = detail.next_msg_seq - 1
        let page = await this.transport.messages(this.sessionId, boundary + 1, this.lifetime.signal)
        const rows = [...page.items]
        const previousOldest = this.history.size ? Math.min(...this.history.keys()) : null
        // Fill a disconnect gap even if more than one page was committed while offline.
        while (previousOldest !== null && page.has_more && page.next_before_seq !== null && page.next_before_seq > this.boundary + 1) {
          page = await this.transport.messages(this.sessionId, page.next_before_seq, this.lifetime.signal)
          rows.push(...page.items)
        }
        if (this.disposed) return false
        for (const row of rows) this.history.set(row.seq, row)
        if (previousOldest === null || (rows.length && Math.min(...rows.map(m => m.seq)) <= previousOldest)) this.hasMore = page.has_more
        this.boundary = boundary // Only advance after ALL required page reads succeeded.
        if (attempt(detail.latest_run) !== attempt(this.detail?.latest_run)) {
          // Another tab may resume the same run during an ordinary history read.
          this.reconnectWanted = true; this.streamAbort?.abort(); this.buffered = []; this.checkedRun = null
          this.clearUnfinishedTools()
        }
        for (const call of this.toolCalls.values()) {
          if (call.status === 'running' && call.seq !== null && this.history.has(call.seq)) call.status = 'completed'
        }
        this.finishToolCalls(detail.latest_run)
        for (const seq of this.live.keys()) if (seq <= boundary || this.reconnectWanted || !active(detail.latest_run)) this.live.delete(seq)
        this.syncFailed = false
        if (attempt(detail.latest_run) !== attempt(this.detail?.latest_run) || !active(detail.latest_run)) this.activity = null
        if (this.activity) {
          if (this.activity.thinkingSeq !== null && this.activity.thinkingSeq <= boundary) this.activity.thinkingSeq = null
          if (this.activity.toolSeq !== null && this.activity.toolSeq <= boundary) this.activity.toolSeq = null
        }
        if (this.question && (detail.latest_run?.status !== 'paused' ||
            attempt(detail.latest_run) !== `${this.question.run_id}:${this.question.resume_time}`)) this.question = null
        this.detail = detail; this.ready = true; this.error = null
        this.reconcilePending()
        if (this.pending && detail.latest_run?.status === 'failed') this.pending.failed = true
        if (detail.latest_run?.status === 'cancelled') this.pending = null
        return true
      } catch (error) {
        if (!this.disposed) {
          this.syncFailed = true
          this.error = messageOf(error)
          this.missing = error instanceof ApiError && error.status === 404
        }
        return false
      }
    }
    this.syncFlight = work().then(ok => {
      this.syncFlight = null; this.syncing = false
      if (this.disposed) return
      const queued = this.buffered.splice(0)
      for (const event of queued) this.apply(event)
      if (!this.syncFlight && this.reconnectWanted) {
        if (ok) { this.reconnectWanted = false; this.connect() }
        else if (!this.missing) this.scheduleReconnect()
      }
      this.emit()
    })
    return this.syncFlight
  }

  private connect() {
    this.streamAbort?.abort()
    if (this.retry) { clearTimeout(this.retry); this.retry = null }
    const run = this.detail?.latest_run
    if (this.disposed || !run || !occupied(run) || (run.status === 'paused' && this.pausedStreamRead === attempt(run))) return
    const abort = new AbortController()
    this.streamAbort = abort
    void this.transport.stream(run.run_id, event => {
      if (this.disposed || abort.signal.aborted || event.run_id !== run.run_id || (event.resume_time ?? 0) !== run.resume_time) return
      this.retryDelay = 500
      // The first model/tool event proves preparation has progressed; read the user row once.
      if (event.msg_seq !== null && this.checkedRun !== attempt(run)) {
        this.checkedRun = attempt(run)
        void this.refresh(false)
      }
      if (this.syncing) this.buffered.push(event)
      else this.apply(event)
    }, abort.signal, run.resume_time).catch(error => {
      if (this.disposed || abort.signal.aborted) return
      this.error = messageOf(error); this.emit(); this.scheduleReconnect()
    })
  }
  private scheduleReconnect() {
    if (this.disposed || this.retry) return
    this.retry = setTimeout(() => { this.retry = null; void this.refresh(true) }, this.retryDelay)
    this.retryDelay = Math.min(this.retryDelay * 2, 5000)
  }
  private finishToolCalls(run: Run | null) {
    if (!run || occupied(run)) return
    for (const [key, call] of this.toolCalls) {
      if (call.status === 'generating') {
        if (run.status === 'success') this.toolCalls.delete(key)
        else call.status = 'interrupted'
      } else if (call.status === 'running') call.status = 'unknown'
    }
  }
  private clearUnfinishedTools() {
    // Keep committed call identities so older cards retain their disclosure state
    // when another run starts. Unfinished previews belong only to their attempt.
    for (const [key, call] of this.toolCalls) {
      if (call.seq === null || !this.history.has(call.seq)) this.toolCalls.delete(key)
    }
  }
  private applyToolEvent(event: StreamEvent, seq: number, content: Record<string, unknown>) {
    const id = content.tool_call_id
    if (typeof id !== 'string' || !id) return
    const key = `${event.run_id}:${event.resume_time ?? 0}:${id}`
    let call = this.toolCalls.get(key)
    if (event.type === 'tool_call_start' || event.type === 'tool_call_delta') {
      // Committed history owns old turns. Keep existing previews until tool_start
      // reconciles them, but never recreate historical previews during replay.
      if ((!call && seq <= this.boundary) || !active(this.detail?.latest_run)) return
      const eventId = event.event_id && /^\d+$/.test(event.event_id) ? BigInt(event.event_id) : undefined
      if (call && eventId !== undefined && call.lastEventId !== undefined && eventId <= call.lastEventId) return
      if (call && call.status !== 'generating') return
      if (event.type === 'tool_call_start') {
        if (!call) {
          call = { key, parentSeq: seq, seq: null, name: String(content.tool_name ?? ''),
            arguments: '', status: 'generating' }
          this.toolCalls.set(key, call)
        }
      } else if (call && typeof content.arguments_delta === 'string') call.arguments += content.arguments_delta
      if (call && eventId !== undefined) call.lastEventId = eventId
    } else if (event.type === 'tool_start') {
      // The authoritative complete arguments replace the transient JSON fragments.
      this.toolCalls.set(key, { key, parentSeq: call?.parentSeq ?? seq, seq,
        name: String(content.tool_name ?? ''), arguments: String(content.arguments ?? ''),
        status: seq <= this.boundary ? 'completed' : 'running', lastEventId: call?.lastEventId })
    } else if (event.type === 'tool_end' && call) {
      call.status = content.error_code ? 'failed' : 'completed'
    }
  }
  private apply(event: StreamEvent) {
    const run = this.detail?.latest_run
    if (!run || event.run_id !== run.run_id || (event.resume_time ?? 0) !== run.resume_time) return
    if (event.type === 'ask_user_question') { void this.receiveQuestion(event); return }
    if (event.type === 'sse_end') {
      const paused = typeof event.content === 'object' && event.content.status === 'paused'
      if (paused) this.pausedStreamRead = attempt(run)
      void this.refresh(!paused); return
    }
    if (event.type === 'error' || event.type === 'response_incomplete') {
      this.activity = null
      this.error = event.error ?? '运行失败'; this.emit(); return
    }
    const seq = event.msg_seq
    if (seq === null) return
    if (!active(run)) return
    const content = typeof event.content === 'object' ? event.content : {}
    if (event.type === 'tool_end' && content.tool_name === 'ask_user_question') {
      if (!content.error_code && typeof content.result === 'object' && content.result !== null &&
          'answers' in content.result && Array.isArray(content.result.answers) && seq > this.boundary) {
        this.live.set(seq, { seq, role: 'tool', tool_name: 'ask_user_question', arguments: '',
          answers: content.result.answers as Answer[] })
      }
      if (this.activity?.toolSeq === seq) this.activity.toolSeq = null
      this.emit(); return
    }
    this.applyToolEvent(event, seq, content)
    // Replayed committed events identify this run's range, but never restart its completed phases.
    if (active(this.detail?.latest_run)) {
      this.activity ??= { firstSeq: seq, thinkingSeq: null, toolSeq: null }
      this.activity.firstSeq = Math.min(this.activity.firstSeq, seq)
    }
    if (seq <= this.boundary) { this.emit(); return }
    const previous = this.live.get(seq)
    const old = previous?.role === 'assistant' ? previous : { seq, role: 'assistant' as const, content: '', complete: false }
    if (event.type === 'message_chunk' || event.type === 'refusal_chunk') {
      const text = old.content + (typeof event.content === 'string' ? event.content : '')
      this.live.set(seq, { ...old, content: text, complete: false })
      if (text && this.activity) this.activity.thinkingSeq = null
    } else if (event.type === 'tool_call_start' || event.type === 'tool_call_delta') {
      if (this.activity) this.activity.thinkingSeq = null
    } else if (event.type === 'reasoning_chunk') {
      const text = typeof event.content === 'string' ? event.content : ''
      if (text) {
        const parts = (old.reasoning ?? []).map(part => ({ ...part }))
        const channel = event.channel ?? 'text'
        const last = parts.at(-1)
        if (last?.channel === channel) last.text += text
        else parts.push({ channel, text })
        this.live.set(seq, { ...old, reasoning: parts })
        if (this.activity && !old.content) this.activity.thinkingSeq = seq
      }
    } else if (event.type === 'message_end') {
      this.live.set(seq, { ...old, complete: true, content: typeof content.text === 'string' ? content.text : '',
        ...(Array.isArray(content.reasoning) ? { reasoning: content.reasoning as ReasoningPart[] } : {}) })
      if (this.activity) this.activity.thinkingSeq = null
    } else if (event.type === 'tool_start') {
      this.live.set(seq, { seq, role: 'tool', tool_name: String(content.tool_name ?? ''), arguments: String(content.arguments ?? '') })
      if (this.activity) { this.activity.toolSeq = seq; this.activity.thinkingSeq = null }
    } else if (event.type === 'tool_end') {
      if (this.activity?.toolSeq === seq) this.activity.toolSeq = null
    }
    // Tool outputs and usage never create additional visible rows.
    this.emit()
  }

  private async receiveQuestion(event: StreamEvent) {
    const content = typeof event.content === 'object' ? event.content : {}
    if (typeof content.interrupt_id !== 'string' || typeof content.tool_call_id !== 'string' || !Array.isArray(content.questions)) return
    try {
      const current = await this.transport.getRun(event.run_id, this.lifetime.signal)
      if (this.disposed || attempt(this.detail?.latest_run) !== attempt(current) ||
          current.resume_time !== (event.resume_time ?? 0)) return
      if (current.status !== 'paused') { await this.refresh(true); return }
      this.detail = { ...this.detail!, latest_run: current }
      this.activity = null
      if (this.question?.interrupt_id !== content.interrupt_id) this.question = {
        run_id: current.run_id, resume_time: current.resume_time, interrupt_id: content.interrupt_id,
        tool_call_id: content.tool_call_id, questions: content.questions as Question[],
      }
      this.emit()
    } catch (error) {
      if (!this.disposed) { this.error = messageOf(error); this.emit() }
    }
  }

  answer = async (answers: Answer[]): Promise<void> => {
    const question = this.question
    if (!question || this.disposed || this.answering || this.cancelling) return
    this.answering = true; this.error = null; this.emit()
    try {
      const submitted = await this.transport.answerRun(question.run_id, question.resume_time,
        question.interrupt_id, answers, this.lifetime.signal)
      if (this.syncFlight) await this.syncFlight
      if (this.disposed) return
      this.question = null
      this.streamAbort?.abort(); this.buffered = []; this.activity = null; this.checkedRun = null
      this.clearUnfinishedTools()
      this.detail = { ...this.detail!, latest_run: { ...this.detail!.latest_run!, ...submitted } }
      await this.refresh(true)
    } catch (error) {
      if (!this.disposed) {
        // Reconcile a lost response without automatically submitting twice.
        await this.refresh(true)
        if (!this.disposed) this.error = messageOf(error)
      }
    } finally { this.answering = false; this.emit() }
  }

  loadOlder = async () => {
    if (this.disposed || this.loadingOlder || !this.hasMore || !this.history.size || this.syncing) return
    this.loadingOlder = true; this.emit()
    try {
      const before = Math.min(...this.history.keys())
      const page = await this.transport.messages(this.sessionId, before, this.lifetime.signal)
      if (this.disposed) return
      for (const row of page.items) this.history.set(row.seq, row)
      this.hasMore = page.has_more
    } catch (error) { if (!this.disposed) this.error = messageOf(error) }
    finally { this.loadingOlder = false; this.emit() }
  }

  send = async (content: string, model: string, appendix: AppendixDisplay[] = []): Promise<boolean> => {
    if (this.disposed || !this.ready || this.submitting || this.cancelling || this.resuming || this.answering || occupied(this.detail?.latest_run) || (!content.trim() && !appendix.length)) return false
    this.submitting = true; this.error = null; this.emit()
    try {
      // Establish a fresh lower bound so repeated text cannot match old historical prompts.
      await this.refresh(false)
      if (this.disposed) return false
      if (occupied(this.detail?.latest_run)) { this.connect(); return false }
      if (this.syncFailed) throw new Error(this.error ?? "读取会话失败")
      this.error = null
      this.pending = { content, appendix, afterSeq: this.boundary, runId: null, failed: false }; this.emit()
      const submitted = await this.transport.chat(this.sessionId, content, model, this.lifetime.signal, appendix)
      if (this.disposed) return false
      if (this.pending) this.pending.runId = submitted.run_id
      this.activity = null
      this.clearUnfinishedTools()
      this.detail = { ...this.detail!, latest_run: { ...submitted, model, error: null, error_code: null, error_desc: null } }
      this.checkedRun = null
      this.connect() // 202 guarantees the stream exists, not that the user message has committed.
      return true
    } catch (error) {
      if (!this.disposed) {
        this.error = messageOf(error)
        if (this.pending) this.pending.failed = true
        // Read-only reconciliation can recover an accepted POST whose response was lost.
        void this.refresh(true)
      }
      return false
    } finally { this.submitting = false; this.emit() }
  }

  resume = async (): Promise<void> => {
    const run = this.detail?.latest_run
    if (this.disposed || !this.ready || this.submitting || this.cancelling || this.resuming || !run ||
        !['failed', 'cancelled'].includes(run.status)) return
    this.resuming = true; this.error = null; this.emit()
    try {
      if (this.syncFlight) await this.syncFlight
      if (this.disposed) return
      const submitted = await this.transport.resumeRun(run.run_id, run.resume_time, this.lifetime.signal)
      if (this.syncFlight) await this.syncFlight
      if (this.disposed) return
      this.streamAbort?.abort(); this.buffered = []; this.live.clear(); this.activity = null; this.checkedRun = null
      this.clearUnfinishedTools()
      if (this.pending) this.pending.failed = false
      this.detail = { ...this.detail!, latest_run: { ...run, ...submitted, error: null, error_code: null, error_desc: null } }
      await this.refresh(true)
    } catch (error) {
      if (!this.disposed) {
        // Reconcile an accepted POST with a lost response; never resubmit automatically.
        await this.refresh(true)
        if (!this.disposed) this.error = messageOf(error)
      }
    } finally { this.resuming = false; this.emit() }
  }

  cancel = async (): Promise<void> => {
    const run = this.detail?.latest_run
    if (this.disposed || this.cancelling || this.resuming || this.answering || !run || !occupied(run)) return
    this.cancelling = true; this.error = null; this.emit()
    try {
      const ended = await this.transport.cancelRun(run.run_id, this.lifetime.signal)
      if (this.disposed) return
      // Drain an older read before applying the terminal response and taking a fresh snapshot.
      if (this.syncFlight) await this.syncFlight
      if (this.disposed) return
      if (this.detail?.latest_run?.run_id === ended.run_id &&
          this.detail.latest_run.resume_time === ended.resume_time) {
        this.detail = { ...this.detail, latest_run: ended }
        this.finishToolCalls(ended)
      }
      await this.refresh(true)
    } catch (error) {
      if (!this.disposed) this.error = messageOf(error)
    } finally { this.cancelling = false; this.emit() }
  }

  dispose() {
    this.disposed = true; this.lifetime.abort(); this.streamAbort?.abort()
    if (this.retry) clearTimeout(this.retry)
    this.listeners.clear()
  }
}
