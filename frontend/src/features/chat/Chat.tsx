import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ArrowDown, ArrowUp, Check, ChevronDown, Copy, Layers, LoaderCircle, Sparkles, Square, Wrench } from 'lucide-react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../../api/resources'
import { chooseModel, sessionModel } from '../../lib/preferences'
import { messageOf } from '../../api/errors'
import { Conversation } from './conversation'
import { groupTimeline, latestWorkLine, type WorkGroup } from './timeline'
import type { Message, ToolMessage } from '../../api/types'
import styles from './Chat.module.css'

function argumentsText(value: string) { try { return JSON.stringify(JSON.parse(value), null, 2) } catch { return value } }
interface Fold { open: boolean; toggle: () => void }
function Disclosure({ label, icon, fold, active = false, preview, children }: { label: string; icon: React.ReactNode; fold: Fold; active?: boolean; preview?: string | null; children: React.ReactNode }) {
  const id = useId()
  const showPreview = active && !fold.open && !!preview
  return <>
    <button type="button" className={`${styles.toolTitle} ${active ? styles.processActive : ''}`} aria-label={label}
      aria-describedby={showPreview ? `${id}-preview` : undefined} aria-expanded={fold.open} aria-controls={id} onClick={fold.toggle}>
      <span className={styles.processIcon} aria-hidden="true">{icon}</span>
      <span className={styles.processHeading}>
        <span className={styles.processLabel}>{label}</span>
        {showPreview && <span className={styles.workPreview} id={`${id}-preview`}>
          <span className={styles.previewBracket}>(</span>
          <span className={styles.previewViewport} title={preview}>
            <span key={preview} className={styles.previewLine}>{preview}</span>
          </span>
          <span className={styles.previewBracket}>)</span>
        </span>}
      </span>
      <ChevronDown size={13} className={`${styles.chevron} ${fold.open ? styles.chevronOpen : ''}`} />
    </button>
    <div id={id} hidden={!fold.open}>{fold.open && children}</div>
  </>
}
function AssistantMarkdown({ text }: { text: string }) {
  return <Markdown remarkPlugins={[remarkGfm]} skipHtml components={{
    a: ({ children, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer">{children}</a>,
  }}>{text}</Markdown>
}
function ToolCall({ message, fold }: { message: ToolMessage; fold: Fold }) {
  const text = argumentsText(message.arguments)
  return <Disclosure label={message.tool_name} icon={<Wrench size={14} />} fold={fold}>
    {text && <pre>{text}</pre>}
  </Disclosure>
}
function MessageCard({ message }: { message: Message }) {
  if (message.role === 'tool') return null
  if (message.role === 'assistant' && !message.content) return null
  return <article data-seq={message.seq} aria-label={message.role === 'user' ? '用户消息' : message.role === 'assistant' ? '助手消息' : '工具调用'}
    className={`${styles.message} ${styles[message.role]}`}>
    {message.role === 'assistant' ? <AssistantMarkdown text={message.content} /> : message.content}
  </article>
}

function WorkCard({ group, fold }: { group: WorkGroup; fold: (keys: string[]) => Fold }) {
  return <section className={styles.work} aria-label="工作过程">
    <Disclosure label={group.active ? '正在工作' : '已工作'} icon={<Layers size={16} />} active={group.active}
      preview={group.active ? latestWorkLine(group) : null}
      fold={fold(group.items.map(item => `work:${item.message.seq}`))}>
      <div className={styles.workContents}>{group.items.map(item => item.kind === 'tool'
        ? <div key={`tool:${item.message.seq}`} data-anchor={`process:${item.message.seq}`} data-members={`process:${item.message.seq}`} data-seq={item.message.seq} className={styles.tool} aria-label="工具调用" aria-busy={item.active}>
          <ToolCall message={item.message} fold={fold([`tool:${item.message.seq}`])} />
        </div>
        : <div key={`thinking:${item.message.seq}`} data-anchor={`process:${item.message.seq}`} data-members={`process:${item.message.seq}`} className={styles.thinking}>
          <Disclosure label={item.active ? '正在思考' : '已思考'} icon={<Sparkles size={16} />} active={item.active} fold={fold([`thinking:${item.message.seq}`])}>
            <div className={styles.reasoning}>{item.message.reasoning?.map((part, index) =>
              <div className={styles.assistant} key={index}><AssistantMarkdown text={part.text} /></div>)}</div>
          </Disclosure>
        </div>)}</div>
    </Disclosure>
  </section>
}

function UserIdBar({ owner, sessionId }: { owner: string; sessionId: number }) {
  const [copied, setCopied] = useState<string | null>(null)
  async function copy(key: string, value: string) {
    try { await navigator.clipboard.writeText(value); setCopied(key); setTimeout(() => setCopied(old => old === key ? null : old), 1500) } catch { /* 剪贴板不可用时忽略 */ }
  }
  const item = (key: string, label: string, value: string) => <>
    <span>{label}: {value}</span>
    <button aria-label={`复制 ${label}`} onClick={() => void copy(key, value)}>
      {copied === key ? <Check size={13} /> : <Copy size={13} />}
    </button>
  </>
  return <div className={styles.userIdBar}>
    {item('user', 'user_id', owner)}
    {item('session', 'session_id', String(sessionId))}
  </div>
}

export function Chat({ owner, sessionId, onMissing }: { owner: string; sessionId: number; onMissing: () => void }) {
  const conversation = useMemo(() => new Conversation(sessionId), [sessionId])
  const snapshot = useSyncExternalStore(conversation.subscribe, conversation.getSnapshot)
  const canCancel = snapshot.detail?.latest_run?.status === 'waiting' || snapshot.detail?.latest_run?.status === 'running'
  const catalog = useQuery({ queryKey: ['models', owner], queryFn: ({ signal }) => api.models(signal) })
  const [model, setModel] = useState('')
  const [draft, setDraft] = useState('')
  const [following, setFollowing] = useState(true)
  const follow = useRef(true)
  const scroller = useRef<HTMLDivElement>(null)
  const textarea = useRef<HTMLTextAreaElement>(null)
  const programmatic = useRef(false)
  const lastTop = useRef(0)
  const anchor = useRef<{ key: string; member: string; offset: number } | null>(null)
  // Member identities survive a leading partial group merging with an older page.
  const folds = useMemo(() => new Map<string, { open: boolean; order: number }>(), [sessionId])
  const foldOrder = useRef(0)
  const [, setFoldVersion] = useState(0)
  function fold(keys: string[]): Fold {
    const latest = keys.map(key => folds.get(key)).filter(value => value !== undefined).sort((a, b) => b.order - a.order)[0]
    const open = latest?.open ?? false
    return { open, toggle: () => {
      const value = { open: !open, order: ++foldOrder.current }
      keys.forEach(key => folds.set(key, value))
      setFoldVersion(value.order)
    } }
  }

  useEffect(() => {
    void conversation.refresh()
    const visible = () => { if (document.visibilityState === 'visible') void conversation.refresh() }
    document.addEventListener('visibilitychange', visible)
    return () => { document.removeEventListener('visibilitychange', visible); conversation.dispose() }
  }, [conversation])
  useEffect(() => { if (snapshot.missing) onMissing() }, [snapshot.missing, onMissing])
  useEffect(() => { if (catalog.data) setModel(sessionModel(owner, sessionId, catalog.data)) }, [catalog.data, owner, sessionId])
  const setFollow = useCallback((value: boolean) => { follow.current = value; setFollowing(value) }, [])
  function moveScroll(fn: (element: HTMLDivElement) => void) {
    if (!scroller.current) return
    programmatic.current = true; fn(scroller.current); lastTop.current = scroller.current.scrollTop
    requestAnimationFrame(() => { programmatic.current = false })
  }
  useLayoutEffect(() => {
    const element = scroller.current
    if (!element) return
    if (anchor.current) {
      const saved = anchor.current
      const candidates = [...element.querySelectorAll<HTMLElement>('[data-anchor]')]
        .filter(row => row.dataset.members?.split(',').includes(saved.member))
      const row = candidates.find(row => row.dataset.anchor === saved.key) ?? candidates[0]
      if (row) moveScroll(el => { el.scrollTop += row.getBoundingClientRect().top - el.getBoundingClientRect().top - saved.offset })
      if (!snapshot.loadingOlder) anchor.current = null
    } else if (follow.current) moveScroll(el => { el.scrollTop = el.scrollHeight })
  }, [snapshot.messages, snapshot.pending, snapshot.loadingOlder])
  useLayoutEffect(() => {
    if (textarea.current) { textarea.current.style.height = 'auto'; textarea.current.style.height = `${Math.min(textarea.current.scrollHeight, 200)}px` }
  }, [draft])
  async function older() {
    const element = scroller.current
    if (!element || snapshot.loadingOlder || snapshot.syncing || !snapshot.hasMore || follow.current) return
    const viewport = element.getBoundingClientRect()
    const visible = [...element.querySelectorAll<HTMLElement>('[data-anchor]')].filter(row => {
      const rect = row.getBoundingClientRect()
      return rect.height > 0 && rect.bottom > viewport.top && rect.top < viewport.bottom
    })
    const row = visible.find(row => !visible.some(child => child !== row && row.contains(child)))
    if (row) anchor.current = { key: row.dataset.anchor!, member: row.dataset.members!.split(',').at(-1)!, offset: row.getBoundingClientRect().top - element.getBoundingClientRect().top }
    await conversation.loadOlder()
  }
  async function send() {
    if (!draft.trim() || !model || snapshot.busy || !snapshot.ready) return
    const submitted = draft
    if (await conversation.send(submitted, model)) setDraft(current => current === submitted ? '' : current)
  }
  const rows: { key: string; seq: number; members: string; node: React.ReactNode }[] = groupTimeline(snapshot.messages, snapshot.pending ? null : snapshot.activity).map(row => ({
    key: row.key, seq: row.seq, members: row.kind === 'work' ? row.items.map(item => `process:${item.message.seq}`).join(',') : `message:${row.seq}`,
    node: row.kind === 'work' ? <WorkCard group={row} fold={fold} /> : <MessageCard message={row.message} />,
  }))
  if (snapshot.pending) rows.push({ key: 'pending', seq: snapshot.pending.afterSeq + 0.5, members: 'pending',
    node: <article className={`${styles.message} ${styles.user} ${snapshot.pending.failed ? styles.failed : ''}`} aria-label="待确认用户消息">{snapshot.pending.content}</article> })
  rows.sort((a, b) => a.seq - b.seq)
  return <main className={styles.chat} aria-label="聊天">
    <UserIdBar owner={owner} sessionId={sessionId} />
    <div className={styles.scrollArea} ref={scroller} aria-label="消息列表" tabIndex={0}
      onWheel={event => { if (event.deltaY < 0 && scroller.current && scroller.current.scrollHeight > scroller.current.clientHeight) setFollow(false) }}
      onScroll={() => {
        const element = scroller.current!
        if (!programmatic.current) {
          if (element.scrollTop < lastTop.current - 1) setFollow(false)
          if (element.scrollHeight - element.clientHeight - element.scrollTop <= 2) setFollow(true)
        }
        lastTop.current = element.scrollTop
        if (element.scrollTop < 160) void older()
      }}>
      <div className={styles.timeline}>{rows.map(row => <div key={row.key} data-anchor={row.key} data-members={row.members}>{row.node}</div>)}</div>
    </div>
    <div className={styles.footer}>
      {!following && <button className={styles.latest} onClick={() => { anchor.current = null; setFollow(true); moveScroll(el => { el.scrollTop = el.scrollHeight }) }}><ArrowDown size={14} />回到最新</button>}
      {(snapshot.error || catalog.error) && <div className="error" role="alert">{snapshot.error ?? messageOf(catalog.error)}</div>}
      {!snapshot.error && !snapshot.busy && snapshot.detail?.latest_run?.status === 'cancelled' && <div role="status" className={styles.cancelled}>已取消</div>}
      <div className={styles.composer}>
        <textarea ref={textarea} aria-label="消息输入" value={draft} rows={2} onChange={e => setDraft(e.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229) { event.preventDefault(); void send() }
          }} />
        <div className={styles.toolbar}>
          <div className={styles.model}>
            <select aria-label="模型" value={model} disabled={!catalog.data} onChange={event => {
              setModel(event.target.value); chooseModel(owner, sessionId, event.target.value)
            }}>{catalog.data?.models.map(item => <option key={item.id} value={item.id}>{item.id}</option>)}</select>
            <ChevronDown size={13} />
          </div>
          {canCancel || snapshot.cancelling
            ? <button className={styles.send} aria-label={snapshot.cancelling ? '正在取消' : '取消生成'} onClick={() => void conversation.cancel()} disabled={snapshot.cancelling}>
              {snapshot.cancelling ? <LoaderCircle size={18} className="spin" /> : <Square size={14} fill="currentColor" />}
            </button>
            : <button className={styles.send} aria-label="发送消息" onClick={() => void send()} disabled={!draft.trim() || !model || snapshot.busy || !snapshot.ready}>
              {snapshot.busy ? <LoaderCircle size={18} className="spin" /> : <ArrowUp size={19} strokeWidth={2} />}
            </button>}
        </div>
      </div>
    </div>
  </main>
}
